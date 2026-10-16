"""Opt-in, locally qualified read-only planner workspace; no model or login.

Only a trusted local controller prepares this policy. Board content cannot
choose an executable, policy, path or qualification receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import threading
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, absolute_directory, canonical, digest, identity
from .dev_test_backend import _binary, _host
from .dev_test_supervisor import _ancestor_identities
from .subscription_probe import _run_bounded, sanitized_environment

PROFILE_NAME = 'board-planner-read-v1'
PROFILE_VERSION = 1
CHECKS = {'inside_read', 'inside_write_denied', 'outside_read_denied',
          'outside_write_denied', 'network_loopback_denied'}
CANARY = r'''
import errno,json,socket,sys
p=json.loads(sys.argv[1]);checks={}
def denied(name,fn):
    try:
        fn();checks[name]=False
    except OSError as e:
        checks[name]=e.errno in (errno.EACCES,errno.EPERM)
def read(path):
    with open(path,'rb') as f:return f.read(64)
def write(path):
    with open(path,'xb') as f:f.write(b'synthetic planner canary')
def connect():
    with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
        s.settimeout(1);s.connect(('127.0.0.1',p['port']))
try:checks['inside_read']=read(p['inside'])==b'synthetic planner canary'
except OSError:checks['inside_read']=False
denied('inside_write_denied',lambda:write(p['inside_write']))
denied('outside_read_denied',lambda:read(p['outside']))
denied('outside_write_denied',lambda:write(p['outside_write']))
denied('network_loopback_denied',connect)
print(json.dumps({'schema':1,'checks':checks},sort_keys=True),flush=True)
raise SystemExit(0 if all(checks.values()) else 78)
'''


class PlannerPolicyError(DevelopmentError):
    pass


def permission_document(workspace):
    return {'filesystem': {':root': 'deny', ':minimal': 'read', ':tmpdir': 'deny',
                           ':slash_tmp': 'deny', workspace: 'read'},
            'network': {'enabled': False}}


def permission_arguments(workspace):
    """Same default selector for exec and the native qualification command."""
    entries = ','.join(json.dumps(key) + '=' + json.dumps(value)
                       for key, value in permission_document(workspace)['filesystem'].items())
    value = '{filesystem={' + entries + '},network={enabled=false}}'
    return ['-c', 'default_permissions=' + json.dumps(PROFILE_NAME),
            '-c', 'permissions.' + PROFILE_NAME + '=' + value]


def _directory(path, *, empty=False, private=False):
    value = os.fspath(path)
    if type(value) is not str or not value.startswith('/') or str(Path(value)) != value:
        raise PlannerPolicyError('planner_workspace_invalid')
    descriptor = absolute_directory(value)
    try:
        info = os.fstat(descriptor)
        if ((empty or private) and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700)
                or empty and os.listdir(descriptor)):
            raise PlannerPolicyError('planner_workspace_not_empty_or_private')
        return list(identity(info))
    finally:
        os.close(descriptor)


def _separate(left, right):
    l, r = _ancestor_identities(left), _ancestor_identities(right)
    if l[0] in r or r[0] in l:
        raise PlannerPolicyError('planner_workspace_overlap')


def prepare_planner_policy(directory, *, workspace, checkout, executable,
                           cli_version, interpreter='/usr/bin/python3'):
    """Prepare immutable private pins without launching a model or subprocess."""
    if _host()['system'] != 'Darwin' or os.geteuid() == 0:
        raise PlannerPolicyError('planner_platform_unqualified')
    if type(cli_version) is not str or not re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}', cli_version):
        raise PlannerPolicyError('planner_cli_version_invalid')
    workspace, checkout = os.fspath(workspace), os.fspath(checkout)
    workspace_identity = _directory(workspace, empty=True)
    checkout_identity = _directory(checkout)
    _separate(workspace, checkout)
    # The CLI/toolchain and controller state must remain outside its readable
    # workspace. Preparation does not create a second authentication home.
    directory = os.fspath(directory)
    executable, interpreter = os.fspath(executable), os.fspath(interpreter)
    plan = {'schema': 1, 'profile_version': PROFILE_VERSION, 'host': _host(),
            'workspace': workspace, 'workspace_identity': workspace_identity,
            'checkout': checkout, 'checkout_identity': checkout_identity,
            'executable': executable, 'cli_sha256': _binary(executable),
            'cli_version': cli_version, 'interpreter': interpreter,
            'interpreter_sha256': _binary(interpreter),
            'policy': permission_document(workspace), 'canary_sha256': digest(CANARY.encode())}
    with PrivateDirectory(directory, create=True) as private:
        _separate(workspace, private.path)
        for path in (Path(executable).parent, Path(interpreter).parent, Path(__file__).parent.parent):
            _separate(workspace, path)
        private.create('planner-plan.json', canonical(plan))
        private.create('planner-qualification.json', canonical(None))
    return digest(canonical(plan))


class PlannerPolicy:
    def __init__(self, directory):
        self.private = PrivateDirectory(directory)
        self._lock_fd = None
        self._thread_lock = threading.Lock()
        try:
            self._plan = decode(self.private.read('planner-plan.json', 16384))
            self._fingerprint = digest(canonical(self._plan))
            self._lock_fd = os.open('planner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                    0o600, dir_fd=self.private.fd)
            self.private.validate_file(self._lock_fd)
            self._pins()
        except BaseException:
            self.close()
            raise

    def __enter__(self):return self
    def __exit__(self, *_):self.close()
    def close(self):
        if self._lock_fd is not None:
            os.close(self._lock_fd);self._lock_fd = None
        self.private.close()

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

    def _pins(self, *, fixture=None):
        p = self._plan
        expected = {'schema','profile_version','host','workspace','workspace_identity',
                    'checkout','checkout_identity','executable','cli_sha256','cli_version',
                    'interpreter','interpreter_sha256','policy','canary_sha256'}
        if (type(p) is not dict or set(p) != expected or type(p['schema']) is not int
                or p['schema'] != 1 or type(p['profile_version']) is not int
                or p['profile_version'] != PROFILE_VERSION or p['host'] != _host()
                or _host()['system'] != 'Darwin' or os.geteuid() == 0
                or any(type(p[k]) is not str for k in ('workspace','checkout','executable','interpreter','cli_sha256','interpreter_sha256'))
                or any(not re.fullmatch(r'[0-9a-f]{64}', p[k]) for k in ('cli_sha256','interpreter_sha256'))
                or any(type(p[k]) is not list or len(p[k]) != 2
                       or any(type(v) is not int or v < 0 for v in p[k])
                       for k in ('workspace_identity','checkout_identity'))
                or p['policy'] != permission_document(p['workspace'])
                or p['canary_sha256'] != digest(CANARY.encode())
                or type(p['cli_version']) is not str
                or not re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}', p['cli_version'])):
            raise PlannerPolicyError('planner_policy_changed')
        self.private.verify()
        if digest(self.private.read('planner-plan.json', 16384)) != self._fingerprint:
            raise PlannerPolicyError('planner_policy_changed')
        if (_directory(p['workspace'], empty=fixture is None, private=True) != p['workspace_identity']
                or _directory(p['checkout']) != p['checkout_identity']
                or _binary(p['executable']) != p['cli_sha256']
                or _binary(p['interpreter']) != p['interpreter_sha256']):
            raise PlannerPolicyError('planner_policy_changed')
        if fixture is not None and set(os.listdir(p['workspace'])) != {fixture}:
            raise PlannerPolicyError('planner_workspace_not_empty_or_private')
        _separate(p['workspace'], p['checkout'])
        _separate(p['workspace'], self.private.path)
        for path in (Path(p['executable']).parent, Path(p['interpreter']).parent, Path(__file__).parent.parent):
            _separate(p['workspace'], path)

    @contextmanager
    def _locked(self):
        if self._lock_fd is None or self.private.fd is None:
            raise PlannerPolicyError('planner_policy_closed')
        if not self._thread_lock.acquire(blocking=False):
            raise PlannerPolicyError('planner_policy_busy')
        acquired = False
        try:
            self.private.verify()
            self.private.validate_file(self._lock_fd)
            current = os.stat('planner.lock', dir_fd=self.private.fd, follow_symlinks=False)
            if identity(current) != identity(os.fstat(self._lock_fd)):
                raise PlannerPolicyError('planner_policy_changed')
            try:fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:raise PlannerPolicyError('planner_policy_busy') from None
            acquired = True
            yield
        except OSError:
            raise PlannerPolicyError('planner_policy_unavailable') from None
        finally:
            if acquired:fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._thread_lock.release()

    def validate_for(self, executable, workspace):
        with self._locked():
            self._pins()
            if os.fspath(executable) != self.executable or os.fspath(workspace) != self.workspace:
                raise PlannerPolicyError('planner_policy_scope_mismatch')
            value = decode(self.private.read('planner-qualification.json', 8192))
            if (type(value) is not dict or set(value) != {'schema','fingerprint','checks','cleanup_confirmed'}
                    or type(value['schema']) is not int or value['schema'] != 1
                    or value['fingerprint'] != self.fingerprint or type(value['checks']) is not dict
                    or set(value['checks']) != CHECKS
                    or any(v is not True for v in value['checks'].values())
                    or value['cleanup_confirmed'] is not True):
                raise PlannerPolicyError('planner_policy_unqualified')
            return permission_arguments(self.workspace)

    def qualify(self):
        """Five synthetic FS/loopback checks only; never run exec/login/model."""
        with self._locked():
            self._pins()
            self.private.replace('planner-qualification.json', canonical(None))
            env = sanitized_environment()
            version = _run_bounded([self.executable, '--version'], self.workspace, 5, 4096, env)
            if version.failure or version.returncode != 0 or version.stdout.decode('utf-8','strict').strip() != self._plan['cli_version']:
                raise PlannerPolicyError('planner_cli_version_changed')
            fixture = 'planner-canary-' + uuid.uuid4().hex
            inside = Path(self.workspace) / fixture
            outside_name = 'outside-canary-' + uuid.uuid4().hex
            self.private.create(outside_name, b'synthetic planner canary')
            listener = None
            inside_fd = None
            workspace_fd = absolute_directory(self.workspace)
            try:
                if list(identity(os.fstat(workspace_fd))) != self._plan['workspace_identity']:
                    raise PlannerPolicyError('planner_policy_changed')
                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                inside_fd = os.open(fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                    0o600, dir_fd=workspace_fd)
                inside_identity = identity(os.fstat(inside_fd))
                PrivateDirectory._write(inside_fd, b'synthetic planner canary')
                listener.bind(('127.0.0.1', 0));listener.listen(1)
                self._pins(fixture=fixture)
                payload = {'inside': str(inside), 'inside_write': str(inside) + '-write',
                           'outside': str(self.private.path / outside_name),
                           'outside_write': str(self.private.path / (outside_name + '-write')),
                           'port': listener.getsockname()[1]}
                # Deliberately no -P: qualify default_permissions, the same
                # selector supplied to exec (which has no -P option).
                command = [self.executable, *permission_arguments(self.workspace), 'sandbox',
                           '--', self._plan['interpreter'], '-I', '-S',
                           '-c', CANARY, canonical(payload).decode()]
                run = _run_bounded(command, self.workspace, 15, 8192, env)
                value = decode(run.stdout)
                if (run.failure or run.returncode != 0 or type(value) is not dict
                        or set(value) != {'schema','checks'} or value['schema'] != 1
                        or type(value['checks']) is not dict or set(value['checks']) != CHECKS
                        or any(v is not True for v in value['checks'].values())):
                    raise PlannerPolicyError('planner_native_policy_unqualified')
            except (OSError, ValueError, UnicodeError, DevelopmentError):
                raise PlannerPolicyError('planner_native_policy_unqualified') from None
            finally:
                try:
                    if listener is not None:listener.close()
                    if inside_fd is not None:
                        os.close(inside_fd)
                        if identity(os.stat(fixture, dir_fd=workspace_fd, follow_symlinks=False)) != inside_identity:
                            raise PlannerPolicyError('planner_qualification_cleanup_unconfirmed')
                        os.unlink(fixture, dir_fd=workspace_fd)
                finally:
                    os.close(workspace_fd)
                    os.unlink(outside_name, dir_fd=self.private.fd)
            self._pins()
            receipt = {'schema': 1, 'fingerprint': self.fingerprint,
                       'checks': value['checks'], 'cleanup_confirmed': True}
            self.private.replace('planner-qualification.json', canonical(receipt))
            return receipt
