"""Process-local content facts for public files that this controller cannot write.

No Board decision, grant, private-file cache or executable bytes are retained.
Only a complete first hash of a held descriptor admits a file. Subsequent
observations retain that digest only while its descriptor, name, root-owned
ancestry, complete metadata signature and controller epoch remain unchanged.
An observed change poisons the epoch, rather than learning a new baseline.
"""
from __future__ import annotations

import atexit
import ctypes
import hashlib
import os
from pathlib import Path
import re
import stat
import sys
import threading

from .dev_manifest import DevelopmentError

MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_FILES = 352
MAX_DIRECTORIES = 128
# Reserve space below 512 for bounded, short-lived /proc observations.
MAX_HELD_FDS = 480
MAX_TOTAL_FDS = 512
# Ownership/ctime from a userspace or remote file server is insufficient for
# this optimization. Unknown filesystems keep the caller's original full read.
LOCAL_FILESYSTEMS = frozenset({0xef53, 0x58465342, 0x9123683e, 0x01021994, 0x794c7630})
_CLOEXEC = getattr(os, 'O_CLOEXEC', 0)
_NOFOLLOW = getattr(os, 'O_NOFOLLOW', 0)


class ProtectedPublicPinsError(DevelopmentError):
    pass


class _Ineligible(Exception):
    pass


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _metadata(info):
    """A separate seam for synthetic kernel observations; never a grant hook."""
    return _signature(info)


def _directory_key(info):
    value = _metadata(info)
    return value[:5]


def _root_directory(info):
    value = _metadata(info)
    return stat.S_ISDIR(value[2]) and value[3] == 0 and not value[2] & 0o022


def _root_file(info, maximum, executable, allow_empty):
    value = _metadata(info)
    return (stat.S_ISREG(value[2]) and value[3] == 0 and not value[2] & 0o022
            and not value[2] & 0o6000 and value[5] == 1
            and (0 if allow_empty else 1) <= value[6] <= maximum
            and (not executable or bool(value[2] & 0o111)))


def _fs_magic(fd):
    libc = ctypes.CDLL(None, use_errno=True)
    call = libc.fstatfs
    call.argtypes, call.restype = (ctypes.c_int, ctypes.c_void_p), ctypes.c_int
    buffer = (ctypes.c_long * 64)()
    if call(fd, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), 'procfs observation failed')
    return buffer[0]


def _parse_controller(status, uid_map, gid_map, namespace, *, pid, tid, ids, groups):
    """Accept only the current non-root thread in the full identity user map."""
    if not (len(status) <= 32768 and len(uid_map) <= 4096 and len(gid_map) <= 4096):
        return None
    try:
        selected = {}
        needed = {'Pid', 'Tgid', 'Uid', 'Gid', 'Groups', 'NoNewPrivs',
                  'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'}
        for line in status.decode('ascii', 'strict').splitlines():
            name, value = line.split(':', 1)
            if name in needed:
                if name in selected:
                    return None
                selected[name] = value.strip()
        if set(selected) != needed:
            return None
        def numbers(value):
            pieces = value.split()
            if len(pieces) > 128 or any(re.fullmatch('[0-9]{1,10}', p) is None for p in pieces):
                raise ValueError
            return tuple(int(p) for p in pieces)
        uid, euid, gid, egid = ids
        if (uid != euid or uid == 0 or gid != egid
                or numbers(selected['Pid']) != (tid,) or numbers(selected['Tgid']) != (pid,)
                or numbers(selected['Uid']) != (uid,) * 4
                or numbers(selected['Gid']) != (gid,) * 4
                or numbers(selected['Groups']) != tuple(sorted(groups))
                or numbers(selected['NoNewPrivs']) != (1,)):
            return None
        caps = []
        for name in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
            if re.fullmatch('[0-9a-f]{16}', selected[name]) is None:
                return None
            value = int(selected[name], 16)
            if name != 'CapBnd' and value != 0:
                return None
            caps.append(value)
        # The initial kernel user namespace exposes this entire identity map.
        # A rootless child namespace's limited or remapped IDs cannot pass.
        full = (0, 0, 4294967295)
        if numbers(uid_map.decode('ascii')) != full or numbers(gid_map.decode('ascii')) != full:
            return None
        if (type(namespace) is not tuple or len(namespace) != 2
                or not all(type(v) is int and v > 0 for v in namespace)):
            return None
        return (pid, uid, euid, gid, egid, tuple(sorted(groups)), tuple(caps), namespace, full)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        return None


