"""Explicit Linux planner policy; no activation, model, login or account lookup.

Preparation pins current public sources and the controller's own kernel metadata.
Only qualify() can request the reviewed synthetic CLI sandbox canaries. A failed
outside-write check is a permanent failure of that attempt, never a waiver.
"""
from __future__ import annotations
from contextlib import contextmanager
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import threading
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, absolute_directory,
                           canonical, digest, identity, sha256, signature)
from .dev_test_backend import _binary, _host
from .linux_input_snapshot import _chain, _path
from .planner_policy import CHECKS, PlannerPolicyError, _directory, _separate
from .subscription_probe import _run_bounded, sanitized_environment

PROFILE_NAME = 'board-linux-planner-read-v1'
PROFILE_VERSION = 1
ENVIRONMENT_SCHEMA = 'blackoutburst-linux-execution-environment-v1'
PLAN_SCHEMA = 'blackoutburst-linux-planner-policy-v1'
LINUX_CANARY = r'''
import errno,json,socket,sys
p=json.loads(sys.argv[1]);checks={}
def denied(name,fn,allowed):
    try:
        fn();checks[name]=False
    except OSError as e:
        checks[name]=e.errno in allowed
def read(path):
    with open(path,'rb') as f:return f.read(64)
def write(path):
    with open(path,'xb') as f:f.write(b'synthetic planner canary')
def connect():
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
        s.settimeout(1);s.connect(('127.0.0.1',p['port']))
try:checks['inside_read']=read(p['inside'])==b'synthetic planner canary'
except OSError:checks['inside_read']=False
denied('inside_write_denied',lambda:write(p['inside_write']),(errno.EACCES,errno.EPERM,errno.EROFS))
denied('outside_read_denied',lambda:read(p['outside']),(errno.EACCES,errno.EPERM,errno.ENOENT))
denied('outside_write_denied',lambda:write(p['outside_write']),(errno.EACCES,errno.EPERM,errno.ENOENT))
denied('network_loopback_denied',connect,(errno.EACCES,errno.EPERM))
print(json.dumps({'schema':1,'checks':checks},sort_keys=True),flush=True)
raise SystemExit(0 if all(checks.values()) else 78)
'''
MAX_PLAN_BYTES = 64 * 1024
MAX_SOURCE_FILE = 256 * 1024
MAX_SOURCE_TOTAL = 4 * 1024 * 1024
LINUX_EXECUTION_SOURCES = ('__init__.py', 'board_reader.py', 'client_lifecycle.py', 'codex_turn.py', 'context.py', 'context_cache.py', 'decision_memory.py', 'dev_broker.py', 'dev_fence.py', 'dev_journal.py', 'dev_manifest.py', 'dev_planner.py', 'dev_planner_journal.py', 'dev_runtime.py', 'dev_test_backend.py', 'dev_test_supervisor.py', 'development_authorization.py', 'development_cleanup.py', 'development_cli.py', 'development_client.py', 'development_delivery.py', 'development_frame.py', 'development_worker.py', 'event_journal.py', 'family_audit.py', 'family_cli.py', 'family_client.py', 'family_journal.py', 'family_plan.py', 'family_sources.py', 'family_worker.py', 'git_delivery.py', 'git_publication.py', 'git_publication_guardian.py', 'git_workspace.py', 'host_config.py', 'linux_execution_qualification.py', 'linux_execution_state.py', 'linux_input_admission.py', 'linux_input_plan.py', 'linux_input_snapshot.py', 'linux_input_state.py', 'linux_planner_policy.py', 'linux_test_backend.py', 'linux_test_namespace.py', 'linux_test_protocol.py', 'linux_test_supervisor.py', 'message_abandon_protocol.py', 'message_admission_seal.py', 'message_config.py', 'message_journal.py', 'message_scope.py', 'model_policy.py', 'persistent_transport.py', 'planner_policy.py', 'run_store.py', 'runner.py', 'runtime_admission.py', 'runtime_client.py', 'runtime_guard.py', 'runtime_journal.py', 'subscription_probe.py', 'work_journal.py', 'worker_client.py')
NS_NAMES = ('cgroup','ipc','mnt','net','pid','pid_for_children','time','time_for_children','user','uts')
CAP_NAMES = ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')
STATUS_NAMES = ('Uid','Gid','Groups','NoNewPrivs','Seccomp','Seccomp_filters') + CAP_NAMES
ENV_FIELDS = {'schema','host','uid','euid','gid','egid','groups','procfs_magic',
              'cgroups','no_new_privs','seccomp','seccomp_filters','capabilities','label','namespaces'}


def _fail(code='linux_planner_environment_unconfirmed'):
    raise PlannerPolicyError(code) from None


def _number(value, minimum=0, maximum=(1 << 64)-1):
    return type(value) is int and minimum <= value <= maximum


def _text(value, maximum):
    return (type(value) is str and 0 < len(value) <= maximum
            and all(32 <= ord(char) < 127 for char in value))


