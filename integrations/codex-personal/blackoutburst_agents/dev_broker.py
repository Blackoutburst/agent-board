"""Disabled-by-default development primitives, without a model transport.

Only complete UTF-8 replacements of existing allowlisted files are supported.
The controller must own an isolated checkout: flock serializes broker writers,
but portable rename is not a CAS against an unrelated noncooperating writer.
There is deliberately no shell, real test backend, listener or CLI integration.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import re
import stat

from .dev_journal import DevelopmentJournal, PrivateDirectory
from .dev_manifest import (DevelopmentError, RepositoryFiles, authorize, canonical,
                           check_content, digest, opaque, relative_path, sha256, signature)


@dataclass(frozen=True)
class TestRequest:
    """Trusted-backend request; never printed or sent to a model."""
    operation_id: str
    manifest_hash: str
    root: str
    root_identity: tuple[int, int]
    name: str
    timeout_seconds: int
    max_output_bytes: int


class DevelopmentBroker:
    def __init__(self, directory, *, authorize_scope, enabled=False, test_backend=None):
        if type(enabled) is not bool or not callable(authorize_scope):
            raise DevelopmentError('invalid_controller')
        self.enabled = enabled
        self.authorize_scope = authorize_scope
        self.test_backend = test_backend
        self.journal = DevelopmentJournal(directory)
        self._test_admissions = set()

    @property
    def manifest(self):
        return self.journal.manifest

    def close(self):
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _authorize(self, action):
        if not self.enabled:
            raise DevelopmentError('broker_disabled')
        authorize(self.authorize_scope, self.manifest, action)

    def _files(self):
        return RepositoryFiles(self.manifest['root'], self.manifest['root_identity'])

    def _path(self, path):
        relative_path(path)
        if path not in self.manifest['files']:
            raise DevelopmentError('path_not_allowed')
        return path

    @staticmethod
    def _operation_id(key):
        return digest(opaque(key, 'invalid_idempotency_key').encode('ascii'))

    def _existing(self, state, key, request):
        operation_id = self._operation_id(key)
        operation = state['operations'].get(operation_id)
        if operation:
            if operation['request_hash'] != digest(canonical(request)):
                raise DevelopmentError('idempotency_conflict')
            if operation['phase'] != 'complete':
                raise DevelopmentError('reconciliation_required')
            return dict(operation['receipt'])
        self.journal.require_settled(state)
        return None

    def _intent(self, state, key, kind, request, detail):
        operation_id = self._operation_id(key)
        operation = {'id': operation_id, 'kind': kind, 'phase': 'intent',
            'request_hash': digest(canonical(request)), 'detail': detail, 'receipt': None}
        state['operations'][operation_id] = operation
        self.journal.save(state)
        return operation

    def _finish(self, state, operation, result, **metadata):
        receipt = {'operation_id': operation['id'], 'kind': operation['kind'],
                   'status': 'needs_review' if result == 'needs_review' else 'complete',
                   'result': result, **metadata}
        operation['phase'] = receipt['status']
        operation['receipt'] = receipt
        self.journal.save(state)
        return dict(receipt)

    def read_file(self, path):
        """Explicit content channel. All other methods return only metadata."""
        self._authorize('repo.read_file')
        path = self._path(path)
        with self.journal.lock() as state:
            remaining = self.manifest['budgets']['max_read_bytes'] - state['used']['read_bytes']
            maximum = min(self.manifest['budgets']['max_file_bytes'], remaining)
            self.journal.charge(state, read_bytes=maximum)
            self.journal.save(state)  # Attempt budget survives failed reads.
            files = self._files()
            try:
                self._authorize('repo.read_file')
                data, _ = files.read(path, maximum)
                text = check_content(data)
                return {'path': path, 'bytes': len(data), 'sha256': digest(data), 'content': text}
            finally:
                files.close()

    def patch_file(self, key, path, expected_sha256, content):
        """Apply one exact full-file patch, never an interpreted patch command."""
        self._authorize('repo.patch_file')
        path = self._path(path)
        sha256(expected_sha256)
        if type(content) is not str:
            raise DevelopmentError('invalid_patch')
        try:
            data = content.encode('utf-8', 'strict')
        except UnicodeError:
            raise DevelopmentError('invalid_patch') from None
        if len(data) > self.manifest['budgets']['max_file_bytes']:
            raise DevelopmentError('file_too_large')
        check_content(data)
        after_hash = digest(data)
        request = {'kind': 'patch', 'path': path, 'before_sha256': expected_sha256,
                   'after_sha256': after_hash, 'bytes': len(data)}
        with self.journal.lock() as state:
            previous = self._existing(state, key, request)
            if previous is not None:
                return previous
            # Reserve bounded preimage/CAS reads before touching the target.
            self.journal.charge(state, read_bytes=2 * self.manifest['budgets']['max_file_bytes'],
                                patch_bytes=len(data))
            self.journal.save(state)
            files = self._files()
            try:
                self._authorize('repo.patch_file')
                before, before_signature = files.read(path, self.manifest['budgets']['max_file_bytes'])
                check_content(before)
                if digest(before) != expected_sha256:
                    raise DevelopmentError('patch_hash_conflict')
                detail = {**request, 'before_signature': list(before_signature)}
                operation = self._intent(state, key, 'patch', request, detail)
                if expected_sha256 == after_hash:
                    return self._finish(state, operation, 'unchanged', path=path,
                        before_sha256=expected_sha256, after_sha256=after_hash, bytes=len(data))
                self._apply(files, operation, data)
                return self._finish(state, operation, 'applied', path=path,
                    before_sha256=expected_sha256, after_sha256=after_hash, bytes=len(data))
            finally:
                files.close()

    @staticmethod
    def _stage_name(operation):
        return '.bb-dev-' + operation['id'] + '.tmp'

    def _apply(self, files, operation, data):
        detail = operation['detail']
        descriptor = None
        with files.parent(detail['path']) as (parent, name):
            stage = self._stage_name(operation)
            try:
                self._authorize('repo.patch_file')
                files.verify_parent(detail['path'], parent)
                descriptor = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o600, dir_fd=parent)
                PrivateDirectory.validate_file(descriptor)
                PrivateDirectory._write(descriptor, data)
                # Check the source content AND inode/version after staging.
                current, current_signature = files.read_at(parent, name, self.manifest['budgets']['max_file_bytes'])
                if (digest(current) != detail['before_sha256']
                        or list(current_signature) != detail['before_signature']):
                    raise DevelopmentError('patch_hash_conflict')
                os.fchmod(descriptor, stat.S_IMODE(current_signature[-1]) & 0o777)
                os.fsync(descriptor)
                self._authorize('repo.patch_file')
                files.verify_parent(detail['path'], parent)
                final = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if signature(final) != current_signature:
                    raise DevelopmentError('file_changed')
                # Atomic visibility for this one file. The filesystem provides
                # no conditional rename against unrelated external writers.
                os.replace(stage, name, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            except OSError:
                raise DevelopmentError('patch_write_uncertain') from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                # Keep an unapplied stage for explicit reconciliation. There
                # is no retry loop that might silently overwrite newer work.

    def _test_suite(self, name):
        opaque(name, 'invalid_test_name', 64)
        suite = next((item for item in self.manifest['tests'] if item['name'] == name), None)
        if suite is None:
            raise DevelopmentError('test_not_allowed')
        return suite

    def _qualified_test(self, name):
        suite = self._test_suite(name)
        backend = self.test_backend
        try:
            qualified = (backend is not None and backend.backend_id == suite['backend_id']
                and callable(backend.run) and callable(backend.lookup)
                and backend.qualified_for(self.journal.manifest_hash, dict(suite)) is True)
        except Exception:
            qualified = False
        if not qualified:
            raise DevelopmentError('test_backend_unqualified')
        return suite

    def _test_request(self, operation):
        suite = self._qualified_test(operation['detail']['name'])
        return TestRequest(operation['id'], self.journal.manifest_hash, self.manifest['root'],
            tuple(self.manifest['root_identity']), suite['name'], suite['timeout_seconds'], suite['max_output_bytes'])

    @staticmethod
    def _test_result(result, maximum):
        if (type(result) is not dict or set(result) != {'status', 'cleanup_confirmed', 'output_bytes', 'output_sha256'}
                or result['status'] not in ('passed', 'failed', 'cancelled', 'needs_review')
                or type(result['cleanup_confirmed']) is not bool
                or type(result['output_bytes']) is not int or not 0 <= result['output_bytes'] <= maximum):
            raise DevelopmentError('invalid_test_receipt')
        sha256(result['output_sha256'])
        return dict(result)

    def admit_test(self, key, name):
        """Pin one exact attempt before preparations, without backend admission."""
        self._authorize('repo.run_test')
        suite = self._test_suite(name)
        request = {'kind': 'test', **suite}
        operation_id = self._operation_id(key)
        request_hash = digest(canonical(request))
        with self.journal.lock() as state:
            operation = state['operations'].get(operation_id)
            if operation is not None:
                if operation['request_hash'] != request_hash:
                    raise DevelopmentError('idempotency_conflict')
                if operation['phase'] == 'complete':
                    if operation['receipt']['result'] == 'not_started':
                        raise DevelopmentError(operation['receipt']['reason'])
                    return dict(operation['receipt'])
                if (operation['phase'] != 'intent'
                        or operation.get('admission', {}).get('dispatch') != 'pending'
                        or operation_id not in self._test_admissions):
                    raise DevelopmentError('reconciliation_required')
                return None
            self.journal.require_settled(state)
            self.journal.charge(state)
            admission = {'schema': 1, 'operation_id': operation_id,
                'manifest_hash': self.journal.manifest_hash, 'request_hash': request_hash,
                'backend_id': suite['backend_id'], 'root_identity': self.manifest['root_identity'],
                'dispatch': 'pending'}
            state['operations'][operation_id] = {'id': operation_id, 'kind': 'test',
                'phase': 'intent', 'request_hash': request_hash, 'detail': dict(suite),
                'receipt': None, 'admission': admission}
            self.journal.save(state)
            self._test_admissions.add(operation_id)
        return None

    def reject_test_before_dispatch(self, key, reason):
        """Record local non-dispatch proof; never infer it from an old intent."""
        if type(reason) is not str or re.fullmatch('[a-z][a-z0-9_]{0,99}', reason) is None:
            reason = 'test_admission_failed'
        with self.journal.lock() as state:
            operation = state['operations'].get(self._operation_id(key))
            if operation is None or operation['kind'] != 'test':
                return None
            if operation['phase'] == 'complete':
                return (dict(operation['receipt'])
                        if operation['receipt']['result'] == 'not_started' else None)
            admission = operation.get('admission')
            if (operation['phase'] != 'intent' or admission is None or admission['dispatch'] != 'pending'
                    or operation['id'] not in self._test_admissions):
                return None
            receipt = self._finish(state, operation, 'not_started', name=operation['detail']['name'],
                cleanup_confirmed=True, started=False, reason=reason,
                manifest_hash=self.journal.manifest_hash, request_hash=operation['request_hash'],
                admission_sha256=digest(canonical(admission)))
            self._test_admissions.discard(operation['id'])
            return receipt

    def run_test(self, key, name, *, _before_dispatch=None):
        if _before_dispatch is not None and not callable(_before_dispatch):
            raise DevelopmentError('invalid_test_admission')
        previous = self.admit_test(key, name)
        if previous is not None:
            return previous
        suite = self._test_suite(name)
        request = {'kind': 'test', **suite}
        with self.journal.lock() as state:
            operation = state['operations'][self._operation_id(key)]
            if operation['request_hash'] != digest(canonical(request)):
                raise DevelopmentError('idempotency_conflict')
            if operation['phase'] == 'complete':
                if operation['receipt']['result'] == 'not_started':
                    raise DevelopmentError(operation['receipt']['reason'])
                return dict(operation['receipt'])
            if (operation['phase'] != 'intent'
                    or operation.get('admission', {}).get('dispatch') != 'pending'
                    or operation['id'] not in self._test_admissions):
                raise DevelopmentError('reconciliation_required')
            files = None
            try:
                try:
                    self._qualified_test(name)
                    files = self._files()
                    self._authorize('repo.run_test')
                    files.verify()
                    test_request = self._test_request(operation)
                    if _before_dispatch is not None:
                        _before_dispatch()  # Preserve exact local deadline/stop failures.
                    self._authorize('repo.run_test')
                    files.verify()
                    self.journal.charge(state, operations=0, tests=1)
                    operation['admission']['dispatch'] = 'dispatched'
                    self.journal.save(state)  # Durable ambiguity boundary before backend.run.
                    self._test_admissions.discard(operation['id'])
                except Exception as error:
                    if operation['admission']['dispatch'] == 'pending':
                        reason = getattr(error, 'code', 'test_admission_failed')
                        if type(reason) is not str or re.fullmatch('[a-z][a-z0-9_]{0,99}', reason) is None:
                            reason = 'test_admission_failed'
                        self._finish(state, operation, 'not_started', name=name,
                            cleanup_confirmed=True, started=False, reason=reason,
                            manifest_hash=self.journal.manifest_hash, request_hash=operation['request_hash'],
                            admission_sha256=digest(canonical(operation['admission'])))
                        self._test_admissions.discard(operation['id'])
                    raise
                try:
                    raw = self.test_backend.run(test_request)
                except Exception:
                    raise DevelopmentError('test_outcome_uncertain') from None
                result = self._test_result(raw, suite['max_output_bytes'])
                outcome = result['status'] if result['cleanup_confirmed'] else 'needs_review'
                return self._finish(state, operation, outcome, name=name, **{
                    key: value for key, value in result.items() if key != 'status'})
            finally:
                if files is not None:
                    files.close()

    def reconcile(self, key):
        """Read exact recorded effects; never apply a patch or rerun a test."""
        operation_id = self._operation_id(key)
        with self.journal.lock() as state:
            operation = state['operations'].get(operation_id)
            if operation is None:
                raise DevelopmentError('operation_not_found')
            self._authorize('repo.patch_file' if operation['kind'] == 'patch' else 'repo.run_test')
            if operation['phase'] == 'complete':
                return dict(operation['receipt'])
            files = self._files()
            try:
                if operation['kind'] == 'test':
                    if operation.get('admission', {}).get('dispatch') == 'pending':
                        # A crash before a final refusal receipt is deliberately
                        # not promoted into cleanup proof during recovery.
                        raise DevelopmentError('test_admission_requires_review')
                    request = self._test_request(operation)
                    self._authorize('repo.run_test')
                    files.verify()
                    try:
                        raw = self.test_backend.lookup(request)
                    except Exception:
                        raise DevelopmentError('test_outcome_uncertain') from None
                    if raw is None:
                        return self._finish(state, operation, 'needs_review', name=request.name)
                    result = self._test_result(raw, request.max_output_bytes)
                    outcome = result['status'] if result['cleanup_confirmed'] else 'needs_review'
                    return self._finish(state, operation, outcome, name=request.name, **{
                        key: value for key, value in result.items() if key != 'status'})
                detail = operation['detail']
                path = self._path(detail['path'])
                with files.parent(path) as (parent, name):
                    current, current_signature = files.read_at(parent, name, self.manifest['budgets']['max_file_bytes'])
                    check_content(current)
                    observed = digest(current)
                    if observed == detail['after_sha256']:
                        outcome = 'verified_after_restart'
                    elif observed == detail['before_sha256'] and list(current_signature) == detail['before_signature']:
                        outcome = 'not_applied'
                    else:
                        return self._finish(state, operation, 'needs_review', path=path,
                            before_sha256=detail['before_sha256'], after_sha256=detail['after_sha256'],
                            observed_sha256=observed)
                    # A deterministic name and matching bytes do not prove
                    # stage ownership: O_EXCL may have refused a preexisting
                    # file. No stage inode was durably pinned, so leave every
                    # remaining stage for explicit operator inspection.
                    stage = self._stage_name(operation)
                    try:
                        os.stat(stage, dir_fd=parent, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise DevelopmentError('stage_cleanup_requires_review')
                    # A visible rename after restart is not proof that the
                    # original parent fsync succeeded. Re-establish durability
                    # before storing a successful reconciliation receipt.
                    self._authorize('repo.patch_file')
                    files.verify_parent(path, parent)
                    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        if signature(os.fstat(descriptor)) != current_signature:
                            raise DevelopmentError('file_changed')
                        os.fsync(descriptor)
                        if signature(os.stat(name, dir_fd=parent, follow_symlinks=False)) != current_signature:
                            raise DevelopmentError('file_changed')
                        os.fsync(parent)
                    finally:
                        os.close(descriptor)
                    return self._finish(state, operation, outcome, path=path,
                        before_sha256=detail['before_sha256'], after_sha256=detail['after_sha256'], bytes=len(current))
            except OSError:
                raise DevelopmentError('patch_reconciliation_uncertain') from None
            finally:
                files.close()

    def diff(self):
        """Hash-only comparison to the frozen manifest, without file content."""
        self._authorize('repo.read_file')
        with self.journal.lock() as state:
            maximum = self.manifest['budgets']['max_file_bytes']
            # Reserve the worst case before reading so a crash cannot reset
            # the total diff read allowance.
            self.journal.charge(state, read_bytes=maximum * len(self.manifest['files']))
            self.journal.save(state)
            files, changed = self._files(), []
            try:
                for path, before in self.manifest['files'].items():
                    self._authorize('repo.read_file')
                    data, _ = files.read(path, maximum)
                    check_content(data)
                    after = digest(data)
                    if after != before['sha256']:
                        changed.append({'path': path, 'before_sha256': before['sha256'],
                                        'after_sha256': after, 'before_bytes': before['bytes'], 'after_bytes': len(data)})
                return {'manifest_hash': self.journal.manifest_hash, 'base_sha': self.manifest['base_sha'],
                        'files': changed}
            finally:
                files.close()

    def status(self, key):
        self._authorize('repo.read_file')
        with self.journal.lock() as state:
            operation = state['operations'].get(self._operation_id(key))
            if operation is None:
                return {'found': False}
            return {'found': True, 'operation_id': operation['id'], 'kind': operation['kind'],
                    'status': operation['phase'], 'receipt': operation['receipt']}
