"""Explicit worker integration, real temporary Board/SQLite and fake inference."""
from copy import deepcopy
from pathlib import Path
import json
import time
import sys
import tempfile
import unittest
import uuid

from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.work_journal import WorkJournal

REPO = Path(__file__).resolve().parents[3]
Board = None
if (REPO / 'board.py').is_file() and (REPO / 'coordination.py').is_file():
    sys.path.insert(0, str(REPO))
    try:
        from board import Board
    finally:
        sys.path.remove(str(REPO))


class Client:
    endpoint = 'https://board.test/mcp'

    def __init__(self, board, raw, session='worker-session'):
        self.board, self.raw, self.session_id = board, raw, session
        self.calls, self.lost, self.register_count = [], set(), 0
        self.on_register = None

    def register(self):
        self.register_count += 1
        if self.on_register:
            self.on_register(self.register_count)
        value = self.board.register_agent(self.raw, self.session_id, 'Worker', [], 'codex', False, 'worker')
        self.actor = self.board.actor_session(self.raw, self.session_id)
        self.registration = value
        return value

    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        values = dict(args)
        if name == 'update_ticket':
            values.update(values.pop('changes'))
        result = getattr(self.board, name)(self.actor, **values)
        if name in self.lost:
            self.lost.remove(name)
            raise TimeoutError('PRIVATE_TRANSPORT_DIAGNOSTIC')
        return result


