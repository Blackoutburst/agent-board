"""Explicit synthetic Linux root/namespace probe; always linux_unqualified.

No Codex, login, model, production backend, installer or persistent namespace.
Only this fixed canary runs. Run as an authorized non-root x86_64 Linux user.
"""
from __future__ import annotations

import argparse
import array
import ast
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import resource
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

MAX_PACKET = 16384
MAX_OUTPUT = 16384
MEMORY = 256 * 1024 * 1024
WORK_BYTES = 4 * 1024 * 1024
ROOT_BYTES = 32 * 1024 * 1024
STARTUP_SECONDS = 5
TEST_SECONDS = 3
TOTAL_SECONDS = 10
UNSHARE = '/usr/bin/unshare'
PYTHON = '/usr/bin/python3.10'
REQUIRED_CHECKS = frozenset({'pid_namespace_init', 'outside_process_hidden_before_filter',
    'no_new_privs_before_filter', 'bounding_caps_zero_before_filter', 'inside_read', 'inside_write',
    'outside_write', 'readonly_file_write', 'runtime_write', 'outside_read', 'old_root_absent',
    'home_proc_private_state_absent', 'work_volume_bound', 'network_loopback', 'network_unix_path',
    'network_unix_abstract', 'fork', 'exec', 'session', 'process_group', 'outside_signal_denied',
    'clone3', 'x32_abi_denied', 'namespace_change_denied', 'caps_zero_after_filter',
    'nproc_raise_denied', 'hard_limits'})

# Native x86_64 only, including explicit denial of x32 syscall numbers. Unknown
# audit architectures kill the process; this policy is not an aarch64 policy.
DENIED_SYSCALLS = frozenset({
    41, 42, 43, 44, 45, 46, 47, 49, 50, 51, 52, 53, 54, 55, 288, 299, 307,
    56, 57, 58, 59, 62, 109, 112, 101, 105, 106, 113, 114, 116, 117, 119,
    122, 123, 126, 129, 130, 132, 133, 134, 135, 136, 155, 157, 160, 161,
    163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 175, 176, 177,
    200, 234, 246, 248, 249, 250, 272, 298, 303, 304, 308, 310, 311, 313,
    317, 320, 321, 322, 323, 424, 425, 426, 427, 428, 429, 430, 431, 432,
    433, 434, 435, 438, 440, 442,
})


class ProbeError(Exception):
    pass


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def component_pins(source):
    text = source.decode('utf-8')
    selected = {'fixed_canaries', 'inner_setup', 'rootfs', 'lock_down', 'run', 'terminate_owned'}
    return {node.name: digest(ast.get_source_segment(text, node).encode('utf-8'))
        for node in ast.parse(text).body if isinstance(node, ast.FunctionDef) and node.name in selected}


def read_pin(path, *, root_owned=False, maximum=64 * 1024 * 1024, expected=None):
    """Explicit public runtime/fixture file, held nofollow bytes and inode."""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise ProbeError('pin_path_invalid')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= maximum
                or before.st_mode & 0o022 or expected is None and before.st_uid not in (0, os.geteuid())
                or root_owned and before.st_uid != 0):
            raise ProbeError('pin_file_invalid')
        data = bytearray()
        while len(data) <= maximum:
            part = os.read(fd, min(65536, maximum + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if len(data) > maximum or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise ProbeError('pin_changed')
        pin = {'identity': [before.st_dev, before.st_ino], 'size': len(data), 'sha256': digest(data)}
        if expected is not None and expected != pin:
            raise ProbeError('pin_changed')
        return bytes(data), pin
    finally:
        os.close(fd)


def public_runtime():
    # Only already loaded root-owned public runtime files. No directory walk,
    # environment dump, authentication file or another process' maps is read.
    paths = {str(Path(sys.executable).resolve())}
    for module in tuple(sys.modules.values()):
        for key in ('__file__', '__cached__'):
            value = getattr(module, key, None)
            if isinstance(value, str) and value.startswith(('/usr/', '/lib/')) and Path(value).is_file():
                paths.add(str(Path(value).resolve()))
    for line in Path('/proc/self/maps').read_text().splitlines():
        parts = line.split(None, 5)
        if len(parts) == 6 and parts[5].startswith(('/usr/', '/lib/')):
            paths.add(str(Path(parts[5]).resolve()))
    pins = {path: read_pin(path, root_owned=True)[1] for path in sorted(paths)}
    if sum(pin['size'] for pin in pins.values()) > 128 * 1024 * 1024:
        raise ProbeError('runtime_inventory_too_large')
    return pins


def seccomp_program():
    # sock_filter tuples: code, jt, jf, k. seccomp_data arch at4, nr at0.
    program = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e),
               (0x06, 0, 0, 0x80000000), (0x20, 0, 0, 0),
               (0x35, 0, 1, 0x40000000), (0x06, 0, 0, 0x00050000 | errno.EPERM)]
    for number in sorted(DENIED_SYSCALLS):
        program.extend([(0x15, 0, 1, number), (0x06, 0, 0, 0x00050000 | errno.EPERM)])
    return program + [(0x06, 0, 0, 0x7fff0000)]


