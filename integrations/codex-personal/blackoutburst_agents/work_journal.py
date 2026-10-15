"""Private durable records for an explicitly requested board-ticket operation.

The journal coordinates local retries, not authentication or board permissions.
Callers hold the exact scope lock throughout effects, write intent before each
effect, and reconcile ambiguous stages themselves. Reopening never replays or
changes an operation. Configuration and payload are private, untrusted data;
only ``status`` is suitable for a metadata receipt.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid


MAX_CONFIGURATION_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 256 * 1024
SCOPE_FIELDS = frozenset({'owner_id', 'agent_id', 'project_id', 'ticket_id',
                          'board_session_id', 'board_endpoint'})
STAGES = ('new', 'claiming', 'claimed', 'prepared', 'running', 'result_ready',
          'publishing', 'complete', 'needs_review', 'abandoned',
          'asking_human', 'waiting_human', 'reclaiming', 'resuming')
# This is a graph: a confirmed answer returns to result_ready through a new
# claimed turn. Tuple order is only for display/validation, never progression.
_NEXT_STAGES = {
    'new': ('claiming',),
    'claiming': ('claimed',),
    'claimed': ('prepared',),
    'prepared': ('running',),
    'running': ('result_ready',),
    'result_ready': ('publishing', 'asking_human'),
    'publishing': ('complete',),
    'complete': (),
    'needs_review': (),
    'abandoned': (),
    'asking_human': ('waiting_human',),
    'waiting_human': ('reclaiming',),
    'reclaiming': ('resuming',),
    'resuming': ('result_ready',),
}
_APPLICATION_ID = 0x4242574A
_CREDENTIAL_KEYS = frozenset({'auth', 'authentication', 'authorization', 'credential',
    'credentials', 'token', 'accesstoken', 'refreshtoken', 'bearertoken', 'apikey',
    'password', 'passwd', 'secret', 'secrets', 'clientsecret', 'cookie', 'cookies'})
_COLUMNS = ('id', 'scope_key', 'scope_json', 'request_id', 'configuration_json',
            'stage', 'revision', 'payload_json', 'created_at', 'updated_at')


class JournalError(Exception):
    """A fixed code; never a path, instruction, response or credential."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _identifier(value, code, limit=256):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        raise JournalError(code)
    return value


