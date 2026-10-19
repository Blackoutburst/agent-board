"""Explicit fixed-canary recovery prototype. Product Linux stays unqualified."""
from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import select
import signal
import socket
import stat
import subprocess
import sys
import time
import types
import uuid

PROTOCOL = 'synthetic-linux-recovery-v2'
MAX_FILE = 256 * 1024
MAX_OUTPUT = 16384
TTL = 1.5
CASES = frozenset({'normal', 'timeout_ignore_term', 'controller_sigkill',
                   'lost_response', 'refuse_before_source'})
FOUNDATION = Path(__file__).with_name('qualify_linux_foundation.py')
# Kept explicit so receipt-only inspection never imports/executes pinned code.
REQUIRED_CHECKS = frozenset({'pid_namespace_init', 'outside_process_hidden_before_filter',
    'no_new_privs_before_filter', 'bounding_caps_zero_before_filter', 'inside_read', 'inside_write',
    'outside_write', 'readonly_file_write', 'runtime_write', 'outside_read', 'old_root_absent',
    'home_proc_private_state_absent', 'work_volume_bound', 'network_loopback', 'network_unix_path',
    'network_unix_abstract', 'fork', 'exec', 'session', 'process_group', 'outside_signal_denied',
    'clone3', 'x32_abi_denied', 'namespace_change_denied', 'caps_zero_after_filter',
    'nproc_raise_denied', 'hard_limits'})
ERROR_CHECKS = frozenset({'outside_write', 'readonly_file_write', 'runtime_write', 'outside_read',
    'network_loopback', 'network_unix_path', 'network_unix_abstract', 'fork', 'exec', 'session',
    'process_group', 'outside_signal_denied', 'clone3', 'x32_abi_denied', 'namespace_change_denied'})
LIMITS = {'address_space_each': 256 * 1024 * 1024, 'startup_seconds': 5,
    'test_seconds': 3, 'total_seconds': 10, 'cleanup_seconds': 2,
    'cpu_supervisor': 5, 'cpu_launcher_test': 1, 'max_processes_outside_controller': 3,
    'output_bytes': MAX_OUTPUT, 'work_bytes': 4 * 1024 * 1024, 'work_inodes': 128}


class RecoveryError(Exception):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise RecoveryError('duplicate_json_key')
        result[key] = value
    return result


def finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise RecoveryError('non_finite_json_number')
    return value


def decoded(data):
    return json.loads(data, object_pairs_hook=_pairs, parse_float=finite_float,
        parse_constant=lambda _: (_ for _ in ()).throw(RecoveryError('non_json_constant')))


def integer(value, minimum=0, maximum=None):
    return type(value) is int and value >= minimum and (maximum is None or value <= maximum)