def libc_api():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p,
                           ctypes.c_ulong, ctypes.c_void_p]
    libc.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.syscall.restype = ctypes.c_long
    return libc


def checked(value):
    if value != 0:
        raise OSError(ctypes.get_errno(), 'kernel_operation_refused')


def prctl(libc, operation, value=0):
    checked(libc.prctl(operation, value, 0, 0, 0))


def mount(libc, source, destination, flags, filesystem=None, data=None):
    checked(libc.mount(None if source is None else os.fsencode(source), os.fsencode(destination),
        None if filesystem is None else os.fsencode(filesystem), flags,
        None if data is None else ctypes.c_char_p(os.fsencode(data))))


def limits(cpu):
    for kind, value in ((resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_CORE, 0),
                        (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_FSIZE, WORK_BYTES),
                        (resource.RLIMIT_CPU, cpu)):
        resource.setrlimit(kind, (value, value))


class CapHeader(ctypes.Structure):
    _fields_ = [('version', ctypes.c_uint32), ('pid', ctypes.c_int)]


class CapData(ctypes.Structure):
    _fields_ = [('effective', ctypes.c_uint32), ('permitted', ctypes.c_uint32),
                ('inheritable', ctypes.c_uint32)]


def caps(libc, *, clear=False):
    header = CapHeader(0x20080522, 0)
    data = (CapData * 2)()
    checked((libc.capset if clear else libc.capget)(ctypes.byref(header), ctypes.byref(data)))
    return [getattr(item, field) for item in data for field in ('effective', 'permitted', 'inheritable')]


def lock_down(libc):
    # Lock namespace-root semantics before dropping every capability/bounding bit.
    prctl(libc, 28, 1 | 2 | 4 | 8)  # PR_SET_SECUREBITS: NOROOT + NO_SETUID_FIXUP, locked.
    for capability in range(64):
        if libc.prctl(24, capability, 0, 0, 0) != 0 and ctypes.get_errno() != errno.EINVAL:
            raise ProbeError('bounding_capability_drop_failed')
    prctl(libc, 47, 4)  # PR_CAP_AMBIENT, CLEAR_ALL.
    caps(libc, clear=True)
    if any(caps(libc)):
        raise ProbeError('capability_drop_failed')
    if any(libc.prctl(23, capability, 0, 0, 0) != 0 for capability in range(41)):
        raise ProbeError('bounding_capability_present')
    prctl(libc, 38, 1)  # PR_SET_NO_NEW_PRIVS (irreversible).
    if libc.prctl(39, 0, 0, 0, 0) != 1:
        raise ProbeError('no_new_privs_missing')
    class Filter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte),
                    ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint32)]
    class Program(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(Filter))]
    bytecode = seccomp_program()
    filters = (Filter * len(bytecode))(*(Filter(*instruction) for instruction in bytecode))
    program = Program(len(bytecode), filters)
    checked(libc.prctl(22, 2, ctypes.cast(ctypes.byref(program), ctypes.c_void_p).value, 0, 0))


