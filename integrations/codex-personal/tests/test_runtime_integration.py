"""Managed text turns against the real temporary Board, with simulated models."""
import hashlib
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from test_board_worker import Board, Client
from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_guard import RuntimeGuard, RuntimeControlError
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.cli import main


class ControlClient:
    endpoint='https://board.test/mcp'
    def __init__(self, board, raw, session):
        self.board,self.raw,self.session_id=board,raw,session
        self.calls=[]
        self.lost=set()
    def call(self,name,args):
        self.calls.append(name)
        result=getattr(self.board,name)({**self.raw,'session_id':self.session_id},**args)
        if name in self.lost:
            self.lost.remove(name)
            raise TimeoutError('PRIVATE_DIAGNOSTIC')
        return result


@unittest.skipIf(Board is None,'Board sources are not bundled in standalone package')
class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.board=Board(self.root/'board',base_url='https://board.test')
        self.board.ensure_owner('owner','Owner')
        self.human={'kind':'human','owner_id':'owner'}
        self.board.set_policy(self.human,True,True,True,True,[],[])
        self.project=self.board.create_project(self.human,'Project',['owner'],idempotency_key='project')
        self.ticket=self.board.create_ticket(self.human,self.project['id'],'Task',idempotency_key='ticket')
        device=self.board.create_device(self.human,'Local',[])
        self.raw=self.board.authenticate(device['token'])
        self.client=Client(self.board,self.raw)
        registration=self.client.register()
        self.scope={'owner_id':'owner','agent_id':registration['identity']['id'],
            'project_id':self.project['id'],'ticket_id':self.ticket['id'],
            'board_endpoint':self.client.endpoint,'board_session_id':self.client.session_id}
        self.store=RunStore(self.root/'runs');self.addCleanup(self.store.close)
        self.work=WorkJournal(self.root/'work');self.addCleanup(self.work.close)
        self.runtime=RuntimeJournal(self.root/'runtime');self.addCleanup(self.runtime.close)
        self.control=ControlClient(self.board,self.raw,self.client.session_id)
        self.tick=0;self.calls=0;self.during=None;self.result_status='completed';self.cleanup=True
        self.worker=BoardWorker(self.work,self.store,self.client,
            runner_factory=lambda store,**options:PersonalRunner(store,self.transport,**options),
            runtime_guard_factory=lambda scope:RuntimeGuard(self.runtime,self.control,scope,clock=lambda:self.tick))

    def transport(self,settings,prompt,workspace,**callbacks):
        self.calls+=1
        callbacks['on_started'](999999999)
        session=callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        if self.during:self.during(callbacks)
        return {'status':self.result_status,'reason':'verified' if self.result_status=='completed' else 'cancelled',
            'session_id':session,'text':'PRIVATE_MODEL_TEXT','verification':{'cleanup_confirmed':self.cleanup,'tool_free':True}}

    def run_worker(self):
        return self.worker.run(scope=self.scope,request_id='request',instruction='PRIVATE_INSTRUCTION',
            workspace=self.root,executable=sys.executable,model='fixture-model',timeout=10,runtime_managed=True)

    def execution(self):
        values=self.board.get_runtime(self.human)
        self.assertEqual(len(values['executions']),1)
        return values['executions'][0]

    def test_success_is_visible_without_private_content_and_replay_never_reserves(self):
        result=self.run_worker()
        self.assertEqual(result['status'],'complete')
        execution=self.execution()
        self.assertEqual((execution['state'],execution['reason'],execution['outcome']),('finished','completed','completed'))
        self.assertTrue(execution['cleanup_confirmed'])
        self.assertEqual(self.board.get_runtime(self.human)['active_count'],0)
        self.assertNotIn('PRIVATE',json.dumps(self.board.get_runtime(self.human)))
        self.assertTrue(self.run_worker()['cached'])
        self.assertEqual(self.calls,1)
        self.assertEqual(self.control.calls.count('reserve_execution'),1)

    def test_runner_cached_turn_looks_up_same_execution_without_start_or_model(self):
        result=self.run_worker();operation=self.work.get(result['operation_id'])
        prior=list(self.control.calls)
        guard=RuntimeGuard(self.runtime,self.control,self.scope)
        cached=PersonalRunner(self.store,self.transport,runtime_guard=guard).run(operation['payload']['work_id'],
            operation['payload']['run_request_id'],operation['payload']['prompt'],timeout=10)
        self.assertTrue(cached['cached'])
        self.assertEqual(self.control.calls[len(prior):],['lookup_execution'])
        self.assertEqual(self.calls,1)

    def test_pause_during_process_requests_cancel_then_confirms_cleanup(self):
        def pause(callbacks):
            limits=self.board.get_runtime(self.human)['limits']
            self.board.update_runtime_limits(self.human,limits['version'],limits['max_concurrent'],True)
            self.tick=6
            self.assertTrue(callbacks['should_cancel']())
        self.during=pause;self.result_status='cancelled'
        result=self.run_worker()
        self.assertEqual(result['published_status'],'blocked')
        execution=self.execution()
        self.assertEqual((execution['state'],execution['outcome']),('finished','cancelled'))
        self.assertTrue(self.board.get_runtime(self.human)['limits']['paused'])

    def test_uncertain_cleanup_keeps_capacity_and_claim(self):
        self.cleanup=False
        result=self.run_worker()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.execution()['state'],'needs_review')
        self.assertEqual(self.board.get_runtime(self.human)['active_count'],1)
        self.assertIsNotNone(self.board.get_ticket(self.human,self.ticket['id'])['ticket']['claimant'])
        self.assertEqual(self.calls,1)

    def test_owner_pause_before_start_does_not_invoke_model(self):
        limits=self.board.get_runtime(self.human)['limits']
        self.board.update_runtime_limits(self.human,limits['version'],1,True)
        result=self.run_worker()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.calls,0)
        self.assertEqual(self.board.get_runtime(self.human)['executions'],[])

    def test_control_channel_does_not_refresh_registration_after_profile_pause(self):
        def pause(callbacks):
            profile=self.board.get_agent_profile(self.human,self.scope['agent_id'])
            self.board.update_agent_profile(self.human,profile['id'],profile['version'],status='paused')
            self.tick=6;self.assertTrue(callbacks['should_cancel']())
        self.during=pause;self.result_status='cancelled'
        result=self.run_worker()
        self.assertEqual(result['status'],'pending')
        self.assertEqual(self.execution()['state'],'finished')
        self.assertNotIn('register_agent',self.control.calls)

    def test_wrong_scope_is_refused_before_runtime_reservation(self):
        result=self.run_worker();operation=self.work.get(result['operation_id'])
        work=self.store.get_work(operation['payload']['work_id'])
        wrong={**self.scope,'agent_id':'other-agent'}
        guard=RuntimeGuard(self.runtime,self.control,wrong)
        with self.assertRaises(RuntimeControlError):guard.before(work,str(uuid.uuid4()))
        self.assertEqual(self.control.calls.count('reserve_execution'),1)

    def cli(self,*args):
        output=io.StringIO()
        with redirect_stdout(output):code=main(['runtime','--journal',str(self.root/'runtime'),*args])
        self.assertNotIn('PRIVATE',output.getvalue())
        self.assertNotIn(str(self.root),output.getvalue())
        return code,json.loads(output.getvalue())

    def test_cli_list_and_status_are_offline_metadata_only(self):
        self.run_worker()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file') as connect:
            code,listing=self.cli('list')
            self.assertEqual(code,0)
            self.assertEqual(len(listing['executions']),1)
            entry=listing['executions'][0]
            self.assertEqual(entry['phase'],'finished')
            self.assertNotIn('configuration',entry)
            self.assertEqual(self.cli('status','--operation',entry['id']),(0,entry))
            connect.assert_not_called()

    def test_cli_reconciles_lost_finish_from_exact_private_result_without_model(self):
        self.control.lost.add('finish_execution')
        self.assertEqual(self.run_worker()['status'],'needs_review')
        operation=self.runtime.list_status()[0]
        self.assertEqual(operation['phase'],'needs_review')
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',return_value=self.control):
            code,result=self.cli('reconcile','--operation',operation['id'],'--state',str(self.root/'runs'),
                '--connection-file',str(self.root/'connection.json'))
        self.assertEqual((code,result['phase']),(0,'finished'))
        self.assertEqual(self.calls,1)
        self.assertEqual(self.control.calls.count('start_execution'),1)
        self.assertEqual(self.control.calls.count('finish_execution'),1)

    def test_cli_wrong_run_store_never_contacts_control_channel(self):
        self.control.lost.add('finish_execution');self.run_worker()
        operation=self.runtime.list_status()[0];before=list(self.control.calls)
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',return_value=self.control):
            self.assertEqual(self.cli('reconcile','--operation',operation['id'],'--state',str(self.root/'other-runs'),
                '--connection-file',str(self.root/'connection.json'))[0],2)
        self.assertEqual(self.control.calls,before)

    def test_cli_wrong_connection_never_reconciles_another_endpoint(self):
        self.control.lost.add('finish_execution');self.run_worker()
        operation=self.runtime.list_status()[0];before=list(self.control.calls)
        self.control.endpoint='https://other.test/mcp'
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',return_value=self.control):
            self.assertEqual(self.cli('reconcile','--operation',operation['id'],'--state',str(self.root/'runs'),
                '--connection-file',str(self.root/'connection.json'))[0],2)
        self.assertEqual(self.control.calls,before)

