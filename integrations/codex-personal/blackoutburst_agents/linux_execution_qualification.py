"""Explicit nine-case Linux backend qualification; never a broker PASSED proof.

Only qualify_execution_backend starts synthetic jobs. Validation only rereads
exact private observations. Faults require a separate live parent observer with
pidfds before ACK; neither logs nor a numeric PID authorize a signal. No model,
account, Board, authentication home or service is used by this module.
"""
from __future__ import annotations
import os
from pathlib import Path
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, absolute_directory, canonical, digest, identity, sha256

SCHEMA = 'blackoutburst-linux-qualification-case-v1'
MATRIX_SCHEMA = 'blackoutburst-linux-qualification-matrix-v1'
QUALIFICATION_SCHEMA = 'blackoutburst-linux-execution-qualification-v1'
CASES = ('normal','assertion_failed','output_limit','wall_timeout','source_revoked',
         'eof_running','controller_loss','supervisor_loss','replay_and_corruption')
FAULT_CASES = frozenset({'controller_loss','supervisor_loss'})
ORDINARY_CASES = frozenset(CASES)-FAULT_CASES
MAX_PROOF = 32*1024
MAX_MESSAGE = 16*1024
MAX_DRIVER_PACKET = 256*1024
MATRIX_SECONDS = 180
CASE_SECONDS = 22
OBSERVATION_SECONDS = 5
ACK_SECONDS = 0.8
NODES = ('driver','supervisor','launcher','init')
DIAGNOSTIC_PHASES = frozenset({'helper_launch_intent','namespace_launch_intent',
    'source_boundary_requested','source_release_intent','source_ack_written','complete'})
DIAGNOSTIC_STAGES = frozenset({'case','execution','native','native_expected','fault','proof'})
CONTROL_CODES = frozenset({'permission_revoked','supervisor_clock_unavailable','supervisor_clock_changed',
    'supervisor_control_invalid','supervisor_authorization_expired','supervisor_control_write_failed',
    'linux_qualification_eof','linux_qualification_source_revoked','linux_source_boundary_unconfirmed'})
SCOPE_CODES = frozenset({'linux_pool_preparation_changed','linux_pool_job_changed','linux_pool_job_not_queued',
    'linux_pool_ticket_not_ready','linux_pool_selection_changed','development_stop_requested',
    'development_registration_mismatch','development_grants_unavailable','development_ticket_unavailable',
    'development_claim_lost','development_claim_invalid','development_context_changed','development_claim_changed'})
NATIVE_REASONS = frozenset({'completed','script_failed','linux_authorization_expired','linux_startup_expired',
    'linux_source_boundary_missing','linux_kernel_guard_failed','linux_script_wall_expired',
    'linux_output_limit','linux_native_unconfirmed'})


def qualification_diagnostic(value):
    """Closed copied observations only; never a proof, path, packet or exception text."""
    try:
        if type(value) is not dict:return None
        value=dict(value)
        if (any(type(key) is not str for key in value)
                or set(value) not in ({'case','stage','phase','controller','native'},
                {'case','stage','phase','controller','native','scope_check'})
                or type(value['case']) is not str or value['case'] not in CASES
                or type(value['stage']) is not str or value['stage'] not in DIAGNOSTIC_STAGES
                or not (value['phase'] is None or type(value['phase']) is str and value['phase'] in DIAGNOSTIC_PHASES)):
            return None
        if value['controller'] is not None:
            if type(value['controller']) is not dict:return None
            item=dict(value['controller'])
            if (any(type(key) is not str for key in item)
                    or set(item)!={'operation','bucket','code'} or type(item['operation']) is not str
                    or item['operation'] not in {'initial_window','initial_send','heartbeat_window','heartbeat_send',
                        'source_window','source_publish','source_send'}
                    or type(item['bucket']) is not str or item['bucket'] not in {'development','os_error'}
                    or not (item['code'] is None or type(item['code']) is str and item['code'] in CONTROL_CODES)
                    or item['bucket']=='os_error' and item['code'] is not None):return None
            value['controller']=item
        if value['native'] is not None:
            if type(value['native']) is not dict:return None
            item=dict(value['native'])
            if (any(type(key) is not str for key in item)
                    or set(item)!={'reason','launcher_attempted','source_release_intent','source_released','checks','cleanup'}):return None
            if type(item['checks']) is not dict or type(item['cleanup']) is not dict:return None
            item['checks'],item['cleanup']=dict(item['checks']),dict(item['cleanup'])
            from .linux_test_backend import CHECKS
            if (type(item['reason']) is not str or item['reason'] not in NATIVE_REASONS
                    or any(type(item[name]) is not bool for name in ('launcher_attempted','source_release_intent','source_released'))
                    or item['checks'] and set(item['checks'])!=CHECKS
                    or any(type(key) is not str or type(flag) is not bool for key,flag in item['checks'].items())
                    or set(item['cleanup'])!={'init_identity_received','init_terminal','launcher_reaped','cleanup_confirmed','numeric_pid_signalled'}
                    or any(type(key) is not str or type(flag) is not bool for key,flag in item['cleanup'].items())):return None
            value['native']=item
        if 'scope_check' in value:
            if type(value['scope_check']) is not dict:return None
            item=dict(value['scope_check'])
            if (any(type(key) is not str for key in item)
                    or set(item)!={'stage','allowed','bucket','code','elapsed_ms'} or type(item['stage']) is not str
                    or item['stage'] not in {'held','fresh','scope'}
                    or type(item['allowed']) is not bool
                    or not (item['bucket'] is None or type(item['bucket']) is str and item['bucket'] in {'development','os_error','other'})
                    or not (item['code'] is None or type(item['code']) is str and item['code'] in SCOPE_CODES)
                    or type(item['elapsed_ms']) is not int or not 0<=item['elapsed_ms']<=600000):return None
            value['scope_check']=item
        return value
    except Exception:return None


