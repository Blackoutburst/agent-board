"""Explicit event CLI: mocked draining, no network or model calls."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from blackoutburst_agents.cli import main
from blackoutburst_agents.event_cli import _public_result
from blackoutburst_agents.event_journal import EventJournalError


class EventCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.instruction = self.root / 'instruction.txt'
        self.instruction.write_text('PRIVATE_LOCAL_INSTRUCTION')
        self.args = ['events', '--journal', str(self.root / 'events'), 'drain',
            '--connection-file', str(self.root / 'connection.json'), '--session', 'session',
            '--owner', 'owner', '--agent', 'agent', '--project', 'project', '--ticket', 'ticket',
            '--instruction-file', str(self.instruction), '--workspace', str(self.root), '--codex', sys.executable]
        self.client = Mock(endpoint='https://board.test/mcp')
        self.receipt = {'status': 'pending', 'reason': 'execution_not_requested', 'after': 4,
                        'ingested_count': 2, 'pages': 1, 'subscription_id': 'subscription', 'batch_id': 'batch'}

    def invoke(self, args=None):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(args or self.args)
        output = stream.getvalue()
        self.assertNotIn('PRIVATE', output)
        self.assertNotIn(str(self.root), output)
        return code, json.loads(output)

    def test_default_is_preview_with_no_runner_or_model_state(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.event_cli.EventJournal'), \
                patch('blackoutburst_agents.event_cli.EventDrainer') as drain, \
                patch('blackoutburst_agents.event_cli.BoardWorker') as worker, \
                patch('blackoutburst_agents.event_cli.RunStore') as runs:
            drain.return_value.drain.return_value = {**self.receipt, 'private': 'PRIVATE_DATA'}
            previous = signal.getsignal(signal.SIGINT)
            code, output = self.invoke()
            self.assertEqual((code, output), (0, self.receipt))
            worker.assert_not_called()
            runs.assert_not_called()
            self.assertIsNone(drain.call_args.args[1])
            self.assertFalse(drain.return_value.drain.call_args.kwargs['execute'])
            self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_execute_requires_both_private_state_paths_before_connection(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file') as client:
            code, output = self.invoke(self.args + ['--execute'])
            self.assertEqual(code, 2)
            self.assertEqual(output['reason'], 'invalid_event_configuration_or_io')
            client.assert_not_called()

    def test_explicit_execution_constructs_worker_and_restores_handlers(self):
        args = self.args[:3] + ['--work-journal', str(self.root / 'work'), '--state', str(self.root / 'runs')] + self.args[3:] + ['--execute']
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.event_cli.EventJournal'), \
                patch('blackoutburst_agents.event_cli.WorkJournal'), \
                patch('blackoutburst_agents.event_cli.RunStore') as runs, \
                patch('blackoutburst_agents.event_cli.BoardWorker') as worker, \
                patch('blackoutburst_agents.event_cli.EventDrainer') as drain:
            drain.return_value.drain.return_value = {**self.receipt, 'status': 'complete', 'reason': 'worker_complete'}
            previous = signal.getsignal(signal.SIGTERM)
            code, output = self.invoke(args)
            self.assertEqual((code, output['status']), (0, 'complete'))
            self.assertTrue(drain.return_value.drain.call_args.kwargs['execute'])
            self.assertIs(drain.call_args.args[1], worker.return_value)
            runs.return_value.close.assert_called_once()
            self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_human_option_uses_opt_in_client_and_pins_recipient_in_preview(self):
        with patch('blackoutburst_agents.event_cli.HumanEventBoardClient.from_connection_file', return_value=self.client) as human, \
                patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file') as plain, \
                patch('blackoutburst_agents.event_cli.EventJournal'), \
                patch('blackoutburst_agents.event_cli.BoardWorker') as worker, \
                patch('blackoutburst_agents.event_cli.EventDrainer') as drain:
            drain.return_value.drain.return_value=self.receipt
            code,output=self.invoke(self.args+['--human-owner','named-human'])
            self.assertEqual((code,output['status']),(0,'pending'))
            self.assertEqual(drain.return_value.drain.call_args.kwargs['human_owner_id'],'named-human')
            human.assert_called_once();plain.assert_not_called();worker.assert_not_called()

    def test_invalid_human_recipient_is_rejected_before_any_connection(self):
        with patch('blackoutburst_agents.event_cli.HumanEventBoardClient.from_connection_file') as human, \
                patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file') as plain:
            for value in ('',' ','x\ny','\ud800','x'*257):
                self.assertEqual(self.invoke(self.args+['--human-owner',value])[0],2)
            human.assert_not_called();plain.assert_not_called()

    def test_invalid_bounds_do_not_connect(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file') as client:
            for extra in (['--after', '-1'], ['--limit', '201'], ['--max-pages', '0'],
                          ['--max-pages', '21'], ['--timeout', 'nan']):
                self.assertEqual(self.invoke(self.args + extra)[0], 2)
            client.assert_not_called()

    def test_failures_are_sanitized_and_handlers_restored(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.event_cli.EventJournal'), \
                patch('blackoutburst_agents.event_cli.EventDrainer') as drain:
            drain.return_value.drain.side_effect = RuntimeError('PRIVATE_REMOTE_BODY')
            previous = signal.getsignal(signal.SIGINT)
            code, output = self.invoke()
            self.assertEqual((code, output['status']), (2, 'blocked'))
            self.assertEqual(signal.getsignal(signal.SIGINT), previous)

    def test_public_receipt_whitelists_and_validates_every_field(self):
        self.assertEqual(_public_result({**self.receipt, 'model_text': 'PRIVATE'}), self.receipt)
        for change in ({'reason': 'PRIVATE remote text'}, {'status': 'bogus'}, {'after': True},
                       {'pages': -1}, {'batch_id': 'PRIVATE/key'}, {'worker_operation_id': ['private']}):
            with self.assertRaises(ValueError):
                _public_result({**self.receipt, **change})

    def test_status_and_reconcile_remain_local_without_model(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient') as client, \
                patch('blackoutburst_agents.event_cli.BoardWorker') as worker, \
                patch('blackoutburst_agents.event_cli.EventJournal') as journal, \
                patch('blackoutburst_agents.event_cli.WorkJournal') as work:
            events = journal.return_value.__enter__.return_value
            events.status.return_value = {'id': 'subscription', 'after': 4, 'pending_count': 0}
            code, output = self.invoke(['events', '--journal', str(self.root / 'events'),
                                        'status', '--subscription', 'subscription'])
            self.assertEqual((code, output['after']), (0, 4))
            events.get_batch.return_value = {'subscription_id': 'subscription'}
            events.get_subscription.return_value = {'scope': {'ticket_id': 'ticket'}}
            events.resolve_batch.return_value = {'id': 'batch'}
            operation = {'id': 'operation', 'stage': 'abandoned', 'scope': {'ticket_id': 'ticket'}}
            work.return_value.__enter__.return_value.get.return_value = operation
            code, output = self.invoke(['events', '--journal', str(self.root / 'events'),
                '--work-journal', str(self.root / 'work'), 'reconcile', '--batch', 'batch', '--operation', 'operation'])
            self.assertEqual((code, output['decision']), (0, 'abandoned'))
            events.resolve_batch.assert_called_once_with('batch', operation)
            client.assert_not_called()
            worker.assert_not_called()

    def test_reconcile_lookup_is_explicit_read_only_and_uses_original_session(self):
        scope={'ticket_id':'ticket','board_session_id':'original-session','board_endpoint':'https://board.test/mcp'}
        configuration={'instruction':'PRIVATE_LOCAL_INSTRUCTION'}
        operation={'id':'operation','stage':'abandoned','scope':scope,'request_id':'request','configuration':configuration}
        request={'id':'known-request'}
        args=['events','--journal',str(self.root/'events'),'--work-journal',str(self.root/'work'),
            'reconcile','--batch','batch','--operation','operation','--connection-file',str(self.root/'connection.json')]
        with patch('blackoutburst_agents.event_cli.EventJournal') as journal, \
                patch('blackoutburst_agents.event_cli.WorkJournal') as work, \
                patch('blackoutburst_agents.event_cli.HumanEventBoardClient.from_connection_file',return_value=self.client) as connect, \
                patch('blackoutburst_agents.event_cli.BoardWorker') as worker, \
                patch('blackoutburst_agents.event_cli.RunStore') as runs:
            events=journal.return_value.__enter__.return_value
            events.get_batch.return_value={'subscription_id':'subscription','request_id':'request'}
            events.get_subscription.return_value={'scope':scope,'configuration':configuration}
            events.resolve_batch.return_value={'id':'batch'}
            work.return_value.__enter__.return_value.get.return_value=operation
            worker.return_value.lookup_human_question.return_value=request
            code,output=self.invoke(args)
            self.assertEqual((code,output['decision']),(0,'abandoned'))
            self.assertEqual(connect.call_args.kwargs['session_id'],'original-session')
            events.resolve_batch.assert_called_once_with('batch',operation,verified_human_request=request)
            worker.return_value.run.assert_not_called();runs.assert_not_called()
            events.resolve_batch.reset_mock()
            worker.return_value.lookup_human_question.return_value=None
            code,output=self.invoke(args)
            self.assertEqual((code,output['status'],output['reason']),(2,'needs_review','human_request_unresolved'))
            events.resolve_batch.assert_not_called()
            worker.return_value.lookup_human_question.reset_mock()
            connect.reset_mock()
            for changed in ({'scope':{**scope,'ticket_id':'another-ticket'}}, {'request_id':'another-request'},
                            {'configuration':{'instruction':'another instruction'}}):
                work.return_value.__enter__.return_value.get.return_value={**operation,**changed}
                self.assertEqual(self.invoke(args)[0],2)
            connect.assert_not_called();worker.return_value.lookup_human_question.assert_not_called()

    def test_ambiguous_local_reconcile_reports_actionable_review_state(self):
        with patch('blackoutburst_agents.event_cli.EventJournal') as journal, \
                patch('blackoutburst_agents.event_cli.WorkJournal') as work, \
                patch('blackoutburst_agents.event_cli.HumanEventBoardClient.from_connection_file') as connect:
            events=journal.return_value.__enter__.return_value
            events.get_batch.return_value={'subscription_id':'subscription'}
            events.get_subscription.return_value={'scope':{'ticket_id':'ticket'}}
            events.resolve_batch.side_effect=EventJournalError('human_request_unresolved')
            work.return_value.__enter__.return_value.get.return_value={'scope':{'ticket_id':'ticket'}}
            code,output=self.invoke(['events','--journal',str(self.root/'events'),'--work-journal',str(self.root/'work'),
                'reconcile','--batch','batch','--operation','operation'])
            self.assertEqual((code,output['status'],output['reason']),(2,'needs_review','human_request_unresolved'))
            connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
