"""Finite activity ingestion with real journals/worker and fake board/model."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.event_drain import EventDrainer
from blackoutburst_agents.event_journal import EventJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.work_journal import WorkJournal


SCOPE = {'owner_id': 'owner', 'agent_id': 'agent', 'project_id': 'project', 'ticket_id': 'ticket',
         'board_session_id': 'session', 'board_endpoint': 'https://board.test/mcp'}


def event(identifier, kind='human', name='ticket_message_posted'):
    return {'id': identifier, 'ticket_id': 'ticket', 'project_id': 'project', 'event': name,
            'actor': {'kind': kind, 'owner_id': 'owner', 'private_ignored': 'PRIVATE_ACTOR_EXTRA'},
            'created_at': float(identifier), 'detail': {'body': 'PRIVATE_EVENT_BODY'}, 'title': 'PRIVATE_EVENT_TITLE'}


class Client:
    endpoint, session_id = SCOPE['board_endpoint'], SCOPE['board_session_id']

    def __init__(self):
        self.events, self.calls, self.registrations = [], [], 0
        self.on_register = self.after_page = None
        self.registration = {'identity': {'id': 'agent', 'owner_id': 'owner', 'device_id': 'device',
            'name': 'Test agent', 'agent_kind': 'codex'}, 'session': {'id': 'session', 'owner_id': 'owner',
            'device_id': 'device', 'agent_id': 'agent', 'agent_kind': 'codex', 'skills': []},
            'policy': {'enabled': True, 'allow_read': True, 'allow_requests': True}}
        self.project = {'id': 'project', 'version': 1, 'participants': ['owner'], 'name': 'Project', 'archived': False}
        self.ticket = {'id': 'ticket', 'project_id': 'project', 'thread_id': 'thread', 'version': 1,
                       'number': 1, 'title': 'Explicit task', 'status': 'todo', 'description': '',
                       'outcome': '', 'evidence': '', 'claimant': None, 'lease_expires_at': None,
                       'blocked_by': [], 'waiting_on_human': False}

    def register(self):
        self.registrations += 1
        if self.on_register:
            self.on_register(self.registrations)
        return deepcopy(self.registration)

    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        if name == 'list_activity':
            result = deepcopy([item for item in self.events if item['id'] > args['after']][:args['limit']])
            if self.after_page:
                self.after_page()
            return result
        if name == 'get_ticket':
            return deepcopy({'project': self.project, 'ticket': self.ticket})
        if name == 'get_thread':
            return {'thread': {'id': 'thread', 'title': 'Discussion', 'participants': ['owner'],
                'ticket': {'id': 'ticket', 'project_id': 'project', 'number': 1, 'title': 'Explicit task'}},
                'messages': [], 'next_before': None}
        if name == 'claim_ticket':
            if self.ticket['version'] != args['expected_version'] or self.ticket['claimant']:
                raise RuntimeError('conflict')
            self.ticket.update(version=self.ticket['version'] + 1, status='in_progress', lease_expires_at=time.time() + 1800,
                claimant={'kind': 'agent', 'owner_id': 'owner', 'agent_id': 'agent', 'device_id': 'device', 'session_id': 'session'})
            return deepcopy(self.ticket)
        if name == 'update_ticket':
            if self.ticket['version'] != args['expected_version']:
                raise RuntimeError('conflict')
            self.ticket.update({key: value for key, value in args['changes'].items() if key != 'release_claim'})
            self.ticket.update(version=self.ticket['version'] + 1, claimant=None, lease_expires_at=None)
            return deepcopy(self.ticket)
        raise AssertionError('Forbidden operation')


class EventDrainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.events = EventJournal(self.root / 'events')
        self.work = WorkJournal(self.root / 'work')
        self.runs = RunStore(self.root / 'runs')
        for state in (self.events, self.work, self.runs):
            self.addCleanup(state.close)
        self.client = Client()
        self.inferences = []
        self.after_model = None
        self.worker = BoardWorker(self.work, self.runs, self.client,
                                 runner_factory=lambda store: PersonalRunner(store, self.transport))
        self.drainer = EventDrainer(self.events, self.worker, self.client)

    def transport(self, settings, prompt, workspace, **callbacks):
        self.inferences.append(prompt)
        callbacks['on_started'](999999999)
        sid = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](sid)
        if self.after_model:
            self.after_model()
        return {'status': 'completed', 'reason': 'verified', 'session_id': sid,
                'text': 'PRIVATE_MODEL_RESULT', 'verification': {'cleanup_confirmed': True}}

    def drain(self, drainer=None, **overrides):
        args = {'scope': SCOPE, 'instruction': 'PRIVATE_HUMAN_INSTRUCTION', 'workspace': self.workspace,
                'executable': sys.executable, 'timeout': 10}
        args.update(overrides)
        return (drainer or self.drainer).drain(**args)

    def test_no_event_never_calls_model_even_with_execute(self):
        value = self.drain(execute=True)
        self.assertEqual((value['status'], value['reason']), ('idle', 'no_pending_events'))
        self.assertEqual(self.inferences, [])
        self.assertFalse(any(name == 'claim_ticket' for name, _ in self.client.calls))

    def test_preview_ingests_durably_without_execution_and_omits_private_content(self):
        self.client.events = [event(3), event(8)]
        value = self.drain()
        self.assertEqual(value['status'], 'pending')
        self.assertEqual(value['after'], 8)
        self.assertEqual(self.inferences, [])
        self.assertNotIn('PRIVATE_', json.dumps(value))
        self.assertEqual(self.drain()['after'], 8)
        self.assertEqual(self.drain(execute=True)['status'], 'complete')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(self.drain(execute=True)['status'], 'idle')
        self.assertEqual(len(self.inferences), 1)

    def test_finite_page_cap_defers_model_until_backlog_is_drained(self):
        self.client.events = [event(n) for n in (2, 7, 12, 20, 35)]
        first = self.drain(max_pages=2, limit=2, execute=True)
        self.assertEqual((first['status'], first['after'], first['pages']), ('more_available', 20, 2))
        self.assertEqual(self.inferences, [])
        second = self.drain(max_pages=2, limit=2, execute=True)
        self.assertEqual((second['status'], second['after']), ('complete', 35))
        self.assertEqual(len(self.inferences), 1)
        reads = [args for name, args in self.client.calls if name == 'list_activity']
        self.assertEqual([args['after'] for args in reads], [0, 7, 20])
        self.assertTrue(all(args['order'] == 'asc' and args['ticket_id'] == 'ticket' and args['project_id'] == 'project' for args in reads))

    def test_all_agent_events_and_unrecognized_human_events_are_ignored(self):
        self.client.events = [event(1, 'agent'), event(3, 'agent', 'ticket_updated'),
                              event(5, 'agent', 'ticket_unblocked'), event(8, 'human', 'ticket_progress')]
        value = self.drain(execute=True)
        self.assertEqual((value['status'], value['after']), ('idle', 8))
        self.assertEqual(self.inferences, [])
        self.assertEqual(self.drain(execute=True)['status'], 'idle')

    def test_human_triggers_are_coalesced_into_one_batch(self):
        self.client.events = [event(i + 1, 'human', kind) for i, kind in enumerate(
            ('ticket_created', 'ticket_updated', 'ticket_message_posted', 'human_input_resolved'))]
        self.assertEqual(self.drain(execute=True)['status'], 'complete')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([name for name, _ in self.client.calls if name == 'claim_ticket']), 1)

    def test_event_append_commit_then_lost_reply_does_not_duplicate_work_on_restart(self):
        self.client.events = [event(4)]
        original, failures = self.events.append_page, [True]
        def append(*args, **kwargs):
            value = original(*args, **kwargs)
            if failures:
                failures.pop()
                raise OSError('PRIVATE_COMMIT_REPLY_LOST')
            return value
        self.events.append_page = append
        first = self.drain(execute=True)
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(self.inferences, [])
        self.events.close()
        restarted = EventJournal(self.root / 'events')
        self.addCleanup(restarted.close)
        second = self.drain(EventDrainer(restarted, self.worker, self.client), execute=True)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(len(self.inferences), 1)

    def test_publication_then_failed_batch_finish_reconciles_review_ticket_without_new_model(self):
        self.client.events = [event(4)]
        original, failures = self.events.finish_batch, [True]
        def finish(*args, **kwargs):
            if failures:
                failures.pop()
                raise OSError('PRIVATE_FINISH_ERROR')
            return original(*args, **kwargs)
        self.events.finish_batch = finish
        first = self.drain(execute=True)
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(self.client.ticket['status'], 'review')
        self.assertEqual(len(self.inferences), 1)
        second = self.drain(execute=True)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([name for name, _ in self.client.calls if name == 'update_ticket']), 1)

    def test_read_revocation_prevents_ingestion_and_request_revocation_prevents_dispatch(self):
        self.client.events = [event(1)]
        self.client.registration['policy']['allow_read'] = False
        self.assertEqual(self.drain()['reason'], 'read_permission_denied')
        self.assertEqual(self.client.calls, [])
        self.client.registration['policy']['allow_read'] = True
        self.assertEqual(self.drain()['status'], 'pending')
        self.client.registration['policy']['allow_requests'] = False
        self.assertEqual(self.drain(execute=True)['reason'], 'work_permission_denied')
        self.assertEqual(self.inferences, [])

    def test_permission_is_rechecked_between_ingestion_and_execution(self):
        self.client.events = [event(1)]
        self.client.after_page = lambda: self.client.registration['policy'].update(allow_requests=False)
        self.assertEqual(self.drain(execute=True)['reason'], 'work_permission_denied')
        self.assertEqual(self.inferences, [])
        self.assertFalse(any(name == 'claim_ticket' for name, _ in self.client.calls))

    def test_nonrunnable_ticket_keeps_batch_pending_until_explicit_human_change(self):
        self.client.events = [event(1)]
        self.client.ticket['status'] = 'review'
        first = self.drain(execute=True)
        self.assertEqual((first['status'], first['reason']), ('waiting', 'ticket_not_ready'))
        self.assertEqual(self.inferences, [])
        self.client.ticket.update(status='todo', version=2)
        second = self.drain(execute=True)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual(second['batch_id'], first['batch_id'])
        self.assertEqual(len(self.inferences), 1)

    def test_another_agents_claim_never_runs_worker(self):
        self.client.events = [event(1)]
        self.client.ticket.update(status='in_progress', claimant={'kind': 'agent', 'owner_id': 'other'}, lease_expires_at=time.time() + 1800)
        self.assertEqual(self.drain(execute=True)['reason'], 'claim_not_owned')
        self.assertEqual(self.inferences, [])

    def test_events_received_during_model_wait_for_the_next_explicit_invocation(self):
        self.client.events = [event(1)]
        self.after_model = lambda: self.client.events.append(event(7))
        first = self.drain(execute=True)
        self.assertEqual((first['status'], first['after']), ('complete', 1))
        second = self.drain(execute=True)
        self.assertEqual((second['status'], second['after']), ('waiting', 7))
        self.assertEqual(len(self.inferences), 1)

    def test_second_consumer_cannot_drain_the_same_scope_concurrently(self):
        other = EventJournal(self.root / 'events')
        self.addCleanup(other.close)
        self.client.events = [event(1)]
        with self.events.lock(SCOPE):
            value = self.drain(EventDrainer(other, self.worker, self.client), execute=True)
        self.assertEqual(value['status'], 'pending')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.inferences, [])

    def test_invalid_order_or_scope_is_not_ingested(self):
        for values in ([event(2), event(1)], [event(1), event(1)], [{**event(1), 'project_id': 'foreign'}]):
            with self.subTest(values=values):
                self.client.events = values
                value = self.drain(execute=True)
                self.assertEqual(value['reason'], 'invalid_activity_page')
                self.assertEqual(value['after'], 0)
                self.assertEqual(self.inferences, [])

    def test_scope_policy_mismatch_or_changed_instruction_never_reaches_model(self):
        self.client.events = [event(1)]
        self.assertEqual(self.drain(scope={**SCOPE, 'agent_id': 'foreign'})['reason'], 'scope_mismatch')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.drain()['status'], 'pending')
        self.assertEqual(self.drain(execute=True, instruction='Different instruction')['status'], 'blocked')
        self.assertEqual(self.inferences, [])

    def test_bounds_and_cancellation_prevent_unbounded_reads(self):
        for overrides in ({'max_pages': 0}, {'max_pages': 21}, {'limit': 201}, {'limit': True}, {'after': -1}):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.drain(**overrides)['reason'], 'invalid_drain_request')
        self.assertEqual(self.drain(should_cancel=lambda: True)['status'], 'cancelled')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.inferences, [])

    def test_worker_needs_review_blocks_all_new_batches_without_automatic_retry(self):
        self.client.events = [event(1)]
        def human_change():
            self.client.ticket['version'] += 1
            self.client.ticket['description'] = 'New human input during inference'
        self.after_model = human_change
        first = self.drain(execute=True)
        self.assertEqual(first['status'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.client.events.append(event(9))
        second = self.drain(execute=True)
        self.assertEqual((second['status'], second['reason']), ('needs_review', 'event_batch_needs_review'))
        self.assertEqual(second['after'], 9)
        self.assertEqual(second['batch_id'], first['batch_id'])
        self.assertEqual(len(self.inferences), 1)
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))

    def test_invalid_model_is_rejected_before_subscription_dispatch_or_board_read(self):
        self.client.events = [event(1)]
        for model in ('', 'bad\nmodel', {'provider': 'unexpected'}):
            with self.subTest(model=model):
                self.assertEqual(self.drain(execute=True, model=model)['reason'], 'invalid_drain_request')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.inferences, [])

    def test_full_event_buffer_runs_durable_batch_then_replays_uncommitted_page(self):
        self.client.events = [event(identifier) for identifier in range(1, 7)]
        with patch('blackoutburst_agents.event_journal.MAX_PENDING_EVENTS', 5):
            first = self.drain(execute=True, max_pages=2, limit=2)
            self.assertEqual((first['status'], first['after']), ('more_available', 4))
            self.assertEqual(self.inferences, [])
            second = self.drain(execute=True, max_pages=2, limit=2)
            self.assertEqual((second['status'], second['after']), ('complete', 4))
            self.assertEqual(len(self.inferences), 1)
            self.assertEqual(self.events.get_batch(second['batch_id'])['event_ids'], [1, 2, 3, 4])
            self.assertEqual(self.events.get_subscription(second['subscription_id'])['after'], 4)
            self.assertEqual(self.events.get_subscription(second['subscription_id'])['pending_count'], 0)
            third = self.drain(execute=True, max_pages=2, limit=2)
            self.assertEqual((third['status'], third['after']), ('waiting', 6))
            self.assertEqual(self.events.get_batch(third['batch_id'])['event_ids'], [5, 6])
            self.assertEqual(len(self.inferences), 1)
            self.client.ticket.update(status='todo', version=self.client.ticket['version'] + 1)
            fourth = self.drain(execute=True, max_pages=2, limit=2)
            self.assertEqual((fourth['status'], fourth['after']), ('complete', 6))
            self.assertEqual(fourth['batch_id'], third['batch_id'])
            self.assertEqual(len(self.inferences), 2)
            self.assertEqual(self.drain(execute=True)['status'], 'idle')
        positions = [args['after'] for name, args in self.client.calls if name == 'list_activity']
        self.assertEqual(positions[:4], [0, 2, 4, 4])

    def test_full_event_buffer_preview_reports_pending_without_consuming_rejected_events(self):
        self.client.events = [event(identifier) for identifier in range(1, 7)]
        with patch('blackoutburst_agents.event_journal.MAX_PENDING_EVENTS', 5):
            first = self.drain(max_pages=2, limit=2)
            self.assertEqual((first['status'], first['after']), ('more_available', 4))
            second = self.drain(max_pages=2, limit=2)
            self.assertEqual((second['status'], second['reason'], second['after']), ('pending', 'event_buffer_full', 4))
            self.assertEqual(self.events.get_batch(second['batch_id'])['event_ids'], [1, 2, 3, 4])
            self.assertEqual(self.events.get_subscription(second['subscription_id'])['pending_count'], 4)
            self.assertEqual(self.inferences, [])
            third = self.drain(execute=True, max_pages=2, limit=2)
            self.assertEqual((third['status'], third['after']), ('complete', 4))
            self.assertEqual(third['batch_id'], second['batch_id'])
            self.assertEqual(len(self.inferences), 1)

    def test_capacity_refusal_without_any_durable_batch_fails_closed(self):
        self.client.events = [event(1), event(2)]
        with patch('blackoutburst_agents.event_journal.MAX_PENDING_EVENTS', 1):
            result = self.drain(execute=True, limit=2)
        self.assertEqual((result['status'], result['reason'], result['after']), ('needs_review', 'event_buffer_full', 0))
        self.assertNotIn('batch_id', result)
        self.assertEqual(self.inferences, [])
        self.assertEqual(self.events.get_subscription(result['subscription_id'])['pending_count'], 0)

    def test_preview_requires_no_worker_but_execute_does(self):
        self.client.events = [event(1)]
        drainer = EventDrainer(self.events, None, self.client)
        self.assertEqual(self.drain(drainer)['status'], 'pending')
        self.assertEqual(self.drain(drainer, execute=True)['reason'], 'worker_unavailable')
        self.assertEqual(self.inferences, [])


if __name__ == '__main__':
    unittest.main()
