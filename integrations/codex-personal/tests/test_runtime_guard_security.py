"""Adversarial runtime handshakes against a real temporary Board, no models/network."""
import copy
import hashlib
import io
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
try:
    from board import Board, BoardError
except ModuleNotFoundError:
    Board = None
    BoardError = Exception

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.runtime_client import RuntimeBoardClient
from blackoutburst_agents.runtime_guard import RuntimeGuard, RuntimeControlError
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runner import PersonalRunner


class Client:
    endpoint = 'https://fixture.test/mcp'
    session_id = 'runtime-security'

    def __init__(self, board, actor):
        self.board, self.actor = board, actor
        self.calls = []
        self.lose = None
        self.lose_after_effect = True
        self.inject = False

    def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if self.lose == name and not self.lose_after_effect:
            self.lose = None
            raise BoardReadError('connection_failed')
        try:
            result = getattr(self.board, name)(self.actor, **args)
        except BoardError:
            raise BoardReadError('rpc_rejected') from None
        if self.lose == name:
            self.lose = None
            raise BoardReadError('connection_failed')
        if self.inject:
            receipt = result.get('execution', result)
            receipt.update(raw_diagnostic='PRIVATE_DIAGNOSTIC', prompt='PRIVATE_PROMPT', token='PRIVATE_CREDENTIAL')
        return result

    def register(self):
        raise AssertionError('Runtime control must never refresh registration')


