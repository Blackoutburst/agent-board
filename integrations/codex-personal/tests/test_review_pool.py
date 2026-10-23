"""Real Pool/Git/review/Board/Runtime primitives; synthetic model and GitHub.

The archived author fixture uses synthetic native receipts. No live account,
server service, external network, native qualification or real model runs here.
"""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import threading
import unittest
import uuid
from unittest.mock import patch

import test_development_pool_delivery as archived
from test_runtime_integration import ControlClient
from blackoutburst_agents import review_pool as adapter
from blackoutburst_agents import linux_review_binding as linux_binding
from blackoutburst_agents.context import ContextScope,build_context
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.git_delivery import LocalGitDeliveryController
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.review_client import DeliveryReviewBoardClient
from blackoutburst_agents.review_worker import DeliveryReviewWorker
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.worker_pool import WorkerPool


class ReviewPoolTests(unittest.TestCase):
    def fixture(self, *, claim=True):
        helper=archived.DevelopmentPoolDeliveryTests();self.addCleanup(helper.doCleanups)
        f=helper.fixture();helper.deliver(f)
        f.review_clients=[];f.review_controls=[];f.review_turns=[];f.review_during=None;f.review_cleanup=True
        device=f.board.create_device(f.human,'Independent reviewer',[])
        profile=f.board.create_agent_profile(f.human,'Review',project_ids=[f.project['id']],policy={
            'allow_read':True,'allow_messages':False,'allow_requests':True,'actions':[], 'resources':[], 'autonomy':'requested'})
        f.board.bind_agent_profile(f.human,profile['id'],profile['version'],device['id'],'reviewer','codex')
        f.review_raw=f.board.authenticate(device['token'])
        connection=f.base/'review-connection.json'
        with PrivateDirectory(f.base) as private:
            private.create(connection.name,canonical(dict(mcp_url=f.client.endpoint,token=device['token'],client='codex',identity_key='reviewer',name='Reviewer')))
        f.review_driver=adapter.DeliveryReviewPoolDriver(f.base/'review-selections',connection_file=connection,
            owner_id='owner',agent_id=profile['id'],board_endpoint=f.client.endpoint)
        f.review_pool=WorkerPool(f.base/'review-pool',concurrency=1,drivers={'review':f.review_driver.binding()})
        self.addCleanup(f.review_pool.close)
        f.review_job=f.review_pool.submit(request_id='explicit-review',driver_id='review',payload={'project_id':f.project['id'],'ticket_id':f.ticket['id']})
        session=f.review_job['session_id']
        f.review_registration=f.board.register_agent(f.review_raw,session,'Reviewer',['review','python'],'codex',False,'reviewer')
        f.review_actor=f.board.actor_session(f.review_raw,session)
        if claim:
            task=f.board.get_ticket(f.human,f.ticket['id'])['ticket'];f.board.claim_ticket(f.review_actor,task['id'],task['version'])
        scope=dict(owner_id='owner',agent_id=profile['id'],project_id=f.project['id'],ticket_id=f.ticket['id'],
                   board_endpoint=f.client.endpoint,board_session_id=session)
        def read(name,args):return getattr(f.board,name)(f.review_actor,**args)
        context=build_context(read,scope=ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}),registration=f.review_registration)
        for name,text in (('review-instruction.txt','Inspect the exact tested delivery.'),('review-material.txt','main.py changed from value = 1 to value = 2.')):
            with PrivateDirectory(f.base) as private:private.create(name,text.encode())
        deliveries=LocalGitDeliveryController(f.workspaces,f.pipeline.paths['deliveries'],f.pipeline.paths['reviews'],
            test_guard=adapter.HistoricalPoolDeliveryTests(f.pipeline,f.operation,f.pipeline.status(f.operation)))
        export=deliveries.prepare_review(operation_id=f.operation,review_operation_id=f.review_job['id'],reviewer_scope=scope,
            context_sha256=context['fingerprint'],material=(f.base/'review-material.txt').read_text())
        f.review_policy=f.base/'review-policy';f.review_policy.mkdir(mode=0o700)
        f.review_workspace=f.base/'review-empty';f.review_workspace.mkdir(mode=0o700)
        f.review_policy_valid=True
        plan=dict(workspace=str(f.review_workspace),checkout=export['workspace'],executable=str(f.python))
        with PrivateDirectory(f.review_policy) as private:
            private.create('planner-plan.json',canonical(plan))
            private.create('planner-qualification.json',canonical(dict(synthetic_qualification=True)))
        def policy_initialize(policy,directory):
            self.assertEqual(str(directory),str(f.review_policy))
            policy._plan=decode(file_document(f.review_policy/'planner-plan.json')[0])
            policy._fingerprint=digest(canonical(policy._plan))
            def validate(executable,workspace):
                if not f.review_policy_valid:raise DevelopmentError('synthetic_review_policy_unqualified')
                self.assertEqual(list(Path(workspace).iterdir()),[])
                self.assertEqual(executable,str(f.python))
            policy.validate_for=validate;policy.close=lambda:None
        f.review_selection=dict(schema=1,operation_id=f.review_job['id'],worker_id=f.review_job['worker_id'],session_id=session,
            scope=scope,registration=f.review_registration,source_operation_id=f.operation,context_sha256=context['fingerprint'],
            instruction_file=str(f.base/'review-instruction.txt'),material_file=str(f.base/'review-material.txt'),
            planner_policy_directory=str(f.review_policy),executable=str(f.python),model='synthetic-review-model',timeout=10)
        f.review_selected=f.base/'review-selection.json'
        with PrivateDirectory(f.base) as private:private.create(f.review_selected.name,canonical(f.review_selection))
        f.review_driver.bind_selection(f.review_job,f.review_selected,delivery_source=f.pipeline)
        def client(path,**options):
            value=DeliveryReviewBoardClient(decode(file_document(path)[0]),options['session_id'],options['timeout'])
            value.connection_identity=tuple(file_document(path)[1]['identity'])
            value._initialize=lambda:None;value.calls=[]
            def tool(name,args):
                self.assertNotEqual(name,'register_agent','dispatch must not register')
                value.calls.append((name,args));actor=f.board.actor_session(f.review_raw,args['session_id'])
                return getattr(f.board,name)(actor,**{key:item for key,item in args.items() if key!='session_id'})
            value._tool=tool;f.review_clients.append((threading.get_ident(),value));return value
        def control(client):
            value=ControlClient(f.board,f.review_raw,client.session_id);value.close=lambda:None
            f.review_controls.append((threading.get_ident(),value));return value
        def transport(settings,prompt,workspace,**callbacks):
            callbacks['before_prompt']();callbacks['on_started'](999999999)
            session=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](session)
            f.review_turns.append((threading.get_ident(),settings.model,prompt))
            if f.review_during:f.review_during(callbacks)
            return dict(status='cancelled' if callbacks['should_cancel']() else 'completed',reason='cancelled',
                text=json.dumps(dict(verdict='corrections',text='Add a boundary test.')),session_id=session,
                verification=dict(cleanup_confirmed=f.review_cleanup,tool_free=True))
        def worker(*args,**options):
            options['runner_factory']=lambda store,**kwargs:PersonalRunner(store,transport,**kwargs)
            return DeliveryReviewWorker(*args,**options)
        for item in (patch.object(DeliveryReviewBoardClient,'from_connection_file',side_effect=client),
                     patch.object(adapter.RuntimeBoardClient,'from_worker',side_effect=control),
                     patch.object(adapter,'DeliveryReviewWorker',side_effect=worker),
                     patch.object(linux_binding.LinuxPlannerPolicy,'__init__',new=policy_initialize)):
            item.start();self.addCleanup(item.stop)
        return f

    def run_review(self,f):
        started=f.review_pool.dispatch_pending()
        for operation in started:f.review_pool.threads[operation].join(timeout=20)
        self.assertTrue(all(not thread.is_alive() for thread in f.review_pool.threads.values()))
        return next(job for job in f.review_pool.status()['jobs'] if job['id']==f.review_job['id'])

    def test_real_independent_review_uses_own_thread_runtime_and_keeps_opinion_private(self):
        f=self.fixture();before=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        old=(f.pushes,f.posts,len(f.inferences),len(f.backend.calls))
        result=self.run_review(f)
        self.assertEqual((result['state'],result['outcome'],result['cleanup_confirmed']),('settled','complete',True))
        self.assertEqual(f.board.get_ticket(f.human,f.ticket['id'])['ticket'],before)
        self.assertEqual((f.pushes,f.posts,len(f.inferences),len(f.backend.calls)),old)
        self.assertEqual(len(f.review_turns),1);self.assertEqual(f.review_turns[0][1],'synthetic-review-model')
        thread=f.review_turns[0][0];self.assertNotEqual(thread,threading.get_ident())
        self.assertTrue(all(owner==thread for owner,_ in f.review_clients+f.review_controls))
        self.assertTrue(all(name in ('get_ticket','get_thread','get_message') for _,client in f.review_clients for name,_ in client.calls))
        runtime=[item for item in f.board.get_runtime(f.human)['executions'] if item['agent_id']==f.review_selection['scope']['agent_id']]
        self.assertEqual(len(runtime),1);self.assertEqual(runtime[0]['state'],'finished');self.assertTrue(runtime[0]['cleanup_confirmed'])
        self.assertEqual(f.review_pool.dispatch_pending(),[]);self.assertEqual(len(f.review_turns),1)
        context=f.review_pool._context({**f.review_job,'payload':dict(project_id=f.project['id'],ticket_id=f.ticket['id'])})
        with ExitStack() as resources:
            old_calls=[list(control.calls) for _,control in f.review_controls]
            deliveries,review=f.review_driver.open_completed_source(resources,context)
            operation=review.journal.get(f.review_job['id'])
            self.assertEqual(operation['payload']['opinion']['text'],'Add a boundary test.')
            self.assertEqual([list(control.calls) for _,control in f.review_controls[:-1]],old_calls)
            self.assertEqual(f.review_controls[-1][1].calls,[])
            self.assertEqual(review.journal.get(f.review_job['id'])['configuration']['workspace'],str(f.review_workspace))

    def test_absent_claim_never_claims_registers_or_runs(self):
        f=self.fixture(claim=False);result=self.run_review(f)
        self.assertEqual(result['outcome'],'needs_review');self.assertFalse(f.review_turns)
        self.assertFalse(any(client.calls for _,client in f.review_controls))

    def test_pool_stop_during_review_cancels_and_cleans_real_runtime(self):
        f=self.fixture();f.review_during=lambda callbacks:f.review_pool.stop(f.review_job['id'])
        result=self.run_review(f)
        self.assertEqual((result['state'],result['outcome'],result['cleanup_confirmed']),('settled','needs_review',True))
        self.assertEqual(len(f.review_turns),1)
        runtime=[item for item in f.board.get_runtime(f.human)['executions'] if item['agent_id']==f.review_selection['scope']['agent_id']]
        self.assertEqual(runtime[0]['state'],'finished');self.assertEqual(runtime[0]['outcome'],'cancelled')

    def test_unknown_cleanup_retains_pool_capacity(self):
        f=self.fixture();f.review_cleanup=False;result=self.run_review(f)
        self.assertEqual(result['state'],'retained');self.assertIsNot(result['cleanup_confirmed'],True)

    def test_source_receipt_removed_never_recreated_or_dispatched(self):
        f=self.fixture();removed=f.pipeline.paths['deliveries']/f.operation/'delivery.json';removed.unlink()
        result=self.run_review(f);self.assertEqual(result['state'],'retained');self.assertFalse(f.review_turns)
        self.assertFalse(removed.exists());self.assertFalse(f.review_clients)

    def test_reopen_requires_explicit_concrete_source_attachment(self):
        f=self.fixture();driver=adapter.DeliveryReviewPoolDriver(f.review_driver.directory,connection_file=f.review_driver.connection_file,
            **f.review_driver.fixed)
        context=f.review_pool._context({**f.review_job,'payload':dict(project_id=f.project['id'],ticket_id=f.ticket['id'])},create=True)
        with self.assertRaisesRegex(DevelopmentError,'review_pool_source_attachment_required'):driver._load(context)
        with self.assertRaises(DevelopmentError):driver.attach_source(f.review_job['id'],{'status':'complete'})
        driver.attach_source(f.review_job['id'],f.pipeline)
        bound,_=driver._load(context);self.assertEqual(bound['selection'],f.review_selection)
        self.assertFalse(f.review_clients);self.assertFalse(f.review_turns)

    def test_approved_material_replacement_refuses_before_any_client(self):
        f=self.fixture()
        with PrivateDirectory(f.base) as private:private.replace('review-material.txt',b'Different material')
        self.assertEqual(self.run_review(f)['state'],'retained');self.assertFalse(f.review_clients)

    def test_unqualified_linux_policy_never_reserves_or_runs(self):
        f=self.fixture();f.review_policy_valid=False
        result=self.run_review(f)
        self.assertEqual(result['state'],'retained');self.assertFalse(f.review_turns)
        self.assertTrue(all(not client.calls for _,client in f.review_controls))

    def test_review_export_change_during_turn_prevents_private_opinion(self):
        f=self.fixture()
        def changed(callbacks):
            folder=f.pipeline.paths['reviews']/f.review_job['id']/'export';folder.chmod(0o700)
            path=folder/'main.py';path.chmod(0o600);path.write_text('value = 999\n')
            path.chmod(0o400);folder.chmod(0o500)
            self.assertTrue(callbacks['should_cancel']())
        f.review_during=changed
        result=self.run_review(f);self.assertNotEqual(result['outcome'],'complete')
        self.assertEqual(len(f.review_turns),1)