def _case_observation(case):
    return {'case':case,'stage':'case','phase':None,'controller':None,'native':None}


def _observe(backend,case,**facts):
    try:
        value=getattr(backend,'_qualification_observation',None)
        if type(value) is dict and value.get('case')==case:
            if value.get('controller') is not None:facts.pop('phase',None)
            value.update(facts)
    except Exception:pass


def _native_summary(value):
    if type(value) is not dict:return None
    return {name:value.get(name) for name in
        ('reason','launcher_attempted','source_release_intent','source_released','checks','cleanup')}


def require(condition, code='linux_qualification_unconfirmed'):
    if not condition: raise DevelopmentError(code) from None


def _same(left,right): return canonical(left)==canonical(right)


def _backend_class():
    from .linux_test_backend import SupervisedLinuxNamedPythonBackend
    return SupervisedLinuxNamedPythonBackend


def _real_host(backend):
    from .linux_planner_policy import linux_execution_identity
    require(sys.platform=='linux' and os.name=='posix' and os.getuid()==os.geteuid()!=0
            and os.getgid()==os.getegid()!=0 and hasattr(os,'pidfd_open')
            and hasattr(signal,'pidfd_send_signal'),'linux_qualification_real_host_required')
    require(_same(linux_execution_identity(),backend.environment),'linux_qualification_environment_changed')


def _read_document(private,name,maximum):
    raw=private.read(name,maximum);value=decode(raw)
    require(canonical(value)==raw,'linux_qualification_document_changed')
    return raw,value


def _bounded_names(descriptor,maximum):
    """Read at most maximum+1 names, never materialize an unknown tree."""
    require(type(maximum) is int and 0<maximum<=64)
    names=[]
    with os.scandir(descriptor) as entries:
        for entry in entries:
            require(len(names)<maximum,'linux_qualification_files_changed')
            require(type(entry.name) is str and 0<len(entry.name)<=255,
                    'linux_qualification_files_changed')
            names.append(entry.name)
    return names


def _private_names(private,maximum):
    private.verify();names=set(_bounded_names(private.fd,maximum));private.verify()
    return names


def _request(backend,case,request):
    from .linux_test_protocol import validate_execution_request_shape
    validate_execution_request_shape(request)
    require(request['manifest_sha256']==digest(canonical(backend.manifest))
            and request['plan_sha256']==backend.document['plan_hash']
            and request['scope_sha256']==digest(canonical(backend.manifest['scope']))
            and request['root']==backend.manifest['root']
            and _same(request['root_identity'],backend.manifest['root_identity'])
            and request['name']=='qualification-'+case
            and request['timeout_seconds']==(1 if case=='wall_timeout' else 5)
            and request['max_output_bytes']==4096
            and request['input_plan_sha256']==backend.plan['input_plan_sha256']
            and request['fence_sha256']==digest(b'qualification-no-broker'),
            'linux_qualification_request_changed')
    return digest(canonical(request))


def _job(backend,case,request):
    from .linux_test_backend import MAX_JOB_BYTES,_state,QUALIFICATION_SCRIPTS,validate_job_shape
    private=PrivateDirectory(backend._job_directory(request))
    try:
        _,job=_read_document(private,'job.json',MAX_JOB_BYTES)
        validate_job_shape(job,private.path)
        _,state=_read_document(private,'state.json',8192)
        _state(state,job)
        require(_same(job['request'],request) and job['qualification'] is True
                and job['qualification_case']==case and job['plan_sha256']==backend.document['plan_hash']
                and job['backend_directory']==str(backend.private.path)
                and job['script_sha256']==digest(QUALIFICATION_SCRIPTS[case]),
                'linux_qualification_job_changed')
        return private,job,state
    except BaseException:
        private.close();raise


def _native(backend,case,request):
    from .linux_test_backend import read_native_observation
    private,job,state=_job(backend,case,request)
    with private:
        _observe(backend,case,stage='native',phase=state['phase'])
        require(state['phase']=='complete','linux_qualification_native_missing')
        raw,value=_read_document(private,'native-receipt.json',128*1024)
        require(digest(raw)==state['native_receipt_sha256'],'linux_qualification_native_changed')
        observed=read_native_observation(private,job,backend.plan)
        require(_same(observed,value),'linux_qualification_native_changed')
        _observe(backend,case,native=_native_summary(value))
        require(value['cleanup']['cleanup_confirmed'] is True
                and value['broker_snapshot_admitted'] is False,'linux_qualification_cleanup_unconfirmed')
        return raw,value


