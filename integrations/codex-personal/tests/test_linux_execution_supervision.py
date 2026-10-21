"""Controller protocol tests with fake children; no process, signal or namespace."""
from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_linux_connected_execution as fixture
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.linux_test_backend import (_QualificationContext, read_native_observation,
                                                   validate_job_shape)
from blackoutburst_agents.linux_test_namespace import input_contents
from blackoutburst_agents.linux_input_snapshot import _chain
from blackoutburst_agents.linux_test_supervisor import BOOTSTRAP


class Pipe:
    def __init__(self,close_callback=None):
        self.read,self.write=os.pipe();self.closed=False;self.close_callback=close_callback
    def fileno(self):return self.write
    def close(self):
        if not self.closed:
            self.closed=True;os.close(self.read);os.close(self.write)
            if self.close_callback:self.close_callback()


class Helper:
    def __init__(self,close_callback=None):self.stdin=Pipe(close_callback);self.exited=False
    def poll(self):return 0 if self.exited else None
    def wait(self,timeout):self.exited=True;return 0


class LinuxSupervisionTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ConnectedExecutionTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups);self.fixture.run_test()
        self.request=next(iter(self.fixture.state()['linux_execution'].values()))['request']
        self.backend=self.fixture.backend
        path=self.fixture.base/'backend';path.mkdir(mode=0o700)
        self.backend.private=PrivateDirectory(path);self.addCleanup(self.backend.private.close)
        self.directory=path/('execution-'+self.request['operation_id'])
        self.context=SimpleNamespace(authorize=Mock(return_value=True),before_source=Mock(return_value=True))
        self.clock=100.0;self.sequence=[];self.helper=None
        stack=self.enterContext(ExitStack())
        stack.enter_context(patch('blackoutburst_agents.linux_test_supervisor.helper_command',return_value=['not-executed']))
        stack.enter_context(patch('blackoutburst_agents.linux_test_backend._control_clock',side_effect=lambda:self.clock))
        stack.enter_context(patch('blackoutburst_agents.dev_test_supervisor._control_clock',side_effect=lambda:self.clock))
        stack.enter_context(patch('blackoutburst_agents.linux_test_backend.time.sleep',return_value=None))

    def refuse(self,call):
        with self.assertRaises(DevelopmentError):call()

    def header(self):
        with PrivateDirectory(self.directory) as private:
            return decode(private.read('job.json',1024*1024)),decode(private.read('state.json',8192))

    def publish(self,source=True):
        job,state=self.header();native=self.fixture.native(self.request);native['nonce']=job['nonce']
        if not source:
            native.update(source_released=False,source_release_intent=False,launcher_attempted=False,
                          exit_code=None,reason='linux_authorization_expired',checks={},identity=None)
            native['cleanup'].update(init_identity_received=False,init_terminal=False,launcher_reaped=False)
        with PrivateDirectory(self.directory) as private:
            if source:
                window=self.sequence[-1]
                private.create('source-admission.json',canonical({'nonce':job['nonce'],
                    'request_sha256':digest(canonical(self.request)),'identity':native['identity'],
                    'control':window,'phase':'source_release_intent'}))
            private.create('native-receipt.json',canonical(native))
            state.update(phase='complete',native_receipt_sha256=digest(canonical(native)))
            private.replace('state.json',canonical(state))
        self.helper.exited=True

    def launch(self,*args,**kw):
        job,state=self.header()
        self.assertEqual(state['phase'],'helper_launch_intent')
        validate_job_shape(job,self.directory)
        self.helper=Helper();self.addCleanup(self.helper.stdin.close)
        state.update(phase='source_boundary_requested',boundary_sequence=1,source_challenge='b'*32)
        with PrivateDirectory(self.directory) as private:private.replace('state.json',canonical(state))
        return self.helper

    def send(self,descriptor,window,**kw):
        self.sequence.append(window)
        if len(self.sequence)==2:
            with PrivateDirectory(self.directory) as private:
                approval=decode(private.read('source-window.json',2048))
                self.assertEqual(private.read('source-window.ready',1),b'')
                self.assertEqual(approval['challenge'],'b'*32)
                self.assertEqual(approval['window_sha256'],digest(canonical(window)))
            self.publish()

    def run_job(self):
        return self.backend._run_job(self.request,b'fixed packet',b'pass\n',self.context,qualification=False)

    def test_parent_boundary_and_challenge_are_durable_before_source_window(self):
        with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=self.launch) as spawn, \
             patch('blackoutburst_agents.linux_test_backend._send_control_window',side_effect=self.send):
            native=self.run_job()
        self.assertEqual(len(self.sequence),2);self.context.before_source.assert_called_once()
        self.assertFalse(native['broker_snapshot_admitted']);spawn.assert_called_once()
        self.assertTrue(self.helper.stdin.closed)
        with PrivateDirectory(self.directory) as private:
            job,state=self.header()
            approval=decode(private.read('source-window.json',2048));approval['challenge']='c'*32
            private.replace('source-window.json',canonical(approval))
            self.refuse(lambda:read_native_observation(private,job,self.backend.plan))

    def test_expired_capture_does_not_send_or_write_a_source_approval(self):
        def before_source():self.clock+=1.5;return True
        self.context.before_source.side_effect=before_source
        def launch(*args,**kw):
            child=self.launch(*args,**kw);child.stdin.close_callback=lambda:self.publish(False)
            return child
        with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=launch), \
             patch('blackoutburst_agents.linux_test_backend._send_control_window',side_effect=self.send):
            native=self.run_job()
        self.assertFalse(native['source_released']);self.assertEqual(len(self.sequence),1)
        self.assertFalse((self.directory/'source-window.json').exists())

    def test_source_pin_checks_consume_the_original_authorization_window(self):
        pin_calls=[]
        def pins():
            pin_calls.append(self.clock)
            # Initial preflight, first frame and pre-Popen checks are quick;
            # only the pins collected at the SOURCE boundary take the TTL.
            if len(pin_calls)==4:self.clock+=1.5
        self.backend._pins=Mock(side_effect=pins)
        def launch(*args,**kw):
            child=self.launch(*args,**kw);child.stdin.close_callback=lambda:self.publish(False)
            return child
        with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=launch), \
             patch('blackoutburst_agents.linux_test_backend._send_control_window',side_effect=self.send):
            native=self.run_job()
        self.assertFalse(native['source_released']);self.assertEqual(len(self.sequence),1)
        self.assertFalse((self.directory/'source-window.json').exists())
        self.context.before_source.assert_called_once()

    def test_marker_publication_does_not_renew_the_original_source_deadline(self):
        from blackoutburst_agents.dev_test_supervisor import _send_control_window
        original=PrivateDirectory.create
        def create(private,name,data):
            result=original(private,name,data)
            if name=='source-window.ready':self.clock+=1.5
            return result
        def launch(*args,**kw):
            child=self.launch(*args,**kw);child.stdin.close_callback=lambda:self.publish(False)
            return child
        def send(descriptor,window,**kw):
            _send_control_window(descriptor,window,clock=lambda:self.clock,**kw)
            self.sequence.append(window)
        with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=launch), \
             patch('blackoutburst_agents.linux_test_backend._send_control_window',side_effect=send), \
             patch.object(PrivateDirectory,'create',create):
            native=self.run_job()
        self.assertFalse(native['source_released']);self.assertEqual(len(self.sequence),1)
        self.assertTrue((self.directory/'source-window.ready').exists())

    def test_ambiguous_popen_keeps_job_and_refuses_second_launch(self):
        with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=OSError('synthetic')) as spawn:
            with self.assertRaises(OSError):self.run_job()
            self.assertEqual(self.header()[1]['phase'],'helper_launch_intent')
            with self.assertRaises(FileExistsError):self.run_job()
            spawn.assert_called_once()

    def test_duplicate_helper_cannot_publish_cleanup_over_an_existing_producer(self):
        from blackoutburst_agents.linux_test_supervisor import supervise
        with patch('blackoutburst_agents.linux_test_backend.subprocess.Popen',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.run_job()
        job,state=self.header()
        with PrivateDirectory(self.directory) as private:
            private.create('supervisor.lock',b'')
            before=private.read('state.json',8192)
        owned=PrivateDirectory(self.directory)
        with patch('blackoutburst_agents.linux_test_backend.load_supervisor_job',return_value=
                   (owned,job,self.backend.plan,state)), \
             patch('blackoutburst_agents.linux_test_supervisor._ControlPipe',side_effect=AssertionError('producer not owned')):
            self.assertEqual(supervise(self.directory),78)
        with PrivateDirectory(self.directory) as private:
            self.assertEqual(private.read('state.json',8192),before)
            self.assertNotIn('native-receipt.json',os.listdir(private.fd))

    def test_loss_injection_cannot_signal_without_independent_ack(self):
        child=SimpleNamespace(poll=lambda:None,kill=Mock())
        for case in ('controller_loss','supervisor_loss'):
            context=_QualificationContext(self.backend,case,fault_authorizer=Mock(return_value=False))
            with patch('blackoutburst_agents.linux_test_backend.os.kill',side_effect=AssertionError('signal')):
                self.refuse(lambda:context.observe_controller_state(child,{'phase':'source_ack_written'},self.request))
            child.kill.assert_not_called()

    def test_inner_decoder_rejects_aliases_offsets_traversal_and_extra_content(self):
        def packet(entries,data=b'a'):
            raw=canonical({'schema':'blackoutburst-linux-test-inputs-v1','snapshot_sha256':'a'*64,'files':entries})
            return b'BB_LINUX_INPUT_V1\n'+len(raw).to_bytes(8,'big')+raw+data
        entry={'path':'one.txt','offset':0,'bytes':1,'sha256':digest(b'a')}
        self.assertEqual(input_contents(packet([entry]),'a'*64),{'one.txt':b'a'})
        for path in ('../escape','/escape','one.txt/../../escape'):
            self.refuse(lambda:input_contents(packet([{**entry,'path':path}]),'a'*64))
        for raw in (packet([{**entry,'offset':1}]),packet([entry],b'ab'),
                    packet([entry,{**entry,'path':'ONE.TXT','offset':1}],b'aa')):
            self.refuse(lambda:input_contents(raw,'a'*64))


class HeldSourceBootstrapTests(unittest.TestCase):
    """Only the loader prefix runs; held module bytes are never executed."""
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup);self.base=Path(temp.name).resolve()
        self.package=self.base/'package';self.package.mkdir(mode=0o700)
        self.raw=b'raise AssertionError("module must remain inert")\n'
        (self.package/'__init__.py').write_bytes(self.raw)
        chain=_chain(str(self.package))
        self.pins={'package':{'path':str(self.package),'identity':chain[-1]['identity'],'chain':chain},
                   'files':{'__init__.py':{'sha256':digest(self.raw),'bytes':len(self.raw)}}}
        self.prefix=BOOTSTRAP.split('class Held(')[0]

    def load(self,phase='supervise'):
        scope={}
        with patch.object(sys,'argv',['bootstrap',str(self.package),canonical(self.pins).decode(),phase,'unused']):
            exec(compile(self.prefix,'<held-bootstrap-prefix>','exec'),scope)
        return scope['sources']

    def test_exact_bytes_are_held_without_import_or_execution(self):
        self.assertEqual(self.load()['__init__.py'][1],self.raw)

    def test_replaced_ancestor_symlink_and_real_directory_refuse_before_file_read(self):
        for link in (True,False):
            with self.subTest(link=link):
                if not link:self.setUp()
                saved=self.base/'saved';self.package.rename(saved)
                foreign=self.base/'foreign';foreign.mkdir(mode=0o700)
                (foreign/'__init__.py').write_bytes(b'unapproved sentinel')
                if link:self.package.symlink_to(foreign,target_is_directory=True)
                else:foreign.rename(self.package)
                with patch('os.read',side_effect=AssertionError('foreign body read')):
                    with self.assertRaises((SystemExit,OSError)):self.load()

    def test_changed_module_hash_is_rejected_and_never_executed(self):
        (self.package/'__init__.py').write_bytes(b'x'*len(self.raw))
        with self.assertRaises(SystemExit):self.load()

    def test_inner_uid_translation_never_waives_inode_hash_or_pid1_guards(self):
        with self.assertRaises(SystemExit):self.load('inner')
        with patch('os.getpid',return_value=1),patch('os.geteuid',return_value=0),patch('os.getegid',return_value=0):
            self.assertEqual(self.load('inner')['__init__.py'][1],self.raw)
            self.pins['package']['chain'][-1]['identity'][1]+=1
            with self.assertRaises(SystemExit):self.load('inner')


