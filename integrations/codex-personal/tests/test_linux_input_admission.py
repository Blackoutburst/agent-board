"""Synthetic files/lock observations only: no real Linux qualification or spawn."""
import copy
import json
import os
from pathlib import Path
import pickle
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch
from linux_lock_fixture import LockObservationFixture
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.dev_journal import DevelopmentJournal, decode
from blackoutburst_agents.dev_manifest import (Budgets, DevelopmentError, NamedTest,
    canonical, digest, prepare_manifest)
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.linux_input_admission import (prepare_linux_input_manifest,
    prepare_linux_input_attempt_locked, inspect_linux_input_attempt)
from blackoutburst_agents.linux_input_plan import pin_linux_input_plan
from blackoutburst_agents.linux_input_snapshot import LinuxInputSnapshot
from blackoutburst_agents.linux_test_protocol import linux_backend_id, validate_linux_request


class InputAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.observation = LockObservationFixture(self)
        temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base/'checkout'; self.root.mkdir(mode=0o700)
        (self.root/'one.txt').write_text('one\n')
        self.scripts = self.base/'approved'; self.scripts.mkdir(mode=0o700)
        self.script = self.scripts/'fixed.py'; self.script.write_text('assert True\n')
        self.spec = {'unit':{'path':'fixed.py','timeout_seconds':2,'max_output_bytes':64}}
        self.plan = pin_linux_input_plan(root=self.root,scripts_root=self.scripts,scripts=self.spec)
        self.scope = dict(owner_id='owner',agent_id='agent',device_id='device',session_id='session',
                          project_id='project',ticket_id='ticket',profile_version=1)
        self.directory = self.base/'broker'
        self.arguments = dict(root=str(self.root),base_sha='a'*40,scope=self.scope,paths=['one.txt'],
            resource='repo:fixture',authorize_scope=lambda *args:True,
            tests=[NamedTest('unit',linux_backend_id(self.plan.sha256),2,64)])
        prepare_linux_input_manifest(self.directory,**self.arguments)
        self.authorize = Mock(return_value=True)
        self.broker = DevelopmentBroker(self.directory,authorize_scope=self.authorize,enabled=True)
        self.addCleanup(self.broker.close)
        self.fence = DevelopmentFence(_test_anchor=self.base/'anchor'); self.addCleanup(self.fence.close)
        self.workflow = str(uuid.uuid4())
        pin = {'path':str(self.directory),'identity':list(self.broker.journal.private.identity)}
        self.binding = {'scope':dict(owner_id='owner',agent_id='agent',project_id='project',ticket_id='ticket',
            board_session_id='session',board_endpoint='https://board.test/mcp'),
            'root_identity':self.broker.manifest['root_identity'],'journal':pin,'configuration_hash':'b'*64,
            'child_scope':{'work_id':str(uuid.uuid4()),'runs':pin,'runtime':pin}}
        self.fence.bind(self.binding,self.workflow)
        self.planner = DevelopmentPlanner._snapshot({'files':[]},self.broker.manifest)
        self.authorize.reset_mock()

    def context(self,lease,**changes):
        args = dict(lock_lease=lease,fence=self.fence,binding=self.binding,workflow_id=self.workflow,
                    planner_snapshot=self.planner,plan=self.plan,name='unit',key='attempt')
        args.update(changes)
        return prepare_linux_input_attempt_locked(self.broker,**args)

    def refuse(self,call,code=None):
        with self.assertRaises(DevelopmentError) as caught: call()
        if code: self.assertEqual(caught.exception.code,code)

    def durable(self): return decode((self.directory/'state.json').read_bytes())

    def finish(self,handle):
        self.assertFalse(handle.observe_boundary('before_source')['execution_supported'])
        handle.observe_boundary('after_result')

    def test_five_real_capture_calls_reserved_before_first_and_fresh_callbacks(self):
        original = LinuxInputSnapshot._capture; seen=[]
        def capture(snapshot):
            state=self.durable();seen.append(state)
            self.assertEqual(state['used']['read_bytes'],5*65536)
            self.assertEqual(next(iter(state['operations'].values()))['phase'],'intent')
            return original(snapshot)
        with patch.object(LinuxInputSnapshot,'_capture',capture),patch('subprocess.Popen',side_effect=AssertionError('spawn')):
            with self.broker.journal.lock() as state:
                lease=self.broker.journal.held_lock_lease()
                with self.context(lease) as handle:
                    request=handle.request_document();packet=handle.input_packet()
                    snapshot=decode((self.directory/('linux-input-'+request['operation_id'])/'snapshot.json').read_bytes())
                    validate_linux_request(request,manifest=self.broker.manifest,snapshot=snapshot,
                        planner_snapshot=self.planner,expected_plan_sha256=self.plan.sha256,input_packet=packet)
                    self.finish(handle)
                self.refuse(handle.input_packet,'linux_input_attempt_not_held')
                self.assertEqual(state['used'],{'operations':1,'read_bytes':5*65536,'patch_bytes':0,'tests':0})
        self.assertEqual(len(seen),5)
        self.assertEqual(self.authorize.call_count,12) # initial + each of five passes, two permissions
        receipt=next(iter(self.durable()['operations'].values()))['receipt']
        self.assertEqual(receipt['result'],'prepared_input');self.assertNotIn('cleanup_confirmed',receipt)
        self.assertNotIn('started',receipt);self.assertFalse(receipt['execution_supported'])

    def test_factory_refuses_preexisting_zero_and_partial_directories_without_conversion(self):
        old=self.base/'old';prepare_manifest(old,**self.arguments)
        before=(old/'state.json').read_bytes()
        self.refuse(lambda:prepare_linux_input_manifest(old,**self.arguments),'linux_input_fresh_directory_required')
        self.assertEqual((old/'state.json').read_bytes(),before)
        partial=self.base/'partial';partial.mkdir(mode=0o700)
        self.refuse(lambda:prepare_linux_input_manifest(partial,**self.arguments))
        self.assertEqual(list(partial.iterdir()),[])

    def test_factory_crash_before_conversion_is_never_adopted(self):
        path=self.base/'interrupted'
        with patch('blackoutburst_agents.linux_input_admission.prepare_manifest',side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):prepare_linux_input_manifest(path,**self.arguments)
        self.assertTrue(path.is_dir())
        self.refuse(lambda:prepare_linux_input_manifest(path,**self.arguments))

    def test_schema1_journal_cannot_prepare_even_with_valid_live_lease(self):
        old=self.base/'old';prepare_manifest(old,**self.arguments)
        with DevelopmentBroker(old,authorize_scope=lambda *args:True,enabled=True) as broker:
            with broker.journal.lock():
                lease=broker.journal.held_lock_lease()
                self.refuse(lambda:prepare_linux_input_attempt_locked(broker,lock_lease=lease,fence=self.fence,
                    binding=self.binding,workflow_id=self.workflow,planner_snapshot=self.planner,plan=self.plan,
                    name='unit',key='x').__enter__(),'linux_input_fresh_journal_required')

    def test_budget_refuses_before_directory_or_capture_and_no_cap_increase(self):
        state_before=self.durable()
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with patch.object(self.broker.journal,'charge',side_effect=DevelopmentError('read_bytes_budget_exhausted')), \
                 patch.object(LinuxInputSnapshot,'_capture',side_effect=AssertionError('capture')):
                self.refuse(lambda:self.context(lease).__enter__(),'read_bytes_budget_exhausted')
        self.assertEqual(self.durable(),state_before)
        self.assertFalse(list(self.directory.glob('linux-input-*')))

    def test_missing_duplicate_and_out_of_order_boundaries_leave_review_without_refund(self):
        for kind in ('missing','duplicate','order'):
            with self.subTest(kind=kind):
                # Every independent case has a fresh state, never erase a charged attempt.
                self.setUp()
                with self.broker.journal.lock():
                    lease=self.broker.journal.held_lock_lease()
                    with self.assertRaises(DevelopmentError):
                        with self.context(lease) as handle:
                            if kind=='duplicate':handle.observe_boundary('before_source');handle.observe_boundary('before_source')
                            if kind=='order':handle.observe_boundary('after_result')
                state=self.durable();self.assertEqual(state['used']['read_bytes'],5*65536)
                self.assertEqual(next(iter(state['operations'].values()))['phase'],'needs_review')
                with self.broker.journal.lock():
                    lease=self.broker.journal.held_lock_lease()
                    self.refuse(lambda:self.context(lease,key='another').__enter__(),'reconciliation_required')

    def test_interrupted_capture_keeps_charge_and_never_not_started(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with patch.object(LinuxInputSnapshot,'_capture',side_effect=RuntimeError('sensitive fixture text')):
                with self.assertRaises(RuntimeError):self.context(lease).__enter__()
        state=self.durable();op=next(iter(state['operations'].values()))
        self.assertEqual(op['phase'],'needs_review');self.assertEqual(state['used']['read_bytes'],5*65536)
        self.assertNotIn('sensitive',canonical(state).decode());self.assertNotIn('not_started',canonical(state).decode())

    def test_complete_is_not_replayed_or_recharged_and_inspection_has_no_handle(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.context(lease) as handle:self.finish(handle)
        before=self.durable()
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with patch.object(LinuxInputSnapshot,'_capture',side_effect=AssertionError('replay')):
                self.refuse(lambda:self.context(lease).__enter__(),'linux_input_attempt_not_reusable')
        self.assertEqual(self.durable(),before)
        status=inspect_linux_input_attempt(self.broker,'attempt')
        self.assertEqual(status['result'],'prepared_input');self.assertFalse(status['adoptable'])
        self.assertEqual(set(status),{'found','operation_id','phase','result','reserved_read_bytes','execution_supported','adoptable'})

    def test_stale_copied_and_same_dict_rewind_cannot_erase_reservations(self):
        with self.broker.journal.lock() as state:
            old=copy.deepcopy(state);lease=self.broker.journal.held_lock_lease()
            with self.context(lease) as handle:self.finish(handle)
            committed=self.durable()
            self.refuse(lambda:self.broker.journal.save(old))
            state.clear();state.update(old)
            self.refuse(lambda:self.broker.journal.save(state))
            self.assertEqual(self.durable(),committed)

    def test_terminal_detail_counter_or_origin_changes_refused(self):
        with self.broker.journal.lock() as state:
            lease=self.broker.journal.held_lock_lease()
            with self.context(lease) as handle:self.finish(handle)
            original=copy.deepcopy(state)
            for mutation in ('origin','detail','receipt','counter','schema'):
                state.clear();state.update(copy.deepcopy(original))
                if mutation=='origin':state['linux_input_origin']=str(uuid.uuid4())
                elif mutation=='detail':next(iter(state['operations'].values()))['detail']['nonce']=str(uuid.uuid4())
                elif mutation=='receipt':next(iter(state['operations'].values()))['receipt']['result']='passed'
                elif mutation=='schema':state.pop('linux_input_origin');state['schema']=1;state['operations']={};state['used']={key:0 for key in state['used']}
                else:state['used']['read_bytes']=0
                self.refuse(lambda:self.broker.journal.save(state))
            self.assertEqual(self.durable(),original)

    def test_external_epoch_change_prevents_terminal_save_and_is_not_overwritten(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.assertRaises(DevelopmentError):
                with self.context(lease) as handle:
                    self.finish(handle)
                    value=self.durable();value['linux_input_origin']=str(uuid.uuid4())
                    # Make the external synthetic epoch coherent but different.
                    for op in value['operations'].values():
                        op['detail']['origin']=value['linux_input_origin']
                        op['request_hash']=digest(canonical({'kind':'linux_input',**op['detail']}))
                    (self.directory/'state.json').write_bytes(canonical(value))
            self.assertEqual(self.durable(),value)
            self.assertEqual(next(iter(value['operations'].values()))['phase'],'intent')

    def test_failure_after_persisted_intent_write_cannot_be_retrocompleted(self):
        original=self.broker.journal.private.replace
        def replace(name,data):original(name,data);raise OSError('ambiguous persisted write')
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with patch.object(self.broker.journal.private,'replace',replace),patch.object(LinuxInputSnapshot,'_capture',side_effect=AssertionError('capture')):
                with self.assertRaises(OSError):self.context(lease).__enter__()
        op=next(iter(self.durable()['operations'].values()))
        self.assertEqual(op['phase'],'intent');self.assertIsNone(op['receipt'])

    def test_foreign_thread_and_expired_handle_cannot_observe(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.assertRaises(DevelopmentError), self.context(lease) as handle:
                outcomes=[]
                def foreign():
                    try:handle.request_document()
                    except DevelopmentError:outcomes.append('refused')
                thread=threading.Thread(target=foreign);thread.start();thread.join(2)
                self.assertEqual(outcomes,['refused'])
                # Foreign use permanently invalidates this cooperative handle.
                self.refuse(handle.request_document)
                with self.assertRaises(DevelopmentError):self.finish(handle)
            # The exit itself refuses too; tested separately below.

    def test_plan_and_handles_are_not_copyable_or_json_adoptable(self):
        for call in (lambda:copy.copy(self.plan),lambda:pickle.dumps(self.plan),lambda:json.dumps(self.plan)):
            with self.assertRaises(TypeError):call()
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.context(lease) as handle:
                for call in (lambda:copy.deepcopy(handle),lambda:pickle.dumps(handle)):
                    with self.assertRaises(TypeError):call()
                self.refuse(lambda:self.context(lease,plan=self.plan.document(),key='other').__enter__())
                self.finish(handle)

    def test_script_path_alias_private_path_and_changed_script_refuse_before_capture(self):
        for path in ('../fixed.py','auth.json','.env/fixed.py'):
            self.refuse(lambda p=path:pin_linux_input_plan(root=self.root,scripts_root=self.scripts,
                scripts={'unit':{**self.spec['unit'],'path':p}}))
        alias={**self.spec,'second':dict(self.spec['unit'])}
        self.refuse(lambda:pin_linux_input_plan(root=self.root,scripts_root=self.scripts,scripts=alias))
        self.script.write_text('assert False\n')
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            self.refuse(lambda:self.context(lease).__enter__())
        self.assertEqual(self.durable()['used']['read_bytes'],0)

    def test_fifth_capture_revalidates_payload_before_terminal_receipt(self):
        original=LinuxInputSnapshot._capture;count=0
        def capture(snapshot):
            nonlocal count
            count+=1;result=original(snapshot)
            if count==5:
                path=next(self.directory.glob('linux-input-*'))/'input.packet'
                path.write_bytes(b'changed')
            return result
        with patch.object(LinuxInputSnapshot,'_capture',capture),self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.assertRaises(DevelopmentError):
                with self.context(lease) as handle:self.finish(handle)
        self.assertEqual(next(iter(self.durable()['operations'].values()))['phase'],'needs_review')

    def test_revoked_callback_refuses_capture_and_keeps_charges(self):
        self.authorize.side_effect=[True,True,False]
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            self.refuse(lambda:self.context(lease).__enter__())
        state=self.durable();self.assertEqual(state['used']['read_bytes'],5*65536)
        self.assertEqual(next(iter(state['operations'].values()))['phase'],'needs_review')

    def test_reconcile_refuses_before_authority_and_workspace_for_all_phases(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.context(lease) as handle:self.finish(handle)
        self.authorize.reset_mock()
        with patch.object(self.broker,'_files',side_effect=AssertionError('workspace')):
            self.refuse(lambda:self.broker.reconcile('attempt'),'linux_input_reconciliation_forbidden')
        self.authorize.assert_not_called()

    def test_old_broker_effect_paths_and_planner_refuse_profile_before_callbacks(self):
        calls=(lambda:self.broker.read_file('one.txt'),lambda:self.broker.patch_file('p','one.txt','0'*64,'x'),
               lambda:self.broker.run_test('t','unit'),self.broker.diff)
        self.authorize.reset_mock()
        with patch.object(self.broker,'_files',side_effect=AssertionError('workspace')):
            for call in calls:self.refuse(call,'linux_input_only_journal')
        self.authorize.assert_not_called()
        with patch('blackoutburst_agents.dev_planner.SupervisedNativeNamedPythonBackend',side_effect=AssertionError('backend')):
            self.refuse(lambda:DevelopmentPlanner(broker=self.broker,store=None,runtime_journal=None,
                control_client=None,board_scope={},planner_policy=None,authorization=lambda:True,
                claim_id='claim',instruction='inert',model='fixture',enabled=True),
                'planner_input_only_journal_forbidden')

    def test_family_audit_and_effect_cleanup_refuse_before_callbacks_or_workspace(self):
        from types import SimpleNamespace
        from blackoutburst_agents.family_audit import audit_workflow
        from blackoutburst_agents.development_cleanup import _effect_receipt
        worker=SimpleNamespace(journal=SimpleNamespace(get=lambda _: {'configuration':{'pins':{
            'broker':{'path':str(self.directory)}}}}))
        authorize=Mock(side_effect=AssertionError('remote callback'))
        self.refuse(lambda:audit_workflow(worker,self.workflow,manifest_hash=self.broker.journal.manifest_hash,
            authorize=authorize),'family_input_preparation_is_not_test_proof')
        authorize.assert_not_called()
        self.refuse(lambda:_effect_receipt({},self.broker.manifest,self.durable()),
                    'development_input_preparation_is_not_cleanup_proof')

    def test_state_schema_strict_types_unknown_keys_and_receipts_cannot_be_success(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.context(lease) as handle:self.finish(handle)
        original=self.durable()
        mutations=(lambda s:s.update(schema=True),lambda s:s.update(extra=True),
            lambda s:s['used'].update(tests=True),
            lambda s:next(iter(s['operations'].values()))['detail'].update(capture_passes=True),
            lambda s:next(iter(s['operations'].values()))['receipt'].update(execution_supported=0),
            lambda s:next(iter(s['operations'].values()))['receipt'].update(result='passed'),
            lambda s:next(iter(s['operations'].values()))['receipt'].update(result='not_started'))
        for mutate in mutations:
            state=copy.deepcopy(original);mutate(state)
            self.refuse(lambda:self.broker.journal.validate_state(state),'journal_corrupt')

    def test_workspace_or_fence_change_at_boundary_retains_review(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.assertRaises(DevelopmentError):
                with self.context(lease) as handle:
                    (self.root/'one.txt').write_text('changed\n')
                    handle.observe_boundary('before_source')
        state=self.durable();self.assertEqual(state['used']['read_bytes'],5*65536)
        self.assertEqual(next(iter(state['operations'].values()))['phase'],'needs_review')

    def test_wrong_planner_and_fence_refuse_before_reservation(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            self.refuse(lambda:self.context(lease,planner_snapshot={'sha256':'0'*64,'files':{}}).__enter__())
            self.refuse(lambda:self.context(lease,binding={**self.binding,'configuration_hash':'c'*64}).__enter__())
        self.assertEqual(self.durable()['used']['read_bytes'],0)

    def test_real_budget_check_rejects_thirteenth_reserved_attempt_without_refund(self):
        # 13 * 327680 = 4259840 > the existing default read cap 4194304.
        for index in range(12):
            with self.broker.journal.lock():
                lease=self.broker.journal.held_lock_lease()
                with self.context(lease,key='attempt-'+str(index)) as handle:self.finish(handle)
        before=self.durable()
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            self.refuse(lambda:self.context(lease,key='over-budget').__enter__(),'read_bytes_budget_exhausted')
        self.assertEqual(self.durable(),before)
        self.assertEqual(len(list(self.directory.glob('linux-input-*'))),12)

    def test_final_receipt_ambiguous_persisted_write_never_repeated(self):
        original=self.broker.journal.private.replace
        def replace(name,data):
            original(name,data)
            if decode(data)['operations'] and next(iter(decode(data)['operations'].values()))['phase']=='complete':
                raise OSError('synthetic persisted terminal write failure')
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with patch.object(self.broker.journal.private,'replace',replace),self.assertRaises(OSError):
                with self.context(lease) as handle:self.finish(handle)
        before=self.durable();self.assertEqual(next(iter(before['operations'].values()))['receipt']['result'],'prepared_input')
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            self.refuse(lambda:self.context(lease).__enter__(),'linux_input_attempt_not_reusable')
        self.assertEqual(self.durable(),before)

    def test_expired_lease_and_plan_wrong_process_never_reconstruct_authority(self):
        with self.broker.journal.lock():lease=self.broker.journal.held_lock_lease()
        with self.broker.journal.lock():
            self.refuse(lambda:self.context(lease).__enter__())
        with patch('os.getpid',return_value=os.getpid()+1):self.refuse(self.plan.document)
        self.refuse(self.plan.verify)

    def test_schema2_raw_capture_without_private_permit_is_refused_before_read(self):
        from blackoutburst_agents.linux_input_snapshot import capture_input_snapshot_locked
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            for permit in (None,True,{},'private'):
                self.refuse(lambda p=permit:capture_input_snapshot_locked(self.broker,lock_lease=lease,
                    fence=self.fence,binding=self.binding,operation_id=self.workflow,
                    planner_snapshot=self.planner,_capture_permit=p).__enter__(),
                    'linux_input_capture_permit_invalid')
        self.assertEqual(self.durable()['used']['read_bytes'],0)

    def test_private_raw_revalidation_cannot_exceed_five_workspace_read_passes(self):
        from blackoutburst_agents.dev_manifest import RepositoryFiles
        original=RepositoryFiles.read_at;reads=[]
        def read(files,*args):
            if files.root==str(self.root):reads.append('workspace')
            return original(*args)
        with patch.object(RepositoryFiles,'read_at',read),self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.assertRaises(DevelopmentError):
                with self.context(lease) as handle:
                    self.finish(handle)
                    # Deliberately touch a private attribute to test the hard
                    # limiter even beyond the ordinary public-handle contract.
                    handle._snapshot.revalidate()
        self.assertEqual(reads,['workspace']*5)
        self.assertEqual(next(iter(self.durable()['operations'].values()))['phase'],'needs_review')

    def test_replaced_or_rewritten_identical_payload_does_not_become_fresh_proof(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with self.assertRaises(DevelopmentError):
                with self.context(lease) as handle:
                    path=next(self.directory.glob('linux-input-*'))/'request.json'
                    same=path.read_bytes();path.write_bytes(same)
                    handle.request_document()
        self.assertEqual(next(iter(self.durable()['operations'].values()))['phase'],'needs_review')

    def test_partial_payload_and_intent_remain_unreconcilable_after_reopen(self):
        with self.broker.journal.lock():
            lease=self.broker.journal.held_lock_lease()
            with patch('blackoutburst_agents.linux_input_admission._PreparedInputs',side_effect=RuntimeError('stop')):
                with self.assertRaises(RuntimeError):self.context(lease).__enter__()
        before=self.durable()
        with DevelopmentBroker(self.directory,authorize_scope=lambda *args:True,enabled=True) as other:
            self.assertEqual(inspect_linux_input_attempt(other,'attempt')['result'],'requires_review')
            self.refuse(lambda:other.reconcile('attempt'),'linux_input_reconciliation_forbidden')
        self.assertEqual(self.durable(),before)
