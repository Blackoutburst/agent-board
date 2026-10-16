"""Private manifest and intent/receipt storage; no repository effects."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import threading
import uuid

from .dev_manifest import (DevelopmentError, MAX_MANIFEST_BYTES, absolute_directory,
                           canonical, digest, identity, sha256, validate_manifest)


MAX_STATE_BYTES = 2 * 1024 * 1024


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
                yield state
            except OSError:
                raise DevelopmentError('journal_access_failed') from None
            finally:
                self._in_lock = False
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

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
                    or set(operation) != {'id', 'kind', 'phase', 'request_hash', 'detail', 'receipt'}
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
            if (set(detail) != {'kind', 'path', 'before_sha256', 'after_sha256', 'bytes', 'before_signature'}
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

    def charge(self, state, *, read_bytes=0, patch_bytes=0, tests=0):
        limits = self.manifest['budgets']
        values = {'operations': 1, 'read_bytes': read_bytes, 'patch_bytes': patch_bytes, 'tests': tests}
        for key, amount in values.items():
            if state['used'][key] + amount > limits['max_' + key]:
                raise DevelopmentError(key + '_budget_exhausted')
        for key, amount in values.items():
            state['used'][key] += amount

    @staticmethod
    def require_settled(state):
        if any(operation['phase'] != 'complete' for operation in state['operations'].values()):
            raise DevelopmentError('reconciliation_required')
