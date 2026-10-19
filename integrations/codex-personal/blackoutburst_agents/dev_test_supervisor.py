"""Explicit per-attempt supervisor for native tests, never a listener or model.

The supervisor owns the sandbox launch and wall deadline independently of its
controller. The only live control is a private pipe: current authorization
heartbeats keep it open, and EOF/revocation cancels the owned attempt. Recovery
reads a durable receipt and never adopts or kills a PID found in a journal.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
from pathlib import Path
import re
import select
import signal
import stat
import subprocess
import sys
import time
import uuid

from .dev_broker import TestRequest
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, absolute_directory, canonical, digest, identity
from .dev_test_backend import (MAX_DOCUMENT, REQUIRED_CHECKS, NativeNamedPythonBackend,
                               _binary, _receipt)


CONTROLLER_FILES = ('__init__.py', 'dev_manifest.py', 'dev_journal.py', 'dev_broker.py',
                    'dev_test_backend.py', 'dev_test_supervisor.py')
SUPERVISED_CHECKS = REQUIRED_CHECKS | {'outside_signal'}
CONTROL_TTL = 1.5
MAX_CONTROL_FRAME = 512
MAX_CONTROL_BYTES = 32768
MAX_CONTROL_FRAMES = 64
MAX_CONTROL_SEQUENCE = 65536


def _control_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _control_time(clock):
    value = clock()
    if not _control_number(value):
        raise DevelopmentError('supervisor_clock_changed')
    return value


def _validate_control_window(value, *, nonce, now, previous=None, allow_expired=False):
    fields = {'schema', 'nonce', 'sequence', 'started_at', 'validated_at', 'deadline'}
    if (not _control_number(now) or type(value) is not dict or set(value) != fields
            or type(value['schema']) is not int or value['schema'] != 1
            or type(nonce) is not str or not re.fullmatch('[0-9a-f]{32}', nonce)
            or value['nonce'] != nonce
            or type(value['sequence']) is not int or not 1 <= value['sequence'] <= MAX_CONTROL_SEQUENCE
            or any(not _control_number(value[key]) for key in ('started_at', 'validated_at', 'deadline'))
            or not value['started_at'] <= value['validated_at'] <= now
            or value['validated_at'] >= value['deadline']
            or value['deadline'] != value['started_at'] + CONTROL_TTL):
        raise DevelopmentError('supervisor_control_invalid')
    if previous is None:
        if value['sequence'] != 1:
            raise DevelopmentError('supervisor_control_invalid')
    elif (previous['nonce'] != nonce or value['sequence'] != previous['sequence'] + 1
            or value['started_at'] < previous['validated_at']
            or value['validated_at'] < previous['validated_at']
            or value['validated_at'] >= previous['deadline']):
        raise DevelopmentError('supervisor_control_invalid')
    if not allow_expired and now >= value['deadline']:
        raise DevelopmentError('supervisor_authorization_expired')
    return value


def _fresh_control_window(authorize, *, nonce, sequence, previous=None, clock=None):
    """One full fresh collection per frame; oldest data sets its age."""
    clock = time.monotonic if clock is None else clock
    started = _control_time(clock)
    if previous is not None and started < previous['validated_at']:
        raise DevelopmentError('supervisor_clock_changed')
    if previous is not None and started >= previous['deadline']:
        raise DevelopmentError('supervisor_authorization_expired')
    _validate_control_window({'schema': 1, 'nonce': nonce, 'sequence': sequence,
        'started_at': started, 'validated_at': started, 'deadline': started + CONTROL_TTL},
        nonce=nonce, now=started, previous=previous)
    result = authorize()
    if result is not None and result is not True:
        raise DevelopmentError('permission_revoked')
    validated = _control_time(clock)
    if validated < started:
        raise DevelopmentError('supervisor_clock_changed')
    if validated >= started + CONTROL_TTL or previous is not None and validated >= previous['deadline']:
        raise DevelopmentError('supervisor_authorization_expired')
    value = {'schema': 1, 'nonce': nonce, 'sequence': sequence, 'started_at': started,
             'validated_at': validated, 'deadline': started + CONTROL_TTL}
    return _validate_control_window(value, nonce=nonce, now=validated, previous=previous)


def _send_control_window(descriptor, value, *, previous=None, clock=None):
    clock = time.monotonic if clock is None else clock
    if type(value) is not dict or type(value.get('nonce')) is not str:
        raise DevelopmentError('supervisor_control_invalid')
    sent_at = _control_time(clock)
    _validate_control_window(value, nonce=value['nonce'], now=sent_at, previous=previous)
    if previous is not None and sent_at >= previous['deadline']:
        raise DevelopmentError('supervisor_authorization_expired')
    raw = canonical(value) + b'\n'
    if len(raw) > MAX_CONTROL_FRAME:
        raise DevelopmentError('supervisor_control_invalid')
    try:
        if os.write(descriptor, raw) != len(raw):
            raise DevelopmentError('supervisor_control_write_failed')
    except OSError:
        raise DevelopmentError('supervisor_control_write_failed') from None
    after = _control_time(clock)
    if after < sent_at:
        raise DevelopmentError('supervisor_clock_changed')
    if after >= value['deadline'] or previous is not None and after >= previous['deadline']:
        raise DevelopmentError('supervisor_authorization_expired')


BOOTSTRAP = r'''
import hashlib,json,os,sys,types
root,pins,packet=sys.argv[1:]
pins=json.loads(pins)
sources={}
for name,expected in pins.items():
    path=os.path.join(root,'blackoutburst_agents',name)
    with open(path,'rb') as stream:
        data=stream.read(1048577)
    if len(data)>1048576 or hashlib.sha256(data).hexdigest()!=expected:
        raise SystemExit(78)
    sources[name]=(path,data)
# Execute the exact held source bytes. Normal package imports could otherwise
# load an unpinned timestamp-matched .pyc from __pycache__.
package=types.ModuleType('blackoutburst_agents')
package.__package__='blackoutburst_agents'
package.__path__=[]
package.__file__=sources['__init__.py'][0]
sys.modules['blackoutburst_agents']=package
exec(compile(sources['__init__.py'][1],package.__file__,'exec'),package.__dict__)
for name in ('dev_manifest.py','dev_journal.py','dev_broker.py','dev_test_backend.py','dev_test_supervisor.py'):
    short=name[:-3]
    full='blackoutburst_agents.'+short
    module=types.ModuleType(full)
    module.__package__='blackoutburst_agents'
    module.__file__=sources[name][0]
    sys.modules[full]=module
    setattr(package,short,module)
    exec(compile(sources[name][1],module.__file__,'exec'),module.__dict__)
from blackoutburst_agents.dev_test_supervisor import supervise_packet
raise SystemExit(supervise_packet(packet))
'''
SIGNAL_CANARY = r'''
import os,signal,sys
signal.signal(signal.SIGUSR1,lambda *_:sys.stdout.buffer.write(b'S'))
print('R '+str(os.getpid()),flush=True)
sys.stdin.buffer.read(1)
'''


def _controller_pin():
    folder = Path(__file__).parent
    pins = {}
    for name in CONTROLLER_FILES:
        descriptor = None
        try:
            descriptor = os.open(folder / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022
                    or info.st_nlink != 1 or info.st_size > 1048576):
                raise DevelopmentError('supervisor_controller_changed')
            data = os.read(descriptor, 1048577)
            after = os.fstat(descriptor)
            if len(data) > 1048576 or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
                    after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise DevelopmentError('supervisor_controller_changed')
            pins[name] = digest(data)
        except OSError:
            raise DevelopmentError('supervisor_controller_unavailable') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return pins


def _ancestor_identities(directory, *, allow_missing=False):
    """Containment by opened directory identity, including case aliases."""
    candidate = Path(directory)
    if not candidate.is_absolute():
        raise DevelopmentError('supervisor_boundary_unknown')
    if allow_missing:
        for _ in range(512):
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                candidate = candidate.parent
                continue
            except OSError:
                raise DevelopmentError('supervisor_boundary_unknown') from None
            if not stat.S_ISDIR(info.st_mode):
                raise DevelopmentError('supervisor_boundary_unknown')
            break
        else:
            raise DevelopmentError('supervisor_boundary_unknown')
    descriptor = absolute_directory(candidate)
    ancestors = []
    try:
        for _ in range(512):
            current = identity(os.fstat(descriptor))
            ancestors.append(current)
            parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            parent_identity = identity(os.fstat(parent))
            os.close(descriptor)
            descriptor = parent
            if parent_identity == current:
                return ancestors
        raise DevelopmentError('supervisor_boundary_unknown')
    except OSError:
        raise DevelopmentError('supervisor_boundary_unknown') from None
    finally:
        os.close(descriptor)


def prepare_supervision(directory, *, executable='/usr/bin/python3'):
    """Controller-only opt-in preparation; no subprocess starts here."""
    executable = str(Path(executable).resolve(strict=True))
    document = {'schema': 1, 'executable': executable, 'executable_sha256': _binary(executable),
        'controller_files': _controller_pin(), 'bootstrap_sha256': digest(BOOTSTRAP.encode()),
        'canary_sha256': digest(SIGNAL_CANARY.encode())}
    with PrivateDirectory(directory) as private:
        private.create('supervision.json', canonical(document))
        private.create('supervision-qualification.json', canonical(None))
    return digest(canonical(document))


class _ControlPipe:
    def __init__(self, descriptor=0, *, nonce, initial, clock=None):
        self.fd = descriptor
        self.clock = time.monotonic if clock is None else clock
        self.last_now = _control_time(self.clock)
        _validate_control_window(initial, nonce=nonce, now=self.last_now, allow_expired=True)
        self.nonce, self.initial = nonce, decode(canonical(initial))
        self.last_window, self.buffer = None, b''
        self.ended = False
        os.set_blocking(self.fd, False)

    def _now(self):
        now = _control_time(self.clock)
        if now < self.last_now:
            raise DevelopmentError('supervisor_clock_changed')
        self.last_now = now
        return now

    def authorized(self):
        if self.ended:
            return False
        total, frames = 0, 0
        try:
            now = self._now()
            if self.last_window is None and now >= self.initial['deadline']:
                self.ended = True
                return False
            while True:
                try:
                    data = os.read(self.fd, min(4096, MAX_CONTROL_BYTES + 1 - total))
                except BlockingIOError:
                    break
                if not data:
                    self.ended = True
                    return False
                total += len(data)
                if total > MAX_CONTROL_BYTES:
                    raise DevelopmentError('supervisor_control_invalid')
                self.buffer += data
                while b'\n' in self.buffer:
                    line, self.buffer = self.buffer.split(b'\n', 1)
                    frames += 1
                    if len(line) + 1 > MAX_CONTROL_FRAME or frames > MAX_CONTROL_FRAMES:
                        raise DevelopmentError('supervisor_control_invalid')
                    value = decode(line)
                    if canonical(value) != line:
                        raise DevelopmentError('supervisor_control_invalid')
                    _validate_control_window(value, nonce=self.nonce, now=self._now(),
                        previous=self.last_window, allow_expired=True)
                    if self.last_window is None and canonical(value) != canonical(self.initial):
                        raise DevelopmentError('supervisor_control_invalid')
                    self.last_window = value
                if len(self.buffer) >= MAX_CONTROL_FRAME:
                    raise DevelopmentError('supervisor_control_invalid')
            now = self._now()
            if self.last_window is not None and now < self.last_window['deadline']:
                return True
            if now >= (self.initial if self.last_window is None else self.last_window)['deadline']:
                self.ended = True
            return False
        except (OSError, DevelopmentError, TypeError, ValueError):
            self.ended = True
            return False

    def wait_initial(self):
        """Inert wait: no canary or source before this exact initial lease."""
        while not self.ended:
            if self.authorized():
                return True
            if self.ended:
                return False
            try:
                remaining = self.initial['deadline'] - self._now()
                if remaining <= 0:
                    self.ended = True
                    return False
                select.select([self.fd], [], [], min(remaining, 0.05))
            except (OSError, DevelopmentError, TypeError, ValueError):
                self.ended = True
                return False
        return False


class _SignalCanary:
    def __init__(self, executable, directory):
        self.process = subprocess.Popen([executable, '-I', '-S', '-c', SIGNAL_CANARY],
            cwd=directory, env={'PATH': '/usr/bin:/bin', 'LANG': 'C'},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True)
        if (not select.select([self.process.stdout], [], [], 3)[0]
                or self.process.stdout.readline() != ('R ' + str(self.process.pid) + '\n').encode()):
            self.close()
            raise DevelopmentError('signal_canary_unavailable')

    @property
    def pid(self):
        return self.process.pid

    def close(self):
        process = self.process
        if not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()  # Exact owned Popen child, never a recovered PID.
            process.wait(timeout=1)
        remainder = process.stdout.read(64)
        process.stdout.close()
        return process.returncode == 0 and not remainder


class SupervisedNativeNamedPythonBackend(NativeNamedPythonBackend):
    """Opt-in replacement backend with one short-lived supervisor per attempt."""
    def __init__(self, directory, *, manifest, authorize_scope):
        super().__init__(directory, manifest=manifest, authorize_scope=authorize_scope)
        try:
            self.supervision = decode(self.private.read('supervision.json', MAX_DOCUMENT))
            self._supervision_pins()
        except BaseException:
            self.close()
            raise

    def _supervision_pins(self):
        document = self.supervision
        if (type(document) is not dict or set(document) != {'schema', 'executable', 'executable_sha256',
                'controller_files', 'bootstrap_sha256', 'canary_sha256'}
                or type(document['schema']) is not int or document['schema'] != 1
                or type(document['executable']) is not str or not document['executable'].startswith('/')):
            raise DevelopmentError('supervisor_controller_changed')
        root = Path(self.plan['root'])
        controller = Path(__file__).parent.parent
        root_ancestors = _ancestor_identities(root)
        root_identity = root_ancestors[0]
        if list(root_identity) != self.plan['root_identity']:
            raise DevelopmentError('workspace_identity_changed')
        controller_ancestors = _ancestor_identities(controller)
        if (root_identity in controller_ancestors or controller_ancestors[0] in root_ancestors
                or root_identity in _ancestor_identities(self.private.path)
                or any(root_identity in _ancestor_identities(Path(value).parent) for value in (
                    self.plan['codex'], self.plan['executable'], document['executable']))):
            raise DevelopmentError('supervisor_workspace_overlap')
        home = os.environ.get('HOME')
        protected = []
        if home:
            home_path = Path(home)
            if root_identity in _ancestor_identities(home_path, allow_missing=True):
                raise DevelopmentError('supervisor_workspace_overlap')
            protected.extend(home_path / name for name in ('.codex', '.ssh', '.aws', '.config', 'Library/Keychains'))
        if os.environ.get('CODEX_HOME'):
            protected.append(Path(os.environ['CODEX_HOME']))
        for path in protected:
            ancestors = _ancestor_identities(path, allow_missing=True)
            if root_identity in ancestors:
                raise DevelopmentError('supervisor_workspace_overlap')
            # A missing private directory has no identity of its own. Existing
            # protected directories must not contain the checkout either.
            try:
                exists = path.exists()
            except OSError:
                raise DevelopmentError('supervisor_boundary_unknown') from None
            if exists and ancestors[0] in root_ancestors:
                raise DevelopmentError('supervisor_workspace_overlap')
        if (document['controller_files'] != _controller_pin()
                or document['bootstrap_sha256'] != digest(BOOTSTRAP.encode())
                or document['canary_sha256'] != digest(SIGNAL_CANARY.encode())
                or _binary(document['executable']) != document['executable_sha256']
                or decode(self.private.read('supervision.json', MAX_DOCUMENT)) != document):
            raise DevelopmentError('supervisor_controller_changed')

    def qualified_for(self, manifest_hash, suite):
        if not super().qualified_for(manifest_hash, suite):
            return False
        try:
            self._supervision_pins()
            value = decode(self.private.read('supervision-qualification.json', MAX_DOCUMENT))
            return (type(value) is dict and type(value.get('schema')) is int
                and type(value.get('cleanup_confirmed')) is bool
                and type(value.get('checks')) is dict
                and all(type(item) is bool for item in value['checks'].values())
                and value == {'schema': 1, 'supervision_hash': digest(canonical(self.supervision)),
                    'plan_hash': self.document['plan_hash'], 'checks': {key: True for key in SUPERVISED_CHECKS},
                    'cleanup_confirmed': True, 'result': 'qualified_supervised_fixture'})
        except (DevelopmentError, TypeError, KeyError):
            return False

    def qualify(self):
        base = super().qualify()
        if base['result'] != 'qualified_single_process_fixture':
            return base
        with self.locked():
            self._authorize()
            result, checks, reason = self._run_supervised(None, qualify=True)
            value = {'schema': 1, 'supervision_hash': digest(canonical(self.supervision)),
                'plan_hash': self.document['plan_hash'], 'checks': checks,
                'cleanup_confirmed': result['cleanup_confirmed'],
                'result': 'qualified_supervised_fixture' if result['status'] == 'passed' else reason}
            self.private.replace('supervision-qualification.json', canonical(value))
            return value

    def run(self, request):
        return self._run_request(request, lambda: self._run_supervised(request, qualify=False),
                                 helper_receipt=True)

    def _admission_controller_hash(self):
        return digest(canonical({'controller_files': _controller_pin(),
                                 'supervision_sha256': digest(canonical(self.supervision))}))

    def _read_job(self, state_name, nonce, request_hash):
        value = decode(self.private.read(state_name, MAX_DOCUMENT))
        if (type(value) is not dict or set(value) != {'schema', 'nonce', 'request_hash', 'phase',
                'supervisor_identity', 'result', 'reason', 'checks'}
                or type(value['schema']) is not int or value['schema'] != 1
                or value['nonce'] != nonce or value['request_hash'] != request_hash
                or value['phase'] not in ('prepared', 'running', 'complete')
                or type(value['checks']) is not dict):
            raise DevelopmentError('supervisor_journal_corrupt')
        if value['phase'] == 'complete':
            value['result'] = self._test_result_checked(value['result'])
        identity = value['supervisor_identity']
        if value['phase'] == 'prepared':
            if identity is not None or value['result'] is not None or value['reason'] is not None or value['checks']:
                raise DevelopmentError('supervisor_journal_corrupt')
        else:
            if (type(identity) is not dict or set(identity) != {'pid', 'pgid', 'uid'}
                    or any(type(item) is not int for item in identity.values())
                    or identity['pid'] < 2 or identity['pgid'] != identity['pid'] or identity['uid'] != os.geteuid()):
                raise DevelopmentError('supervisor_journal_corrupt')
            if value['phase'] == 'running' and (value['result'] is not None or value['reason'] is not None or value['checks']):
                raise DevelopmentError('supervisor_journal_corrupt')
        if value['phase'] == 'complete':
            if (type(value['reason']) is not str or not re.fullmatch('[a-z_]{1,80}', value['reason'])
                    or not set(value['checks']).issubset(SUPERVISED_CHECKS)
                    or any(type(item) is not bool for item in value['checks'].values())
                    or (value['result']['status'] == 'passed' and (
                        value['result']['cleanup_confirmed'] is not True
                        or value['checks'] != {key: True for key in SUPERVISED_CHECKS}))):
                raise DevelopmentError('supervisor_journal_corrupt')
        return value

    @staticmethod
    def _test_result_checked(result):
        from .dev_broker import DevelopmentBroker
        return DevelopmentBroker._test_result(result, 1048576)

    def _launch(self, packet_name):
        self._supervision_pins()
        packet = decode(self.private.read(packet_name, MAX_DOCUMENT))
        _validate_control_window(packet['control'], nonce=packet['nonce'], now=_control_time(time.monotonic))
        package_root = str(Path(__file__).parent.parent)
        env = {key: os.environ[key] for key in ('HOME', 'CODEX_HOME', 'TMPDIR') if key in os.environ}
        env.update({'LANG': 'C', 'PATH': '/usr/bin:/bin'})
        self._mark_launch()
        return subprocess.Popen([self.supervision['executable'], '-I', '-S', '-c', BOOTSTRAP,
            package_root, canonical(self.supervision['controller_files']).decode(),
            str(self.private.path / packet_name)], cwd=self.private.path, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)

    def _run_supervised(self, request, *, qualify):
        self._authorize()
        self._pins()  # Trusted source pins precede even the owned helper launch.
        self._supervision_pins()
        job = uuid.uuid4().hex + uuid.uuid4().hex if qualify else request.operation_id
        nonce = uuid.uuid4().hex
        packet_name = 'supervision-' + job + '.job.json'
        state_name = 'supervision-' + job + '.state.json'
        request_hash = digest(canonical({'qualify': qualify, 'request': None if qualify else vars(request)}))
        packet = {'schema': 1, 'nonce': nonce, 'backend_dir': str(self.private.path), 'manifest': self.manifest,
            'request': None if qualify else vars(request), 'qualify': qualify,
            'supervision_hash': digest(canonical(self.supervision)), 'request_hash': request_hash,
            'state_name': state_name, 'packet_name': packet_name}
        first = _fresh_control_window(self._authorize, nonce=nonce, sequence=1)
        packet['control'] = first
        self.private.create(packet_name, canonical(packet))
        self.private.create(state_name, canonical({'schema': 1, 'nonce': nonce, 'request_hash': request_hash,
            'phase': 'prepared', 'supervisor_identity': None, 'result': None, 'reason': None, 'checks': {}}))
        child = self._launch(packet_name)
        os.set_blocking(child.stdin.fileno(), False)
        deadline = time.monotonic() + (50 if qualify else request.timeout_seconds + 50)
        revoked = False
        previous = first
        try:
            try:
                _send_control_window(child.stdin.fileno(), first)
            except (DevelopmentError, OSError):
                revoked = True
            while True:
                state = self._read_job(state_name, nonce, request_hash)
                if state['phase'] == 'complete':
                    if state['result']['output_bytes'] > (0 if qualify else request.max_output_bytes):
                        raise DevelopmentError('supervisor_journal_corrupt')
                    child.wait(timeout=2)
                    return state['result'], state['checks'], state['reason']
                if child.poll() is not None:
                    return _receipt(), {}, 'supervisor_lost'
                if time.monotonic() >= deadline:
                    revoked = True
                if not revoked:
                    try:
                        fresh = _fresh_control_window(self._authorize, nonce=nonce,
                            sequence=previous['sequence'] + 1, previous=previous)
                        _send_control_window(child.stdin.fileno(), fresh, previous=previous)
                        previous = fresh
                    except (DevelopmentError, OSError):
                        revoked = True
                if revoked and not child.stdin.closed:
                    child.stdin.close()
                    deadline = min(deadline, time.monotonic() + 5)
                if revoked and time.monotonic() >= deadline:
                    return _receipt(), {}, 'supervisor_cleanup_unconfirmed'
                time.sleep(0.05)
        finally:
            if not child.stdin.closed:
                try:
                    child.stdin.close()
                except OSError:
                    pass
            # No PID recovered from a journal is signalled here. If this
            # parent disappears, pipe EOF instructs the owned helper to stop.


def supervise_packet(packet_path):
    """Entrypoint used only by the pinned private bootstrap."""
    path = Path(packet_path)
    if not re.fullmatch('supervision-[0-9a-f]{64}\\.job\\.json', path.name):
        return 78
    private = PrivateDirectory(path.parent)
    backend = canary = None
    lock = None
    try:
        packet = decode(private.read(path.name, MAX_DOCUMENT))
        if (type(packet) is not dict or set(packet) != {'schema', 'nonce', 'backend_dir', 'manifest',
                'request', 'qualify', 'supervision_hash', 'request_hash', 'state_name', 'packet_name', 'control'}
                or type(packet['schema']) is not int or packet['schema'] != 1
                or type(packet['qualify']) is not bool or packet['packet_name'] != path.name
                or packet['backend_dir'] != str(private.path)
                or packet['state_name'] != path.name.replace('.job.json', '.state.json')
                or not re.fullmatch('[0-9a-f]{32}', packet['nonce'])
                or digest(canonical({'qualify': packet['qualify'], 'request': packet['request']})) != packet['request_hash']):
            return 78
        control = _ControlPipe(nonce=packet['nonce'], initial=packet['control'])
        scope = packet['manifest']['scope']
        resource = packet['manifest']['resource']
        def authorize(given, action, requested):
            return given == scope and action == 'repo.run_test' and requested == resource and control.authorized()
        backend = SupervisedNativeNamedPythonBackend(private.path, manifest=packet['manifest'], authorize_scope=authorize)
        if digest(canonical(backend.supervision)) != packet['supervision_hash']:
            return 78
        request = None if packet['qualify'] else TestRequest(**{
            **packet['request'], 'root_identity': tuple(packet['request']['root_identity'])})
        if request is not None:
            backend._request(request)
            if path.name != 'supervision-' + request.operation_id + '.job.json':
                return 78
            original = backend._read_operation(request.operation_id + '.json',
                backend._request(request), request.max_output_bytes)
            if (original is None or original['phase'] != 'intent'
                    or (original['schema'] == 2 and original['admission']['launch'] != 'entered')):
                return 78  # A stale prepared packet cannot revive a refused attempt.
        lock = os.open(path.name + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=private.fd)
        private.validate_file(lock)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = backend._read_job(packet['state_name'], packet['nonce'], packet['request_hash'])
        if state['phase'] != 'prepared':
            return 78  # Never restart an existing helper attempt.
        state.update(phase='running', supervisor_identity={'pid': os.getpid(), 'pgid': os.getpgrp(), 'uid': os.geteuid()})
        private.replace(packet['state_name'], canonical(state))
        result, checks, reason = _receipt(), {}, 'supervisor_execution_uncertain'
        try:
            if not control.wait_initial():
                raise DevelopmentError('permission_revoked')
            backend._authorize()
            backend._pins()
            canary = _SignalCanary(backend.supervision['executable'], private.path)
            result, checks, reason = backend._execute(request, qualify=packet['qualify'], outside_signal_pid=canary.pid)
        except DevelopmentError as error:
            if error.code == 'permission_revoked' and canary is None:
                result, reason = _receipt('cancelled', cleanup=True), 'controller_closed_before_launch'
        finally:
            if canary is not None and not canary.close():
                result, reason = _receipt(), 'signal_canary_cleanup_uncertain'
        if request is not None:
            operation_name = request.operation_id + '.json'
            original_hash = backend._request(request)
            original = backend._read_operation(operation_name, original_hash, request.max_output_bytes)
            if original is None or original['phase'] != 'intent':
                return 78
            private.replace(operation_name, canonical({'schema': 1, 'request_hash': original_hash,
                'phase': 'complete', 'result': result, 'reason': reason}))
        state.update(phase='complete', result=result, checks=checks, reason=reason)
        private.replace(packet['state_name'], canonical(state))
        return 0
    except (OSError, DevelopmentError, ValueError, TypeError, KeyError):
        return 78
    finally:
        if lock is not None:
            os.close(lock)
        if backend is not None:
            backend.close()
        private.close()
