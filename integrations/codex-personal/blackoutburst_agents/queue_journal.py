"""Durable local selection of at most one ticket per explicit queue request.

The lock excludes project IDs so changing projects cannot bypass outstanding
work. Selection and worker request keys are committed before external effects.
Opening this database never contacts the board or executes a model.
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
import uuid


from .work_journal import (JournalError, MAX_CONFIGURATION_BYTES, MAX_PAYLOAD_BYTES,
    _encode, _identifier, _private_file, _operation_id, _scope as _work_scope)
from .event_journal import _configuration as _work_configuration, _receipt as _worker_receipt

QueueJournalError = JournalError
SCOPE_FIELDS = frozenset({'owner_id', 'agent_id', 'board_session_id', 'board_endpoint'})
STAGES = ('new', 'selecting', 'selected', 'dispatched', 'complete', 'needs_review', 'abandoned')
_NEXT_STAGES = {'new': ('selecting',), 'selecting': ('selected', 'complete'),
                'selected': ('dispatched',), 'dispatched': ('complete',),
                'complete': (), 'needs_review': (), 'abandoned': ()}
_APPLICATION_ID = 0x4242514A
_COLUMNS = ('id', 'scope_key', 'scope_json', 'request_id', 'configuration_json',
            'stage', 'revision', 'payload_json', 'created_at', 'updated_at')


def _scope(value):
    if not isinstance(value, dict) or set(value) != SCOPE_FIELDS:
        raise JournalError('invalid_scope')
    # Reuse the connection/identifier validator without including a project in
    # the queue lock: changing projects cannot escape unfinished selection.
    _work_scope({**value, 'project_id': 'queue', 'ticket_id': 'queue'})
    encoded = _encode(value, 4096, 'invalid_scope')
    # Sessions remain immutable within an operation but share the same lock.
    # A new chat cannot sidestep an unresolved selection for this profile.
    identity = _encode({key: item for key, item in value.items() if key != 'board_session_id'}, 4096, 'invalid_scope')
    return encoded, hashlib.sha256(identity.encode('utf-8')).hexdigest()


def _configuration(value):
    if not isinstance(value, dict) or 'project_id' not in value:
        raise JournalError('invalid_configuration')
    _identifier(value['project_id'], 'invalid_configuration')
    if 'routing' not in value or value.get('model') is not None:
        _identifier(value.get('model'), 'invalid_configuration')
    _work_configuration({key: item for key, item in value.items() if key != 'project_id'})
    return _encode(value, MAX_CONFIGURATION_BYTES, 'invalid_configuration')


class QueueJournal:
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
            database = self.directory / 'queue-journal.sqlite3'
            os.close(_private_file(database))
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    os.close(_private_file(sidecar))
            self._db = sqlite3.connect(database, timeout=5, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
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
                    db.execute("CREATE UNIQUE INDEX one_open_queue_operation ON operations(scope_key) WHERE stage NOT IN ('complete','abandoned')")
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
                    or _configuration(configuration) != row['configuration_json']
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
        result = {key: value for key, value in operation.items()
                  if key not in ('configuration', 'payload', 'scope', 'request_id')}
        for key in ('ticket_id', 'worker_operation_id'):
            value = operation['payload'].get(key)
            if value is not None:
                _identifier(value, 'journal_corrupt')
                result[key] = value
        return result

    def begin(self, scope, request_id, configuration):
        self._ready()
        scope_json, key = _scope(scope)
        self._require_lock(key)
        _identifier(request_id, 'invalid_request_id')
        if not isinstance(configuration, dict):
            raise JournalError('invalid_configuration')
        configuration_json = _configuration(configuration)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM operations WHERE scope_key=? AND request_id=?',
                             (key, request_id)).fetchone()
            if row is not None:
                operation = self._decode(row)
                if row['configuration_json'] != configuration_json or row['scope_json'] != scope_json:
                    raise JournalError('request_configuration_conflict')
                return operation
            if db.execute("SELECT 1 FROM operations WHERE scope_key=? AND stage NOT IN ('complete','abandoned')", (key,)).fetchone():
                raise JournalError('scope_has_unfinished_operation')
            operation_id, now = str(uuid.uuid4()), time.time()
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (operation_id, key, scope_json, request_id, configuration_json, 'new', 1,
                        _encode({'selection_key': 'queue-select-' + operation_id,
                                 'worker_request_id': 'queue-work-' + operation_id},
                                MAX_PAYLOAD_BYTES, 'invalid_payload'), now, now))
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
            if current in ('complete', 'abandoned'):
                raise JournalError('invalid_stage_transition')
            for name in ('selection_key', 'worker_request_id', 'ticket_id', 'selected_version',
                         'profile_sha256', 'project', 'worker_operation_id', 'selection_attempted',
                         'selection_confirmed', 'worker_preparing'):
                if name in operation['payload'] and payload.get(name) != operation['payload'][name]:
                    raise JournalError('immutable_queue_identity')
            if payload.get('selection_key') != 'queue-select-' + operation_id or payload.get('worker_request_id') != 'queue-work-' + operation_id:
                raise JournalError('invalid_payload')
            if stage in ('selected', 'dispatched'):
                _identifier(payload.get('ticket_id'), 'invalid_payload')
                if type(payload.get('selected_version')) is not int or payload['selected_version'] < 1:
                    raise JournalError('invalid_payload')
            if stage == 'dispatched':
                _operation_id(payload.get('worker_operation_id'))
            allowed = {current, *_NEXT_STAGES[current]}
            if current not in ('complete', 'needs_review', 'abandoned'):
                allowed.add('needs_review')
            if stage not in allowed or stage == 'abandoned':
                raise JournalError('invalid_stage_transition')
            db.execute('''UPDATE operations SET stage=?,payload_json=?,revision=revision+1,updated_at=?
                          WHERE id=? AND revision=?''',
                       (stage, payload_json, time.time(), operation_id, expected_revision))
            return self.get(operation_id)

    def abandon(self, operation_id, expected_revision, reason, *, proof=None, worker_absent=False):
        """Record an explicit human resolution; never infer external cleanup.

        The caller must reconcile board effects and close any linked RunStore
        work before this final local transition. Old request IDs stay tombstones.
        """
        _identifier(reason, 'invalid_abandonment_reason')
        if type(worker_absent) is not bool:
            raise JournalError('invalid_worker_absence')
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
            if (operation['payload'].get('selection_attempted') is True
                    and operation['payload'].get('selection_confirmed') is not True):
                raise JournalError('selection_unconfirmed')
            if worker_absent and (operation['payload'].get('worker_preparing') is not True
                                  or operation['payload'].get('worker_operation_id') or proof is not None):
                raise JournalError('invalid_worker_absence')
            if (operation['payload'].get('worker_operation_id') or operation['payload'].get('worker_preparing')) and not worker_absent:
                self._worker_proof(operation, proof)
                if proof['stage'] not in ('complete', 'abandoned'):
                    raise JournalError('worker_not_resolved')
            previous = operation['payload']
            payload = dict(previous) if isinstance(previous, dict) else {'previous_payload': previous}
            payload['abandonment'] = {'reason': reason, 'at': time.time()}
            if worker_absent:
                payload['abandonment']['worker_absence_confirmed'] = True
            if proof is not None:
                payload['abandonment']['worker_operation_id'] = proof['id']
                payload['abandonment']['worker_revision'] = proof['revision']
                payload['abandonment']['worker_stage'] = proof['stage']
            encoded = _encode(payload, MAX_PAYLOAD_BYTES, 'invalid_payload')
            db.execute('''UPDATE operations SET stage='abandoned',payload_json=?,revision=revision+1,updated_at=?
                          WHERE id=? AND revision=?''', (encoded, time.time(), operation_id, expected_revision))
            return self.get(operation_id)

    def record_selection(self, operation_id, expected_revision, result):
        """Record a read-only lookup result before explicit human abandonment.

        The caller must query the original pinned connection, session, key and
        project under this queue lock. An absent record is never proof of no
        effect after an attempted send. This method does not resume work.
        """
        operation = self.get(operation_id)
        if (operation['stage'] not in ('selecting', 'needs_review') or not isinstance(result, dict)
                or result.get('found') is not True or type(result.get('claimed')) is not bool
                or result.get('retried') is not True):
            raise JournalError('selection_unconfirmed')
        payload = {**operation['payload'], 'selection_confirmed': True}
        if result['claimed']:
            ticket = result.get('ticket')
            if (not isinstance(ticket, dict) or ticket.get('project_id') != operation['configuration']['project_id']
                    or type(ticket.get('version')) is not int or ticket['version'] < 1):
                raise JournalError('selection_proof_mismatch')
            _identifier(ticket.get('id'), 'selection_proof_mismatch')
            payload.update(ticket_id=ticket['id'], selected_version=ticket['version'])
        else:
            if result.get('reason') not in ('no_ready_ticket', 'unfinished_work', 'waiting_on_human'):
                raise JournalError('selection_proof_mismatch')
            payload['selection_reason'] = result['reason']
        return self.update(operation_id, expected_revision, stage=operation['stage'], payload=payload)

    @staticmethod
    def _worker_proof(operation, proof):
        """Match the precise child, never adopt work by scope alone."""
        payload, config = operation['payload'], operation['configuration']
        expected_scope = {**operation['scope'], 'project_id': config['project_id'],
                          'ticket_id': payload.get('ticket_id')}
        expected_config = {key: value for key, value in config.items() if key != 'project_id'}
        if (not isinstance(proof, dict) or operation['stage'] not in ('dispatched', 'needs_review', 'complete', 'abandoned')
                or not payload.get('ticket_id') or proof.get('scope') != expected_scope
                or proof.get('request_id') != payload['worker_request_id']
                or proof.get('configuration') != expected_config
                or (payload.get('worker_operation_id') and proof.get('id') != payload['worker_operation_id'])):
            raise JournalError('worker_proof_mismatch')
        _operation_id(proof.get('id'))
        if type(proof.get('revision')) is not int or proof['revision'] < 1:
            raise JournalError('worker_proof_mismatch')
        return proof

    def finish_worker(self, operation_id, expected_revision, receipt, proof):
        """Called under queue + child work lock after the worker returns."""
        _worker_receipt(receipt)
        operation = self.get(operation_id)
        self._worker_proof(operation, proof)
        if receipt.get('operation_id') != proof['id'] or receipt['stage'] != proof['stage']:
            raise JournalError('worker_proof_mismatch')
        stage = 'dispatched'
        reason = 'worker_pending'
        if receipt['status'] == 'complete':
            publication = proof.get('payload', {}).get('publication', {}).get('changes', {})
            if receipt.get('published_status') != publication.get('status'):
                raise JournalError('worker_proof_mismatch')
            stage = 'complete' if receipt.get('published_status') == 'review' else 'needs_review'
            reason = 'publication_confirmed' if stage == 'complete' else 'worker_blocked'
        elif receipt['status'] != 'pending':
            stage, reason = 'needs_review', 'worker_needs_review'
        return self.update(operation_id, expected_revision, stage=stage,
            payload={**operation['payload'], 'worker_operation_id': proof['id'],
                     'worker_receipt': receipt, 'reason': reason})

    def resolve(self, operation_id, proof):
        """Local reconciliation with an exact completed/abandoned child.

        A blocked publication is deliberately not success. An operator may
        acknowledge its effects and explicitly abandon the queue operation.
        """
        operation = self.get(operation_id)
        self._require_lock(_scope(operation['scope'])[1])
        self._worker_proof(operation, proof)
        if proof['stage'] not in ('complete', 'abandoned'):
            raise JournalError('worker_not_resolved')
        if proof['stage'] == 'complete' and proof.get('payload', {}).get('publication', {}).get('changes', {}).get('status') != 'review':
            raise JournalError('worker_not_successful')
        target = 'complete' if proof['stage'] == 'complete' else 'abandoned'
        with self._transaction() as db:
            current = self.get(operation_id)
            if current['revision'] != operation['revision']:
                raise JournalError('revision_conflict')
            resolution = {'worker_operation_id': proof['id'], 'worker_revision': proof['revision'], 'decision': proof['stage']}
            if current['stage'] in ('complete', 'abandoned'):
                if current['payload'].get('resolution') == resolution:
                    return current
                raise JournalError('resolution_conflict')
            payload = {**current['payload'], 'worker_operation_id': proof['id'],
                       'resolution': resolution, 'reason': 'worker_reconciled'}
            db.execute('''UPDATE operations SET stage=?,payload_json=?,revision=revision+1,updated_at=?
                WHERE id=? AND revision=?''', (target, _encode(payload, MAX_PAYLOAD_BYTES, 'invalid_payload'),
                time.time(), operation_id, operation['revision']))
            return self.get(operation_id)