def _procfs_magic(descriptor):
    # Linux statfs starts with a native long f_type. A generous aligned buffer
    # avoids guessing the remaining architecture-specific structure layout.
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.fstatfs
    function.argtypes, function.restype = (ctypes.c_int,ctypes.c_void_p),ctypes.c_int
    buffer = (ctypes.c_long * 64)()
    if function(descriptor,ctypes.byref(buffer)) != 0: _fail()
    return buffer[0]


def _proc_read(directory, name, maximum):
    descriptor = None
    try:
        descriptor = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _procfs_magic(descriptor) != 0x9fa0: _fail()
        chunks,total = [],0
        while total <= maximum:
            data = os.read(descriptor,min(4096,maximum+1-total))
            if not data: break
            total += len(data);chunks.append(data)
        if total > maximum: _fail()
        return b''.join(chunks)
    finally:
        if descriptor is not None: os.close(descriptor)


def _parse_status(data, *, uid, gid, groups):
    if type(data) is not bytes or not 0 < len(data) <= 32768 or not data.endswith(b'\n'): _fail()
    try:
        selected = {}
        for line in data.decode('ascii').splitlines():
            name,value = line.split(':',1)
            if name in selected or (name.startswith('Cap') and name not in CAP_NAMES): _fail()
            if name in STATUS_NAMES: selected[name] = value.strip()
        if set(selected) != set(STATUS_NAMES): _fail()
        def decimals(value):
            parts = value.split()
            if len(parts) > 64 or any(re.fullmatch('[0-9]{1,10}',item) is None for item in parts): _fail()
            numbers = [int(item) for item in parts]
            if any(number >= (1 << 32) for number in numbers): _fail()
            return numbers
        if decimals(selected['Uid']) != [uid]*4 or decimals(selected['Gid']) != [gid]*4: _fail()
        observed_groups = decimals(selected['Groups'])
        if len(set(observed_groups)) != len(observed_groups) or sorted(observed_groups) != groups: _fail()
        fields = {}
        for name,maximum in (('NoNewPrivs',1),('Seccomp',2),('Seccomp_filters',4096)):
            values = decimals(selected[name])
            if len(values) != 1 or values[0] > maximum: _fail()
            fields[name] = values[0]
        if (fields['Seccomp'] == 2) != (fields['Seccomp_filters'] > 0): _fail()
        caps = {}
        for name in CAP_NAMES:
            if re.fullmatch('[0-9a-f]{16}',selected[name]) is None: _fail()
            caps[name] = int(selected[name],16)
        if any(caps[name] for name in ('CapInh','CapPrm','CapEff','CapAmb')): _fail()
        return {'no_new_privs':fields['NoNewPrivs'],'seccomp':fields['Seccomp'],
                'seccomp_filters':fields['Seccomp_filters'],'capabilities':caps}
    except (UnicodeError,ValueError,OverflowError): _fail()


def _parse_cgroups(data):
    if type(data) is not bytes or not 0 < len(data) <= 4096 or not data.endswith(b'\n'): _fail()
    try:
        result,seen = [],set()
        for line in data.decode('ascii').splitlines():
            if not _text(line,1024): _fail()
            parts = line.split(':',2)
            if len(parts) != 3 or re.fullmatch('[0-9]{1,10}',parts[0]) is None: _fail()
            hierarchy = int(parts[0]);controllers,path = parts[1:]
            if hierarchy >= (1 << 31) or hierarchy in seen or not path.startswith('/'): _fail()
            if (len(path) > 1024 or any(item in ('.','..') for item in path.split('/'))
                    or '//' in path or (path != '/' and path.endswith('/'))): _fail()
            names = controllers.split(',') if controllers else []
            if (len(names) > 64 or len(set(names)) != len(names)
                    or any(re.fullmatch('[A-Za-z0-9_=.-]{1,80}',name) is None for name in names)
                    or (hierarchy == 0) != (not names)): _fail()
            seen.add(hierarchy)
            result.append({'hierarchy':hierarchy,'controllers':sorted(names),'path':path})
        if not 1 <= len(result) <= 64: _fail()
        return sorted(result,key=lambda item:item['hierarchy'])
    except (UnicodeError,ValueError): _fail()