class SourceWindowTests(unittest.TestCase):
    def test_namespace_rechecks_same_window_without_restarting_its_clock(self):
        from blackoutburst_agents.linux_test_namespace import validate_source_release
        nonce='a'*32;window={'schema':1,'nonce':nonce,'sequence':3,'started_at':100.0,
            'validated_at':100.1,'deadline':101.5}
        raw=canonical({'schema':'linux-source-release-v1','nonce':nonce,'control':window})
        self.assertEqual(validate_source_release(raw,nonce,_clock=lambda:101.49),window)
        for now in (100.09,101.5,101.7):
            with self.assertRaises(DevelopmentError):validate_source_release(raw,nonce,_clock=lambda:now)
        for invalid in (raw+b' ',b'x'*1025,canonical({'schema':'linux-source-release-v1','nonce':'b'*32,'control':window})):
            with self.assertRaises(DevelopmentError):validate_source_release(invalid,nonce,_clock=lambda:100.2)

    def test_later_fresh_heartbeat_cannot_extend_the_original_capture_deadline(self):
        from blackoutburst_agents.linux_test_supervisor import _source_authorized
        window={'validated_at':100.1,'deadline':101.5}
        control=SimpleNamespace(authorized=Mock(return_value=True),last_window={'deadline':102.0})
        with patch('blackoutburst_agents.linux_test_supervisor._control_clock',return_value=101.499):
            _source_authorized(control,window)
        for now in (101.5,101.7,100.0):
            with patch('blackoutburst_agents.linux_test_supervisor._control_clock',return_value=now):
                with self.assertRaises(DevelopmentError):_source_authorized(control,window)