def _expected_native(case,native):
    expected={'normal':'completed','assertion_failed':'script_failed','output_limit':'linux_output_limit',
        'wall_timeout':'linux_script_wall_expired','source_revoked':'linux_authorization_expired',
        'eof_running':'linux_authorization_expired','controller_loss':'linux_authorization_expired',
        'replay_and_corruption':'completed'}
    require(case in expected and native['reason']==expected[case], 'linux_qualification_outcome_changed')
    require(native['source_released'] is (case!='source_revoked'),'linux_qualification_source_unconfirmed')
    require(native['launcher_attempted'] is True
            and native['cleanup']['init_identity_received'] is True
            and native['cleanup']['init_terminal'] is True
            and native['cleanup']['launcher_reaped'] is True
            and native['cleanup']['cleanup_confirmed'] is True
            and native['cleanup']['numeric_pid_signalled'] is False,
            'linux_qualification_cleanup_unconfirmed')
    if case in ('normal','replay_and_corruption'):require(type(native['exit_code']) is int and native['exit_code']==0)
    if case=='assertion_failed':require(type(native['exit_code']) is int and native['exit_code']!=0)
    from .linux_test_backend import CHECKS
    require(type(native['checks']) is dict and set(native['checks'])==CHECKS
            and all(value is True for value in native['checks'].values()),'linux_qualification_guards_unconfirmed')
    if case=='source_revoked':require(native['source_release_intent'] is False)
    if case=='output_limit':require(native['output_bytes']==4096)
    return True


def _base_proof(backend,case,request):
    return {'schema':SCHEMA,'case':case,'plan_sha256':backend.document['plan_hash'],
        'environment_sha256':digest(canonical(backend.environment)),'request':request,
        'request_sha256':_request(backend,case,request),'native_receipt_sha256':None,
        'observation':None,'cleanup_confirmed':True,**_matrix_binding(backend)}


def _matrix_binding(backend):
    backend.private.verify()
    with PrivateDirectory(backend.private.path/'qualification-matrix') as private:
        _,intent=_read_document(private,'intent.json',8192)
        require(type(intent) is dict and set(intent)=={'schema','origin','plan_sha256',
            'environment_sha256','cases','backend_directory','backend_identity','matrix_identity'}
            and intent['schema']==MATRIX_SCHEMA and type(intent['origin']) is str
            and str(uuid.UUID(intent['origin']))==intent['origin']
            and _same(intent['cases'],list(CASES)) and intent['plan_sha256']==backend.document['plan_hash']
            and intent['environment_sha256']==digest(canonical(backend.environment))
            and intent['backend_directory']==str(backend.private.path)
            and _same(intent['backend_identity'],list(backend.private.identity))
            and _same(intent['matrix_identity'],list(private.identity)),
            'linux_qualification_origin_changed')
        return {'backend_directory':intent['backend_directory'],'backend_identity':intent['backend_identity'],
            'matrix_identity':intent['matrix_identity'],'matrix_origin':intent['origin']}


def _ordinary(backend,case):
    result=backend._qualify_case(case)
    require(type(result) is dict and set(result)=={'request','native','native_receipt_sha256'})
    proof=_base_proof(backend,case,result['request'])
    raw,native=_native(backend,case,result['request'])
    _observe(backend,case,stage='native_expected');_expected_native(case,native)
    require(_same(native,result['native']) and digest(raw)==result['native_receipt_sha256'])
    replay=corruption=False
    if case=='replay_and_corruption':
        from .linux_test_backend import QUALIFICATION_SCRIPTS,_QualificationContext,validate_native_receipt
        private,job,state=_job(backend,case,result['request'])
        with private:
            before={name:private.read(name,maximum) for name,maximum in
                    (('job.json',1024*1024),('state.json',8192),('native-receipt.json',128*1024))}
            packet=private.read('input.packet',4*1024*1024+128*1024)
            # The exact same operation directory already exists. The backend
            # must reject this replay at exclusive mkdir, before any Popen.
            try:
                backend._run_job(result['request'],packet,QUALIFICATION_SCRIPTS[case],
                    _QualificationContext(backend,case),qualification=True,qualification_case=case)
            except FileExistsError: replay=True
            require(replay,'linux_qualification_replay_not_refused')
            require(all(private.read(name,maximum)==before[name] for name,maximum in
                (('job.json',1024*1024),('state.json',8192),('native-receipt.json',128*1024))),
                'linux_qualification_replay_changed_job')
            changed=decode(canonical(native));changed['request_sha256']='0'*64
            try:validate_native_receipt(changed,job,backend.plan)
            except DevelopmentError:corruption=True
            require(corruption,'linux_qualification_corruption_not_refused')
    proof['native_receipt_sha256']=digest(raw)
    proof['observation']={'kind':'native','replay_refused':replay,'corruption_refused':corruption}
    return proof


def _parse_status(raw):
    require(type(raw) is bytes and 0<len(raw)<=32768 and raw.endswith(b'\n'))
    values={}
    try:
        for line in raw.decode('ascii').splitlines():
            key,value=line.split(':',1)
            if key in ('Pid','PPid','NSpid','Uid','Gid'):
                require(key not in values);values[key]=value.split()
        require(set(values)=={'Pid','PPid','NSpid','Uid','Gid'})
        for key,parts in values.items():
            require(1<=len(parts)<=4 and all(re.fullmatch('[0-9]{1,10}',item) for item in parts))
            values[key]=[int(item) for item in parts]
        require(len(values['Pid'])==len(values['PPid'])==1
                and 1<=len(values['NSpid'])<=2 and len(values['Uid'])==len(values['Gid'])==4)
        return values
    except (UnicodeError,ValueError,TypeError):raise DevelopmentError('linux_qualification_process_unconfirmed') from None


def _parse_start(raw):
    require(type(raw) is bytes and 0<len(raw)<=4096 and raw.endswith(b'\n'))
    try:
        end=raw.rindex(b') ');parts=raw[end+2:].split()
        require(len(parts)>=20 and re.fullmatch(b'[0-9]{1,20}',parts[19]) is not None)
        return int(parts[19])
    except (ValueError,TypeError):raise DevelopmentError('linux_qualification_process_unconfirmed') from None


