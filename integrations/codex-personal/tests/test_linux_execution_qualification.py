"""Pure/fake matrix contracts. No native job, PID, CLI or signal is exercised."""
import copy
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch

from blackoutburst_agents import linux_execution_qualification as module
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest


class FakeBackend:
    def __init__(self,path):
        self.private=PrivateDirectory(path)
        self.document={'plan_hash':'a'*64};self.environment={'fixture':'unexecuted synthetic metadata'}
        self.manifest={'scope':{'fixture':True},'root':'/synthetic/root','root_identity':[10,20]}
        self.plan={'input_plan_sha256':'b'*64}
        self._pins=Mock();self._authorize=Mock(side_effect=AssertionError('no authority callback'))


class MatrixTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve();self.path=self.root/'backend';self.path.mkdir(mode=0o700)
        self.backend=FakeBackend(self.path);self.addCleanup(self.backend.private.close)
        self.requests={case:{'operation_id':digest(case.encode()),'name':'qualification-'+case} for case in module.CASES}
        self.calls=[]
        self.backend_class=patch.object(module,'_backend_class',return_value=FakeBackend);self.backend_class.start();self.addCleanup(self.backend_class.stop)
        self.real=patch.object(module,'_real_host',return_value=None);self.real.start();self.addCleanup(self.real.stop)
        self.req=patch.object(module,'_request',side_effect=self.check_request);self.req.start();self.addCleanup(self.req.stop)
        self.native=patch.object(module,'_native',side_effect=self.fake_native);self.native.start();self.addCleanup(self.native.stop)
        self.expected=patch.object(module,'_expected_native',return_value=True);self.expected.start();self.addCleanup(self.expected.stop)
        self.job=patch.object(module,'_job',side_effect=self.fake_job);self.job.start();self.addCleanup(self.job.stop)
        self.ordinary=patch.object(module,'_ordinary',side_effect=self.proof);self.ordinary.start();self.addCleanup(self.ordinary.stop)
        self.fault=patch.object(module,'_fault',side_effect=self.proof);self.fault.start();self.addCleanup(self.fault.stop)
        self.spawn=patch.object(module.subprocess,'Popen',side_effect=AssertionError('native subprocess forbidden'))
        self.spawn.start();self.addCleanup(self.spawn.stop)
        self.signal=patch.object(module.signal,'pidfd_send_signal',side_effect=AssertionError('signal forbidden'),create=True)
        self.signal.start();self.addCleanup(self.signal.stop)

    def check_request(self,backend,case,request):
        module.require(backend is self.backend and request==self.requests[case]);return digest(canonical(request))

    def ack_state(self,case):return {'phase':'source_ack_written','case':case,'native_receipt_sha256':None}

    def state(self,case):
        value=self.ack_state(case)
        if case=='controller_loss':value.update(phase='complete',native_receipt_sha256=digest(canonical({'fixture':case})))
        return value

    def fake_job(self,backend,case,request):
        path=self.path/('execution-'+request['operation_id'])
        if not path.exists():
            path.mkdir(mode=0o700)
            with PrivateDirectory(path) as private:private.create('state.json',canonical(self.state(case)))
        return PrivateDirectory(path),{'synthetic':True},decode((path/'state.json').read_bytes())

    def fake_native(self,backend,case,request):
        value={'fixture':case};return canonical(value),value

    def proof(self,backend,case):
        self.calls.append(case)
        proof=module._base_proof(backend,case,copy.deepcopy(self.requests[case]))
        if case in module.FAULT_CASES:
            observation={'kind':'independent_fault','relationships_verified':True,'pidfds_acquired_before_ack':True,
                'terminal':{name:True for name in module.NODES},'driver_reaped':True,'cleanup_method':'passive',
                'numeric_pid_signalled':False,'source_ack_sha256':digest(canonical(self.ack_state(case))),
                'driver_outcome':'self_sigkill' if case=='controller_loss' else 'supervisor_loss_confirmed'}
            if case=='supervisor_loss':observation['native_outcome']='unknown'
            private,_,_=self.fake_job(backend,case,proof['request']);private.close()
        else:observation={'kind':'native','replay_refused':case=='replay_and_corruption','corruption_refused':case=='replay_and_corruption'}
        proof['observation']=observation
        if case!='supervisor_loss':proof['native_receipt_sha256']=digest(self.fake_native(backend,case,proof['request'])[0])
        return proof

    def qualify(self):
        evidence=module.qualify_execution_backend(self.backend)
        return {'schema':module.QUALIFICATION_SCHEMA,'plan_sha256':self.backend.document['plan_hash'],
            'environment_sha256':digest(canonical(self.backend.environment)),**evidence}

    def test_one_fresh_matrix_requires_all_nine_cases_and_persists_each_exact_proof(self):
        receipt=self.qualify()
        self.assertEqual(self.calls,list(module.CASES));self.assertEqual(set(receipt['checks']),set(module.CASES))
        self.assertTrue(module.validate_qualification_evidence(self.backend,receipt))
        self.backend._authorize.assert_not_called()
        for case in module.CASES:
            raw=(self.path/'qualification-matrix'/(case+'.json')).read_bytes()
            self.assertEqual(digest(raw),receipt['native_receipts'][case])
        with self.assertRaises(FileExistsError):self.qualify()
        self.assertEqual(self.calls,list(module.CASES))

    def test_partial_matrix_is_never_retried_or_adopted(self):
        original=self.proof
        def stop(backend,case):
            if case=='wall_timeout':raise DevelopmentError('synthetic fault')
            return original(backend,case)
        with patch.object(module,'_ordinary',side_effect=stop):
            with self.assertRaises(DevelopmentError):self.qualify()
        directory=self.path/'qualification-matrix'
        self.assertTrue((directory/'wall_timeout-intent.json').exists());self.assertFalse((directory/'wall_timeout.json').exists())
        before={path.name:path.read_bytes() for path in directory.iterdir()}
        with self.assertRaises(FileExistsError):self.qualify()
        self.assertEqual(before,{path.name:path.read_bytes() for path in directory.iterdir()})

    def test_validation_never_spawns_signals_or_calls_authority_even_after_revocation(self):
        receipt=self.qualify();self.calls.clear()
        with (patch.object(module,'_ordinary',side_effect=AssertionError('new job')),
              patch.object(module,'_fault',side_effect=AssertionError('new job'))):
            self.assertTrue(module.validate_qualification_evidence(self.backend,receipt))
        self.assertEqual(self.calls,[]);self.backend._authorize.assert_not_called()

    def test_missing_foreign_extra_corrupted_or_boolean_proof_refuses(self):
        receipt=self.qualify();directory=self.path/'qualification-matrix';target=directory/'normal.json';saved=target.read_bytes()
        variants=[lambda p:p.update(cleanup_confirmed=1),lambda p:p.update(request_sha256='0'*64),
            lambda p:p.update(environment_sha256='f'*64),lambda p:p.update(extra='unknown')]
        for change in variants:
            with self.subTest(change=change):
                value=decode(saved);change(value);raw=canonical(value);target.write_bytes(raw)
                altered=copy.deepcopy(receipt);altered['native_receipts']['normal']=digest(raw)
                self.assertFalse(module.validate_qualification_evidence(self.backend,altered))
        target.write_bytes(saved);extra=directory/'unknown';extra.write_text('synthetic unknown')
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt));self.assertTrue(extra.exists());extra.unlink()
        target.unlink();self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))

    def test_all_nine_strict_true_receipts_required_and_wrong_backend_refused(self):
        receipt=self.qualify()
        for kind in ('missing','false','integer','bad_hash','wrong_plan','cleanup'):
            value=copy.deepcopy(receipt)
            if kind=='missing':value['checks'].pop('supervisor_loss')
            elif kind=='false':value['checks']['normal']=False
            elif kind=='integer':value['checks']['normal']=1
            elif kind=='bad_hash':value['native_receipts']['normal']='z'*64
            elif kind=='wrong_plan':value['plan_sha256']='b'*64
            else:value['cleanup_confirmed']=1
            self.assertFalse(module.validate_qualification_evidence(self.backend,value))
        self.assertFalse(module.validate_qualification_evidence(SimpleNamespace(),receipt))

    def test_supervisor_loss_stays_unknown_without_native_receipt(self):
        receipt=self.qualify();case='supervisor_loss';path=self.path/('execution-'+self.requests[case]['operation_id'])
        (path/'native-receipt.json').write_text('synthetic unexpected native')
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))
        (path/'native-receipt.json').unlink();(path/'state.json').write_text('{}')
        # The job fixture reports the retained state; current bytes must match.
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))

    def test_intent_origin_mismatch_and_symlink_proof_are_not_adopted(self):
        receipt=self.qualify();directory=self.path/'qualification-matrix';marker=directory/'normal-intent.json'
        value=decode(marker.read_bytes());value['origin']='00000000-0000-0000-0000-000000000000';marker.write_bytes(canonical(value))
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))
        target=directory/'normal.json';saved=self.root/'held-proof';target.rename(saved);target.symlink_to(saved)
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt));self.assertTrue(saved.exists())

    def test_copied_matrix_and_mutated_physical_origin_cannot_reuse_receipts(self):
        receipt=self.qualify();matrix=self.path/'qualification-matrix';held=self.path/'held-matrix'
        matrix.rename(held);shutil.copytree(held,matrix)
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))
        marker=matrix/'intent.json';value=decode(marker.read_bytes())
        value['matrix_identity']=list(module.identity(matrix.stat()));marker.write_bytes(canonical(value))
        # Rewriting the intent does not rewrite each proof's retained origin.
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))

    def test_current_source_or_environment_pin_failure_prevents_validation(self):
        receipt=self.qualify();self.backend._pins.side_effect=DevelopmentError('synthetic changed pin')
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))
        self.backend._authorize.assert_not_called()

    def test_controller_loss_ack_hash_must_match_reconstructed_terminal_state(self):
        receipt=self.qualify();target=self.path/'qualification-matrix'/'controller_loss.json'
        value=decode(target.read_bytes());value['observation']['source_ack_sha256']='f'*64
        raw=canonical(value);target.write_bytes(raw)
        # Even if the outer file hash is recomputed, the changed ACK cannot be
        # accepted against the retained job/native terminal chain.
        receipt['native_receipts']['controller_loss']=digest(raw)
        self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))

    def test_controller_loss_terminal_native_hash_or_boundary_change_refuses(self):
        receipt=self.qualify();case='controller_loss';path=self.path/('execution-'+self.requests[case]['operation_id'])/'state.json'
        original=path.read_bytes()
        for key,value in (('native_receipt_sha256','0'*64),('source_challenge','e'*32)):
            state=decode(original);state[key]=value;path.write_bytes(canonical(state))
            self.assertFalse(module.validate_qualification_evidence(self.backend,receipt))
        path.write_bytes(original);self.assertTrue(module.validate_qualification_evidence(self.backend,receipt))


