"""Private manifest and intent/receipt storage; no repository effects."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import uuid

from .dev_manifest import (DevelopmentError, MAX_MANIFEST_BYTES, absolute_directory,
                           canonical, digest, identity, sha256, validate_manifest)


MAX_STATE_BYTES = 2 * 1024 * 1024


def _lease_error():
    raise DevelopmentError('journal_lock_lease_invalid') from None


def _lease_chain(path):
    descriptor = None
    result = []
    try:
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in [None] + list(Path(path).parts[1:]):
            if part is not None:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor);descriptor = child
            info = os.fstat(descriptor)
            result.append((info.st_dev, info.st_ino, info.st_uid, info.st_mode))
        return tuple(result)
    finally:
        if descriptor is not None:os.close(descriptor)


MAX_LOCK_FDINFO = 4096


def _linux_lock_supported():
    return sys.platform == 'linux'


def _parse_lock_fdinfo(data, *, pid, device, inode):
    """Strict observation codec, not authority without the private proc read."""
    if (any(type(value) is not int for value in (pid,device,inode))
            or pid <= 0 or device < 0 or inode <= 0
            or type(data) is not bytes or not 0 < len(data) <= MAX_LOCK_FDINFO
            or not data.endswith(b'\n') or b'\r' in data or b'\x00' in data):_lease_error()
    try:
        values = {}
        for line in data.decode('ascii').splitlines():
            key, value = line.split(':', 1)
            if key in values: _lease_error()
            values[key] = value.strip(' \t')
        if set(values) != {'pos','flags','mnt_id','ino','lock'}:_lease_error()
        def number(value, base=10, maximum=(1 << 64)-1):
            pattern = '[0-9]{1,20}' if base == 10 else '[0-7]{1,16}' if base == 8 else '[0-9a-fA-F]{1,8}'
            if not re.fullmatch(pattern, value):_lease_error()
            result = int(value, base)
            if result > maximum:_lease_error()
            return result
        number(values['pos'], maximum=(1 << 63)-1)
        flags = number(values['flags'], 8, (1 << 32)-1)
        mount = number(values['mnt_id'], maximum=(1 << 31)-1)
        if not mount or number(values['ino']) != inode or flags & os.O_ACCMODE != os.O_RDWR:_lease_error()
        lock = values['lock'].split()
        if (len(lock) != 8 or not lock[0].endswith(':') or not number(lock[0][:-1])
                or lock[1:4] != ['FLOCK','ADVISORY','WRITE'] or number(lock[4]) != pid
                or lock[6:] != ['0','EOF']):_lease_error()
        parts = lock[5].split(':')
        if (len(parts) != 3 or number(parts[0],16) != os.major(device)
                or number(parts[1],16) != os.minor(device) or number(parts[2]) != inode):_lease_error()
        return mount, flags
    except (UnicodeError,ValueError,TypeError,OverflowError):
        _lease_error()


def _linux_lock_observation(descriptor, *, pid, expected):
    """Read this process's fd/OFD lock state; never acquire or upgrade it.

    The kernel and procfs of the trusted host are prerequisites, not established
    by this observation. Unsupported or unavailable observations fail closed.
    """
    directory = stream = None
    try:
        if (not _linux_lock_supported() or type(descriptor) is not int or descriptor < 0
                or type(pid) is not int or pid <= 0 or os.getpid() != pid):_lease_error()
        if os.readlink('/proc/self') != str(pid):_lease_error()
        before = os.fstat(descriptor)
        if identity(before) != expected:_lease_error()
        directory = absolute_directory('/proc/' + str(pid) + '/fdinfo')
        stream = os.open(str(descriptor), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(stream).st_mode):_lease_error()
        data = b''
        while len(data) <= MAX_LOCK_FDINFO:
            part = os.read(stream, MAX_LOCK_FDINFO + 1 - len(data))
            if not part:break
            data += part
        observation = _parse_lock_fdinfo(data, pid=pid, device=before.st_dev, inode=before.st_ino)
        if identity(os.fstat(descriptor)) != expected or os.getpid() != pid:_lease_error()
        return observation
    except (OSError,ValueError,TypeError):
        _lease_error()
    finally:
        if stream is not None:os.close(stream)
        if directory is not None:os.close(directory)


class _JournalLockLease:
    """Private live controller handle; neither data nor authority to execute.

    A dup retains the actual locked open-file description. An independent
    observation must name that description, and the journal fd must report
    the same held exclusive lock without any attempt to acquire it.
    Python code running arbitrarily inside this trusted controller is outside
    this cooperative boundary; it can mutate objects or close arbitrary fds.
    """
    __slots__ = ('_journal','_token','_thread','_pid','_lock_fd','_private_fd',
                 '_identity','_chain','_guard','_active','_observation')

    def __init__(self, journal, token):
        self._journal,self._token = journal,token
        self._thread,self._pid = threading.current_thread(),os.getpid()
        self._lock_fd,self._private_fd,self._identity,_ = journal._lock_pin
        self._chain,self._guard,self._observation = None,None,None
        self._active = True

    def _issue(self):
        journal = self._journal
        if (journal._lock_fd != self._lock_fd or journal.private.fd != self._private_fd
                or identity(os.fstat(self._lock_fd)) != self._identity
                or identity(os.fstat(self._private_fd)) != journal._lock_pin[3]):_lease_error()
        journal.private.verify()
        journal.private.validate_file(self._lock_fd)
        self._chain = _lease_chain(journal.private.path)
        self._guard = os.dup(self._lock_fd)
        guard = _linux_lock_observation(self._guard, pid=self._pid, expected=self._identity)
        original = _linux_lock_observation(self._lock_fd, pid=self._pid, expected=self._identity)
        if guard != original:_lease_error()
        self._observation = guard

    def __reduce_ex__(self, protocol):
        raise TypeError('Live journal leases cannot be serialized')

    def __copy__(self):
        raise TypeError('Live journal leases cannot be copied')

    def __deepcopy__(self, memo):
        raise TypeError('Live journal leases cannot be copied')

    def _owns(self, descriptor):
        """Positive read-only kernel observation; cannot reacquire a lost lock."""
        directory = None
        try:
            if identity(os.fstat(descriptor)) != self._identity:return False
            directory = absolute_directory(self._journal.private.path)
            if identity(os.fstat(directory)) != self._journal.private.identity:return False
            current = os.stat('broker.lock', dir_fd=directory, follow_symlinks=False)
            if identity(current) != self._identity:return False
            observation = _linux_lock_observation(descriptor, pid=self._pid, expected=self._identity)
            return self._observation is None or observation == self._observation
        except (DevelopmentError,OSError,TypeError,ValueError):
            return False
        finally:
            if directory is not None:os.close(directory)

    def _validate(self, journal):
        try:
            if (not self._active or self._journal is not journal or journal._lease is not self
                    or journal._lock_token is not self._token or not journal._in_lock
                    or os.getpid() != self._pid or journal._process_id != self._pid
                    or threading.current_thread() is not self._thread
                    or journal._lock_owner != (self._pid,self._thread)
                    or journal._lock_fd != self._lock_fd or journal.private.fd != self._private_fd):
                _lease_error()
            journal.private.verify()
            if (identity(os.fstat(self._private_fd)) != journal.private.identity
                    or _lease_chain(journal.private.path) != self._chain):_lease_error()
            journal.private.validate_file(self._guard)
            journal.private.validate_file(self._lock_fd)
            if (not self._owns(self._guard) or not self._owns(self._lock_fd)):_lease_error()
            manifest = validate_manifest(decode(journal.private.read('manifest.json',MAX_MANIFEST_BYTES)))
            if digest(canonical(manifest)) != journal.manifest_hash:_lease_error()
            return True
        except (DevelopmentError,OSError,TypeError,ValueError):
            self._active = False
            _lease_error()

    def _revoke(self):
        # Invalidate before releasing any kernel lock. Unknown/reused numbers
        # are left untouched; this is not a claim of cleanup after tampering.
        self._active = False
        # A child after fork must never unlock the parent's shared description.
        if os.getpid() != self._pid:return
        owns_guard,owns_original = self._owns(self._guard),self._owns(self._lock_fd)
        try:
            if identity(os.fstat(self._journal.private.fd)) != self._journal.private.identity:
                self._journal.private.fd = None
        except (OSError,TypeError):
            self._journal.private.fd = None
        original_registered = self._journal._lock_fd == self._lock_fd
        if not owns_original or not original_registered:
            self._journal._lock_fd = None
        if owns_guard:
            fcntl.flock(self._guard,fcntl.LOCK_UN)
            os.close(self._guard)
            self._guard = None
        elif owns_original:
            fcntl.flock(self._lock_fd,fcntl.LOCK_UN)
        if owns_original and not original_registered:os.close(self._lock_fd)


class PrivateDirectory:
    def __init__(self, directory, *, create=False):
        self.path = Path(directory).absolute()
        self.fd = None
        if create:
            try:
                self.path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            except OSError:
                raise DevelopmentError('private_directory_unavailable') from None
        self.fd = absolute_directory(self.path)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            self.close()
            raise DevelopmentError('insecure_private_directory')
        self.identity = identity(info)

    def verify(self):
        fresh = absolute_directory(self.path)
        try:
            info = os.fstat(fresh)
            if (identity(info) != self.identity or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise DevelopmentError('private_directory_changed')
        finally:
            os.close(fresh)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def validate_file(descriptor):
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise DevelopmentError('insecure_private_file')

    def create(self, name, data):
        self.verify()
        descriptor = None
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=self.fd)
            self.validate_file(descriptor)
            self._write(descriptor, data)
            os.fsync(self.fd)
        except FileExistsError:
            raise DevelopmentError('private_file_exists') from None
        except OSError:
            raise DevelopmentError('private_write_failed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _write(descriptor, data):
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:
                raise DevelopmentError('private_write_failed')
            view = view[count:]
        os.fsync(descriptor)

    def read(self, name, maximum):
        self.verify()
        descriptor = None
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
            self.validate_file(descriptor)
            before = os.fstat(descriptor)
            if before.st_size > maximum:
                raise DevelopmentError('private_document_too_large')
            chunks, total = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum:
                    raise DevelopmentError('private_document_too_large')
                chunks.append(chunk)
            after = os.fstat(descriptor)
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if ((before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    or identity(current) != identity(before)):
                raise DevelopmentError('private_document_changed')
            return b''.join(chunks)
        except OSError:
            raise DevelopmentError('private_read_failed') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def replace(self, name, data):
        self.verify()
        # Validate the old destination before atomically replacing it. This
        # directory is private and all cooperating writers hold its lock.
        self.read(name, MAX_STATE_BYTES)
        temporary = 'write-' + uuid.uuid4().hex
        self.create(temporary, data)
        try:
            self.verify()
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        except OSError:
            raise DevelopmentError('private_write_failed') from None
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass


def decode(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise DevelopmentError('duplicate_document_key')
            value[key] = item
        return value
    try:
        return json.loads(data.decode('utf-8'), object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(DevelopmentError('invalid_document')))
    except (ValueError, UnicodeError, RecursionError):
        raise DevelopmentError('invalid_document') from None


class DevelopmentJournal:
    def __init__(self, directory):
        self.private = PrivateDirectory(directory)
        self._thread_lock = threading.RLock()
        self._in_lock = False
        self._lock_fd = None
        self._lease,self._lock_token,self._lock_owner = None,None,None
        self._lock_pin = None
        self._process_id = os.getpid()
        try:
            raw = self.private.read('manifest.json', MAX_MANIFEST_BYTES)
            self._manifest = validate_manifest(decode(raw))
            self.manifest_hash = digest(canonical(self.manifest))
            if self.private.path.is_relative_to(Path(self.manifest['root']).absolute()):
                raise DevelopmentError('journal_inside_workspace')
            try:
                self._lock_fd = os.open('broker.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                        0o600, dir_fd=self.private.fd)
            except OSError:
                raise DevelopmentError('journal_lock_unavailable') from None
            self.private.validate_file(self._lock_fd)
            with self.lock():
                pass
        except BaseException:
            self.close()
            raise

    @property
    def manifest(self):
        # Callers cannot mutate the durable plan through the public projection.
        return decode(canonical(self._manifest))

    def close(self):
        if self._lease is not None:
            self._lease._revoke();self._lease = None
        self._lock_token,self._lock_owner,self._lock_pin = None,None,None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
        self.private.close()

    @contextmanager
    def lock(self):
        with self._thread_lock:
            if self._in_lock:
                raise DevelopmentError('broker_busy')
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, TypeError):
                raise DevelopmentError('broker_busy') from None
            self._in_lock = True
            self._lock_token,self._lock_owner = object(),(os.getpid(),threading.current_thread())
            try:
                self.private.verify()
                current = os.stat('broker.lock', dir_fd=self.private.fd, follow_symlinks=False)
                if identity(current) != identity(os.fstat(self._lock_fd)):
                    raise DevelopmentError('journal_lock_changed')
                self.private.validate_file(self._lock_fd)
                manifest = validate_manifest(decode(self.private.read('manifest.json', MAX_MANIFEST_BYTES)))
                if digest(canonical(manifest)) != self.manifest_hash:
                    raise DevelopmentError('manifest_changed')
                state = decode(self.private.read('state.json', MAX_STATE_BYTES))
                self.validate_state(state)
                self._lock_pin = (self._lock_fd,self.private.fd,identity(current),self.private.identity)
                yield state
            except OSError:
                raise DevelopmentError('journal_access_failed') from None
            finally:
                lease,self._lease = self._lease,None
                self._lock_token,self._lock_owner = None,None
                self._lock_pin = None
                self._in_lock = False
                if lease is not None:lease._revoke()
                elif self._lock_fd is not None:fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def held_lock_lease(self):
        """Issue only inside this thread/process's existing acquisition.

        lock() still yields its original state dict. A live lease can be checked
        repeatedly during that acquisition; it cannot be replayed in another.
        """
        if (not _linux_lock_supported() or not self._in_lock or self._lock_token is None
                or self._lock_pin is None
                or self._process_id != os.getpid()
                or self._lock_owner != (os.getpid(),threading.current_thread())):_lease_error()
        try:
            if self._lease is None:
                self._lease = _JournalLockLease(self,self._lock_token)
                self._lease._issue()
            self.require_lock_lease(self._lease)
            return self._lease
        except (DevelopmentError,OSError,TypeError,ValueError):
            if self._lease is not None:self._lease._active = False
            _lease_error()

    def require_lock_lease(self, lease):
        if type(lease) is not _JournalLockLease:_lease_error()
        return lease._validate(self)

    def validate_state(self, state):
        if (type(state) is not dict or set(state) != {'schema', 'manifest_hash', 'used', 'operations'}
                or type(state['schema']) is not int or state['schema'] != 1
                or state['manifest_hash'] != self.manifest_hash
                or type(state['used']) is not dict
                or set(state['used']) != {'operations', 'read_bytes', 'patch_bytes', 'tests'}
                or any(type(value) is not int or value < 0 for value in state['used'].values())
                or type(state['operations']) is not dict or len(state['operations']) > 256):
            raise DevelopmentError('journal_corrupt')
        if (state['used']['operations'] < len(state['operations'])
                or any(amount > self._manifest['budgets']['max_' + key] for key, amount in state['used'].items())):
            raise DevelopmentError('journal_corrupt')
        for key, operation in state['operations'].items():
            if (type(operation) is not dict or operation.get('id') != key
                    or operation.get('kind') not in ('patch', 'test')
                    or operation.get('phase') not in ('intent', 'complete', 'needs_review')
                    or type(operation.get('request_hash')) is not str
                    or set(operation) not in ({'id', 'kind', 'phase', 'request_hash', 'detail', 'receipt'},
                        {'id', 'kind', 'phase', 'request_hash', 'detail', 'receipt', 'admission'})
                    or type(operation['detail']) is not dict
                    or (operation['receipt'] is not None and type(operation['receipt']) is not dict)):
                raise DevelopmentError('journal_corrupt')
            try:
                self._validate_operation(key, operation)
            except (DevelopmentError, KeyError, TypeError, ValueError):
                raise DevelopmentError('journal_corrupt') from None

    def _validate_operation(self, key, operation):
        sha256(key)
        sha256(operation['request_hash'])
        detail, receipt = operation['detail'], operation['receipt']
        if operation['kind'] == 'patch':
            if ('admission' in operation
                    or set(detail) != {'kind', 'path', 'before_sha256', 'after_sha256', 'bytes', 'before_signature'}
                    or detail['kind'] != 'patch' or detail['path'] not in self._manifest['files']
                    or type(detail['bytes']) is not int or not 0 <= detail['bytes'] <= self._manifest['budgets']['max_file_bytes']
                    or type(detail['before_signature']) is not list or len(detail['before_signature']) != 8
                    or any(type(value) is not int for value in detail['before_signature'])):
                raise DevelopmentError('journal_corrupt')
            sha256(detail['before_sha256'])
            sha256(detail['after_sha256'])
            request = {name: value for name, value in detail.items() if name != 'before_signature'}
        else:
            if detail not in self._manifest['tests']:
                raise DevelopmentError('journal_corrupt')
            request = {'kind': 'test', **detail}
        if digest(canonical(request)) != operation['request_hash']:
            raise DevelopmentError('journal_corrupt')
        admission = operation.get('admission')
        if 'admission' in operation:
            if type(admission) is not dict:
                raise DevelopmentError('journal_corrupt')
            expected = {'schema': 1, 'operation_id': key, 'manifest_hash': self.manifest_hash,
                'request_hash': operation['request_hash'], 'backend_id': detail['backend_id'],
                'root_identity': self._manifest['root_identity'], 'dispatch': admission.get('dispatch')}
            if (admission != expected
                    or type(admission['schema']) is not int
                    or type(admission['root_identity']) is not list
                    or len(admission['root_identity']) != 2
                    or any(type(value) is not int for value in admission['root_identity'])
                    or admission['dispatch'] not in ('pending', 'dispatched')):
                raise DevelopmentError('journal_corrupt')
        if operation['phase'] == 'intent':
            if receipt is not None:
                raise DevelopmentError('journal_corrupt')
            return
        if (type(receipt) is not dict or receipt.get('operation_id') != key
                or receipt.get('kind') != operation['kind'] or receipt.get('status') != operation['phase']):
            raise DevelopmentError('journal_corrupt')
        common = {'operation_id', 'kind', 'status', 'result'}
        if (receipt['result'] == 'needs_review') != (operation['phase'] == 'needs_review'):
            raise DevelopmentError('journal_corrupt')
        if operation['kind'] == 'patch':
            keys = common | {'path', 'before_sha256', 'after_sha256'}
            keys |= {'observed_sha256'} if operation['phase'] == 'needs_review' else {'bytes'}
            if (set(receipt) != keys or receipt['result'] not in
                    ('needs_review', 'applied', 'unchanged', 'not_applied', 'verified_after_restart')
                    or any(receipt[name] != detail[name] for name in ('path', 'before_sha256', 'after_sha256'))):
                raise DevelopmentError('journal_corrupt')
            if operation['phase'] == 'needs_review':
                sha256(receipt['observed_sha256'])
            elif (type(receipt['bytes']) is not int or not 0 <= receipt['bytes'] <= self._manifest['budgets']['max_file_bytes']):
                raise DevelopmentError('journal_corrupt')
        else:
            keys = common | {'name'}
            metadata = {'cleanup_confirmed', 'output_bytes', 'output_sha256'}
            if receipt.get('result') == 'not_started':
                extra = {'backend_admission'} if admission is not None and admission['dispatch'] == 'dispatched' else set()
                if (admission is None
                        or operation['phase'] != 'complete'
                        or set(receipt) != keys | {'cleanup_confirmed', 'started', 'reason',
                            'manifest_hash', 'request_hash', 'admission_sha256'} | extra
                        or receipt['name'] != detail['name']
                        or receipt['cleanup_confirmed'] is not True or receipt['started'] is not False
                        or receipt['manifest_hash'] != self.manifest_hash
                        or receipt['request_hash'] != operation['request_hash']
                        or receipt['admission_sha256'] != digest(canonical(admission))
                        or type(receipt['reason']) is not str
                        or re.fullmatch('[a-z][a-z0-9_]{0,99}', receipt['reason']) is None):
                    raise DevelopmentError('journal_corrupt')
                if extra:
                    from .dev_broker import TestRequest, validate_backend_not_started
                    request = TestRequest(key, self.manifest_hash, self._manifest['root'],
                        tuple(self._manifest['root_identity']), detail['name'],
                        detail['timeout_seconds'], detail['max_output_bytes'])
                    validate_backend_not_started(request, detail['backend_id'], receipt['backend_admission'])
                return
            if admission is not None and admission['dispatch'] != 'dispatched':
                raise DevelopmentError('journal_corrupt')
            if (receipt['name'] != detail['name'] or receipt['result'] not in ('passed', 'failed', 'cancelled', 'needs_review')
                    or set(receipt) not in (keys, keys | metadata)):
                raise DevelopmentError('journal_corrupt')
            if 'cleanup_confirmed' not in receipt:
                if operation['phase'] != 'needs_review':
                    raise DevelopmentError('journal_corrupt')
            else:
                if (type(receipt['cleanup_confirmed']) is not bool
                        or (operation['phase'] == 'complete' and not receipt['cleanup_confirmed'])
                        or type(receipt['output_bytes']) is not int
                        or not 0 <= receipt['output_bytes'] <= detail['max_output_bytes']):
                    raise DevelopmentError('journal_corrupt')
                sha256(receipt['output_sha256'])

    def save(self, state):
        self.validate_state(state)
        encoded = canonical(state)
        if len(encoded) > MAX_STATE_BYTES:
            raise DevelopmentError('journal_full')
        self.private.replace('state.json', encoded)

    def charge(self, state, *, operations=1, read_bytes=0, patch_bytes=0, tests=0):
        limits = self.manifest['budgets']
        values = {'operations': operations, 'read_bytes': read_bytes, 'patch_bytes': patch_bytes, 'tests': tests}
        for key, amount in values.items():
            if state['used'][key] + amount > limits['max_' + key]:
                raise DevelopmentError(key + '_budget_exhausted')
        for key, amount in values.items():
            state['used'][key] += amount

    @staticmethod
    def require_settled(state):
        if any(operation['phase'] != 'complete' for operation in state['operations'].values()):
            raise DevelopmentError('reconciliation_required')
