"""Development Board workflow on a temporary real Board; model/native work fake."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from test_board_worker import Board, Client
from test_runtime_integration import ControlClient
from test_dev_planner import FakePolicy
from test_development_broker import FakeTestBackend
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, digest, prepare_manifest
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal


class FixtureClient(Client):
    def register_initial(self):
        if hasattr(self,'registration'):
            raise AssertionError('No implicit registration')
        return self.register()
    def adopt_registration(self):
        pass


class FixtureBackend(FakeTestBackend):
    def __init__(self):
        super().__init__()
        self.document={'plan_hash':digest(b'fixture named tests')}
        self.supervision={'fixture':'simulated supervision'}
    def close(self):
        pass


class Crash(BaseException):
    pass


@unittest.skipIf(Board is None,'Board sources not bundled')
class DevelopmentWorkerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup);self.base=Path(temp.name).resolve()
        self.board=Board(self.base/'board')
        self.board.ensure_owner('owner','Owner');self.human={'kind':'human','owner_id':'owner'}
        self.board.set_policy(self.human,True,True,True,True,
                              ['repo.read_file','repo.patch_file','repo.run_test'],['repo:fixture'])
        self.project=self.board.create_project(self.human,'Fixture',['owner'],idempotency_key='project')
        self.ticket=self.board.create_ticket(self.human,self.project['id'],'Fixture task',idempotency_key='ticket')
        device=self.board.create_device(self.human,'Fixture',[]);self.raw=self.board.authenticate(device['token'])
        self.client=FixtureClient(self.board,self.raw)
        registration=self.client.register()
        # prepare will explicitly register once with this existing identity;
        # this initial setup only obtains the controller manifest's scope pins.
        del self.client.registration
        self.client.register_count=0
        self.scope=dict(owner_id='owner',agent_id=registration['identity']['id'],project_id=self.project['id'],
            ticket_id=self.ticket['id'],board_endpoint=self.client.endpoint,board_session_id=self.client.session_id)
        self.manifest_scope={key:self.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        self.manifest_scope.update(device_id=registration['identity']['device_id'],session_id=self.client.session_id,
                                   profile_version=registration['profile_version'])
        self.root=self.base/'checkout';self.root.mkdir(mode=0o700);(self.root/'main.py').write_text('value = 1\n')
        self.planner_root=self.base/'empty';self.planner_root.mkdir(mode=0o700)
        self.broker_dir=self.base/'broker'
        self.backend=FixtureBackend()
        prepare_manifest(self.broker_dir,root=self.root,base_sha='a'*40,scope=self.manifest_scope,
            paths=['main.py'],resource='repo:fixture',authorize_scope=lambda *_:True,
            tests=[NamedTest('unit',backend_id=self.backend.backend_id)])
        self.backend_dir=self.base/'backend';self.backend_dir.mkdir(mode=0o700)
        self.policy_dir=self.base/'policy';self.policy_dir.mkdir(mode=0o700)
        self.policy=FakePolicy(self.planner_root);self.policy.close=lambda:None
        self.store=RunStore(self.base/'runs');self.addCleanup(self.store.close)
        self.runtime=RuntimeJournal(self.base/'runtime');self.addCleanup(self.runtime.close)
        self.journal=WorkJournal(self.base/'work');self.addCleanup(self.journal.close)
        self.control=ControlClient(self.board,self.raw,self.client.session_id)
        self.actions=[];self.inferences=[];self.after_model=None
        self.worker=self.make_worker()

    def backend_factory(self,directory,*,manifest,authorize_scope):
        self.backend.authorize_scope=authorize_scope
        return self.backend

    def make_worker(self,**options):
        values=dict(journal=self.journal,store=self.store,runtime_journal=self.runtime,client=self.client,
            control_client=self.control,backend_factory=self.backend_factory,policy_factory=lambda _:self.policy,
            _transport=self.transport,_test_anchor=self.base/'anchor',_allow_fixture_backend=True)
        values.update(options);return DevelopmentBoardWorker(**values)

    def prepare(self,**options):
        values=dict(scope=self.scope,request_id='fixture',instruction='Change the fixture and verify it.',
            broker_directory=self.broker_dir,backend_directory=self.backend_dir,
            planner_policy_directory=self.policy_dir,model='fixture-model')
        values.update(options);return self.worker.prepare(**values)

    def transport(self,settings,prompt,workspace,**callbacks):
        self.inferences.append(json.loads(prompt))
        callbacks['on_started'](999999999)
        sid=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](sid)
        if self.after_model:self.after_model(callbacks)
        return {'status':'completed','reason':'verified','session_id':sid,'text':json.dumps(self.actions.pop(0)),
            'verification':dict(tool_free=True,cleanup_confirmed=True,exit_zero=True,turn_completed=True,
                                session_verified=True,authentication='chatgpt')}

    def action(self,kind,**values):return {'schema':1,'kind':kind,**values}
    def successful_actions(self):
        self.actions=[self.action('patch',path='main.py',expected_sha256=digest(b'value = 1\n'),content='value = 2\n'),
                      self.action('test',name='unit'),self.action('final',summary='verified')]
    def read_ticket(self):return self.board.get_ticket(self.human,self.ticket['id'])['ticket']

    def test_prepare_is_read_plan_only_and_run_captures_post_claim_context(self):
        self.successful_actions();prepared=self.prepare()
        self.assertEqual(prepared['status'],'prepared')
        self.assertIsNone(self.read_ticket()['claimant'])
        self.assertFalse(self.inferences);self.assertFalse(self.backend.calls);self.assertFalse(self.control.calls)
        self.assertFalse((self.base/'anchor').exists())
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(self.client.register_count,1)
        self.assertEqual(len(self.backend.calls),1)
        self.assertEqual((self.root/'main.py').read_text(),'value = 2\n')
        post=self.read_ticket()
        self.assertEqual(post['status'],'in_progress');self.assertIsNotNone(post['claimant'])
        context=self.inferences[0]['board_context']
        self.assertEqual(context['sources']['ticket']['version'],post['version'])
        self.assertEqual(context['data']['ticket']['status'],'in_progress')
        self.assertNotIn('token',json.dumps(context).lower())
        self.assertEqual(self.worker.run(prepared['operation_id']),result)
        self.assertEqual(len(self.inferences),3)

    def test_empty_real_actions_resources_refuse_before_claim_or_planner(self):
        self.board.set_policy(self.human,True,True,True,True,[],[])
        with self.assertRaises(DevelopmentError) as caught:self.prepare()
        self.assertEqual(caught.exception.code,'development_grants_unavailable')
        self.assertIsNone(self.read_ticket()['claimant'])
        self.assertFalse(self.inferences);self.assertFalse(self.control.calls)
        self.assertFalse((self.base/'anchor').exists())

    def test_context_change_between_prepare_and_run_preserves_no_claim_no_model(self):
        prepared=self.prepare()
        ticket=self.read_ticket();self.board.update_ticket(self.human,ticket['id'],ticket['version'],description='Changed externally')
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertIsNone(self.read_ticket()['claimant']);self.assertFalse(self.inferences)

    def test_known_owned_renewal_is_applied_before_final_context_capture(self):
        ticket=self.read_ticket();self.board.claim_ticket(self.client.actor,ticket['id'],ticket['version'])
        self.successful_actions();prepared=self.prepare()
        initial=self.read_ticket()['version']
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(self.inferences[0]['board_context']['sources']['ticket']['version'],initial+1)
        self.assertEqual([name for name,_ in self.client.calls].count('renew_ticket'),1)

    def test_lost_claim_response_reconciles_exact_transition_without_second_claim(self):
        self.successful_actions();prepared=self.prepare();self.client.lost.add('claim_ticket')
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual([name for name,_ in self.client.calls].count('claim_ticket'),1)

    def test_external_edit_during_claim_is_not_absorbed_into_final_context(self):
        prepared=self.prepare();original=self.client.call
        def mutate(name,args):
            result=original(name,args)
            if name=='claim_ticket':
                ticket=self.read_ticket();self.board.update_ticket(self.human,ticket['id'],ticket['version'],description='External change')
            return result
        self.client.call=mutate
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['reason'],'development_context_changed_during_claim')
        self.assertFalse(self.inferences);self.assertIsNotNone(self.read_ticket()['claimant'])

    def test_context_change_during_model_refuses_before_patch(self):
        prepared=self.prepare();self.successful_actions()
        def mutate(callbacks):
            ticket=self.read_ticket();self.board.update_ticket(self.human,ticket['id'],ticket['version'],description='Changed')
            self.assertTrue(callbacks['should_cancel']())
        self.after_model=mutate
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual((self.root/'main.py').read_text(),'value = 1\n')
        self.assertEqual(len(self.inferences),1)

    def test_crash_after_parent_binding_blocks_alternate_journal_before_claim(self):
        prepared=self.prepare();original=self.worker._save
        def crash(op,stage=None,**values):
            if 'parent_binding' in values:raise Crash()
            return original(op,stage,**values)
        self.worker._save=crash
        with self.assertRaises(Crash):self.worker.run(prepared['operation_id'])
        self.assertIsNone(self.read_ticket()['claimant'])
        other=WorkJournal(self.base/'different-work');self.addCleanup(other.close)
        worker=self.make_worker(journal=other)
        self.worker._save=original
        # Explicit planning is still a read-only action. Its run cannot claim
        # a root whose parent intent already exists under a different journal.
        registration=self.client.registration
        self.client.register_initial=lambda:registration
        alternate=worker.prepare(scope=self.scope,request_id='other',instruction='Change the fixture and verify it.',
            broker_directory=self.broker_dir,backend_directory=self.backend_dir,
            planner_policy_directory=self.policy_dir,model='fixture-model')
        calls_before=len(self.client.calls)
        result=worker.run(alternate['operation_id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(len(self.client.calls),calls_before)
        self.assertIsNone(self.read_ticket()['claimant'])
        self.assertFalse(self.inferences)

    def test_crash_after_claim_can_resume_without_new_claim_or_context_adoption(self):
        prepared=self.prepare();self.successful_actions();original=self.worker._save
        failed=[False]
        def crash(op,stage=None,**values):
            if stage=='claimed' and not failed[0]:failed[0]=True;raise Crash()
            return original(op,stage,**values)
        self.worker._save=crash
        with self.assertRaises(Crash):self.worker.run(prepared['operation_id'])
        self.worker._save=original
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual([name for name,_ in self.client.calls].count('claim_ticket'),1)

    def test_stop_before_run_is_durable_and_offline(self):
        prepared=self.prepare();prior=len(self.client.calls)
        self.worker.stop(prepared['operation_id'])
        self.assertEqual(len(self.client.calls),prior)
        self.assertEqual(self.worker.run(prepared['operation_id'])['reason'],'development_stop_requested')
        self.assertIsNone(self.read_ticket()['claimant']);self.assertFalse(self.inferences)

    def test_stop_during_model_does_not_wait_for_work_lock(self):
        prepared=self.prepare();self.successful_actions()
        def stop(callbacks):
            result=self.worker.stop(prepared['operation_id'])
            self.assertTrue(result['stop_requested'])
            self.assertTrue(callbacks['should_cancel']())
        self.after_model=stop
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual((self.root/'main.py').read_text(),'value = 1\n')
        self.assertEqual(len(self.inferences),1)

    def test_crash_after_context_seal_resumes_without_claim_or_renew_again(self):
        prepared=self.prepare();self.successful_actions();original=self.worker._save
        failed=[False]
        def crash(op,stage=None,**values):
            result=original(op,stage,**values)
            if stage=='prepared' and not failed[0]:failed[0]=True;raise Crash()
            return result
        self.worker._save=crash
        with self.assertRaises(Crash):self.worker.run(prepared['operation_id'])
        self.worker._save=original
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual([name for name,_ in self.client.calls].count('claim_ticket'),1)
        self.assertNotIn('renew_ticket',[name for name,_ in self.client.calls])

    def test_lost_model_runtime_finish_reconciles_same_parent_without_new_claim(self):
        self.successful_actions();prepared=self.prepare();self.control.lost.add('finish_execution')
        first=self.worker.run(prepared['operation_id'])
        self.assertEqual(first['status'],'needs_review',first)
        recovered=self.worker.run(prepared['operation_id'],reconcile=True)
        self.assertEqual(recovered['planner']['status'],'action_ready',recovered)
        self.assertEqual(len(self.inferences),1)
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual([name for name,_ in self.client.calls].count('claim_ticket'),1)

    def test_profile_change_after_prepare_does_not_implicitly_register(self):
        prepared=self.prepare();profile=self.board.get_agent_profile(self.human,self.scope['agent_id'])
        self.board.update_agent_profile(self.human,profile['id'],profile['version'],description='New preference')
        result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(self.client.register_count,1);self.assertFalse(self.inferences)

    def test_publish_is_explicit_dated_review_and_lost_reply_is_idempotent(self):
        self.successful_actions();prepared=self.prepare();result=self.worker.run(prepared['operation_id'])
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(self.read_ticket()['status'],'in_progress')
        self.client.lost.add('update_ticket')
        published=self.worker.publish(prepared['operation_id'])
        self.assertEqual(published['status'],'complete',published)
        ticket=self.read_ticket()
        self.assertEqual(ticket['status'],'review');self.assertIsNone(ticket['claimant'])
        self.assertIn('Dated audit only',ticket['evidence']);self.assertNotIn('value =',ticket['evidence'])
        self.assertEqual(self.worker.publish(prepared['operation_id']),published)
        self.assertEqual([name for name,_ in self.client.calls].count('update_ticket'),1)