def _proc_read(directory,name,maximum):
    from .linux_planner_policy import _procfs_magic
    fd=None
    try:
        fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        require(stat.S_ISREG(os.fstat(fd).st_mode) and _procfs_magic(fd)==0x9fa0)
        raw=b''
        while len(raw)<=maximum:
            part=os.read(fd,min(4096,maximum+1-len(raw)))
            if not part:break
            raw+=part
        require(len(raw)<=maximum);return raw
    finally:
        if fd is not None:os.close(fd)


def _descriptor_pin(descriptor):
    info=os.fstat(descriptor)
    return (info.st_dev,info.st_ino,info.st_mode,info.st_uid,info.st_gid,info.st_rdev)


class _LiveProcess:
    """Pidfd acquired against a live child relation; never reconstructed later."""
    def __init__(self,proc,pid,*,parent,namespace_init):
        from .linux_planner_policy import _procfs_magic
        require(type(pid) is int and pid>1 and type(parent) is int and parent>0)
        self.fd=self.pidfd=None;self.fd_pin=self.pidfd_pin=None
        self.pid=pid;self.proc=proc;self.closed=False
        try:
            self.fd=os.open(str(pid),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=proc)
            self.fd_pin=_descriptor_pin(self.fd)
            require(_procfs_magic(self.fd)==0x9fa0)
            self.status=_parse_status(_proc_read(self.fd,'status',32768))
            self.start=_parse_start(_proc_read(self.fd,'stat',4096))
            require(self.status['Pid']==[pid] and self.status['PPid']==[parent]
                    and self.status['Uid']==[os.geteuid()]*4 and self.status['Gid']==[os.getegid()]*4
                    and self.status['NSpid']==([pid,1] if namespace_init else [pid]))
            self.namespace=os.stat('ns/pid',dir_fd=self.fd,follow_symlinks=True)
            self.namespace=(self.namespace.st_dev,self.namespace.st_ino)
            self.pidfd=os.pidfd_open(pid,0);os.set_inheritable(self.pidfd,False)
            self.pidfd_pin=_descriptor_pin(self.pidfd)
            require(self._pidfd_owned(allow_dead=False))
            self.verify(parent=parent)
        except BaseException:self.close();raise
    def verify(self,*,parent):
        require(not self.closed and not self.terminal())
        require(_parse_status(_proc_read(self.fd,'status',32768))==self.status
                and _parse_start(_proc_read(self.fd,'stat',4096))==self.start
                and self.status['PPid']==[parent])
        current=os.stat('ns/pid',dir_fd=self.fd,follow_symlinks=True)
        require((current.st_dev,current.st_ino)==self.namespace)
    def children(self):
        task=thread=None
        try:
            task=os.open('task',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.fd)
            require(_bounded_names(task,2)==[str(self.pid)],'linux_qualification_threads_unconfirmed')
            thread=os.open(str(self.pid),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=task)
            raw=_proc_read(thread,'children',256)
            require(re.fullmatch(rb'(?:[1-9][0-9]{0,9} ?){0,4}',raw) is not None)
            result=[int(value) for value in raw.split()]
            require(len(result)<=1 and len(result)==len(set(result)))
            return result
        finally:
            for fd in (thread,task):
                if fd is not None:os.close(fd)
    def terminal(self):
        require(self.pidfd is not None and not self.closed and self._pidfd_owned(allow_dead=True))
        return bool(select.select([self.pidfd],[],[],0)[0])
    def _pidfd_owned(self,*,allow_dead):
        if self.pidfd is None or _descriptor_pin(self.pidfd)!=self.pidfd_pin:return False
        own=info=None
        try:
            own=os.open(str(os.getpid()),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=self.proc)
            info=os.open('fdinfo',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=own)
            raw=_proc_read(info,str(self.pidfd),4096);fields={}
            for line in raw.decode('ascii').splitlines():
                if ':' in line:
                    key,value=line.split(':',1)
                    if key in ('Pid','NSpid'):require(key not in fields);fields[key]=value.split()
            return (fields=={'Pid':[str(self.pid)],'NSpid':[str(item) for item in self.status['NSpid']]}
                    or allow_dead and fields=={'Pid':['-1'],'NSpid':['-1']})
        finally:
            for fd in (info,own):
                if fd is not None:os.close(fd)
    def kill_if_live(self):
        if not self.terminal():
            try:signal.pidfd_send_signal(self.pidfd,signal.SIGKILL)
            except ProcessLookupError:pass
    def close(self):
        if self.closed:return
        self.closed=True
        for name in ('pidfd','fd'):
            fd=getattr(self,name,None)
            if fd is not None:
                try:
                    owned=(_descriptor_pin(fd)==getattr(self,name+'_pin')
                           and (name!='pidfd' or self._pidfd_owned(allow_dead=True)))
                    if owned:os.close(fd)
                except (DevelopmentError,OSError,ValueError,UnicodeError):pass
                setattr(self,name,None)


