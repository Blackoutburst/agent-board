"""Real temporary Board families, simulated model and named-test transports."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from test_board_worker import Board
from test_development_worker import FixtureClient,FixtureBackend
from test_runtime_integration import ControlClient
from test_dev_planner import FakePolicy
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from blackoutburst_agents.dev_manifest import DevelopmentError,NamedTest,digest,prepare_manifest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.family_journal import FamilyJournal,SLOTS
from blackoutburst_agents.family_worker import DevelopmentFamilyWorker
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal


class Crash(BaseException):pass


@unittest.skipIf(Board is None,'Board sources not bundled')
class FamilyWorkerTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup);self.base=Path(temp.name).resolve();self.now=time.time()
        self.board=Board(self.base/'board');self.board.ensure_owner('owner','Owner');self.human={'kind':'human','owner_id':'owner'}
        self.board.set_policy(self.human,True,True,True,True,
            ['repo.read_file','repo.patch_file','repo.run_test'],['repo:fixture'])
        self.project=self.board.create_project(self.human,'Fixture',['owner'],idempotency_key='project')
        ticket=self.board.create_ticket(self.human,self.project['id'],'Root',resource='repo:fixture',idempotency_key='ticket')
        device=self.board.create_device(self.human,'Fixture',[]);self.raw=self.board.authenticate(device['token'])
        self.workflows={};self.registrations={};self.roots={};self.actions={};self.inferences=[];self.after_model=None
        self.backend={};self.policies={};self.items={}
        for slot in SLOTS:
            client=FixtureClient(self.board,self.raw,'family-'+slot);registration=client.register()
            self.registrations[slot]=registration
            root=self.base/slot;root.mkdir(mode=0o700)
            for name in ('left.py','right.py'):(root/name).write_text('value = 1\n')
            self.roots[slot]=root
            planner=self.base/(slot+'-empty');planner.mkdir(mode=0o700)
            self.policies[slot]=FakePolicy(planner);self.policies[slot].close=lambda:None
            backend=FixtureBackend();self.backend[slot]=backend
            store=RunStore(self.base/(slot+'-runs'));self.addCleanup(store.close)
            runtime=RuntimeJournal(self.base/(slot+'-runtime'));self.addCleanup(runtime.close)
            journal=WorkJournal(self.base/(slot+'-work'));self.addCleanup(journal.close)
            def backend_factory(directory,*,manifest,authorize_scope,chosen=slot):
                result=self.backend[chosen];result.authorize_scope=authorize_scope;return result
            worker=DevelopmentBoardWorker(journal,store,runtime,client,
                ControlClient(self.board,self.raw,client.session_id),backend_factory=backend_factory,
                policy_factory=lambda _,chosen=slot:self.policies[chosen],
                _transport=lambda *args,chosen=slot,**kwargs:self.transport(chosen,*args,**kwargs),
                _test_anchor=self.base/'anchor',_allow_fixture_backend=True,clock=lambda:self.now)
            self.workflows[slot]=worker
            item={key:str(self.base/(slot+'-'+kind)) for key,kind in (
                ('broker_directory','broker'),('backend_directory','backend'),('planner_policy_directory','policy'))}
            for path in item.values():Path(path).mkdir(mode=0o700)
            item.update(model='fixture-model',instruction='Change the selected fixture; run unit then final.',
                budgets=PlannerBudgets(max_turns=5,max_actions=5,max_wall_seconds=10).document())
            self.items[slot]=item
        parent=self.workflows['parent'];registration=self.registrations['parent']
        self.scope=dict(owner_id='owner',agent_id=registration['identity']['id'],project_id=self.project['id'],
            ticket_id=ticket['id'],board_endpoint=parent.client.endpoint,board_session_id=parent.client.session_id)
        self.board.claim_ticket(parent.client.actor,ticket['id'],ticket['version'])
        self.make_manifest('parent',self.scope,['left.py','right.py'])
        children=[]
        for index,slot in enumerate(SLOTS[:2]):
            children.append({**self.items[slot],'session_id':self.workflows[slot].client.session_id,
                'role':slot,'title':'Change '+slot,'description':'Fixture only','acceptance_criteria':'Unit passes',
                'files':[['left.py'],['right.py']][index],'root':str(self.roots[slot])})
        self.request={'scope':self.scope,'parent':self.items['parent'],'children':children,
            'limits':{'max_model_turns':16,'max_tool_actions':16,'max_concurrent':1,'deadline':self.now+90}}
        self.journal=FamilyJournal(self.base/'family',_test_anchor=self.base/'anchor',clock=lambda:self.now)
        self.addCleanup(self.journal.close)
        self.worker=DevelopmentFamilyWorker(self.journal,self.workflows,registrations=self.registrations,clock=lambda:self.now)

    def make_manifest(self,slot,scope,paths):
        registration=self.registrations[slot]
        manifest_scope={key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        manifest_scope.update(device_id=registration['identity']['device_id'],session_id=scope['board_session_id'],
            profile_version=registration['profile_version'])
        prepare_manifest(self.items[slot]['broker_directory'],root=self.roots[slot],base_sha='a'*40,
            scope=manifest_scope,paths=paths,resource='repo:fixture',authorize_scope=lambda *_:True,
            tests=[NamedTest('unit',backend_id=self.backend[slot].backend_id)])

    def ready(self):
        self.worker.prepare(self.request);self.worker.open();state=self.journal.read()
        for slot in SLOTS[:2]:
            self.make_manifest(slot,state['payload']['slot_scopes'][slot],self.request['children'][SLOTS.index(slot)]['files'])
        self.worker.bind()
        for slot,path in zip(SLOTS[:2],('left.py','right.py')):
            self.actions[slot]=[{'schema':1,'kind':'patch','path':path,'expected_sha256':digest(b'value = 1\n'),'content':'value = 2\n'},
                                {'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'verified'}]
        self.actions['parent']=[{'schema':1,'kind':'patch','path':path,'expected_sha256':digest(b'value = 1\n'),'content':'value = 2\n'} for path in ('left.py','right.py')]+[
            {'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'verified'}]

    def transport(self,slot,settings,prompt,workspace,**callbacks):
        self.inferences.append((slot,json.loads(prompt)));callbacks['on_started'](999999999)
        sid=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](sid)
        if self.after_model:self.after_model(slot,callbacks)
        return {'status':'completed','reason':'verified','session_id':sid,'text':json.dumps(self.actions[slot].pop(0)),
            'verification':dict(tool_free=True,cleanup_confirmed=True,exit_zero=True,turn_completed=True,
                                session_verified=True,authentication='chatgpt')}

    def ticket(self):return self.board.get_ticket(self.human,self.scope['ticket_id'])['ticket']

    def test_prepare_reserves_three_roots_but_opens_no_remote_family_or_runtime(self):
        result=self.worker.prepare(self.request)
        self.assertEqual(result['status'],'prepared');self.assertIsNone(result['family_id'])
        self.assertFalse(self.inferences)
        self.assertFalse(any(w.control.calls for w in self.workflows.values()))
        self.assertFalse(any(name=='open_delegation_family' for name,_ in self.workflows['parent'].client.calls))
        self.assertEqual(self.worker.prepare(self.request),result)

    def test_full_sequential_family_audits_accepts_and_integrates_without_publishing_parent(self):
        self.ready();result=self.worker.run()
        self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(result['children_accepted'],2)
        self.assertEqual([slot for slot,_ in self.inferences],['child_0']*3+['child_1']*3+['parent']*4)
        self.assertEqual(self.ticket()['status'],'in_progress');self.assertIsNotNone(self.ticket()['claimant'])
        self.assertEqual((self.roots['parent']/'left.py').read_text(),'value = 2\n')
        self.assertEqual((self.roots['parent']/'right.py').read_text(),'value = 2\n')
        prior=len(self.inferences);self.assertEqual(self.worker.run(),result);self.assertEqual(len(self.inferences),prior)

    def test_reconcile_prepared_is_read_only_and_does_not_open(self):
        self.worker.prepare(self.request);before=len(self.workflows['parent'].client.calls)
        self.assertEqual(self.worker.reconcile()['status'],'prepared')
        self.assertEqual(len(self.workflows['parent'].client.calls),before);self.assertFalse(self.inferences)

    def test_lost_open_response_uses_same_exact_lookup_key(self):
        self.worker.prepare(self.request);self.workflows['parent'].client.lost.add('open_delegation_family')
        result=self.worker.open();self.assertEqual(result['status'],'opened')
        self.worker.open();calls=self.workflows['parent'].client.calls
        self.assertEqual(sum(name=='open_delegation_family' for name,_ in calls),1)
        keys=[args['idempotency_key'] for name,args in calls if name=='lookup_delegation_family']
        self.assertEqual(len(set(keys)),1)

    def test_unbound_run_refuses_before_runtime_and_model(self):
        self.worker.prepare(self.request);self.worker.open();result=self.worker.run()
        self.assertEqual(result['reason'],'family_bindings_required');self.assertFalse(self.inferences)
        self.assertFalse(any(w.control.calls for w in self.workflows.values()))

    def test_parent_missing_candidate_changes_refuses_finalize(self):
        self.ready();self.actions['parent']=self.actions['parent'][1:]
        result=self.worker.run()
        self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(result['reason'],'family_source_snapshot_changed',result)
        self.assertFalse(any(name=='finalize_delegation_family' for name,_ in self.workflows['parent'].client.calls))

    def test_stop_during_child_model_cancels_before_patch_and_no_parent_prepare(self):
        self.ready()
        def stop(slot,callbacks):
            self.worker.stop();self.assertTrue(callbacks['should_cancel']())
        self.after_model=stop;result=self.worker.run()
        self.assertEqual(result['status'],'needs_review');self.assertEqual(len(self.inferences),1)
        self.assertEqual((self.roots['child_0']/'left.py').read_text(),'value = 1\n')
        self.assertNotIn('parent',self.journal.read()['payload']['workflow_intents'])


# Focused ordinary regressions use the same real controller chain; no model API.
def _test_lost_accept_and_finalize_responses_are_confirmed_without_duplicates(self):
    self.ready();self.workflows['parent'].client.lost.update({'accept_delegation_result','finalize_delegation_family'})
    result=self.worker.run();self.assertEqual(result['status'],'review_ready',result)
    names=[name for name,_ in self.workflows['parent'].client.calls]
    self.assertEqual(names.count('accept_delegation_result'),2);self.assertEqual(names.count('finalize_delegation_family'),1)
FamilyWorkerTests.test_lost_accept_and_finalize_responses_are_confirmed_without_duplicates=_test_lost_accept_and_finalize_responses_are_confirmed_without_duplicates


def _test_crash_before_accept_is_receipt_only_reconciled_then_same_intent_runs(self):
    self.ready();original=self.workflows['parent'].client.call;crashed=[False]
    def crash(name,args):
        if name=='accept_delegation_result' and not crashed[0]:crashed[0]=True;raise Crash()
        return original(name,args)
    self.workflows['parent'].client.call=crash
    with self.assertRaises(Crash):self.worker.run()
    before=len(self.inferences);calls=len(self.workflows['parent'].client.calls)
    self.worker.reconcile();self.assertEqual(len(self.inferences),before)
    self.assertFalse(any(name in ('open_delegation_family','accept_delegation_result','finalize_delegation_family','claim_ticket','renew_ticket')
                         for name,_ in self.workflows['parent'].client.calls[calls:]))
    self.workflows['parent'].client.call=original
    result=self.worker.run();self.assertEqual(result['status'],'review_ready',result)
    self.assertEqual(len([slot for slot,_ in self.inferences if slot=='child_0']),3)
FamilyWorkerTests.test_crash_before_accept_is_receipt_only_reconciled_then_same_intent_runs=_test_crash_before_accept_is_receipt_only_reconciled_then_same_intent_runs


def _test_stop_during_uncertain_open_remains_pending_then_explicit_stop_confirms(self):
    self.worker.prepare(self.request);original=self.workflows['parent'].client.call;observed=[]
    def stop_in_flight(name,args):
        result=original(name,args)
        if name=='open_delegation_family':observed.append(self.worker.stop())
        return result
    self.workflows['parent'].client.call=stop_in_flight
    with self.assertRaises(DevelopmentError):self.worker.open()
    self.assertEqual(observed[0]['server_stop'],'pending')
    state=self.journal.read();self.assertIn('family_id',state['payload'])
    result=self.worker.stop();self.assertEqual(result['server_stop'],'confirmed')
    family=original('get_delegation_family',{'family_id':state['payload']['family_id']})
    self.assertEqual(family['state'],'cancelled');self.assertFalse(self.inferences)
FamilyWorkerTests.test_stop_during_uncertain_open_remains_pending_then_explicit_stop_confirms=_test_stop_during_uncertain_open_remains_pending_then_explicit_stop_confirms


def _test_code_pin_change_refuses_before_any_runtime(self):
    from unittest.mock import patch
    self.ready()
    with patch('blackoutburst_agents.family_worker.controller_sources',return_value={'changed':True}):result=self.worker.run()
    self.assertEqual(result['reason'],'family_controller_sources_changed');self.assertFalse(self.inferences)
    self.assertFalse(any(w.control.calls for w in self.workflows.values()))
FamilyWorkerTests.test_code_pin_change_refuses_before_any_runtime=_test_code_pin_change_refuses_before_any_runtime


def _test_ordinary_worker_refuses_family_ticket_before_new_claim_or_runtime(self):
    self.ready();state=self.journal.read();slot='child_0';base=self.workflows[slot];item=self.items[slot]
    with self.assertRaises(DevelopmentError):
        base.prepare(scope=state['payload']['slot_scopes'][slot],request_id='ordinary',instruction='Not family controlled',
            broker_directory=item['broker_directory'],backend_directory=item['backend_directory'],
            planner_policy_directory=item['planner_policy_directory'],model=item['model'])
    self.assertFalse(self.inferences);self.assertFalse(base.control.calls)
FamilyWorkerTests.test_ordinary_worker_refuses_family_ticket_before_new_claim_or_runtime=_test_ordinary_worker_refuses_family_ticket_before_new_claim_or_runtime


def _test_cleanup_after_grant_withdrawal_confirms_only_exact_existing_runtime(self):
    self.ready();control=self.workflows['child_0'].control;original=control.call;lost=[False]
    def fail_finish(name,args):
        if name=='finish_execution' and len(self.inferences)==2 and not lost[0]:lost[0]=True;raise TimeoutError()
        return original(name,args)
    control.call=fail_finish
    result=self.worker.run();self.assertEqual(result['status'],'needs_review',result)
    self.assertEqual(len(self.inferences),2)
    self.board.set_policy(self.human,True,False,False,False,[],[])
    control.call=original;before=len(control.calls)
    cleaned=self.worker.cleanup();self.assertTrue(cleaned['cleanup']['child_0']['cleanup_confirmed'])
    self.assertEqual(len(self.inferences),2)
    self.assertTrue(set(control.calls[before:])<={'lookup_execution','finish_execution'})
FamilyWorkerTests.test_cleanup_after_grant_withdrawal_confirms_only_exact_existing_runtime=_test_cleanup_after_grant_withdrawal_confirms_only_exact_existing_runtime


def _test_crash_after_child_publication_does_not_reprepare_released_claim(self):
    self.ready();original=self.worker._audit;crashed=[False]
    def crash(state,slot,worker,**kwargs):
        if slot=='child_0' and not crashed[0]:crashed[0]=True;raise Crash()
        return original(state,slot,worker,**kwargs)
    self.worker._audit=crash
    with self.assertRaises(Crash):self.worker.run()
    self.worker._audit=original;result=self.worker.run()
    self.assertEqual(result['status'],'review_ready',result)
    self.assertEqual(len([slot for slot,_ in self.inferences if slot=='child_0']),3)
    self.assertEqual(sum(name=='claim_ticket' for name,_ in self.workflows['child_0'].client.calls),1)
FamilyWorkerTests.test_crash_after_child_publication_does_not_reprepare_released_claim=_test_crash_after_child_publication_does_not_reprepare_released_claim


def _test_crash_after_remote_finalize_reconcile_only_confirms_receipt(self):
    self.ready();original=self.workflows['parent'].client.call;crashed=[False]
    def crash(name,args):
        result=original(name,args)
        if name=='finalize_delegation_family' and not crashed[0]:crashed[0]=True;raise Crash()
        return result
    self.workflows['parent'].client.call=crash
    with self.assertRaises(Crash):self.worker.run()
    self.workflows['parent'].client.call=original;before=len(self.inferences);calls=len(self.workflows['parent'].client.calls)
    result=self.worker.reconcile();self.assertEqual(result['status'],'review_ready',result)
    self.assertEqual(len(self.inferences),before)
    self.assertFalse(any(name in ('open_delegation_family','accept_delegation_result','finalize_delegation_family','claim_ticket','renew_ticket')
                         for name,_ in self.workflows['parent'].client.calls[calls:]))
FamilyWorkerTests.test_crash_after_remote_finalize_reconcile_only_confirms_receipt=_test_crash_after_remote_finalize_reconcile_only_confirms_receipt


def _test_deadline_refuses_before_initial_child_runtime(self):
    self.ready();self.now+=100;result=self.worker.run()
    self.assertEqual(result['reason'],'family_deadline');self.assertFalse(self.inferences)
    self.assertFalse(any(w.control.calls for w in self.workflows.values()))
FamilyWorkerTests.test_deadline_refuses_before_initial_child_runtime=_test_deadline_refuses_before_initial_child_runtime


def _test_parent_is_prepared_only_after_two_server_acceptances(self):
    self.ready()
    def observe(slot,callbacks):
        state=self.journal.read();scope=state['payload']['slot_scopes']['parent']
        prior=self.workflows['parent'].journal.find(scope,'family-'+state['workflow_ids']['parent'])
        family=self.workflows['parent'].client.call('get_delegation_family',{'family_id':state['payload']['family_id']})
        if slot!='parent':self.assertIsNone(prior)
        else:self.assertEqual(sum(child['state']=='accepted' for child in family['children']),2)
    self.after_model=observe;result=self.worker.run();self.assertEqual(result['status'],'review_ready',result)
FamilyWorkerTests.test_parent_is_prepared_only_after_two_server_acceptances=_test_parent_is_prepared_only_after_two_server_acceptances


if __name__=='__main__':unittest.main()


def _test_parent_publication_is_separate_explicit_review_and_cached(self):
    self.ready();result=self.worker.run();self.assertEqual(result['status'],'review_ready',result)
    self.assertEqual(self.ticket()['status'],'in_progress');prior=len(self.inferences)
    published=self.worker.publish_parent();self.assertEqual(published['status'],'review_ready',published)
    self.assertEqual(self.ticket()['status'],'review');self.assertIsNone(self.ticket()['claimant'])
    self.assertEqual(self.worker.publish_parent(),published);self.assertEqual(len(self.inferences),prior)
FamilyWorkerTests.test_parent_publication_is_separate_explicit_review_and_cached=_test_parent_publication_is_separate_explicit_review_and_cached
