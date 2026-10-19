"""Inert, private input copies for a future Linux named-test backend.

This module does not qualify or register a backend. It never executes source,
starts a process, writes a journal, or changes Darwin protocols. The default
contexts perform no authorization callback; explicit authorize_reads=True in
the locked context asks the concrete broker for both permissions each pass.
The context owns the existing broker lock and checks its durable checkout fence;
it is a cooperative controller boundary, not an atomic filesystem snapshot
against an unrelated writer or an administrator. Future integration must charge
its bounded reads before capture and keep full authorization/stop checks.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading
import unicodedata
import uuid

from .dev_broker import DevelopmentBroker
from .dev_fence import DevelopmentFence
from .dev_journal import DevelopmentJournal, decode
from .dev_manifest import (DevelopmentError, RepositoryFiles, canonical,
                           check_content, digest, identity, relative_path,
                           sha256, validate_manifest)


SCHEMA = 'blackoutburst-linux-input-snapshot-v1'
MAX_FILES = 128
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024
MAX_DOCUMENT_BYTES = 512 * 1024
MAX_DEPTH = 32


def _error(code='linux_input_snapshot_invalid'):
    raise DevelopmentError(code)


def _integer(value, minimum=0, maximum=2 ** 64 - 1):
    return type(value) is int and minimum <= value <= maximum


def _path(value):
    if (type(value) is not str or not value.startswith('/') or value == '/'
            or str(Path(value)) != value or any(part in ('', '.', '..') for part in value.split('/')[1:])
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _error()
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError:
        _error()
    if len(value.encode()) > 4096 or len(value.split('/')) > 256:
        _error()
    return value


def _directory_pin(path, info):
    return {'path': path, 'identity': list(identity(info)),
            'uid': info.st_uid, 'mode': info.st_mode}


def _chain(path):
    """Read canonical absolute ancestors without following any symbolic link."""
    _path(path)
    descriptor = None
    pins = []
    try:
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        current = ''
        pins.append(_directory_pin('/', os.fstat(descriptor)))
        for part in path.split('/')[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current += '/' + part
            pins.append(_directory_pin(current, os.fstat(descriptor)))
        return pins
    except OSError:
        _error('linux_input_directory_changed')
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _relative_parents(files, path):
    parts = relative_path(path)
    descriptor = os.dup(files.fd)
    pins = [_directory_pin(files.root, os.fstat(descriptor))]
    current = files.root
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                _error('linux_input_directory_changed')
            current += '/' + part
            pins.append(_directory_pin(current, info))
        return descriptor, parts[-1], pins
    except BaseException:
        os.close(descriptor)
        raise


def _manifest(manifest):
    validate_manifest(manifest)
    _path(manifest['root'])
    normalized = set()
    for path in manifest['files']:
        if len(relative_path(path)) > MAX_DEPTH:
            _error('linux_input_depth_exceeded')
        normalized_path = unicodedata.normalize('NFC', path).casefold()
        if normalized_path in normalized:
            _error('linux_input_alias_refused')
        normalized.add(normalized_path)
    return {'max_files': MAX_FILES,
            'max_file_bytes': min(MAX_FILE_BYTES, manifest['budgets']['max_file_bytes']),
            'max_total_bytes': min(MAX_TOTAL_BYTES, manifest['budgets']['max_read_bytes'])}


def validate_planner_snapshot(value, manifest):
    """Validate the exact current DevelopmentPlanner._snapshot projection."""
    _manifest(manifest)
    if (type(value) is not dict or set(value) != {'sha256', 'files'}
            or type(value['files']) is not dict
            or set(value['files']) != set(manifest['files'])):
        _error('linux_input_planner_snapshot_invalid')
    sha256(value['sha256'])
    for hashed in value['files'].values():
        sha256(hashed)
    if value['sha256'] != digest(canonical(value['files'])):
        _error('linux_input_planner_snapshot_invalid')
    return value


def _validate_pin(pin, path):
    if (type(pin) is not dict or set(pin) != {'path', 'identity', 'uid', 'mode'}
            or pin['path'] != path or type(pin['identity']) is not list
            or len(pin['identity']) != 2 or not _integer(pin['identity'][0])
            or not _integer(pin['identity'][1], 1) or not _integer(pin['uid'])
            or not _integer(pin['mode'], 0, 0o177777)
            or not stat.S_ISDIR(pin['mode'])):
        _error()


def validate_input_snapshot(value, *, manifest, planner_snapshot):
    """Validate metadata only; a valid document is never execution proof."""
    limits = _manifest(manifest)
    validate_planner_snapshot(planner_snapshot, manifest)
    if (type(value) is not dict or set(value) != {'schema', 'manifest_hash',
            'operation_id', 'fence_record_sha256', 'root', 'root_chain',
            'limits', 'files', 'planner_snapshot'} or value['schema'] != SCHEMA
            or value['manifest_hash'] != digest(canonical(manifest))
            or value['root'] != manifest['root'] or type(value['limits']) is not dict or value['limits'] != limits
            or any(type(value['limits'].get(key)) is not int for key in limits)
            or type(value['planner_snapshot']) is not dict or value['planner_snapshot'] != planner_snapshot
            or type(value['root_chain']) is not list
            or type(value['files']) is not dict
            or set(value['files']) != set(manifest['files'])):
        _error()
    try:
        if type(value['operation_id']) is not str or str(uuid.UUID(value['operation_id'])) != value['operation_id']:
            _error()
    except ValueError:
        _error()
    sha256(value['fence_record_sha256'])
    parts = value['root'].split('/')[1:]
    expected_paths = ['/'] + ['/' + '/'.join(parts[:index]) for index in range(1, len(parts) + 1)]
    if len(value['root_chain']) != len(expected_paths):
        _error()
    for pin, path in zip(value['root_chain'], expected_paths):
        _validate_pin(pin, path)
    validate_planner_snapshot(value['planner_snapshot'], manifest)
    if value['root_chain'][-1]['identity'] != manifest['root_identity']:
        _error()
    total, identities = 0, set()
    directory_paths, directory_identities = {}, {}
    def directory(pin):
        path, pair = pin['path'], tuple(pin['identity'])
        if ((path in directory_paths and directory_paths[path] != pin)
                or (pair in directory_identities and directory_identities[pair] != path)):
            _error('linux_input_alias_refused')
        directory_paths[path] = pin
        directory_identities[pair] = path
    for pin in value['root_chain']:
        directory(pin)
    for path, entry in value['files'].items():
        if (type(entry) is not dict or set(entry) != {'sha256', 'bytes', 'signature', 'parents'}
                or not _integer(entry['bytes'], 0, limits['max_file_bytes'])
                or type(entry['signature']) is not list or len(entry['signature']) != 8
                or any(not _integer(item) for item in entry['signature'])
                or entry['signature'][1] == 0 or entry['signature'][2] != entry['bytes']
                or entry['signature'][5] != 1 or entry['signature'][6] != os.geteuid()
                or entry['signature'][7] > 0o177777
                or not stat.S_ISREG(entry['signature'][7]) or entry['signature'][7] & 0o022
                or type(entry['parents']) is not list):
            _error()
        sha256(entry['sha256'])
        if entry['sha256'] != planner_snapshot['files'][path]:
            _error('linux_input_planner_snapshot_mismatch')
        parents = relative_path(path)[:-1]
        if len(entry['parents']) != len(parents) + 1:
            _error()
        for index, pin in enumerate(entry['parents']):
            expected = value['root'] + (('/' + '/'.join(parents[:index])) if index else '')
            _validate_pin(pin, expected)
            directory(pin)
            if pin['uid'] != os.geteuid() or pin['mode'] & 0o022:
                _error()
        if entry['parents'][0] != value['root_chain'][-1]:
            _error()
        file_identity = tuple(entry['signature'][:2])
        if file_identity in identities:
            _error('linux_input_alias_refused')
        identities.add(file_identity)
        total += entry['bytes']
    if total > limits['max_total_bytes'] or len(canonical(value)) > MAX_DOCUMENT_BYTES:
        _error('linux_input_snapshot_too_large')
    return value


def _fence_record(broker, fence, binding, operation_id):
    """Read a real durable reservation; a caller's string is not lock proof."""
    record = fence.inspect_checkout(broker.manifest['root_identity'])
    manifest = broker.manifest
    expected_scope = {key: manifest['scope'][key] for key in
                      ('owner_id', 'agent_id', 'project_id', 'ticket_id')}
    if (record is None or record['binding'] != binding or record['operation_id'] != operation_id
            or any(binding['scope'].get(key) != item for key, item in expected_scope.items())
            or binding['scope'].get('board_session_id') != manifest['scope']['session_id']
            or binding['root_identity'] != manifest['root_identity']
            or binding['journal'] != {'path': str(broker.journal.private.path),
                                      'identity': list(broker.journal.private.identity)}
            or ('family' in record and record['family']['stage'] != 'planner')):
        _error('linux_input_fence_mismatch')
    return record


