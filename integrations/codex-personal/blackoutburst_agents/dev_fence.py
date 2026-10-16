"""One local, non-configurable anchor for development operations.

This is a same-user, same-machine durability fence, not protection against an
administrator deleting its state or a coordination service for multiple hosts.
The underscored anchor override exists solely for isolated fixture tests.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import pwd
import stat
import threading
import urllib.parse

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, identity


def normalized_origin(endpoint):
    try:
        if type(endpoint) is not str:
            raise ValueError
        value = urllib.parse.urlsplit(endpoint)
        if ( not value.hostname or value.username is not None
                or value.password is not None or value.query or value.fragment
                or (value.scheme != 'https' and not (value.scheme == 'http' and
                    value.hostname in ('127.0.0.1', '::1', 'localhost')))):
            raise ValueError
        port = value.port
        host = value.hostname.encode('idna').decode('ascii').lower()
        if ':' in host:
            host = '[' + host + ']'
        if port is not None and port != (443 if value.scheme == 'https' else 80):
            host += ':' + str(port)
        return value.scheme + '://' + host
    except (ValueError, AttributeError, UnicodeError, TypeError):
        raise DevelopmentError('invalid_board_origin') from None


def canonical_anchor():
    # HOME and CODEX_HOME deliberately do not select the fence.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/blackoutburst-agents/development'


def _create_chain(path):
    """Create components relative to open nofollow directories, never a symlink."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise DevelopmentError('invalid_fence_directory')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if (info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and
                    not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                raise DevelopmentError('insecure_fence_ancestor')
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise DevelopmentError('insecure_fence_directory')
    except OSError:
        raise DevelopmentError('fence_directory_unavailable') from None
    finally:
        os.close(fd)
    return path


class DevelopmentFence:
    def __init__(self, *, _test_anchor=None):
        self.private = PrivateDirectory(_create_chain(canonical_anchor() if _test_anchor is None else _test_anchor))
        self._thread_lock = threading.Lock()
        self.fd = None
        try:
            self.fd = os.open('fence.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=self.private.fd)
            self.private.validate_file(self.fd)
            with self.lock():
                try:
                    self.private.read('fence.json', 2 * 1024 * 1024)
                except DevelopmentError as error:
                    if error.code != 'private_read_failed':
                        raise
                    try:
                        os.stat('fence.json', dir_fd=self.private.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        self.private.create('fence.json', canonical({'schema': 1, 'records': {}}))
                    else:
                        raise error
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.private.close()

    @contextmanager
    def lock(self):
        if not self._thread_lock.acquire(blocking=False):
            raise DevelopmentError('fence_busy')
        locked = False
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                self.private.verify()
                current = os.stat('fence.lock', dir_fd=self.private.fd, follow_symlinks=False)
                if identity(current) != identity(os.fstat(self.fd)):
                    raise DevelopmentError('fence_lock_changed')
                self.private.validate_file(self.fd)
            except (OSError, TypeError):
                raise DevelopmentError('fence_busy') from None
            yield
        finally:
            if locked:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            self._thread_lock.release()

    @staticmethod
    def key(scope, root_identity):
        return digest(canonical({'origin': normalized_origin(scope['board_endpoint']),
            **{key: scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')},
            'root_identity': list(root_identity)}))

    def _read(self):
        state = decode(self.private.read('fence.json', 2 * 1024 * 1024))
        if (type(state) is not dict or set(state) != {'schema', 'records'} or state['schema'] != 1
                or type(state['schema']) is not int or type(state['records']) is not dict):
            raise DevelopmentError('fence_corrupt')
        try:
            import uuid
            from .dev_manifest import sha256
            from .work_journal import _scope
            for key, value in state['records'].items():
                binding = value['binding']
                _scope(binding['scope'])
                sha256(binding['configuration_hash'])
                if (type(value) is not dict or set(value) != {'binding', 'operation_id'}
                        or type(binding) is not dict or set(binding) != {'scope', 'root_identity',
                            'journal', 'configuration_hash', 'child_scope'}
                        or type(binding['root_identity']) is not list or len(binding['root_identity']) != 2
                        or any(type(item) is not int or item < 0 for item in binding['root_identity'])
                        or str(uuid.UUID(value['operation_id'])) != value['operation_id']
                        or key != self.key(binding['scope'], binding['root_identity'])):
                    raise ValueError
                children = binding['child_scope']
                if (type(children) is not dict or set(children) != {'work_id', 'runs', 'runtime'}
                        or str(uuid.UUID(children['work_id'])) != children['work_id']):
                    raise ValueError
                for pin in (binding['journal'], children['runs'], children['runtime']):
                    if (type(pin) is not dict or set(pin) != {'path', 'identity'}
                            or type(pin['path']) is not str or not pin['path'].startswith('/')
                            or type(pin['identity']) is not list or len(pin['identity']) != 2
                            or any(type(v) is not int or v < 0 for v in pin['identity'])):
                        raise ValueError
        except Exception:
            raise DevelopmentError('fence_corrupt') from None
        return state

    def bind(self, binding, operation_id):
        """Reserve before preparing children; uncertainty never frees the row."""
        with self.lock():
            state = self._read()
            key = self.key(binding['scope'], binding['root_identity'])
            existing = state['records'].get(key)
            if existing is not None:
                if existing['binding'] != binding:
                    raise DevelopmentError('canonical_operation_conflict')
                return existing['operation_id'], False
            # Exclusive checkout remains fenced even under another ticket or
            # MCP-origin spelling. There is no implicit expiry/reset.
            if any(value['binding']['root_identity'] == binding['root_identity']
                   for value in state['records'].values()):
                raise DevelopmentError('canonical_checkout_busy')
            if len(state['records']) >= 4096:
                raise DevelopmentError('fence_full')
            state['records'][key] = {'binding': binding, 'operation_id': operation_id}
            encoded = canonical(state)
            if len(encoded) > 2 * 1024 * 1024:
                raise DevelopmentError('fence_full')
            self.private.replace('fence.json', encoded)
            return operation_id, True

    def verify(self, binding, operation_id):
        with self.lock():
            found = self._read()['records'].get(self.key(binding['scope'], binding['root_identity']))
            if found != {'binding': binding, 'operation_id': operation_id}:
                raise DevelopmentError('canonical_fence_changed')
