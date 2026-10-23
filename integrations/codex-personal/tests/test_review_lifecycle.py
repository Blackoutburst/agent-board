"""Visible review lifecycle using real Board/Git/Pool/Runtime primitives.

Native qualification, GitHub and model transport are explicit synthetic fixtures.
No service, live account, deployment, external publication or model is invoked.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch

import test_development_pool_delivery as archived
from test_runtime_integration import ControlClient
from blackoutburst_agents import review_lifecycle as module
from blackoutburst_agents import linux_review_binding
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.development_pool_delivery import DevelopmentPoolResultDelivery
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.review_client import DeliveryReviewBoardClient
from blackoutburst_agents.review_lifecycle_client import DeliveryReviewLifecycleClient
from blackoutburst_agents.review_pool import DeliveryReviewPoolDriver
from blackoutburst_agents.worker_pool import WorkerPool


class Crash(BaseException):pass


class ReviewLifecycleTests(unittest.TestCase):
    def fixture(self,*,publish_author=True,same_pool=False,submit_review=True):
        helper=archived.DevelopmentPoolDeliveryTests();self.addCleanup(helper.doCleanups)
        f=helper.fixture()
        f.native_prepares=f.native_qualifications=0;f.lifecycle_calls=[];f.review_calls=[];f.review_turns=[]
        f.lost_claim=False;f.lost_update=False;f.fail_after_update=False;f.before_update=None;f.opinion_text='Add the missing boundary test.'
        device=f.board.create_device(f.human,'Independent review controller',[])
        profile=f.board.create_agent_profile(f.human,'Independent review',project_ids=[f.project['id']],policy={
            'allow_read':True,'allow_messages':False,'allow_requests':True,'actions':[],'resources':[],'autonomy':'requested'})
        f.board.bind_agent_profile(f.human,profile['id'],profile['version'],device['id'],'reviewer','codex')
        f.review_raw=f.board.authenticate(device['token'])
        f.review_connection=f.base/'review-controller.json'
        with PrivateDirectory(f.base) as private:
            private.create(f.review_connection.name,canonical(dict(mcp_url=f.client.endpoint,token=device['token'],client='codex',identity_key='reviewer',name='Reviewer')))
        f.review_driver=DeliveryReviewPoolDriver(f.base/'review-controller-selections',connection_file=f.review_connection,
            owner_id='owner',agent_id=profile['id'],board_endpoint=f.client.endpoint)
        if same_pool:
            f.pool.close()
            f.pool=WorkerPool(f.base/'pool',drivers={'development':f.driver.binding(),'review':f.review_driver.binding()})
            f.pipeline=DevelopmentPoolResultDelivery(f.pipeline.directory,pool=f.pool,driver=f.driver,workspaces=f.workspaces,
                publication_grant=f.pipeline._grant,gh_executable=f.gh,python_executable=f.python)
            f.review_pool=f.pool
        else:
            f.review_pool=WorkerPool(f.base/'review-controller-pool',concurrency=1,drivers={'review':f.review_driver.binding()})
        self.addCleanup(f.review_pool.close)
        if publish_author:helper.deliver(f)
        if submit_review:
            f.review_job=f.review_pool.submit(request_id='visible-review',driver_id='review',payload={'project_id':f.project['id'],'ticket_id':f.ticket['id']})
        f.lifecycle=module.DeliveryReviewLifecycle(f.base/'review-lifecycle',pool=f.review_pool,driver=f.review_driver,source=f.pipeline,
            executable=f.python,cli_version='codex-cli 0.163.0',interpreter=f.python)
        def client_factory(cls):
            def factory(path,*,session_id,timeout):
                value=cls(decode(file_document(path)[0]),session_id,timeout)
                value.connection_identity=tuple(file_document(path)[1]['identity']);value._initialize=lambda:None
                def tool(name,args):
                    calls=f.lifecycle_calls if cls is DeliveryReviewLifecycleClient else f.review_calls
                    calls.append((name,deepcopy(args)))
                    if name=='register_agent':return f.board.register_agent(f.review_raw,**args)
                    actor=f.board.actor_session(f.review_raw,session_id);values={key:item for key,item in args.items() if key!='session_id'}
                    if name=='update_ticket':
                        if f.before_update:f.before_update(value,values)
                        values={**values,**values.pop('changes')};values.pop('changes',None)
                    result=getattr(f.board,name)(actor,**values)
                    if name=='list_activity':return {'items':result}
                    if name=='claim_ticket' and f.lost_claim:f.lost_claim=False;raise Crash()
                    if name=='update_ticket' and f.lost_update:f.lost_update=False;raise TimeoutError('synthetic lost CAS reply')
                    if name=='update_ticket' and f.fail_after_update:f.fail_after_update=False;raise Crash()
                    return result
                value._tool=tool;return value
            return factory
        def control(client):
            raw=f.raw if client.session_id==f.job['session_id'] else f.review_raw
            value=ControlClient(f.board,raw,client.session_id);value.close=lambda:None;return value
        def prepare(directory,*,workspace,checkout,executable,cli_version,interpreter):
            f.native_prepares+=1;Path(directory).mkdir(mode=0o700)
            plan=dict(workspace=str(workspace),checkout=str(checkout),executable=str(executable),synthetic_qualification=True)
            with PrivateDirectory(directory) as private:
                private.create('planner-plan.json',canonical(plan));private.create('planner-qualification.json',canonical(None))
            return digest(canonical(plan))
        def policy_initialize(policy,directory):
            policy._plan=decode(file_document(Path(directory)/'planner-plan.json')[0]);policy._fingerprint=digest(canonical(policy._plan))
            policy.close=lambda:None
            def qualify():
                f.native_qualifications+=1
                with PrivateDirectory(directory) as private:private.replace('planner-qualification.json',canonical({'synthetic':True}))
            policy.qualify=qualify
            def validate(executable,workspace):
                self.assertEqual(executable,str(f.python));self.assertEqual(list(Path(workspace).iterdir()),[])
                self.assertEqual(decode(file_document(Path(directory)/'planner-qualification.json')[0]),{'synthetic':True})
            policy.validate_for=validate
        def transport(settings,prompt,workspace,**callbacks):
            callbacks['before_prompt']();callbacks['on_started'](999999999)
            sid=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](sid)
            f.review_turns.append(prompt)
            return dict(status='completed',text=json.dumps(dict(verdict='corrections',text=f.opinion_text)),session_id=sid,
                        verification=dict(cleanup_confirmed=True,tool_free=True))
        for item in (patch.object(DeliveryReviewLifecycleClient,'from_connection_file',side_effect=client_factory(DeliveryReviewLifecycleClient)),
                     patch.object(DeliveryReviewBoardClient,'from_connection_file',side_effect=client_factory(DeliveryReviewBoardClient)),
                     patch('blackoutburst_agents.review_pool.RuntimeBoardClient.from_worker',side_effect=control),
                     patch.object(module,'prepare_linux_planner_policy',side_effect=prepare),
                     patch.object(module.LinuxPlannerPolicy,'__init__',new=policy_initialize),
                     patch.object(linux_review_binding,'execute_turn',side_effect=transport)):
            item.start();self.addCleanup(item.stop)
        return f

    @staticmethod
    def prepare(f):
        return f.lifecycle.prepare(f.review_job,source_operation_id=f.operation,instruction='Review the explicit tested change.',
            material='Exact main.py change: value = 1 becomes value = 2.',model='synthetic-review-model',timeout=10,skills=['review','python'])

    def run_review(self,f):
        ids=f.review_pool.dispatch_pending()
        for operation_id in ids:f.review_pool.threads[operation_id].join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in f.review_pool.threads.values()))
        job=next(row for row in f.review_pool.status()['jobs'] if row['id']==f.review_job['id'])
        self.assertEqual((job['state'],job['outcome'],job['cleanup_confirmed']),('settled','complete',True))

    def test_controller_claims_prepares_real_review_and_publishes_exact_visible_opinion(self):
        f=self.fixture();author=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        self.assertEqual(self.prepare(f)['status'],'prepared');self.run_review(f)
        result=f.lifecycle.publish(f.review_job['id']);current=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        self.assertEqual(result['status'],'complete');self.assertEqual(current['status'],'review');self.assertIsNone(current['claimant'])
        self.assertEqual(current['version'],author['version']+2)
        self.assertTrue(current['evidence'].startswith(author['evidence']));self.assertTrue(current['outcome'].startswith(author['outcome']))
        self.assertIn(f.opinion_text,current['outcome']);self.assertIn(f.opinion_text,current['evidence'])
        self.assertIn(result['delivery_sha'],current['evidence']);self.assertIn('/pull/17',current['evidence'])
        self.assertEqual((f.native_prepares,f.native_qualifications,len(f.review_turns)),(1,1,1))
        mutations=[args for name,args in f.lifecycle_calls if name=='update_ticket']
        self.assertEqual(len(mutations),1);self.assertEqual(mutations[0]['changes']['status'],'review')
        self.assertIs(mutations[0]['changes']['release_claim'],True)
        self.assertEqual(f.lifecycle.publish(f.review_job['id']),result)
        self.assertEqual(f.lifecycle.status(f.review_job['id'])['status'],'complete')

    def test_lost_claim_is_retained_without_registration_claim_qualification_or_model_replay(self):
        f=self.fixture();f.lost_claim=True
        with self.assertRaises(Crash):self.prepare(f)
        before=deepcopy(f.lifecycle_calls)
        self.assertEqual(self.prepare(f)['status'],'retained');self.assertEqual(f.lifecycle_calls,before)
        self.assertEqual((f.native_prepares,f.native_qualifications,len(f.review_turns)),(0,0,0))

    def test_lost_cas_ack_is_confirmed_from_exact_attributed_server_event(self):
        f=self.fixture();self.prepare(f);self.run_review(f);f.lost_update=True
        result=f.lifecycle.publish(f.review_job['id']);self.assertEqual(result['status'],'complete')
        self.assertEqual(sum(name=='update_ticket' for name,_ in f.lifecycle_calls),1)

    def test_crash_after_cas_reopens_publication_without_model_or_claim_replay(self):
        f=self.fixture();self.prepare(f);self.run_review(f);f.fail_after_update=True
        with self.assertRaises(Crash):f.lifecycle.publish(f.review_job['id'])
        before=(len(f.review_turns),sum(name=='register_agent' for name,_ in f.lifecycle_calls),sum(name=='claim_ticket' for name,_ in f.lifecycle_calls))
        result=f.lifecycle.publish(f.review_job['id']);self.assertEqual(result['status'],'complete')
        self.assertEqual((len(f.review_turns),sum(name=='register_agent' for name,_ in f.lifecycle_calls),sum(name=='claim_ticket' for name,_ in f.lifecycle_calls)),before)
        self.assertEqual(sum(name=='update_ticket' for name,_ in f.lifecycle_calls),1)

    def test_model_result_tamper_cannot_publish_even_if_work_stage_stays_complete(self):
        f=self.fixture();self.prepare(f);self.run_review(f)
        job=f.review_pool._job(f.review_pool.journal.read(),f.review_job['id']);context=f.review_pool._context(job)
        with ExitStack() as resources:
            _,worker=f.review_driver.open_completed_source(resources,context)
            op=worker.journal.get(f.review_job['id']);payload=op['payload'];payload['opinion']['text']='Invented approval'
            worker.journal._db.execute('UPDATE operations SET payload_json=? WHERE id=?',(canonical(payload).decode(),op['id']))
        with self.assertRaises(DevelopmentError):f.lifecycle.publish(f.review_job['id'])
        self.assertFalse(any(name=='update_ticket' for name,_ in f.lifecycle_calls))

    def test_stop_before_publication_prevents_remote_update(self):
        f=self.fixture();self.prepare(f);self.run_review(f);f.lifecycle.stop(f.review_job['id'])
        with self.assertRaises(DevelopmentError):f.lifecycle.publish(f.review_job['id'])
        self.assertFalse(any(name=='update_ticket' for name,_ in f.lifecycle_calls))

    def test_stop_during_policy_preparation_never_launches_qualification(self):
        f=self.fixture();original=module.prepare_linux_planner_policy
        def stopped(*args,**kwargs):
            result=original(*args,**kwargs);f.lifecycle.stop(f.review_job['id']);return result
        with patch.object(module,'prepare_linux_planner_policy',side_effect=stopped):
            with self.assertRaises(DevelopmentError):self.prepare(f)
        self.assertEqual((f.native_prepares,f.native_qualifications,len(f.review_turns)),(1,0,0))

    def test_changed_context_after_model_never_publishes_stale_opinion(self):
        f=self.fixture();self.prepare(f);self.run_review(f)
        f.board.post_message(f.human,f.ticket['thread_id'],'Changed acceptance after review',mentions=[],idempotency_key='new-context')
        with self.assertRaises(DevelopmentError):f.lifecycle.publish(f.review_job['id'])
        self.assertFalse(any(name=='update_ticket' for name,_ in f.lifecycle_calls))

    def test_human_lookalike_poststate_is_never_adopted_as_this_reviewer_publication(self):
        f=self.fixture();self.prepare(f);self.run_review(f)
        def human_update(_client,args):
            f.before_update=None
            f.board.update_ticket(f.human,args['ticket_id'],args['expected_version'],**args['changes'])
        f.before_update=human_update
        with self.assertRaisesRegex(DevelopmentError,'review_lifecycle_publication_event_unconfirmed'):
            f.lifecycle.publish(f.review_job['id'])
        self.assertFalse((f.lifecycle.directory/f.review_job['id']/'published.json').exists())

    def test_lost_pre_effect_response_reuses_exact_same_cas_and_publication_key(self):
        f=self.fixture();self.prepare(f);self.run_review(f)
        def lost(_client,_args):
            f.before_update=None
            raise TimeoutError('synthetic pre-effect loss')
        f.before_update=lost
        with self.assertRaises(DevelopmentError):f.lifecycle.publish(f.review_job['id'])
        intent=decode((f.lifecycle.directory/f.review_job['id']/'publication-intent.json').read_bytes())
        result=f.lifecycle.publish(f.review_job['id'])
        updates=[args for name,args in f.lifecycle_calls if name=='update_ticket']
        self.assertEqual(len(updates),2);self.assertEqual(updates[0],updates[1])
        self.assertEqual(result['publication_key'],intent['key']);self.assertEqual(len(f.review_turns),1)

    def test_retained_cas_cannot_be_edited_to_publish_different_opinion_text(self):
        f=self.fixture();self.prepare(f);self.run_review(f)
        def lost(_client,_args):
            f.before_update=None
            raise TimeoutError('synthetic pre-effect loss')
        f.before_update=lost
        with self.assertRaises(DevelopmentError):f.lifecycle.publish(f.review_job['id'])
        with PrivateDirectory(f.lifecycle.directory/f.review_job['id']) as private:
            request=decode(private.read('publication-intent.json',512*1024));request['rpc']['changes']['outcome']='Forged approval'
            private.replace('publication-intent.json',canonical(request))
        with self.assertRaisesRegex(DevelopmentError,'review_lifecycle_intent_changed'):f.lifecycle.publish(f.review_job['id'])
        self.assertEqual(sum(name=='update_ticket' for name,_ in f.lifecycle_calls),1)

    def test_same_pool_can_keep_unpublished_author_source_until_explicit_workflow_delivery(self):
        f=self.fixture(publish_author=False,same_pool=True,submit_review=False)
        self.assertIs(f.review_pool,f.pool)
        self.assertFalse(any(name=='register_agent' for name,_ in f.lifecycle_calls))
        archived.DevelopmentPoolDeliveryTests.deliver(f)
        f.review_job=f.pool.submit(request_id='review-after-author',driver_id='review',payload={'project_id':f.project['id'],'ticket_id':f.ticket['id']})
        self.prepare(f);self.run_review(f)
        result=f.lifecycle.publish(f.review_job['id'])
        self.assertEqual(result['status'],'complete');self.assertEqual(len(f.review_turns),1)

    def test_historical_status_has_no_client_or_journal_constructor_and_checks_real_files(self):
        f=self.fixture();self.prepare(f);self.run_review(f);result=f.lifecycle.publish(f.review_job['id'])
        before=(len(f.lifecycle_calls),len(f.review_calls),len(f.review_turns))
        with (patch.object(module.DeliveryReviewLifecycleClient,'from_connection_file',side_effect=AssertionError('no client')),
              patch('blackoutburst_agents.work_journal.WorkJournal.__init__',side_effect=AssertionError('no migration'))):
            self.assertEqual(f.lifecycle.status(f.review_job['id']),result)
        self.assertEqual((len(f.lifecycle_calls),len(f.review_calls),len(f.review_turns)),before)
        job=f.review_pool._job(f.review_pool.journal.read(),f.review_job['id'])
        context=f.review_pool._context(job);removed=Path(context.journals['runtime'])/'runtime.sqlite3';removed.unlink()
        with self.assertRaises(DevelopmentError):f.lifecycle.status(f.review_job['id'])
        self.assertFalse(removed.exists())


class ReviewLifecycleClientTests(unittest.TestCase):
    def test_controller_only_adds_explicit_claim_and_exact_review_publication(self):
        client=DeliveryReviewLifecycleClient(dict(mcp_url='https://board.test/mcp',token='SYNTHETIC_TOKEN',client='codex',
            identity_key='reviewer',name='Reviewer'),'review-session',3)
        client._initialize=lambda:None;calls=[]
        client._tool=lambda name,args:calls.append((name,args)) or {'ok':True}
        client.pin_registration({'session':{'id':client.session_id}})
        client.set_effect_guard(lambda:None)
        for name in ('claim_next_ticket','release_ticket','post_message','create_ticket','renew_ticket'):
            with self.assertRaises(BoardReadError):client.call(name,{})
        for changes in ({'status':'done'},{'status':'review','release_claim':False,'outcome':'x','evidence':'y'},
                        {'status':'review','release_claim':True,'outcome':'x','evidence':'y','assignee_agent_id':'other'}):
            with self.assertRaises(BoardReadError):client.call('update_ticket',dict(ticket_id='ticket',expected_version=1,changes=changes))
        self.assertEqual(calls,[])
        client.call('claim_ticket',dict(ticket_id='ticket',expected_version=1))
        client.call('update_ticket',dict(ticket_id='ticket',expected_version=2,
            changes=dict(status='review',release_claim=True,outcome='Exact selected opinion',evidence='Exact selected proof')))
        self.assertEqual([name for name,_ in calls],['claim_ticket','update_ticket'])

    def test_stop_after_initialize_refuses_the_final_mutation_tool(self):
        client=DeliveryReviewLifecycleClient(dict(mcp_url='https://board.test/mcp',token='SYNTHETIC_TOKEN',client='codex',
            identity_key='reviewer',name='Reviewer'),'review-session',3)
        state={'stopped':False};calls=[]
        client._initialize=lambda:state.update(stopped=True)
        client._tool=lambda name,args:calls.append((name,args)) or {'ok':True}
        client.pin_registration({'session':{'id':client.session_id}})
        def guard():
            if state['stopped']:raise BoardReadError('review_controller_stopped')
        client.set_effect_guard(guard)
        with self.assertRaises(BoardReadError):client.call('update_ticket',dict(ticket_id='ticket',expected_version=2,
            changes=dict(status='review',release_claim=True,outcome='Exact selected opinion',evidence='Exact selected proof')))
        self.assertEqual(calls,[])