class LinuxInputSnapshot:
    """Private controller copy, valid only while its real broker lock is held."""
    def __init__(self, broker, fence, binding, operation_id, planner_snapshot, lock_lease, *, authorize_reads=False, _capture_permit=None):
        self._broker, self._fence = broker, fence
        self._authorize_reads = authorize_reads
        self._capture_permit = _capture_permit
        self._lock_lease = lock_lease
        self._binding, self._operation_id = decode(canonical(binding)), operation_id
        self._planner_snapshot = decode(canonical(planner_snapshot))
        self._thread = threading.get_ident()
        self._active = True
        self._record = _fence_record(broker, fence, self._binding, operation_id)
        self._document, self._contents = self._capture()
        self.revalidate()

    def _held(self):
        if (not self._active or threading.get_ident() != self._thread
                or not self._broker.journal._in_lock):
            _error('linux_input_snapshot_not_held')
        self._broker.journal.require_lock_lease(self._lock_lease)
        private = self._broker.journal.private
        private.verify()
        private.validate_file(self._broker.journal._lock_fd)
        current = os.stat('broker.lock', dir_fd=private.fd, follow_symlinks=False)
        if (identity(current) != identity(os.fstat(self._broker.journal._lock_fd))
                or digest(canonical(decode(private.read('manifest.json', 128 * 1024)))) != self._broker.journal.manifest_hash):
            _error('linux_input_broker_changed')
        if _fence_record(self._broker, self._fence, self._binding, self._operation_id) != self._record:
            _error('linux_input_fence_changed')

    def _capture(self):
        self._held()
        if self._broker.journal._linux_input_profile:
            from .linux_input_admission import _CapturePermit
            if type(self._capture_permit) is not _CapturePermit:
                _error('linux_input_capture_permit_invalid')
            self._capture_permit.take(self)
        if self._authorize_reads or self._broker.journal._linux_input_profile:
            self._broker._authorize('repo.read_file')
            self._broker._authorize('repo.run_test')
            self._held()
        manifest = self._broker.manifest
        limits = _manifest(manifest)
        root_chain = _chain(manifest['root'])
        # No alias of a private control directory may become an input root.
        for private in (self._broker.journal.private.path, self._fence.private.path):
            private_chain = _chain(str(private))
            if (manifest['root_identity'] in [pin['identity'] for pin in private_chain]
                    or private_chain[-1]['identity'] in [pin['identity'] for pin in root_chain]):
                _error('linux_input_control_overlap')
        entries, contents, total = {}, {}, 0
        files = RepositoryFiles(manifest['root'], manifest['root_identity'])
        try:
            for path in sorted(manifest['files']):
                parent, name, parents = _relative_parents(files, path)
                try:
                    data, signature = files.read_at(parent, name, limits['max_file_bytes'])
                    check_content(data)
                    fresh, _, current = _relative_parents(files, path)
                    os.close(fresh)
                    if current != parents:
                        _error('linux_input_directory_changed')
                    total += len(data)
                    if total > limits['max_total_bytes']:
                        _error('linux_input_snapshot_too_large')
                    entries[path] = {'sha256': digest(data), 'bytes': len(data),
                                     'signature': list(signature), 'parents': parents}
                    contents[path] = data
                finally:
                    os.close(parent)
            files.verify()
        except OSError:
            _error('linux_input_directory_changed')
        finally:
            files.close()
        if _chain(manifest['root']) != root_chain:
            _error('linux_input_directory_changed')
        projection = {'files': {path: entry['sha256'] for path, entry in entries.items()}}
        projection['sha256'] = digest(canonical(projection['files']))
        if projection != self._planner_snapshot:
            _error('linux_input_planner_snapshot_mismatch')
        document = {'schema': SCHEMA, 'manifest_hash': digest(canonical(manifest)),
                    'operation_id': self._operation_id,
                    'fence_record_sha256': digest(canonical(self._record)),
                    'root': manifest['root'], 'root_chain': root_chain, 'limits': limits,
                    'files': entries, 'planner_snapshot': projection}
        validate_input_snapshot(document, manifest=manifest, planner_snapshot=self._planner_snapshot)
        self._held()
        return document, contents

    def document(self):
        self._held()
        return decode(canonical(self._document))

    def read(self, path):
        self._held()
        relative_path(path)
        if path not in self._contents:
            _error('linux_input_path_not_allowed')
        return self._contents[path]

    def revalidate(self):
        """Call before source ACK and after result in a future backend."""
        try:
            document, _ = self._capture()
            if document != self._document:
                _error('linux_input_snapshot_changed')
            return True
        except DevelopmentError:
            self._close()
            raise

    def _close(self):
        self._active = False
        self._contents.clear()