class KernelCodecTests(unittest.TestCase):
    def test_status_requires_real_shape_unique_numeric_fields_and_expected_namespaces(self):
        raw=b'Name:\tfixed\nPid:\t123\nPPid:\t100\nNSpid:\t123 1\nUid:\t1000 1000 1000 1000\nGid:\t1000 1000 1000 1000\n'
        self.assertEqual(module._parse_status(raw)['NSpid'],[123,1])
        for invalid in (raw+b'Pid:\t456\n',raw.replace(b'123 1',b'123 x'),raw[:-1],b'x'*32769):
            with self.assertRaises(DevelopmentError):module._parse_status(invalid)

    def test_stat_starttime_uses_kernel_field_22_after_last_parenthesis(self):
        data=b'123 (name with ) paren) S '+b' '.join([b'0']*18+[b'7654321',b'0'])+b'\n'
        self.assertEqual(module._parse_start(data),7654321)
        for invalid in (b'123 invalid\n',data.replace(b'7654321',b'x'),b'x'*4097):
            with self.assertRaises(DevelopmentError):module._parse_start(invalid)

    def test_unknown_or_partial_fault_observation_is_never_success(self):
        base={'kind':'independent_fault','relationships_verified':True,'pidfds_acquired_before_ack':True,
            'terminal':{name:True for name in module.NODES},'driver_reaped':True,'cleanup_method':'passive',
            'numeric_pid_signalled':False,'source_ack_sha256':'a'*64,'driver_outcome':'self_sigkill'}
        module._validate_observation('controller_loss',base)
        for mutate in (lambda x:x.update(pidfds_acquired_before_ack=False),lambda x:x.update(numeric_pid_signalled=True),
                       lambda x:x['terminal'].update(init=False),lambda x:x.update(driver_reaped=1),
                       lambda x:x.update(cleanup_method='global_kill'),lambda x:x.update(extra='ignored')):
            value=copy.deepcopy(base);mutate(value)
            with self.assertRaises(DevelopmentError):module._validate_observation('controller_loss',value)

    def test_policy_fails_closed_on_mac_without_kernel_probes_or_native_jobs(self):
        with patch.object(module.sys,'platform','darwin'),patch.object(module.os,'pidfd_open',create=True) as pidfd:
            with self.assertRaises(DevelopmentError):module._real_host(SimpleNamespace())
        pidfd.assert_not_called()


