"""Explicit worker commands using mocked orchestration and real private state."""
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.cli import main
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.work_journal import JournalError, WorkJournal


class WorkerCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = self.root / 'journal'
        self.state = self.root / 'runner'
        self.instruction = self.root / 'instruction.txt'
        self.instruction.write_text('PRIVATE INSTRUCTION')
        self.connection = self.root / 'connection.json'
        self.operation_id = str(uuid.uuid4())
        self.result = {'status': 'complete', 'stage': 'complete', 'reason': 'publication_confirmed',
                       'cached': False, 'operation_id': self.operation_id, 'published_status': 'review'}
        self.client = Mock(endpoint='https://board.test/mcp')
        self.client.session_id = 'explicit-session'

    def args(self, *extra):
        return ['worker', '--journal', str(self.journal), '--state', str(self.state), 'run',
            '--connection-file', str(self.connection), '--session', 'explicit-session',
            '--owner', 'owner', '--agent', 'agent', '--project', 'project', '--ticket', 'ticket',
            '--request', 'stable-request', '--instruction-file', str(self.instruction),
            '--workspace', str(self.root), '--codex', sys.executable, *extra]

    def invoke(self, args):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args)
        text = stream.getvalue()
        return code, json.loads(text), text

    def test_run_selects_one_ticket_and_outputs_only_whitelisted_receipt(self):
        original_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client) as connect, \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            worker.return_value.run.return_value = {**self.result, 'text': 'PRIVATE RESPONSE',
                                                   'payload': {'credentials': 'PRIVATE TOKEN'}}
            code, output, text = self.invoke(self.args('--model', 'test-model', '--timeout', '20'))
            connect.assert_called_once_with(self.connection, session_id='explicit-session')
            args = worker.return_value.run.call_args.kwargs
            self.assertEqual(args['scope'], {'owner_id': 'owner', 'agent_id': 'agent', 'project_id': 'project',
                'ticket_id': 'ticket', 'board_session_id': 'explicit-session', 'board_endpoint': 'https://board.test/mcp'})
            self.assertEqual(args['request_id'], 'stable-request')
            self.assertEqual(args['instruction'], 'PRIVATE INSTRUCTION')
            self.assertEqual(args['model'], 'test-model')
            self.assertEqual(args['timeout'], 20)
            self.assertFalse(args['should_cancel']())
        self.assertEqual(code, 0)
        self.assertEqual(output, self.result)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn(str(self.root), text)
        for sig, handler in original_handlers.items():
            self.assertIs(signal.getsignal(sig), handler)
        # Both private stores were closed even after normal orchestration.
        journal, store, client = worker.call_args.args
        self.assertIsNone(journal._db)
        self.assertIsNone(store._db)
        self.assertIs(client, self.client)

    def test_status_is_offline_and_does_not_require_runner_state(self):
        scope = {'owner_id': 'owner', 'agent_id': 'agent', 'project_id': 'project', 'ticket_id': 'ticket',
                 'board_session_id': 'explicit-session', 'board_endpoint': 'https://board.test/mcp'}
        with WorkJournal(self.journal) as journal:
            with journal.lock(scope):
                operation = journal.begin(scope, 'request', {'instruction': 'PRIVATE', 'workspace': '/private/path'})
                journal.update(operation['id'], 1, stage='claiming', payload={'response': 'PRIVATE'})
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient') as connect, \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker, \
                patch('blackoutburst_agents.worker_cli.RunStore') as store:
            code, output, text = self.invoke(['worker', '--journal', str(self.journal), 'status', '--operation', operation['id']])
            connect.assert_not_called()
            worker.assert_not_called()
            store.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(output['stage'], 'claiming')
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('/private/path', text)
        self.assertNotIn('configuration', text)
        self.assertNotIn('payload', text)

    def test_invalid_instruction_timeout_model_and_missing_state_never_connect(self):
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file') as connect, \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            for raw in (b'', b'\xff', b'bad\x00input', b'x' * 12001):
                self.instruction.write_bytes(raw)
                code, output, _ = self.invoke(self.args())
                self.assertEqual((code, output['reason']), (2, 'invalid_worker_configuration_or_local_io'))
            self.instruction.write_text('Valid')
            for timeout in ('0', '301', 'nan', 'inf'):
                code, _, _ = self.invoke(self.args('--timeout', timeout))
                self.assertEqual(code, 2)
            self.assertEqual(self.invoke(self.args('--model', 'invalid model'))[0], 2)
            args = self.args()
            del args[3:5]  # --state and its value
            self.assertEqual(self.invoke(args)[0], 2)
            connect.assert_not_called()
            worker.assert_not_called()

    def test_instruction_fifo_is_rejected_without_blocking(self):
        self.instruction.unlink()
        os.mkfifo(self.instruction, 0o600)
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient') as connect:
            self.assertEqual(self.invoke(self.args())[0], 2)
            connect.assert_not_called()

    def test_failure_does_not_print_exception_content_or_leave_signal_handlers(self):
        original = signal.getsignal(signal.SIGTERM)
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            worker.return_value.run.side_effect = RuntimeError('PRIVATE TOKEN AND INSTRUCTION')
            code, output, text = self.invoke(self.args())
        self.assertEqual((code, output['reason']), (2, 'worker_failed'))
        self.assertNotIn('PRIVATE', text)
        self.assertIs(signal.getsignal(signal.SIGTERM), original)
        journal, store, _ = worker.call_args.args
        self.assertIsNone(journal._db)
        self.assertIsNone(store._db)

    def test_connection_denial_is_a_safe_local_diagnostic(self):
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', side_effect=BoardReadError('permission_denied')):
            code, output, text = self.invoke(self.args())
        self.assertEqual((code, output['reason']), (2, 'permission_denied'))
        self.assertNotIn(str(self.connection), text)

    def test_signal_requests_cooperative_cancellation_and_pending_is_not_success(self):
        def run(**args):
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            self.assertTrue(args['should_cancel']())
            return {**self.result, 'status': 'pending', 'stage': 'publishing', 'reason': 'publication_unconfirmed'}
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            worker.return_value.run.side_effect = run
            code, output, _ = self.invoke(self.args())
        self.assertEqual(code, 2)
        self.assertEqual(output['status'], 'pending')

    def test_invalid_worker_receipt_is_not_forwarded(self):
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            for result in ({**self.result, 'reason': 'PRIVATE RESPONSE'},
                           {**self.result, 'published_status': 'done'},
                           {**self.result, 'operation_id': 'PRIVATE INVALID ID'}, None):
                worker.return_value.run.return_value = result
                code, _, text = self.invoke(self.args())
                self.assertEqual(code, 2)
                self.assertNotIn('PRIVATE', text)

    def review_operation(self, *, with_work=False, running_pid=None, wrong_scope=False):
        scope = {'owner_id': 'owner', 'agent_id': 'agent', 'project_id': 'project', 'ticket_id': 'ticket',
                 'board_session_id': 'explicit-session', 'board_endpoint': 'https://board.test/mcp'}
        configuration = {'instruction': 'PRIVATE INSTRUCTION', 'workspace': str(self.root.resolve()),
                         'executable': str(Path(sys.executable).resolve()), 'model': None, 'timeout': 120}
        payload = {'result': 'PRIVATE RESPONSE'}
        if with_work:
            identity = json.dumps({key: scope[key] for key in ('board_endpoint', 'board_session_id', 'agent_id')},
                                  ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            agent_id = 'board-agent-' + hashlib.sha256(identity.encode()).hexdigest()
            store = RunStore(self.state)
            try:
                work = store.create_work(owner_id='owner', agent_id=agent_id, project_id='project',
                    ticket_id='wrong-ticket' if wrong_scope else 'ticket', workspace=self.root,
                    executable=sys.executable, model=None)
                payload['work_id'] = work['id']
                if running_pid is not None:
                    with store.work_lock(work['id']):
                        turn = store.begin_turn(work['id'], 'old-turn', 'PRIVATE PROMPT')['turn']
                        store.on_process(turn['id'], running_pid)
            finally:
                store.close()
        with WorkJournal(self.journal) as journal:
            with journal.lock(scope):
                operation = journal.begin(scope, 'request', configuration)
                operation = journal.update(operation['id'], 1, stage='needs_review', payload=payload)
        return operation

    def abandon_args(self, operation):
        return ['worker', '--journal', str(self.journal), '--state', str(self.state), 'abandon',
            '--operation', operation['id'], '--revision', str(operation['revision']),
            '--reason', 'PRIVATE HUMAN FINDING', '--acknowledge-board-reconciled']

    def test_inspect_exports_private_operation_and_rejects_symlink(self):
        operation = self.review_operation()
        destination = self.root / 'inspection.json'
        args = ['worker', '--journal', str(self.journal), 'inspect', '--operation', operation['id'], '--output', str(destination)]
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient') as client:
            code, output, text = self.invoke(args)
            client.assert_not_called()
        self.assertEqual((code, output['status']), (0, 'exported'))
        self.assertEqual(json.loads(destination.read_text()), operation)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn(str(destination), text)
        destination.unlink()
        keep = self.root / 'keep'
        keep.write_text('KEEP')
        destination.symlink_to(keep)
        self.assertEqual(self.invoke(args)[0], 2)
        self.assertEqual(keep.read_text(), 'KEEP')

    def test_abandon_before_work_is_offline_repeatable_and_preserves_old_request(self):
        operation = self.review_operation()
        args = self.abandon_args(operation)
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient') as client, \
                patch('blackoutburst_agents.worker_cli.RunStore') as store:
            code, output, text = self.invoke(args)
            repeated = self.invoke(args)
            client.assert_not_called()
            store.assert_not_called()
        self.assertEqual((code, output['stage']), (0, 'abandoned'))
        self.assertEqual(repeated[1], output)
        self.assertNotIn('PRIVATE', text)
        with WorkJournal(self.journal) as journal:
            with journal.lock(operation['scope']):
                old = journal.begin(operation['scope'], 'request', operation['configuration'])
                self.assertEqual(old['stage'], 'abandoned')
                self.assertEqual(old['payload']['result'], 'PRIVATE RESPONSE')
                self.assertEqual(old['payload']['abandonment']['reason'], 'PRIVATE HUMAN FINDING')
                self.assertNotEqual(journal.begin(operation['scope'], 'new', operation['configuration'])['id'], old['id'])

    def test_abandon_closes_only_matching_work_and_allows_clean_reopen(self):
        operation = self.review_operation(with_work=True)
        code, output, _ = self.invoke(self.abandon_args(operation))
        self.assertEqual((code, output['stage']), (0, 'abandoned'))
        store = RunStore(self.state)
        try:
            work = store.get_work(operation['payload']['work_id'])
            self.assertEqual(work['status'], 'closed')
            next_work = store.create_work(**{key: work[key] for key in
                ('owner_id', 'agent_id', 'project_id', 'ticket_id', 'workspace', 'executable', 'model')})
            self.assertNotEqual(next_work['id'], work['id'])
        finally:
            store.close()

    def test_abandon_refuses_live_process_then_retries_after_verified_exit(self):
        operation = self.review_operation(with_work=True, running_pid=os.getpid())
        args = self.abandon_args(operation)
        code, output, text = self.invoke(args)
        self.assertEqual((code, output['reason']), (2, 'process_still_running'))
        self.assertNotIn('PRIVATE', text)
        with WorkJournal(self.journal) as journal:
            waiting = journal.get(operation['id'])
            self.assertEqual(waiting['stage'], 'needs_review')
            self.assertEqual(waiting['revision'], operation['revision'] + 1)
        # Test the separate process-group check even with an absent leader.
        with patch('blackoutburst_agents.run_store.os.kill', side_effect=ProcessLookupError), \
                patch('blackoutburst_agents.run_store.os.killpg', return_value=None):
            self.assertEqual(self.invoke(args)[1]['reason'], 'process_still_running')
        with patch('blackoutburst_agents.run_store.os.kill', side_effect=ProcessLookupError), \
                patch('blackoutburst_agents.run_store.os.killpg', side_effect=ProcessLookupError):
            self.assertEqual(self.invoke(args)[1]['stage'], 'abandoned')

    def test_abandon_retry_survives_gap_after_work_closes(self):
        operation = self.review_operation(with_work=True)
        args = self.abandon_args(operation)
        with patch('blackoutburst_agents.worker_cli.WorkJournal.abandon', side_effect=JournalError('journal_unavailable')):
            self.assertEqual(self.invoke(args)[1]['reason'], 'journal_unavailable')
        store = RunStore(self.state)
        try:
            self.assertEqual(store.get_work(operation['payload']['work_id'])['status'], 'closed')
        finally:
            store.close()
        self.assertEqual(self.invoke(args)[1]['stage'], 'abandoned')

    def test_abandon_rejects_wrong_work_scope_stale_revision_and_changed_reason(self):
        operation = self.review_operation(with_work=True, wrong_scope=True)
        args = self.abandon_args(operation)
        stale = list(args)
        stale[stale.index('--revision') + 1] = '1'
        self.assertEqual(self.invoke(stale)[1]['reason'], 'revision_conflict')
        self.assertEqual(self.invoke(args)[1]['reason'], 'linked_work_scope_mismatch')
        changed = list(args)
        changed[changed.index('--reason') + 1] = 'Different finding'
        self.assertEqual(self.invoke(changed)[1]['reason'], 'abandonment_conflict')
        store = RunStore(self.state)
        try:
            self.assertEqual(store.get_work(operation['payload']['work_id'])['status'], 'open')
        finally:
            store.close()

    def test_abandon_requires_explicit_acknowledgement_and_state(self):
        operation = self.review_operation()
        args = self.abandon_args(operation)
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(args[:-1])
        self.assertEqual(caught.exception.code, 2)
        del args[3:5]
        self.assertEqual(self.invoke(args)[1]['reason'], 'invalid_worker_configuration_or_local_io')

    def test_run_of_abandoned_request_is_not_successful_work(self):
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            worker.return_value.run.return_value = {'status': 'abandoned', 'stage': 'abandoned',
                'reason': 'manually_abandoned', 'cached': True, 'operation_id': self.operation_id}
            self.assertEqual(self.invoke(self.args())[0], 2)


if __name__ == '__main__':
    unittest.main()
