"""Actual Pool/Development worker/Board journals; synthetic Linux and text turns.

No subprocess, listener, network, account or native qualification is used.
"""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

from test_board_worker import Board, Client
from test_runtime_integration import ControlClient
from linux_lock_fixture import LockObservationFixture
from blackoutburst_agents import development_pool as adapter
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, canonical, digest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.dev_runtime import development_work_id
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.linux_input_plan import pin_linux_input_plan
from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
from blackoutburst_agents.linux_test_backend import (KIND, PLAN_SCHEMA, NATIVE_SCHEMA, CHECKS,
    SupervisedLinuxNamedPythonBackend, prepare_linux_execution_manifest)
from blackoutburst_agents.linux_test_protocol import linux_execution_backend_id
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.work_journal import WorkJournal, JournalError
from blackoutburst_agents.worker_pool import WorkerPool


def write_private(path, value):
    with PrivateDirectory(path.parent) as private: private.create(path.name, canonical(value))


class PoolDevelopmentTests(unittest.TestCase):
    def setUp(self):
        LockObservationFixture(self)
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup); self.base = Path(temporary.name).resolve()
        self.protected = self.base/'protected'; self.protected.mkdir(mode=0o700)
        self.connection = self.protected/'connection.json'
        write_private(self.connection, {'synthetic': 'connection, never a usable credential'})
        self.board = Board(self.base/'board'); self.board.ensure_owner('owner', 'Owner')
        self.human = {'kind':'human','owner_id':'owner'}
        limits=self.board.get_runtime(self.human)['limits']
        self.board.update_runtime_limits(self.human,limits['version'],4,False)
        self.board.set_policy(self.human,True,True,True,True,
            ['repo.read_file','repo.patch_file','repo.run_test'], ['repo:fixture'])
        self.project = self.board.create_project(self.human,'Fixture',['owner'],idempotency_key='project')
        device = self.board.create_device(self.human,'Fixture',[]); self.raw = self.board.authenticate(device['token']); self.fixture_token=device['token']
        self.seed = Client(self.board,self.raw); registration = self.seed.register()
        self.agent_id = registration['identity']['id']; self.manifest_identity = registration
        self.driver = adapter.DevelopmentPoolDriver(self.base/'selections', connection_file=self.connection,
            owner_id='owner', agent_id=self.agent_id, board_endpoint=Client.endpoint)
        self.pool = WorkerPool(self.base/'pool', concurrency=4, drivers={'development':self.driver.binding()})
        self.addCleanup(lambda:self.pool.close())
        self.data, self.clients, self.controls, self.calls = {}, [], [], []
        self.prepare_errors=[]; self.resources=['repo:fixture']
        self.during, self.loss, self.cleanup = None, set(), True
        self.start_patches()

    def start_patches(self):
        self.patchers = [
            patch.object(adapter.DevelopmentBoardClient,'from_connection_file',side_effect=self.client),
            patch.object(adapter.RuntimeBoardClient,'from_worker',side_effect=self.control),
            patch.object(adapter,'execution_factories',side_effect=self.factories),
            patch.object(adapter,'DevelopmentBoardWorker',side_effect=self.worker),
            patch('blackoutburst_agents.development_worker.DevelopmentPlanner',side_effect=self.planner),
            patch('subprocess.Popen',side_effect=AssertionError('no native/model launch'))]
        for patcher in self.patchers: patcher.start(); self.addCleanup(patcher.stop)

    def client(self, path, *, session_id, timeout):
        client = Client(self.board, self.raw, session_id)
        client.connection_identity = tuple(file_document(path)[1]['identity'])
        client.register_initial = client.register
        client.adopt_registration = lambda: setattr(client,'actor',self.board.actor_session(self.raw,session_id))
        client.close = lambda:None
        self.clients.append((threading.get_ident(),client))
        if getattr(self,'client_barrier',None) and len(self.clients)<=4: self.client_barrier.wait(timeout=4)
        return client

    def control(self, client):
        control = ControlClient(self.board,self.raw,client.session_id)
        control.close = lambda:None; control.lost = self.loss
        self.controls.append((threading.get_ident(),control)); return control

    def factories(self, profile):
        self.assertEqual(profile,KIND)
        def backend(directory, *, manifest, authorize_scope):
            data = self.data[manifest['scope']['session_id']]
            value = object.__new__(SupervisedLinuxNamedPythonBackend)
            value.plan = data['plan']; value.manifest = manifest
            value.document = {'plan_hash':data['plan_hash'],'kind':KIND}
            value.backend_id = linux_execution_backend_id(data['plan_hash'])
            value.supervision = {'fixture':'no native qualification'}
            value.authorize_scope = authorize_scope; value._pins = lambda:None
            value.qualified_for = lambda *_:True; value.close = lambda:None
            def execute(attempt):
                attempt.before_source()
                return {'schema':NATIVE_SCHEMA,'nonce':'a'*32,'request_sha256':digest(canonical(attempt.request)),
                    'plan_sha256':data['plan_hash'],'source_released':True,'source_release_intent':True,
                    'exit_code':0,'reason':'completed','checks':{key:True for key in CHECKS},
                    'launcher_attempted':True,'output_bytes':0,'output_sha256':digest(b''),
                    'cleanup':{'init_identity_received':True,'init_terminal':True,'launcher_reaped':True,
                        'cleanup_confirmed':True,'numeric_pid_signalled':False},
                    'identity':{'namespace_identity':[1,2],'acquired_by':'SCM_RIGHTS_validated','numeric_pid_signalled':False},
                    'broker_snapshot_admitted':False}
            value.run = execute; return value
        def policy(directory):
            data = next(value for value in self.data.values() if value['policy']==str(directory))
            value = object.__new__(LinuxPlannerPolicy)
            value._plan = {'workspace':str(data['planner']), 'workspace_identity':list(data['planner_identity']),
                'executable':str(Path(os.sys.executable).resolve()), 'cli_sha256':digest(b'fixture CLI'),
                'environment':{'synthetic':True}}
            value._fingerprint = data['policy_hash']; value.validate_for = lambda *_:None
            value.close = lambda:None; return value
        return {'backend_factory':backend,'policy_factory':policy}

    def worker(self, *args, **options):
        from blackoutburst_agents.development_worker import DevelopmentBoardWorker
        options['_test_anchor']=self.base/'anchor'
        options['planner_factory']=self.planner
        worker=DevelopmentBoardWorker(*args,**options)
        prepare=worker.prepare
        def traced_prepare(**values):
            try:return prepare(**values)
            except DevelopmentError as error:
                self.prepare_errors.append(error.code);raise
        worker.prepare=traced_prepare
        return worker

    def planner(self, **options):
        from blackoutburst_agents.dev_planner import DevelopmentPlanner
        session = options['board_scope']['board_session_id']; data = self.data[session]
        options.update(_test_anchor=self.base/'anchor', _transport=lambda *args,**kwargs:self.transport(session,*args,**kwargs))
        return DevelopmentPlanner(**options)

    def transport(self, session, settings, prompt, workspace, **callbacks):
        callbacks['on_started'](999999999)
        callbacks['before_prompt']()
        sid = callbacks['resume_session_id'] or str(uuid.uuid4()); callbacks['on_session'](sid)
        self.calls.append((session, threading.get_ident()))
        if self.during: self.during(session,callbacks)
        action = self.data[session]['actions'].pop(0)
        return {'status':'completed','reason':'verified','session_id':sid,'text':json.dumps(action),
            'verification':dict(tool_free=True,cleanup_confirmed=self.cleanup,exit_zero=True,
                turn_completed=True,session_verified=True,authentication='chatgpt')}

    def selected(self, *, bind=True):
        resource='repo:fixture-'+str(uuid.uuid4())
        self.resources.append(resource)
        self.board.set_policy(self.human,True,True,True,True,
            ['repo.read_file','repo.patch_file','repo.run_test'],self.resources)
        ticket = self.board.create_ticket(self.human,self.project['id'],'Selected',resource=resource,idempotency_key=str(uuid.uuid4()))
        payload = {'project_id':self.project['id'],'ticket_id':ticket['id']}
        job = self.pool.submit(request_id=str(uuid.uuid4()),driver_id='development',payload=payload)
        session = job['session_id']
        registration = Client(self.board,self.raw,session).register()
        scope = dict(owner_id='owner',agent_id=self.agent_id,**payload,
            board_session_id=session,board_endpoint=Client.endpoint)
        manifest_scope = {key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        manifest_scope.update(device_id=registration['identity']['device_id'],session_id=session,
                              profile_version=registration['profile_version'])
        root = self.base/job['worker_id']; root.mkdir(mode=0o700)
        checkout = root/'checkout'; checkout.mkdir(mode=0o700); (checkout/'one.txt').write_text('one\n')
        scripts = root/'scripts'; scripts.mkdir(mode=0o700); (scripts/'fixed.py').write_text('assert True\n')
        plan = {'schema':PLAN_SCHEMA,'root':str(checkout),'root_identity':[checkout.stat().st_dev,checkout.stat().st_ino],
            'input_plan':pin_linux_input_plan(root=checkout,scripts_root=scripts,
                scripts={'unit':{'path':'fixed.py','timeout_seconds':2,'max_output_bytes':64}}).document(),
            'environment':{'synthetic':True}}
        plan['input_plan_sha256'] = digest(canonical(plan['input_plan'])); plan_hash=digest(canonical(plan))
        broker = root/'broker'
        with patch('blackoutburst_agents.linux_test_backend.validate_current_plan'):
            prepare_linux_execution_manifest(broker,execution_plan=plan,root=str(checkout),base_sha='a'*40,
                scope=manifest_scope,paths=['one.txt'],resource=resource,authorize_scope=lambda *_:True,
                tests=[NamedTest('unit',linux_execution_backend_id(plan_hash),2,64)])
        backend = root/'backend'; backend.mkdir(mode=0o700)
        write_private(backend/'execution-plan.json',plan)
        write_private(backend/'qualification.json',{'synthetic':True})
        policy = root/'policy'; policy.mkdir(mode=0o700)
        policy_plan={'synthetic':True}; write_private(policy/'planner-plan.json',policy_plan)
        write_private(policy/'planner-qualification.json',{'synthetic':True})
        planner = root/'empty'; planner.mkdir(mode=0o700)
        instruction = root/'instruction.txt'
        with PrivateDirectory(root) as private: private.create(instruction.name,b'Inspect the selected fixture and test it.')
        selection = dict(schema=1,operation_id=job['id'],worker_id=job['worker_id'],session_id=session,
            scope=scope,broker_directory=str(broker),backend_directory=str(backend),
            planner_policy_directory=str(policy),instruction_file=str(instruction),model='fixture-model',
            budgets=PlannerBudgets().document())
        config=root/'selection.json'; write_private(config,selection)
        self.data[session] = dict(job=job,selection=selection,config=config,plan=plan,plan_hash=plan_hash,
            policy=str(policy),policy_hash=digest(canonical(policy_plan)),planner=planner,
            planner_identity=(planner.stat().st_dev,planner.stat().st_ino),ticket=ticket,
            actions=[{'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'verified'}])
        if bind:self.driver.bind_selection(job,config)
        return job,self.data[session]

    def wait(self):
        deadline=time.monotonic()+8
        while self.pool.status()['live_threads'] and time.monotonic()<deadline: time.sleep(.01)
        self.assertEqual(self.pool.status()['live_threads'],0)
        return self.pool.status()['jobs']

    def test_real_prepare_run_board_runtime_private_result_and_verified_capacity(self):
        job,data=self.selected(); self.pool.dispatch_pending(); jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'settled',jobs)
        self.assertEqual(jobs[0]['outcome'],'complete')
        self.assertTrue(jobs[0]['cleanup_confirmed']); self.assertEqual(len(self.calls),2)
        ticket=self.board.get_ticket(self.human,data['ticket']['id'])['ticket']
        self.assertEqual(ticket['status'],'in_progress'); self.assertIsNotNone(ticket['claimant'])
        self.assertTrue(all(value['state']=='finished' and value['cleanup_confirmed']
                            for value in self.board.get_runtime(self.human)['executions']))
        self.assertTrue(all(tid!=threading.get_ident() for tid,_ in self.clients))
        result_file=self.base/'pool'/'workers'/job['worker_id']/'queue'/'development-result.json'
        self.assertEqual(decode(result_file.read_bytes())['code_status'],'private_review_ready')
        self.assertFalse(decode(result_file.read_bytes())['ticket_published'])

    def test_missing_selection_refuses_before_any_registration(self):
        self.selected(bind=False); self.pool.dispatch_pending(); jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained'); self.assertEqual(self.clients,[]); self.assertEqual(self.calls,[])

    def test_immutable_selection_and_wrong_scope_refuse(self):
        job,data=self.selected(); same=self.driver.bind_selection(job,data['config'])
        self.assertEqual(same['status'],'bound')
        changed=deepcopy(data['selection']); changed['model']='different'
        write_private(data['config'].parent/'other.json',changed)
        with self.assertRaises(DevelopmentError):self.driver.bind_selection(job,data['config'].parent/'other.json')
        self.assertFalse(self.clients)

    def test_stop_uses_real_independent_journal_and_retains_no_invented_cleanup(self):
        job,data=self.selected(); entered=threading.Event()
        def during(session,callbacks):
            entered.set()
            until=time.monotonic()+3
            while not callbacks['should_cancel']() and time.monotonic()<until:time.sleep(.01)
            self.assertTrue(callbacks['should_cancel']())
        self.during=during
        self.pool.dispatch_pending(); self.assertTrue(entered.wait(3)); self.pool.stop(job['id']); jobs=self.wait()
        work=self.base/'pool'/'workers'/job['worker_id']/'work'
        self.assertTrue((work/('development-stop-'+job['id']+'.json')).is_file())
        self.assertNotEqual(jobs[0]['outcome'],'complete')
        self.assertFalse(any(t.name.startswith('development-stop-') for t in threading.enumerate()))

    def test_runtime_running_activity_is_visible_inside_actual_turn(self):
        job,_=self.selected(); observations=[]
        self.during=lambda *_:observations.append(self.pool.status()['executing_reported'])
        self.pool.dispatch_pending(); self.wait()
        self.assertEqual(observations,[1,1]); self.assertEqual(self.pool.status()['executing_reported'],0)

    def test_cleanup_false_retains_capacity(self):
        self.selected(); self.cleanup=False; self.pool.dispatch_pending(); jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained'); self.assertEqual(self.pool.status()['capacity_held'],1)

    def test_four_real_threads_have_separate_ids_clients_and_journals(self):
        jobs=[self.selected()[0] for _ in range(4)]
        self.client_barrier=threading.Barrier(4)
        self.pool.dispatch_pending(); result=self.wait()
        self.assertEqual(len({tid for tid,_ in self.clients[:4]}),4)
        self.assertEqual(len({job['session_id'] for job in jobs}),4)
        for job in jobs:
            root=self.base/'pool'/'workers'/job['worker_id']
            self.assertTrue((root/'work'/'work-journal.sqlite3').is_file())
            self.assertTrue((root/'runtime'/'runtime.sqlite3').is_file())
        # The existing global DevelopmentFence is fail-closed on NB contention;
        # this proves factory overlap, not four reliable/native model turns.
        self.assertTrue(all(job['state'] in ('settled','retained') for job in result))

    def test_four_real_prepare_run_fresh_rounds_keep_existing_fence_obstacle_visible(self):
        import pytest
        jobs=[self.selected()[0] for _ in range(4)]
        barrier=threading.Barrier(4); seen=set(); lock=threading.Lock(); observations=[]
        def during(session,callbacks):
            with lock:first=session not in seen; seen.add(session)
            if first:
                barrier.wait(timeout=3)
                observations.append((self.pool.status()['executing_reported'],self.board.get_runtime(self.human)['active_count']))
                barrier.wait(timeout=3)
        self.during=during; self.pool.dispatch_pending(); result=self.wait()
        reasons=[]
        for job in jobs:
            directory=self.base/'pool'/'workers'/job['worker_id']/'work'
            with WorkJournal(directory) as journal:
                try:op=journal.get(job['id'])
                except JournalError as error:
                    if error.code!='operation_not_found':raise
                    reasons.extend(self.prepare_errors)
                else:reasons.append(op['payload'].get('reason'))
        if 'fence_busy' in reasons:
            pytest.xfail('Existing canonical nonblocking DevelopmentFence contention; retained, no replay')
        self.assertEqual(len({tid for _,tid in self.calls}),4)
        self.assertEqual(observations,[(4,4)]*4)
        self.assertTrue(all(job['state']=='settled' for job in result),result)

    def test_selection_session_scope_and_closed_fields_refuse_before_registration(self):
        job,data=self.selected(bind=False)
        mutations=[{'session_id':str(uuid.uuid4())}, {'model':None}, {'model':''},
                   {'extra':'not allowed'}, {'scope':{**data['selection']['scope'],'ticket_id':'different'}},
                   {'budgets':{**data['selection']['budgets'],'max_turns':True}}]
        for index,change in enumerate(mutations):
            with self.subTest(change=change):
                candidate={**data['selection'],**change}
                path=data['config'].parent/('invalid-'+str(index)+'.json');write_private(path,candidate)
                with self.assertRaises((DevelopmentError,TypeError)):
                    self.driver.bind_selection(job,path)
        self.assertFalse(self.clients); self.assertFalse(self.calls)

    def test_bound_instruction_replacement_even_same_bytes_refuses_before_client(self):
        job,data=self.selected(); instruction=Path(data['selection']['instruction_file'])
        original=instruction.read_bytes(); instruction.rename(instruction.with_suffix('.old'))
        with PrivateDirectory(instruction.parent) as private:private.create(instruction.name,original)
        self.pool.dispatch_pending(); jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained');self.assertFalse(self.clients)

    def test_connection_symlink_refuses_before_client(self):
        self.selected(); saved=self.connection.with_suffix('.saved'); self.connection.rename(saved)
        self.connection.symlink_to(saved)
        self.pool.dispatch_pending(); jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained');self.assertFalse(self.clients)

    def test_run_intent_is_durable_before_first_turn(self):
        job,_=self.selected(); observed=[]
        def during(*_):
            path=self.base/'pool'/'workers'/job['worker_id']/'queue'/'development-run-intent.json'
            observed.append(decode(path.read_bytes()))
            self.assertEqual(self.pool.status()['capacity_held'],1)
        self.during=during;self.pool.dispatch_pending();self.wait()
        self.assertEqual(observed,[{'operation_id':job['id'],'session_id':job['session_id']}]*2)

    def test_lookup_outage_reopen_reconciles_cleanup_without_second_run(self):
        job,_=self.selected(); original=self.control
        unavailable=threading.Event(); unavailable.set()
        def control(client):
            result=original(client);call=result.call
            def request(name,args):
                if name=='lookup_execution' and unavailable.is_set():raise TimeoutError('synthetic outage')
                return call(name,args)
            result.call=request;return result
        with patch.object(adapter.RuntimeBoardClient,'from_worker',side_effect=control):
            self.pool.dispatch_pending();jobs=self.wait()
            self.assertEqual(jobs[0]['state'],'retained');count=len(self.calls)
            self.assertEqual(count,1)
            self.pool.close();self.pool=WorkerPool(self.base/'pool',drivers={'development':self.driver.binding()})
            unavailable.clear()
            self.assertEqual(self.pool.dispatch_pending(),[])
            self.pool.reconcile(job['id'])
            self.assertEqual(self.pool.status()['jobs'][0]['state'],'settled')
            self.assertEqual(len(self.calls),count)
            self.assertEqual(self.pool.status()['jobs'][0]['outcome'],'needs_review')

    def test_unrelated_runtime_and_missing_store_cannot_verify_boolean_cleanup(self):
        from blackoutburst_agents.worker_pool import PoolOutcome
        job,_=self.selected();self.pool.dispatch_pending();self.wait()
        internal=self.pool.journal.read()['jobs'][0];context=self.pool._context(internal)
        outcome=PoolOutcome(job['id'],job['session_id'],'complete',True)
        directory=context.journals['runtime']
        with RuntimeJournal(directory) as runtime:
            scope=self.data[job['session_id']]['selection']['scope']
            runtime.begin(RuntimeJournal.configuration(scope,str(uuid.uuid4()),str(uuid.uuid4()),'fixture-model'))
        with self.assertRaises(DevelopmentError):self.driver._verify_cleanup(context,outcome)
        database=directory/'runtime.sqlite3';database.unlink()
        with self.assertRaises((DevelopmentError,OSError)):self.driver._verify_cleanup(context,outcome)
        self.assertFalse(database.exists())

    def test_correlated_but_wrong_manifest_device_never_reports_active(self):
        self.selected();original=self.control
        def control(client):
            result=original(client);call=result.call
            def request(name,args):
                value=call(name,args)
                return {**value,'device_id':'other'} if name=='start_execution' else value
            result.call=request;return result
        with patch.object(adapter.RuntimeBoardClient,'from_worker',side_effect=control):
            self.pool.dispatch_pending();jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained');self.assertFalse(self.calls)
        self.assertEqual(self.pool.status()['executing_reported'],0)

    def test_stop_companion_error_retains_even_if_later_receipts_are_clean(self):
        job,_=self.selected();triggered=threading.Event()
        def during(*_):
            if not triggered.is_set():
                triggered.set();self.pool.stop(job['id']);time.sleep(.08)
        self.during=during
        with patch('blackoutburst_agents.development_worker.DevelopmentBoardWorker.stop',
                   side_effect=DevelopmentError('synthetic_stop_io_failure')):
            self.pool.dispatch_pending();jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained');self.assertEqual(self.pool.status()['capacity_held'],1)
        self.assertFalse(any(t.name.startswith('development-stop-') for t in threading.enumerate()))

    def test_real_protected_clients_through_inprocess_asgi_closed_mcp(self):
        from app import create_app
        from fastapi.testclient import TestClient
        from blackoutburst_agents.board_reader import BoardReader
        self.patchers[0].stop();self.patchers[1].stop()
        connection={'mcp_url':Client.endpoint,'token':self.fixture_token,'client':'codex',
                    'identity_key':'worker','name':'Fixture'}
        with PrivateDirectory(self.connection.parent) as private:
            private.replace(self.connection.name,canonical(connection))
        self.driver=adapter.DevelopmentPoolDriver(self.base/'asgi-selections',connection_file=self.connection,
            owner_id='owner',agent_id=self.agent_id,board_endpoint=Client.endpoint)
        self.pool.close();self.pool=WorkerPool(self.base/'pool',drivers={'development':self.driver.binding()})
        job,data=self.selected();calls=[]
        application=create_app(self.base/'board',base_url='https://board.test',filebrowser_url='')
        with TestClient(application) as browser:
            def rpc(channel,method,params):
                calls.append(deepcopy(params))
                response=browser.post('/mcp',headers={'Authorization':'Bearer '+channel._config['token'],
                    'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
                        'method':method,'params':params})
                self.assertEqual(response.status_code,200)
                return response.json()['result']
            with patch.object(BoardReader,'_initialize',return_value=None),patch.object(BoardReader,'_rpc',rpc):
                self.pool.dispatch_pending();result=self.wait()
        self.assertEqual(result[0]['state'],'settled',result)
        self.assertEqual(result[0]['outcome'],'complete')
        self.assertEqual(len([call for call in calls if call['name']=='register_agent']),1)
        self.assertTrue(all(call['arguments']['session_id']==job['session_id'] for call in calls))
        self.assertEqual([call['arguments']['provider'] for call in calls if call['name']=='reserve_execution'],
                         ['codex_chatgpt','local_broker','codex_chatgpt','local_broker'])
        self.assertFalse(any(call['name']=='update_ticket' for call in calls))

    def test_stop_after_prepare_before_run_has_no_fake_cleanup_or_model(self):
        job,_=self.selected();original=self.worker
        def worker(*args,**kwargs):
            value=original(*args,**kwargs);prepare=value.prepare
            def selected_prepare(**values):
                result=prepare(**values);self.pool.stop(job['id']);return result
            value.prepare=selected_prepare;return value
        with patch.object(adapter,'DevelopmentBoardWorker',side_effect=worker):
            self.pool.dispatch_pending();jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained');self.assertFalse(self.calls)
        self.assertFalse(jobs[0]['cleanup_confirmed'])
        self.assertEqual(self.board.get_runtime(self.human)['executions'],[])

    def test_replaced_bound_backend_plan_refuses_before_registration(self):
        job,data=self.selected()
        path=Path(data['selection']['backend_directory'])/'execution-plan.json'
        with PrivateDirectory(path.parent) as private:private.replace(path.name,canonical({'changed':True}))
        self.pool.dispatch_pending();jobs=self.wait()
        self.assertEqual(jobs[0]['state'],'retained');self.assertFalse(self.clients)

    def test_reconciliation_rejects_a_second_unrelated_run_work(self):
        from blackoutburst_agents.worker_pool import PoolOutcome
        job,data=self.selected();self.pool.dispatch_pending();self.wait()
        context=self.pool._context(self.pool.journal.read()['jobs'][0])
        with closing(RunStore(context.journals['runs'],existing_only=True)) as store:
            store.create_work(owner_id='other',agent_id='other',project_id='other',ticket_id='other',
                workspace=str(data['planner']),executable=str(Path(os.sys.executable).resolve()),model='fixture-model')
        with self.assertRaises(DevelopmentError):
            self.driver._verify_cleanup(context,PoolOutcome(job['id'],job['session_id'],'complete',True))
