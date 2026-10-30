"""Explicit local Git copies for a future development worker, currently unwired.

The trusted controller supplies a repository grant; board/model text cannot
create one. This module authenticates neither a Board claim nor that grant. It
has no network transport, credentials, model, tests, commit or publication API.
An interrupted copy is retained for inspection, never adopted or retried.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import selectors
import signal
import stat
import subprocess
import time
import unicodedata
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, SCOPE_FIELDS, absolute_directory,
                           canonical, digest, identity, opaque, relative_path)


SCHEMA = 'local-git-workspace-v1'
MAX_RECORD = 32768
MAX_OUTPUT = 2 * 1024 * 1024
MAX_REPOSITORY_BYTES = 512 * 1024 * 1024
MAX_REPOSITORY_FILES = 25000
MAX_CHECKOUT_BYTES = 128 * 1024 * 1024
MAX_CHECKOUT_FILE_BYTES = 16 * 1024 * 1024
MAX_CHECKOUT_FILES = 10000


def _fail(code='git_workspace_invalid'):
    raise DevelopmentError(code)


def _path(value):
    value = os.fspath(value)
    if (type(value) is not str or not value.startswith('/') or any(ord(c) < 32 for c in value)
            or str(Path(value)) != value or any(p in ('.', '..') for p in value.split('/'))):
        _fail('git_workspace_path_refused')
    return Path(value)


def _directory(path, *, private=False):
    try:
        descriptor = absolute_directory(str(path))
    except (OSError, ValueError):
        _fail('git_workspace_directory_refused')
    try:
        info = os.fstat(descriptor)
        if (info.st_uid != os.geteuid() or info.st_mode & 0o022
                or private and stat.S_IMODE(info.st_mode) != 0o700):
            _fail('git_workspace_directory_refused')
        return list(identity(info))
    finally:
        os.close(descriptor)


def _ancestry(path):
    """Physical ancestors also detect case/normalization aliases of roots."""
    descriptor = None
    try:
        descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        result = [identity(os.fstat(descriptor))]
        for name in path.parts[1:]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            result.append(identity(os.fstat(descriptor)))
        return result
    except (OSError, ValueError):
        _fail('git_workspace_directory_refused')
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _file(path, maximum, *, executable=False):
    parent = descriptor = None
    try:
        parent = absolute_directory(str(path.parent))
        parent_pin = identity(os.fstat(parent))
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or (before.st_nlink != 1 and not executable)
                or executable and before.st_uid not in (0, os.geteuid())
                or before.st_size > maximum or before.st_mode & 0o022):
            _fail('git_workspace_file_refused')
        parts, total = [], 0
        while total <= maximum:
            part = os.read(descriptor, min(65536, maximum + 1 - total))
            if not part:
                break
            parts.append(part)
            total += len(part)
        data = b''.join(parts)
        after = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        pin = lambda i: (i.st_dev, i.st_ino, i.st_mode, i.st_uid, i.st_size, i.st_mtime_ns, i.st_ctime_ns)
        if len(data) > maximum or pin(before) != pin(after) or pin(after) != pin(named):
            _fail('git_workspace_file_changed')
        current_parent = absolute_directory(str(path.parent))
        try:
            if identity(os.fstat(current_parent)) != parent_pin:
                _fail('git_workspace_file_changed')
        finally:
            os.close(current_parent)
        return data
    except (OSError, ValueError):
        _fail('git_workspace_file_refused')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


def _commit(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', value) is None:
        _fail('git_workspace_base_refused')
    return value


def _operation(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        _fail('git_workspace_operation_refused')
    return value


def _scope(value):
    if type(value) is not dict or set(value) != SCOPE_FIELDS:
        _fail('git_workspace_scope_refused')
    for key, item in value.items():
        if key == 'profile_version':
            if type(item) is not int or item < 1:
                _fail('git_workspace_scope_refused')
        else:
            opaque(item, 'git_workspace_scope_refused')
    return decode(canonical(value))


def _unsupported_metadata(relative):
    """One full-copy layout contract, for both source and ready copies.

    These markers redirect storage or suppress object/history verification.
    Replacement objects are also disabled in the fixed Git environment; their
    metadata is refused rather than becoming an implicit alternate history.
    """
    relative = unicodedata.normalize('NFC', relative).casefold()
    return (relative in {'commondir', 'shallow', 'info/grafts',
                         'objects/info/alternates', 'objects/info/http-alternates', 'refs/replace'}
            or relative.startswith('refs/replace/')
            or relative.startswith('objects/pack/') and relative.endswith('.promisor'))


@dataclass(frozen=True)
class LocalRepositoryGrant:
    """Exact owner-supplied local resource declaration, not Board authority."""

    owner_id: str
    repository_id: str
    repository: str
    workspace_root: str
    base_commit: str

    def document(self):
        opaque(self.owner_id, 'git_workspace_grant_refused')
        opaque(self.repository_id, 'git_workspace_grant_refused')
        _path(self.repository)
        _path(self.workspace_root)
        _commit(self.base_commit)
        return vars(self).copy()


class LocalGitWorkspaceController:
    """Independent full local copy per operation; never a shared Git worktree.

    Both roots must already exist with mode 0700. The repository must be an
    ordinary owned checkout with an actual .git directory: linked worktrees,
    bare/shallow repos, alternates, symlinks and gitlinks are refused. Copies
    share no object hardlinks. Only exact pinned local source bytes are used.
    """

    def __init__(self, state_root, *, grant, git_executable='/usr/bin/git', timeout_seconds=30):
        if type(grant) is not LocalRepositoryGrant:
            _fail('git_workspace_grant_refused')
        self.grant = grant.document()
        self.state_root = _path(state_root)
        self.repository = _path(grant.repository)
        self.workspace_root = _path(grant.workspace_root)
        self.git = _path(git_executable)
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
            _fail('git_workspace_timeout_refused')
        self.timeout = timeout_seconds
        roots = (self.state_root, self.repository, self.workspace_root)
        if any(a.is_relative_to(b) or b.is_relative_to(a)
               for n, a in enumerate(roots) for b in roots[n + 1:]):
            _fail('git_workspace_roots_overlap')
        self.pins = {'state_root': _directory(self.state_root, private=True),
                     'repository': _directory(self.repository),
                     'repository_git': _directory(self.repository / '.git'),
                     'workspace_root': _directory(self.workspace_root, private=True),
                     'git_sha256': digest(_file(self.git, 64 * 1024 * 1024, executable=True))}
        if not os.access(self.git, os.X_OK):
            _fail('git_workspace_git_refused')
        self._source()

    def _roots(self):
        if (self.pins['state_root'] != _directory(self.state_root, private=True)
                or self.pins['workspace_root'] != _directory(self.workspace_root, private=True)
                or self.pins['repository'] != _directory(self.repository)
                or self.pins['repository_git'] != _directory(self.repository / '.git')
                or self.pins['git_sha256'] != digest(_file(self.git, 64 * 1024 * 1024, executable=True))):
            _fail('git_workspace_pin_changed')
        chains = [_ancestry(root) for root in (self.state_root, self.repository, self.workspace_root)]
        if any(a[-1] in b or b[-1] in a for n, a in enumerate(chains) for b in chains[n + 1:]):
            _fail('git_workspace_roots_overlap')

    def _source(self):
        self._roots()
        git_directory = self.repository / '.git'
        pin = _directory(git_directory)
        # --local clone copies object files, never invokes upload-pack or a
        # remote helper. Bound and inspect that complete input before Git runs.
        total = count = 0
        for root, directories, files in os.walk(git_directory, followlinks=False):
            for name in directories + files:
                path = Path(root) / name
                item = path.lstat()
                if (_unsupported_metadata(path.relative_to(git_directory).as_posix())
                        or item.st_uid != os.geteuid() or item.st_mode & 0o022
                        or not (stat.S_ISREG(item.st_mode) or stat.S_ISDIR(item.st_mode))):
                    _fail('git_workspace_source_layout_refused')
                count += 1
                total += item.st_size if stat.S_ISREG(item.st_mode) else 0
                if count > MAX_REPOSITORY_FILES or total > MAX_REPOSITORY_BYTES:
                    _fail('git_workspace_source_too_large')
        keys = self._git(['config', '--file', str(git_directory / 'config'), '--no-includes',
                         '--null', '--name-only', '--list'], cwd=self.state_root).split(b'\x00')
        for key in keys:
            key = key.lower()
            if (key.startswith((b'include.', b'includeif.')) or key in
                    (b'core.worktree', b'extensions.partialclone')
                    or key.endswith((b'.promisor', b'.partialclonefilter'))):
                _fail('git_workspace_source_layout_refused')
        return pin

    def _git(self, arguments, *, cwd=None):
        """Built-ins only, fixed argv, no inherited Git config/environment."""
        self._roots()
        options = {'core.hooksPath': '/dev/null', 'core.fsmonitor': 'false',
                   'core.attributesFile': '/dev/null', 'protocol.allow': 'never',
                   'protocol.file.allow': 'always', 'submodule.recurse': 'false',
                   'uploadpack.packObjectsHook': ''}
        environment = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C',
                       'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                       'GIT_CONFIG_SYSTEM': '/dev/null', 'GIT_TERMINAL_PROMPT': '0',
                       'GIT_ALLOW_PROTOCOL': 'file', 'GIT_PROTOCOL_FROM_USER': '0',
                       'GIT_NO_LAZY_FETCH': '1', 'GIT_NO_REPLACE_OBJECTS': '1',
                       'GIT_OPTIONAL_LOCKS': '0', 'GIT_CONFIG_COUNT': str(len(options))}
        for n, (key, value) in enumerate(options.items()):
            environment['GIT_CONFIG_KEY_' + str(n)] = key
            environment['GIT_CONFIG_VALUE_' + str(n)] = value
        process = subprocess.Popen([str(self.git), *arguments], cwd=cwd, env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, umask=0o077)
        output = bytearray()
        total = 0
        deadline = time.monotonic() + self.timeout
        try:
            with selectors.DefaultSelector() as poll:
                poll.register(process.stdout, selectors.EVENT_READ, True)
                poll.register(process.stderr, selectors.EVENT_READ, False)
                while poll.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        _fail('git_workspace_command_timeout')
                    for key, _ in poll.select(min(remaining, 0.1)):
                        data = os.read(key.fileobj.fileno(), min(65536, MAX_OUTPUT + 1 - total))
                        if not data:
                            poll.unregister(key.fileobj)
                        else:
                            total += len(data)
                            if key.data:
                                output.extend(data)
                            if total > MAX_OUTPUT:
                                _fail('git_workspace_output_limit')
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                _fail('git_workspace_command_failed')
            return bytes(output)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            raise
        finally:
            process.stdout.close()
            process.stderr.close()

    def _base(self):
        self._source()
        base = self.grant['base_commit']
        if self._git(['cat-file', '-t', base], cwd=self.repository) != b'commit\n':
            _fail('git_workspace_base_refused')
        tree = self._git(['ls-tree', '-r', '-l', '-z', '--full-tree', base], cwd=self.repository)
        if not tree or not tree.endswith(b'\x00'):
            _fail('git_workspace_tree_refused')
        entries = tree[:-1].split(b'\x00')
        if len(entries) > MAX_CHECKOUT_FILES:
            _fail('git_workspace_tree_too_large')
        total = 0
        for item in entries:
            try:
                metadata, raw_path = item.split(b'\t', 1)
                mode, kind, hashed, size = metadata.split()
                relative_path(raw_path.decode('utf-8', 'strict'))
                _commit(hashed.decode('ascii'))
            except (ValueError, UnicodeError):
                _fail('git_workspace_tree_refused')
            if mode not in (b'100644', b'100755') or kind != b'blob':
                _fail('git_workspace_tree_refused')
            if not size.isdigit() or int(size) > MAX_CHECKOUT_FILE_BYTES:
                _fail('git_workspace_tree_too_large')
            total += int(size)
            if total > MAX_CHECKOUT_BYTES:
                _fail('git_workspace_tree_too_large')

    @contextmanager
    def _locked(self, operation_id, *, create=False):
        self._roots()
        path = self.state_root / _operation(operation_id)
        fresh = False
        if create:
            try:
                os.mkdir(path, 0o700)
                with PrivateDirectory(self.state_root) as parent:
                    os.fsync(parent.fd)
                fresh = True
            except FileExistsError:
                pass
        with PrivateDirectory(path) as private:
            descriptor = os.open('workspace.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                 0o600, dir_fd=private.fd)
            try:
                private.validate_file(descriptor)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    _fail('git_workspace_busy')
                private.verify()
                yield private, fresh
            finally:
                os.close(descriptor)

    def _read(self, private):
        value = decode(private.read('workspace.json', MAX_RECORD))
        if (type(value) is not dict or set(value) != {'schema', 'phase', 'request', 'request_sha256',
                'pins', 'checkout_identity', 'git_config_sha256', 'reason'}
                or value['schema'] != SCHEMA or value['phase'] not in ('planned', 'preparing', 'ready', 'needs_review')
                or type(value['request']) is not dict or value['request_sha256'] != digest(canonical(value['request']))
                or value['pins'] != self.pins or value['request'].get('grant') != self.grant
                or value['request'].get('operation_id') != private.path.name):
            _fail('git_workspace_record_refused')
        expected = self._request(private.path.name, _scope(value['request'].get('scope')))
        if value['request'] != expected:
            _fail('git_workspace_record_refused')
        return value

    def _request(self, operation_id, scope):
        if scope['owner_id'] != self.grant['owner_id']:
            _fail('git_workspace_owner_refused')
        branch = 'codex/cloud-' + digest(scope['ticket_id'].encode())[:12] + '-' + uuid.UUID(operation_id).hex
        return {'operation_id': operation_id, 'scope': scope, 'grant': self.grant,
                'branch': branch, 'checkout': str(self.workspace_root / operation_id / 'checkout')}

    @staticmethod
    def _public(value):
        request = value['request']
        return {'schema': SCHEMA, 'operation_id': request['operation_id'],
                'status': 'ready' if value['phase'] == 'ready' else 'planned' if value['phase'] == 'planned' else 'needs_review',
                'phase': value['phase'], 'reason': value['reason'], 'branch': request['branch'],
                'checkout': request['checkout'], 'base_commit': request['grant']['base_commit'],
                'request_sha256': value['request_sha256'], 'scope': decode(canonical(request['scope']))}

    def plan(self, *, operation_id, scope):
        """Pin an exact request. Does not copy a checkout or change a branch."""
        operation_id, scope = _operation(operation_id), _scope(scope)
        request = self._request(operation_id, scope)
        with self._locked(operation_id, create=True) as (private, fresh):
            if not fresh:
                value = self._read(private)
                if value['request'] != request:
                    _fail('git_workspace_request_conflict')
                return self._public(value)
            self._base()
            value = {'schema': SCHEMA, 'phase': 'planned', 'request': request,
                     'request_sha256': digest(canonical(request)), 'pins': self.pins,
                     'checkout_identity': None, 'git_config_sha256': None, 'reason': ''}
            private.create('workspace.json', canonical(value))
            return self._public(value)

    def _clone(self, private, value):
        """Only the first holder of the durable preparing intent calls this."""
        path = self.workspace_root / value['request']['operation_id']
        os.mkdir(path, 0o700)
        with PrivateDirectory(self.workspace_root) as root:
            os.fsync(root.fd)
        templates = path / 'empty-templates'
        os.mkdir(templates, 0o700)
        checkout = path / 'checkout'
        self._source()
        self._git(['clone', '--local', '--no-hardlinks', '--no-checkout', '--no-tags',
                   '--template=' + str(templates), '--', str(self.repository), str(checkout)])
        self._git(['fsck', '--strict', '--no-reflogs', '--no-dangling'], cwd=checkout)
        self._git(['checkout', '--no-track', '-b', value['request']['branch'],
                   self.grant['base_commit'], '--'], cwd=checkout)
        self._fsync_tree(path)
        value['checkout_identity'] = _directory(checkout, private=True)
        value['git_config_sha256'] = digest(_file(checkout / '.git/config', 32768))

    @staticmethod
    def _fsync_tree(path):
        """Make the copied bytes and directory entries durable before ready."""
        for root, directories, files in os.walk(path, topdown=False, followlinks=False):
            for name in files:
                candidate = Path(root) / name
                descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(descriptor)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.geteuid() or info.st_mode & 0o022):
                        _fail('git_workspace_copy_layout_refused')
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            for name in directories:
                _directory(Path(root) / name, private=True)
            with PrivateDirectory(root) as directory:
                os.fsync(directory.fd)

    @staticmethod
    def _copy_metadata(checkout):
        """Read the complete physical Git tree, never following external links.

        The operation lock excludes cooperating controller writers. Held parent
        descriptors and before/after observations refuse substitution during
        this read; arbitrary same-user code is not an OS security boundary.
        """
        root = checkout / '.git'
        snapshot = []
        total = 0

        def pin(info):
            return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
                    info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

        def walk(descriptor, prefix, depth):
            nonlocal total
            if depth > 32:
                _fail('git_workspace_checkout_changed')
            with os.scandir(descriptor) as entries:
                names = []
                for entry in entries:
                    if len(snapshot) + len(names) >= MAX_REPOSITORY_FILES:
                        _fail('git_workspace_checkout_changed')
                    names.append(entry.name)
                names.sort()
            for name in names:
                before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                relative = prefix + name
                if (_unsupported_metadata(relative) or before.st_uid != os.geteuid() or before.st_mode & 0o022
                        or not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode))
                        or stat.S_ISREG(before.st_mode) and before.st_nlink != 1):
                    _fail('git_workspace_checkout_changed')
                snapshot.append((relative, pin(before)))
                if len(snapshot) > MAX_REPOSITORY_FILES:
                    _fail('git_workspace_checkout_changed')
                if stat.S_ISDIR(before.st_mode):
                    if stat.S_IMODE(before.st_mode) != 0o700:
                        _fail('git_workspace_checkout_changed')
                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    try:
                        if pin(os.fstat(child)) != pin(before):
                            _fail('git_workspace_checkout_changed')
                        walk(child, relative + '/', depth + 1)
                        if pin(os.fstat(child)) != pin(before):
                            _fail('git_workspace_checkout_changed')
                    finally:
                        os.close(child)
                else:
                    total += before.st_size
                    if total > MAX_REPOSITORY_BYTES:
                        _fail('git_workspace_checkout_changed')
                if pin(os.stat(name, dir_fd=descriptor, follow_symlinks=False)) != pin(before):
                    _fail('git_workspace_checkout_changed')

        descriptor = None
        try:
            descriptor = absolute_directory(str(root))
            original = pin(os.fstat(descriptor))
            if (original[2] != os.geteuid() or stat.S_IMODE(original[4]) != 0o700):
                _fail('git_workspace_checkout_changed')
            walk(descriptor, '', 0)
            current = absolute_directory(str(root))
            try:
                if pin(os.fstat(current)) != original or pin(os.fstat(descriptor)) != original:
                    _fail('git_workspace_checkout_changed')
            finally:
                os.close(current)
            return (original, snapshot)
        except (OSError, ValueError):
            _fail('git_workspace_checkout_changed')
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _ready(self, value):
        self._roots()
        checkout = _path(value['request']['checkout'])
        _directory(checkout.parent, private=True)
        if (value['checkout_identity'] != _directory(checkout, private=True)
                or _directory(checkout / '.git', private=True) == _directory(self.repository / '.git')):
            _fail('git_workspace_checkout_changed')
        metadata = self._copy_metadata(checkout)
        if value['git_config_sha256'] != digest(_file(checkout / '.git/config', 32768)):
            _fail('git_workspace_checkout_changed')

        def read_git(arguments):
            if self._copy_metadata(checkout) != metadata:
                _fail('git_workspace_checkout_changed')
            result = self._git(arguments, cwd=checkout)
            if self._copy_metadata(checkout) != metadata:
                _fail('git_workspace_checkout_changed')
            return result

        read_git(['fsck', '--strict', '--no-reflogs', '--no-dangling'])
        if (read_git(['symbolic-ref', '--quiet', 'HEAD']).strip()
                != ('refs/heads/' + value['request']['branch']).encode()
                or read_git(['rev-parse', '--verify', 'HEAD']).strip()
                != self.grant['base_commit'].encode()
                or read_git(['config', '--local', '--no-includes', '--get', 'remote.origin.url']).strip()
                != str(self.repository).encode()):
            _fail('git_workspace_checkout_changed')
        if self._copy_metadata(checkout) != metadata:
            _fail('git_workspace_checkout_changed')

    def materialize(self, operation_id):
        """Copy once. Ambiguous copies are never removed, adopted or replayed."""
        with self._locked(operation_id) as (private, _):
            value = self._read(private)
            if value['phase'] == 'ready':
                self._ready(value)
                return self._public(value)
            if value['phase'] != 'planned':
                _fail('git_workspace_interrupted_requires_review')
            self._base()
            value['phase'] = 'preparing'
            private.replace('workspace.json', canonical(value))
            try:
                self._clone(private, value)
                self._ready(value)
                value['phase'] = 'ready'
                private.replace('workspace.json', canonical(value))
            except Exception as error:
                value['phase'] = 'needs_review'
                value['reason'] = error.code if isinstance(error, DevelopmentError) else 'git_workspace_preparation_failed'
                private.replace('workspace.json', canonical(value))
                _fail(value['reason'])
            return self._public(value)

    def status(self, operation_id):
        """Historical local metadata only; manifest_inputs revalidates ready."""
        with self._locked(operation_id) as (private, _):
            return self._public(self._read(private))

    def manifest_inputs(self, operation_id):
        """Return inputs for prepare_manifest; that API must recheck live grants.

        No manifest, claim, worker or Runtime is created here. A changed HEAD is
        refused, but working-file edits are preserved for the future worker.
        """
        with self._locked(operation_id) as (private, _):
            value = self._read(private)
            if value['phase'] != 'ready':
                _fail('git_workspace_not_ready')
            self._ready(value)
            return {'root': value['request']['checkout'], 'base_sha': self.grant['base_commit'],
                    'scope': decode(canonical(value['request']['scope']))}