class ObserverTests(unittest.TestCase):
    def node(self,pid,children,namespace=(1,2),terminal=False):
        return SimpleNamespace(pid=pid,namespace=namespace,children=Mock(return_value=children),
            verify=Mock(),terminal=Mock(return_value=terminal),kill_if_live=Mock(),close=Mock())

    def observer(self,nodes):
        obj=module._FaultObserver.__new__(module._FaultObserver);obj.proc=99;obj.nodes=nodes;obj.acquired=False
        return obj

    def test_acquisition_demands_exact_live_four_node_chain_before_ack(self):
        nodes=[self.node(10,[20]),self.node(20,[30]),self.node(30,[40]),self.node(40,[],(3,4))]
        observer=self.observer(nodes[:1])
        with patch.object(module,'_LiveProcess',side_effect=nodes[1:]) as acquire:
            self.assertTrue(observer.acquire(20))
        self.assertTrue(observer.acquired);self.assertEqual(acquire.call_count,3)
        self.assertTrue(all(node.verify.called for node in nodes));self.assertFalse(any(node.kill_if_live.called for node in nodes))

    def test_reported_pid_alone_never_acquires_or_signals_anyone(self):
        driver=self.node(10,[99]);observer=self.observer([driver])
        with patch.object(module,'_LiveProcess',side_effect=AssertionError('must not acquire unknown child')):
            with self.assertRaises(DevelopmentError):observer.acquire(20)
        driver.kill_if_live.assert_not_called()

    def test_missing_namespace_or_unknown_sibling_refuses(self):
        for sibling in (False,True):
            nodes=[self.node(10,[20]),self.node(20,[30,31] if sibling else [30]),
                   self.node(30,[40]),self.node(40,[])]
            observer=self.observer(nodes[:1])
            with patch.object(module,'_LiveProcess',side_effect=nodes[1:]):
                with self.assertRaises(DevelopmentError):observer.acquire(20)
            self.assertFalse(observer.acquired);self.assertFalse(any(node.kill_if_live.called for node in nodes))

    def test_finish_is_passive_for_terminal_pidfds_and_reaps_only_owned_driver(self):
        nodes=[self.node(10+i,[],terminal=True) for i in range(4)]
        observer=self.observer(nodes);observer.acquired=True;driver=SimpleNamespace(wait=Mock())
        value=observer.finish(driver)
        self.assertEqual(value['cleanup_method'],'passive');self.assertTrue(value['driver_reaped'])
        driver.wait.assert_called_once();self.assertFalse(any(node.kill_if_live.called for node in nodes))