def sha256(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def identity(value):
    return (type(value) is list and len(value) == 2
            and integer(value[0]) and integer(value[1], 1))


def file_pin(value):
    return (type(value) is dict and set(value) == {'identity', 'size', 'sha256'}
            and identity(value['identity']) and integer(value['size'], 0, 64 * 1024 * 1024)
            and sha256(value['sha256']))


def canonical_uuid(value):
    if type(value) is not str:
        return False
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        return False
    return str(parsed) == value and parsed.version == 4


def nonce(value):
    return type(value) is str and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def runtime_pins(value):
    return (type(value) is dict and 1 <= len(value) <= 256 and '/usr/bin/python3.10' in value
        and all(type(path) is str and len(path) <= 4096 and path.startswith(('/usr/', '/lib/'))
                and '..' not in Path(path).parts and str(Path(path)) == path and file_pin(pin)
                for path, pin in value.items())
        and sum(pin['size'] for pin in value.values()) <= 128 * 1024 * 1024)


def namespace_identity(value):
    return (type(value) is dict and set(value) == {'acquired_by', 'kernel_fdinfo_nspid',
            'host_pid_namespace_identity', 'init_pid_namespace_identity',
            'init_is_exact_launcher_child', 'namespace_numbers_used_for_host_signals'}
        and value['acquired_by'] == 'SCM_RIGHTS_from_trusted_init'
        and type(value['kernel_fdinfo_nspid']) is list and len(value['kernel_fdinfo_nspid']) == 2
        and integer(value['kernel_fdinfo_nspid'][0], 2)
        and type(value['kernel_fdinfo_nspid'][1]) is int and value['kernel_fdinfo_nspid'][1] == 1
        and identity(value['host_pid_namespace_identity']) and identity(value['init_pid_namespace_identity'])
        and value['host_pid_namespace_identity'] != value['init_pid_namespace_identity']
        and value['init_is_exact_launcher_child'] is True
        and value['namespace_numbers_used_for_host_signals'] is False)


def validate_state(state):
    if type(state) is not dict or not integer(state.get('namespace_launches'), 0, 1):
        raise RecoveryError('state_schema_invalid')
    phase = state.get('phase')
    if type(phase) is not str:
        raise RecoveryError('state_schema_invalid')
    keys = {'phase', 'namespace_launches'}
    if phase in ('prepared', 'launch_intent', 'helper_started'):
        valid = state['namespace_launches'] == 0
    elif phase == 'namespace_launch_intent':
        valid = state['namespace_launches'] == 1
    elif phase == 'refused_before_release':
        keys.add('refusal')
        valid = state['namespace_launches'] == 1 and state.get('refusal') == 'explicit_synthetic_stop'
    elif phase in ('source_release_intent', 'source_ack_written'):
        keys.add('admission_sha256')
        valid = state['namespace_launches'] == 1 and sha256(state.get('admission_sha256'))
    elif phase == 'terminal':
        keys.add('receipt_sha256')
        valid = sha256(state.get('receipt_sha256'))
        if 'admission_sha256' in state:
            keys.add('admission_sha256');valid = valid and sha256(state['admission_sha256'])
        if 'refusal' in state:
            keys.add('refusal')
            valid = valid and state['refusal'] == 'explicit_synthetic_stop' and 'admission_sha256' not in state
    else:
        valid = False
    if not valid or set(state) != keys:
        raise RecoveryError('state_schema_invalid')
    return state


def read_file(path):
    path = Path(path)
    if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
        raise RecoveryError('private_path_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.geteuid() or before.st_mode & 0o077
                or before.st_size > MAX_FILE):
            raise RecoveryError('private_file_invalid')
        data = bytearray()
        while len(data) <= MAX_FILE:
            part = os.read(fd, min(65536, MAX_FILE + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if len(data) > MAX_FILE or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise RecoveryError('private_file_changed')
        return bytes(data)
    finally:
        os.close(fd)


def directory_pin(directory):
    directory = Path(directory)
    if not directory.is_absolute() or '..' in directory.parts or any(p.is_symlink() for p in (directory, *directory.parents)):
        raise RecoveryError('private_directory_invalid')
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RecoveryError('private_directory_invalid')
    return {'path': str(directory), 'identity': [info.st_dev, info.st_ino]}


def fsync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def new_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(data);output.flush();os.fsync(output.fileno())
    fsync_directory(Path(path).parent)


def replace_state(directory, state):
    validate_state(state)
    temporary = directory / 'state.next'
    new_file(temporary, encoded(state))
    os.replace(temporary, directory / 'state.json')
    fsync_directory(directory)


def private_lock(directory, name):
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size != 0):
            raise RecoveryError('private_lock_invalid')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RecoveryError('controller_or_helper_busy') from None
        return fd
    except BaseException:
        os.close(fd)
        raise


def foundation(expected=None):
    # Compile exact public held source bytes, never a package or pyc fallback.
    path = FOUNDATION.resolve()
    if FOUNDATION.is_symlink():
        raise RecoveryError('foundation_source_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        source = os.read(fd, MAX_FILE + 1)
    finally:
        os.close(fd)
    if len(source) > MAX_FILE or expected is not None and digest(source) != expected:
        raise RecoveryError('foundation_source_changed')
    module = types.ModuleType('fixed_linux_foundation')
    module.__file__ = str(path)
    exec(compile(source, str(path), 'exec'), module.__dict__)
    return module, digest(source)


def bindings(job):
    request = job['request']
    return {'protocol': PROTOCOL, 'job_uuid': request['job_uuid'], 'nonce': request['nonce'],
        'request_sha256': job['request_sha256'], 'plan_sha256': job['plan_sha256'],
        'source_sha256': request['driver_sha256'], 'foundation_sha256': request['foundation_sha256'],
        'runtime_sha256': request['runtime_sha256']}


def load_job(directory):
    pin = directory_pin(directory)
    job = decoded(read_file(directory / 'job.json'))
    request_keys = {'protocol', 'job_uuid', 'nonce', 'case', 'journal', 'plan',
                    'foundation_sha256', 'driver_sha256', 'runtime_sha256', 'outside_pin'}
    if (type(job) is not dict or set(job) != {'request', 'request_sha256', 'plan_sha256'}
            or type(job.get('request')) is not dict or set(job['request']) != request_keys
            or job['request'].get('protocol') != PROTOCOL
            or type(job['request'].get('plan')) is not dict
            or set(job['request']['plan']) != {'runtime', 'tools', 'limits', 'seccomp_sha256'}
            or encoded(job['request']['plan']['limits']) != encoded(LIMITS)
            or not runtime_pins(job['request']['plan']['runtime'])
            or type(job['request']['plan']['tools']) is not dict
            or set(job['request']['plan']['tools']) != {'/usr/bin/python3.10', '/usr/bin/unshare'}
            or not all(file_pin(pin) for pin in job['request']['plan']['tools'].values())
            or encoded(job['request']['plan']['tools']['/usr/bin/python3.10']) !=
               encoded(job['request']['plan']['runtime']['/usr/bin/python3.10'])
            or not sha256(job['request']['plan'].get('seccomp_sha256'))
            or not canonical_uuid(job['request'].get('job_uuid'))
            or not nonce(job['request'].get('nonce'))
            or job['request']['runtime_sha256'] != digest(encoded(job['request']['plan']['runtime']))
            or encoded(job['request'].get('journal')) != encoded(pin)
            or not file_pin(job['request'].get('outside_pin'))
            or job['request']['outside_pin']['size'] != len(b'synthetic')
            or job['request']['outside_pin']['sha256'] != digest(b'synthetic')
            or any(not sha256(job['request'].get(k)) for k in
                   ('foundation_sha256', 'driver_sha256', 'runtime_sha256'))
            or digest(encoded(job['request'])) != job['request_sha256']
            or digest(encoded(job['request']['plan'])) != job['plan_sha256']
            or job['request'].get('case') not in CASES):
        raise RecoveryError('job_binding_invalid')
    return job


def verify_sources(job, base):
    request = job['request']
    if base.read_pin(Path(__file__).resolve())[1]['sha256'] != request['driver_sha256']:
        raise RecoveryError('driver_source_changed')
    for path, pin in request['plan']['runtime'].items():
        if (not path.startswith(('/usr/', '/lib/'))
                or base.read_pin(path, root_owned=True)[1] != pin):
            raise RecoveryError('public_runtime_pin_changed')
    for path, pin in request['plan']['tools'].items():
        if base.read_pin(path, root_owned=True)[1] != pin:
            raise RecoveryError('tool_pin_changed')


def prepare(directory, case):
    if case not in CASES or directory.exists():
        raise RecoveryError('new_fixture_required')
    base, source_hash = foundation()
    if base.REQUIRED_CHECKS != REQUIRED_CHECKS:
        raise RecoveryError('foundation_schema_changed')
    if platform.system() != 'Linux' or platform.machine() != 'x86_64' or os.geteuid() == 0 or sys.version_info[:2] != (3, 10):
        raise RecoveryError('unsupported_probe_host')
    directory.mkdir(mode=0o700)
    (directory / 'root').mkdir(mode=0o700)
    outside = directory / 'outside-canary'
    new_file(outside, b'synthetic')
    runtime = base.public_runtime()
    plan = {'runtime': runtime, 'tools': {path: base.read_pin(path, root_owned=True)[1]
            for path in (base.PYTHON, base.UNSHARE)}, 'limits': LIMITS,
        'seccomp_sha256': digest(base.encoded(base.seccomp_program()))}
    request = {'protocol': PROTOCOL, 'job_uuid': str(uuid.uuid4()), 'nonce': os.urandom(16).hex(),
        'case': case, 'journal': directory_pin(directory), 'plan': plan,
        'foundation_sha256': source_hash, 'driver_sha256': base.read_pin(Path(__file__).resolve())[1]['sha256'],
        'runtime_sha256': digest(base.encoded(runtime)), 'outside_pin': base.read_pin(outside)[1]}
    job = {'request': request, 'request_sha256': digest(encoded(request)), 'plan_sha256': digest(encoded(plan))}
    new_file(directory / 'job.json', encoded(job))
    new_file(directory / 'state.json', encoded({'phase': 'prepared', 'namespace_launches': 0}))
    return {'result': 'linux_unqualified', 'prepared': True, **bindings(job)}


def validate_frame(frame, job):
    expected = bindings(job)
    if (type(frame) is not dict or set(frame) != set(expected) | {'phase', 'checks', 'errors'}
            or any(frame.get(key) != value for key, value in expected.items())
            or frame.get('phase') not in ('source_started', 'result')):
        raise RecoveryError('source_frame_binding_invalid')
    if (type(frame['checks']) is not dict or set(frame['checks']) != REQUIRED_CHECKS
            or any(type(value) is not bool for value in frame['checks'].values())
            or type(frame['errors']) is not dict or set(frame['errors']) != ERROR_CHECKS
            or any(not integer(value, 0, 4095) for value in frame['errors'].values())):
        raise RecoveryError('source_frame_content_invalid')
    for name, code in frame['errors'].items():
        expected = {1, 13, 30} if name in {'outside_write', 'readonly_file_write', 'runtime_write'} else (
            {2} if name == 'outside_read' else {1})
        if frame['checks'][name] != (code in expected):
            raise RecoveryError('source_errno_check_inconsistent')
    return frame


def inner(packet):
    base, _ = foundation(packet['source_sha256'])
    if base.read_pin(Path(__file__).resolve())[1]['sha256'] != packet['job_binding']['source_sha256']:
        raise RecoveryError('driver_source_changed')
    original = base.fixed_canaries
    def canary(libc, data, setup, runtime_names):
        result = original(libc, data, setup, runtime_names)
        if data['case'] in ('timeout_ignore_term', 'controller_sigkill'):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        started = {**data['job_binding'], 'phase': 'source_started', **result}
        os.write(1, encoded(started) + b'\n')
        if data['case'] in ('timeout_ignore_term', 'controller_sigkill'):
            time.sleep(6)  # Independent synthetic bound even if its supervisor fails.
        return {**data['job_binding'], 'phase': 'result', **result}
    base.fixed_canaries = canary
    return base.inner(packet)


def outer(packet):
    base, _ = foundation(packet['source_sha256'])
    base.limits(1)
    libc = base.libc_api();base.prctl(libc, 1, signal.SIGKILL)
    if os.getppid() != packet['supervisor_pid']:
        raise RecoveryError('supervisor_changed')
    command = [base.UNSHARE, '--user', '--map-root-user', '--mount', '--pid', '--fork',
        '--kill-child=SIGKILL', '--net', '--ipc', '--uts', '--propagation', 'private', '--',
        base.PYTHON, '-I', '-S', '-B', str(Path(__file__).resolve()), '--inner', encoded(packet).decode()]
    os.execv(base.UNSHARE, command)


class Control:
    def __init__(self, channel):
        self.channel = channel
        self.channel.setblocking(False)
        self.last = None

    def check(self):
        while True:
            try:
                data = self.channel.recv(4096)
            except BlockingIOError:
                break
            if not data:
                raise RecoveryError('controller_eof')
            if any(byte != ord('G') for byte in data):
                raise RecoveryError('controller_control_invalid')
            self.last = time.monotonic()
        if self.last is None or time.monotonic() - self.last >= TTL:
            raise RecoveryError('controller_heartbeat_expired')

    def notice(self, phase):
        try:
            self.channel.send(encoded({'phase': phase}) + b'\n')
        except OSError:
            pass


def await_socket(channel, control, deadline):
    while True:
        control.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RecoveryError('startup_deadline_exhausted')
        if select.select([channel, control.channel], [], [], min(remaining, 0.05))[0] and select.select([channel], [], [], 0)[0]:
            return


def cleanup(base, launcher, launcher_fd, init_fd, *, term=False):
    deadline = time.monotonic() + 2
    term_sent = False
    terminal_after_term = None
    if term and init_fd is not None:
        try:
            signal.pidfd_send_signal(init_fd, signal.SIGTERM);term_sent = True
        except ProcessLookupError:
            pass
        terminal_after_term = bool(select.select([init_fd], [], [], min(0.1, max(0, deadline - time.monotonic())))[0])
    # Foundation cleanup uses a two-second shared deadline. Constrain its clock
    # by doing TERM only within that same envelope, then own the remaining wait.
    errors = False
    for descriptor in (init_fd, launcher_fd):
        if descriptor is not None:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                errors = True
    if launcher_fd is None and launcher.poll() is None:
        launcher.kill()  # Exact unreaped Popen host child only.
    terminal = bool(init_fd is not None and select.select([init_fd], [], [], max(0, deadline - time.monotonic()))[0])
    try:
        launcher.wait(timeout=max(0, deadline - time.monotonic()));reaped = True
    except subprocess.TimeoutExpired:
        reaped = False
    return {'kernel_init_identity_received': init_fd is not None, 'namespace_init_exited': terminal,
        'launcher_reaped': reaped, 'cleanup_confirmed': terminal and reaped and not errors,
        'term_sent_to_init_pidfd': term_sent, 'init_terminal_after_term': terminal_after_term,
        'namespace_pid_numbers_used_for_signals': False}


def publish(directory, job, state, observation):
    result = {**bindings(job), 'phase': 'terminal', 'observation': observation,
              'namespace_launches': state['namespace_launches']}
    receipt = {'result': result, 'result_sha256': digest(encoded(result))}
    new_file(directory / 'receipt.next', encoded(receipt))
    os.replace(directory / 'receipt.next', directory / 'receipt.json');fsync_directory(directory)
    state.update(phase='terminal', receipt_sha256=digest(encoded(receipt)))
    replace_state(directory, state)
    return receipt


def supervisor(directory, descriptor):
    job = load_job(directory)
    base, _ = foundation(job['request']['foundation_sha256'])
    base.limits(5);verify_sources(job, base)
    lock_fd = private_lock(directory, 'helper.lock')
    state = validate_state(decoded(read_file(directory / 'state.json')))
    if state != {'phase': 'launch_intent', 'namespace_launches': 0}:
        raise RecoveryError('job_already_attempted')
    state.update(phase='helper_started')
    replace_state(directory, state)
    auth = socket.socket(fileno=descriptor);control = Control(auth)
    started = time.monotonic()
    while control.last is None and time.monotonic() - started < TTL:
        if select.select([auth], [], [], 0.05)[0]:
            control.check()
    control.check()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    launcher = None;launcher_fd = init_fd = None
    source_released = release_intent = False
    source_frame = None;begin = None;identity = None;admission_sha256 = None
    reason = 'not_started';source_outcome = 'not_started';stream_bytes = 0
    try:
        packet = {'nonce': job['request']['nonce'], 'source_sha256': job['request']['foundation_sha256'],
            'runtime': job['request']['plan']['runtime'], 'supervisor_pid': os.getpid(),
            'control_fd': child.fileno(), 'root': str(directory / 'root'),
            'outside_canary': str(directory / 'outside-canary'), 'job_binding': bindings(job),
            'case': job['request']['case']}
        if len(encoded(packet)) > 64 * 1024:
            raise RecoveryError('launch_packet_too_large')
        control.check();verify_sources(job, base)
        state.update(phase='namespace_launch_intent', namespace_launches=1)
        replace_state(directory, state)
        launcher = subprocess.Popen([base.PYTHON, '-I', '-S', '-B', str(Path(__file__).resolve()),
            '--outer', encoded(packet).decode()], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, close_fds=True, pass_fds=(child.fileno(),),
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, start_new_session=True)
        child.close();launcher_fd = os.pidfd_open(launcher.pid)
        deadline = started + 5
        await_socket(parent, control, deadline)
        message, candidate = base.receive(parent, packet['nonce'], want_fd=True)
        try:
            identity = base.verify_init(candidate, launcher, message)
        except BaseException:
            os.close(candidate);raise
        init_fd = candidate
        control.check();parent.sendall(b'I')
        await_socket(parent, control, deadline)
        ready = base.receive(parent, packet['nonce'])
        if (ready.get('stage') != 'ready' or ready.get('runtime_pin_sha256') != job['request']['runtime_sha256']
                or ready.get('seccomp_sha256') != job['request']['plan']['seccomp_sha256']
                or ready.get('work_input_pin', {}).get('sha256') != digest(b'synthetic')):
            raise RecoveryError('source_ready_binding_invalid')
        control.check();verify_sources(job, base)
        if job['request']['case'] == 'refuse_before_source':
            state.update(phase='refused_before_release', refusal='explicit_synthetic_stop')
            replace_state(directory, state)
            raise RecoveryError('explicit_synthetic_stop')
        admission = {**bindings(job), 'phase': 'source_release_intent', 'namespace_identity': identity,
                     'work_input_pin': ready['work_input_pin']}
        release_intent = True  # Even a partial/uncertain write may not become not_started later.
        new_file(directory / 'admission.json', encoded(admission))
        admission_sha256 = digest(encoded(admission))
        state.update(phase='source_release_intent', admission_sha256=admission_sha256)
        replace_state(directory, state)  # Exact durable admission precedes SOURCE.
        control.check();verify_sources(job, base)
        if time.monotonic() >= deadline:
            raise RecoveryError('startup_deadline_exhausted')
        parent.sendall(b'G');source_released = True;parent.close()
        state.update(phase='source_ack_written');replace_state(directory, state)
        source_deadline = min(time.monotonic() + 3, started + 10)
        streams = {launcher.stdout.fileno(): bytearray(), launcher.stderr.fileno(): bytearray()}
        opened = set(streams)
        for fd in opened:
            os.set_blocking(fd, False)
        while opened or launcher.poll() is None:
            control.check()
            if time.monotonic() >= source_deadline:
                raise RecoveryError('source_wall_deadline_exhausted')
            for fd in select.select(list(opened), [], [], 0.05)[0]:
                data = os.read(fd, min(4096, MAX_OUTPUT + 1 - stream_bytes))
                if not data:
                    opened.remove(fd);continue
                stream_bytes += len(data)
                if stream_bytes > MAX_OUTPUT:
                    raise RecoveryError('source_output_bound_exceeded')
                streams[fd].extend(data)
                if fd == launcher.stdout.fileno():
                    while b'\n' in streams[fd]:
                        line, _, rest = streams[fd].partition(b'\n');streams[fd][:] = rest
                        frame = validate_frame(decoded(line), job)
                        if frame['phase'] == 'source_started' and begin is None and source_frame is None:
                            begin = frame;control.notice('source_started')
                        elif frame['phase'] == 'result' and begin is not None and source_frame is None:
                            source_frame = frame
                        else:
                            raise RecoveryError('source_frame_order_invalid')
        if launcher.returncode != 0 or source_frame is None or any(streams.values()):
            raise RecoveryError('source_result_missing_or_invalid')
        if not all(source_frame['checks'].values()):
            raise RecoveryError('source_guard_failed')
        source_outcome = 'completed';reason = 'completed'
    except Exception as error:
        reason = str(error) if isinstance(error, (RecoveryError, base.ProbeError)) else 'supervisor_refused'
        source_outcome = 'cancelled' if source_released else 'not_started'
        if release_intent and not source_released:
            source_outcome = 'release_uncertain'
    finally:
        parent.close();child.close()
        clean = {'kernel_init_identity_received': False, 'cleanup_confirmed': False,
                 'namespace_init_exited': False, 'launcher_reaped': False,
                 'term_sent_to_init_pidfd': False, 'init_terminal_after_term': None,
                 'namespace_pid_numbers_used_for_signals': False}
        if launcher is not None:
            clean = cleanup(base, launcher, launcher_fd, init_fd,
                            term=reason == 'source_wall_deadline_exhausted')
            launcher.stdout.close();launcher.stderr.close()
        for fd in (launcher_fd, init_fd):
            if fd is not None:
                os.close(fd)
        outside = base.read_pin(directory / 'outside-canary')[1]
        observation = {'outcome': source_outcome, 'reason': reason, 'source_ack_written': source_released,
            'admission_sha256': admission_sha256,
            'source_frame_sha256': None if source_frame is None else digest(encoded(source_frame)),
            'source_begin_frame': begin, 'source_result_frame': source_frame,
            'source_begin_sha256': None if begin is None else digest(encoded(begin)),
            'source_checks': None if begin is None else begin['checks'], 'output_bytes': stream_bytes,
            'cleanup': clean, 'namespace_identity': identity,
            'outside_canary_before': job['request']['outside_pin'], 'outside_canary_after': outside,
            'outside_canary_unchanged': outside == job['request']['outside_pin'],
            'elapsed_seconds': round(time.monotonic() - started, 6)}
        publish(directory, job, state, observation)
        control.notice('finished');auth.close();os.close(lock_fd)
    return 0


def validate_receipt(receipt, state, job, admission=None):
    validate_state(state)
    if (type(receipt) is not dict or set(receipt) != {'result', 'result_sha256'}
            or not sha256(receipt.get('result_sha256'))
            or digest(encoded(receipt['result'])) != receipt['result_sha256']
            or state['phase'] != 'terminal' or state['namespace_launches'] != 1
            or state['receipt_sha256'] != digest(encoded(receipt))):
        raise RecoveryError('terminal_receipt_missing_or_invalid')
    result = receipt['result']
    if (type(result) is not dict
            or set(result) != set(bindings(job)) | {'phase', 'observation', 'namespace_launches'}
            or any(type(result.get(key)) is not str or result[key] != value
                   for key, value in bindings(job).items())
            or result['phase'] != 'terminal' or not integer(result['namespace_launches'], 1, 1)):
        raise RecoveryError('terminal_receipt_binding_or_cleanup_invalid')
    observation = result['observation']
    keys = {'outcome', 'reason', 'source_ack_written', 'admission_sha256', 'source_frame_sha256',
        'source_begin_sha256', 'source_checks', 'source_begin_frame', 'source_result_frame',
        'output_bytes', 'cleanup', 'namespace_identity', 'outside_canary_before',
        'outside_canary_after', 'outside_canary_unchanged', 'elapsed_seconds'}
    if (type(observation) is not dict or set(observation) != keys
            or observation['outcome'] not in ('completed', 'cancelled', 'not_started')
            or type(observation['reason']) is not str or not 1 <= len(observation['reason']) <= 128
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_' for c in observation['reason'])
            or not integer(observation['output_bytes'], 0, MAX_OUTPUT)
            or type(observation['elapsed_seconds']) not in (int, float)
            or not 0 <= observation['elapsed_seconds'] <= LIMITS['total_seconds'] + LIMITS['cleanup_seconds']
            or not math.isfinite(observation['elapsed_seconds'])
            or not namespace_identity(observation['namespace_identity'])
            or not file_pin(observation['outside_canary_before'])
            or not file_pin(observation['outside_canary_after'])
            or encoded(observation['outside_canary_before']) != encoded(job['request']['outside_pin'])
            or encoded(observation['outside_canary_after']) != encoded(job['request']['outside_pin'])
            or observation['outside_canary_unchanged'] is not True):
        raise RecoveryError('terminal_observation_schema_invalid')
    clean = observation['cleanup']
    if (type(clean) is not dict or set(clean) != {'kernel_init_identity_received',
            'namespace_init_exited', 'launcher_reaped', 'cleanup_confirmed',
            'term_sent_to_init_pidfd', 'init_terminal_after_term', 'namespace_pid_numbers_used_for_signals'}
            or any(clean[k] is not True for k in ('kernel_init_identity_received',
                       'namespace_init_exited', 'launcher_reaped', 'cleanup_confirmed'))
            or clean['namespace_pid_numbers_used_for_signals'] is not False
            or type(clean['term_sent_to_init_pidfd']) is not bool
            or (clean['term_sent_to_init_pidfd'] and type(clean['init_terminal_after_term']) is not bool)
            or (not clean['term_sent_to_init_pidfd'] and clean['init_terminal_after_term'] is not None)):
        raise RecoveryError('terminal_cleanup_evidence_invalid')
    released = observation['source_ack_written']
    if type(released) is not bool or released != (observation['outcome'] in ('completed', 'cancelled')):
        raise RecoveryError('source_release_receipt_inconsistent')
    if released:
        if (type(admission) is not dict or set(admission) != set(bindings(job)) |
                {'phase', 'namespace_identity', 'work_input_pin'}
                or any(type(admission.get(key)) is not str or admission[key] != value
                       for key, value in bindings(job).items())
                or admission['phase'] != 'source_release_intent'
                or not namespace_identity(admission['namespace_identity'])
                or not file_pin(admission['work_input_pin'])
                or admission['work_input_pin']['size'] != len(b'synthetic')
                or admission['work_input_pin']['sha256'] != digest(b'synthetic')
                or observation['admission_sha256'] != digest(encoded(admission))
                or state.get('admission_sha256') != digest(encoded(admission))
                or encoded(admission['namespace_identity']) != encoded(observation['namespace_identity'])
                or 'refusal' in state):
            raise RecoveryError('admission_receipt_missing_or_invalid')
    elif (admission is not None or observation['admission_sha256'] is not None
            or 'admission_sha256' in state):
        raise RecoveryError('non_dispatch_proof_invalid')
    frames = (('source_begin_frame', 'source_begin_sha256', 'source_started'),
              ('source_result_frame', 'source_frame_sha256', 'result'))
    for name, hash_name, phase in frames:
        frame = observation[name]
        if frame is None:
            if observation[hash_name] is not None:
                raise RecoveryError('source_frame_receipt_invalid')
        else:
            validate_frame(frame, job)
            if (not released or frame['phase'] != phase or not sha256(observation[hash_name])
                    or observation[hash_name] != digest(encoded(frame))):
                raise RecoveryError('source_frame_receipt_invalid')
    begin, final = observation['source_begin_frame'], observation['source_result_frame']
    if ((begin is None and (final is not None or observation['source_checks'] is not None))
            or (begin is not None and encoded(observation['source_checks']) != encoded(begin['checks']))):
        raise RecoveryError('source_frame_order_or_checks_invalid')
    if observation['outcome'] == 'completed':
        if (observation['reason'] != 'completed' or begin is None or final is None
                or not all(begin['checks'].values()) or not all(final['checks'].values())
                or observation['output_bytes'] < len(encoded(begin)) + len(encoded(final)) + 2):
            raise RecoveryError('completed_source_evidence_missing')
    elif observation['reason'] == 'completed':
        raise RecoveryError('non_completed_reason_invalid')
    if 'refusal' in state and (observation['outcome'] != 'not_started' or observation['reason'] != state['refusal']):
        raise RecoveryError('refusal_state_inconsistent')
    if not released and (begin is not None or final is not None or observation['output_bytes'] != 0):
        raise RecoveryError('non_dispatch_source_evidence_invalid')
    return receipt


def inspect(directory):
    job = load_job(directory)
    state = validate_state(decoded(read_file(directory / 'state.json')))
    # A real admission must never disappear merely because state omits its hash.
    admission_path = directory / 'admission.json'
    admission = decoded(read_file(admission_path)) if os.path.lexists(admission_path) else None
    receipt = validate_receipt(decoded(read_file(directory / 'receipt.json')), state, job, admission)
    return {'result': 'linux_unqualified', 'backend_execution_qualified': False,
        'receipt_sha256': digest(encoded(receipt)), 'terminal_result': receipt['result'],
        'result_sha256': receipt['result_sha256'], 'new_namespace_launches': 0}


def corruptions(directory):
    """Metadata-only negative cases; never writes/reopens an existing attempt."""
    job = load_job(directory)
    state = validate_state(decoded(read_file(directory / 'state.json')))
    receipt = decoded(read_file(directory / 'receipt.json'))
    admission = decoded(read_file(directory / 'admission.json'))
    validate_receipt(receipt, state, job, admission)
    checks = {}
    for field in bindings(job):
        changed = decoded(encoded(receipt));changed['result'][field] = 'inconsistent'
        # Recomputing ordinary integrity hashes still must not cross job binding.
        changed['result_sha256'] = digest(encoded(changed['result']))
        altered_state = {**state, 'receipt_sha256': digest(encoded(changed))}
        try:
            validate_receipt(changed, altered_state, job, admission)
        except RecoveryError:
            checks['wrong_' + field] = True
        else:
            checks['wrong_' + field] = False
    for phase in ('launch_intent', 'source_release_intent', 'helper_started'):
        try:
            validate_receipt(receipt, {**state, 'phase': phase}, job, admission)
        except RecoveryError:
            checks['open_' + phase] = True
        else:
            checks['open_' + phase] = False
    try:
        validate_receipt(receipt, state, job, None)
    except RecoveryError:
        checks['missing_admission'] = True
    else:
        checks['missing_admission'] = False
    return {'result': 'linux_unqualified', 'backend_execution_qualified': False,
        'case': 'metadata_corruptions', 'checks': checks, 'new_namespace_launches': 0,
        'baseline_receipt_sha256': digest(encoded(receipt))}


def controller(directory):
    load_job(directory)
    if validate_state(decoded(read_file(directory / 'state.json')))['phase'] != 'prepared':
        return inspect(directory)
    lock_fd = private_lock(directory, 'controller.lock')
    try:
        return _controller_locked(directory)  # Re-read state under the admission lock.
    finally:
        os.close(lock_fd)


def _controller_locked(directory):
    job = load_job(directory)
    state = validate_state(decoded(read_file(directory / 'state.json')))
    if state.get('phase') != 'prepared':
        return inspect(directory)  # Receipt only; an open intent cannot be relaunched.
    base, _ = foundation(job['request']['foundation_sha256'])
    base.limits(5);verify_sources(job, base)
    replace_state(directory, {'phase': 'launch_intent', 'namespace_launches': 0})
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        helper = subprocess.Popen([base.PYTHON, '-I', '-S', '-B', str(Path(__file__).resolve()),
            '--supervisor', '--directory', str(directory), '--control-fd', str(child.fileno())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            pass_fds=(child.fileno(),), close_fds=True, start_new_session=True,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        child.close();parent.setblocking(False)
        deadline = time.monotonic() + 12
        buffer = bytearray()
        while time.monotonic() < deadline:
            try:
                parent.send(b'G')
            except OSError:
                break
            if select.select([parent], [], [], 0.1)[0]:
                data = parent.recv(4096)
                if not data:
                    break
                buffer.extend(data)
                if len(buffer) > 4096:
                    raise RecoveryError('helper_notice_too_large')
                while b'\n' in buffer:
                    line, _, rest = buffer.partition(b'\n');buffer[:] = rest
                    notice = decoded(line)
                    if notice == {'phase': 'source_started'} and job['request']['case'] == 'controller_sigkill':
                        os.kill(os.getpid(), signal.SIGKILL)  # This exact synthetic controller only.
                    if notice == {'phase': 'finished'}:
                        helper.wait(timeout=2)
                        if job['request']['case'] == 'lost_response':
                            return {'result': 'linux_unqualified', 'response_deliberately_lost': True}
                        return inspect(directory)
            if helper.poll() is not None:
                break
        helper.wait(timeout=2)
        return inspect(directory)
    finally:
        parent.close();child.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for flag in ('prepare', 'controller', 'inspect', 'corruptions', 'supervisor'):
        mode.add_argument('--' + flag, action='store_true')
    mode.add_argument('--inner');mode.add_argument('--outer')
    parser.add_argument('--directory', type=Path)
    parser.add_argument('--case', choices=sorted(CASES))
    parser.add_argument('--control-fd', type=int)
    args = parser.parse_args(argv)
    try:
        if args.inner is not None:
            return inner(decoded(args.inner))
        if args.outer is not None:
            outer(decoded(args.outer));return 2
        if args.supervisor:
            return supervisor(args.directory, args.control_fd)
        result = prepare(args.directory, args.case) if args.prepare else (
            controller(args.directory) if args.controller else (
                corruptions(args.directory) if args.corruptions else inspect(args.directory)))
    except Exception as error:
        result = {'result': 'linux_unqualified', 'backend_execution_qualified': False,
                  'status': 'needs_review', 'reason': str(error) if isinstance(error, RecoveryError) else 'prototype_refused'}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('status') != 'needs_review' else 1


if __name__ == '__main__':
    raise SystemExit(main())