def _controller_facts():
    if sys.platform != 'linux' or not _CLOEXEC or not _NOFOLLOW:
        return None
    pid, tid = os.getpid(), threading.get_native_id()
    ids = (os.getuid(), os.geteuid(), os.getgid(), os.getegid())
    if ids[0] == 0 or ids[0] != ids[1] or ids[2] != ids[3]:
        return None
    descriptors = []
    try:
        def opened(path, flags, *, dir_fd=None):
            fd = _CACHE._open(path, flags | _CLOEXEC, dir_fd=dir_fd, temporary=True)
            descriptors.append(fd)
            return fd
        proc = opened('/proc', os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW)
        if _fs_magic(proc) != 0x9fa0:
            return None
        # /proc/TID addresses this exact thread, even when TID != process PID.
        thread = opened(str(tid), os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=proc)
        def read(name, maximum):
            fd = opened(name, os.O_RDONLY | _NOFOLLOW | os.O_NONBLOCK, dir_fd=thread)
            if not stat.S_ISREG(os.fstat(fd).st_mode) or _fs_magic(fd) != 0x9fa0:
                raise ValueError
            data = bytearray()
            while len(data) <= maximum:
                part = os.read(fd, min(4096, maximum + 1 - len(data)))
                if not part:
                    break
                data.extend(part)
            _CACHE._close(fd);descriptors.remove(fd)
            if len(data) > maximum:
                raise ValueError
            return bytes(data)
        status = read('status', 32768)
        uid_map, gid_map = read('uid_map', 4096), read('gid_map', 4096)
        ns = opened('ns', os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=thread)
        if _fs_magic(ns) != 0x9fa0:
            return None
        # This is a kernel magic link in the verified own-thread procfs dir.
        user = opened('user', os.O_RDONLY, dir_fd=ns)
        if _fs_magic(user) != 0x6e736673:
            return None
        info = os.fstat(user)
        result = _parse_controller(status, uid_map, gid_map, (info.st_dev, info.st_ino),
            pid=pid, tid=tid, ids=ids, groups=os.getgroups())
        if (os.getpid() != pid or threading.get_native_id() != tid
                or ids != (os.getuid(), os.geteuid(), os.getgid(), os.getegid())):
            return None
        return result
    except (OSError, ValueError, _Ineligible):
        return None
    finally:
        for fd in reversed(descriptors):
            _CACHE._close(fd)