def rootfs(libc, directory, runtime):
    base = Path(directory)
    mount(libc, None, '/', 1 << 14 | 1 << 18)  # MS_REC | MS_PRIVATE.
    mount(libc, 'tmpfs', base, 2 | 4, 'tmpfs', 'size=32M,nr_inodes=2048,mode=755')
    for part in ('work', 'runtime', 'oldroot'):
        (base / part).mkdir(mode=0o700)
    (base / 'readonly-canary').write_bytes(b'synthetic')
    runtime_names = []
    for index, (path, pin) in enumerate(runtime.items()):
        # Root ownership was verified outside the user namespace. Host UID0 is
        # unmapped here; require the exact held parent pin instead of treating
        # the overflow UID as a new root-ownership claim.
        read_pin(path, expected=pin)
        target = base / 'runtime' / str(index)
        target.touch(mode=0o444)
        mount(libc, path, target, 1 << 12)  # MS_BIND.
        mount(libc, None, target, 1 << 12 | 32 | 1 | 2 | 4 | 8)  # bind,remount,RO,nosuid,nodev,noexec.
        runtime_names.append('/runtime/' + str(index))
    mount(libc, 'tmpfs', base / 'work', 2 | 4 | 8, 'tmpfs', 'size=4M,nr_inodes=128,mode=700')
    (base / 'work' / 'input').write_bytes(b'synthetic')
    os.chdir(base)
    checked(libc.syscall(155, ctypes.c_char_p(b'.'), ctypes.c_char_p(b'oldroot')))
    os.chdir('/')
    checked(libc.umount2(b'/oldroot', 2))  # MNT_DETACH; never leave an old-root escape.
    os.rmdir('/oldroot')
    mount(libc, None, '/', 32 | 1 | 2 | 4)  # readonly root; separate /work remains RW.
    os.chdir('/work')
    return runtime_names


def _attempt(checks, errors, name, call, expected):
    try:
        call()
    except OSError as error:
        checks[name] = error.errno in expected
        errors[name] = error.errno
    else:
        checks[name] = False
        errors[name] = 0


def fixed_canaries(libc, packet, setup, runtime_names):
    checks = dict(setup)
    errors = {}
    def write(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(fd, b'synthetic')
        finally:
            os.close(fd)
    checks['inside_read'] = Path('/work/input').read_bytes() == b'synthetic'
    write('/work/new')
    checks['inside_write'] = Path('/work/new').read_bytes() == b'synthetic'
    fs_errors = {errno.EROFS, errno.EACCES, errno.EPERM}
    _attempt(checks, errors, 'outside_write', lambda: write('/outside-created'), fs_errors)
    _attempt(checks, errors, 'readonly_file_write', lambda: write('/readonly-canary'), fs_errors)
    _attempt(checks, errors, 'runtime_write', lambda: write(runtime_names[0]), fs_errors)
    _attempt(checks, errors, 'outside_read', lambda: Path(packet['outside_canary']).read_bytes(), {errno.ENOENT})
    checks['old_root_absent'] = not Path('/oldroot').exists()
    checks['home_proc_private_state_absent'] = all(not Path(p).exists() for p in ('/home', '/root', '/proc', '/run'))
    volume = os.statvfs('/work')
    checks['work_volume_bound'] = volume.f_blocks * volume.f_frsize <= WORK_BYTES and volume.f_files <= 128
    for name, family, address in (
            ('network_loopback', socket.AF_INET, ('127.0.0.1', 1)),
            ('network_unix_path', socket.AF_UNIX, '/outside-socket'),
            ('network_unix_abstract', socket.AF_UNIX, '\0synthetic-foundation')):
        def connect(family=family, address=address):
            with socket.socket(family, socket.SOCK_STREAM) as channel:
                channel.connect(address)
        _attempt(checks, errors, name, connect, {errno.EPERM})
    def fork():
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
    _attempt(checks, errors, 'fork', fork, {errno.EPERM})
    _attempt(checks, errors, 'exec', lambda: os.execve('/not-an-executable', ['/not-an-executable'], {}), {errno.EPERM})
    _attempt(checks, errors, 'session', os.setsid, {errno.EPERM})
    _attempt(checks, errors, 'process_group', lambda: os.setpgid(0, 0), {errno.EPERM})
    _attempt(checks, errors, 'outside_signal_denied', lambda: os.kill(packet['supervisor_pid'], signal.SIGUSR1), {errno.EPERM})
    def syscall(number):
        ctypes.set_errno(0)
        if libc.syscall(number, 0, 0, 0, 0, 0, 0) == -1:
            raise OSError(ctypes.get_errno(), 'synthetic_syscall_refused')
    _attempt(checks, errors, 'clone3', lambda: syscall(435), {errno.EPERM})
    _attempt(checks, errors, 'x32_abi_denied', lambda: syscall(0x40000000 | 39), {errno.EPERM})
    _attempt(checks, errors, 'namespace_change_denied', lambda: syscall(272), {errno.EPERM})
    checks['caps_zero_after_filter'] = not any(caps(libc))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (2, 2))
        checks['nproc_raise_denied'] = False
    except (ValueError, OSError):
        checks['nproc_raise_denied'] = True
    checks['hard_limits'] = all(resource.getrlimit(kind) == (value, value) for kind, value in (
        (resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_NOFILE, 64),
        (resource.RLIMIT_FSIZE, WORK_BYTES), (resource.RLIMIT_NPROC, 1), (resource.RLIMIT_CPU, 1)))
    return {'checks': checks, 'errors': errors}