@unittest.skipIf(Board is None, 'Board sources are not bundled in standalone package')
class RuntimeGuardSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.board = Board(self.root / 'board')
        self.board.ensure_owner('owner', 'Owner')
        self.human = {'kind': 'human', 'owner_id': 'owner'}
        self.board.set_policy(self.human, True, True, True, True, [], [])
        self.project = self.board.create_project(self.human, 'Fixture', ['owner'], idempotency_key='project')
        self.ticket = self.board.create_ticket(self.human, self.project['id'], 'Fixture', idempotency_key='ticket')
        device = self.board.create_device(self.human, 'Fixture device', [])
        raw = self.board.authenticate(device['token'])
        self.registration = self.board.register_agent(raw, Client.session_id, 'Fixture', [], 'codex', False, 'fixture')
        self.actor = {**raw, 'session_id': Client.session_id}
        self.board.claim_ticket(self.actor, self.ticket['id'], self.ticket['version'])
        self.scope = {'owner_id': 'owner', 'agent_id': self.registration['identity']['id'],
                      'project_id': self.project['id'], 'ticket_id': self.ticket['id'],
                      'board_endpoint': Client.endpoint, 'board_session_id': Client.session_id}
        self.client = Client(self.board, self.actor)
        self.journal = RuntimeJournal(self.root / 'runtime')
        self.addCleanup(self.journal.close)
        self.store = RunStore(self.root / 'runs')
        self.addCleanup(self.store.close)
        workspace = self.root / 'workspace'
        workspace.mkdir()
        digest = hashlib.sha256(json.dumps({key: self.scope[key] for key in
            ('board_endpoint', 'board_session_id', 'agent_id')}, ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        self.work = self.store.create_work(owner_id='owner', agent_id='board-agent-' + digest,
            project_id=self.project['id'], ticket_id=self.ticket['id'], workspace=str(workspace),
            executable=sys.executable, model='fixture-model')
        self.model_calls = 0
        self.clock = 100.0

    def guard(self):
        return RuntimeGuard(self.journal, self.client, self.scope, clock=lambda: self.clock)

    def result(self, callbacks, *, status='completed', reason='verified', cleanup=True):
        self.model_calls += 1
        callbacks['on_started'](999999999)
        session = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        return {'status': status, 'reason': reason, 'session_id': session,
                'text': 'PRIVATE_RESPONSE' if status == 'completed' else None,
                'verification': {'cleanup_confirmed': cleanup}}

    def transport(self, _settings, _prompt, _workspace, **callbacks):
        return self.result(callbacks)

    def run_turn(self, *, guard=None, transport=None, request='same'):
        return PersonalRunner(self.store, transport or self.transport, runtime_guard=guard or self.guard()).run(
            self.work['id'], request, 'PRIVATE_PROMPT')

    def execution(self):
        values = self.board.get_runtime(self.human)['executions']
        self.assertEqual(len(values), 1)
        return values[0]

    def assert_no_sensitive_runtime_fields(self):
        rows = self.journal.db.execute('SELECT * FROM executions').fetchall()
        encoded = json.dumps([dict(row) for row in rows])
        for text in ('PRIVATE_PROMPT', 'PRIVATE_RESPONSE', 'PRIVATE_DIAGNOSTIC', 'PRIVATE_CREDENTIAL'):
            self.assertNotIn(text, encoded)
            self.assertNotIn(text, json.dumps(self.board.get_runtime(self.human)))

    def test_verified_success_and_cached_restart_never_relaunch(self):
        first = self.run_turn()
        self.assertEqual(self.execution()['reason'], 'completed')
        self.assertEqual(self.execution()['state'], 'finished')
        before = len(self.client.calls)
        cached = self.run_turn()
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['turn_id'], first['turn_id'])
        self.assertEqual(self.model_calls, 1)
        self.assertEqual([name for name, _ in self.client.calls[before:]], ['lookup_execution'])
        self.assert_no_sensitive_runtime_fields()

    def test_lost_reservation_response_with_effect_reconciles_reserved_only(self):
        self.client.lose = 'reserve_execution'
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.execution()['state'], 'reserved')
        cached = self.run_turn()
        self.assertTrue(cached['cached'])
        self.assertEqual(cached['result']['status'], 'blocked')
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.execution()['state'], 'finished')
        self.assertEqual(self.execution()['outcome'], 'blocked')
        self.assertNotIn('start_execution', [name for name, _ in self.client.calls])

    def test_lost_reservation_without_effect_never_resends_on_cached_retry(self):
        self.client.lose = 'reserve_execution'
        self.client.lose_after_effect = False
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        for _ in range(2):
            with self.assertRaises(RuntimeControlError):
                self.run_turn()
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 0)
        self.assertEqual([name for name, _ in self.client.calls].count('reserve_execution'), 1)
        row = self.journal.db.execute('SELECT * FROM executions').fetchone()
        self.assertEqual(row['phase'], 'needs_review')

    def test_lost_start_response_does_not_launch_or_free_running_slot(self):
        self.client.lose = 'start_execution'
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.execution()['state'], 'running')
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 1)
        self.assertNotIn('finish_execution', [name for name, _ in self.client.calls])
        row = self.journal.db.execute('SELECT * FROM executions').fetchone()
        self.assertEqual(row['start_confirmed'], 0)

    def test_active_persistence_failure_after_ack_never_uses_nondurable_confirmation(self):
        original = self.journal.update
        def fail_active(identifier, revision, phase, *args, **kwargs):
            if phase == 'active':
                raise RuntimeError('PRIVATE_DIAGNOSTIC')
            return original(identifier, revision, phase, *args, **kwargs)
        with patch.object(self.journal, 'update', side_effect=fail_active):
            with self.assertRaises(RuntimeControlError):
                self.run_turn()
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.execution()['state'], 'running')
        self.assertNotIn('finish_execution', [name for name, _ in self.client.calls])
        self.assert_no_sensitive_runtime_fields()

    def test_lost_finish_response_is_reconciled_from_cache_without_start(self):
        self.client.lose = 'finish_execution'
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        self.assertEqual(self.model_calls, 1)
        self.assertEqual(self.execution()['state'], 'finished')
        names_before = len(self.client.calls)
        cached = self.run_turn()
        self.assertTrue(cached['cached'])
        self.assertEqual(self.model_calls, 1)
        self.assertEqual([name for name, _ in self.client.calls[names_before:]], ['lookup_execution'])
        self.assert_no_sensitive_runtime_fields()

    def test_heartbeat_response_loss_cancels_then_releases_only_after_confirmed_cleanup(self):
        self.client.lose = 'heartbeat_execution'
        def transport(_settings, _prompt, _workspace, **callbacks):
            self.clock += 6
            self.assertTrue(callbacks['should_cancel']())
            self.assertTrue(callbacks['should_cancel']())
            return self.result(callbacks, status='cancelled', reason='cancelled')
        value = self.run_turn(transport=transport)
        self.assertEqual(value['result']['status'], 'cancelled')
        self.assertEqual(self.execution()['state'], 'finished')
        self.assertEqual([name for name, _ in self.client.calls].count('heartbeat_execution'), 1)

    def assert_provider_failure_pauses(self, reason):
        self.board.update_runtime_limits(self.human, 1, 2, False)
        def transport(_settings, _prompt, _workspace, **callbacks):
            return self.result(callbacks, status='blocked', reason=reason, cleanup=False)
        with self.assertRaises(RuntimeControlError):
            self.run_turn(transport=transport)
        view = self.board.get_runtime(self.human)
        self.assertTrue(view['limits']['paused'])
        self.assertEqual(view['quota'], {'status': 'blocked_observed', 'reason': reason})
        self.assertEqual(self.execution()['state'], 'needs_review')
        self.assertEqual(view['active_count'], 1)

    def test_quota_with_uncertain_cleanup_still_pauses_owner(self):
        self.assert_provider_failure_pauses('quota_or_rate_limit')

    def test_auth_failure_with_uncertain_cleanup_still_pauses_owner(self):
        self.assert_provider_failure_pauses('authentication_failed')

    def test_contradictory_cleanup_flag_does_not_free_global_capacity(self):
        def transport(_settings, _prompt, _workspace, **callbacks):
            return self.result(callbacks, status='blocked', reason='execution_failed', cleanup=True) | {'cleanup_failed': True}
        with self.assertRaises(RuntimeControlError):
            self.run_turn(transport=transport)
        self.assertEqual(self.execution()['state'], 'needs_review')
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 1)

    def test_cleanup_failure_reason_overrides_a_true_verification_flag(self):
        def transport(_settings, _prompt, _workspace, **callbacks):
            return self.result(callbacks, status='blocked', reason='cleanup_failed', cleanup=True)
        with self.assertRaises(RuntimeControlError):
            self.run_turn(transport=transport)
        self.assertEqual(self.execution()['state'], 'needs_review')
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 1)

    def test_profile_pause_during_model_cancels_via_original_session_without_register(self):
        def transport(_settings, _prompt, _workspace, **callbacks):
            profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
            self.board.update_agent_profile(self.human, profile['id'], profile['version'], status='paused')
            self.clock += 6
            self.assertTrue(callbacks['should_cancel']())
            return self.result(callbacks, status='cancelled', reason='cancelled')
        self.run_turn(transport=transport)
        self.assertEqual(self.execution()['state'], 'finished')
        self.assertEqual(self.execution()['outcome'], 'cancelled')
        self.assertNotIn('register_agent', [name for name, _ in self.client.calls])

    def test_extra_remote_fields_are_never_persisted_or_forwarded(self):
        self.client.inject = True
        self.run_turn()
        self.assert_no_sensitive_runtime_fields()

    def test_wrong_scope_receipt_cannot_activate_transport(self):
        call = self.client.call
        def wrong_scope(name, args):
            value = call(name, args)
            if name == 'reserve_execution':
                value['execution']['owner_id'] = 'another-owner'
            return value
        with patch.object(self.client, 'call', side_effect=wrong_scope):
            with self.assertRaises(RuntimeControlError):
                self.run_turn()
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.execution()['state'], 'reserved')
        self.assertNotIn('start_execution', [name for name, _ in self.client.calls])

    def test_confirmed_start_is_monotone_and_cannot_return_from_review_to_active(self):
        guard = self.guard()
        turn = str(uuid.uuid4())
        guard.before(self.work, turn)
        self.assertTrue(guard.operation['start_confirmed'])
        guard.review()
        self.assertTrue(guard.operation['start_confirmed'])
        self.assertEqual(guard.operation['phase'], 'needs_review')
        from blackoutburst_agents.work_journal import JournalError
        with self.assertRaises(JournalError):
            self.journal.update(guard.operation['id'], guard.operation['revision'], 'active')
        self.assertEqual(self.execution()['state'], 'running')

    def test_local_finish_save_failure_reconciles_without_relaunch(self):
        original = self.journal.update
        def fail_finished(identifier, revision, phase, *args, **kwargs):
            if phase == 'finished':
                raise RuntimeError('PRIVATE_DIAGNOSTIC')
            return original(identifier, revision, phase, *args, **kwargs)
        with patch.object(self.journal, 'update', side_effect=fail_finished):
            with self.assertRaises(RuntimeControlError):
                self.run_turn()
        self.assertEqual(self.execution()['state'], 'finished')
        cached = self.run_turn()
        self.assertTrue(cached['cached'])
        self.assertEqual(self.model_calls, 1)
        self.assertEqual([name for name, _ in self.client.calls].count('start_execution'), 1)
        self.assertEqual([name for name, _ in self.client.calls].count('finish_execution'), 1)
        self.assert_no_sensitive_runtime_fields()

    def test_changed_profile_version_in_start_ack_is_not_accepted(self):
        call = self.client.call
        def wrong_profile(name, args):
            value = call(name, args)
            if name == 'start_execution':
                value['profile_version'] += 1
            return value
        with patch.object(self.client, 'call', side_effect=wrong_profile):
            with self.assertRaises(RuntimeControlError):
                self.run_turn()
        self.assertEqual(self.model_calls, 0)
        self.assertEqual(self.execution()['state'], 'running')
        self.assertEqual(self.journal.db.execute('SELECT start_confirmed FROM executions').fetchone()[0], 0)

    def runtime_cli_reconcile(self, operation):
        from blackoutburst_agents.cli import main
        output = io.StringIO()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file', return_value=self.client), redirect_stdout(output):
            code = main(['runtime', '--journal', str(self.root / 'runtime'), 'reconcile',
                '--operation', operation['id'], '--state', str(self.root / 'runs'),
                '--connection-file', str(self.root / 'synthetic-connection.json')])
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        return code, json.loads(output.getvalue())

    def test_cli_reconcile_does_not_free_an_interrupted_model_turn(self):
        guard = self.guard()
        with self.store.work_lock(self.work['id']):
            entry = self.store.begin_turn(self.work['id'], 'crashed', 'PRIVATE_PROMPT')
            guard.before(self.work, entry['turn']['id'])
        calls = list(self.client.calls)
        code, receipt = self.runtime_cli_reconcile(guard.operation)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['status'], 'needs_review')
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.execution()['state'], 'running')
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 1)
        self.assertEqual(self.model_calls, 0)

    def test_cli_reconcile_refuses_busy_work_before_control_calls(self):
        self.run_turn()
        operation = self.journal.get(self.journal.list_status()[0]['id'])
        calls = list(self.client.calls)
        with self.store.work_lock(self.work['id']):
            code, receipt = self.runtime_cli_reconcile(operation)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['reason'], 'work_busy')
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.model_calls, 1)

    def test_cli_reconcile_refuses_private_work_from_another_owner(self):
        self.client.lose = 'finish_execution'
        with self.assertRaises(RuntimeControlError):
            self.run_turn()
        operation = self.journal.get(self.journal.list_status()[0]['id'])
        self.store._db.execute('UPDATE works SET owner_id=? WHERE id=?', ('different-owner', self.work['id']))
        calls = list(self.client.calls)
        code, receipt = self.runtime_cli_reconcile(operation)
        self.assertEqual(code, 2)
        self.assertEqual(receipt['reason'], 'runtime_scope_mismatch')
        self.assertEqual(self.client.calls, calls)
        self.assertEqual(self.model_calls, 1)

    def test_runtime_client_has_no_registration_or_extra_tool_escape(self):
        client = RuntimeBoardClient({'mcp_url': Client.endpoint, 'token': 'synthetic-only',
            'client': 'codex', 'identity_key': 'fixture', 'name': 'Fixture'}, Client.session_id, 3)
        with patch.object(client, '_initialize') as initialize, patch.object(client, '_tool') as tool:
            with self.assertRaises(BoardReadError):
                client.register()
            for name, args in [('register_agent', {}), ('post_message', {}),
                ('lookup_execution', {'idempotency_key': 'key', 'session_id': 'other'}),
                ('finish_execution', {'execution_id': 'id', 'expected_revision': True,
                    'status': 'cancelled', 'cleanup_confirmed': True, 'reason': 'cancelled'})]:
                with self.assertRaises(BoardReadError):
                    client.call(name, args)
            initialize.assert_not_called()
            tool.assert_not_called()
            tool.return_value = {'found': False}
            self.assertEqual(client.call('lookup_execution', {'idempotency_key': 'key'}), {'found': False})
            tool.assert_called_once_with('lookup_execution', {'idempotency_key': 'key', 'session_id': Client.session_id})


if __name__ == '__main__':
    unittest.main()