class _PublicPins:
    def __init__(self):
        self.lock = threading.RLock()
        self.pid = os.getpid()
        self.epoch = None
        self.invalid = False
        self.disabled = False
        self.users = 0
        self.owned = {}
        self.directories = {}
        self.files = {}
        self.pending = set()
        self.pending_bytes = 0
        self.total_bytes = 0

    def __reduce_ex__(self, _):
        raise TypeError('Public pin handles are process-local')

    def __copy__(self):
        raise TypeError('Public pin handles cannot be copied')

    def __deepcopy__(self, _):
        raise TypeError('Public pin handles cannot be copied')

    def _check(self):
        if self.invalid or os.getpid() != self.pid:
            self._poison()
            raise ProtectedPublicPinsError('protected_public_epoch_changed')

    def _active(self):
        self._check()
        if self.disabled:
            raise ProtectedPublicPinsError('protected_public_disabled')

    def _poison(self):
        self.invalid = True
        if self.users == 0:
            self._close_all()

    def _changed(self):
        self._poison()
        raise ProtectedPublicPinsError('protected_public_epoch_changed')

    def _open(self, path, flags, *, dir_fd=None, temporary=False):
        with self.lock:
            self._active()
            if len(self.owned) >= (MAX_TOTAL_FDS if temporary else MAX_HELD_FDS):
                raise _Ineligible
            fd = os.open(path, flags, **({} if dir_fd is None else {'dir_fd': dir_fd}))
            if fd in self.owned:
                # An owned descriptor was closed outside this handle. Do not
                # silently replace its identity with an unrelated new open.
                os.close(fd)
                self._changed()
            try:
                info = os.fstat(fd)
                self.owned[fd] = (info.st_dev, info.st_ino)
            except BaseException:
                os.close(fd)
                raise
            return fd

    def _close(self, fd):
        with self.lock:
            if fd in self.owned:
                expected = self.owned.pop(fd)
                try:
                    info = os.fstat(fd)
                    if (info.st_dev, info.st_ino) == expected:os.close(fd)
                except OSError:pass

    def _close_all(self):
        for fd in tuple(self.owned):
            self._close(fd)
        self.owned.clear()

    def _context(self, value):
        self._active()
        if self.epoch is not None and value != self.epoch:
            self._changed()

    def _directory(self, path, parent, name):
        found = self.directories.get(path)
        if found is None:
            if len(self.directories) >= MAX_DIRECTORIES:
                raise _Ineligible
            fd = self._open('/' if parent is None else name,
                os.O_RDONLY | os.O_DIRECTORY | _CLOEXEC | _NOFOLLOW,
                dir_fd=None if parent is None else parent['fd'])
            try:
                info = os.fstat(fd)
                if not _root_directory(info) or _fs_magic(fd) not in LOCAL_FILESYSTEMS:
                    raise _Ineligible
            except BaseException:
                self._close(fd)
                raise
            found = {'fd':fd, 'parent':parent, 'name':name, 'key':_directory_key(info)}
            self.directories[path] = found
        self._verify_directory(found)
        return found

    def _verify_directory(self, node):
        info = os.fstat(node['fd'])
        parent = node['parent']
        named = os.stat('/' if parent is None else node['name'], follow_symlinks=False,
            **({} if parent is None else {'dir_fd':parent['fd']}))
        if (not _root_directory(info) or not _root_directory(named)
                or _fs_magic(node['fd']) not in LOCAL_FILESYSTEMS
                or node['key'] != _directory_key(info) or node['key'] != _directory_key(named)):
            self._changed()

    def _chain(self, path):
        result = []
        current = ''
        parent = None
        for name in (None, *Path(path).parent.parts[1:]):
            current = '/' if name is None else current.rstrip('/')+'/'+name
            parent = self._directory(current, parent, name)
            result.append(parent)
        return result

    def _verify(self, entry):
        for node in entry['chain']:
            self._verify_directory(node)
        current = os.fstat(entry['fd'])
        named = os.stat(entry['name'], dir_fd=entry['chain'][-1]['fd'], follow_symlinks=False)
        if (entry['signature'] != _metadata(current) or entry['signature'] != _metadata(named)
                or _fs_magic(entry['fd']) not in LOCAL_FILESYSTEMS):
            self._changed()

    @staticmethod
    def _facts(entry):
        value = entry['signature']
        return {'sha256':entry['sha256'], 'size':value[6], 'identity':[value[0],value[1]],
            'uid':value[3], 'gid':value[4], 'mode':value[2], 'nlink':value[5], 'signature':list(value)}

    def pin(self, path, *, maximum, executable, allow_empty):
        if (type(path) is not str or not path.startswith('/') or str(Path(path)) != path
                or '..' in Path(path).parts or len(path) > 4096 or len(Path(path).parts) > 128
                or type(maximum) is not int or not 0 <= maximum <= MAX_FILE_BYTES
                or type(executable) is not bool or type(allow_empty) is not bool):
            raise ProtectedPublicPinsError('protected_public_request_invalid')
        fd = None;reserved = 0;pending = False
        with self.lock:
            self._check()
            if self.disabled:
                return None
            self.users += 1
        try:
            first = _controller_facts()
            with self.lock:
                self._context(first)
                if first is None:
                    return None
                entry = self.files.get(path)
                if entry is not None:
                    self._verify(entry)
                    if not _root_file(os.fstat(entry['fd']), maximum, executable, allow_empty):
                        self._changed()
                else:
                    if path in self.pending or len(self.files)+len(self.pending) >= MAX_FILES:
                        return None
                    self.pending.add(path);pending = True
                    try:
                        chain = self._chain(path)
                        fd = self._open(Path(path).name, os.O_RDONLY | os.O_NONBLOCK | _CLOEXEC | _NOFOLLOW,
                            dir_fd=chain[-1]['fd'])
                        info = os.fstat(fd)
                        if (not _root_file(info, maximum, executable, allow_empty)
                                or _fs_magic(fd) not in LOCAL_FILESYSTEMS):
                            return None
                        signature = _metadata(info)
                        if self.total_bytes+self.pending_bytes+signature[6] > MAX_TOTAL_BYTES:
                            return None
                        reserved = signature[6];self.pending_bytes += reserved
                    except (OSError, _Ineligible):
                        return None
            if entry is None:
                # No global lock is held during a potentially 300MiB cold hash.
                hashed = hashlib.sha256();size = 0
                while True:
                    with self.lock:self._active()
                    part = os.read(fd, min(1024*1024, maximum+1-size))
                    if not part:
                        break
                    size += len(part)
                    if size > maximum:
                        with self.lock:self._changed()
                    hashed.update(part)
                entry = {'fd':fd, 'chain':chain, 'name':Path(path).name,
                    'signature':signature, 'sha256':hashed.hexdigest()}
                if size != signature[6]:
                    with self.lock:self._changed()
            second = _controller_facts()
            with self.lock:
                self._context(second)
                if first != second:
                    self._changed()
                self._verify(entry)
                if self.epoch is None:
                    self.epoch = first
                if path not in self.files:
                    self.files[path] = entry
                    self.total_bytes += entry['signature'][6]
                    fd = None  # Owned by this epoch until explicit close/exit.
                return self._facts(entry)
        except (OSError, _Ineligible):
            with self.lock:self._changed()
        finally:
            with self.lock:
                if fd is not None:self._close(fd)
                if pending:self.pending.discard(path)
                self.pending_bytes -= reserved
                self.users -= 1
                if (self.invalid or self.disabled) and self.users == 0:self._close_all()

    def disable(self):
        with self.lock:
            self._check()
            self.disabled = True
            if self.users == 0:self._close_all()

    def close(self):
        with self.lock:self._poison()

    def _before_fork(self):
        self.lock.acquire()

    def _after_fork_parent(self):
        self.lock.release()

    def _after_fork_child(self):
        # Only the forking thread survives. Close even another thread's cold FD.
        self._close_all()
        self.invalid = True
        self.users = 0
        self.lock = threading.RLock()


_CACHE = _PublicPins()


def protected_public_pin(path, *, maximum=MAX_FILE_BYTES, executable=False, allow_empty=False):
    """Return fresh verified public facts, or None for an initially ineligible file.

    None means the caller must retain its original complete reader/hasher. An
    active epoch's changed file/controller instead raises, never falls back.
    The returned dictionary is data, not an execution or authorization handle.
    """
    return _CACHE.pin(os.fspath(path), maximum=maximum, executable=executable, allow_empty=allow_empty)


def disable_protected_public_pins_for_process():
    """Cold helper entry only: preserve original reads before low FD limits."""
    _CACHE.disable()


def close_protected_public_pins():
    """Irreversible teardown. A closed epoch cannot be adopted or warmed again."""
    _CACHE.close()


atexit.register(close_protected_public_pins)
if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=lambda:_CACHE._before_fork(),
        after_in_parent=lambda:_CACHE._after_fork_parent(),
        after_in_child=lambda:_CACHE._after_fork_child())