@contextmanager
def capture_input_snapshot(broker, *, fence, binding, operation_id, planner_snapshot):
    """Hold the actual broker lock, capture twice, and recheck before release.

    This accepts concrete existing controllers only, not a duck-typed lock or
    caller asserted proof. Authorization must precede entry; this function does
    not invoke the broker's possibly remote callback. It cannot replace the
    future backend's input/scope/request protocol or durable read accounting.
    """
    if (type(broker) is not DevelopmentBroker or type(broker.journal) is not DevelopmentJournal
            or type(fence) is not DevelopmentFence or broker.enabled is not True):
        _error('linux_input_controller_required')
    validate_planner_snapshot(planner_snapshot, broker.manifest)
    with broker.journal.lock():
        with capture_input_snapshot_locked(broker,lock_lease=broker.journal.held_lock_lease(),
                fence=fence,binding=binding,operation_id=operation_id,planner_snapshot=planner_snapshot) as snapshot:
            yield snapshot


@contextmanager
def capture_input_snapshot_locked(broker, *, lock_lease, fence, binding, operation_id, planner_snapshot, authorize_reads=False, _capture_permit=None):
    """Capture under the exact live lease; never acquire or release its lock.

    The owner must keep DevelopmentJournal.lock() open for this complete
    context. A bool, callback, copied handle or expired acquisition is refused.
    Durable read accounting remains the caller's obligation. With the explicit
    authorize_reads flag each capture invokes the concrete broker permissions
    (read_file and run_test); False preserves the original metadata-only API.
    """
    if (type(broker) is not DevelopmentBroker or type(broker.journal) is not DevelopmentJournal
            or type(fence) is not DevelopmentFence or broker.enabled is not True):
        _error('linux_input_controller_required')
    if type(authorize_reads) is not bool:
        _error('linux_input_controller_required')
    broker.journal.require_lock_lease(lock_lease)
    validate_planner_snapshot(planner_snapshot,broker.manifest)
    snapshot = None
    try:
        snapshot = LinuxInputSnapshot(broker,fence,binding,operation_id,planner_snapshot,lock_lease,
                                      authorize_reads=authorize_reads, _capture_permit=_capture_permit)
        yield snapshot
        snapshot.revalidate()
    finally:
        if snapshot is not None:snapshot._close()