@unittest.skipIf(Board is None, 'Repository Board sources not bundled in standalone package')
class BoardWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.board = Board(self.root / 'board', base_url='https://board.test')
        self.board.ensure_owner('owner', 'Owner')
        self.human = {'kind': 'human', 'owner_id': 'owner'}
        self.board.set_policy(self.human, True, True, True, True, [], [])
        self.project = self.board.create_project(self.human, 'Project', ['owner'], idempotency_key='project')
        self.ticket = self.board.create_ticket(self.human, self.project['id'], 'Explicit task', idempotency_key='ticket')
        device = self.board.create_device(self.human, 'Local test', [])
        self.raw = self.board.authenticate(device['token'])
        self.client = Client(self.board, self.raw)
        registration = self.client.register()
        self.scope = {'owner_id': 'owner', 'agent_id': registration['identity']['id'],
                      'project_id': self.project['id'], 'ticket_id': self.ticket['id'],
                      'board_session_id': self.client.session_id, 'board_endpoint': self.client.endpoint}
        self.journal = WorkJournal(self.root / 'journal')
        self.store = RunStore(self.root / 'runs')
        self.addCleanup(self.journal.close)
        self.addCleanup(self.store.close)
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.inferences, self.after_model = [], None
        self.worker = self.make_worker()

    def make_worker(self, journal=None, store=None, client=None):
        return BoardWorker(journal or self.journal, store or self.store, client or self.client,
                           runner_factory=lambda state: PersonalRunner(state, self.transport))

    def transport(self, settings, prompt, workspace, **callbacks):
        self.inferences.append({'prompt': prompt, 'resume': callbacks['resume_session_id']})
        callbacks['on_started'](999999999)
        sid = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](sid)
        if self.after_model:
            self.after_model()
        return {'status': 'completed', 'reason': 'verified', 'session_id': sid, 'text': 'PRIVATE_MODEL_RESULT'}

    def run_worker(self, worker=None, **overrides):
        args = {'scope': self.scope, 'request_id': 'explicit-request', 'instruction': 'Draft a short answer.',
                'workspace': self.workspace, 'executable': sys.executable, 'timeout': 10}
        args.update(overrides)
        return (worker or self.worker).run(**args)

    def read(self):
        return self.board.get_ticket(self.human, self.ticket['id'])['ticket']

    def change(self, **changes):
        ticket = self.read()
        return self.board.update_ticket(self.human, ticket['id'], ticket['version'], **changes)

    def test_happy_path_publishes_review_releases_claim_preserves_existing_evidence(self):
        self.change(outcome='Previous result', evidence='Prior human evidence')
        result = self.run_worker()
        self.assertEqual(result['status'], 'complete')
        ticket = self.read()
        self.assertEqual(ticket['status'], 'review')
        self.assertIsNone(ticket['claimant'])
        self.assertTrue(ticket['outcome'].startswith('Previous result\n\n'))
        self.assertTrue(ticket['evidence'].startswith('Prior human evidence\n\n'))
        self.assertIn('PRIVATE_MODEL_RESULT', ticket['outcome'])
        self.assertEqual(len(self.inferences), 1)
        self.assertNotIn('PRIVATE_MODEL_RESULT', json.dumps(result))
        self.assertNotIn('Draft a short answer', json.dumps(result))
        self.assertEqual({name for name, _ in self.client.calls}, {'claim_ticket', 'get_ticket', 'get_thread', 'update_ticket'})

    def test_duplicate_invocation_neither_infers_nor_publishes_again(self):
        first = self.run_worker()
        calls = len([c for c in self.client.calls if c[0] == 'update_ticket'])
        second = self.run_worker()
        self.assertEqual(second['operation_id'], first['operation_id'])
        self.assertTrue(second['cached'])
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'update_ticket']), calls)
        self.assertEqual(self.run_worker(instruction='Different instruction')['reason'], 'local_operation_conflict')
        self.assertEqual(len(self.inferences), 1)

    def test_lost_claim_response_reconciles_exact_claim_without_reclaiming(self):
        self.client.lost.add('claim_ticket')
        self.assertEqual(self.run_worker()['status'], 'complete')
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'claim_ticket']), 1)
        self.assertEqual(len(self.inferences), 1)

    def test_lost_publication_reply_and_restart_reconcile_without_inference_or_write(self):
        self.client.lost.add('update_ticket')
        first = self.run_worker()
        self.assertEqual((first['status'], first['stage']), ('pending', 'publishing'))
        self.journal.close()
        restarted = WorkJournal(self.root / 'journal')
        self.addCleanup(restarted.close)
        result = self.run_worker(self.make_worker(journal=restarted))
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'update_ticket']), 1)

    def test_model_result_survives_journal_failure_before_publication(self):
        original = self.journal.update
        pending = [True]
        def update(identifier, revision, **kwargs):
            if kwargs.get('stage') == 'result_ready' and pending and pending.pop():
                raise OSError('PRIVATE_DISK_DETAIL')
            return original(identifier, revision, **kwargs)
        self.journal.update = update
        first = self.run_worker()
        self.assertEqual(first['stage'], 'running')
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(self.run_worker()['status'], 'complete')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'update_ticket']), 1)

    def test_other_worker_cannot_claim_while_first_model_is_running(self):
        other = Client(self.board, self.raw, 'other-session')
        other.register()
        scope = {**self.scope, 'board_session_id': other.session_id}
        other_journal = WorkJournal(self.root / 'other-journal')
        self.addCleanup(other_journal.close)
        receipts = []
        self.after_model = lambda: receipts.append(self.run_worker(self.make_worker(journal=other_journal, client=other), scope=scope))
        self.assertEqual(self.run_worker()['status'], 'complete')
        self.assertEqual(receipts[0]['reason'], 'claim_lost_or_expired')
        self.assertEqual(len(self.inferences), 1)
        self.assertFalse(any(name == 'claim_ticket' for name, _ in other.calls))

    def test_scoped_local_lock_rejects_second_process_before_mcp(self):
        other_journal = WorkJournal(self.root / 'journal')
        self.addCleanup(other_journal.close)
        with self.journal.lock(self.scope):
            result = self.run_worker(self.make_worker(journal=other_journal))
        self.assertEqual(result['reason'], 'local_operation_conflict')
        self.assertEqual(self.client.calls, [])

    def test_registration_scope_and_policy_rejected_before_claim(self):
        for mutation in ({'owner_id': 'somebody-else'}, {'agent_id': 'someone-else'},
                         {'board_session_id': 'other'}, {'board_endpoint': 'https://other.test/mcp'}):
            with self.subTest(mutation=mutation):
                result = self.run_worker(scope={**self.scope, **mutation})
                self.assertEqual(result['status'], 'needs_review')
                self.assertEqual(self.client.calls, [])
        self.board.set_policy(self.human, True, True, True, False, [], [])
        self.assertEqual(self.run_worker()['reason'], 'work_permission_denied')
        self.assertEqual(self.inferences, [])

    def test_live_revocation_after_model_prevents_publication(self):
        self.after_model = lambda: self.board.set_policy(self.human, True, True, True, False, [], [])
        self.assertEqual(self.run_worker()['reason'], 'work_permission_denied')
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))
        self.assertEqual(len(self.inferences), 1)

    def test_expired_claim_never_recovers_automatically(self):
        self.board.claim_ticket(self.client.actor, self.ticket['id'], self.ticket['version'])
        with self.board._tx() as db:
            db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?', (self.ticket['id'],))
        self.assertEqual(self.run_worker()['reason'], 'claim_lost_or_expired')
        self.assertFalse(any(name == 'claim_ticket' for name, _ in self.client.calls))
        self.assertEqual(self.inferences, [])

    def test_human_edit_after_preparation_stops_before_model(self):
        # First registration happened during fixture setup, second at run entry,
        # third immediately before the model.
        self.client.on_register = lambda count: self.change(description='Human changed the task') if count == 3 else None
        result = self.run_worker()
        self.assertEqual(result['reason'], 'context_changed')
        self.assertEqual(self.inferences, [])

    def test_human_message_during_model_stops_publication(self):
        self.after_model = lambda: self.board.post_message(self.human, self.ticket['thread_id'], 'A new decision', idempotency_key='message')
        result = self.run_worker()
        self.assertEqual(result['reason'], 'context_changed')
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))
        self.assertEqual(len(self.inferences), 1)

    def test_new_human_outcome_after_ambiguous_publication_is_never_overwritten(self):
        self.client.lost.add('update_ticket')
        self.assertEqual(self.run_worker()['stage'], 'publishing')
        self.change(outcome='New independent human outcome')
        result = self.run_worker()
        self.assertEqual(result['reason'], 'publication_conflict')
        self.assertEqual(self.read()['outcome'], 'New independent human outcome')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([c for c in self.client.calls if c[0] == 'update_ticket']), 1)

    def test_large_prior_evidence_is_preserved_without_partial_submission(self):
        self.change(evidence='x' * 20000)
        result = self.run_worker()
        self.assertEqual(result['reason'], 'publication_too_large')
        self.assertEqual(self.read()['evidence'], 'x' * 20000)
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))

    def test_same_board_ids_in_other_endpoint_and_session_have_separate_codex_contexts(self):
        first = self.run_worker()
        self.assertEqual(first['status'], 'complete')
        # Reopen as a human, then use a separately pinned connection. Same real
        # ticket and identity IDs must never implicitly resume the first model.
        self.change(status='todo')
        other = Client(self.board, self.raw, 'second-session')
        other.endpoint = 'https://different-board.test/mcp'
        other.register()
        scope = {**self.scope, 'board_session_id': other.session_id, 'board_endpoint': other.endpoint}
        second = self.run_worker(self.make_worker(client=other), scope=scope)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual([item['resume'] for item in self.inferences], [None, None])
        op1, op2 = self.journal.get(first['operation_id']), self.journal.get(second['operation_id'])
        self.assertNotEqual(op1['payload']['work_id'], op2['payload']['work_id'])

    def test_cancel_before_claim_has_no_remote_ticket_effect(self):
        result = self.run_worker(should_cancel=lambda: True)
        self.assertEqual(result['reason'], 'cancel_requested')
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.inferences, [])

    def test_cancel_during_model_retains_result_without_publication(self):
        cancelled = [False]
        self.after_model = lambda: cancelled.__setitem__(0, True)
        result = self.run_worker(should_cancel=lambda: cancelled[0])
        self.assertEqual(result['reason'], 'cancel_requested')
        self.assertEqual(len(self.inferences), 1)
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))
        self.assertEqual(self.journal.get(result['operation_id'])['payload']['result']['text'], 'PRIVATE_MODEL_RESULT')

    def test_distinct_authenticated_owners_never_share_provider_context(self):
        first = self.run_worker()
        self.assertEqual(first['status'], 'complete')
        self.board.ensure_owner('bob', 'Bob')
        human = {'kind': 'human', 'owner_id': 'bob'}
        self.board.set_policy(human, True, True, True, True, [], [])
        project = self.board.create_project(human, 'Bob project', ['bob'], idempotency_key='bob-project')
        ticket = self.board.create_ticket(human, project['id'], 'Bob task', idempotency_key='bob-ticket')
        device = self.board.create_device(human, 'Bob device', [])
        client = Client(self.board, self.board.authenticate(device['token']), 'bob-session')
        registration = client.register()
        scope = {'owner_id': 'bob', 'agent_id': registration['identity']['id'], 'project_id': project['id'],
                 'ticket_id': ticket['id'], 'board_session_id': client.session_id, 'board_endpoint': client.endpoint}
        second = self.run_worker(self.make_worker(client=client), scope=scope)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual([item['resume'] for item in self.inferences], [None, None])
        self.assertNotIn(self.scope['agent_id'], self.inferences[-1]['prompt'])
        self.assertNotIn(self.scope['ticket_id'], self.inferences[-1]['prompt'])

    def test_failed_turn_publishes_safe_blocked_outcome_and_releases_claim(self):
        def failed(settings, prompt, workspace, **callbacks):
            return {'status': 'blocked', 'reason': 'PRIVATE_ARBITRARY_REASON', 'session_id': None, 'text': None, 'verification': {'cleanup_confirmed': True}}
        worker = BoardWorker(self.journal, self.store, self.client,
                             runner_factory=lambda state: PersonalRunner(state, failed))
        result = self.run_worker(worker)
        self.assertEqual((result['status'], result['published_status']), ('complete', 'blocked'))
        ticket = self.read()
        self.assertIsNone(ticket['claimant'])
        self.assertNotIn('PRIVATE_ARBITRARY_REASON', json.dumps(result) + ticket['outcome'] + ticket['evidence'])
        # The failure is a recheckable block, never a reasonless ghost.
        self.assertTrue(ticket['blocked_reason'].startswith('Codex text turn could not complete'))
        self.assertEqual(ticket['block_reasons'], ['explicit'])
        self.assertLessEqual(ticket['recheck_at'] - time.time(), 3600)

    def test_real_transport_shaped_cancelled_result_has_safe_blocked_publication(self):
        def cancelled(settings, prompt, workspace, **callbacks):
            return {'status': 'cancelled', 'reason': 'PRIVATE_CANCEL_DIAGNOSTIC', 'session_id': None, 'text': None, 'verification': {'cleanup_confirmed': True}}
        worker = BoardWorker(self.journal, self.store, self.client,
                             runner_factory=lambda state: PersonalRunner(state, cancelled))
        result = self.run_worker(worker)
        self.assertEqual((result['status'], result['published_status']), ('complete', 'blocked'))
        ticket = self.read()
        self.assertIsNone(ticket['claimant'])
        self.assertNotIn('PRIVATE_CANCEL_DIAGNOSTIC', json.dumps(result) + ticket['outcome'] + ticket['evidence'])
        self.assertEqual(self.journal.get(result['operation_id'])['payload']['result'], {'status': 'cancelled', 'text': ''})

    def test_partial_failure_text_is_not_retained_or_published(self):
        def failed(settings, prompt, workspace, **callbacks):
            return {'status': 'blocked', 'reason': 'PRIVATE_REASON', 'session_id': None, 'text': 'PRIVATE_PARTIAL_TEXT', 'verification': {'cleanup_confirmed': True}}
        worker = BoardWorker(self.journal, self.store, self.client,
                             runner_factory=lambda state: PersonalRunner(state, failed))
        result = self.run_worker(worker)
        self.assertEqual(result['published_status'], 'blocked')
        ticket = self.read()
        operation = self.journal.get(result['operation_id'])
        self.assertNotIn('PRIVATE_PARTIAL_TEXT', json.dumps(result) + json.dumps(operation) + ticket['outcome'] + ticket['evidence'])

    def test_durability_failure_while_recording_scope_stop_returns_safe_pending(self):
        original = self.journal.update
        def update(identifier, revision, **kwargs):
            if kwargs.get('stage') == 'needs_review':
                raise OSError('PRIVATE_REVIEW_WRITE_FAILURE')
            return original(identifier, revision, **kwargs)
        self.journal.update = update
        result = self.run_worker(scope={**self.scope, 'agent_id': 'not-this-agent'})
        self.assertEqual((result['status'], result['stage']), ('pending', 'new'))
        self.assertEqual(result['reason'], 'effect_unconfirmed')
        self.assertNotIn('PRIVATE_REVIEW_WRITE_FAILURE', json.dumps(result))
        self.assertEqual(self.inferences, [])

    def test_durability_failure_while_recording_context_error_returns_safe_pending(self):
        original_update = self.journal.update
        original_call = self.client.call
        def update(identifier, revision, **kwargs):
            if kwargs.get('stage') == 'needs_review':
                raise OSError('PRIVATE_CONTEXT_WRITE_FAILURE')
            return original_update(identifier, revision, **kwargs)
        def call(name, args):
            result = original_call(name, args)
            if name == 'get_thread':
                result['thread']['participants'] = ['another-owner']
            return result
        self.journal.update, self.client.call = update, call
        result = self.run_worker()
        self.assertEqual((result['status'], result['stage']), ('pending', 'claimed'))
        self.assertEqual(result['reason'], 'effect_unconfirmed')
        self.assertNotIn('PRIVATE_CONTEXT_WRITE_FAILURE', json.dumps(result))
        self.assertEqual(self.inferences, [])

    def test_large_unicode_request_fails_definitively_without_pending_retry_loop(self):
        from blackoutburst_agents.worker_client import _arguments
        self.change(outcome='é' * 6000)
        original_call = self.client.call
        def validated(name, args):
            _arguments(name, args)
            return original_call(name, args)
        self.client.call = validated
        calls = []
        def unicode_turn(settings, prompt, workspace, **callbacks):
            calls.append(prompt)
            result = self.transport(settings, prompt, workspace, **callbacks)
            return {**result, 'text': 'é' * 6000, 'verification': {'cleanup_confirmed': True}}
        worker = BoardWorker(self.journal, self.store, self.client,
                             runner_factory=lambda state: PersonalRunner(state, unicode_turn))
        result = self.run_worker(worker)
        self.assertEqual((result['status'], result['reason']), ('needs_review', 'board_arguments_invalid'))
        repeated = self.run_worker(worker)
        self.assertEqual(repeated['stage'], 'needs_review')
        self.assertTrue(repeated['cached'])
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))
        self.assertEqual(self.read()['outcome'], 'é' * 6000)

    def test_uncertain_process_cleanup_never_releases_claim(self):
        for variant in ('explicit_false', 'missing', 'cleanup_reason'):
            with self.subTest(variant=variant):
                ticket = self.board.create_ticket(self.human, self.project['id'], 'Cleanup ' + variant,
                                                 idempotency_key='cleanup-' + variant)
                scope = {**self.scope, 'ticket_id': ticket['id']}
                def failed(settings, prompt, workspace, **callbacks):
                    result = {'status': 'blocked', 'reason': 'PRIVATE_DIAGNOSTIC', 'session_id': None, 'text': None}
                    if variant == 'explicit_false':
                        result['verification'] = {'cleanup_confirmed': False}
                    elif variant == 'cleanup_reason':
                        result['reason'] = 'cleanup_failed'
                        result['verification'] = {'cleanup_confirmed': True}
                    return result
                worker = BoardWorker(self.journal, self.store, self.client,
                                     runner_factory=lambda state: PersonalRunner(state, failed))
                result = self.run_worker(worker, scope=scope)
                self.assertEqual(result['reason'], 'model_cleanup_unconfirmed')
                current = self.board.get_ticket(self.human, ticket['id'])['ticket']
                self.assertIsNotNone(current['claimant'])
                self.assertEqual(current['status'], 'in_progress')
                self.assertNotIn('PRIVATE_DIAGNOSTIC', json.dumps(result))
                self.assertEqual(self.journal.get(result['operation_id'])['payload']['cleanup_uncertain'], True)
        self.assertFalse(any(name == 'update_ticket' for name, _ in self.client.calls))

    def test_wrong_project_membership_never_claims(self):
        scope = {**self.scope, 'project_id': 'different-project'}
        self.assertEqual(self.run_worker(scope=scope)['reason'], 'ticket_scope_mismatch')
        self.assertEqual(self.inferences, [])
        self.assertFalse(any(name == 'claim_ticket' for name, _ in self.client.calls))


if __name__ == '__main__':
    unittest.main()