def inner(packet):
    """Trusted PID1 setup. Only fixed canary code may follow the second ACK."""
    channel = socket.socket(fileno=packet['control_fd'])
    phase = {'name': 'identity'}
    try:
        return inner_setup(packet, channel, phase)
    except Exception as error:
        # Fixed phase + errno only, never exception text or file contents.
        failure = {'stage': 'refused', 'nonce': packet['nonce'], 'phase': phase['name'],
            'errno': error.errno if isinstance(error, OSError) and type(error.errno) is int else 0}
        try:
            os.write(channel.fileno(), encoded(failure))
        except OSError:
            pass
        os.write(1, encoded({'bootstrap_failure': failure}) + b'\n')
        return 78


def inner_setup(packet, channel, phase):
    libc = libc_api()
    # Inherit the launcher's hard CPU ceiling; never raise a hard limit inside
    # a user namespace. One CPU second is stricter than the approved two.
    limits(1)
    if read_pin(Path(__file__).resolve())[1]['sha256'] != packet['source_sha256']:
        raise ProbeError('source_pin_changed')
    prctl(libc, 1, signal.SIGKILL)  # PR_SET_PDEATHSIG for this exact unshare parent.
    channel.settimeout(STARTUP_SECONDS)
    if os.getpid() != 1:
        raise ProbeError('namespace_init_missing')
    namespace = os.stat('/proc/self/ns/pid')
    pidfd = os.pidfd_open(os.getpid())
    try:
        channel.sendmsg([encoded({'stage': 'identity', 'nonce': packet['nonce'],
            'namespace_identity': [namespace.st_dev, namespace.st_ino]})],
            [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [pidfd]).tobytes())])
    finally:
        os.close(pidfd)
    if channel.recv(1) != b'I':
        raise ProbeError('identity_ack_missing')
    phase['name'] = 'runtime_inventory'
    runtime = packet['runtime']
    setup = {'pid_namespace_init': True}
    # Check namespace invisibility BEFORE generic signal/seccomp denial. ESRCH
    # from pidfd_open is distinct evidence from the later EPERM signal canary.
    try:
        other = os.pidfd_open(packet['supervisor_pid'])
    except ProcessLookupError:
        setup['outside_process_hidden_before_filter'] = True
    else:
        os.close(other)
        setup['outside_process_hidden_before_filter'] = False
    phase['name'] = 'rootfs'
    runtime_names = rootfs(libc, packet['root'], runtime)
    work_pin = read_pin(Path('/work/input'))[1]
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
    channel.setblocking(True)
    phase['name'] = 'capabilities_seccomp'
    lock_down(libc)
    setup['no_new_privs_before_filter'] = True
    setup['bounding_caps_zero_before_filter'] = True
    # os.write/read work on the already owned control socket; no new socket or
    # sendmsg is permitted after seccomp. That control FD is closed before code.
    phase['name'] = 'source_ack'
    os.write(channel.fileno(), encoded({'stage': 'ready', 'nonce': packet['nonce'],
        'runtime_files': len(runtime), 'runtime_pin_sha256': digest(encoded(runtime)),
        'work_input_pin': work_pin,
        'seccomp_sha256': digest(encoded(seccomp_program()))}))
    if os.read(channel.fileno(), 1) != b'G':
        raise ProbeError('source_ack_missing')
    channel.close()
    os.closerange(3, 64)
    phase['name'] = 'fixed_canaries'
    result = fixed_canaries(libc, packet, setup, runtime_names)
    os.write(1, encoded(result) + b'\n')
    return 0 if all(result['checks'].values()) else 78


