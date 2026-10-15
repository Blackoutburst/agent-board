"""Finite, explicitly invoked activity drain for one human-selected ticket.

This is not a listener or task selector. Events are wake-up data, never model
instructions. The private instruction/configuration is pinned on subscription,
while durable event IDs and a stable batch request key prevent duplicate work.

Normally a full page budget defers execution until ingestion catches up. If the
bounded durable buffer is full, a confirmed existing batch may run first to
free capacity. The rejected page keeps its old cursor and is read again later;
the worker still refreshes live ticket context before any model or publication.
"""
from __future__ import annotations

import math
from pathlib import Path
import time

from .event_journal import EventJournalError


class _Abort(Exception):
    def __init__(self, reason, status='blocked'):
        self.reason, self.status = reason, status


class EventDrainer:
    def __init__(self, event_journal, worker, client):
        self.journal, self.worker, self.client = event_journal, worker, client

    @staticmethod
    def _cancel(should_cancel):
        if should_cancel is not None:
            try:
                cancelled = should_cancel()
            except Exception:
                raise _Abort('drain_state_unavailable') from None
            if cancelled:
                raise _Abort('cancel_requested', 'cancelled')

    def _register(self, scope, *, execute):
        if self.client.endpoint != scope['board_endpoint'] or self.client.session_id != scope['board_session_id']:
            raise _Abort('scope_mismatch')
        try:
            registration = self.client.register()
        except Exception:
            raise _Abort('board_unavailable', 'pending') from None
        if not isinstance(registration, dict):
            raise _Abort('scope_mismatch')
        identity, session, policy = [registration.get(key) for key in ('identity', 'session', 'policy')]
        if not all(isinstance(value, dict) for value in (identity, session, policy)):
            raise _Abort('scope_mismatch')
        if policy.get('enabled') is not True or policy.get('allow_read') is not True:
            raise _Abort('read_permission_denied')
        if execute and policy.get('allow_requests') is not True:
            raise _Abort('work_permission_denied')
        if (identity.get('id') != scope['agent_id'] or identity.get('owner_id') != scope['owner_id']
                or session.get('owner_id') != scope['owner_id'] or session.get('agent_id') != scope['agent_id']
                or session.get('id') != scope['board_session_id']
                or not isinstance(identity.get('device_id'), str) or not identity['device_id']
                or session.get('device_id') != identity['device_id']
                or session.get('agent_kind') != identity.get('agent_kind')
                or not isinstance(identity.get('agent_kind'), str) or not identity['agent_kind']
                or not isinstance(identity.get('name'), str)):
            raise _Abort('scope_mismatch')
        return registration

    def _call(self, name, args):
        if name not in ('list_activity', 'get_ticket'):
            raise _Abort('invalid_drain_request')
        try:
            return self.client.call(name, args)
        except Exception:
            raise _Abort('board_unavailable', 'pending') from None

    def _ticket(self, scope):
        response = self._call('get_ticket', {'ticket_id': scope['ticket_id']})
        if not isinstance(response, dict):
            raise _Abort('scope_mismatch')
        project, ticket = response.get('project'), response.get('ticket')
        if (not isinstance(project, dict) or not isinstance(ticket, dict)
                or project.get('id') != scope['project_id'] or ticket.get('project_id') != scope['project_id']
                or ticket.get('id') != scope['ticket_id'] or not isinstance(project.get('participants'), list)
                or scope['owner_id'] not in project['participants']):
            raise _Abort('scope_mismatch')
        return project, ticket

    @staticmethod
    def _page(response, scope, after, limit):
        if not isinstance(response, list) or len(response) > limit:
            raise _Abort('invalid_activity_page')
        result, previous = [], after
        for event in response:
            if (not isinstance(event, dict) or type(event.get('id')) is not int or event['id'] <= previous
                    or event['id'] >= 2 ** 63 or event.get('ticket_id') != scope['ticket_id'] or event.get('project_id') != scope['project_id']
                    or not isinstance(event.get('event'), str) or not 1 <= len(event['event']) <= 100
                    or any(ord(char) < 32 for char in event['event'])
                    or type(event.get('created_at')) not in (int, float) or not math.isfinite(event['created_at'])):
                raise _Abort('invalid_activity_page')
            actor = event.get('actor')
            if (not isinstance(actor, dict) or actor.get('kind') not in ('human', 'agent')
                    or not isinstance(actor.get('owner_id'), str) or not 1 <= len(actor['owner_id']) <= 256
                    or any(ord(char) < 32 for char in actor['owner_id'])):
                raise _Abort('invalid_activity_page')
            result.append({'id': event['id'], 'ticket_id': event['ticket_id'], 'project_id': event['project_id'],
                           'event': event['event'], 'actor': {'kind': actor['kind'], 'owner_id': actor['owner_id']},
                           'created_at': event['created_at']})
            if event['event'] == 'human_input_resolved':
                detail = event.get('detail')
                if isinstance(detail, dict) and 'request_id' in detail:
                    # Only this opaque identifier can associate an answer with
                    # the original batch. No message/question/answer is stored.
                    result[-1]['request_id'] = detail['request_id']
            previous = event['id']
        return result

    @staticmethod
    def _ready(project, ticket, scope, registration):
        if (project.get('archived') is True or ticket.get('status') not in ('todo', 'in_progress')
                or ticket.get('blocked_by') or ticket.get('waiting_on_human')
                or ticket.get('assignee_owner_id') not in (None, scope['owner_id'])
                or ticket.get('assignee_agent_id') not in (None, scope['agent_id'])):
            return 'ticket_not_ready'
        claimant = ticket.get('claimant')
        if claimant is None:
            return None if ticket.get('status') == 'todo' else 'claim_not_owned'
        exact = {'kind': 'agent', 'owner_id': scope['owner_id'], 'agent_id': scope['agent_id'],
                 'session_id': scope['board_session_id'], 'device_id': registration['identity']['device_id']}
        lease = ticket.get('lease_expires_at')
        if (not isinstance(claimant, dict) or not all(claimant.get(key) == value for key, value in exact.items())
                or type(lease) not in (int, float) or not math.isfinite(lease) or lease <= time.time()
                or ticket.get('claim_expired') is True):
            return 'claim_not_owned'
        return None

    def drain(self, *, scope, instruction, workspace, executable, model=None, timeout=120,
              max_pages=4, limit=50, execute=False, after=0, should_cancel=None, human_owner_id=None):
        subscription = batch = None
        pages = ingested = 0
        buffer_full = False

        def receipt(status, reason, *, worker_operation_id=None):
            value = {'status': status, 'reason': reason, 'after': subscription['after'] if subscription else (after if type(after) is int and 0 <= after < 2 ** 63 else 0),
                     'pages': pages, 'ingested_count': ingested}
            if subscription:
                value['subscription_id'] = subscription['id']
            if batch:
                value['batch_id'] = batch['id']
            if worker_operation_id is not None:
                value['worker_operation_id'] = worker_operation_id
            return value

        try:
            try:
                if (type(max_pages) is not int or not 1 <= max_pages <= 20
                        or type(limit) is not int or not 1 <= limit <= 200
                        or type(after) is not int or not 0 <= after < 2 ** 63 or type(execute) is not bool
                        or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 300
                        or not isinstance(instruction, str) or not instruction.strip()
                        or len(instruction.encode('utf-8')) > 12000 or '\x00' in instruction
                        or (model is not None and (not isinstance(model, str) or not 1 <= len(model) <= 256
                            or any(ord(char) < 32 or ord(char) == 127 for char in model)))):
                    raise _Abort('invalid_drain_request')
                configuration = {'instruction': instruction, 'workspace': str(Path(workspace).expanduser().resolve(strict=True)),
                                 'executable': str(Path(executable).expanduser().resolve(strict=True)),
                                 'model': model, 'timeout': timeout}
                if human_owner_id is not None:
                    configuration['human_owner_id'] = human_owner_id
                if not Path(configuration['workspace']).is_dir() or not Path(configuration['executable']).is_file():
                    raise _Abort('invalid_drain_request')
                if execute and self.worker is None:
                    raise _Abort('worker_unavailable')
            except (OSError, ValueError, TypeError, UnicodeError):
                raise _Abort('invalid_drain_request') from None
            with self.journal.lock(scope):
                self._cancel(should_cancel)
                self._register(scope, execute=execute)
                self._ticket(scope)  # Current project membership precedes ingestion.
                subscription = self.journal.open_subscription(scope, configuration, after=after)
                for _ in range(max_pages):
                    self._cancel(should_cancel)
                    page = self._page(self._call('list_activity', {'project_id': scope['project_id'],
                        'ticket_id': scope['ticket_id'], 'after': subscription['after'], 'limit': limit, 'order': 'asc'}),
                        scope, subscription['after'], limit)
                    try:
                        subscription = self.journal.append_page(subscription['id'], subscription['after'], page)
                    except EventJournalError as error:
                        if error.code != 'pending_event_limit':
                            raise
                        # append_page rolled back this entire page. Keep its
                        # cursor unchanged and process only durable events to
                        # free bounded capacity instead of retrying forever.
                        buffer_full = True
                        break
                    pages += 1
                    ingested += len(page)
                    if len(page) < limit:
                        break
                else:
                    # A full final page might be followed by more activity. Do
                    # not start a model with a partially ingested backlog.
                    return receipt('more_available', 'more_activity_available')
                batch = self.journal.prepare_batch(subscription['id'])
                if batch is None:
                    if buffer_full:
                        # A capacity refusal without a durable batch is not a
                        # reason to skip events or silently raise the limit.
                        return receipt('needs_review', 'event_buffer_full')
                    return receipt('idle', 'no_pending_events')
                if batch['status'] == 'needs_review':
                    return receipt('needs_review', 'event_batch_needs_review')
                if not execute:
                    return receipt('pending', 'event_buffer_full' if buffer_full else 'execution_not_requested')
                self._cancel(should_cancel)
                registration = self._register(scope, execute=True)
                project, ticket = self._ticket(scope)
                if not batch['dispatched']:
                    waiting = self._ready(project, ticket, scope, registration)
                    if waiting:
                        return receipt('waiting', waiting)
                self._cancel(should_cancel)
                # Persist dispatch before calling the worker. A retry of this
                # exact batch must reach its idempotent reconciliation even if
                # the first call already changed the ticket to review.
                batch = self.journal.start_batch(batch['id'])
                human_options = {'human_owner_id': human_owner_id} if human_owner_id is not None else {}
                result = self.worker.run(scope=scope, request_id=batch['request_id'],
                    instruction=configuration['instruction'], workspace=configuration['workspace'],
                    executable=configuration['executable'], model=model, timeout=timeout,
                    should_cancel=should_cancel, **human_options)
                if human_owner_id is not None and result.get('operation_id'):
                    # Read only the operation identified by this exact worker
                    # receipt, never adopt another operation by scope. Retain
                    # the work lock through the atomic event-link/receipt save.
                    with self.worker.journal.lock(scope):
                        operation = self.worker.journal.get(result['operation_id'])
                        batch = self.journal.finish_batch(batch['id'], result, operation=operation)
                else:
                    batch = self.journal.finish_batch(batch['id'], result)
                status = {'complete': 'complete', 'pending': 'pending'}.get(result.get('status'), 'needs_review')
                return receipt(status, 'worker_' + status, worker_operation_id=result.get('operation_id'))
        except _Abort as error:
            return receipt(error.status, error.reason)
        except EventJournalError as error:
            if error.code in ('invalid_configuration', 'subscription_configuration_conflict', 'invalid_scope'):
                return receipt('blocked', 'invalid_drain_request')
            if error.code in ('event_scope_mismatch', 'invalid_event', 'invalid_event_page', 'invalid_event_order'):
                return receipt('blocked', 'invalid_activity_page')
            return receipt('pending', 'drain_state_unavailable')
        except Exception:
            # Journal conflicts, storage failures and worker exceptions keep the
            # persisted batch available for inspection/retry without raw text.
            return receipt('pending', 'drain_state_unavailable')