class _FaultObserver:
    def __init__(self,driver):
        from .linux_planner_policy import _procfs_magic
        self.proc=None;self.nodes=[];self.acquired=False
        require(type(driver.pid) is int and driver.pid>1 and driver.poll() is None)
        try:
            self.proc=os.open('/proc',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            require(_procfs_magic(self.proc)==0x9fa0)
            require(os.readlink('self',dir_fd=self.proc)==str(os.getpid()))
            self.nodes.append(_LiveProcess(self.proc,driver.pid,parent=os.getpid(),namespace_init=False))
        except BaseException:self.close();raise
    def acquire(self,supervisor_pid):
        require(not self.acquired and len(self.nodes)==1)
        driver=self.nodes[0]
        require(driver.children()==[supervisor_pid])
        parent=driver
        for index in range(1,4):
            children=parent.children();require(len(children)==1)
            node=_LiveProcess(self.proc,children[0],parent=parent.pid,namespace_init=index==3)
            self.nodes.append(node);parent=node
        require(not self.nodes[-1].children())
        require(all(node.namespace==driver.namespace for node in self.nodes[:3])
                and self.nodes[-1].namespace!=driver.namespace)
        for index,node in enumerate(self.nodes):
            node.verify(parent=os.getpid() if index==0 else self.nodes[index-1].pid)
            require(node.children()==([] if index==3 else [self.nodes[index+1].pid]))
        self.acquired=True
        return True
    def finish(self,driver):
        require(self.acquired and len(self.nodes)==4)
        deadline=time.monotonic()+OBSERVATION_SECONDS
        while time.monotonic()<deadline and not all(node.terminal() for node in self.nodes):time.sleep(0.02)
        passive=all(node.terminal() for node in self.nodes)
        if not passive:
            for node in reversed(self.nodes):node.kill_if_live()
            deadline=time.monotonic()+2
            while time.monotonic()<deadline and not all(node.terminal() for node in self.nodes):time.sleep(0.02)
        terminal=all(node.terminal() for node in self.nodes)
        try:driver.wait(timeout=2);reaped=True
        except (OSError,subprocess.TimeoutExpired):reaped=False
        require(terminal and reaped,'linux_qualification_cleanup_unconfirmed')
        return {'relationships_verified':True,'pidfds_acquired_before_ack':True,
            'terminal':{name:True for name in NODES},'driver_reaped':True,
            'cleanup_method':'passive' if passive else 'owned_pidfds','numeric_pid_signalled':False}
    def emergency_cleanup(self,driver):
        # On a failed acquisition we can clean only what was positively held.
        # Unknown descendants remain unconfirmed; this can never qualify a case.
        for node in reversed(self.nodes):
            try:node.kill_if_live()
            except (DevelopmentError,OSError):pass
        try:driver.wait(timeout=2)
        except (OSError,subprocess.TimeoutExpired):pass
    def close(self):
        for node in reversed(getattr(self,'nodes',[])):node.close()
        self.nodes=[]
        if self.proc is not None:os.close(self.proc);self.proc=None


# The driver is fixed controller code imported from held, hash-pinned package
# bytes. It does not inherit auth state or read/copy account files. The request
# packet is synthetic and sealed; no module is imported from test inputs.
DRIVER_BOOTSTRAP = r'''
import hashlib,importlib.abc,importlib.util,json,os,stat,sys
spec,token=sys.argv[1:];spec=json.loads(spec);held={};total=0;dirs=[]
if not isinstance(spec,dict) or set(spec)!={'package','files'}:raise SystemExit(78)
pins=spec['files'];package=spec['package']
if not isinstance(pins,dict) or not 1<=len(pins)<=128 or not isinstance(package,dict) or set(package)!={'path','identity','chain'}:raise SystemExit(78)
root=package['path'];chain=package['chain']
if not isinstance(root,str) or not root.startswith('/') or any(p in ('','.','..') for p in root.split('/')[1:]) or not isinstance(chain,list) or not 1<=len(chain)<=256:raise SystemExit(78)
def check(info,pin,path):
 if not isinstance(pin,dict) or set(pin)!={'path','identity','uid','mode'} or pin['path']!=path or pin['identity']!=[info.st_dev,info.st_ino] or type(pin['uid'])is not int or pin['uid']!=info.st_uid or type(pin['mode'])is not int or pin['mode']!=info.st_mode:raise SystemExit(78)
fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);dirs.append((None,None,fd));check(os.fstat(fd),chain[0],'/');path=''
try:
 parts=root.split('/')[1:]
 if len(chain)!=len(parts)+1:raise SystemExit(78)
 for index,part in enumerate(parts):
  parent=fd;fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);dirs.append((parent,part,fd));path+='/'+part
  check(os.fstat(fd),chain[index+1],path)
 if package['identity']!=chain[-1]['identity']:raise SystemExit(78)
 for name,expected in pins.items():
  if not isinstance(name,str) or '/' in name or not name.endswith('.py') or not isinstance(expected,str) or len(expected)!=64:raise SystemExit(78)
  source=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd)
  try:
   before=os.fstat(source)
   if not stat.S_ISREG(before.st_mode) or before.st_uid not in (0,os.geteuid()) or before.st_nlink!=1 or before.st_mode&0o022 or not 0<before.st_size<=262144:raise SystemExit(78)
   data=b''
   while len(data)<=262144:
    chunk=os.read(source,min(65536,262145-len(data)))
    if not chunk:break
    data+=chunk
   after=os.fstat(source);linked=os.stat(name,dir_fd=fd,follow_symlinks=False)
   keys=('st_dev','st_ino','st_mode','st_uid','st_gid','st_nlink','st_size','st_mtime_ns','st_ctime_ns')
   if len(data)>262144 or any(getattr(before,k)!=getattr(after,k) or getattr(before,k)!=getattr(linked,k) for k in keys) or hashlib.sha256(data).hexdigest()!=expected:raise SystemExit(78)
   total+=len(data)
   if total>4194304:raise SystemExit(78)
   held[name]=(root+'/'+name,data)
  finally:os.close(source)
 for index,(parent,name,descriptor) in enumerate(dirs):
  check(os.fstat(descriptor),chain[index],chain[index]['path'])
  if parent is not None:check(os.stat(name,dir_fd=parent,follow_symlinks=False),chain[index],chain[index]['path'])
finally:
 for _,_,descriptor in reversed(dirs):os.close(descriptor)
class Held(importlib.abc.MetaPathFinder,importlib.abc.Loader):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='blackoutburst_agents':name='__init__.py'
  elif fullname.startswith('blackoutburst_agents.') and fullname.count('.')==1:name=fullname.split('.')[1]+'.py'
  else:return None
  if name not in held:raise ImportError('unpinned_module')
  return importlib.util.spec_from_loader(fullname,self,is_package=name=='__init__.py')
 def create_module(self,spec):return None
 def exec_module(self,module):
  name='__init__.py' if module.__name__=='blackoutburst_agents' else module.__name__.split('.')[1]+'.py'
  path,data=held[name];module.__file__=path
  if name=='__init__.py':module.__path__=[]
  exec(compile(data,path,'exec',dont_inherit=True),module.__dict__)
sys.meta_path.insert(0,Held())
from blackoutburst_agents.linux_execution_qualification import _driver_entry
raise SystemExit(_driver_entry(token))
'''


def _send(channel,value):
    raw=canonical(value);require(0<len(raw)<=MAX_MESSAGE,'linux_qualification_protocol_limit')
    require(channel.send(raw)==len(raw),'linux_qualification_protocol_unconfirmed')


def _receive(channel,timeout):
    require(select.select([channel],[],[],max(0,timeout))[0],'linux_qualification_protocol_timeout')
    raw,ancillary,flags,_=channel.recvmsg(MAX_MESSAGE)
    require(raw and not ancillary and not flags&(socket.MSG_TRUNC|socket.MSG_CTRUNC),
            'linux_qualification_protocol_unconfirmed')
    value=decode(raw);require(canonical(value)==raw,'linux_qualification_protocol_unconfirmed');return value


def _driver_entry(token):
    """Fixed subprocess entry; callable only with a sealed synthetic packet."""
    from .linux_test_namespace import read_sealed
    token=decode(token.encode());require(type(token) is dict and set(token)=={'fd','sha256'})
    raw=read_sealed(token['fd'],token['sha256'],MAX_DRIVER_PACKET);packet=decode(raw)
    require(canonical(packet)==raw and type(packet) is dict and set(packet)==
            {'case','backend_directory','manifest','plan_sha256','environment_sha256','channel_fd','nonce'})
    case=packet['case'];require(case in FAULT_CASES and type(packet['channel_fd']) is int and packet['channel_fd']>=3)
    require(type(packet['nonce']) is str and re.fullmatch('[0-9a-f]{32}',packet['nonce']) is not None)
    channel=socket.socket(fileno=packet['channel_fd']);backend=None
    try:
        backend=_backend_class()(packet['backend_directory'],manifest=packet['manifest'],authorize_scope=lambda *_:True)
        _real_host(backend)
        require(backend.document['plan_hash']==packet['plan_sha256']
                and digest(canonical(backend.environment))==packet['environment_sha256'])
        def authorize_fault(actual,child,state,request):
            require(actual==case and child.poll() is None and state['phase']=='source_ack_written'
                    and state['request_sha256']==_request(backend,case,request))
            message={'schema':MATRIX_SCHEMA,'kind':'fault_ready','case':case,'nonce':packet['nonce'],
                'supervisor_pid':child.pid,'state':state,'request':request}
            started=time.monotonic();_send(channel,message)
            ack=_receive(channel,ACK_SECONDS)
            require(ack=={'schema':MATRIX_SCHEMA,'kind':'fault_ack','case':case,'nonce':packet['nonce'],
                'request_sha256':digest(canonical(request))}
                and time.monotonic()-started<ACK_SECONDS,'linux_qualification_observer_required')
            return True
        try:
            backend._qualify_case(case,fault_authorizer=authorize_fault)
        except DevelopmentError as error:
            if case=='supervisor_loss' and error.code=='linux_supervisor_lost':
                _send(channel,{'schema':MATRIX_SCHEMA,'kind':'fault_observed','case':case,'nonce':packet['nonce'],
                    'reason':'linux_supervisor_lost'});return 0
            raise
        require(False,'linux_qualification_fault_not_observed')
    except (DevelopmentError,OSError,ValueError,TypeError):
        try:_send(channel,{'schema':MATRIX_SCHEMA,'kind':'driver_refused','case':case,'nonce':packet['nonce']})
        except (DevelopmentError,OSError):pass
        return 2
    finally:
        if backend is not None:backend.close()
        channel.close()


def _fault(backend,case):
    from .linux_test_namespace import sealed_memfd
    parent,child=socket.socketpair(socket.AF_UNIX,socket.SOCK_SEQPACKET)
    driver=observer=None;packet_fd=None;success=False
    nonce=uuid.uuid4().hex
    try:
        packet={'case':case,'backend_directory':str(backend.private.path),'manifest':backend.manifest,
            'plan_sha256':backend.document['plan_hash'],'environment_sha256':digest(canonical(backend.environment)),
            'channel_fd':child.fileno(),'nonce':nonce}
        raw=canonical(packet);require(len(raw)<=MAX_DRIVER_PACKET)
        packet_fd=sealed_memfd('linux-qualification-driver',raw)
        pins={name:value['sha256'] for name,value in backend.plan['sources']['files'].items()}
        command=[backend.plan['interpreter'],'-I','-B','-S','-c',DRIVER_BOOTSTRAP,
            canonical({'package':backend.plan['sources']['package'],'files':pins}).decode(),
            canonical({'fd':packet_fd,'sha256':digest(raw)}).decode()]
        started=time.monotonic()
        driver=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            env={'LANG':'C.UTF-8','PATH':'/usr/bin:/bin'},cwd=backend.private.path,
            pass_fds=(packet_fd,child.fileno()),close_fds=True,start_new_session=True)
        child.close();observer=_FaultObserver(driver)
        message=_receive(parent,CASE_SECONDS)
        require(type(message) is dict and set(message)=={'schema','kind','case','nonce','supervisor_pid','state','request'}
                and message['schema']==MATRIX_SCHEMA and message['kind']=='fault_ready'
                and message['case']==case and message['nonce']==nonce)
        request=message['request'];proof=_base_proof(backend,case,request)
        private,job,state=_job(backend,case,request)
        with private:
            require(_same(state,message['state']) and state['phase']=='source_ack_written')
            _observe(backend,case,stage='fault',phase=state['phase'])
            require(observer.acquire(message['supervisor_pid']) is True,'linux_qualification_observer_required')
            # Read the exact linked state again after kernel handle acquisition.
            require(private.read('state.json',8192)==canonical(state)
                    and time.monotonic()-started<CASE_SECONDS)
            _send(parent,{'schema':MATRIX_SCHEMA,'kind':'fault_ack','case':case,'nonce':nonce,
                'request_sha256':digest(canonical(request))})
        if case=='supervisor_loss':
            reply=_receive(parent,CASE_SECONDS-(time.monotonic()-started))
            require(reply=={'schema':MATRIX_SCHEMA,'kind':'fault_observed','case':case,'nonce':nonce,
                'reason':'linux_supervisor_lost'})
        # Observe the intended driver outcome BEFORE any fallback cleanup can
        # send SIGKILL. Our own cleanup must never masquerade as self-loss.
        try:driver.wait(timeout=2)
        except subprocess.TimeoutExpired:require(False,'linux_qualification_fault_not_observed')
        require(driver.returncode==(-signal.SIGKILL if case=='controller_loss' else 0),
                'linux_qualification_fault_not_observed')
        observation=observer.finish(driver)
        require(driver.returncode==(-signal.SIGKILL if case=='controller_loss' else 0),
                'linux_qualification_fault_not_observed')
        observation.update(kind='independent_fault',source_ack_sha256=digest(canonical(state)),
            driver_outcome='self_sigkill' if case=='controller_loss' else 'supervisor_loss_confirmed')
        if case=='controller_loss':
            raw,native=_native(backend,case,request)
            _observe(backend,case,stage='native_expected');_expected_native(case,native)
            proof['native_receipt_sha256']=digest(raw)
        else:
            with PrivateDirectory(backend._job_directory(request)) as private:
                require(private.read('state.json',8192)==canonical(state))
                require('native-receipt.json' not in _private_names(private,16),
                        'linux_qualification_unexpected_native_receipt')
            observation['native_outcome']='unknown'
        proof['observation']=observation;success=True;return proof
    finally:
        parent.close();child.close()
        if driver is not None and not success:
            if observer is not None:observer.emergency_cleanup(driver)
            elif driver.poll() is None:
                # Exact unreaped owned Popen child only. Descendants unknown =>
                # the case remains refused regardless of this best effort.
                driver.kill()
                try:driver.wait(timeout=2)
                except (OSError,subprocess.TimeoutExpired):pass
        if observer is not None:observer.close()
        if packet_fd is not None:os.close(packet_fd)


