"""Opt-in, locally qualified single-process named Python tests; no model calls.

This backend is not registered by a worker or CLI. A controller creates its
immutable plan and explicitly qualifies this host before it can run a test.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import selectors
import signal
import socket
import stat
import subprocess
import threading
import time
import uuid
import unicodedata

from .dev_broker import TestRequest
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, RepositoryFiles, authorize, canonical,
                           digest, opaque, relative_path, sha256, validate_manifest, check_content, identity)


PROFILE_VERSION = 2
PROFILE_NAME = 'board-named-python-v2'
MAX_SCRIPT_BYTES = 65536
HEADER = b'BB_NAMED_TEST_V1 '
MAX_DOCUMENT = 128 * 1024
REQUIRED_CHECKS = {'inside_read', 'inside_write', 'outside_read', 'outside_write',
                   'network_loopback', 'fork', 'spawn', 'session', 'process_group', 'nproc_raise'}
PRELAUNCH_REASONS = frozenset({'permission_revoked', 'test_backend_unqualified',
    'backend_executable_changed', 'trusted_script_changed', 'trusted_script_access_refused',
    'trusted_script_directory_refused', 'workspace_identity_changed',
    'supervisor_controller_changed', 'supervisor_workspace_overlap',
    'test_backend_busy', 'test_backend_access_failed', 'native_prelaunch_failed',
    'supervisor_authorization_expired', 'supervisor_clock_changed', 'supervisor_control_invalid'})


def _prelaunch_reason(error):
    code = getattr(error, 'code', None)
    return code if type(code) is str and code in PRELAUNCH_REASONS else 'native_prelaunch_failed'

# This is trusted controller code, not a script or argument supplied by a model.
# The same process verifies the actual sandbox and hard limits immediately
# before running one reviewed script in that same interpreter. No shell is used.
WRAPPER = r'''
import base64,errno,hashlib,json,os,resource,select,signal,socket,sys
p=json.loads(sys.argv[1])
if os.getsid(0) != os.getpid():
    os.setsid()
resource.setrlimit(resource.RLIMIT_NPROC,(1,1))
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))
resource.setrlimit(resource.RLIMIT_CPU,(p['timeout']+1,p['timeout']+1))
checks={}
def denied(name,fn,allowed):
    try:
        fn()
        checks[name]=False
    except OSError as e:
        checks[name]=e.errno in allowed
def read(path):
    with open(path,'rb') as f:
        return f.read(64)
def write(path):
    with open(path,'w') as f:
        f.write('synthetic canary')
def connect():
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
        s.settimeout(1)
        s.connect(('127.0.0.1',p['port']))
def fork():
    child=os.fork()
    if child == 0:
        os._exit(0)
    os.waitpid(child,0)
def spawn():
    child=os.posix_spawn('/usr/bin/true',['/usr/bin/true'],{})
    os.waitpid(child,0)
try:
    checks['inside_read']=read(p['inside']) == b'synthetic canary'
except OSError:
    checks['inside_read']=False
try:
    write(p['inside_write'])
    checks['inside_write']=True
    os.unlink(p['inside_write'])
except OSError:
    checks['inside_write']=False
denied('outside_read',lambda:read(p['outside']),{errno.EPERM,errno.EACCES})
denied('outside_write',lambda:write(p['outside_write']),{errno.EPERM,errno.EACCES})
denied('network_loopback',connect,{errno.EPERM,errno.EACCES})
denied('fork',fork,{errno.EAGAIN,errno.EPERM,errno.EACCES})
denied('spawn',spawn,{errno.EAGAIN,errno.EPERM,errno.EACCES})
denied('session',os.setsid,{errno.EPERM})
denied('process_group',lambda:os.setpgid(0,0),{errno.EPERM})
if p.get('outside_signal_pid') is not None:
    denied('outside_signal',lambda:os.kill(p['outside_signal_pid'],signal.SIGUSR1),{errno.EPERM,errno.EACCES})
try:
    resource.setrlimit(resource.RLIMIT_NPROC,(2,2))
    checks['nproc_raise']=False
except (ValueError,OSError):
    checks['nproc_raise']=True
ok=all(checks.values()) and resource.getrlimit(resource.RLIMIT_NPROC)==(1,1)
header={'pid':os.getpid(),'pgid':os.getpgrp(),'sid':os.getsid(0),'checks':checks,'ok':ok}
print('BB_NAMED_TEST_V1 '+json.dumps(header,sort_keys=True),flush=True)
if not ok:
    os._exit(78)
ready,_,_=select.select([0],[],[],5)
if not ready or os.read(0,1) != b'G':
    os._exit(79)
os.close(0)
null=os.open('/dev/null',os.O_RDONLY)
if null != 0:
    os.dup2(null,0)
    os.close(null)
if p['qualify']:
    os._exit(0)
os.environ.clear()
os.environ.update(p['environment'])
source=base64.b64decode(p['script_base64'],validate=True)
if len(source)>65536 or hashlib.sha256(source).hexdigest()!=p['script_sha256']:
    os._exit(80)
sys.argv[:]=[p['script']]
code=compile(source,p['script'],'exec',dont_inherit=True)
exec(code,{'__name__':'__main__','__file__':p['script'],'__package__':None,'__cached__':None})
'''


def _host():
    return {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()}


def _binary(path):
    """Pin a regular executable without reading configuration or auth files."""
    value = os.fspath(path)
    if type(value) is not str or not value.startswith('/') or str(Path(value)) != value:
        raise DevelopmentError('invalid_backend_executable')
    descriptor = None
    try:
        # Canonical path components cannot redirect through a symbolic link.
        from .dev_manifest import absolute_directory
        parent = absolute_directory(Path(value).parent)
        try:
            descriptor = os.open(Path(value).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        finally:
            os.close(parent)
        before = os.fstat(descriptor)
        immutable_system_links = (before.st_uid == 0 and os.geteuid() != 0
            and not os.access(value, os.W_OK, effective_ids=True))
        if (not stat.S_ISREG(before.st_mode) or (before.st_nlink != 1 and not immutable_system_links)
                or not before.st_mode & 0o111
                or before.st_mode & 0o022 or before.st_uid not in (0, os.geteuid())
                or before.st_size > 512 * 1024 * 1024):
            raise DevelopmentError('invalid_backend_executable')
        hashed = hashlib.sha256()
        total = 0
        while True:
            data = os.read(descriptor, 1024 * 1024)
            if not data:
                break
            total += len(data)
            if total > 512 * 1024 * 1024:
                raise DevelopmentError('backend_executable_changed')
            hashed.update(data)
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise DevelopmentError('backend_executable_changed')
        return hashed.hexdigest()
    except OSError:
        raise DevelopmentError('backend_executable_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def permission_document(root):
    return {'filesystem': {':root': 'deny', ':minimal': 'read', ':tmpdir': 'deny',
                           ':slash_tmp': 'deny', root: 'write'}, 'network': {'enabled': False}}


def _toml_policy(root):
    document = permission_document(root)
    entries = ','.join(json.dumps(key) + '=' + json.dumps(value)
                       for key, value in document['filesystem'].items())
    return '{filesystem={' + entries + '},network={enabled=false}}'


@contextmanager
def _script_parent(files, path):
    parts=relative_path(path);files.verify();fd=os.dup(files.fd)
    parents=[list(files.identity)]
    try:
        for part in parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child;info=os.fstat(fd)
            if info.st_uid!=os.geteuid() or info.st_mode&0o022:
                raise DevelopmentError('trusted_script_directory_refused')
            parents.append(list(identity(info)))
        yield fd,parts[-1],parents
    except OSError:
        raise DevelopmentError('trusted_script_access_refused') from None
    finally:os.close(fd)


def _script_read(files,path):
    with _script_parent(files,path) as (parent,name,parents):
        data,signature=files.read_at(parent,name,MAX_SCRIPT_BYTES)
        check_content(data)
        with _script_parent(files,path) as (fresh,_,current):
            if current!=parents or identity(os.fstat(parent))!=identity(os.fstat(fresh)):
                raise DevelopmentError('trusted_script_changed')
        files.verify()
        return data,{'sha256':digest(data),'bytes':len(data),'identity':list(signature[:2]),'parents':parents}


def _script_pin(pin,path):
    if (type(pin) is not dict or set(pin)!={'sha256','bytes','identity','parents'}
            or type(pin['bytes']) is not int or not 0<=pin['bytes']<=MAX_SCRIPT_BYTES
            or type(pin['identity']) is not list or len(pin['identity'])!=2
            or type(pin['parents']) is not list or len(pin['parents'])!=len(relative_path(path))
            or any(type(pair) is not list or len(pair)!=2 or any(type(v) is not int or v<0 for v in pair)
                   for pair in [pin['identity'],*pin['parents']])):
        raise DevelopmentError('invalid_trusted_script_pin')
    sha256(pin['sha256'])


def _disjoint_scripts(plan,manifest):
    """Separate trusted test code from every model-patchable path and alias."""
    normalized={unicodedata.normalize('NFC',path).casefold() for path in manifest['files']}
    with _repository(plan) as files:
        identities=set()
        for path in manifest['files']:
            try:
                with files.parent(path) as (parent,name):
                    info=os.stat(name,dir_fd=parent,follow_symlinks=False)
                    identities.add(identity(info))
            except OSError:
                raise DevelopmentError('trusted_script_access_refused') from None
        for test in plan['tests'].values():
            if (unicodedata.normalize('NFC',test['script']).casefold() in normalized
                    or tuple(test['script_pin']['identity']) in identities):
                raise DevelopmentError('trusted_script_in_patch_caps')


@contextmanager
def _repository(plan):
    files=RepositoryFiles(plan['root'],plan['root_identity'])
    try:yield files
    finally:files.close()


def pin_test_plan(*, root, codex, cli_version, scripts, executable='/usr/bin/python3'):
    """Trusted controller step; creates no process and performs no model call.

    scripts maps each approved test name to {script, timeout_seconds,
    max_output_bytes}. The paths must be existing trusted files outside the model manifest
    allowlist when the backend is bound later. First backend supports Darwin only.
    """
    if _host()['system'] != 'Darwin' or os.getuid() == 0:
        raise DevelopmentError('native_test_platform_unqualified')
    if (type(cli_version) is not str or not 1 <= len(cli_version) <= 100
            or any(ord(char) < 32 for char in cli_version)
            or type(scripts) is not dict or not 1 <= len(scripts) <= 32):
        raise DevelopmentError('invalid_test_plan')
    files = RepositoryFiles(root)
    try:
        tests = {}
        for name, suite in scripts.items():
            opaque(name, 'invalid_test_name', 64)
            if type(suite) is not dict or set(suite) != {'script', 'timeout_seconds', 'max_output_bytes'}:
                raise DevelopmentError('invalid_test_plan')
            relative_path(suite['script'])
            if (type(suite['timeout_seconds']) is not int or not 1 <= suite['timeout_seconds'] <= 600
                    or type(suite['max_output_bytes']) is not int or not 0 <= suite['max_output_bytes'] <= 1048576):
                raise DevelopmentError('invalid_test_plan')
            _,pin=_script_read(files,suite['script'])
            tests[name] = {**suite,'script_pin':pin}
        plan = {'schema': 2, 'profile_version': PROFILE_VERSION, 'host': _host(),
            'root': files.root, 'root_identity': list(files.identity), 'codex': os.fspath(codex),
            'codex_sha256': _binary(codex), 'cli_version': cli_version,
            'executable': os.fspath(executable), 'executable_sha256': _binary(executable),
            'wrapper_sha256': digest(WRAPPER.encode()), 'policy': permission_document(files.root),
            'tests': tests, 'environment': {'LANG': 'C', 'PATH': '/usr/bin:/bin'}}
        return plan
    finally:
        files.close()


def backend_id(plan):
    return 'native-python-v2-' + digest(canonical(plan))[:40]


def _manifest_binding(plan,manifest):
    expected=backend_id(plan)
    if (plan['root']!=manifest['root'] or plan['root_identity']!=manifest['root_identity']
            or set(plan['tests'])!={test['name'] for test in manifest['tests']}):
        raise DevelopmentError('test_plan_scope_mismatch')
    for suite in manifest['tests']:
        test=plan['tests'][suite['name']]
        if (suite['backend_id']!=expected or any(suite[key]!=test[key]
                for key in ('timeout_seconds','max_output_bytes'))):
            raise DevelopmentError('test_plan_scope_mismatch')


def prepare_backend(directory, *, manifest, plan):
    """Bind an immutable controller plan to the frozen development manifest."""
    validate_manifest(manifest)
    _validate_plan(plan)
    _manifest_binding(plan,manifest)
    expected=backend_id(plan)
    _disjoint_scripts(plan,manifest)
    with _repository(plan) as files:
        for test in plan['tests'].values():
            if _script_read(files,test['script'])[1]!=test['script_pin']:
                raise DevelopmentError('trusted_script_changed')
    if Path(directory).absolute().is_relative_to(Path(plan['root']).absolute()):
        raise DevelopmentError('journal_inside_workspace')
    document = {'schema': 2, 'manifest_hash': digest(canonical(manifest)),
                'plan': plan, 'backend_id': expected, 'plan_hash': digest(canonical(plan))}
    encoded=canonical(document)
    if len(encoded)>MAX_DOCUMENT:raise DevelopmentError('test_plan_too_large')
    with PrivateDirectory(directory, create=True) as private:
        private.create('plan.json', encoded)
        private.create('qualification.json', canonical(None))


def _validate_plan(plan):
    if (type(plan) is not dict or set(plan) != {'schema', 'profile_version', 'host', 'root',
            'root_identity', 'codex', 'codex_sha256', 'cli_version', 'executable', 'executable_sha256',
            'wrapper_sha256', 'policy', 'tests', 'environment'}
            or type(plan['schema']) is not int or plan['schema'] != 2
            or type(plan['profile_version']) is not int or plan['profile_version'] != PROFILE_VERSION
            or plan['host'] != _host() or plan['host']['system'] != 'Darwin'
            or type(plan['root']) is not str or not plan['root'].startswith('/')
            or type(plan['root_identity']) is not list or len(plan['root_identity']) != 2
            or any(type(item) is not int or item < 0 for item in plan['root_identity'])
            or plan['wrapper_sha256'] != digest(WRAPPER.encode())
            or type(plan['cli_version']) is not str or not 1 <= len(plan['cli_version']) <= 100
            or any(ord(char) < 32 for char in plan['cli_version'])
            or any(type(plan[key]) is not str or not plan[key].startswith('/')
                or str(Path(plan[key])) != plan[key] or '..' in Path(plan[key]).parts
                for key in ('codex', 'executable'))
            or plan['policy'] != permission_document(plan['root'])
            or plan['environment'] != {'LANG': 'C', 'PATH': '/usr/bin:/bin'}
            or type(plan['tests']) is not dict or not 1 <= len(plan['tests']) <= 32):
        raise DevelopmentError('invalid_test_plan')
    sha256(plan['codex_sha256'])
    sha256(plan['executable_sha256'])
    for name, test in plan['tests'].items():
        opaque(name, 'invalid_test_name', 64)
        if (type(test) is not dict or set(test) != {'script', 'timeout_seconds', 'max_output_bytes','script_pin'}
                or type(test['timeout_seconds']) is not int or not 1 <= test['timeout_seconds'] <= 600
                or type(test['max_output_bytes']) is not int or not 0 <= test['max_output_bytes'] <= 1048576):
            raise DevelopmentError('invalid_test_plan')
        relative_path(test['script']);_script_pin(test['script_pin'],test['script'])
        if test['script_pin']['parents'][0]!=plan['root_identity']:
            raise DevelopmentError('invalid_trusted_script_pin')
    if len(canonical(plan))>MAX_DOCUMENT:raise DevelopmentError('test_plan_too_large')


def _receipt(status='needs_review', *, cleanup=False, output=b''):
    return {'status': status, 'cleanup_confirmed': cleanup,
            'output_bytes': len(output), 'output_sha256': digest(output)}


class NativeNamedPythonBackend:
    def __init__(self, directory, *, manifest, authorize_scope):
        validate_manifest(manifest)
        if not callable(authorize_scope):
            raise DevelopmentError('invalid_controller')
        self.private = PrivateDirectory(directory)
        self._lock = None
        self._thread_lock = threading.Lock()
        self._active_admission = None
        try:
            self.document = decode(self.private.read('plan.json', MAX_DOCUMENT))
            if (type(self.document) is not dict or set(self.document) != {
                    'schema', 'manifest_hash', 'plan', 'backend_id', 'plan_hash'}
                    or type(self.document['schema']) is not int or self.document['schema'] != 2
                    or self.document['manifest_hash'] != digest(canonical(manifest))):
                raise DevelopmentError('invalid_test_plan')
            self.plan = self.document['plan']
            _validate_plan(self.plan)
            if (self.document['plan_hash'] != digest(canonical(self.plan))
                    or self.document['backend_id'] != backend_id(self.plan)):
                raise DevelopmentError('invalid_test_plan')
            _manifest_binding(self.plan,manifest)
            _disjoint_scripts(self.plan,manifest)
            self.backend_id = self.document['backend_id']
            self.manifest = decode(canonical(manifest))
            self.authorize_scope = authorize_scope
            try:
                self._lock = os.open('backend.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                     0o600, dir_fd=self.private.fd)
            except OSError:
                raise DevelopmentError('test_backend_lock_unavailable') from None
            self.private.validate_file(self._lock)
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
        self.private.close()

    @contextmanager
    def locked(self):
        if not self._thread_lock.acquire(blocking=False):
            raise DevelopmentError('test_backend_busy')
        try:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, TypeError):
                raise DevelopmentError('test_backend_busy') from None
        except BaseException:
            self._thread_lock.release()
            raise
        try:
            self.private.verify()
            info = os.stat('backend.lock', dir_fd=self.private.fd, follow_symlinks=False)
            held = os.fstat(self._lock)
            if (info.st_dev, info.st_ino) != (held.st_dev, held.st_ino):
                raise DevelopmentError('test_backend_lock_changed')
            self.private.validate_file(self._lock)
            if decode(self.private.read('plan.json', MAX_DOCUMENT)) != self.document:
                raise DevelopmentError('test_plan_changed')
            yield
        except OSError:
            raise DevelopmentError('test_backend_access_failed') from None
        finally:
            try:
                fcntl.flock(self._lock, fcntl.LOCK_UN)
            finally:
                self._thread_lock.release()

    def _authorize(self):
        try:
            authorize(self.authorize_scope, self.manifest, 'repo.run_test')
        except DevelopmentError:
            if getattr(self, '_active_admission', None) is not None:
                self._active_admission['refusal_reason'] = 'permission_revoked'
            raise

    def _pins(self):
        _validate_plan(self.plan)
        if (_binary(self.plan['codex']) != self.plan['codex_sha256']
                or _binary(self.plan['executable']) != self.plan['executable_sha256']):
            raise DevelopmentError('backend_executable_changed')
        _manifest_binding(self.plan,self.manifest)
        _disjoint_scripts(self.plan,self.manifest)
        for name in self.plan['tests']:self._script_payload(name)

    def _script_payload(self,name):
        test=self.plan['tests'].get(name)
        if test is None:raise DevelopmentError('invalid_test_name')
        with _repository(self.plan) as files:
            data,pin=_script_read(files,test['script'])
        if pin!=test['script_pin']:raise DevelopmentError('trusted_script_changed')
        return {'script':test['script'],'script_base64':base64.b64encode(data).decode('ascii'),
                'script_sha256':pin['sha256']}

    def qualified_for(self, manifest_hash, suite):
        try:
            self._authorize()
            self._pins()
            qualification = decode(self.private.read('qualification.json', MAX_DOCUMENT))
            test = self.plan['tests'].get(suite['name'])
            return (manifest_hash == self.document['manifest_hash'] and test is not None
                and suite == {'name': suite['name'], 'backend_id': self.backend_id,
                    'timeout_seconds': test['timeout_seconds'], 'max_output_bytes': test['max_output_bytes']}
                and type(qualification) is dict
                and type(qualification.get('schema')) is int
                and type(qualification.get('cleanup_confirmed')) is bool
                and type(qualification.get('checks')) is dict
                and all(type(value) is bool for value in qualification['checks'].values())
                and qualification == {
                    'schema': 1, 'plan_hash': self.document['plan_hash'], 'checks': {key: True for key in REQUIRED_CHECKS},
                    'cleanup_confirmed': True, 'result': 'qualified_single_process_fixture'})
        except (DevelopmentError, KeyError, TypeError):
            return False

    def qualify(self):
        """Explicit no-model canaries; not called by qualified_for or run."""
        with self.locked():
            self._authorize()
            self._pins()
            result, checks, reason = self._execute(None, qualify=True)
            qualification = {'schema': 1, 'plan_hash': self.document['plan_hash'], 'checks': checks,
                'cleanup_confirmed': result['cleanup_confirmed'],
                'result': 'qualified_single_process_fixture' if result['status'] == 'passed' else reason}
            self.private.replace('qualification.json', canonical(qualification))
            return decode(canonical(qualification))

    def _request(self, request):
        if type(request) is not TestRequest:
            raise DevelopmentError('invalid_test_request')
        sha256(request.operation_id)
        test = self.plan['tests'].get(request.name)
        if (test is None or type(request.timeout_seconds) is not int
                or type(request.max_output_bytes) is not int
                or request.manifest_hash != self.document['manifest_hash']
                or request.root != self.plan['root'] or list(request.root_identity) != self.plan['root_identity']
                or request.timeout_seconds != test['timeout_seconds']
                or request.max_output_bytes != test['max_output_bytes']):
            raise DevelopmentError('test_plan_scope_mismatch')
        return digest(canonical(vars(request)))

    def _read_operation(self, name, request_hash, maximum=1048576):
        try:
            raw = self.private.read(name, MAX_DOCUMENT)
        except DevelopmentError as error:
            if error.code == 'private_read_failed':
                try:
                    os.stat(name, dir_fd=self.private.fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
            raise
        value = decode(raw)
        if (type(value) is not dict or set(value) not in (
                    {'schema', 'request_hash', 'phase', 'result', 'reason'},
                    {'schema', 'request_hash', 'phase', 'result', 'reason', 'admission'})
                or type(value['schema']) is not int or value['schema'] not in (1, 2) or value['request_hash'] != request_hash
                or value['phase'] not in ('intent', 'complete')
                or (value['phase'] == 'intent' and (value['result'] is not None or value['reason'] is not None))):
            raise DevelopmentError('test_backend_journal_corrupt')
        if value['schema'] == 2:
            self._validate_admission(value, name, request_hash)
        elif 'admission' in value:
            raise DevelopmentError('test_backend_journal_corrupt')
        if value['phase'] == 'complete':
            result = value['result']
            if (type(result) is not dict or set(result) != {'status', 'cleanup_confirmed', 'output_bytes', 'output_sha256'}
                    or result['status'] not in ('passed', 'failed', 'cancelled', 'needs_review')
                    or type(result['cleanup_confirmed']) is not bool or type(result['output_bytes']) is not int
                    or not 0 <= result['output_bytes'] <= maximum
                    or (result['status'] != 'needs_review' and result['cleanup_confirmed'] is not True)):
                raise DevelopmentError('test_backend_journal_corrupt')
            sha256(result['output_sha256'])
        return value

    def _admission_controller_hash(self):
        """Bounded public controller source, never configuration or auth."""
        descriptor = os.open(Path(__file__), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid not in (0, os.geteuid())
                    or before.st_nlink != 1 or before.st_mode & 0o022 or before.st_size > 1048576):
                raise DevelopmentError('backend_controller_changed')
            raw = os.read(descriptor, 1048577)
            after = os.fstat(descriptor)
            if (len(raw) > 1048576 or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise DevelopmentError('backend_controller_changed')
            return digest(raw)
        finally:
            os.close(descriptor)

    def _validate_admission(self, value, name, request_hash):
        admission = value.get('admission')
        expected = {'schema', 'operation_id', 'request_hash', 'manifest_hash',
                    'backend_id', 'plan_hash', 'controller_sha256', 'nonce', 'launch'}
        if (type(admission) is not dict or set(admission) != expected
                or type(admission['schema']) is not int or admission['schema'] != 1
                or type(admission['operation_id']) is not str
                or admission['operation_id'] + '.json' != name
                or admission['request_hash'] != request_hash
                or admission['manifest_hash'] != self.document['manifest_hash']
                or admission['backend_id'] != self.backend_id
                or admission['plan_hash'] != self.document['plan_hash']
                or admission['controller_sha256'] != self._admission_controller_hash()
                or type(admission['nonce']) is not str or not re.fullmatch('[0-9a-f]{32}', admission['nonce'])
                or admission['launch'] not in ('pending', 'entered', 'not_started')):
            raise DevelopmentError('test_backend_journal_corrupt')
        for key in ('operation_id', 'request_hash', 'manifest_hash', 'plan_hash', 'controller_sha256'):
            sha256(admission[key])
        if value['phase'] == 'intent':
            if admission['launch'] == 'not_started':
                raise DevelopmentError('test_backend_journal_corrupt')
        elif (admission['launch'] != 'not_started' or type(value['reason']) is not str
                or value['reason'] not in PRELAUNCH_REASONS
                or value['result'] != _receipt('cancelled', cleanup=True)):
            raise DevelopmentError('test_backend_journal_corrupt')

    def _mark_launch(self):
        """Durable ambiguity boundary immediately before the owned Popen."""
        active = self._active_admission
        if active is None:
            return  # Qualification or the already-owned supervisor process.
        value = self._read_operation(active['name'], active['request_hash'])
        if (value != active['value'] or active['entered']
                or value['admission']['launch'] != 'pending'):
            raise DevelopmentError('test_backend_journal_corrupt')
        value['admission']['launch'] = 'entered'
        self.private.replace(active['name'], canonical(value))
        active['entered'] = True

    def _record_not_started(self, error):
        active = self._active_admission
        if active is None or active['entered']:
            return
        value = self._read_operation(active['name'], active['request_hash'])
        if value != active['value'] or value['admission']['launch'] != 'pending':
            return
        reason = _prelaunch_reason(error)
        if reason == 'test_backend_unqualified' and active.get('refusal_reason') == 'permission_revoked':
            reason = 'permission_revoked'
        value['phase'], value['result'], value['reason'] = (
            'complete', _receipt('cancelled', cleanup=True), reason)
        value['admission']['launch'] = 'not_started'
        self.private.replace(active['name'], canonical(value))

    def lookup_not_started(self, request):
        """Exact private terminal observation; no authorization or launch.

        Absence, legacy/pending/entered records never establish non-launch.
        The controller may use this after revocation only for identity cleanup.
        """
        request_hash = self._request(request)
        with self.locked():
            value = self._read_operation(request.operation_id + '.json', request_hash, request.max_output_bytes)
            if (value is None or value['schema'] != 2 or value['phase'] != 'complete'
                    or value['admission']['launch'] != 'not_started'):
                return None
            return {'schema': 1, **{key: value['admission'][key] for key in
                ('operation_id', 'request_hash', 'manifest_hash', 'backend_id', 'plan_hash',
                 'controller_sha256', 'nonce')}, 'started': False, 'cleanup_confirmed': True,
                'reason': value['reason'], 'receipt_sha256': digest(canonical(value))}

    def _run_request(self, request, execute, *, helper_receipt=False):
        request_hash = self._request(request)
        with self.locked():
            name = request.operation_id + '.json'
            old = self._read_operation(name, request_hash, request.max_output_bytes)
            if old is not None:
                self._authorize()
                return dict(old['result']) if old['phase'] == 'complete' else _receipt()
            self._require_settled()
            admission = {'schema': 1, 'operation_id': request.operation_id,
                'request_hash': request_hash, 'manifest_hash': request.manifest_hash,
                'backend_id': self.backend_id, 'plan_hash': self.document['plan_hash'],
                'controller_sha256': self._admission_controller_hash(),
                'nonce': uuid.uuid4().hex, 'launch': 'pending'}
            value = {'schema': 2, 'request_hash': request_hash, 'phase': 'intent',
                     'result': None, 'reason': None, 'admission': admission}
            self.private.create(name, canonical(value))  # Before any fallible callback.
            self._active_admission = {'name': name, 'request_hash': request_hash,
                                      'value': decode(canonical(value)), 'entered': False}
            try:
                self._authorize()
                suite = {'name': request.name, 'backend_id': self.backend_id,
                    'timeout_seconds': request.timeout_seconds, 'max_output_bytes': request.max_output_bytes}
                if not self.qualified_for(request.manifest_hash, suite):
                    raise DevelopmentError('test_backend_unqualified')
                result, checks, reason = execute()
                if not helper_receipt:
                    self.private.replace(name, canonical({'schema': 1, 'request_hash': request_hash,
                        'phase': 'complete', 'result': result, 'reason': reason}))
                return result
            except Exception as error:
                # A live call can prove its own prelaunch refusal. A crash is
                # BaseException and never reaches this branch; recovery cannot
                # convert an old pending admission into a terminal proof.
                try:
                    self._record_not_started(error)
                except (DevelopmentError, OSError, ValueError, TypeError):
                    pass  # A failed proof write stays unknown.
                raise
            finally:
                self._active_admission = None

    def _require_settled(self):
        names = os.listdir(self.private.fd)
        if len(names) > 1024:
            raise DevelopmentError('test_backend_journal_full')
        operation_names = [name for name in names if re.fullmatch('[0-9a-f]{64}\\.json', name)]
        if len(operation_names) >= self.manifest['budgets']['max_tests']:
            raise DevelopmentError('tests_budget_exhausted')
        for name in operation_names:
            value = decode(self.private.read(name, MAX_DOCUMENT))
            if type(value) is not dict or type(value.get('request_hash')) is not str:
                raise DevelopmentError('test_backend_journal_corrupt')
            value = self._read_operation(name, value['request_hash'])
            if value['phase'] != 'complete' or value['result']['cleanup_confirmed'] is not True:
                raise DevelopmentError('native_reconciliation_required')

    def run(self, request):
        def execute():
            self._authorize()
            return self._execute(request, qualify=False)
        return self._run_request(request, execute)

    def lookup(self, request):
        request_hash = self._request(request)
        with self.locked():
            self._authorize()
            value = self._read_operation(request.operation_id + '.json', request_hash, request.max_output_bytes)
            if value is None:
                return None
            return dict(value['result']) if value['phase'] == 'complete' else _receipt()

    def _command(self, payload):
        return [self.plan['codex'], 'sandbox', '-P', PROFILE_NAME, '--include-managed-config',
            '-C', self.plan['root'], '-c', 'permissions.' + PROFILE_NAME + '=' + _toml_policy(self.plan['root']),
            '--', self.plan['executable'], '-I', '-S', '-c', WRAPPER, canonical(payload).decode()]

    @staticmethod
    def _gone(group):
        try:
            os.killpg(group, 0)
            return False
        except ProcessLookupError:
            return True
        except OSError:
            return False

    @staticmethod
    def _signal(group, value):
        try:
            os.killpg(group, value)
        except ProcessLookupError:
            pass

    def _execute(self, request, *, qualify, outside_signal_pid=None):
        if outside_signal_pid is not None and (type(outside_signal_pid) is not int
                or outside_signal_pid < 2 or outside_signal_pid == os.getpid()):
            raise DevelopmentError('invalid_signal_canary')
        required_checks = REQUIRED_CHECKS | ({'outside_signal'} if outside_signal_pid is not None else set())
        timeout = 15 if qualify else request.timeout_seconds
        maximum = 0 if qualify else request.max_output_bytes
        nonce = uuid.uuid4().hex
        inside_name = '.bb-dev-canary-' + nonce
        outside_name = 'canary-' + nonce
        inside = Path(self.plan['root']) / inside_name
        outside = self.private.path / outside_name
        child = None
        selector = selectors.DefaultSelector()
        wrapper_pid = None
        header = bytearray()
        output = hashlib.sha256()
        output_size = 0
        checks = {}
        reason = 'startup_failed'
        diagnostics_size = 0
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._authorize()
            self._pins()
            with open(inside, 'xb') as stream:
                stream.write(b'synthetic canary')
            self.private.create(outside_name, b'synthetic outside canary')
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            payload = {'inside': str(inside), 'inside_write': str(inside) + '-write',
                'outside': str(outside), 'outside_write': str(outside) + '-write',
                'port': listener.getsockname()[1], 'timeout': timeout, 'qualify': qualify,
                'outside_signal_pid': outside_signal_pid,
                'executable': self.plan['executable'],
                'environment': self.plan['environment']}
            if not qualify:payload.update(self._script_payload(request.name))
            # Preserve HOME/CODEX_HOME values for the CLI itself; no auth file
            # is opened by this controller, and none reaches the test env.
            cli_env = {key: os.environ[key] for key in ('HOME', 'CODEX_HOME', 'TMPDIR') if key in os.environ}
            cli_env.update({'PATH': '/usr/bin:/bin', 'LANG': 'C'})
            self._authorize()
            self._mark_launch()
            child = subprocess.Popen(self._command(payload), cwd=self.plan['root'], env=cli_env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                close_fds=True, start_new_session=True)
            for stream in (child.stdout, child.stderr):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ)
            # Sandbox startup/canaries have a separate fixed deadline. The
            # named suite's timeout starts as soon as its guard is observed.
            deadline = time.monotonic() + 15
            reason = 'completed'
            while selector.get_map() or child.poll() is None:
                try:
                    self._authorize()
                except DevelopmentError:
                    reason = 'permission_revoked'
                    break
                if time.monotonic() >= deadline:
                    reason = 'timeout'
                    break
                for key, _ in selector.select(0.05):
                    data = os.read(key.fd, 65536)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    if key.fileobj is child.stdout and wrapper_pid is None:
                        header.extend(data)
                        if b'\n' not in header:
                            if len(header) > 8192:
                                reason = 'guard_protocol_invalid'
                                break
                            continue
                        line, data = bytes(header).split(b'\n', 1)
                        header.clear()
                        if len(line) > 8192 or not line.startswith(HEADER):
                            reason = 'guard_protocol_invalid'
                            break
                        value = decode(line[len(HEADER):])
                        if (type(value) is not dict or set(value) != {'pid', 'pgid', 'sid', 'checks', 'ok'}
                                or type(value['pid']) is not int or value['pid'] < 2
                                or value['pid'] == os.getpid()
                                or value['pgid'] != value['pid'] or value['sid'] != value['pid']
                                or type(value['checks']) is not dict or set(value['checks']) != required_checks
                                or any(type(item) is not bool for item in value['checks'].values())):
                            reason = 'guard_protocol_invalid'
                            break
                        wrapper_pid = value['pid']
                        checks = value['checks']
                        if value['ok'] is not True or not all(checks.values()):
                            reason = 'guard_failed'
                            break
                        # The source cannot start merely because the wrapper
                        # printed a guard. Parent validation/current permission
                        # must explicitly open this bounded one-byte gate.
                        self._authorize()
                        child.stdin.write(b'G')
                        child.stdin.flush()
                        child.stdin.close()
                        deadline = time.monotonic() + timeout
                    # CLI diagnostics have a separate small ceiling and are
                    # never returned, persisted, hashed or interpreted.
                    if key.fileobj is child.stderr:
                        diagnostics_size += len(data)
                        if diagnostics_size > 8192:
                            reason = 'diagnostic_limit'
                            break
                    if key.fileobj is child.stdout and data:
                        retained = data[:max(0, maximum - output_size)]
                        output.update(retained)
                        output_size += len(retained)
                        if len(data) > len(retained):
                            reason = 'output_limit'
                            break
                if reason != 'completed':
                    break
            if reason != 'completed':
                if wrapper_pid is not None:
                    self._signal(wrapper_pid, signal.SIGTERM)
                    grace = time.monotonic() + 0.1
                    while not self._gone(wrapper_pid) and time.monotonic() < grace:
                        child.poll()
                        time.sleep(0.01)
                    if not self._gone(wrapper_pid):
                        self._signal(wrapper_pid, signal.SIGKILL)
                else:
                    self._signal(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if wrapper_pid is not None:
                    self._signal(wrapper_pid, signal.SIGKILL)
                self._signal(child.pid, signal.SIGKILL)
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            if wrapper_pid is not None and not self._gone(wrapper_pid):
                if reason == 'completed':
                    reason = 'wrapper_left_running'
                self._signal(wrapper_pid, signal.SIGKILL)
            cleanup_deadline = time.monotonic() + 1
            while wrapper_pid is not None and not self._gone(wrapper_pid) and time.monotonic() < cleanup_deadline:
                time.sleep(0.02)
            cleanup = (wrapper_pid is not None and child.poll() is not None
                and self._gone(wrapper_pid) and self._gone(child.pid))
            if not checks or not all(checks.values()):
                cleanup = False
                if reason == 'completed':
                    reason = 'guard_missing'
            status = ('passed' if reason == 'completed' and child.returncode == 0 else
                      'cancelled' if reason == 'permission_revoked' else 'failed')
            if not cleanup:
                status = 'needs_review'
            return {'status': status, 'cleanup_confirmed': cleanup,
                'output_bytes': output_size, 'output_sha256': output.hexdigest()}, checks, reason
        except (OSError, DevelopmentError, ValueError, TypeError):
            if self._active_admission is not None and not self._active_admission['entered']:
                raise
            return _receipt(), checks, 'native_launch_uncertain'
        finally:
            if child is not None and not child.stdin.closed:
                try:
                    child.stdin.close()  # EOF prevents an unacknowledged launch.
                except OSError:
                    # A broken pipe must not skip process cleanup below.
                    pass
            # If an exception interrupted observation, kill every positively
            # identified group; an unknown wrapper is never claimed cleaned.
            if wrapper_pid is not None and not self._gone(wrapper_pid):
                self._signal(wrapper_pid, signal.SIGKILL)
            if child is not None and child.poll() is None:
                self._signal(child.pid, signal.SIGKILL)
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            selector.close()
            if child is not None:
                child.stdout.close()
                child.stderr.close()
            listener.close()
            for path in (inside, Path(str(inside) + '-write'), outside, Path(str(outside) + '-write')):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
