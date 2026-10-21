"""Composed validators over synthetic durable observations; never qualification."""
import copy
import os
import shutil
import unittest
import uuid
from unittest.mock import Mock
from unittest.mock import patch
from contextlib import contextmanager
from types import SimpleNamespace

from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import canonical,digest
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.linux_test_backend import (KIND,QUALIFICATION_SCRIPTS,
    SupervisedLinuxNamedPythonBackend)
from blackoutburst_agents import linux_execution_qualification as evidence
import test_linux_connected_execution as fixture


class EvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        f=fixture.ConnectedExecutionTests();f.setUp();self.addCleanup(f.doCleanups)
        self.f=f;f.run_test();self.backend=f.backend
        del self.backend.qualified_for  # Exercise the real composed validator.
        self.backend.authorize_scope=Mock(side_effect=AssertionError('no authorization during evidence lookup'))
        path=f.base/'backend';path.mkdir(mode=0o700)
        self.backend.private=PrivateDirectory(path);self.addCleanup(self.backend.private.close)
        matrix=path/'qualification-matrix';matrix.mkdir(mode=0o700)
        self.matrix=PrivateDirectory(matrix);self.addCleanup(self.matrix.close)
        origin=str(uuid.uuid4())
        self.matrix.create('intent.json',canonical({'schema':evidence.MATRIX_SCHEMA,'origin':origin,
            'plan_sha256':f.plan_hash,'environment_sha256':digest(canonical(self.backend.environment)),
            'cases':list(evidence.CASES),'backend_directory':str(path),
            'backend_identity':list(self.backend.private.identity),'matrix_identity':list(self.matrix.identity)}))
        self.requests={};hashes={}
        for case in evidence.CASES:
            request=copy.deepcopy(next(iter(f.state()['linux_execution'].values()))['request'])
            request.update(operation_id=digest(uuid.uuid4().bytes),workflow_id=str(uuid.uuid4()),
                name='qualification-'+case,timeout_seconds=1 if case=='wall_timeout' else 5,
                max_output_bytes=4096,fence_sha256=digest(b'qualification-no-broker'))
            self.requests[case]=request
            directory=self.backend._job_directory(request);directory.mkdir(mode=0o700)
            nonce=uuid.uuid4().hex;challenge=uuid.uuid4().hex
            first={'schema':1,'nonce':nonce,'sequence':1,'started_at':10.0,'validated_at':10.1,'deadline':11.5}
            window={**first,'sequence':3,'started_at':10.2,'validated_at':10.3,'deadline':11.7}
            job={'schema':KIND,'nonce':nonce,'request':request,'backend_directory':str(path),
                'plan_sha256':f.plan_hash,'script_sha256':digest(QUALIFICATION_SCRIPTS[case]),
                'timeout_seconds':request['timeout_seconds'],'max_output_bytes':4096,'control':first,
                'qualification':True,'qualification_case':case}
            ack={'schema':1,'nonce':nonce,'request_sha256':digest(canonical(request)),
                'phase':'source_ack_written','boundary_sequence':2,'source_challenge':challenge,
                'native_receipt_sha256':None}
            with PrivateDirectory(directory) as private:
                private.create('job.json',canonical(job))
                if case!='supervisor_loss':
                    native=f.native(request);native['nonce']=nonce
                    native['reason']={'normal':'completed','assertion_failed':'script_failed',
                        'output_limit':'linux_output_limit','wall_timeout':'linux_script_wall_expired',
                        'source_revoked':'linux_authorization_expired','eof_running':'linux_authorization_expired',
                        'controller_loss':'linux_authorization_expired','replay_and_corruption':'completed'}[case]
                    if case=='assertion_failed':native['exit_code']=1
                    elif case not in ('normal','replay_and_corruption'):native['exit_code']=None
                    if case=='source_revoked':native.update(source_released=False,source_release_intent=False)
                    if case=='output_limit':native.update(output_bytes=4096,output_sha256=digest(b'x'*4096))
                    raw=canonical(native);native_sha=digest(raw)
                    private.create('native-receipt.json',raw)
                    private.create('state.json',canonical({**ack,'phase':'complete','native_receipt_sha256':native_sha}))
                    if native['source_release_intent']:
                        private.create('source-admission.json',canonical({'nonce':nonce,
                            'request_sha256':digest(canonical(request)),'identity':native['identity'],
                            'control':window,'phase':'source_release_intent'}))
                        private.create('source-window.json',canonical({'nonce':nonce,'challenge':challenge,
                            'request_sha256':digest(canonical(request)),'window_sha256':digest(canonical(window))}))
                        private.create('source-window.ready',b'')
                else:
                    native_sha=None;private.create('state.json',canonical(ack))
            proof=evidence._base_proof(self.backend,case,request)
            proof['native_receipt_sha256']=native_sha
            if case not in evidence.FAULT_CASES:
                proof['observation']={'kind':'native','replay_refused':case=='replay_and_corruption',
                                      'corruption_refused':case=='replay_and_corruption'}
            else:
                proof['observation']={'kind':'independent_fault','relationships_verified':True,
                    'pidfds_acquired_before_ack':True,'terminal':{name:True for name in evidence.NODES},
                    'driver_reaped':True,'cleanup_method':'passive','numeric_pid_signalled':False,
                    'source_ack_sha256':digest(canonical(ack)),
                    'driver_outcome':'self_sigkill' if case=='controller_loss' else 'supervisor_loss_confirmed'}
                if case=='supervisor_loss':proof['observation']['native_outcome']='unknown'
            raw=canonical(proof);hashes[case]=digest(raw)
            self.matrix.create(case+'-intent.json',canonical({'schema':evidence.MATRIX_SCHEMA,'case':case,'origin':origin}))
            self.matrix.create(case+'.json',raw)
        self.receipt={'schema':evidence.QUALIFICATION_SCHEMA,'plan_sha256':f.plan_hash,
            'environment_sha256':digest(canonical(self.backend.environment)),
            'checks':{case:True for case in evidence.CASES},'native_receipts':hashes,'cleanup_confirmed':True}
        self.backend.private.create('qualification.json',canonical(self.receipt))

    def valid(self):
        return SupervisedLinuxNamedPythonBackend.qualified_for(self.backend,self.f.broker.journal.manifest_hash,
                                                               self.f.broker.manifest['tests'][0])

    def test_real_composed_private_job_native_state_and_matrix_validation(self):
        self.assertTrue(self.valid())
        self.backend.authorize_scope.assert_not_called()

    def test_rehashed_changed_controller_ack_and_changed_native_observation_refuse(self):
        self.assertTrue(self.valid())
        raw=self.matrix.read('controller_loss.json',evidence.MAX_PROOF);proof=decode(raw)
        proof['observation']['source_ack_sha256']='0'*64;raw=canonical(proof)
        self.matrix.replace('controller_loss.json',raw)
        receipt=copy.deepcopy(self.receipt);receipt['native_receipts']['controller_loss']=digest(raw)
        self.backend.private.replace('qualification.json',canonical(receipt))
        self.assertFalse(self.valid())

    def test_copied_ledger_cannot_be_adopted_from_another_backend_directory(self):
        target=self.f.base/'copied';shutil.copytree(self.backend.private.path,target)
        original=self.backend.private
        with PrivateDirectory(target) as private:
            self.backend.private=private
            try:self.assertFalse(self.valid())
            finally:self.backend.private=original

    def test_missing_source_ack_proof_refuses_without_repair_or_dispatch(self):
        directory=self.backend._job_directory(self.requests['normal'])
        with PrivateDirectory(directory) as private:
            os.unlink('source-admission.json',dir_fd=private.fd)
        self.assertFalse(self.valid());self.assertFalse((directory/'source-admission.json').exists())

    def test_null_source_challenge_is_never_an_admitted_source_boundary(self):
        directory=self.backend._job_directory(self.requests['normal'])
        with PrivateDirectory(directory) as private:
            state=decode(private.read('state.json',8192));state['source_challenge']=None
            private.replace('state.json',canonical(state))
            approval=decode(private.read('source-window.json',2048));approval['challenge']=None
            private.replace('source-window.json',canonical(approval))
        self.assertFalse(self.valid())

    def test_requalification_never_erases_positive_or_partial_matrix(self):
        backend=self.backend;backend._busy=False
        backend._lock_fd=os.open('execution.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600,dir_fd=backend.private.fd)
        self.addCleanup(os.close,backend._lock_fd)
        info=os.fstat(backend._lock_fd);backend._lock_identity=(info.st_dev,info.st_ino)
        before=backend.private.read('qualification.json',128*1024)
        with patch('blackoutburst_agents.linux_execution_qualification.qualify_execution_backend',
                   side_effect=AssertionError('second qualification')):
            with self.assertRaises(DevelopmentError):backend.qualify()
            self.assertEqual(backend.private.read('qualification.json',128*1024),before)
            backend.private.replace('qualification.json',canonical(None))
            with self.assertRaises(DevelopmentError):backend.qualify()
            self.assertIsNone(decode(backend.private.read('qualification.json',128*1024)))


class EvidenceDirectoryBoundTests(unittest.TestCase):
    def test_many_unknown_names_stop_at_one_sentinel_and_never_materialize_all(self):
        consumed=[]
        @contextmanager
        def scan(_):
            def names():
                for number in range(1000000):
                    consumed.append(number);yield SimpleNamespace(name=str(number))
            yield names()
        with patch('blackoutburst_agents.linux_execution_qualification.os.scandir',side_effect=scan):
            with self.assertRaises(DevelopmentError):evidence._bounded_names(7,19)
        self.assertEqual(len(consumed),20)