def _validate_observation(case,observation):
    if case not in FAULT_CASES:
        require(type(observation) is dict and set(observation)=={'kind','replay_refused','corruption_refused'}
            and observation['kind']=='native' and type(observation['replay_refused']) is bool
            and type(observation['corruption_refused']) is bool
            and observation['replay_refused'] is (case=='replay_and_corruption')
            and observation['corruption_refused'] is (case=='replay_and_corruption'))
        return
    fields={'kind','relationships_verified','pidfds_acquired_before_ack','terminal','driver_reaped',
        'cleanup_method','numeric_pid_signalled','source_ack_sha256','driver_outcome'}
    if case=='supervisor_loss':fields.add('native_outcome')
    require(type(observation) is dict and set(observation)==fields and observation['kind']=='independent_fault'
        and observation['relationships_verified'] is True and observation['pidfds_acquired_before_ack'] is True
        and observation['driver_reaped'] is True and observation['numeric_pid_signalled'] is False
        and observation['cleanup_method'] in ('passive','owned_pidfds')
        and type(observation['terminal']) is dict and set(observation['terminal'])==set(NODES)
        and all(value is True for value in observation['terminal'].values())
        and observation['driver_outcome']==('self_sigkill' if case=='controller_loss' else 'supervisor_loss_confirmed'))
    sha256(observation['source_ack_sha256'])
    if case=='supervisor_loss':require(observation['native_outcome']=='unknown')