def _observe_identity():
    host = _host()
    if (sys.platform != 'linux' or os.name != 'posix' or type(host) is not dict
            or set(host) != {'system','release','machine'} or host['system'] != 'Linux'
            or host['machine'] != 'x86_64' or not _text(host['release'],256)
            or ctypes.sizeof(ctypes.c_void_p) != 8): _fail()
    uname = os.uname()
    if (uname.sysname != 'Linux' or uname.release != host['release'] or uname.machine != 'x86_64'
            or not _text(uname.nodename,256) or not _text(uname.version,256)): _fail()
    host = {**host,'node':uname.nodename,'kernel_version':uname.version,'pointer_bits':64}
    uid,euid,gid,egid = os.getuid(),os.geteuid(),os.getgid(),os.getegid()
    if (any(not _number(item,1,(1<<32)-1) for item in (uid,euid,gid,egid))
            or uid != euid or gid != egid): _fail()
    groups = os.getgroups()
    if (type(groups) is not list or len(groups) > 64 or any(not _number(item,0,(1<<32)-1) for item in groups)
            or len(set(groups)) != len(groups)): _fail()
    groups = sorted(groups)
    pid,tid = os.getpid(),threading.get_native_id()
    proc = process = task = current = namespace = attr = None
    try:
        proc = os.open('/proc',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        if _procfs_magic(proc) != 0x9fa0: _fail()
        if (os.readlink('self',dir_fd=proc) != str(pid)
                or os.readlink('thread-self',dir_fd=proc) != f'{pid}/task/{tid}'): _fail()
        process = os.open(str(pid),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=proc)
        task = os.open('task',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=process)
        current = os.open(str(tid),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=task)
        if _procfs_magic(current) != 0x9fa0: _fail()
        security = _parse_status(_proc_read(current,'status',32768),uid=uid,gid=gid,groups=groups)
        cgroups = _parse_cgroups(_proc_read(current,'cgroup',4096))
        namespace = os.open('ns',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
        if _procfs_magic(namespace) != 0x9fa0: _fail()
        namespaces = {}
        for name in NS_NAMES:
            link = os.readlink(name,dir_fd=namespace)
            match = re.fullmatch(re.escape(name.split('_for_children')[0])+r':\[([1-9][0-9]{0,19})\]',link)
            if match is None or int(match[1]) >= (1<<64): _fail()
            namespaces[name] = int(match[1])
        try:
            attr = os.open('attr',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            data = _proc_read(attr,'current',1024)
            if data.endswith(b'\n'): data = data[:-1]
            value = data.decode('ascii')
            if not _text(value,1024): _fail()
            label = {'state':'present','value':value}
        except OSError as error:
            if error.errno not in (errno.ENOENT,errno.ENOTSUP): _fail()
            label = {'state':'absent'}
        if (os.getpid() != pid or threading.get_native_id() != tid
                or (os.getuid(),os.geteuid(),os.getgid(),os.getegid()) != (uid,euid,gid,egid)
                or sorted(os.getgroups()) != groups): _fail()
        return {'schema':ENVIRONMENT_SCHEMA,'host':host,'uid':uid,'euid':euid,'gid':gid,'egid':egid,
            'groups':groups,'procfs_magic':0x9fa0,'cgroups':cgroups,'namespaces':namespaces,'label':label,**security}
    except (OSError,UnicodeError,ValueError,TypeError): _fail()
    finally:
        for descriptor in (attr,namespace,current,task,process,proc):
            if descriptor is not None: os.close(descriptor)


def validate_linux_execution_identity(value):
    """Strict metadata codec only; it never establishes host/unit authority."""
    if (type(value) is not dict or set(value) != ENV_FIELDS or value['schema'] != ENVIRONMENT_SCHEMA
            or type(value['host']) is not dict or set(value['host']) != {'system','release','machine','node','kernel_version','pointer_bits'}
            or value['host']['system'] != 'Linux' or value['host']['machine'] != 'x86_64'
            or not _text(value['host']['release'],256)
            or not _text(value['host']['node'],256) or not _text(value['host']['kernel_version'],256)
            or type(value['host']['pointer_bits']) is not int or value['host']['pointer_bits'] != 64
            or any(not _number(value[name],1,(1<<32)-1) for name in ('uid','euid','gid','egid'))
            or value['uid'] != value['euid'] or value['gid'] != value['egid']
            or type(value['groups']) is not list or len(value['groups']) > 64
            or any(not _number(item,0,(1<<32)-1) for item in value['groups'])
            or value['groups'] != sorted(set(value['groups']))
            or type(value['procfs_magic']) is not int or value['procfs_magic'] != 0x9fa0
            or not _number(value['no_new_privs'],0,1) or not _number(value['seccomp'],0,2)
            or not _number(value['seccomp_filters'],0,4096)
            or (value['seccomp'] == 2) != (value['seccomp_filters'] > 0)
            or type(value['capabilities']) is not dict or set(value['capabilities']) != set(CAP_NAMES)
            or any(not _number(item) for item in value['capabilities'].values())
            or any(value['capabilities'][name] for name in ('CapInh','CapPrm','CapEff','CapAmb'))
            or type(value['namespaces']) is not dict or set(value['namespaces']) != set(NS_NAMES)
            or any(not _number(item,1) for item in value['namespaces'].values())): _fail()
    label = value['label']
    if (type(label) is not dict or label.get('state') not in ('present','absent')
            or (label['state']=='absent' and set(label) != {'state'})
            or (label['state']=='present' and (set(label) != {'state','value'} or not _text(label['value'],1024)))): _fail()
    cgroups = value['cgroups']
    if type(cgroups) is not list or not 1 <= len(cgroups) <= 64: _fail()
    lines = []
    for item in cgroups:
        if (type(item) is not dict or set(item) != {'hierarchy','controllers','path'}
                or not _number(item['hierarchy'],0,(1<<31)-1) or type(item['controllers']) is not list
                or any(type(name) is not str for name in item['controllers']) or type(item['path']) is not str): _fail()
        lines.append(str(item['hierarchy'])+':'+','.join(item['controllers'])+':'+item['path'])
    try: encoded = ('\n'.join(lines)+'\n').encode('ascii','strict')
    except UnicodeError: _fail()
    if _parse_cgroups(encoded) != cgroups: _fail()
    if len(canonical(value)) > 12*1024: _fail()
    return value


def linux_execution_identity():
    """Observe only this controller thread's own bounded procfs metadata twice.

    No PID is a durable authority. Namespace/cgroup/label/capability metadata
    binds inherited context, not the undisclosed contents of systemd or seccomp.
    No account, authentication, environment or configuration file is opened.
    """
    try:
        first = validate_linux_execution_identity(_observe_identity())
        if validate_linux_execution_identity(_observe_identity()) != first: _fail()
        return decode(canonical(first))
    except (DevelopmentError,OSError,TypeError,ValueError,UnicodeError): _fail()


def _public_source_at(parent,name,maximum):
    """Read one closed public module, owned by root or this controller UID."""
    if (name not in LINUX_EXECUTION_SOURCES or type(maximum) is not int
            or not 0 <= maximum <= MAX_SOURCE_FILE): _fail('linux_planner_sources_unconfirmed')
    descriptor = None
    try:
        descriptor = os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid not in (0,os.geteuid()) or before.st_mode & 0o022
                or not 0 <= before.st_size <= maximum): _fail('linux_planner_sources_unconfirmed')
        data = b''
        while len(data) <= maximum:
            chunk = os.read(descriptor,min(65536,maximum+1-len(data)))
            if not chunk: break
            data += chunk
        after = os.stat(name,dir_fd=parent,follow_symlinks=False)
        if (len(data) > maximum or signature(before) != signature(os.fstat(descriptor))
                or signature(before) != signature(after)): _fail('linux_planner_sources_unconfirmed')
        return data
    finally:
        if descriptor is not None: os.close(descriptor)


def linux_source_pins():
    """One closed public-source inventory shared by backend and planner.

    Missing future integration modules refuse; their hashes are never invented.
    Source bytes are data to hash, never imported or executed by this function.
    """
    root = str(Path(__file__).parent)
    descriptor = None
    try:
        chain = _chain(root)
        descriptor = absolute_directory(root)
        info = os.fstat(descriptor)
        if (list(identity(info)) != chain[-1]['identity'] or info.st_uid not in (0,os.geteuid())
                or info.st_mode & 0o022): _fail('linux_planner_sources_unconfirmed')
        result,total = {},0
        for name in LINUX_EXECUTION_SOURCES:
            data = _public_source_at(descriptor,name,min(MAX_SOURCE_FILE,MAX_SOURCE_TOTAL-total));total += len(data)
            if total > MAX_SOURCE_TOTAL: _fail('linux_planner_sources_unconfirmed')
            result[name] = {'sha256':digest(data),'bytes':len(data)}
        if _chain(root) != chain: _fail('linux_planner_sources_unconfirmed')
        return {'package':{'path':root,'identity':chain[-1]['identity'],'chain':chain},'files':result}
    except (DevelopmentError,OSError,TypeError,ValueError): _fail('linux_planner_sources_unconfirmed')
    finally:
        if descriptor is not None: os.close(descriptor)


def permission_document(workspace, *, executable):
    # Callers pin this exact regular executable before using the document.
    # The executable contributes only its canonical file path, never its parent.
    workspace,executable = (_path(os.fspath(path)) for path in (workspace,executable))
    return {'filesystem':{':root':'deny',':minimal':'read',':tmpdir':'deny',':slash_tmp':'deny',
                          workspace:'read',executable:'read'},
            'network':{'enabled':False}}


def permission_arguments(workspace, *, executable):
    entries = ','.join(json.dumps(key)+'='+json.dumps(value) for key,value in
                      permission_document(workspace,executable=executable)['filesystem'].items())
    return ['-c','default_permissions='+json.dumps(PROFILE_NAME),'-c',
            'permissions.'+PROFILE_NAME+'={filesystem={'+entries+'},network={enabled=false}}']


def _executable_pin(path):
    value = _path(os.fspath(path))
    parent = absolute_directory(Path(value).parent)
    descriptor = None
    try:
        descriptor = os.open(Path(value).name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        before = os.fstat(descriptor)
        if before.st_mode & 0o6000: _fail('linux_planner_executable_unconfirmed')
        hashed = _binary(value)
        after = os.stat(Path(value).name,dir_fd=parent,follow_symlinks=False)
        if signature(before) != signature(os.fstat(descriptor)) or signature(before) != signature(after):
            _fail('linux_planner_executable_unconfirmed')
        return {'path':value,'sha256':hashed,'signature':list(signature(before)),'parents':_chain(str(Path(value).parent))}
    except (OSError,DevelopmentError): _fail('linux_planner_executable_unconfirmed')
    finally:
        if descriptor is not None: os.close(descriptor)
        os.close(parent)


def _known_process_cleanup(result):
    # _run_bounded always stops/reaps its own Popen session and replaces any
    # failure with cleanup_failed if that observation is not positive.
    failure = result.failure
    return failure is None or (type(failure) is str and failure in
        {'timeout','output_limit','launch_failed','process_io_failed','unsupported_platform'})


def _canary_host_fixtures(owned):
    # A sandbox ENOENT is meaningful only with the original host fixtures held.
    for directory,name,descriptor,pin in owned:
        if (list(signature(os.fstat(descriptor))) != pin
                or list(signature(os.stat(name,dir_fd=directory,follow_symlinks=False))) != pin):
            _fail('linux_planner_policy_changed')


def prepare_linux_planner_policy(directory, *, workspace, checkout, executable,
                                 cli_version, interpreter='/usr/bin/python3'):
    """New private plan only. No subprocess, native CLI, model or grant probe."""
    environment = linux_execution_identity()
    if type(cli_version) is not str or re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}',cli_version) is None:
        _fail('linux_planner_cli_version_invalid')
    directory,workspace,checkout = (_path(os.fspath(path)) for path in (directory,workspace,checkout))
    _directory(workspace,empty=True);_directory(checkout);_separate(workspace,checkout)
    parent = absolute_directory(Path(directory).parent)
    try:
        try: os.mkdir(Path(directory).name,0o700,dir_fd=parent)
        except OSError: _fail('linux_planner_fresh_directory_required')
        os.fsync(parent)
        expected_identity = identity(os.stat(Path(directory).name,dir_fd=parent,follow_symlinks=False))
        with PrivateDirectory(directory) as private:
            if private.identity != expected_identity: _fail('linux_planner_policy_changed')
            for path in (directory,str(Path(executable).parent),str(Path(interpreter).parent),str(Path(__file__).parent)):
                _separate(workspace,path)
            plan = {'schema':PLAN_SCHEMA,'profile_version':PROFILE_VERSION,'origin':str(uuid.uuid4()),
                'workspace':workspace,'workspace_identity':_directory(workspace,empty=True),
                'workspace_chain':_chain(workspace),'checkout':checkout,'checkout_identity':_directory(checkout),
                'checkout_chain':_chain(checkout),'private_identity':list(private.identity),'private_chain':_chain(directory),
                'executable':os.fspath(executable),'executable_pin':_executable_pin(executable),
                'cli_version':cli_version,'cli_sha256':_binary(executable),
                'interpreter':os.fspath(interpreter),'interpreter_pin':_executable_pin(interpreter),
                'policy':permission_document(workspace,executable=executable),'canary_sha256':digest(LINUX_CANARY.encode()),
                'source_pins':linux_source_pins(),'environment':environment,'environment_sha256':digest(canonical(environment))}
            if plan['cli_sha256'] != plan['executable_pin']['sha256'] or linux_execution_identity() != environment:
                _fail('linux_planner_policy_changed')
            encoded = canonical(plan)
            if len(encoded) > MAX_PLAN_BYTES: _fail('linux_planner_plan_too_large')
            private.create('planner-plan.json',encoded)
            private.create('planner-qualification.json',canonical(None))
            private.create('planner.lock',b'')
            return digest(encoded)
    finally: os.close(parent)


class LinuxPlannerPolicy:
    """Concrete Linux-only policy. JSON never grants a current qualification."""
    def __init__(self,directory):
        self.private = PrivateDirectory(directory)
        self._lock_fd = None
        self._thread_lock = threading.Lock()
        self._pid,self._thread = os.getpid(),threading.current_thread()
        self._invalid = False
        try:
            data = self.private.read('planner-plan.json',MAX_PLAN_BYTES)
            self._plan = decode(data);self._fingerprint = digest(data)
            if canonical(self._plan) != data: _fail('linux_planner_policy_changed')
            self._lock_fd = os.open('planner.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=self.private.fd)
            self._lock_identity = identity(os.fstat(self._lock_fd))
            self.private.validate_file(self._lock_fd)
            self._pins()
        except BaseException:
            self.close();raise

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def __reduce_ex__(self,protocol):raise TypeError('Linux policy handles cannot be serialized')
    def __copy__(self):raise TypeError('Linux policy handles cannot be copied')
    def __deepcopy__(self,memo):raise TypeError('Linux policy handles cannot be copied')
    @property
    def fingerprint(self):return self._fingerprint
    @property
    def workspace(self):return self._plan['workspace']
    @property
    def workspace_identity(self):return tuple(self._plan['workspace_identity'])
    @property
    def executable(self):return self._plan['executable']
    @property
    def cli_sha256(self):return self._plan['cli_sha256']
    @property
    def environment(self):return decode(canonical(self._plan['environment']))
    @property
    def environment_sha256(self):return self._plan['environment_sha256']

    def close(self):
        self._invalid = True
        if self._lock_fd is not None:
            try:
                if identity(os.fstat(self._lock_fd)) == getattr(self,'_lock_identity',None): os.close(self._lock_fd)
            except OSError: pass
            self._lock_fd = None
        if self.private.fd is not None:
            try:
                if identity(os.fstat(self.private.fd)) != self.private.identity: self.private.fd = None
            except OSError: self.private.fd = None
            self.private.close()

    def _pins(self, *, fixture=None):
        p = self._plan
        expected = {'schema','profile_version','origin','workspace','workspace_identity','workspace_chain',
            'checkout','checkout_identity','checkout_chain','private_identity','private_chain','executable',
            'executable_pin','cli_version','cli_sha256','interpreter','interpreter_pin','policy',
            'canary_sha256','source_pins','environment','environment_sha256'}
        try:
            if (self._invalid or self._pid != os.getpid() or self._thread is not threading.current_thread()
                    or type(p) is not dict or set(p) != expected or p['schema'] != PLAN_SCHEMA
                    or type(p['profile_version']) is not int or p['profile_version'] != PROFILE_VERSION
                    or type(p['origin']) is not str or str(uuid.UUID(p['origin'])) != p['origin']
                    or type(p['cli_version']) is not str or re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}',p['cli_version']) is None
                    or canonical(p['policy']) != canonical(permission_document(p['workspace'],executable=p['executable']))
                    or p['canary_sha256'] != digest(LINUX_CANARY.encode())): _fail('linux_planner_policy_changed')
            validate_linux_execution_identity(p['environment'])
            sha256(p['cli_sha256']);sha256(p['environment_sha256'])
            self.private.verify()
            if (digest(self.private.read('planner-plan.json',MAX_PLAN_BYTES)) != self.fingerprint
                    or canonical(_directory(p['workspace'],empty=fixture is None,private=True)) != canonical(p['workspace_identity'])
                    or canonical(_directory(p['checkout'])) != canonical(p['checkout_identity'])
                    or canonical(list(self.private.identity)) != canonical(p['private_identity'])
                    or canonical(_chain(p['workspace'])) != canonical(p['workspace_chain']) or canonical(_chain(p['checkout'])) != canonical(p['checkout_chain'])
                    or canonical(_chain(str(self.private.path))) != canonical(p['private_chain'])
                    or canonical(_executable_pin(p['executable'])) != canonical(p['executable_pin'])
                    or canonical(_executable_pin(p['interpreter'])) != canonical(p['interpreter_pin'])
                    or p['cli_sha256'] != p['executable_pin']['sha256']
                    or canonical(linux_source_pins()) != canonical(p['source_pins'])
                    or linux_execution_identity() != p['environment']
                    or digest(canonical(p['environment'])) != p['environment_sha256']): _fail('linux_planner_policy_changed')
            if fixture is not None and set(os.listdir(p['workspace'])) != {fixture}: _fail('linux_planner_policy_changed')
            _separate(p['workspace'],p['checkout']);_separate(p['workspace'],self.private.path)
            for path in (Path(p['executable']).parent,Path(p['interpreter']).parent,Path(__file__).parent):
                _separate(p['workspace'],path)
        except (DevelopmentError,OSError,ValueError,TypeError,KeyError):
            self._invalid = True
            _fail('linux_planner_policy_changed')

    @contextmanager
    def _locked(self):
        if self._invalid or self._lock_fd is None or self.private.fd is None: _fail('linux_planner_policy_closed')
        if self._pid != os.getpid() or self._thread is not threading.current_thread(): _fail('linux_planner_policy_changed')
        if not self._thread_lock.acquire(blocking=False): _fail('linux_planner_policy_busy')
        acquired = False
        try:
            self.private.verify();self.private.validate_file(self._lock_fd)
            if (identity(os.fstat(self._lock_fd)) != self._lock_identity
                    or identity(os.stat('planner.lock',dir_fd=self.private.fd,follow_symlinks=False)) != self._lock_identity):
                _fail('linux_planner_policy_changed')
            try: fcntl.flock(self._lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: _fail('linux_planner_policy_busy')
            acquired = True
            yield
        except OSError: _fail('linux_planner_policy_unavailable')
        finally:
            try:
                if acquired:
                    try:
                        unchanged = identity(os.fstat(self._lock_fd)) == self._lock_identity
                    except OSError:
                        unchanged = False
                    if not unchanged:
                        self._invalid = True;self._lock_fd = None
                        _fail('linux_planner_policy_changed')
                    try: fcntl.flock(self._lock_fd,fcntl.LOCK_UN)
                    except OSError:
                        self._invalid = True
                        _fail('linux_planner_policy_unavailable')
            finally: self._thread_lock.release()

    def validate_for(self,executable,workspace):
        with self._locked():
            self._pins()
            if os.fspath(executable) != self.executable or os.fspath(workspace) != self.workspace:
                _fail('linux_planner_policy_scope_mismatch')
            value = decode(self.private.read('planner-qualification.json',8192))
            expected = {'schema','fingerprint','environment_sha256','source_pins_sha256','attempt_sha256','checks','cleanup_confirmed'}
            if (type(value) is not dict or set(value) != expected or value['schema'] != PLAN_SCHEMA
                    or value['fingerprint'] != self.fingerprint or value['environment_sha256'] != self.environment_sha256
                    or value['source_pins_sha256'] != digest(canonical(self._plan['source_pins']))
                    or type(value['checks']) is not dict or set(value['checks']) != CHECKS
                    or any(item is not True for item in value['checks'].values()) or value['cleanup_confirmed'] is not True):
                _fail('linux_planner_policy_unqualified')
            try:
                marker = decode(self.private.read('planner-attempt.json',8192))
                if (type(marker) is not dict or set(marker) != {'schema','nonce','fingerprint','environment_sha256','source_pins_sha256'}
                        or marker['schema'] != PLAN_SCHEMA or type(marker['nonce']) is not str
                        or str(uuid.UUID(marker['nonce'])) != marker['nonce']
                        or any(marker[name] != value[name] for name in ('fingerprint','environment_sha256','source_pins_sha256'))
                        or digest(canonical(marker)) != value['attempt_sha256']): _fail('linux_planner_policy_unqualified')
            except (DevelopmentError,OSError,ValueError,TypeError,KeyError): _fail('linux_planner_policy_unqualified')
            return permission_arguments(self.workspace,executable=self.executable)

    def qualify(self):
        """ONE explicit synthetic sandbox attempt; never exec/login/model.

        A failed canary is recorded as a refusal with its actual boolean checks.
        Unknown files/identities leave fixtures in place and cleanup false. No
        automatic retry, rmtree, historical receipt adoption or waiver exists.
        """
        with self._locked():
            self._pins()
            if decode(self.private.read('planner-qualification.json',8192)) is not None:
                _fail('linux_planner_attempt_not_reusable')
            marker = {'schema':PLAN_SCHEMA,'nonce':str(uuid.uuid4()),'fingerprint':self.fingerprint,
                'environment_sha256':self.environment_sha256,
                'source_pins_sha256':digest(canonical(self._plan['source_pins']))}
            try: self.private.create('planner-attempt.json',canonical(marker))
            except DevelopmentError: _fail('linux_planner_attempt_not_reusable')
            marker_hash = digest(canonical(marker))
            checks,reason,process_cleanup = {},None,True
            listener = inside_fd = outside_fd = workspace_fd = None
            owned = []
            cleanup = False
            fixture = 'planner-canary-'+uuid.uuid4().hex
            outside = 'outside-canary-'+uuid.uuid4().hex
            private_expected = {'planner-plan.json','planner-qualification.json','planner.lock','planner-attempt.json'}
            try:
                env = sanitized_environment()
                version = _run_bounded([self.executable,'--version'],self.workspace,5,4096,env)
                process_cleanup = _known_process_cleanup(version)
                if (type(version.returncode) is not int or type(version.stdout) is not bytes or version.failure or version.returncode != 0
                        or version.stdout.decode('utf-8','strict').strip() != self._plan['cli_version']):
                    _fail('linux_planner_cli_version_changed')
                workspace_fd = absolute_directory(self.workspace)
                if list(identity(os.fstat(workspace_fd))) != self._plan['workspace_identity']:
                    _fail('linux_planner_policy_changed')
                inside_fd = os.open(fixture,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=workspace_fd)
                owned.append((workspace_fd,fixture,inside_fd,list(signature(os.fstat(inside_fd)))))
                PrivateDirectory._write(inside_fd,b'synthetic planner canary')
                owned[-1] = (workspace_fd,fixture,inside_fd,list(signature(os.fstat(inside_fd))))
                self.private.create(outside,b'synthetic planner canary')
                outside_fd = os.open(outside,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.private.fd)
                self.private.validate_file(outside_fd)
                owned.append((self.private.fd,outside,outside_fd,list(signature(os.fstat(outside_fd)))))
                private_expected.add(outside)
                listener = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
                listener.bind(('127.0.0.1',0));listener.listen(1)
                self._pins(fixture=fixture)
                payload = {'inside':self.workspace+'/'+fixture,'inside_write':self.workspace+'/'+fixture+'-write',
                    'outside':str(self.private.path/outside),'outside_write':str(self.private.path/(outside+'-write')),
                    'port':listener.getsockname()[1]}
                command = [self.executable,*permission_arguments(self.workspace,executable=self.executable),
                    'sandbox','--permission-profile',PROFILE_NAME,'--include-managed-config',
                    '--',self._plan['interpreter'],'-I','-S','-c',LINUX_CANARY,canonical(payload).decode()]
                _canary_host_fixtures(owned)
                run = _run_bounded(command,self.workspace,15,8192,env)
                process_cleanup = process_cleanup and _known_process_cleanup(run)
                _canary_host_fixtures(owned)
                if type(run.stdout) is not bytes: _fail('linux_planner_canary_output_invalid')
                value = decode(run.stdout)
                if (type(value) is not dict or set(value) != {'schema','checks'}
                        or type(value['schema']) is not int or value['schema'] != 1
                        or type(value['checks']) is not dict or set(value['checks']) != CHECKS
                        or any(type(item) is not bool for item in value['checks'].values())):
                    _fail('linux_planner_canary_output_invalid')
                checks = value['checks']
                # The exact predefined write destinations must remain absent;
                # a claimed denial never overrides an observed created file.
                for directory,name,check in ((workspace_fd,fixture+'-write','inside_write_denied'),
                                             (self.private.fd,outside+'-write','outside_write_denied')):
                    try: os.stat(name,dir_fd=directory,follow_symlinks=False)
                    except FileNotFoundError: continue
                    checks[check] = False
                    _fail('linux_planner_canary_created_file')
                if type(run.returncode) is not int or run.failure or run.returncode != 0 or any(item is not True for item in checks.values()):
                    _fail('linux_planner_native_policy_unqualified')
            except (DevelopmentError,OSError,ValueError,UnicodeError) as error:
                allowed = {'linux_planner_cli_version_changed','linux_planner_policy_changed',
                    'linux_planner_canary_output_invalid','linux_planner_canary_created_file',
                    'linux_planner_native_policy_unqualified'}
                reason = error.code if isinstance(error,DevelopmentError) and error.code in allowed else 'linux_planner_native_policy_unqualified'
            finally:
                if listener is not None: listener.close()
                # Validate the WHOLE predefined set before removing any file.
                # Unexpected files (including a successful denied-write canary)
                # are never enumerated into ownership or silently deleted.
                try:
                    self.private.verify()
                    if (_chain(str(self.private.path)) != self._plan['private_chain']
                            or set(os.listdir(self.private.fd)) != private_expected):
                        _fail('linux_planner_qualification_cleanup_unconfirmed')
                    if workspace_fd is None:
                        if owned: _fail('linux_planner_qualification_cleanup_unconfirmed')
                        if _directory(self.workspace,empty=True,private=True) != self._plan['workspace_identity']:
                            _fail('linux_planner_qualification_cleanup_unconfirmed')
                    else:
                        if (list(identity(os.fstat(workspace_fd))) != self._plan['workspace_identity']
                                or _chain(self.workspace) != self._plan['workspace_chain']
                                or set(os.listdir(workspace_fd)) != ({fixture} if inside_fd is not None else set())):
                            _fail('linux_planner_qualification_cleanup_unconfirmed')
                    for directory,name,descriptor,pin in owned:
                        if (list(signature(os.fstat(descriptor))) != pin
                                or list(signature(os.stat(name,dir_fd=directory,follow_symlinks=False))) != pin):
                            _fail('linux_planner_qualification_cleanup_unconfirmed')
                    for directory,name,descriptor,pin in owned:
                        if list(signature(os.stat(name,dir_fd=directory,follow_symlinks=False))) != pin:
                            _fail('linux_planner_qualification_cleanup_unconfirmed')
                        os.unlink(name,dir_fd=directory);os.fsync(directory)
                    if (set(os.listdir(self.private.fd)) != private_expected - {outside}
                            or (workspace_fd is not None and os.listdir(workspace_fd))):
                        _fail('linux_planner_qualification_cleanup_unconfirmed')
                    cleanup = True
                except (DevelopmentError,OSError,ValueError,TypeError):
                    cleanup = False
                finally:
                    for descriptor in (inside_fd,outside_fd):
                        if descriptor is not None:
                            matching = [pin for _,_,saved,pin in owned if saved == descriptor]
                            try:
                                if matching and list(identity(os.fstat(descriptor))) == matching[0][:2]: os.close(descriptor)
                            except OSError: pass
                    if workspace_fd is not None:
                        try:
                            if list(identity(os.fstat(workspace_fd))) == self._plan['workspace_identity']: os.close(workspace_fd)
                        except OSError: pass
            cleanup = cleanup and process_cleanup
            if not cleanup: reason = 'linux_planner_qualification_cleanup_unconfirmed'
            if reason is None:
                try:
                    self._pins()
                    if self.private.read('planner-attempt.json',8192) != canonical(marker):
                        _fail('linux_planner_policy_changed')
                    if self.private.read('planner-qualification.json',8192) != canonical(None):
                        _fail('linux_planner_policy_changed')
                except (DevelopmentError,OSError): reason = 'linux_planner_policy_changed'
            if reason is not None:
                refusal = {'schema':'blackoutburst-linux-planner-refusal-v1','fingerprint':self.fingerprint,
                    'attempt_sha256':marker_hash,'checks':checks,'cleanup_confirmed':cleanup,'reason':reason,
                    'environment_sha256':self.environment_sha256,'result':'refused'}
                # Only replace the exact initial qualification document; do not
                # overwrite an unknown/replaced file while reporting failure.
                try:
                    if self.private.read('planner-qualification.json',8192) == canonical(None):
                        self.private.replace('planner-qualification.json',canonical(refusal))
                except (DevelopmentError,OSError): pass
                _fail(reason)
            receipt = {'schema':PLAN_SCHEMA,'fingerprint':self.fingerprint,
                'environment_sha256':self.environment_sha256,
                'source_pins_sha256':digest(canonical(self._plan['source_pins'])),
                'attempt_sha256':marker_hash,'checks':checks,'cleanup_confirmed':True}
            self.private.replace('planner-qualification.json',canonical(receipt))
            return decode(canonical(receipt))