class FaultFlowTests(unittest.TestCase):
    """Drive actual fault orchestration using only fake process/socket objects."""
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve();self.root.chmod(0o700)
        self.backend=SimpleNamespace(private=SimpleNamespace(path=self.root),document={'plan_hash':'a'*64},
            environment={'synthetic':True},manifest={'synthetic':True},
            plan={'interpreter':'/synthetic/python','sources':{'package':{'path':'/synthetic/package','identity':[1,2],'chain':[]},
                'files':{'linux_execution_qualification.py':{'sha256':'b'*64}}}})
        self.case='controller_loss';self.request={'operation_id':'c'*64};self.state={'phase':'source_ack_written'}
        self.job_path=self.root/'job';self.job_path.mkdir(mode=0o700)
        with PrivateDirectory(self.job_path) as private:private.create('state.json',canonical(self.state))
        self.backend._job_directory=lambda _:self.job_path
        self.parent=SimpleNamespace(close=Mock(),send=Mock(),fileno=lambda:91)
        self.child=SimpleNamespace(close=Mock(),fileno=lambda:92)
        self.driver=SimpleNamespace(pid=123,returncode=-9,poll=Mock(return_value=None),wait=Mock(),kill=Mock())
        observation={'relationships_verified':True,'pidfds_acquired_before_ack':True,'terminal':{name:True for name in module.NODES},
            'driver_reaped':True,'cleanup_method':'passive','numeric_pid_signalled':False}
        self.observer=SimpleNamespace(acquire=Mock(return_value=True),finish=Mock(return_value=observation),
            emergency_cleanup=Mock(),close=Mock())
        self.base={'fixture':'pure fake proof','native_receipt_sha256':None}
        self.nonce='d'*32
        from contextlib import ExitStack
        self.stack=ExitStack();self.addCleanup(self.stack.close)
        self.packet_count=0
        def synthetic_packet(name,data):
            self.packet_count+=1
            self.packet_fd=os.open(self.root/('packet-'+str(self.packet_count)),os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600)
            os.write(self.packet_fd,data);os.lseek(self.packet_fd,0,os.SEEK_SET);return self.packet_fd
        namespace=SimpleNamespace(sealed_memfd=Mock(side_effect=synthetic_packet))
        self.stack.enter_context(patch.dict(sys.modules,{'blackoutburst_agents.linux_test_namespace':namespace}))
        self.stack.enter_context(patch.object(module.socket,'socketpair',return_value=(self.parent,self.child)))
        self.popen=self.stack.enter_context(patch.object(module.subprocess,'Popen',return_value=self.driver))
        self.stack.enter_context(patch.object(module,'_FaultObserver',return_value=self.observer))
        self.stack.enter_context(patch.object(module,'_base_proof',side_effect=lambda *_:copy.deepcopy(self.base)))
        self.stack.enter_context(patch.object(module,'_job',side_effect=lambda *_:(PrivateDirectory(self.job_path),{},copy.deepcopy(self.state))))
        self.stack.enter_context(patch.object(module,'_native',return_value=(b'{}',{})))
        self.stack.enter_context(patch.object(module,'_expected_native',return_value=True))
        self.stack.enter_context(patch.object(module.uuid,'uuid4',return_value=SimpleNamespace(hex=self.nonce)))
        self.send=self.stack.enter_context(patch.object(module,'_send'))
        self.receive=self.stack.enter_context(patch.object(module,'_receive',side_effect=self.message))

    def message(self,*_):
        return {'schema':module.MATRIX_SCHEMA,'kind':'fault_ready','case':self.case,'nonce':self.nonce,
                'supervisor_pid':456,'state':copy.deepcopy(self.state),'request':copy.deepcopy(self.request)}

    def test_controller_loss_requires_positive_kernel_observer_before_ack(self):
        value=module._fault(self.backend,'controller_loss')
        self.observer.acquire.assert_called_once_with(456)
        self.assertEqual(self.send.call_args.args[1]['kind'],'fault_ack')
        self.assertEqual(value['observation']['driver_outcome'],'self_sigkill')
        self.assertEqual(value['native_receipt_sha256'],digest(b'{}'))
        self.observer.emergency_cleanup.assert_not_called();self.driver.kill.assert_not_called()
        command=self.popen.call_args.args[0]
        self.assertEqual(command[:5],['/synthetic/python','-I','-B','-S','-c'])
        self.assertNotIn('HOME',self.popen.call_args.kwargs['env'])
        self.assertEqual(self.popen.call_args.kwargs['pass_fds'],(self.packet_fd,92))

    def test_observer_refusal_never_sends_ack_or_reports_success(self):
        self.observer.acquire.return_value=False
        with self.assertRaises(DevelopmentError):module._fault(self.backend,'controller_loss')
        self.send.assert_not_called();self.observer.finish.assert_not_called()
        self.observer.emergency_cleanup.assert_called_once_with(self.driver)

    def test_parent_fallback_sigkill_cannot_masquerade_as_controller_self_loss(self):
        self.driver.wait.side_effect=module.subprocess.TimeoutExpired('synthetic',2)
        with self.assertRaises(DevelopmentError) as caught:module._fault(self.backend,'controller_loss')
        self.assertEqual(caught.exception.code,'linux_qualification_fault_not_observed')
        self.observer.finish.assert_not_called();self.observer.emergency_cleanup.assert_called_once()

    def test_wrong_nonce_or_nonexact_state_refuses_before_observer_ack(self):
        for wrong in ('nonce','state'):
            message=self.message()
            if wrong=='nonce':message['nonce']='e'*32
            else:message['state']={'phase':'wrong'}
            self.receive.side_effect=None;self.receive.return_value=message
            with self.assertRaises(DevelopmentError):module._fault(self.backend,'controller_loss')
        self.send.assert_not_called();self.observer.acquire.assert_not_called()

    def test_supervisor_loss_requires_driver_confirmation_and_no_native_outcome(self):
        self.case='supervisor_loss';self.driver.returncode=0
        ready=self.message();confirmed={'schema':module.MATRIX_SCHEMA,'kind':'fault_observed','case':self.case,
            'nonce':self.nonce,'reason':'linux_supervisor_lost'}
        self.receive.side_effect=[ready,confirmed]
        value=module._fault(self.backend,self.case)
        self.assertIsNone(value['native_receipt_sha256']);self.assertEqual(value['observation']['native_outcome'],'unknown')
        self.observer.finish.assert_called_once();self.driver.kill.assert_not_called()

    def test_supervisor_loss_with_unexpected_native_receipt_stays_refused(self):
        self.case='supervisor_loss';self.driver.returncode=0
        self.receive.side_effect=[self.message(),{'schema':module.MATRIX_SCHEMA,'kind':'fault_observed',
            'case':self.case,'nonce':self.nonce,'reason':'linux_supervisor_lost'}]
        (self.job_path/'native-receipt.json').write_text('unexpected synthetic file')
        with self.assertRaises(DevelopmentError):module._fault(self.backend,self.case)
        self.assertTrue((self.job_path/'native-receipt.json').exists())


class DriverBootstrapTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup);self.root=Path(temporary.name).resolve();self.package=self.root/'package';self.package.mkdir(mode=0o700)
        self.source=self.package/'__init__.py';self.source.write_bytes(b'# synthetic bytes, never compiled or executed\n')
        from blackoutburst_agents.linux_input_snapshot import _chain
        chain=_chain(str(self.package))
        self.spec={'package':{'path':str(self.package),'identity':chain[-1]['identity'],'chain':chain},
                   'files':{'__init__.py':digest(self.source.read_bytes())}}
        self.prefix=module.DRIVER_BOOTSTRAP.split('class Held(')[0]

    def hold(self):
        namespace={}
        with patch.object(sys,'argv',['fixed-driver',canonical(self.spec).decode(),'not-used-in-prefix']):
            exec(compile(self.prefix,'<fixed-qualification-bootstrap-prefix>','exec'),namespace)
        return namespace['held']

    def test_prefix_only_holds_hash_pinned_public_bytes_and_never_imports_them(self):
        held=self.hold();self.assertEqual(held['__init__.py'][1],self.source.read_bytes())
        self.assertEqual(set(held),{'__init__.py'})

    def test_symlink_ancestor_or_wrong_source_hash_refuses_before_import(self):
        self.spec['files']['__init__.py']='0'*64
        with self.assertRaises(SystemExit):self.hold()
        self.spec['files']['__init__.py']=digest(self.source.read_bytes())
        original=self.root/'original';self.package.rename(original);self.package.symlink_to(original)
        with self.assertRaises(OSError):self.hold()

    def test_parent_substitution_keeps_reads_on_owned_fd_then_refuses_linkage(self):
        original_open=os.open;changed=False;held=self.root/'held-original'
        def replace(name,flags,*args,**kwargs):
            nonlocal changed
            if name=='__init__.py' and kwargs.get('dir_fd') is not None and not changed:
                changed=True;self.package.rename(held);self.package.mkdir(mode=0o700)
                (self.package/'__init__.py').write_bytes(b'unknown synthetic replacement; must not be read')
            return original_open(name,flags,*args,**kwargs)
        with patch.object(os,'open',side_effect=replace):
            with self.assertRaises(SystemExit):self.hold()
        self.assertTrue(changed);self.assertEqual((held/'__init__.py').read_bytes(),b'# synthetic bytes, never compiled or executed\n')
        self.assertEqual((self.package/'__init__.py').read_bytes(),b'unknown synthetic replacement; must not be read')
