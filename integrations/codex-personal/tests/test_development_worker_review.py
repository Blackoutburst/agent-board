"""Independent ordinary CLI/parent boundary regressions; all models are fake."""
import unittest
from unittest.mock import patch
import uuid
import io
from contextlib import redirect_stdout

import test_development_worker as fixture
from blackoutburst_agents.development_cli import _inspect
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_planner import _agent_id
from blackoutburst_agents.cli import main


class DevelopmentWorkerReviewTests(unittest.TestCase):
    setUp=fixture.DevelopmentWorkerTests.setUp
    backend_factory=fixture.DevelopmentWorkerTests.backend_factory
    make_worker=fixture.DevelopmentWorkerTests.make_worker
    prepare=fixture.DevelopmentWorkerTests.prepare
    transport=fixture.DevelopmentWorkerTests.transport
    action=fixture.DevelopmentWorkerTests.action
    successful_actions=fixture.DevelopmentWorkerTests.successful_actions
    read_ticket=fixture.DevelopmentWorkerTests.read_ticket

    def test_receipt_only_reconcile_does_not_claim_a_prepared_new_operation(self):
        prepared=self.prepare();before=len(self.client.calls)
        self.worker.run(prepared['operation_id'],reconcile=True)
        self.assertIsNone(self.read_ticket()['claimant'])
        self.assertFalse(any(name in ('claim_ticket','renew_ticket','update_ticket') for name,_ in self.client.calls[before:]))
        self.assertFalse(self.inferences);self.assertFalse(self.backend.calls);self.assertFalse(self.control.calls)

    def test_receipt_only_reconcile_does_not_execute_an_unapplied_claim_intent(self):
        prepared=self.prepare();original=self.client.call
        def crash_before_effect(name,args):
            if name=='claim_ticket':raise fixture.Crash()
            return original(name,args)
        with patch.object(self.client,'call',side_effect=crash_before_effect):
            with self.assertRaises(fixture.Crash):self.worker.run(prepared['operation_id'])
        op=self.journal.get(prepared['operation_id'])
        self.assertEqual(op['stage'],'claiming');self.assertIsNone(self.read_ticket()['claimant'])
        before=len(self.client.calls)
        self.worker.run(prepared['operation_id'],reconcile=True)
        self.assertIsNone(self.read_ticket()['claimant'])
        self.assertFalse(any(name in ('claim_ticket','renew_ticket','update_ticket') for name,_ in self.client.calls[before:]))
        self.assertFalse(self.inferences);self.assertFalse(self.backend.calls);self.assertFalse(self.control.calls)

    def test_private_inspection_refuses_replaced_checkout_before_export(self):
        prepared=self.prepare()
        self.root.rename(self.base/'previous-checkout')
        self.root.mkdir(mode=0o700)
        destination=self.root/'private-export.json'
        with self.assertRaises(DevelopmentError):_inspect(self.journal,prepared['operation_id'],destination)
        self.assertFalse(destination.exists())

    def test_private_inspection_refuses_replaced_planner_before_export(self):
        prepared=self.prepare()
        self.planner_root.rename(self.base/'previous-planner')
        self.planner_root.mkdir(mode=0o700)
        destination=self.planner_root/'private-export.json'
        with self.assertRaises(DevelopmentError):_inspect(self.journal,prepared['operation_id'],destination)
        self.assertFalse(destination.exists())

    def test_new_parent_does_not_adopt_an_existing_provider_dialogue(self):
        prior=self.store.create_work(owner_id=self.scope['owner_id'],agent_id=_agent_id(self.scope),
            project_id=self.scope['project_id'],ticket_id=self.scope['ticket_id'],workspace=self.policy.workspace,
            executable=self.policy.executable,model='fixture-model')
        provider=str(uuid.uuid4())
        with self.store.work_lock(prior['id']):
            entry=self.store.begin_turn(prior['id'],'prior-plan','Ordinary prior fixture context')
            self.store.on_session(entry['turn']['id'],provider)
            self.store.finish_turn(entry['turn']['id'],dict(status='completed',reason='completed',session_id=provider,
                text='Prior fixture result',verification=dict(cleanup_confirmed=True,tool_free=True)))
        observed=[];transport=self.worker.transport
        def record_resume(*args,**kwargs):
            observed.append(kwargs['resume_session_id'])
            return transport(*args,**kwargs)
        self.worker.transport=record_resume;self.successful_actions();prepared=self.prepare()
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertTrue(observed);self.assertIsNone(observed[0])
        op=self.journal.get(prepared['operation_id'])
        self.assertNotEqual(op['payload']['parent_binding']['child_scope']['work_id'],prior['id'])
        self.assertEqual(self.store.get_work(prior['id'])['provider_session_id'],provider)

    def _unfinished_model(self):
        self.successful_actions();prepared=self.prepare();original=self.control.call;failed=[False]
        def lose_before_finish(name,args):
            if name=='finish_execution' and not failed[0]:failed[0]=True;raise TimeoutError('ordinary lost request')
            return original(name,args)
        with patch.object(self.control,'call',side_effect=lose_before_finish):result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'],1)
        self.board.set_policy(self.human,False,False,False,False,[],[])
        operation=self.runtime.list_status()[0]
        return prepared['operation_id'],operation['id']

    def _cleanup(self,parent_id,runtime_id):
        output=io.StringIO()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',return_value=self.control),redirect_stdout(output):
            code=main(['runtime','--journal',str(self.runtime.directory),'reconcile','--operation',runtime_id,
                '--state',str(self.store.state_dir),'--connection-file',str(self.base/'unused-connection'),
                '--development-operation',parent_id])
        return code,output.getvalue()

    def test_exact_parent_cleanup_remains_possible_after_permissions_withdrawal(self):
        parent,runtime_id=self._unfinished_model();before=list(self.control.calls);inferences=len(self.inferences)
        code,output=self._cleanup(parent,runtime_id)
        self.assertEqual(code,0,output)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'],0)
        self.assertEqual(self.control.calls[len(before):],['lookup_execution','finish_execution'])
        self.assertEqual(len(self.inferences),inferences)
        self.assertEqual((self.root/'main.py').read_text(),'value = 1\n')

    def test_wrong_parent_cleanup_refuses_before_runtime_lookup(self):
        _,runtime_id=self._unfinished_model();before=list(self.control.calls)
        code,_=self._cleanup(str(uuid.uuid4()),runtime_id)
        self.assertEqual(code,2)
        self.assertEqual(self.control.calls,before)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'],1)


if __name__=='__main__':unittest.main()