def outer(packet):
    """Trusted owned launcher: fixed argv, no shell, no provider environment."""
    limits(1)
    libc = libc_api()
    prctl(libc, 1, signal.SIGKILL)
    if os.getppid() != packet['supervisor_pid']:
        raise ProbeError('supervisor_changed_before_launch')
    os.execv(UNSHARE, command(packet, Path(__file__).resolve()))


def command(packet, source):
    return [UNSHARE, '--user', '--map-root-user', '--mount', '--pid', '--fork',
        '--kill-child=SIGKILL', '--net', '--ipc', '--uts', '--propagation', 'private',
        '--', PYTHON, '-I', '-S', str(source), '--inner', encoded(packet).decode()]


def receive(channel, nonce, *, want_fd=False):
    data, ancillary, flags, _address = channel.recvmsg(MAX_PACKET, socket.CMSG_SPACE(array.array('i').itemsize))
    descriptors = []
    try:
        for level, kind, values in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise ProbeError('unexpected_control_descriptor')
            fds = array.array('i');fds.frombytes(values);descriptors.extend(fds)
        message = json.loads(data)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or message.get('nonce') != nonce:
            raise ProbeError('control_packet_invalid')
        if message.get('stage') == 'refused':
            if (message.get('phase') not in {'identity', 'runtime_inventory', 'rootfs',
                    'capabilities_seccomp', 'source_ack', 'fixed_canaries'}
                    or type(message.get('errno')) is not int or not 0 <= message['errno'] <= 4096):
                raise ProbeError('bootstrap_diagnostic_invalid')
            raise ProbeError('bootstrap_' + message['phase'] + '_errno_' + str(message['errno']))
        if len(descriptors) != (1 if want_fd else 0):
            raise ProbeError('control_descriptor_count')
        if want_fd:
            descriptor = descriptors.pop()
            os.set_inheritable(descriptor, False)
            return message, descriptor
        return message
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def verify_init(pidfd, launcher, message):
    if message.get('stage') != 'identity' or select.select([pidfd], [], [], 0)[0]:
        raise ProbeError('namespace_identity_unavailable')
    fields = dict(line.split(':', 1) for line in Path('/proc/self/fdinfo/' + str(pidfd)).read_text().splitlines() if ':' in line)
    host_pid = int(fields['Pid'])
    identifiers = [int(value) for value in fields['NSpid'].split()]
    status = dict(line.split(':', 1) for line in Path('/proc/' + str(host_pid) + '/status').read_text().splitlines() if ':' in line)
    namespace = os.stat('/proc/' + str(host_pid) + '/ns/pid')
    host_namespace = os.stat('/proc/self/ns/pid')
    if (host_pid <= 1 or int(status['PPid']) != launcher.pid or identifiers != [host_pid, 1]
            or [namespace.st_dev, namespace.st_ino] != message.get('namespace_identity')
            or (namespace.st_dev, namespace.st_ino) == (host_namespace.st_dev, host_namespace.st_ino)):
        raise ProbeError('namespace_identity_invalid')
    return {'acquired_by': 'SCM_RIGHTS_from_trusted_init', 'kernel_fdinfo_nspid': identifiers,
        'host_pid_namespace_identity': [host_namespace.st_dev, host_namespace.st_ino],
        'init_pid_namespace_identity': [namespace.st_dev, namespace.st_ino],
        'init_is_exact_launcher_child': True, 'namespace_numbers_used_for_host_signals': False}


def terminate_owned(launcher, launcher_fd, init_fd):
    """No PID from namespace output or a recovered journal is ever signalled."""
    deadline = time.monotonic() + 2
    init_exited = False
    signal_errors = False
    for descriptor in (init_fd, launcher_fd):
        if descriptor is not None:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError:
                signal_errors = True
    if launcher_fd is None and launcher.poll() is None:
        # Only the unreaped Popen child, never a namespace number or journal PID.
        try:
            launcher.kill()
        except OSError:
            signal_errors = True
    if init_fd is not None:
        init_exited = bool(select.select([init_fd], [], [], max(0, deadline - time.monotonic()))[0])
    try:
        launcher.wait(timeout=max(0, deadline - time.monotonic()))
        launcher_reaped = True
    except subprocess.TimeoutExpired:
        launcher_reaped = False
    return {'kernel_init_identity_received': init_fd is not None,
        'namespace_init_exited': init_exited, 'launcher_reaped': launcher_reaped,
        'cleanup_confirmed': init_fd is not None and init_exited and launcher_reaped and not signal_errors}


