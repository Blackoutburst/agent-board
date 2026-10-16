"""Controller-created filesystem manifest for the disabled development broker.

No model, subprocess, Git command, authentication or network operation is used.
The controller supplies a reviewed base commit and scope; these are not inferred
from repository text. This module is a filesystem boundary, not an OS sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat


class DevelopmentError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


ACTIONS = frozenset({'repo.read_file', 'repo.patch_file', 'repo.run_test'})
SCOPE_FIELDS = frozenset({'owner_id', 'agent_id', 'device_id', 'session_id', 'project_id',
                         'ticket_id', 'profile_version'})
PRIVATE_PARTS = frozenset({'.git', '.codex', '.agents', '.ssh', '.aws', '.config', '.gnupg',
    '.azure', '.kube', '.npmrc', '.pypirc', '.netrc', 'auth.json', 'credentials.json',
    'credentials', 'secrets', 'secrets.json', 'id_rsa', 'id_ed25519', 'token.json', 'tokens.json'})
MAX_MANIFEST_BYTES = 128 * 1024
SECRET_PATTERNS = (
    re.compile(rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
    re.compile(rb'\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})\b'),
    re.compile(rb'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}'),
    re.compile(rb'(?i)["\'](?:access_token|refresh_token|api_key|client_secret|password)["\']\s*:\s*["\'][^"\'\r\n]{8,}["\']'),
)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise DevelopmentError('invalid_document') from None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def opaque(value, code='invalid_identifier', maximum=200):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,' + str(maximum - 1) + '}', value):
        raise DevelopmentError(code)
    return value


def sha256(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise DevelopmentError('invalid_sha256')
    return value


def relative_path(value):
    if type(value) is not str or not value or len(value.encode('utf-8', 'surrogatepass')) > 512:
        raise DevelopmentError('invalid_path')
    parts = value.split('/')
    if any(not part or part in ('.', '..') or '\\' in part or any(ord(c) < 32 or ord(c) == 127 for c in part)
           for part in parts):
        raise DevelopmentError('invalid_path')
    try:
        value.encode('utf-8', 'strict')
    except UnicodeError:
        raise DevelopmentError('invalid_path') from None
    for part in parts:
        lower = part.casefold()
        if (lower in PRIVATE_PARTS or lower == '.env' or lower.startswith(('.env.', '.bb-dev-'))
                or lower.endswith(('.pem', '.key', '.p12', '.pfx', '.keychain', '.keychain-db'))):
            raise DevelopmentError('private_path')
    return parts


def check_content(data):
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):
        raise DevelopmentError('secret_content_refused')
    try:
        text = data.decode('utf-8', 'strict')
    except UnicodeError:
        raise DevelopmentError('file_not_utf8') from None
    if '\x00' in text:
        raise DevelopmentError('file_not_text')
    return text


def absolute_directory(path):
    """Open every absolute path component without following a symlink."""
    value = os.fspath(path)
    if type(value) is not str or not value.startswith('/') or '\x00' in value:
        raise DevelopmentError('invalid_directory')
    parts = value.split('/')[1:]
    if any(part in ('.', '..') for part in parts):
        raise DevelopmentError('invalid_directory')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in filter(None, parts):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except OSError:
        os.close(descriptor)
        raise DevelopmentError('directory_access_refused') from None


def identity(info):
    return (info.st_dev, info.st_ino)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_nlink, info.st_uid, info.st_mode)


class RepositoryFiles:
    def __init__(self, root, expected_identity=None):
        self.root = os.fspath(root)
        self.fd = absolute_directory(root)
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            self.close()
            raise DevelopmentError('workspace_not_exclusive')
        self.identity = identity(info)
        if expected_identity is not None and tuple(expected_identity) != self.identity:
            self.close()
            raise DevelopmentError('workspace_identity_changed')

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def verify(self):
        current = absolute_directory(self.root)
        try:
            info = os.fstat(current)
            if identity(info) != self.identity or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise DevelopmentError('workspace_identity_changed')
        finally:
            os.close(current)

    @contextmanager
    def parent(self, path):
        parts = relative_path(path)
        self.verify()
        descriptor = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                if info.st_uid != os.geteuid() or info.st_mode & 0o022:
                    raise DevelopmentError('directory_not_exclusive')
            yield descriptor, parts[-1]
        except OSError:
            raise DevelopmentError('file_access_refused') from None
        finally:
            os.close(descriptor)

    def verify_parent(self, path, descriptor):
        with self.parent(path) as (fresh, _):
            if identity(os.fstat(fresh)) != identity(os.fstat(descriptor)):
                raise DevelopmentError('parent_changed')

    @staticmethod
    def read_at(parent, name, maximum):
        descriptor = None
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            before = os.fstat(descriptor)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_uid != os.geteuid()
                    or before.st_mode & 0o022):
                raise DevelopmentError('file_type_not_allowed')
            if before.st_size > maximum:
                raise DevelopmentError('file_too_large')
            chunks, total = [], 0
            while True:
                chunk = os.read(descriptor, min(65536, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise DevelopmentError('file_too_large')
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if signature(before) != signature(os.fstat(descriptor)) or signature(before) != signature(current):
                raise DevelopmentError('file_changed')
            return b''.join(chunks), signature(before)
        except OSError:
            raise DevelopmentError('file_access_refused') from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def read(self, path, maximum):
        with self.parent(path) as (parent, name):
            result = self.read_at(parent, name, maximum)
            self.verify_parent(path, parent)
            return result


@dataclass(frozen=True)
class NamedTest:
    name: str
    backend_id: str = 'unqualified'
    timeout_seconds: int = 60
    max_output_bytes: int = 32768

    def document(self):
        opaque(self.name, 'invalid_test_name', 64)
        opaque(self.backend_id, 'invalid_backend_id', 64)
        if (type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 600
                or type(self.max_output_bytes) is not int or not 0 <= self.max_output_bytes <= 1048576):
            raise DevelopmentError('invalid_test_budget')
        return vars(self).copy()


@dataclass(frozen=True)
class Budgets:
    max_operations: int = 128
    max_file_bytes: int = 65536
    max_read_bytes: int = 4 * 1024 * 1024
    max_patch_bytes: int = 1024 * 1024
    max_tests: int = 8

    def document(self):
        bounds = {'max_operations': (1, 256), 'max_file_bytes': (1, 1048576),
                  'max_read_bytes': (1, 32 * 1048576), 'max_patch_bytes': (0, 16 * 1048576),
                  'max_tests': (0, 32)}
        for key, (minimum, maximum) in bounds.items():
            value = getattr(self, key)
            if type(value) is not int or not minimum <= value <= maximum:
                raise DevelopmentError('invalid_budget')
        return vars(self).copy()


def validate_manifest(value):
    if type(value) is not dict or set(value) != {
            'schema', 'scope', 'root', 'root_identity', 'base_sha', 'resource', 'actions', 'files', 'tests', 'budgets'}:
        raise DevelopmentError('invalid_manifest')
    scope = value['scope']
    if type(scope) is not dict or set(scope) != SCOPE_FIELDS:
        raise DevelopmentError('invalid_scope')
    for key, item in scope.items():
        if key == 'profile_version':
            if type(item) is not int or item < 1:
                raise DevelopmentError('invalid_scope')
        else:
            opaque(item, 'invalid_scope')
    if (type(value['schema']) is not int or value['schema'] != 1 or type(value['root']) is not str
            or not value['root'].startswith('/') or '\x00' in value['root'] or '..' in value['root'].split('/')
            or type(value['root_identity']) is not list or len(value['root_identity']) != 2
            or any(type(item) is not int or item < 0 for item in value['root_identity'])
            or type(value['base_sha']) is not str or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', value['base_sha'])):
        raise DevelopmentError('invalid_manifest')
    opaque(value['resource'], 'invalid_resource')
    if (type(value['actions']) is not list or not value['actions']
            or any(type(action) is not str or action not in ACTIONS for action in value['actions'])
            or len(set(value['actions'])) != len(value['actions'])):
        raise DevelopmentError('invalid_actions')
    if type(value['files']) is not dict or not 1 <= len(value['files']) <= 128:
        raise DevelopmentError('invalid_files')
    for path, entry in value['files'].items():
        relative_path(path)
        if type(entry) is not dict or set(entry) != {'sha256', 'bytes'} or type(entry['bytes']) is not int or entry['bytes'] < 0:
            raise DevelopmentError('invalid_files')
        sha256(entry['sha256'])
    if type(value['tests']) is not list or len(value['tests']) > 32:
        raise DevelopmentError('invalid_tests')
    try:
        tests = [NamedTest(**item).document() for item in value['tests'] if type(item) is dict]
        budgets = Budgets(**value['budgets']).document()
    except (TypeError, KeyError):
        raise DevelopmentError('invalid_manifest') from None
    if (len(tests) != len(value['tests']) or len({test['name'] for test in tests}) != len(tests)
            or any(entry['bytes'] > budgets['max_file_bytes'] for entry in value['files'].values())):
        raise DevelopmentError('invalid_manifest')
    if len(canonical(value)) > MAX_MANIFEST_BYTES:
        raise DevelopmentError('manifest_too_large')
    return value


def authorize(callback, manifest, action):
    if action not in manifest['actions']:
        raise DevelopmentError('action_not_allowed')
    try:
        allowed = callback(dict(manifest['scope']), action, manifest['resource']) is True
    except Exception:
        allowed = False
    if not allowed:
        raise DevelopmentError('permission_revoked')


def prepare_manifest(directory, *, root, base_sha, scope, paths, resource, authorize_scope,
                     tests=(), budgets=None, actions=tuple(sorted(ACTIONS))):
    """Trusted controller preparation, never a model-dispatched operation."""
    from .dev_journal import PrivateDirectory
    if budgets is not None and type(budgets) is not Budgets:
        raise DevelopmentError('invalid_budget')
    budget_values = (Budgets() if budgets is None else budgets).document()
    if type(scope) is not dict or type(tests) not in (list, tuple) or any(type(test) is not NamedTest for test in tests):
        raise DevelopmentError('invalid_manifest')
    if type(paths) not in (tuple, list) or not 1 <= len(paths) <= 128:
        raise DevelopmentError('invalid_files')
    for path in paths:
        relative_path(path)
    if len(set(paths)) != len(paths):
        raise DevelopmentError('invalid_files')
    repository = RepositoryFiles(root)
    try:
        manifest = dict(schema=1, scope=dict(scope), root=os.fspath(root), root_identity=list(repository.identity),
            base_sha=base_sha, resource=resource, actions=list(actions), files={},
            tests=[test.document() for test in tests], budgets=budget_values)
        # Validate scalar inputs before reading any repository content.
        validate_manifest({**manifest, 'files': {paths[0]: {'sha256': '0' * 64, 'bytes': 0}}})
        total = 0
        for path in paths:
            authorize(authorize_scope, manifest, 'repo.read_file')
            data, _ = repository.read(path, budget_values['max_file_bytes'])
            check_content(data)
            total += len(data)
            if total > budget_values['max_read_bytes']:
                raise DevelopmentError('read_bytes_budget_exhausted')
            manifest['files'][path] = {'sha256': digest(data), 'bytes': len(data)}
        validate_manifest(manifest)
        if Path(directory).absolute().is_relative_to(Path(root).absolute()):
            raise DevelopmentError('journal_inside_workspace')
        with PrivateDirectory(directory, create=True) as private:
            private.create('manifest.json', canonical(manifest))
            private.create('state.json', canonical({'schema': 1, 'manifest_hash': digest(canonical(manifest)),
                'used': {'operations': 0, 'read_bytes': 0, 'patch_bytes': 0, 'tests': 0}, 'operations': {}}))
        return digest(canonical(manifest))
    finally:
        repository.close()
