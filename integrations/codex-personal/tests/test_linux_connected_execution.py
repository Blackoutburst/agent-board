"""Real local journals/snapshots with a simulated backend, never a native launch."""
import copy
import ast
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from linux_lock_fixture import LockObservationFixture
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, canonical, digest
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.linux_input_plan import pin_linux_input_plan
from blackoutburst_agents.linux_input_snapshot import LinuxInputSnapshot
from blackoutburst_agents.linux_test_backend import (KIND, PLAN_SCHEMA, NATIVE_SCHEMA, CHECKS,
    SupervisedLinuxNamedPythonBackend, execution_context, prepare_linux_execution_manifest,
    prepare_linux_backend, lookup_linux_native_cleanup, validate_native_receipt)
from blackoutburst_agents.linux_test_protocol import linux_execution_backend_id


class ConnectedExecutionTests(unittest.TestCase):
    def setUp(self):
        if not hasattr(self,'lock_fixture'):
            self.lock_fixture=LockObservationFixture(self)
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup); self.base=Path(temp.name).resolve()
        self.root=self.base/'checkout'; self.root.mkdir(mode=0o700)
        (self.root/'one.txt').write_text('one\n')
        self.scripts=self.base/'scripts'; self.scripts.mkdir(mode=0o700)
        (self.scripts/'fixed.py').write_text('assert True\n')
        input_plan=pin_linux_input_plan(root=self.root,scripts_root=self.scripts,
            scripts={'unit':{'path':'fixed.py','timeout_seconds':2,'max_output_bytes':64}}).document()
        # Native preparation is deliberately mocked; only these local documents
        # are used to exercise admission. They cannot qualify a real backend.
        self.plan={'schema':PLAN_SCHEMA,'root':str(self.root),
            'root_identity':input_plan['root_chain'][-1]['identity'],'input_plan':input_plan,
            'environment':{'synthetic':True},
            'input_plan_sha256':digest(canonical(input_plan))}
        self.plan_hash=digest(canonical(self.plan)); self.directory=self.base/'broker'
        self.scope=copy.deepcopy(getattr(self,'scope_override',dict(owner_id='owner',agent_id='agent',
            device_id='device',session_id='session',project_id='project',ticket_id='ticket',profile_version=1)))
        with patch('blackoutburst_agents.linux_test_backend.validate_current_plan'):
            prepare_linux_execution_manifest(self.directory,execution_plan=self.plan,root=str(self.root),
                base_sha='a'*40,scope=self.scope,paths=['one.txt'],resource='repo:fixture',
                authorize_scope=lambda *args:True,
                tests=[NamedTest('unit',linux_execution_backend_id(self.plan_hash),2,64)])
        self.backend=object.__new__(SupervisedLinuxNamedPythonBackend)
        self.backend.plan=self.plan;self.backend.document={'plan_hash':self.plan_hash,'kind':KIND}
        self.backend.backend_id=linux_execution_backend_id(self.plan_hash)
        self.backend._pins=Mock();self.backend.qualified_for=Mock(return_value=True)
        self.backend.run=Mock(side_effect=self.native_run)
        self.authorize=Mock(return_value=True)
        self.backend.authorize_scope=self.authorize
        self.broker=DevelopmentBroker(self.directory,authorize_scope=self.authorize,enabled=True,test_backend=self.backend)
        self.addCleanup(self.broker.close);self.backend.manifest=self.broker.manifest
        self.fence=DevelopmentFence(_test_anchor=self.base/'anchor');self.addCleanup(self.fence.close)
        self.workflow=str(uuid.uuid4());pin={'path':str(self.directory),'identity':list(self.broker.journal.private.identity)}
        binding={'scope':{**{key:self.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')},
            'board_session_id':self.scope['session_id'],'board_endpoint':'https://board.test/mcp'},
            'root_identity':self.broker.manifest['root_identity'],'journal':pin,'configuration_hash':'b'*64,
            'child_scope':{'work_id':str(uuid.uuid4()),'runs':pin,'runtime':pin}}
        self.fence.bind(binding,self.workflow)
        planner=object.__new__(DevelopmentPlanner);self.planner=planner
        planner.broker=self.broker;planner.binding=binding;planner.operation_id=self.workflow
        planner.fence=self.fence;planner.active_guard=None;planner._fresh=Mock();planner._local_fresh=Mock()
        planner.authorization=Mock();planner._base_authorize=self.authorize
        planner._linux_context_fresh=planner._fresh
        self.step={'action':{'schema':1,'kind':'test','name':'unit'},'action_key':'attempt','receipt':None,'tool_finished':False,
            'source_snapshot':DevelopmentPlanner._snapshot({'files':[]},self.broker.manifest)}
        planner._state={'phase':'tool_intent','step':self.step}
        planner.journal=SimpleNamespace(read=lambda:copy.deepcopy(planner._state))
        self.spawn_guard=patch('subprocess.Popen',side_effect=AssertionError('native launch forbidden'))
        self.spawn_guard.start();self.addCleanup(self.spawn_guard.stop)

    def state(self):return decode((self.directory/'state.json').read_bytes())
    def context(self):return execution_context(self.planner,self.planner._state)
    def refuse(self,call,code=None):
        with self.assertRaises(DevelopmentError) as caught:call()
        if code:self.assertEqual(caught.exception.code,code)

    def native_run(self,attempt):
        state=self.state();op=state['operations'][attempt.request['operation_id']]
        self.assertEqual(op['admission']['dispatch'],'dispatched')
        self.assertEqual(state['linux_execution'][op['id']]['phase'],'dispatched')
        self.assertEqual(state['used']['read_bytes'],5*65537)
        self.assertEqual(state['used']['tests'],1)
        attempt.before_source()
        return self.native(attempt.request)

    def native(self,request):
        return {'schema':NATIVE_SCHEMA,'nonce':'a'*32,'request_sha256':digest(canonical(request)),
            'plan_sha256':self.plan_hash,'source_released':True,'source_release_intent':True,
            'exit_code':0,'reason':'completed','checks':{key:True for key in CHECKS},
            'launcher_attempted':True,'output_bytes':0,'output_sha256':digest(b''),
            'cleanup':{'init_identity_received':True,'init_terminal':True,'launcher_reaped':True,
                'cleanup_confirmed':True,'numeric_pid_signalled':False},
            'identity':{'namespace_identity':[1,2],'acquired_by':'SCM_RIGHTS_validated','numeric_pid_signalled':False},
            'broker_snapshot_admitted':False}

    def run_test(self):return self.broker.run_test('attempt','unit',_linux_context=self.context())

    def test_full_routed_broker_admission_five_captures_and_replay(self):
        captures=[];original=LinuxInputSnapshot._capture
        def capture(snapshot):
            state=self.state();captures.append(state['used']['read_bytes'])
            self.assertEqual(state['used']['tests'],1)
            return original(snapshot)
        with patch.object(LinuxInputSnapshot,'_capture',capture):receipt=self.run_test()
        self.assertEqual(captures,[5*65537]*5);self.assertEqual(receipt['result'],'passed')
        state=self.state();record=next(iter(state['linux_execution'].values()))
        self.assertEqual(record['phase'],'broker_admitted')
        self.assertEqual(record['admitted_snapshot_sha256'],self.step['source_snapshot']['sha256'])
        self.assertEqual(self.run_test(),receipt);self.backend.run.assert_called_once()
        self.assertEqual(self.state(),state)

    def test_plain_read_and_diff_keep_charges_without_operation_rows(self):
        self.broker.read_file('one.txt');self.broker.diff()
        before=self.state()['used']['read_bytes'];self.assertGreater(before,0)
        self.assertEqual(self.state()['operations'],{})
        # The test fixture checks the charged capture independently from reads.
        def run(attempt):attempt.before_source();return self.native(attempt.request)
        self.backend.run.side_effect=run;self.run_test()
        state=self.state();self.assertEqual(len(state['operations']),1)
        self.assertEqual(state['used']['read_bytes'],before+5*65537)

    def test_schema3_patch_and_preapply_reconciliation_keep_existing_semantics(self):
        with patch.object(self.broker,'_apply',side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):
                self.broker.patch_file('patch','one.txt',digest(b'one\n'),'two\n')
        self.assertEqual(self.broker.reconcile('patch')['result'],'not_applied')
        result=self.broker.patch_file('next-patch','one.txt',digest(b'one\n'),'two\n')
        self.assertEqual(result['result'],'applied')
        self.assertEqual(self.state()['linux_execution'],{})

    def test_source_change_before_source_prevents_admission_and_replay(self):
        def run(attempt):
            (self.root/'one.txt').write_text('different\n');attempt.before_source()
        self.backend.run.side_effect=run;self.refuse(self.run_test)
        state=self.state();self.assertEqual(next(iter(state['operations'].values()))['phase'],'needs_review')
        self.assertEqual(next(iter(state['linux_execution'].values()))['phase'],'needs_review')
        self.refuse(self.run_test,'reconciliation_required');self.backend.run.assert_called_once()

    def test_change_at_fourth_or_fifth_capture_never_promotes_native_success(self):
        for index in (4,5):
            with self.subTest(index=index):
                if index==5:self.setUp()
                original=LinuxInputSnapshot._capture;calls=[]
                def capture(snapshot):
                    calls.append(1)
                    if len(calls)==index:(self.root/'one.txt').write_text('new\n')
                    return original(snapshot)
                with patch.object(LinuxInputSnapshot,'_capture',capture):self.refuse(self.run_test)
                self.assertNotEqual(next(iter(self.state()['linux_execution'].values()))['phase'],'broker_admitted')

    def test_fresh_guard_revocation_and_missing_context_never_dispatch(self):
        self.refuse(lambda:self.broker.run_test('attempt','unit'),'linux_execution_context_required')
        self.assertEqual(self.state()['operations'],{})
        self.planner._fresh.side_effect=DevelopmentError('revoked')
        self.refuse(self.run_test,'revoked');self.backend.run.assert_not_called()

    def test_distinct_backend_refusal_precedes_source_read_and_dispatch(self):
        from blackoutburst_agents.dev_manifest import RepositoryFiles
        self.backend.authorize_scope=Mock(return_value=False)
        with patch.object(RepositoryFiles,'read_at',side_effect=AssertionError('unauthorized source read')):
            self.refuse(self.run_test,'permission_revoked')
        self.backend.run.assert_not_called()

    def test_backend_revoked_after_source_never_admits_native_success(self):
        def run(attempt):
            attempt.before_source()
            self.backend.authorize_scope=Mock(return_value=False)
            return self.native(attempt.request)
        self.backend.run.side_effect=run
        self.refuse(self.run_test,'permission_revoked')
        self.assertEqual(next(iter(self.state()['linux_execution'].values()))['phase'],'needs_review')
        self.backend.run.assert_called_once()

    def test_ambiguous_dispatched_save_never_enters_backend_and_never_retries(self):
        original=self.broker.journal.private.replace
        def replace(name,data):
            value=decode(data)
            original(name,data)
            if name=='state.json' and any(r['phase']=='dispatched' for r in value.get('linux_execution',{}).values()):
                raise DevelopmentError('private_write_failed')
        with patch.object(self.broker.journal.private,'replace',replace):self.refuse(self.run_test)
        self.backend.run.assert_not_called()
        self.assertEqual(next(iter(self.state()['operations'].values()))['admission']['dispatch'],'dispatched')
        self.refuse(self.run_test,'reconciliation_required')

    def test_dispatched_without_local_context_refuses_reconciliation_even_if_native_exists(self):
        self.backend.run.side_effect=DevelopmentError('lost');self.refuse(self.run_test)
        self.refuse(lambda:self.broker.reconcile('attempt'),'linux_execution_identity_cleanup_required')

    def test_capture_budget_refuses_before_first_source_read_and_prelaunch_receipt_is_not_admitted(self):
        original=self.broker.journal.charge
        def charge(state,**kw):
            if kw.get('read_bytes'):raise DevelopmentError('read_bytes_budget_exhausted')
            return original(state,**kw)
        with patch.object(self.broker.journal,'charge',charge), \
             patch.object(LinuxInputSnapshot,'_capture',side_effect=AssertionError('unbudgeted read')):
            self.refuse(self.run_test,'read_bytes_budget_exhausted')
        self.backend.run.assert_not_called();state=self.state()
        self.assertEqual(state['linux_execution'],{})
        self.assertEqual(next(iter(state['operations'].values()))['receipt']['result'],'not_started')

    def test_step_action_identity_change_after_launch_refuses_even_with_native_success(self):
        def run(attempt):
            attempt.before_source();self.step['action_key']='another'
            return self.native(attempt.request)
        self.backend.run.side_effect=run;self.refuse(self.run_test,'linux_execution_step_changed')
        self.assertEqual(next(iter(self.state()['linux_execution'].values()))['phase'],'needs_review')

    def test_saved_profile_counters_and_terminal_receipt_cannot_be_rewritten(self):
        self.run_test();original=self.state()
        for change in ('schema','refund','receipt','request','record'):
            with self.subTest(change=change):
                with self.broker.journal.lock() as state:
                    if change=='schema':state['schema']=1
                    if change=='refund':state['used']['read_bytes']-=1
                    key=next(iter(state['operations']))
                    if change=='receipt':state['operations'][key]['receipt']['result']='failed'
                    if change=='request':state['linux_execution'][key]['request']['workflow_id']=str(uuid.uuid4())
                    if change=='record':state['linux_execution'].clear()
                    self.refuse(lambda:self.broker.journal.save(state))
                self.assertEqual(self.state(),original)

    def test_new_backend_control_directory_must_be_exclusive_and_outside_inputs(self):
        with patch('blackoutburst_agents.linux_test_backend.validate_current_plan'):
            self.refuse(lambda:prepare_linux_backend(self.root/'private-control',manifest=self.broker.manifest,
                plan=self.plan),'linux_execution_directory_overlap')
            self.assertFalse((self.root/'private-control').exists())
            self.assertEqual(prepare_linux_backend(self.base/'backend',manifest=self.broker.manifest,
                plan=self.plan),self.plan_hash)
            with self.assertRaises(FileExistsError):prepare_linux_backend(self.base/'backend',
                manifest=self.broker.manifest,plan=self.plan)

    def test_loss_faults_require_observer_before_any_launch(self):
        for case in ('controller_loss','supervisor_loss'):
            self.refuse(lambda:self.backend._qualify_case(case),'linux_qualification_observer_required')

    def test_input_only_and_unsealed_family_profiles_are_never_proof(self):
        from blackoutburst_agents.family_audit import _execution_profile as family_profile
        from blackoutburst_agents.development_cleanup import _execution_profile as cleanup_profile
        for profile in (family_profile,cleanup_profile):
            self.refuse(lambda:profile({'schema':2},{}))
            self.refuse(lambda:profile(self.state(),{}))
            self.assertTrue(profile(self.state(),{'execution_profile':KIND,
                'linux_execution_plan_sha256':self.plan_hash}))

    def test_native_success_is_metadata_only_and_strict(self):
        self.run_test();request=next(iter(self.state()['linux_execution'].values()))['request']
        job={'nonce':'a'*32,'request':request,'max_output_bytes':64}
        self.assertFalse(validate_native_receipt(self.native(request),job,self.plan)['broker_snapshot_admitted'])
        for key,value in [('broker_snapshot_admitted',True),('identity',{'namespace_identity':['1',2],
            'acquired_by':'SCM_RIGHTS_validated','numeric_pid_signalled':False}),('checks',{})]:
            native=self.native(request);native[key]=value
            self.refuse(lambda:validate_native_receipt(native,job,self.plan))

    def test_historical_native_cleanup_after_revoke_stays_blocked_and_reads_no_current_pins(self):
        self.run_test();state=self.state();operation_id=next(iter(state['linux_execution']))
        record=copy.deepcopy(state['linux_execution'][operation_id]);record['phase']='needs_review'
        record['admitted_snapshot_sha256']=None
        backend=self.base/'backend';backend.mkdir(mode=0o700)
        request=record['request'];job={'schema':KIND,'nonce':'a'*32,'request':request,
            'backend_directory':str(backend),'plan_sha256':self.plan_hash,'max_output_bytes':64,
            'timeout_seconds':2,'script_sha256':digest(b'assert True\n'),
            'control':{'schema':1,'nonce':'a'*32,'sequence':1,'started_at':10.0,'validated_at':10.1,'deadline':11.5},
            'qualification':False,'qualification_case':None}
        native=self.native(request);raw=canonical(native)
        with PrivateDirectory(backend) as private:
            private.create('execution-plan.json',canonical(self.plan))
            private.create('backend-manifest.json',canonical(self.broker.manifest))
        execution=backend/('execution-'+operation_id);execution.mkdir(mode=0o700)
        with PrivateDirectory(execution) as private:
            private.create('job.json',canonical(job));private.create('native-receipt.json',raw)
            window={**job['control'],'sequence':4,'started_at':10.2,'validated_at':10.3,'deadline':11.7}
            private.create('source-admission.json',canonical({'nonce':'a'*32,
                'request_sha256':digest(canonical(request)),'identity':native['identity'],
                'control':window,'phase':'source_release_intent'}))
            private.create('source-window.json',canonical({'nonce':'a'*32,'challenge':'b'*32,
                'request_sha256':digest(canonical(request)),'window_sha256':digest(canonical(window))}))
            private.create('source-window.ready',b'')
            private.create('state.json',canonical({'schema':1,'nonce':'a'*32,
                'request_sha256':digest(canonical(request)),'phase':'complete','boundary_sequence':3,
                'source_challenge':'b'*32,'native_receipt_sha256':digest(raw)}))
        with patch('blackoutburst_agents.linux_test_backend._environment',side_effect=AssertionError('environment')), \
             patch('blackoutburst_agents.linux_test_backend._sources',side_effect=AssertionError('sources')):
            proof=lookup_linux_native_cleanup(backend,manifest=self.broker.manifest,record=record,
                operation_id=operation_id,workflow_id=self.workflow,expected_plan_sha256=self.plan_hash)
        self.assertEqual(proof['status'],'blocked');self.assertFalse(proof['broker_snapshot_admitted'])
        record['reservation']['workflow_id']=str(uuid.uuid4())
        self.refuse(lambda:lookup_linux_native_cleanup(backend,manifest=self.broker.manifest,record=record,
            operation_id=operation_id,workflow_id=self.workflow,expected_plan_sha256=self.plan_hash))


class PlannerConnectedExecutionTests(unittest.TestCase):
    """The actual planner/runtime/journal path, with all external effects faked."""
    def setUp(self):
        from test_dev_planner import FakePolicy,FakeControl
        from blackoutburst_agents.run_store import RunStore
        from blackoutburst_agents.runtime_journal import RuntimeJournal
        self.fixture=ConnectedExecutionTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.broker=self.fixture.broker
        self.store=RunStore(self.fixture.base/'runs');self.addCleanup(self.store.close)
        self.runtime=RuntimeJournal(self.fixture.base/'runtime');self.addCleanup(self.runtime.close)
        self.scope={key:self.fixture.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        self.scope.update(board_session_id='session',board_endpoint='https://board.test/mcp')
        self.control=FakeControl(self.scope,self.fixture.scope);self.control.session_id='session'
        workspace=self.fixture.base/'empty';workspace.mkdir(mode=0o700);self.policy=FakePolicy(workspace)
        self.grant={'scope':self.fixture.scope,'claim_id':'claim','owner_enabled':True,
            'profile_enabled':True,'project_allowed':True,'claim_current':True,
            'actions':self.broker.manifest['actions'],'resources':['repo:fixture']}
        self.actions=[{'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'Inspected.'}]
        self.revoke=False
        def run(attempt):
            attempt.before_source()
            if self.revoke:self.grant['claim_current']=False
            return self.fixture.native(attempt.request)
        self.fixture.backend.run.side_effect=run

    def transport(self,settings,prompt,workspace,**callbacks):
        callbacks['on_started'](999999999)
        if 'before_prompt' in callbacks:callbacks['before_prompt']()
        session=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](session)
        return {'status':'completed','reason':'verified','session_id':session,
            'text':canonical(self.actions.pop(0)).decode(),
            'verification':dict(cleanup_confirmed=True,tool_free=True,session_verified=True,
                turn_completed=True,exit_zero=True,authentication='chatgpt')}

    def create(self):
        planner=DevelopmentPlanner(broker=self.broker,store=self.store,runtime_journal=self.runtime,
            control_client=self.control,board_scope=self.scope,planner_policy=self.policy,
            authorization=lambda:copy.deepcopy(self.grant),claim_id='claim',instruction='Run the named test.',
            model='fixture-model',enabled=True,_test_anchor=self.fixture.base/'planner-anchor',
            _transport=self.transport,_allow_fixture_backend=True,_clock=lambda:100.0)
        self.addCleanup(planner.close);return planner

    def test_complete_planner_test_and_final_bind_real_versioned_step_and_snapshot(self):
        planner=self.create();result=planner.run()
        self.assertEqual(result['status'],'review_ready',result)
        state=self.fixture.state();record=next(iter(state['linux_execution'].values()))
        self.assertEqual(record['phase'],'broker_admitted')
        self.assertEqual(record['reservation']['workflow_id'],planner.operation_id)
        self.assertEqual(record['admitted_snapshot_sha256'],result['review']['snapshot_sha256'])
        self.assertEqual(self.control.max_active,1)
        self.assertTrue(all(row['cleanup_confirmed'] for row in self.control.rows.values()))
        self.assertEqual(planner.run(),result);self.fixture.backend.run.assert_called_once()

    def test_revoke_after_native_success_keeps_runtime_and_test_proof_unconfirmed(self):
        self.revoke=True;result=self.create().run()
        self.assertEqual(result['status'],'needs_review')
        record=next(iter(self.fixture.state()['linux_execution'].values()))
        self.assertEqual(record['phase'],'needs_review');self.assertIsNone(record['admitted_snapshot_sha256'])
        self.assertTrue(any(not row['cleanup_confirmed'] for row in self.control.rows.values()))


class LinuxPairSelectionTests(unittest.TestCase):
    def test_source_roster_is_literal_transitively_closed_and_recipe_matches(self):
        from blackoutburst_agents.linux_planner_policy import LINUX_EXECUTION_SOURCES
        from blackoutburst_agents.family_sources import MODULES
        import blackoutburst_agents.linux_test_backend as backend_module
        package=Path(backend_module.__file__).parent
        roster=set(LINUX_EXECUTION_SOURCES)
        self.assertEqual(tuple(sorted(roster)),LINUX_EXECUTION_SOURCES)
        self.assertEqual(MODULES,LINUX_EXECUTION_SOURCES)
        for name in roster:
            path=package/name
            self.assertTrue(path.exists(),name)
            for node in ast.walk(ast.parse(path.read_bytes())):
                if isinstance(node,ast.ImportFrom) and node.level==1 and node.module:
                    self.assertIn(node.module.split('.')[0]+'.py',roster,(name,node.module))
        path=package.parent/'tests'/'qualify_linux_planner_policy.py'
        tree=ast.parse(path.read_bytes())
        actual=next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
            and any(isinstance(target,ast.Name) and target.id=='ALLOWED_SOURCES' for target in node.targets))
        self.assertEqual(actual,LINUX_EXECUTION_SOURCES)

    def test_factory_selection_is_explicit_and_family_requires_one_pair(self):
        from blackoutburst_agents.development_worker import execution_factories
        from blackoutburst_agents.dev_test_supervisor import SupervisedNativeNamedPythonBackend
        from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
        from blackoutburst_agents.family_plan import execution_profile
        self.assertIs(execution_factories()['backend_factory'],SupervisedNativeNamedPythonBackend)
        selected=execution_factories(KIND)
        self.assertIs(selected['backend_factory'],SupervisedLinuxNamedPythonBackend)
        self.assertIs(selected['policy_factory'],LinuxPlannerPolicy)
        for profile in ('linux','auto','input-only',None,{'kind':KIND}):
            with self.assertRaises(DevelopmentError):execution_factories(profile)
        valid={'parent':{'execution_profile':KIND},'children':[{'execution_profile':KIND},{'execution_profile':KIND}]}
        self.assertEqual(execution_profile(valid),KIND)
        for value in ({}, {'parent':{},'children':[{},None]},
                      {**valid,'children':[{'execution_profile':KIND},{}]}):
            with self.assertRaises(DevelopmentError):execution_profile(value)

    def test_worker_checks_concrete_pair_and_same_environment_before_yield(self):
        from blackoutburst_agents.development_worker import DevelopmentBoardWorker
        from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
        f=ConnectedExecutionTests();f.setUp();self.addCleanup(f.doCleanups)
        policy=object.__new__(LinuxPlannerPolicy)
        policy._plan={'environment':copy.deepcopy(f.plan['environment']),'executable':'fixed-cli','workspace':'empty'}
        policy.validate_for=Mock();policy.close=Mock();f.backend.close=Mock()
        worker=object.__new__(DevelopmentBoardWorker);worker.allow_fixture=False
        worker.policy_factory=lambda _:policy;worker.backend_factory=lambda *a,**kw:f.backend
        with worker._resources(f.directory,'unused','unused',authorize=lambda *_:True) as resources:
            self.assertIs(resources[1],policy);self.assertIs(resources[2],f.backend)
        policy._plan['environment']={'synthetic':False}
        with self.assertRaises(DevelopmentError):
            with worker._resources(f.directory,'unused','unused',authorize=lambda *_:True):pass