def _validate_proof(backend,case,proof):
    require(type(proof) is dict and set(proof)=={'schema','case','plan_sha256','environment_sha256','request',
        'request_sha256','native_receipt_sha256','observation','cleanup_confirmed',
        'backend_directory','backend_identity','matrix_identity','matrix_origin'}
        and proof['schema']==SCHEMA and proof['case']==case and proof['cleanup_confirmed'] is True
        and proof['plan_sha256']==backend.document['plan_hash']
        and proof['environment_sha256']==digest(canonical(backend.environment))
        and proof['request_sha256']==_request(backend,case,proof['request']))
    require(_same({name:proof[name] for name in ('backend_directory','backend_identity','matrix_identity','matrix_origin')},
                  _matrix_binding(backend)),'linux_qualification_origin_changed')
    _validate_observation(case,proof['observation'])
    if case!='supervisor_loss':
        raw,native=_native(backend,case,proof['request'])
        _observe(backend,case,stage='native_expected');_expected_native(case,native)
        require(digest(raw)==proof['native_receipt_sha256'])
        if case=='controller_loss':
            private,job,state=_job(backend,case,proof['request'])
            with private:
                require(state['phase']=='complete'
                        and state['native_receipt_sha256']==proof['native_receipt_sha256'])
                # The supervisor's terminal write changes only these two
                # fields. Bind the independently observed pre-loss ACK to the
                # exact nonce/request/boundary/challenge retained at completion.
                ack={**state,'phase':'source_ack_written','native_receipt_sha256':None}
                require(digest(canonical(ack))==proof['observation']['source_ack_sha256'],
                        'linux_qualification_source_ack_changed')
    else:
        require(proof['native_receipt_sha256'] is None)
        private,job,state=_job(backend,case,proof['request'])
        with private:
            require(state['phase']=='source_ack_written' and digest(canonical(state))==proof['observation']['source_ack_sha256']
                    and 'native-receipt.json' not in _private_names(private,16))
    return True