def _encode(value, maximum, code):
    nodes = 0

    def validate(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 20 or nodes > 50000:
            raise JournalError(code)
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise JournalError(code)
                normalized = ''.join(character for character in key.lower() if character.isalnum())
                if normalized in _CREDENTIAL_KEYS:
                    # The existing text transport includes this bounded
                    # diagnostic, never an authentication credential.
                    if not (normalized == 'authentication' and isinstance(child, str)
                            and child in ('chatgpt', 'unknown', 'missing', 'api_key')):
                        raise JournalError('credential_fields_forbidden')
                validate(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                validate(child, depth + 1)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise JournalError(code)

    validate(value)
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             allow_nan=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > maximum:
            raise JournalError(code)
        return encoded
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise JournalError(code) from None


def _scope(value):
    if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
        raise JournalError('invalid_scope')
    for key, item in value.items():
        _identifier(item, 'invalid_scope', 2048 if key == 'board_endpoint' else 256)
    try:
        endpoint = urllib.parse.urlsplit(value['board_endpoint'])
        endpoint.port  # Reject malformed ports before persisting the scope.
        if (not endpoint.hostname or endpoint.username is not None or endpoint.password is not None
                or endpoint.query or endpoint.fragment
                or (endpoint.scheme != 'https' and not (
                    endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1')))):
            raise ValueError
    except ValueError:
        raise JournalError('invalid_scope') from None
    encoded = _encode(value, 4096, 'invalid_scope')
    return encoded, hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def _operation_id(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise JournalError('invalid_operation_id') from None


def _private_file(path):
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise JournalError('invalid_journal_file') from None
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(descriptor)
        raise JournalError('insecure_journal_file')
    return descriptor


class WorkJournal:
    def __init__(self, directory):
        self._db = None
        self._locks = {}
        self._lock_guard = threading.Lock()
        try:
            self.directory = Path(directory).expanduser().absolute()
            try:
                self.directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise JournalError('insecure_journal_directory')
            database = self.directory / 'work-journal.sqlite3'
            os.close(_private_file(database))
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    os.close(_private_file(sidecar))
            self._db = sqlite3.connect(database, timeout=5, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise JournalError('unsupported_journal_version')
            if version == 0:
                # Never silently add this schema to an unrelated database.
                if self._db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise JournalError('journal_corrupt')
            elif (self._db.execute('PRAGMA application_id').fetchone()[0] != _APPLICATION_ID
                    or tuple(row['name'] for row in self._db.execute('PRAGMA table_info(operations)')) != _COLUMNS):
                raise JournalError('journal_corrupt')
            self._db.execute('PRAGMA journal_mode=DELETE')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA secure_delete=ON')
            if version == 0:
                with self._transaction() as db:
                    db.execute('''CREATE TABLE operations (
                        id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, scope_json TEXT NOT NULL,
                        request_id TEXT NOT NULL, configuration_json TEXT NOT NULL,
                        stage TEXT NOT NULL, revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        UNIQUE(scope_key, request_id))''')
                    db.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                    db.execute('PRAGMA user_version=1')
        except (JournalError, OSError, sqlite3.Error, ValueError, TypeError) as error:
            if self._db is not None:
                self._db.close()
                self._db = None
            if isinstance(error, JournalError):
                raise
            raise JournalError('journal_unavailable') from None

    def __enter__(self):
        self._ready()
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        with self._lock_guard:
            if self._locks:
                raise JournalError('journal_locked')
            if self._db is not None:
                self._db.close()
                self._db = None

    def _ready(self):
        if self._db is None:
            raise JournalError('journal_closed')

    @contextmanager
    def _transaction(self):
        self._ready()
        try:
            self._db.execute('BEGIN IMMEDIATE')
            yield self._db
            self._db.execute('COMMIT')
        except BaseException as error:
            if self._db.in_transaction:
                self._db.execute('ROLLBACK')
            if isinstance(error, sqlite3.Error):
                raise JournalError('journal_unavailable') from None
            raise

    def _require_lock(self, key):
        with self._lock_guard:
            held = self._locks.get(key)
        if held is None or held[0] is None or held[1:] != (os.getpid(), threading.get_ident()):
            raise JournalError('scope_lock_required')

    @contextmanager
    def lock(self, scope):
        self._ready()
        _, key = _scope(scope)
        with self._lock_guard:
            if key in self._locks:
                raise JournalError('scope_busy')
            # Reserve before opening so an unsuccessful contender can never
            # remove another thread's entry during cleanup.
            self._locks[key] = (None, os.getpid(), threading.get_ident())
        descriptor = None
        try:
            descriptor = _private_file(self.directory / (key + '.lock'))
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise JournalError('scope_busy') from None
            except OSError:
                raise JournalError('scope_lock_unavailable') from None
            with self._lock_guard:
                self._locks[key] = (descriptor, os.getpid(), threading.get_ident())
            yield
        finally:
            with self._lock_guard:
                self._locks.pop(key, None)
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _decode(row):
        try:
            scope = json.loads(row['scope_json'])
            scope_json, key = _scope(scope)
            configuration = json.loads(row['configuration_json'])
            payload = json.loads(row['payload_json'])
            _operation_id(row['id'])
            _identifier(row['request_id'], 'invalid_request_id')
            if (key != row['scope_key'] or scope_json != row['scope_json']
                    or not isinstance(configuration, dict)
                    or not isinstance(payload, dict)
                    or _encode(configuration, MAX_CONFIGURATION_BYTES, 'invalid_configuration') != row['configuration_json']
                    or _encode(payload, MAX_PAYLOAD_BYTES, 'invalid_payload') != row['payload_json']
                    or row['stage'] not in STAGES or type(row['revision']) is not int or row['revision'] < 1
                    or any(type(row[field]) not in (float, int) or not math.isfinite(row[field])
                           for field in ('created_at', 'updated_at'))):
                raise ValueError
            return {'id': row['id'], 'scope': scope, 'request_id': row['request_id'],
                    'configuration': configuration, 'stage': row['stage'], 'revision': row['revision'],
                    'payload': payload, 'created_at': row['created_at'], 'updated_at': row['updated_at']}
        except (JournalError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise JournalError('journal_corrupt') from None

    def get(self, operation_id):
        self._ready()
        _operation_id(operation_id)
        try:
            row = self._db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
            if row is None:
                raise JournalError('operation_not_found')
            return self._decode(row)
        except sqlite3.Error:
            raise JournalError('journal_unavailable') from None

    def status(self, operation_id):
        operation = self.get(operation_id)
        return {key: value for key, value in operation.items() if key not in ('configuration', 'payload')}

    def find(self, scope, request_id):
        """Read an exact local request without creating or advancing work.

        Hold the scope lock across this read and a related recovery decision
        when the caller must exclude a concurrent begin/dispatch.
        """
        self._ready()
        _, key = _scope(scope)
        _identifier(request_id, 'invalid_request_id')
        try:
            row = self._db.execute('SELECT * FROM operations WHERE scope_key=? AND request_id=?',
                                   (key, request_id)).fetchone()
            return self._decode(row) if row is not None else None
        except sqlite3.Error:
            raise JournalError('journal_unavailable') from None

    def begin(self, scope, request_id, configuration):
        self._ready()
        scope_json, key = _scope(scope)
        self._require_lock(key)
        _identifier(request_id, 'invalid_request_id')
        if not isinstance(configuration, dict):
            raise JournalError('invalid_configuration')
        configuration_json = _encode(configuration, MAX_CONFIGURATION_BYTES, 'invalid_configuration')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE scope_key=? AND request_id=?',
                             (key, request_id)).fetchone()
            if row is not None:
                operation = self._decode(row)
                if row['configuration_json'] != configuration_json:
                    raise JournalError('request_configuration_conflict')
                return operation
            if db.execute("SELECT 1 FROM operations WHERE scope_key=? AND stage NOT IN ('complete','abandoned')", (key,)).fetchone():
                raise JournalError('scope_has_unfinished_operation')
            operation_id, now = str(uuid.uuid4()), time.time()
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (operation_id, key, scope_json, request_id, configuration_json, 'new', 1, '{}', now, now))
            return self.get(operation_id)

    def update(self, operation_id, expected_revision, *, stage, payload):
        if type(expected_revision) is not int or expected_revision < 1:
            raise JournalError('invalid_revision')
        if not isinstance(stage, str) or stage not in STAGES:
            raise JournalError('invalid_stage')
        if not isinstance(payload, dict):
            raise JournalError('invalid_payload')
        payload_json = _encode(payload, MAX_PAYLOAD_BYTES, 'invalid_payload')
        operation = self.get(operation_id)
        self._require_lock(_scope(operation['scope'])[1])
        with self._transaction() as db:
            operation = self.get(operation_id)
            if operation['revision'] != expected_revision:
                raise JournalError('revision_conflict')
            current = operation['stage']
            allowed = {current, *_NEXT_STAGES[current]}
            if current not in ('complete', 'needs_review', 'abandoned'):
                allowed.add('needs_review')
            if stage not in allowed or stage == 'abandoned':
                raise JournalError('invalid_stage_transition')
            # Human handoffs add graph states which a 0.5 reader cannot
            # interpret. Mark that compatibility boundary in the same commit
            # as the first handoff intent, never merely on opening an old
            # text-only journal. Keep version 2 after completion/abandonment.
            if stage == 'asking_human' and db.execute('PRAGMA user_version').fetchone()[0] < 2:
                db.execute('PRAGMA user_version=2')
            db.execute('''UPDATE operations SET stage=?,payload_json=?,revision=revision+1,updated_at=?
                          WHERE id=? AND revision=?''',
                       (stage, payload_json, time.time(), operation_id, expected_revision))
            return self.get(operation_id)

    def abandon(self, operation_id, expected_revision, reason):
        """Record an explicit human resolution; never infer external cleanup.

        The caller must reconcile board effects and close any linked RunStore
        work before this final local transition. Old request IDs stay tombstones.
        """
        _identifier(reason, 'invalid_abandonment_reason')
        if type(expected_revision) is not int or expected_revision < 1:
            raise JournalError('invalid_revision')
        operation = self.get(operation_id)
        self._require_lock(_scope(operation['scope'])[1])
        with self._transaction() as db:
            operation = self.get(operation_id)
            if operation['revision'] != expected_revision:
                raise JournalError('revision_conflict')
            if operation['stage'] == 'abandoned':
                if operation['payload'].get('abandonment', {}).get('reason') != reason:
                    raise JournalError('abandonment_conflict')
                return operation
            if operation['stage'] != 'needs_review':
                raise JournalError('invalid_stage_transition')
            previous = operation['payload']
            payload = dict(previous) if isinstance(previous, dict) else {'previous_payload': previous}
            payload['abandonment'] = {'reason': reason, 'at': time.time()}
            encoded = _encode(payload, MAX_PAYLOAD_BYTES, 'invalid_payload')
            db.execute('''UPDATE operations SET stage='abandoned',payload_json=?,revision=revision+1,updated_at=?
                          WHERE id=? AND revision=?''', (encoded, time.time(), operation_id, expected_revision))
            return self.get(operation_id)
