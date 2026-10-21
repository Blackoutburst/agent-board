"""Trusted Linux namespace executor, extracted from the reviewed foundation probe.

No process, mount, import of a test, or qualification occurs at import. Only the
pinned supervisor may call outer/inner with sealed descriptors and exact pins.
The product never imports tests/qualify_linux_foundation.py.
"""
from __future__ import annotations
import array
import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import resource
import select
import signal
import socket
import stat
import sys
import unicodedata

from .dev_manifest import DevelopmentError, canonical, digest, relative_path
from .dev_journal import decode

MEMORY = 256 * 1024 * 1024
WORK_BYTES = 4 * 1024 * 1024
INPUT_BYTES = 8 * 1024 * 1024
ROOT_BYTES = 32 * 1024 * 1024
STARTUP_SECONDS = 5
MAX_PACKET = 128 * 1024
MAX_RUNTIME_BYTES = 128 * 1024 * 1024
MAX_RUNTIME_FILES = 256
MAX_SCRIPT_BYTES = 65536
MAX_INPUT_PACKET = 4 * 1024 * 1024 + 128 * 1024 + 64
class ProbeError(DevelopmentError):
    pass


def require(ok, reason='linux_namespace_invalid'):
    if not ok:
        raise ProbeError(reason)


def read_public(path, *, root_owned=False, maximum=64 * 1024 * 1024, expected=None, namespace=False, allow_empty=False):
    """Canonical path and held nofollow chain; only explicit public runtime files."""
    path = Path(path)
    require(type(namespace) is bool and type(allow_empty) is bool
            and (not namespace or os.getpid() == 1 and os.geteuid() == 0))
    require(path.is_absolute() and '..' not in path.parts and len(path.parts) <= 128)
    nodes, leaf = [], None
    try:
        for name in [None] + list(path.parent.parts[1:]):
            fd = os.open('/' if name is None else name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                         **({} if name is None else {'dir_fd': nodes[-1][0]}))
            info = os.fstat(fd)
            nodes.append((fd, name, info))
            require(not info.st_mode & 0o022 and (namespace or info.st_uid in (0, os.geteuid())))
        leaf = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=nodes[-1][0])
        before = os.fstat(leaf)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                and (0 if allow_empty else 1) <= before.st_size <= maximum and not before.st_mode & 0o022
                and (namespace or (before.st_uid == 0 if root_owned else before.st_uid in (0, os.geteuid()))))
        data = bytearray()
        while len(data) <= maximum:
            part = os.read(leaf, min(65536, maximum + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        keys = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        after = os.fstat(leaf)
        linked = os.stat(path.name, dir_fd=nodes[-1][0], follow_symlinks=False)
        require(len(data) == before.st_size <= maximum
                and all(getattr(before, key) == getattr(after, key) == getattr(linked, key) for key in keys))
        for i, (fd, name, initial) in enumerate(nodes):
            current = os.fstat(fd)
            linked = os.stat('/' if i == 0 else name, follow_symlinks=False,
                             **({} if i == 0 else {'dir_fd': nodes[i-1][0]}))
            require(all(getattr(initial, key) == getattr(current, key) == getattr(linked, key)
                        for key in keys[:5]))
        pin = {'identity': [before.st_dev, before.st_ino], 'size': len(data), 'sha256': digest(data),
               'uid': before.st_uid, 'gid': before.st_gid, 'mode': before.st_mode, 'nlink': before.st_nlink}
        if expected is not None:
            require(type(expected) is dict and set(expected) == set(pin), 'linux_runtime_changed')
            require(all(pin[key] == expected[key] for key in pin if not namespace or key not in ('uid', 'gid')),
                    'linux_runtime_changed')
        return bytes(data), pin
    finally:
        if leaf is not None:
            os.close(leaf)
        for fd, _, _ in reversed(nodes):
            os.close(fd)


def public_runtime():
    """Only this process's loaded public files, never accounts or other maps."""
    require(len(sys.modules)<=4096,'linux_runtime_inventory_limit')
    paths = {str(Path(sys.executable).resolve())}
    for module in tuple(sys.modules.values()):
        for key in ('__file__', '__cached__'):
            value = getattr(module, key, None)
            if isinstance(value, str) and value.startswith(('/usr/', '/lib/')):
                require(len(value)<=4096,'linux_runtime_inventory_limit')
                path = Path(value).resolve(strict=True)
                if path.is_file():
                    paths.add(str(path))
                    require(len(paths)<=MAX_RUNTIME_FILES,'linux_runtime_inventory_limit')
    fd = os.open('/proc/self/maps', os.O_RDONLY | os.O_CLOEXEC)
    try:
        data = os.read(fd, 256 * 1024 + 1)
        require(len(data) <= 256 * 1024, 'linux_runtime_inventory_limit')
        for line in data.decode('ascii', 'strict').splitlines():
            fields = line.split(None, 5)
            if len(fields) == 6 and fields[5].startswith(('/usr/', '/lib/')):
                require(len(fields[5])<=4096,'linux_runtime_inventory_limit')
                paths.add(str(Path(fields[5]).resolve(strict=True)))
                require(len(paths)<=MAX_RUNTIME_FILES,'linux_runtime_inventory_limit')
    finally:
        os.close(fd)
    require(1 <= len(paths) <= MAX_RUNTIME_FILES, 'linux_runtime_inventory_limit')
    result, size = {}, 0
    for path in sorted(paths):
        _, pin = read_public(path, root_owned=True,maximum=min(64*1024*1024,MAX_RUNTIME_BYTES-size),allow_empty=True)
        size += pin['size']
        require(size <= MAX_RUNTIME_BYTES, 'linux_runtime_inventory_limit')
        result[path] = pin
    return result


def sealed_memfd(name, data):
    require(type(data) is bytes and len(data) <= MAX_INPUT_PACKET)
    fd = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        offset = 0
        while offset < len(data):
            count = os.write(fd, data[offset:])
            require(count > 0)
            offset += count
        os.lseek(fd, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, seals)
        require(fcntl.fcntl(fd, fcntl.F_GET_SEALS) == seals)
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_sealed(fd, expected_hash, maximum):
    require(type(fd) is int and fd >= 3)
    info = os.fstat(fd)
    seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
    require(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum
            and fcntl.fcntl(fd, fcntl.F_GET_SEALS) == seals)
    os.lseek(fd, 0, os.SEEK_SET)
    data = bytearray()
    while len(data) <= maximum:
        part = os.read(fd, min(65536, maximum + 1 - len(data)))
        if not part:
            break
        data.extend(part)
    require(len(data) == info.st_size <= maximum and digest(data) == expected_hash)
    return bytes(data)


REQUIRED_CHECKS = frozenset({'pid_namespace_init', 'outside_process_hidden_before_filter',
    'no_new_privs_before_filter', 'bounding_caps_zero_before_filter', 'inside_read', 'inside_write',
    'outside_write', 'readonly_file_write', 'runtime_write', 'outside_read', 'old_root_absent',
    'home_proc_private_state_absent', 'work_volume_bound', 'network_loopback', 'network_unix_path',
    'network_unix_abstract', 'fork', 'exec', 'session', 'process_group', 'outside_signal_denied',
    'clone3', 'x32_abi_denied', 'namespace_change_denied', 'caps_zero_after_filter',
    'nproc_raise_denied', 'hard_limits'})

DENIED_SYSCALLS = frozenset({
    41, 42, 43, 44, 45, 46, 47, 49, 50, 51, 52, 53, 54, 55, 288, 299, 307,
    56, 57, 58, 59, 62, 109, 112, 101, 105, 106, 113, 114, 116, 117, 119,
    122, 123, 126, 129, 130, 132, 133, 134, 135, 136, 155, 157, 160, 161,
    163, 164, 165, 166, 167, 168, 169, 170, 171, 172, 173, 175, 176, 177,
    200, 234, 246, 248, 249, 250, 272, 298, 303, 304, 308, 310, 311, 313,
    317, 320, 321, 322, 323, 424, 425, 426, 427, 428, 429, 430, 431, 432,
    433, 434, 435, 438, 440, 442,
})

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


def limits(cpu, *, file_bytes=WORK_BYTES):
    require(type(cpu) is int and 1 <= cpu <= 61)
    require(type(file_bytes) is int and file_bytes in (WORK_BYTES, MAX_INPUT_PACKET))
    for kind, value in ((resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_CORE, 0),
                        (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_FSIZE, file_bytes),
                        (resource.RLIMIT_CPU, cpu)):
        resource.setrlimit(kind, (value, value))


def input_contents(raw, snapshot_sha256):
    """Minimal inner decoder: no host UID/grant/manifest validator in namespace."""
    magic = b'BB_LINUX_INPUT_V1\n'
    prefix = len(magic) + 8
    require(type(raw) is bytes and prefix < len(raw) <= MAX_INPUT_PACKET
            and raw.startswith(magic))
    size = int.from_bytes(raw[len(magic):prefix], 'big')
    require(0 < size <= 128 * 1024 and prefix + size <= len(raw))
    header_raw, data = raw[prefix:prefix+size], raw[prefix+size:]
    header = decode(header_raw)
    require(type(header) is dict and set(header) == {'schema', 'snapshot_sha256', 'files'}
            and header['schema'] == 'blackoutburst-linux-test-inputs-v1'
            and header['snapshot_sha256'] == snapshot_sha256
            and canonical(header) == header_raw and type(header['files']) is list
            and 1 <= len(header['files']) <= 128 and len(data) <= 4 * 1024 * 1024)
    result, offset, directories, aliases = {}, 0, set(), set()
    for entry in header['files']:
        require(type(entry) is dict and set(entry) == {'path', 'offset', 'bytes', 'sha256'}
                and type(entry['offset']) is int and entry['offset'] == offset
                and type(entry['bytes']) is int and 0 <= entry['bytes'] <= 1048576)
        parts = relative_path(entry['path'])
        alias=unicodedata.normalize('NFC',entry['path']).casefold()
        require(alias not in aliases and len(parts) <= 32
                and (not result or entry['path'] > next(reversed(result))))
        aliases.add(alias)
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        piece = data[offset:offset+entry['bytes']]
        require(len(piece) == entry['bytes'] and digest(piece) == entry['sha256'])
        result[entry['path']] = piece
        offset += entry['bytes']
    require(offset == len(data) and len(directories) + len(result) + 1 <= 256
            and not set(result)&directories)
    return result


def read_source_release(channel):
    """Wait on the existing channel without renewing the SOURCE window."""
    descriptor = channel.fileno()
    require(select.select([descriptor], [], [], STARTUP_SECONDS)[0], 'linux_source_ack_missing')
    raw = os.read(descriptor, 1025)
    require(0 < len(raw) <= 1024, 'linux_source_ack_missing')
    return raw


def validate_source_release(raw,nonce,*,_clock=None):
    """Check the original dated frame in the namespace, without resetting it."""
    from .dev_test_supervisor import _control_clock,_validate_control_window
    require(type(raw) is bytes and 0<len(raw)<=1024,'linux_source_ack_missing')
    value=decode(raw)
    require(type(value) is dict and set(value)=={'schema','nonce','control'}
            and value['schema']=='linux-source-release-v1' and value['nonce']==nonce
            and canonical(value)==raw and type(value['control']) is dict,'linux_source_ack_missing')
    window=value['control']
    require(type(window.get('sequence')) is int and 2<=window['sequence']<=65536,
            'linux_source_ack_missing')
    now=(_control_clock if _clock is None else _clock)()
    # The supervising process has validated the complete sequence. This local
    # isolated frame checks shape, nonce and the unchanged oldest timestamp.
    _validate_control_window({**window,'sequence':1},nonce=nonce,now=now)
    return window


def rootfs(libc, directory, runtime, contents):
    base = Path(directory)
    mount(libc, None, '/', 1 << 14 | 1 << 18)
    mount(libc, 'tmpfs', base, 2 | 4, 'tmpfs', 'size=32M,nr_inodes=2048,mode=755')
    for part in ('work', 'inputs', 'runtime', 'oldroot'):
        (base / part).mkdir(mode=0o700)
    (base / 'readonly-canary').write_bytes(b'synthetic')
    names = []
    for index, (path, pin) in enumerate(runtime.items()):
        read_public(path, expected=pin, namespace=True, allow_empty=True)
        target = base / 'runtime' / str(index)
        target.touch(mode=0o444)
        mount(libc, path, target, 1 << 12)
        mount(libc, None, target, 1 << 12 | 32 | 1 | 2 | 4 | 8)
        names.append('/runtime/' + str(index))
    mount(libc, 'tmpfs', base / 'inputs', 2 | 4 | 8, 'tmpfs', 'size=8M,nr_inodes=512,mode=700')
    for name, data in contents.items():
        target = base / 'inputs' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        try:
            offset = 0
            while offset < len(data):
                count = os.write(fd, data[offset:])
                require(count > 0)
                offset += count
        finally:
            os.close(fd)
    mount(libc, None, base / 'inputs', 32 | 1 | 2 | 4 | 8)
    mount(libc, 'tmpfs', base / 'work', 2 | 4 | 8, 'tmpfs', 'size=4M,nr_inodes=128,mode=700')
    (base / 'work' / 'input').write_bytes(b'synthetic')
    os.chdir(base)
    checked(libc.syscall(155, ctypes.c_char_p(b'.'), ctypes.c_char_p(b'oldroot')))
    os.chdir('/')
    checked(libc.umount2(b'/oldroot', 2))
    os.rmdir('/oldroot')
    mount(libc, None, '/', 32 | 1 | 2 | 4)
    os.chdir('/inputs')
    return names


def kernel_checks(libc, packet, setup, runtime_names, cpu):
    checks = dict(setup)
    def denied(name, call, errors):
        try:
            call()
        except OSError as error:
            checks[name] = error.errno in errors
        else:
            checks[name] = False
    def write(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(fd, b'synthetic')
        finally:
            os.close(fd)
    checks['inside_read'] = Path('/work/input').read_bytes() == b'synthetic'
    write('/work/new')
    checks['inside_write'] = Path('/work/new').read_bytes() == b'synthetic'
    fs = {errno.EROFS, errno.EACCES, errno.EPERM}
    denied('outside_write', lambda: write('/outside-created'), fs)
    denied('readonly_file_write', lambda: write('/readonly-canary'), fs)
    denied('runtime_write', lambda: write(runtime_names[0]), fs)
    denied('inputs_write', lambda: write('/inputs/unapproved'), fs)
    denied('outside_read', lambda: Path('/outside-controller-state').read_bytes(), {errno.ENOENT})
    checks['old_root_absent'] = not Path('/oldroot').exists()
    checks['home_proc_private_state_absent'] = all(not Path(p).exists() for p in ('/home', '/root', '/proc', '/run'))
    volume = os.statvfs('/work')
    checks['work_volume_bound'] = volume.f_blocks * volume.f_frsize <= WORK_BYTES and volume.f_files <= 128
    for name, family, address in (('network_loopback', socket.AF_INET, ('127.0.0.1', 1)),
            ('network_unix_path', socket.AF_UNIX, '/outside-socket'),
            ('network_unix_abstract', socket.AF_UNIX, '\0linux-test-denied')):
        def connect(family=family, address=address):
            with socket.socket(family, socket.SOCK_STREAM) as channel:
                channel.connect(address)
        denied(name, connect, {errno.EPERM})
    def fork():
        child = os.fork()
        if child == 0:
            os._exit(0)
        os.waitpid(child, 0)
    denied('fork', fork, {errno.EPERM})
    denied('exec', lambda: os.execve('/invalid', ['/invalid'], {}), {errno.EPERM})
    denied('session', os.setsid, {errno.EPERM})
    denied('process_group', lambda: os.setpgid(0, 0), {errno.EPERM})
    denied('outside_signal_denied', lambda: os.kill(packet['supervisor_pid'], signal.SIGUSR1), {errno.EPERM})
    def syscall(number):
        ctypes.set_errno(0)
        if libc.syscall(number, 0, 0, 0, 0, 0, 0) == -1:
            raise OSError(ctypes.get_errno(), 'syscall_refused')
    for name, number in (('clone3', 435), ('x32_abi_denied', 0x40000000 | 39), ('namespace_change_denied', 272)):
        denied(name, lambda number=number: syscall(number), {errno.EPERM})
    checks['caps_zero_after_filter'] = not any(caps(libc))
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (2, 2))
        checks['nproc_raise_denied'] = False
    except (ValueError, OSError):
        checks['nproc_raise_denied'] = True
    checks['hard_limits'] = all(resource.getrlimit(kind) == (value, value) for kind, value in (
        (resource.RLIMIT_AS, MEMORY), (resource.RLIMIT_NOFILE, 64), (resource.RLIMIT_FSIZE, WORK_BYTES),
        (resource.RLIMIT_NPROC, 1), (resource.RLIMIT_CPU, cpu)))
    require(set(checks) == REQUIRED_CHECKS | {'inputs_write'} and all(v is True for v in checks.values()),
            'linux_kernel_guard_failed')
    return checks


def inner(packet):
    """Only trusted pinned helper calls this, before any approved script runs."""
    channel = socket.socket(fileno=packet['control_fd'])
    try:
        libc = libc_api()
        cpu = packet['cpu_seconds']
        limits(cpu)
        require(os.getpid() == 1 and 1 <= cpu <= 61)
        prctl(libc, 1, signal.SIGKILL)
        channel.settimeout(STARTUP_SECONDS)
        namespace = os.stat('/proc/self/ns/pid')
        pidfd = os.pidfd_open(os.getpid())
        try:
            channel.sendmsg([canonical({'stage': 'identity', 'nonce': packet['nonce'],
                'namespace_identity': [namespace.st_dev, namespace.st_ino]})],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [pidfd]).tobytes())])
        finally:
            os.close(pidfd)
        require(channel.recv(1) == b'I', 'linux_identity_ack_missing')
        script = read_sealed(packet['script_fd'], packet['script_sha256'], MAX_SCRIPT_BYTES)
        raw = read_sealed(packet['inputs_fd'], packet['input_packet_sha256'], MAX_INPUT_PACKET)
        contents = input_contents(raw, packet['input_snapshot_sha256'])
        os.close(packet['script_fd']); os.close(packet['inputs_fd'])
        setup = {'pid_namespace_init': True}
        try:
            unexpected = os.pidfd_open(packet['supervisor_pid'])
        except ProcessLookupError:
            setup['outside_process_hidden_before_filter'] = True
        else:
            os.close(unexpected)
            setup['outside_process_hidden_before_filter'] = False
        runtime_names = rootfs(libc, packet['root'], packet['runtime'], contents)
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        lock_down(libc)
        setup['no_new_privs_before_filter'] = True
        setup['bounding_caps_zero_before_filter'] = True
        checks = kernel_checks(libc, packet, setup, runtime_names, cpu)
        os.write(channel.fileno(), canonical({'stage': 'ready', 'nonce': packet['nonce'],
            'runtime_sha256': digest(canonical(packet['runtime'])), 'script_sha256': digest(script),
            'input_packet_sha256': digest(raw), 'checks': checks}))
        source_release=read_source_release(channel)
        channel.close()
        os.closerange(3, 64)
        allowed = {'json': json, 'pathlib': sys.modules['pathlib']}
        import builtins
        restricted = dict(vars(builtins))
        def approved_import(name, globals=None, locals=None, fromlist=(), level=0):
            if level != 0 or name not in allowed:
                raise ImportError('unapproved_import')
            return allowed[name]
        restricted['__import__'] = approved_import
        validate_source_release(source_release,packet['nonce'])
        try:
            exec(compile(script, '/trusted_test.py', 'exec', dont_inherit=True),
                 {'__name__': '__main__', '__file__': '/trusted_test.py', '__builtins__': restricted})
        except SystemExit as exit:
            return 0 if exit.code is None or type(exit.code) is int and exit.code == 0 else 1
        except BaseException:
            return 1
        return 0
    except BaseException:
        return 78
    finally:
        channel.close()


def outer(packet, command):
    """Owned launcher only; no shell or provider environment."""
    limits(packet['cpu_seconds'])
    libc = libc_api()
    prctl(libc, 1, signal.SIGKILL)
    require(os.getppid() == packet['supervisor_pid'], 'linux_supervisor_changed')
    os.execv(command[0], command)