def collect(launcher, deadline):
    """Bound both pipes while reading, not after communicate has accumulated them."""
    streams = {launcher.stdout.fileno(): bytearray(), launcher.stderr.fileno(): bytearray()}
    opened = set(streams)
    for descriptor in opened:
        os.set_blocking(descriptor, False)
    while opened or launcher.poll() is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError('source_wall_deadline_exhausted')
        ready = select.select(list(opened), [], [], min(remaining, 0.05))[0]
        for descriptor in ready:
            part = os.read(descriptor, min(4096, MAX_OUTPUT + 1 - sum(map(len, streams.values()))))
            if not part:
                opened.remove(descriptor)
            else:
                streams[descriptor].extend(part)
                if sum(map(len, streams.values())) > MAX_OUTPUT:
                    raise ProbeError('output_bound_exceeded')
    return bytes(streams[launcher.stdout.fileno()]), bytes(streams[launcher.stderr.fileno()])


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ProbeError('startup_deadline_exhausted')
    return value


def run():
    report = {'schema': 1, 'result': 'linux_unqualified', 'prototype': 'synthetic_linux_foundation',
        'backend_execution_qualified': False, 'models': 0, 'account_operations': 0,
        'persistent_services': 0, 'host_configuration_changed': False,
        'controller_crash_qualified': False, 'durable_supervisor_qualified': False,
        'host': {'system': platform.system(), 'release': platform.release(), 'machine': platform.machine()},
        'checks': {}, 'reason': 'not_started'}
    if (platform.system() != 'Linux' or platform.machine() != 'x86_64'
            or os.geteuid() == 0 or sys.version_info[:2] != (3, 10)
            or not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal')):
        report['reason'] = 'unsupported_probe_host';return report
    limits(5)
    pins = {path: read_pin(path, root_owned=True)[1] for path in (PYTHON, UNSHARE)}
    source = Path(__file__).resolve()
    held_source, source_pin = read_pin(source)
    report['source_sha256'] = source_pin['sha256']
    report['component_sha256'] = component_pins(held_source)
    report['tool_pins'] = pins
    runtime = public_runtime()
    report['limits'] = {'memory_bytes_each': MEMORY, 'max_test_processes': 1, 'max_processes_excluding_controller': 3,
        'test_cpu_seconds': 1, 'supervisor_cpu_seconds': 5, 'launcher_cpu_seconds': 1,
        'startup_seconds': STARTUP_SECONDS, 'test_wall_seconds': TEST_SECONDS,
        'total_wall_seconds': TOTAL_SECONDS, 'cleanup_seconds': 2,
        'output_bytes': MAX_OUTPUT, 'root_tmpfs_bytes': ROOT_BYTES, 'work_tmpfs_bytes': WORK_BYTES, 'work_inodes': 128}
    got_signal = []
    previous_signal = signal.signal(signal.SIGUSR1, lambda *_: got_signal.append(True))
    init_fd = launcher_fd = None
    launcher = None
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix='synthetic-linux-foundation-') as folder:
        base = Path(folder);(base / 'root').mkdir(mode=0o700)
        outside = base / 'outside-canary';outside.write_bytes(b'synthetic');outside.chmod(0o600)
        outside_data, outside_pin = read_pin(outside)
        if outside_data != b'synthetic':
            raise ProbeError('synthetic_outside_canary_changed')
        report['outside_canary_before'] = {'accessible_read_verified': True, **outside_pin}
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            parent.settimeout(remaining(started + STARTUP_SECONDS))
            packet = {'nonce': os.urandom(16).hex(), 'supervisor_pid': os.getpid(),
                'control_fd': child.fileno(), 'root': str(base / 'root'), 'outside_canary': str(outside),
                'runtime': runtime, 'source_sha256': source_pin['sha256']}
            if len(encoded(packet)) > 64 * 1024:
                raise ProbeError('runtime_packet_too_large')
            launcher = subprocess.Popen([PYTHON, '-I', '-S', str(source), '--outer', encoded(packet).decode()],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, close_fds=True,
                pass_fds=(child.fileno(),), start_new_session=True)
            child.close()
            launcher_fd = os.pidfd_open(launcher.pid)
            message, candidate = receive(parent, packet['nonce'], want_fd=True)
            try:
                identity = verify_init(candidate, launcher, message)
            except BaseException:
                os.close(candidate);raise
            init_fd = candidate
            report['namespace_identity'] = identity
            parent.sendall(b'I')
            parent.settimeout(remaining(started + STARTUP_SECONDS))
            ready = receive(parent, packet['nonce'])
            if (ready.get('stage') != 'ready' or ready.get('seccomp_sha256') != digest(encoded(seccomp_program()))
                    or ready.get('runtime_pin_sha256') != digest(encoded(runtime))
                    or ready.get('runtime_files') != len(runtime)
                    or ready.get('work_input_pin', {}).get('sha256') != digest(b'synthetic')
                    or ready.get('work_input_pin', {}).get('size') != len(b'synthetic')):
                raise ProbeError('ready_packet_invalid')
            if time.monotonic() - started >= STARTUP_SECONDS:
                raise ProbeError('startup_deadline_exhausted')
            report['runtime_files'] = ready['runtime_files']
            report['runtime_pin_sha256'] = ready['runtime_pin_sha256']
            report['seccomp_sha256'] = ready['seccomp_sha256']
            report['work_input_pin'] = ready['work_input_pin']
            parent.sendall(b'G');parent.close()
            output, diagnostic = collect(launcher, min(time.monotonic() + TEST_SECONDS, started + TOTAL_SECONDS))
            measured = json.loads(output)
            if (launcher.returncode not in (0, 78) or set(measured) != {'checks', 'errors'}
                    or set(measured['checks']) != REQUIRED_CHECKS
                    or any(type(value) is not bool for value in measured['checks'].values())
                    or type(measured['errors']) is not dict
                    or any(type(value) is not int or value < 0 for value in measured['errors'].values())):
                raise ProbeError('canary_packet_invalid')
            report['checks'] = measured['checks'];report['errors'] = measured['errors']
            report['reason'] = 'foundation_guards_passed' if all(measured['checks'].values()) else 'foundation_guard_failed'
        except (OSError, ValueError, KeyError, ProbeError, subprocess.TimeoutExpired) as error:
            report['reason'] = str(error) if isinstance(error, ProbeError) else 'probe_start_or_exchange_failed'
        finally:
            parent.close();child.close()
            if launcher is not None:
                report['cleanup'] = terminate_owned(launcher, launcher_fd, init_fd)
                launcher.stdout.close();launcher.stderr.close()
            for descriptor in (init_fd, launcher_fd):
                if descriptor is not None:
                    os.close(descriptor)
            report['outside_canary_unchanged'] = outside.read_bytes() == b'synthetic'
            report['outside_canary_after'] = read_pin(outside)[1]
            report['outside_canary_same_pin'] = report['outside_canary_after'] == outside_pin
            report['outside_write_host_absent'] = not (base / 'root' / 'outside-created').exists()
            report['outside_signal_received'] = bool(got_signal)
            signal.signal(signal.SIGUSR1, previous_signal)
    if report['reason'] == 'foundation_guards_passed' and (
            not report.get('cleanup', {}).get('cleanup_confirmed')
            or not report['outside_canary_unchanged'] or not report['outside_canary_same_pin']
            or not report['outside_write_host_absent']
            or report['outside_signal_received']):
        report['reason'] = 'foundation_host_or_cleanup_guard_failed'
    report['elapsed_seconds'] = round(time.monotonic() - started, 6)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--inner')
    mode.add_argument('--outer')
    args = parser.parse_args(argv)
    try:
        if args.inner is not None:
            return inner(json.loads(args.inner))
        if args.outer is not None:
            outer(json.loads(args.outer));return 2
        result = run()
    except Exception as error:
        result = {'schema': 1, 'result': 'linux_unqualified', 'backend_execution_qualified': False,
                  'reason': str(error) if isinstance(error, ProbeError) else 'prototype_failed',
                  'errno': error.errno if isinstance(error, OSError) and type(error.errno) is int else 0,
                  'models': 0, 'account_operations': 0}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get('reason') == 'foundation_guards_passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
