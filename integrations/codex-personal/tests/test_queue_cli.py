"""Queue command boundaries with doubles; no network or model processes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from blackoutburst_agents.cli import main
from blackoutburst_agents.queue_cli import _public
from blackoutburst_agents.queue_journal import QueueJournal


class QueueCliTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.instruction=self.root/'instruction.txt'
        self.instruction.write_text('PRIVATE_LOCAL_INSTRUCTION')
        self.args=['queue','--journal',str(self.root/'queue'),'run',
            '--connection-file',str(self.root/'connection.json'),'--session','session',
            '--owner','owner','--agent','agent','--project','project','--request','request',
            '--instruction-file',str(self.instruction),'--workspace',str(self.root),
            '--codex',sys.executable,'--model','fixture-model']
        self.registration={'identity':{'id':'agent','owner_id':'owner','device_id':'device','agent_kind':'codex','name':'PRIVATE_PROFILE_NAME'},
            'session':{'id':'session','agent_id':'agent','owner_id':'owner','device_id':'device','agent_kind':'codex','skills':[]},
            'policy':{'enabled':True,'allow_read':True,'allow_requests':False}}
        self.client=Mock(endpoint='https://board.test/mcp')
        self.client.register.return_value=deepcopy(self.registration)
        self.client.call.side_effect=lambda name,args: ({'id':'project','participants':['owner','human'],'archived':False}
            if name=='get_project' else {'ready':[{'body':'PRIVATE_READY'}], 'recovery':[], 'waiting':[{'body':'PRIVATE_WAITING'}]})
        self.receipt={'status':'complete','stage':'complete','reason':'publication_confirmed','cached':False,
                      'operation_id':'queue-operation','ticket_id':'ticket','worker_operation_id':'worker-operation'}

    def invoke(self,args=None):
        output=io.StringIO()
        with redirect_stdout(output): code=main(self.args if args is None else args)
        raw=output.getvalue()
        self.assertNotIn('PRIVATE',raw)
        self.assertNotIn(str(self.root),raw)
        return code,json.loads(raw)

    def executing(self,*extra):
        return self.args[:3]+['--work-journal',str(self.root/'work'),'--state',str(self.root/'runs')]+self.args[3:]+['--execute',*extra]

    def execution_doubles(self,stack):
        modules={name:stack.enter_context(patch('blackoutburst_agents.queue_cli.'+name))
                 for name in ('QueueJournal','WorkJournal','RunStore','BoardWorker','QueueWorker')}
        modules['QueueWorker'].return_value.run.return_value=dict(self.receipt)
        return modules

    def test_preview_is_read_only_without_any_journal_or_worker(self):
        with ExitStack() as stack:
            connect=stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file',return_value=self.client))
            doubles=self.execution_doubles(stack)
            code,result=self.invoke()
            self.assertEqual((code,result),(0,{'status':'preview','ready_count':1,'recovery_count':0,'waiting_count':1}))
            connect.assert_called_once()
            self.client.register.assert_called_once_with()
            self.client.register_for_selection.assert_not_called()
            self.assertEqual([call.args[0] for call in self.client.call.call_args_list],['get_project','get_work'])
            self.assertEqual(self.client.call.call_args_list[-1].args[1],{'project_id':'project','limit':20})
            for value in doubles.values(): value.assert_not_called()
            self.assertFalse((self.root/'queue').exists())

    def test_model_or_policy_is_required_before_connection(self):
        args=self.args[:-2]
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file') as connect:
            code,result=self.invoke(args)
            self.assertEqual((code,result['reason']),(2,'invalid_queue_configuration_or_io'))
            connect.assert_not_called()

    def test_execute_requires_both_private_state_paths_before_connection(self):
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file') as connect:
            for prefix in ([],['--work-journal',str(self.root/'work')],['--state',str(self.root/'runs')]):
                args=self.args[:3]+prefix+self.args[3:]+['--execute']
                code,result=self.invoke(args)
                self.assertEqual((code,result['reason']),(2,'invalid_queue_configuration_or_io'))
            connect.assert_not_called()

    def test_dispatches_exactly_one_worker_with_pinned_options_and_restores_handlers(self):
        with ExitStack() as stack:
            stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file',return_value=self.client))
            doubles=self.execution_doubles(stack)
            doubles['QueueWorker'].return_value.run.return_value={**self.receipt,'private':'PRIVATE_MODEL_RESPONSE'}
            old={s:signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)}
            code,result=self.invoke(self.executing())
            self.assertEqual((code,result),(0,self.receipt))
            invocation=doubles['QueueWorker'].return_value.run
            invocation.assert_called_once()
            values=invocation.call_args.kwargs
            self.assertEqual(values['scope'],{'owner_id':'owner','agent_id':'agent','board_session_id':'session','board_endpoint':'https://board.test/mcp'})
            self.assertEqual(values['request_id'],'request')
            self.assertEqual(values['project_id'],'project')
            self.assertEqual(values['model'],'fixture-model')
            self.assertEqual(values['instruction'],'PRIVATE_LOCAL_INSTRUCTION')
            self.assertIsNone(values['human_owner_id'])
            self.assertFalse(values['should_cancel']())
            self.assertIs(doubles['QueueWorker'].call_args.args[1],doubles['BoardWorker'].return_value)
            doubles['RunStore'].return_value.close.assert_called_once()
            self.assertEqual({s:signal.getsignal(s) for s in old},old)
            self.client.call.assert_not_called()

    def test_human_recipient_is_opt_in_and_forwarded_to_one_worker(self):
        with ExitStack() as stack:
            human=stack.enter_context(patch('blackoutburst_agents.queue_cli.HumanQueueBoardClient.from_connection_file',return_value=self.client))
            plain=stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file'))
            doubles=self.execution_doubles(stack)
            self.assertEqual(self.invoke(self.executing('--human-owner','human'))[0],0)
            human.assert_called_once();plain.assert_not_called()
            self.assertEqual(doubles['QueueWorker'].return_value.run.call_args.kwargs['human_owner_id'],'human')

    def test_invalid_configuration_bounds_are_rejected_before_connection(self):
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file') as plain, \
                patch('blackoutburst_agents.queue_cli.HumanQueueBoardClient.from_connection_file') as human:
            for extra in (['--timeout','nan'],['--timeout','0'],['--timeout','301'],['--timeout','inf'],
                          ['--model',''],['--model',' '],['--model','model\nprivate'],['--model','m'*257],
                          ['--human-owner',''],['--human-owner',' '],['--human-owner','h'*257],
                          ['--human-owner','\ud800'],['--human-owner','h\nprivate'],
                          ['--workspace',str(self.instruction)],['--codex','relative-codex']):
                with self.subTest(extra=extra):self.assertEqual(self.invoke(self.args+extra)[0],2)
            plain.assert_not_called();human.assert_not_called()

    def test_invalid_instruction_file_never_connects_or_creates_state(self):
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file') as connect:
            for value in ('', ' ', 'é'*6001, 'bad\x00instruction'):
                self.instruction.write_text(value)
                self.assertEqual(self.invoke(self.executing())[0],2)
            connect.assert_not_called()
            self.assertFalse((self.root/'queue').exists())
            self.assertFalse((self.root/'work').exists())
            self.assertFalse((self.root/'runs').exists())

    def test_public_receipt_is_a_strict_metadata_whitelist(self):
        self.assertEqual(_public({**self.receipt,'model_text':'PRIVATE','configuration':{'token':'PRIVATE'}}),self.receipt)
        for mutation in ({'status':'done'},{'stage':'unknown'},{'reason':'PRIVATE diagnostic text'},
                         {'cached':1},{'operation_id':'/private/file'},{'ticket_id':['private']},
                         {'worker_operation_id':'id\nprivate'}):
            with self.assertRaises(ValueError):_public({**self.receipt,**mutation})

    def test_failures_hide_diagnostics_and_restore_handlers_and_close_store(self):
        with ExitStack() as stack:
            stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file',return_value=self.client))
            doubles=self.execution_doubles(stack)
            doubles['QueueWorker'].return_value.run.side_effect=RuntimeError('PRIVATE remote diagnostic')
            old=signal.getsignal(signal.SIGINT)
            code,result=self.invoke(self.executing())
            self.assertEqual((code,result['reason']),(2,'invalid_queue_configuration_or_io'))
            self.assertEqual(signal.getsignal(signal.SIGINT),old)
            doubles['RunStore'].return_value.close.assert_called_once()

    def test_status_and_private_inspect_are_offline(self):
        scope={'owner_id':'owner','agent_id':'agent','board_session_id':'session','board_endpoint':'https://board.test/mcp'}
        config={'project_id':'project','instruction':'PRIVATE_LOCAL_INSTRUCTION','workspace':str(self.root),
                'executable':sys.executable,'model':'fixture-model','timeout':120}
        with QueueJournal(self.root/'queue') as journal:
            with journal.lock(scope):operation=journal.begin(scope,'request',config)
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient') as client, \
                patch('blackoutburst_agents.queue_cli.QueueWorker') as worker, \
                patch('blackoutburst_agents.queue_cli.RunStore') as runs:
            code,result=self.invoke(['queue','--journal',str(self.root/'queue'),'status','--operation',operation['id']])
            self.assertEqual((code,result['stage']),(0,'new'))
            self.assertNotIn('configuration',result);self.assertNotIn('scope',result)
            destination=self.root/'private-inspection.json'
            code,result=self.invoke(['queue','--journal',str(self.root/'queue'),'inspect','--operation',operation['id'],'--output',str(destination)])
            self.assertEqual((code,result['status']),(0,'exported'))
            self.assertEqual(destination.stat().st_mode&0o777,0o600)
            self.assertIn('PRIVATE_LOCAL_INSTRUCTION',destination.read_text())
            client.assert_not_called();worker.assert_not_called();runs.assert_not_called()

    def test_selection_reconciliation_only_constructs_lookup_coordinator(self):
        with ExitStack() as stack:
            connect=stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file',return_value=self.client))
            doubles=self.execution_doubles(stack)
            operation={'id':'queue-operation','scope':{'board_session_id':'original-session'}}
            doubles['QueueJournal'].return_value.__enter__.return_value.get.return_value=operation
            doubles['QueueWorker'].return_value.reconcile_selection.return_value={**self.receipt,'status':'needs_review','stage':'needs_review','reason':'selection_unconfirmed'}
            code,result=self.invoke(['queue','--journal',str(self.root/'queue'),'reconcile-selection',
                '--operation','queue-operation','--connection-file',str(self.root/'connection.json')])
            self.assertEqual((code,result['reason']),(2,'selection_unconfirmed'))
            self.assertEqual(connect.call_args.kwargs['session_id'],'original-session')
            self.assertIsNone(doubles['QueueWorker'].call_args.args[1])
            doubles['QueueWorker'].return_value.reconcile_selection.assert_called_once_with('queue-operation')
            doubles['QueueWorker'].return_value.run.assert_not_called()
            for name in ('RunStore','BoardWorker','WorkJournal'):doubles[name].assert_not_called()


if __name__=='__main__':unittest.main()
