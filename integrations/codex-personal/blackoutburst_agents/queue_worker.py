"""One explicit queue selection, one durable child worker, then exit.

A selection send is never blindly retried. Its read-only lookup reconciles the
same key; an absent record after an ambiguous send requires human review. The
child BoardWorker owns all ticket/model effects after dispatch, including a
bounded human question. Queue state contains no source text or model result.
"""
from __future__ import annotations

from pathlib import Path

from .board_reader import BoardReadError, _text
from .board_worker import BoardWorker
from .context import ContextScope, _profile, _hash
from .queue_journal import JournalError, _configuration


class _Stop(Exception):
    pass


class _Unavailable(Exception):
    pass


class QueueWorker:
    def __init__(self, journal, worker, client):
        self.journal, self.worker, self.client = journal, worker, client

    @staticmethod
    def _receipt(op=None, *, reason=None, pending=False, cached=False):
        stage = op['stage'] if op else 'needs_review'
        status = 'pending' if pending else stage if stage in ('complete', 'abandoned') else 'needs_review'
        payload = op['payload'] if op else {}
        if stage == 'complete' and payload.get('selection_reason') == 'no_ready_ticket':
            status = 'idle'
        result = {'status': status, 'stage': stage, 'reason': reason or payload.get('reason', 'queue_review_required'), 'cached': cached}
        if op:
            result['operation_id'] = op['id']
        for key in ('ticket_id', 'worker_operation_id'):
            if payload.get(key):
                result[key] = payload[key]
        return result

    @staticmethod
    def _cancel(callback):
        if callback is not None:
            try:
                cancelled = callback()
            except Exception:
                raise _Stop('cancellation_state_unknown') from None
            if cancelled:
                raise _Stop('cancel_requested')

    def _save(self, op, stage, **fields):
        return self.journal.update(op['id'], op['revision'], stage=stage,
                                   payload={**op['payload'], **fields})

    def _call(self, name, args):
        try:
            return self.client.call(name, args)
        except BoardReadError as error:
            if error.code in ('invalid_arguments', 'tool_not_allowed'):
                raise _Stop('queue_arguments_invalid') from None
            raise _Unavailable() from None
        except Exception:
            raise _Unavailable() from None

    def _registration(self, op, *, selection=False):
        scope = op['scope']
        if self.client.endpoint != scope['board_endpoint'] or self.client.session_id != scope['board_session_id']:
            raise _Stop('connection_scope_mismatch')
        try:
            result = self.client.register_for_selection() if selection else self.client.register()
        except Exception:
            raise _Unavailable() from None
        if not isinstance(result, dict):
            raise _Stop('registration_invalid')
        identity, session, policy = [result.get(key) for key in ('identity', 'session', 'policy')]
        if not all(isinstance(value, dict) for value in (identity, session, policy)):
            raise _Stop('registration_invalid')
        if any(policy.get(key) is not True for key in ('enabled', 'allow_read', 'allow_requests')):
            raise _Stop('work_permission_denied')
        if policy.get('autonomy') != 'queue' or (selection and session.get('autonomous') is not True):
            raise _Stop('queue_permission_denied')
        if (identity.get('id') != scope['agent_id'] or identity.get('owner_id') != scope['owner_id']
                or session.get('id') != scope['board_session_id'] or session.get('agent_id') != scope['agent_id']
                or session.get('owner_id') != scope['owner_id']
                or not isinstance(identity.get('device_id'), str) or not identity['device_id']
                or identity['device_id'] != session.get('device_id')
                or not isinstance(identity.get('agent_kind'), str) or not identity['agent_kind']
                or identity['agent_kind'] != session.get('agent_kind')):
            raise _Stop('registration_scope_mismatch')
        try:
            fingerprint = _hash(_profile(result, ContextScope(owner_id=scope['owner_id'], agent_id=scope['agent_id'],
                project_id=op['configuration']['project_id'], ticket_id='queue-selection')))
        except Exception:
            raise _Stop('registration_invalid') from None
        if op['payload'].get('profile_sha256') and fingerprint != op['payload']['profile_sha256']:
            raise _Stop('profile_changed')
        return result, fingerprint

    def _project(self, op):
        scope, config = op['scope'], op['configuration']
        project = self._call('get_project', {'project_id': config['project_id']})
        if (not isinstance(project, dict) or project.get('id') != config['project_id']
                or type(project.get('version')) is not int or not 1 <= project['version'] < 2 ** 63
                or not isinstance(project.get('participants'), list)
                or scope['owner_id'] not in project['participants'] or project.get('archived') is not False):
            raise _Stop('project_scope_mismatch')
        if config.get('human_owner_id') and config['human_owner_id'] not in project['participants']:
            raise _Stop('human_owner_not_in_project')
        pinned = {'id': project['id'], 'version': project['version'], 'participants': project['participants']}
        if op['payload'].get('project') and pinned != op['payload']['project']:
            raise _Stop('project_changed')
        return pinned

    def _lookup(self, op):
        result = self._call('lookup_ticket_selection', {'idempotency_key': op['payload']['selection_key'],
                                                       'project_id': op['configuration']['project_id']})
        if not isinstance(result, dict) or type(result.get('found')) is not bool:
            raise _Stop('selection_lookup_invalid')
        if result['found'] is False:
            if set(result) != {'found'}:
                raise _Stop('selection_lookup_invalid')
            return None
        return result

    def _selected(self, op, result, registration):
        if not isinstance(result, dict) or type(result.get('claimed')) is not bool or type(result.get('retried')) is not bool:
            raise _Stop('selection_result_invalid')
        if not result['claimed']:
            reason = result.get('reason')
            if reason not in ('no_ready_ticket', 'unfinished_work', 'waiting_on_human'):
                raise _Stop('selection_result_invalid')
            return self._save(op, 'complete' if reason == 'no_ready_ticket' else 'needs_review',
                              selection_reason=reason, selection_confirmed=True, reason=reason)
        ticket = result.get('ticket')
        if (not isinstance(ticket, dict) or not _text(ticket.get('id'), 256)
                or ticket.get('project_id') != op['configuration']['project_id']
                or type(ticket.get('version')) is not int or not 1 <= ticket['version'] < 2 ** 63):
            raise _Stop('selection_result_invalid')
        try:
            ticket['id'].encode('utf-8', 'strict')
        except UnicodeError:
            raise _Stop('selection_result_invalid') from None
        # Save the selected ID before any further remote call or dispatch. A
        # subsequent claim/version mismatch never turns into another selection.
        op = self._save(op, 'selected', ticket_id=ticket['id'], selected_version=ticket['version'], selection_confirmed=True)
        self._assert_selected(op, ticket, registration)
        return op

    @staticmethod
    def _assert_selected(op, ticket, registration):
        scope = {**op['scope'], 'project_id': op['configuration']['project_id'], 'ticket_id': op['payload']['ticket_id']}
        if (ticket.get('id') != scope['ticket_id'] or ticket.get('project_id') != scope['project_id']
                or ticket.get('version') != op['payload']['selected_version']):
            raise _Stop('selection_version_changed')
        if not BoardWorker._own_claim(ticket, registration, scope, remaining=op['configuration']['timeout'] + 10):
            raise _Stop('claim_lost_or_expired')
        if (ticket.get('status') != 'in_progress' or ticket.get('blocked_by') or ticket.get('waiting_on_human')
                or ticket.get('assignee_owner_id') not in (None, scope['owner_id'])
                or ticket.get('assignee_agent_id') not in (None, scope['agent_id'])):
            raise _Stop('ticket_no_longer_runnable')

    def _advance(self, op, should_cancel):
        self._cancel(should_cancel)
        registration, profile_hash = self._registration(op)
        project = self._project(op)
        if op['stage'] == 'new':
            # Establish read-only lookup support before allowing a new effect.
            if self._lookup(op) is not None:
                raise _Stop('unexpected_existing_selection')
            op = self._save(op, 'selecting', profile_sha256=profile_hash, project=project)
            self._cancel(should_cancel)
            registration, _ = self._registration(op, selection=True)
            self._project(op)
            op = self._save(op, 'selecting', selection_attempted=True)
            try:
                selected = self._call('claim_next_ticket', {'idempotency_key': op['payload']['selection_key'],
                                                          'project_id': op['configuration']['project_id']})
            except _Unavailable:
                selected = self._lookup(op)
                if selected is None:
                    raise _Stop('selection_unconfirmed')
            op = self._selected(op, selected, registration)
        elif op['stage'] == 'selecting':
            selected = self._lookup(op)
            if selected is None:
                # The previous process may have died before OR after sending;
                # absence cannot prove a delayed remote write was cancelled.
                raise _Stop('selection_unconfirmed')
            op = self._selected(op, selected, registration)
        if op['stage'] in ('complete', 'needs_review'):
            return self._receipt(op)
        if op['stage'] == 'selected':
            self._cancel(should_cancel)
            registration, _ = self._registration(op)
            self._project(op)
            response = self._call('get_ticket', {'ticket_id': op['payload']['ticket_id']})
            if not isinstance(response, dict) or not isinstance(response.get('ticket'), dict):
                raise _Stop('selected_ticket_invalid')
            self._assert_selected(op, response['ticket'], registration)
            config = op['configuration']
            child_scope = {**op['scope'], 'project_id': config['project_id'], 'ticket_id': op['payload']['ticket_id']}
            child_config = {key: value for key, value in config.items() if key != 'project_id'}
            op = self._save(op, 'selected', worker_preparing=True)
            # Preallocate the exact child without executing any of its effects.
            # Its ID is therefore known even if worker.run never returns.
            with self.worker.journal.lock(child_scope):
                child = self.worker.journal.begin(child_scope, op['payload']['worker_request_id'], child_config)
                if child['stage'] != 'new':
                    raise _Stop('unexpected_worker_operation')
                op = self._save(op, 'dispatched', worker_operation_id=child['id'])
        self._cancel(should_cancel)
        registration, _ = self._registration(op)
        self._project(op)
        config = op['configuration']
        scope = {**op['scope'], 'project_id': config['project_id'], 'ticket_id': op['payload']['ticket_id']}
        kwargs = {'human_owner_id': config['human_owner_id']} if config.get('human_owner_id') else {}
        result = self.worker.run(scope=scope, request_id=op['payload']['worker_request_id'],
            instruction=config['instruction'], workspace=config['workspace'], executable=config['executable'],
            model=config['model'], timeout=config['timeout'], should_cancel=should_cancel, **kwargs)
        if not isinstance(result, dict) or not result.get('operation_id'):
            raise _Stop('worker_operation_unconfirmed')
        with self.worker.journal.lock(scope):
            proof = self.worker.journal.get(result['operation_id'])
            op = self.journal.finish_worker(op['id'], op['revision'], result, proof)
        return self._receipt(op, pending=op['stage'] == 'dispatched')

    def reconcile_selection(self, operation_id):
        """Read the original selection key; never claim or invoke a model.

        A confirmed result is recorded for inspection. It deliberately remains
        in review until the human reconciles the claim and explicitly abandons
        this local request. An absent result cannot cancel an in-flight send.
        """
        op = None
        try:
            op = self.journal.get(operation_id)
            with self.journal.lock(op['scope']):
                op = self.journal.get(operation_id)
                if op['stage'] in ('complete', 'abandoned'):
                    return self._receipt(op, cached=True)
                if op['payload'].get('selection_confirmed') is True:
                    return self._receipt(op, cached=True)
                if op['stage'] not in ('selecting', 'needs_review') or not op['payload'].get('selection_attempted'):
                    raise _Stop('selection_not_attempted')
                if (self.client.endpoint != op['scope']['board_endpoint']
                        or self.client.session_id != op['scope']['board_session_id']):
                    raise _Stop('connection_scope_mismatch')
                # Normal registration is non-autonomous. The read-only lookup
                # rechecks current membership/profile access on the server.
                try:
                    registration = self.client.register()
                except Exception:
                    raise _Unavailable() from None
                identity = registration.get('identity', {}) if isinstance(registration, dict) else {}
                session = registration.get('session', {}) if isinstance(registration, dict) else {}
                policy = registration.get('policy', {}) if isinstance(registration, dict) else {}
                if (identity.get('id') != op['scope']['agent_id'] or identity.get('owner_id') != op['scope']['owner_id']
                        or session.get('id') != op['scope']['board_session_id']
                        or session.get('agent_id') != op['scope']['agent_id']
                        or session.get('owner_id') != op['scope']['owner_id']
                        or policy.get('enabled') is not True or policy.get('allow_read') is not True):
                    raise _Stop('selection_lookup_denied')
                result = self._lookup(op)
                if result is not None:
                    op = self.journal.record_selection(op['id'], op['revision'], result)
                if op['stage'] != 'needs_review':
                    op = self._save(op, 'needs_review', reason='selection_reconciled' if result is not None else 'selection_unconfirmed')
                elif result is not None:
                    op = self._save(op, 'needs_review', reason='selection_reconciled')
                return self._receipt(op, reason='selection_reconciled' if result is not None else 'selection_unconfirmed')
        except _Unavailable:
            return self._receipt(op, reason='board_unavailable', pending=True)
        except _Stop as error:
            return self._receipt(op, reason=str(error))
        except Exception:
            return self._receipt(op, reason='queue_state_unavailable', pending=op is not None)

    def run(self, *, scope, request_id, project_id, instruction, workspace, executable,
            model, timeout=120, human_owner_id=None, should_cancel=None):
        op = None
        try:
            config = {'project_id': project_id, 'instruction': instruction,
                      'workspace': str(Path(workspace).expanduser().resolve(strict=True)),
                      'executable': str(Path(executable).expanduser().resolve(strict=True)),
                      'model': model, 'timeout': timeout}
            if human_owner_id is not None:
                config['human_owner_id'] = human_owner_id
            _configuration(config)
            if not Path(config['workspace']).is_dir() or not Path(config['executable']).is_file():
                raise _Stop('invalid_queue_request')
            with self.journal.lock(scope):
                op = self.journal.begin(scope, request_id, config)
                if op['stage'] in ('complete', 'needs_review', 'abandoned'):
                    return self._receipt(op, cached=True)
                try:
                    return self._advance(op, should_cancel)
                except _Stop as error:
                    op = self.journal.get(op['id'])
                    op = self._save(op, 'needs_review', reason=str(error))
                    return self._receipt(op)
                except _Unavailable:
                    return self._receipt(self.journal.get(op['id']), reason='board_unavailable', pending=True)
        except JournalError as error:
            if op is None:
                return self._receipt(reason=error.code)
            return self._receipt(op, reason='queue_state_unavailable', pending=True)
        except (ValueError, TypeError, OSError, UnicodeError, _Stop):
            return self._receipt(op, reason='invalid_queue_request', pending=op is not None)
        except Exception:
            return self._receipt(op, reason='queue_state_unavailable', pending=op is not None)