def qualify_execution_backend(backend):
    """One fresh durable matrix. Never retries/adopts a partial matrix."""
    require(type(backend) is _backend_class(),'linux_qualification_concrete_backend_required')
    _real_host(backend);backend._pins()
    parent=backend.private;parent.verify()
    os.mkdir('qualification-matrix',0o700,dir_fd=parent.fd);os.fsync(parent.fd)
    started=time.monotonic()
    with PrivateDirectory(parent.path/'qualification-matrix') as private:
        intent={'schema':MATRIX_SCHEMA,'origin':str(uuid.uuid4()),'plan_sha256':backend.document['plan_hash'],
            'environment_sha256':digest(canonical(backend.environment)),'cases':list(CASES),
            'backend_directory':str(backend.private.path),'backend_identity':list(backend.private.identity),
            'matrix_identity':list(private.identity)}
        private.create('intent.json',canonical(intent))
        hashes={}
        for case in CASES:
            require(time.monotonic()-started<MATRIX_SECONDS,'linux_qualification_matrix_expired')
            backend._pins()
            private.create(case+'-intent.json',canonical({'schema':MATRIX_SCHEMA,'case':case,'origin':intent['origin']}))
            backend._qualification_observation=_case_observation(case)
            try:
                _observe(backend,case,stage='fault' if case in FAULT_CASES else 'execution')
                proof=_fault(backend,case) if case in FAULT_CASES else _ordinary(backend,case)
                _observe(backend,case,stage='proof')
                _validate_proof(backend,case,proof)
            except DevelopmentError as error:
                try:
                    observation=qualification_diagnostic(backend._qualification_observation)
                    if observation is not None:error._linux_backend_qualification_diagnostic=observation
                except Exception:pass
                raise
            raw=canonical(proof);require(len(raw)<=MAX_PROOF)
            private.create(case+'.json',raw);hashes[case]=digest(raw)
        backend._pins();_real_host(backend)
        evidence={'checks':{case:True for case in CASES},'native_receipts':hashes,'cleanup_confirmed':True}
        receipt={'schema':QUALIFICATION_SCHEMA,'plan_sha256':backend.document['plan_hash'],
            'environment_sha256':digest(canonical(backend.environment)),**evidence}
        require(validate_qualification_evidence(backend,receipt),'linux_qualification_evidence_changed')
        return evidence


def validate_qualification_evidence(backend,receipt):
    """Read-only exact evidence lookup. No authority callback, spawn or signal."""
    try:
        require(type(backend) is _backend_class() and type(receipt) is dict and set(receipt)==
            {'schema','plan_sha256','environment_sha256','checks','native_receipts','cleanup_confirmed'}
            and receipt['schema']==QUALIFICATION_SCHEMA and receipt['plan_sha256']==backend.document['plan_hash']
            and receipt['environment_sha256']==digest(canonical(backend.environment))
            and receipt['cleanup_confirmed'] is True and type(receipt['checks']) is dict
            and set(receipt['checks'])==set(CASES) and all(value is True for value in receipt['checks'].values())
            and type(receipt['native_receipts']) is dict and set(receipt['native_receipts'])==set(CASES))
        backend._pins()
        backend.private.verify()
        binding=_matrix_binding(backend)
        with PrivateDirectory(backend.private.path/'qualification-matrix') as private:
            _,intent=_read_document(private,'intent.json',8192)
            require(intent['origin']==binding['matrix_origin']
                and all(intent[name]==receipt[name] for name in ('plan_sha256','environment_sha256')))
            expected={'intent.json'}|{case+suffix for case in CASES for suffix in ('.json','-intent.json')}
            require(_private_names(private,len(expected))==expected,'linux_qualification_files_changed')
            for case in CASES:
                sha256(receipt['native_receipts'][case])
                _,marker=_read_document(private,case+'-intent.json',8192)
                require(marker=={'schema':MATRIX_SCHEMA,'case':case,'origin':intent['origin']})
                raw,proof=_read_document(private,case+'.json',MAX_PROOF)
                require(digest(raw)==receipt['native_receipts'][case]);_validate_proof(backend,case,proof)
        backend._pins()
        return True
    except (DevelopmentError,OSError,ValueError,TypeError,KeyError,RecursionError):return False
