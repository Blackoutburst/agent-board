"""Fresh SOURCE collection composition; no model, network or native process."""
from copy import copy,deepcopy
from pathlib import Path
import sys
from types import MethodType,SimpleNamespace
import uuid
from unittest import TestCase
from unittest.mock import Mock,patch

import test_development_frame_client as frames
import test_linux_connected_execution as connected
from linux_lock_fixture import LockObservationFixture
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.dev_test_supervisor import _fresh_control_window
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.linux_test_backend import (LinuxExecutionContext,SupervisedLinuxNamedPythonBackend,
                                                    _QualificationContext)


class LinuxSourceFrameTests(TestCase):
    def setUp(self):
        # One portable lock observer owns the entire composed fixture, including
        # the family FDs opened before the isolated broker is constructed.
        self.lock_fixture=LockObservationFixture(self)
        f=frames.DevelopmentFrameClientTests('test_one_fresh_call_per_boundary_and_exact_existing_context')
        self.addCleanup(f.doCleanups)
        repo=str(Path(__file__).resolve().parents[3]);sys.path.insert(0,repo)
        try:f.setUp()
        finally:sys.path.remove(repo)
        self.f=f
        authority=f.authority;authority.seal(f.snapshot());self.authority=authority
        worker=f.worker._worker(f.state,'child_0');operation=worker._controller_operation_id()
        authority.stop_requested=lambda:worker._controller_cancelled(operation,inspection=False)
        authority.local_stop_requested=lambda:worker._local_cancelled(operation,inspection=False)
        planner=object.__new__(DevelopmentPlanner);self.planner=planner
        planner.authorization=authority;planner.claim_id=authority.claim_id;planner._state={}
        planner._base_authorize=authority.allowed;planner._local_fresh=Mock()
        planner.active_guard=SimpleNamespace(should_cancel=Mock(return_value=False))
        broker=SimpleNamespace(manifest=authority.manifest,enabled=True)
        broker._authorize=MethodType(DevelopmentBroker._authorize,broker)
        broker.authorize_scope=planner._broker_authorize;planner.broker=broker
        backend=object.__new__(SupervisedLinuxNamedPythonBackend)
        backend.manifest=authority.manifest;backend.authorize_scope=authority.allowed
        broker.test_backend=backend
        context=object.__new__(LinuxExecutionContext);context.planner=planner;context.state=planner._state
        context._check=Mock();self.context=context
        self.clock=100.;self.cost=.5;self.read_cost=0.;self.frames=[]
        original=f.client.get_development_frame.side_effect
        def frame(**args):
            self.frames.append(args);self.clock+=self.cost
            return original(**args)
        self.enterContext(patch.object(f.client,'get_development_frame',side_effect=frame))
        self.addCleanup(f.no_effects)

    def capture(self):
        shared=self.context.authorize_capture()
        self.clock+=self.read_cost
        self.context.capture_complete(shared)
        return True

    def window(self,**kw):
        return _fresh_control_window(self.capture,nonce='a'*32,sequence=kw.pop('sequence',1),
                                     clock=lambda:self.clock,**kw)

    def test_one_actual_frame_proves_both_actions_and_context_per_boundary(self):
        first=self.window();self.assertEqual(len(self.frames),1)
        self.assertEqual(first['started_at'],100.)
        self.assertEqual(first['validated_at'],100.5)
        self.assertEqual(first['deadline'],101.5)
        second=self.window(sequence=2,previous=first)
        self.assertEqual(len(self.frames),2);self.assertEqual(second['started_at'],100.5)
        self.assertGreaterEqual(self.planner._local_fresh.call_count,6)
        self.assertGreaterEqual(self.planner.active_guard.should_cancel.call_count,6)

    def test_single_frame_does_not_waive_collection_and_read_cost_or_old_deadline(self):
        self.cost=.5;self.read_cost=1.
        with self.assertRaisesRegex(DevelopmentError,'supervisor_authorization_expired'):self.window()
        self.assertEqual(len(self.frames),1)
        self.read_cost=0.;self.clock=100.;first=self.window()
        self.clock=101.3
        with self.assertRaises(DevelopmentError):self.window(sequence=2,previous=first)
        self.assertEqual(len(self.frames),3)

    def test_second_boundary_sees_real_late_revocation_without_reusing_grant(self):
        self.window()
        self.f.board.set_policy(self.f.human,True,True,True,True,['repo.read_file'],['repo:fixture'])
        with self.assertRaises(DevelopmentError):self.window()
        self.assertEqual(len(self.frames),2)

    def test_new_human_context_after_first_capture_is_not_cached(self):
        self.window()
        thread=self.f.board.get_ticket(self.f.human,self.f.child_scope['ticket_id'])['ticket']['thread_id']
        self.f.board.post_message(self.f.human,thread,'New source',idempotency_key='source-change')
        with self.assertRaises(DevelopmentError):self.window()
        self.assertEqual(len(self.frames),2)

    def test_missing_either_action_resource_claim_or_scope_fails_pure_validation(self):
        grant=self.authority()
        for modify in (lambda g:g.update(actions=['repo.read_file']),
                       lambda g:g.update(actions=['repo.run_test']),
                       lambda g:g.update(resources=[]),lambda g:g.update(claim_id='foreign'),
                       lambda g:g['scope'].update(profile_version=True)):
            bad=deepcopy(grant);modify(bad)
            with patch.object(DevelopmentAuthorization,'__call__',return_value=bad),self.assertRaises(DevelopmentError):
                self.window()

    def test_opaque_adapter_and_wrong_bound_authority_keep_complete_callbacks(self):
        original=self.authority.allowed
        self.planner._base_authorize=lambda *args:original(*args)
        self.capture();self.assertEqual(len(self.frames),5)
        self.frames.clear()
        different=copy(self.authority)
        self.planner._base_authorize=different.allowed
        self.capture();self.assertEqual(len(self.frames),5)

    def test_changed_current_broker_callback_is_never_skipped(self):
        guard=Mock(return_value=False);self.planner.broker.authorize_scope=guard
        with self.assertRaises(DevelopmentError):self.capture()
        guard.assert_called_once_with(self.authority.manifest['scope'],'repo.read_file',
                                      self.authority.manifest['resource'])
        self.assertEqual(self.frames,[])

    def test_runtime_and_local_stop_during_read_are_rechecked(self):
        shared=self.context.authorize_capture()
        self.planner.active_guard.should_cancel.return_value=True
        with self.assertRaisesRegex(DevelopmentError,'planner_runtime_stopped'):self.context.capture_complete(shared)
        self.planner.active_guard.should_cancel.return_value=False
        self.planner._local_fresh.side_effect=DevelopmentError('planner_stop_requested')
        with self.assertRaisesRegex(DevelopmentError,'planner_stop_requested'):self.context.capture_complete(shared)
        self.assertEqual(len(self.frames),1)

    def test_broker_disabled_before_collection_or_during_read_is_not_skipped(self):
        self.planner.broker.enabled=False
        with self.assertRaisesRegex(DevelopmentError,'broker_disabled'):self.capture()
        self.assertEqual(self.frames,[])
        self.planner.broker.enabled=True;shared=self.context.authorize_capture()
        self.planner.broker.enabled=False
        with self.assertRaisesRegex(DevelopmentError,'broker_disabled'):self.context.capture_complete(shared)
        self.assertEqual(len(self.frames),1)

    def test_adapter_replacement_during_read_cannot_change_capture_mode(self):
        shared=self.context.authorize_capture()
        original=self.authority.allowed
        self.planner._base_authorize=lambda *args:original(*args)
        with self.assertRaisesRegex(DevelopmentError,'planner_authorization_changed'):
            self.context.capture_complete(shared)
        self.assertEqual(len(self.frames),1)

    def test_new_concrete_producer_cannot_replace_shared_authority_during_read(self):
        token=self.context.authorize_capture()
        other=copy(self.authority)
        self.planner.authorization=other;self.planner._base_authorize=other.allowed
        self.planner.broker.test_backend.authorize_scope=other.allowed
        self.assertTrue(self.planner._linux_capture_shared())
        with self.assertRaisesRegex(DevelopmentError,'planner_authorization_changed'):
            self.context.capture_complete(token)
        self.assertEqual(len(self.frames),1)

    def test_two_opaque_callbacks_cannot_replace_each_other_during_read(self):
        original=self.authority.allowed
        self.planner._base_authorize=lambda *args:original(*args)
        token=self.context.authorize_capture()
        self.planner._base_authorize=lambda *args:original(*args)
        self.assertFalse(self.planner._linux_capture_shared())
        with self.assertRaisesRegex(DevelopmentError,'planner_authorization_changed'):
            self.context.capture_complete(token)
        self.assertEqual(len(self.frames),4)

    def test_callable_with_forged_bound_method_attributes_is_not_an_exact_adapter(self):
        authority=self.authority
        class Impostor:
            __self__=authority
            __func__=DevelopmentAuthorization.allowed
            def __call__(self,*args):return authority.allowed(*args)
        self.planner._base_authorize=Impostor()
        self.assertFalse(self.planner._linux_capture_shared())
        self.capture();self.assertEqual(len(self.frames),5)

    def test_launch_and_continuation_share_only_the_exact_same_backend_producer(self):
        self.context.authorize();self.assertEqual(len(self.frames),1)

    def test_distinct_backend_refusal_is_respected_before_launch(self):
        guard=Mock(return_value=False)
        self.planner.broker.test_backend.authorize_scope=guard
        with self.assertRaisesRegex(DevelopmentError,'permission_revoked'):self.context.authorize()
        guard.assert_called_once_with(self.authority.manifest['scope'],'repo.run_test',
                                      self.authority.manifest['resource'])
        self.assertEqual(len(self.frames),2)

    def test_backend_revocation_after_source_refuses_the_next_continuation(self):
        self.capture()
        guard=Mock(return_value=False)
        self.planner.broker.test_backend.authorize_scope=guard
        with self.assertRaisesRegex(DevelopmentError,'permission_revoked'):self.context.authorize()
        guard.assert_called_once();self.assertEqual(len(self.frames),3)

    def test_backend_callback_or_object_replacement_during_read_refuses(self):
        for change in ('callback','backend'):
            with self.subTest(change=change):
                backend=self.planner.broker.test_backend
                callback=backend.authorize_scope
                token=self.context.authorize_capture()
                if change=='callback':backend.authorize_scope=lambda *args:True
                else:
                    other=object.__new__(SupervisedLinuxNamedPythonBackend)
                    other.manifest=backend.manifest;other.authorize_scope=callback
                    self.planner.broker.test_backend=other
                with self.assertRaisesRegex(DevelopmentError,'planner_authorization_changed'):
                    self.context.capture_complete(token)
                backend.authorize_scope=callback;self.planner.broker.test_backend=backend

    def test_offline_qualification_context_retains_its_local_callback_and_no_board_reads(self):
        backend=self.planner.broker.test_backend
        backend.authorize_scope=Mock(return_value=True)
        context=_QualificationContext(backend,'normal')
        self.assertTrue(context.authorize());self.assertTrue(context.before_source())
        self.assertEqual(backend.authorize_scope.call_count,2);self.assertEqual(self.frames,[])

    def test_context_continuation_observes_disabled_broker_before_and_during_collection(self):
        self.planner.broker.enabled=False
        with self.assertRaisesRegex(DevelopmentError,'broker_disabled'):self.context.authorize()
        self.assertEqual(self.frames,[])
        self.planner.broker.enabled=True
        original=self.f.client.get_development_frame.side_effect
        def revoke(**args):
            result=original(**args);self.planner.broker.enabled=False;return result
        with patch.object(self.f.client,'get_development_frame',side_effect=revoke), \
                self.assertRaisesRegex(DevelopmentError,'broker_disabled'):
            self.context.authorize()
        self.assertEqual(len(self.frames),1)

    def tool_fixture(self, *, backend_allowed=True):
        """Actual planner/broker/lease/five captures + real temporary Board frame.

        Only qualification/pins, model transport and Runtime service are doubles.
        The live authority scope is used by the concrete isolated input broker.
        """
        from test_dev_planner import FakePolicy,FakeControl
        from blackoutburst_agents.run_store import RunStore
        from blackoutburst_agents.runtime_journal import RuntimeJournal
        from blackoutburst_agents.dev_manifest import canonical,RepositoryFiles
        f=connected.ConnectedExecutionTests();f.scope_override=self.authority.manifest['scope']
        f.lock_fixture=self.lock_fixture
        f.setUp();self.addCleanup(f.doCleanups)
        allowed=self.authority.allowed
        f.broker.authorize_scope=allowed
        f.backend.authorize_scope=allowed if backend_allowed else Mock(return_value=False)
        store=RunStore(f.base/'runs');self.addCleanup(store.close)
        runtime=RuntimeJournal(f.base/'runtime');self.addCleanup(runtime.close)
        scope={key:f.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        scope.update(board_endpoint=self.authority.scope['board_endpoint'],board_session_id=f.scope['session_id'])
        control=FakeControl(scope,f.scope);control.session_id=f.scope['session_id']
        workspace=f.base/'planner-empty';workspace.mkdir(mode=0o700)
        actions=[{'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'Inspected.'}]
        def transport(settings,prompt,workspace,**callbacks):
            callbacks['on_started'](999999999)
            if 'before_prompt' in callbacks:callbacks['before_prompt']()
            session=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](session)
            return {'status':'completed','reason':'verified','session_id':session,
                'text':canonical(actions.pop(0)).decode(),'verification':dict(cleanup_confirmed=True,
                    tool_free=True,session_verified=True,turn_completed=True,exit_zero=True,authentication='chatgpt')}
        observations=[];read_at=RepositoryFiles.read_at
        def run(attempt):
            self.frames.clear();self.clock=100.;reads=[]
            def read(*args,**kw):
                result=read_at(*args,**kw);reads.append(len(self.frames));return result
            with patch.object(RepositoryFiles,'read_at',staticmethod(read)):
                window=_fresh_control_window(attempt.before_source,nonce='a'*32,sequence=1,
                                              clock=lambda:self.clock)
            observations.append({'frames':len(self.frames),'reads_at_frame':reads,
                                 'started_at':window['started_at'],'deadline':window['deadline'],
                                 'validated_at':window['validated_at']})
            return f.native(attempt.request)
        f.backend.run.side_effect=run
        p=DevelopmentPlanner(broker=f.broker,store=store,runtime_journal=runtime,control_client=control,
            board_scope=scope,planner_policy=FakePolicy(workspace),authorization=self.authority,
            claim_id=self.authority.claim_id,instruction='Run the approved named test.',model='fixture-model',
            enabled=True,_test_anchor=f.base/'planner-anchor',_transport=transport,
            _allow_fixture_backend=True,_clock=lambda:100.)
        self.addCleanup(p.close)
        return p,f,observations

    def test_actual_tool_with_real_source_read_keeps_one_frame_and_unchanged_ttl(self):
        planner,fixture,observed=self.tool_fixture()
        result=planner.run();self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(observed,[{'frames':1,'reads_at_frame':[1],
            'started_at':100.,'deadline':101.5,'validated_at':100.5}])
        self.assertGreaterEqual(fixture.state()['used']['read_bytes'],5*65537)
        self.assertEqual(next(iter(fixture.state()['linux_execution'].values()))['phase'],'broker_admitted')

    def test_actual_tool_does_not_bypass_a_distinct_backend_refusal(self):
        planner,fixture,observed=self.tool_fixture(backend_allowed=False)
        result=planner.run();self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(result['reason'],'permission_revoked',result)
        fixture.backend.run.assert_not_called();self.assertEqual(observed,[])


class LinuxSourceCaptureCompositionTests(TestCase):
    def test_schema1_tool_keeps_its_existing_composed_backend_guard(self):
        from test_dev_planner import PlannerTests
        f=PlannerTests();f.setUp();self.addCleanup(f.doCleanups)
        f.actions=[f.action('test',name='unit'),f.action('final',summary='Inspected.')]
        original=Mock(return_value=True);f.backend.authorize_scope=original
        actual_run=f.backend.run;callbacks=[]
        def run(request):
            callback=f.backend.authorize_scope;callbacks.append(callback)
            self.assertIsNot(callback,original)
            self.assertTrue(callback(f.grant_scope,'repo.run_test','repo:fixture'))
            return actual_run(request)
        f.backend.run=run
        result=f.create().run();self.assertEqual(result['status'],'review_ready',result)
        self.assertEqual(len(callbacks),1);original.assert_called_once_with(
            f.grant_scope,'repo.run_test','repo:fixture')
        self.assertIs(f.backend.authorize_scope,original)

    def test_five_real_captures_have_five_distinct_observations_and_no_refund(self):
        f=connected.ConnectedExecutionTests();f.setUp();self.addCleanup(f.doCleanups)
        planner=f.planner
        producer=object.__new__(DevelopmentAuthorization);producer.expected_context={'synthetic':True}
        planner.authorization=producer;planner.claim_id='claim';planner._base_authorize=producer.allowed
        planner.broker.authorize_scope=planner._broker_authorize
        f.backend.authorize_scope=producer.allowed
        # Restore actual planner methods; only this explicit source producer is a double.
        del planner._fresh
        grant={'scope':deepcopy(f.scope),'claim_id':'claim','owner_enabled':True,
            'profile_enabled':True,'project_allowed':True,'claim_current':True,
            'actions':list(f.broker.manifest['actions']),'resources':[f.broker.manifest['resource']]}
        observations=[];in_capture=[False]
        from blackoutburst_agents.linux_input_snapshot import LinuxInputSnapshot
        capture=LinuxInputSnapshot._capture
        def tracked(snapshot):
            in_capture[0]=True
            try:return capture(snapshot)
            finally:in_capture[0]=False
        def collect(_):
            if in_capture[0]:observations.append(len(observations)+1)
            return deepcopy(grant)
        with patch.object(DevelopmentAuthorization,'__call__',collect),patch.object(LinuxInputSnapshot,'_capture',tracked):
            result=f.run_test()
        self.assertEqual(result['result'],'passed');self.assertEqual(observations,[1,2,3,4,5])
        self.assertEqual(f.state()['used']['read_bytes'],5*65537)
