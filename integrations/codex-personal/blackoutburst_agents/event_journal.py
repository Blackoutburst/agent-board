"""Private durable ingestion for one explicitly selected ticket's activity.

Only event metadata is retained. A page and its cursor commit together; a batch
keeps its request key across interruptions. This is storage, not a scheduler:
opening it never reads the board, runs a model or retries an effect.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
from .model_policy import PolicyError, validate_routing_configuration
import re
import sqlite3
import stat
import threading
import time
import uuid

from .work_journal import (JournalError, STAGES, MAX_CONFIGURATION_BYTES,
                           _encode, _identifier, _private_file, _scope)


# Shared fixed error type lets callers handle private storage failures without
# exposing an underlying SQLite error, instruction, path or event content.
EventJournalError = JournalError
MAX_PAGE_EVENTS = 200
MAX_BATCH_EVENTS = 200
MAX_PENDING_EVENTS = 10000
MAX_EVENT_BYTES = 2048
MAX_RECEIPT_BYTES = 4096
HUMAN_TRIGGER_EVENTS = frozenset({'ticket_created', 'ticket_updated',
    'ticket_message_posted', 'human_input_resolved'})
_APPLICATION_ID = 0x4242454A
_EVENT_FIELDS = frozenset({'id', 'ticket_id', 'project_id', 'event', 'actor', 'created_at'})
_CONFIGURATION_FIELDS = frozenset({'instruction', 'workspace', 'executable', 'model', 'timeout'})
_COLUMNS = {
    'subscriptions': ('id', 'scope_key', 'scope_json', 'configuration_json',
                      'initial_after', 'after_id', 'created_at', 'updated_at'),
    'events': ('subscription_id', 'event_id', 'metadata_json', 'selected', 'batch_id'),
    'batches': ('id', 'subscription_id', 'event_ids_json', 'request_id', 'status',
                'dispatched', 'receipt_json', 'resolution_json', 'created_at', 'updated_at'),
}
_HUMAN_COLUMNS = {
    'human_links': ('batch_id', 'subscription_id', 'operation_id', 'request_id', 'owner_id',
                    'question_sha256', 'resolved'),
    'consumed_response_events': ('subscription_id', 'event_id', 'batch_id'),
}


def _uuid(value, code):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise EventJournalError(code) from None


def _integer(value, code, minimum=0):
    if type(value) is not int or not minimum <= value < 2 ** 63:
        raise EventJournalError(code)


def _time(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _configuration(value):
    if (not isinstance(value, dict) or not _CONFIGURATION_FIELDS <= set(value)
            or set(value) - _CONFIGURATION_FIELDS - {'human_owner_id', 'routing', 'runtime_managed'}):
        raise EventJournalError('invalid_configuration')
    if 'runtime_managed' in value and (value['runtime_managed'] is not True
            or (value.get('model') is None and 'routing' not in value)):
        raise EventJournalError('invalid_configuration')
    if 'routing' in value:
        try:
            validate_routing_configuration(value['routing'])
        except PolicyError:
            raise EventJournalError('invalid_configuration') from None
    if 'human_owner_id' in value:
        _identifier(value['human_owner_id'], 'invalid_configuration')
    instruction = value['instruction']
    if (not isinstance(instruction, str) or not instruction.strip() or '\x00' in instruction
            or not _time(value['timeout']) or not 1 <= value['timeout'] <= 300):
        raise EventJournalError('invalid_configuration')
    try:
        if len(instruction.encode('utf-8')) > 12000:
            raise ValueError
    except (ValueError, UnicodeError):
        raise EventJournalError('invalid_configuration') from None
    for field in ('workspace', 'executable'):
        _identifier(value[field], 'invalid_configuration', 4096)
        if not Path(value[field]).is_absolute():
            raise EventJournalError('invalid_configuration')
    if value['model'] is not None:
        _identifier(value['model'], 'invalid_configuration', 256)
    return _encode(value, MAX_CONFIGURATION_BYTES, 'invalid_configuration')


def _event(value, scope):
    if (not isinstance(value, dict) or not _EVENT_FIELDS <= set(value)
            or set(value) - _EVENT_FIELDS - {'request_id'}):
        raise EventJournalError('invalid_event')
    if 'request_id' in value:
        if value['event'] != 'human_input_resolved':
            raise EventJournalError('invalid_event')
        _identifier(value['request_id'], 'invalid_event')
    _integer(value['id'], 'invalid_event', 1)
    if value['ticket_id'] != scope['ticket_id'] or value['project_id'] != scope['project_id']:
        raise EventJournalError('event_scope_mismatch')
    actor = value['actor']
    if (not isinstance(actor, dict) or set(actor) != {'kind', 'owner_id'}
            or actor['kind'] not in ('human', 'agent') or not _time(value['created_at'])):
        raise EventJournalError('invalid_event')
    _identifier(actor['owner_id'], 'invalid_event')
    if not isinstance(value['event'], str) or not re.fullmatch('[a-z][a-z0-9_]{0,100}', value['event']):
        raise EventJournalError('invalid_event')
    encoded = _encode(value, MAX_EVENT_BYTES, 'invalid_event')
    selected = actor['kind'] == 'human' and value['event'] in HUMAN_TRIGGER_EVENTS
    return encoded, int(selected)


def _receipt(value):
    required = {'status', 'stage', 'reason', 'cached'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'operation_id', 'published_status'}
            or value['status'] not in ('complete', 'pending', 'needs_review', 'abandoned')
            or value['stage'] not in STAGES or type(value['cached']) is not bool
            or not isinstance(value['reason'], str)
            or not re.fullmatch('[a-z][a-z0-9_]{0,100}', value['reason'])
            or (value['status'] == 'complete' and value['stage'] != 'complete')
            or (value['status'] == 'pending' and value['stage'] in ('complete', 'abandoned', 'needs_review'))):
        raise EventJournalError('invalid_receipt')
    if 'operation_id' in value:
        _uuid(value['operation_id'], 'invalid_receipt')
    if 'published_status' in value and value['published_status'] not in ('review', 'blocked'):
        raise EventJournalError('invalid_receipt')
    return _encode(value, MAX_RECEIPT_BYTES, 'invalid_receipt')


class EventJournal:
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
                raise EventJournalError('insecure_journal_directory')
            database = self.directory / 'event-journal.sqlite3'
            os.close(_private_file(database))
            for suffix in ('-journal', '-wal', '-shm'):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    os.close(_private_file(sidecar))
            self._db = sqlite3.connect(database, timeout=5, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            version = self._db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1, 2):
                raise EventJournalError('unsupported_journal_version')
            if version == 0:
                if self._db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                    raise EventJournalError('journal_corrupt')
            elif (self._db.execute('PRAGMA application_id').fetchone()[0] != _APPLICATION_ID
                    or any(tuple(row['name'] for row in self._db.execute(f'PRAGMA table_info({table})')) != fields
                           for table, fields in (_COLUMNS | (_HUMAN_COLUMNS if version == 2 else {})).items())):
                raise EventJournalError('journal_corrupt')
            self._db.execute('PRAGMA journal_mode=DELETE')
            self._db.execute('PRAGMA synchronous=FULL')
            self._db.execute('PRAGMA secure_delete=ON')
            self._db.execute('PRAGMA foreign_keys=ON')
            if version == 0:
                with self._transaction() as db:
                    db.execute('''CREATE TABLE subscriptions (
                        id TEXT PRIMARY KEY, scope_key TEXT NOT NULL UNIQUE, scope_json TEXT NOT NULL,
                        configuration_json TEXT NOT NULL, initial_after INTEGER NOT NULL,
                        after_id INTEGER NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
                    db.execute('''CREATE TABLE batches (
                        id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                        event_ids_json TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                        dispatched INTEGER NOT NULL, receipt_json TEXT NOT NULL, resolution_json TEXT NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
                    db.execute("CREATE UNIQUE INDEX one_open_batch ON batches(subscription_id) WHERE status!='complete'")
                    db.execute('''CREATE TABLE events (
                        subscription_id TEXT NOT NULL REFERENCES subscriptions(id), event_id INTEGER NOT NULL,
                        metadata_json TEXT NOT NULL, selected INTEGER NOT NULL,
                        batch_id TEXT REFERENCES batches(id), PRIMARY KEY(subscription_id,event_id))''')
                    db.execute('CREATE INDEX event_pending ON events(subscription_id,selected,batch_id,event_id)')
                    db.execute(f'PRAGMA application_id={_APPLICATION_ID}')
                    db.execute('PRAGMA user_version=1')
            # v2 makes response absorption durable. Older binaries must refuse
            # the new journal rather than create another batch for its answer.
            if version in (0, 1):
                with self._transaction() as db:
                    db.execute('''CREATE TABLE human_links (
                        batch_id TEXT PRIMARY KEY REFERENCES batches(id),
                        subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                        operation_id TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                        question_sha256 TEXT NOT NULL, resolved INTEGER NOT NULL,
                        UNIQUE(subscription_id,request_id))''')
                    db.execute('''CREATE TABLE consumed_response_events (
                        subscription_id TEXT NOT NULL, event_id INTEGER NOT NULL,
                        batch_id TEXT NOT NULL REFERENCES human_links(batch_id),
                        PRIMARY KEY(subscription_id,event_id),
                        FOREIGN KEY(subscription_id,event_id) REFERENCES events(subscription_id,event_id))''')
                    db.execute('PRAGMA user_version=2')
        except (EventJournalError, OSError, sqlite3.Error, ValueError, TypeError) as error:
            if self._db is not None:
                self._db.close()
                self._db = None
            if isinstance(error, EventJournalError):
                raise
            raise EventJournalError('journal_unavailable') from None

    def __enter__(self):
        self._ready()
        return self

    def __exit__(self, *_):
        self.close()

    def _ready(self):
        if self._db is None:
            raise EventJournalError('journal_closed')

    def close(self):
        with self._lock_guard:
            if self._locks:
                raise EventJournalError('journal_locked')
            if self._db is not None:
                self._db.close()
                self._db = None

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
                raise EventJournalError('journal_unavailable') from None
            raise

    def _require_lock(self, key):
        with self._lock_guard:
            held = self._locks.get(key)
        if held is None or held[0] is None or held[1:] != (os.getpid(), threading.get_ident()):
            raise EventJournalError('scope_lock_required')

    @contextmanager
    def lock(self, scope):
        self._ready()
        _, key = _scope(scope)
        with self._lock_guard:
            if key in self._locks:
                raise EventJournalError('scope_busy')
            self._locks[key] = (None, os.getpid(), threading.get_ident())
        descriptor = None
        try:
            descriptor = _private_file(self.directory / (key + '.events.lock'))
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise EventJournalError('scope_busy') from None
            except OSError:
                raise EventJournalError('scope_lock_unavailable') from None
            with self._lock_guard:
                self._locks[key] = (descriptor, os.getpid(), threading.get_ident())
            yield
        finally:
            with self._lock_guard:
                self._locks.pop(key, None)
            if descriptor is not None:
                os.close(descriptor)

    def _decode_subscription(self, row):
        try:
            scope = json.loads(row['scope_json'])
            scope_json, key = _scope(scope)
            configuration = json.loads(row['configuration_json'])
            _uuid(row['id'], 'journal_corrupt')
            _integer(row['initial_after'], 'journal_corrupt')
            _integer(row['after_id'], 'journal_corrupt')
            if (scope_json != row['scope_json'] or key != row['scope_key']
                    or _configuration(configuration) != row['configuration_json']
                    or row['after_id'] < row['initial_after']
                    or not all(_time(row[field]) for field in ('created_at', 'updated_at'))):
                raise ValueError
            pending = self._db.execute('''SELECT COUNT(*) FROM events e LEFT JOIN batches b ON e.batch_id=b.id
                WHERE e.subscription_id=? AND e.selected=1 AND (e.batch_id IS NULL OR b.status!='complete')
                AND NOT EXISTS (SELECT 1 FROM consumed_response_events c
                    WHERE c.subscription_id=e.subscription_id AND c.event_id=e.event_id)''',
                (row['id'],)).fetchone()[0]
            return {'id': row['id'], 'scope': scope, 'configuration': configuration,
                    'initial_after': row['initial_after'], 'after': row['after_id'], 'pending_count': pending,
                    'created_at': row['created_at'], 'updated_at': row['updated_at']}
        except (EventJournalError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise EventJournalError('journal_corrupt') from None

    def get_subscription(self, subscription_id):
        self._ready()
        _uuid(subscription_id, 'invalid_subscription_id')
        try:
            row = self._db.execute('SELECT * FROM subscriptions WHERE id=?', (subscription_id,)).fetchone()
            if row is None:
                raise EventJournalError('subscription_not_found')
            return self._decode_subscription(row)
        except sqlite3.Error:
            raise EventJournalError('journal_unavailable') from None

    def status(self, subscription_id):
        """Metadata only; configuration and connection scope remain private."""
        return {key: value for key, value in self.get_subscription(subscription_id).items()
                if key not in ('scope', 'configuration')}

    def open_subscription(self, scope, configuration, after=0):
        self._ready()
        scope_json, key = _scope(scope)
        self._require_lock(key)
        configuration_json = _configuration(configuration)
        _integer(after, 'invalid_cursor')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM subscriptions WHERE scope_key=?', (key,)).fetchone()
            if row is not None:
                subscription = self._decode_subscription(row)
                if row['configuration_json'] != configuration_json or row['initial_after'] != after:
                    raise EventJournalError('subscription_configuration_conflict')
                return subscription
            subscription_id, now = str(uuid.uuid4()), time.time()
            db.execute('INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)',
                       (subscription_id, key, scope_json, configuration_json, after, after, now, now))
            return self.get_subscription(subscription_id)

    def append_page(self, subscription_id, expected_after, events):
        """Insert a complete ASC page and advance its cursor in one commit.

        A replay with a stale cursor succeeds only when the entire supplied
        page is already stored identically. It never appends a stale suffix.
        """
        _integer(expected_after, 'invalid_cursor')
        if not isinstance(events, list) or len(events) > MAX_PAGE_EVENTS:
            raise EventJournalError('invalid_event_page')
        subscription = self.get_subscription(subscription_id)
        self._require_lock(_scope(subscription['scope'])[1])
        previous, encoded_events = expected_after, []
        for event in events:
            encoded, selected = _event(event, subscription['scope'])
            if event['id'] <= previous:
                raise EventJournalError('invalid_event_order')
            previous = event['id']
            encoded_events.append((event['id'], encoded, selected))
        with self._transaction() as db:
            subscription = self.get_subscription(subscription_id)
            new_events = []
            for event_id, encoded, selected in encoded_events:
                row = db.execute('SELECT metadata_json,selected FROM events WHERE subscription_id=? AND event_id=?',
                                 (subscription_id, event_id)).fetchone()
                if row is not None:
                    if row['metadata_json'] != encoded or row['selected'] != selected:
                        raise EventJournalError('event_conflict')
                else:
                    new_events.append((subscription_id, event_id, encoded, selected, None))
            if expected_after != subscription['after']:
                if expected_after < subscription['after'] and encoded_events and not new_events:
                    return subscription
                raise EventJournalError('cursor_conflict')
            db.executemany('INSERT INTO events VALUES(?,?,?,?,?)', new_events)
            self._consume_responses(db, subscription_id)
            if self.get_subscription(subscription_id)['pending_count'] > MAX_PENDING_EVENTS:
                raise EventJournalError('pending_event_limit')
            if encoded_events:
                db.execute('UPDATE subscriptions SET after_id=?,updated_at=? WHERE id=?',
                           (encoded_events[-1][0], time.time(), subscription_id))
            return self.get_subscription(subscription_id)

    def _decode_batch(self, row):
        try:
            _uuid(row['id'], 'journal_corrupt')
            _uuid(row['subscription_id'], 'journal_corrupt')
            ids = json.loads(row['event_ids_json'])
            receipt = json.loads(row['receipt_json'])
            resolution = json.loads(row['resolution_json'])
            if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_BATCH_EVENTS
                    or any(type(item) is not int or not 0 < item < 2 ** 63 for item in ids)
                    or ids != sorted(set(ids)) or _encode(ids, 8192, 'journal_corrupt') != row['event_ids_json']
                    or row['request_id'] != 'board-events-' + row['id']
                    or row['status'] not in ('prepared', 'pending', 'complete', 'needs_review')
                    or row['dispatched'] not in (0, 1)
                    or not all(_time(row[field]) for field in ('created_at', 'updated_at'))
                    or (receipt is not None and _receipt(receipt) != row['receipt_json'])
                    or (row['status'] == 'prepared' and receipt is not None)
                    or (row['status'] != 'prepared' and not row['dispatched'])
                    or (row['status'] != 'prepared' and receipt is None and resolution is None)):
                raise ValueError
            if resolution is not None:
                if (not isinstance(resolution, dict) or set(resolution) != {'decision', 'operation_id', 'revision'}
                        or resolution['decision'] not in ('complete', 'abandoned') or row['status'] != 'complete'
                        or _encode(resolution, 1024, 'journal_corrupt') != row['resolution_json']):
                    raise ValueError
                _uuid(resolution['operation_id'], 'journal_corrupt')
                _integer(resolution['revision'], 'journal_corrupt', 1)
                if receipt is not None and receipt.get('operation_id', resolution['operation_id']) != resolution['operation_id']:
                    raise ValueError
            elif receipt is not None:
                mapped = 'needs_review' if receipt['status'] == 'abandoned' else receipt['status']
                if mapped != row['status']:
                    raise ValueError
            subscription = self.get_subscription(row['subscription_id'])
            stored = self._db.execute('SELECT * FROM events WHERE batch_id=? ORDER BY event_id', (row['id'],)).fetchall()
            if [item['event_id'] for item in stored] != ids:
                raise ValueError
            for item in stored:
                encoded, selected = _event(json.loads(item['metadata_json']), subscription['scope'])
                if (item['subscription_id'] != row['subscription_id'] or not selected or item['selected'] != 1
                        or encoded != item['metadata_json'] or json.loads(encoded)['id'] != item['event_id']
                        or item['event_id'] > subscription['after']):
                    raise ValueError
            return {'id': row['id'], 'subscription_id': row['subscription_id'], 'event_ids': ids,
                    'request_id': row['request_id'], 'status': row['status'],
                    'dispatched': bool(row['dispatched']), 'receipt': receipt, 'resolution': resolution,
                    'created_at': row['created_at'], 'updated_at': row['updated_at']}
        except (EventJournalError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise EventJournalError('journal_corrupt') from None

    def get_batch(self, batch_id):
        self._ready()
        _uuid(batch_id, 'invalid_batch_id')
        try:
            row = self._db.execute('SELECT * FROM batches WHERE id=?', (batch_id,)).fetchone()
            if row is None:
                raise EventJournalError('batch_not_found')
            return self._decode_batch(row)
        except sqlite3.Error:
            raise EventJournalError('journal_unavailable') from None

    def prepare_batch(self, subscription_id):
        subscription = self.get_subscription(subscription_id)
        self._require_lock(_scope(subscription['scope'])[1])
        with self._transaction() as db:
            row = db.execute("SELECT * FROM batches WHERE subscription_id=? AND status!='complete'",
                             (subscription_id,)).fetchone()
            if row is not None:
                return self._decode_batch(row)
            rows = db.execute('''SELECT event_id FROM events e WHERE subscription_id=? AND selected=1
                AND batch_id IS NULL AND NOT EXISTS (SELECT 1 FROM consumed_response_events c
                    WHERE c.subscription_id=e.subscription_id AND c.event_id=e.event_id)
                ORDER BY event_id LIMIT ?''', (subscription_id, MAX_BATCH_EVENTS)).fetchall()
            if not rows:
                return None
            ids = [row['event_id'] for row in rows]
            batch_id, now = str(uuid.uuid4()), time.time()
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (batch_id, subscription_id, _encode(ids, 8192, 'invalid_event_page'),
                        'board-events-' + batch_id, 'prepared', 0, 'null', 'null', now, now))
            db.executemany('UPDATE events SET batch_id=? WHERE subscription_id=? AND event_id=?',
                           [(batch_id, subscription_id, event_id) for event_id in ids])
            return self.get_batch(batch_id)

    def start_batch(self, batch_id):
        """Record dispatch intent before the first potentially remote effect."""
        batch = self.get_batch(batch_id)
        subscription = self.get_subscription(batch['subscription_id'])
        self._require_lock(_scope(subscription['scope'])[1])
        with self._transaction() as db:
            batch = self.get_batch(batch_id)
            if batch['status'] == 'needs_review':
                raise EventJournalError('batch_needs_review')
            if not batch['dispatched']:
                db.execute('UPDATE batches SET dispatched=1,updated_at=? WHERE id=?', (time.time(), batch_id))
            return self.get_batch(batch_id)

    def _consume_responses(self, db, subscription_id):
        """Absorb only a response to the proven question of an existing batch.

        Keep original event metadata/selection immutable for replay validation.
        A separate relation also avoids growing a full 200-event batch when its
        answer arrives. Unrelated messages and edits remain pending. An event
        is only a deduplication hint, never proof to resume: BoardWorker still
        reads and validates the actual answer. Keeping the link after explicit
        abandonment prevents a late answer from starting replacement work.
        """
        rows = db.execute('''SELECT e.* FROM events e WHERE subscription_id=?
            AND selected=1 AND batch_id IS NULL AND NOT EXISTS (
                SELECT 1 FROM consumed_response_events c WHERE c.subscription_id=e.subscription_id
                AND c.event_id=e.event_id)''', (subscription_id,)).fetchall()
        for row in rows:
            event = json.loads(row['metadata_json'])
            if event.get('event') != 'human_input_resolved' or not event.get('request_id'):
                continue
            link = db.execute('''SELECT * FROM human_links WHERE subscription_id=?
                AND request_id=? AND owner_id=?''',
                (subscription_id, event['request_id'], event['actor']['owner_id'])).fetchone()
            if link is not None and event['actor']['kind'] == 'human':
                db.execute('INSERT INTO consumed_response_events VALUES(?,?,?)',
                           (subscription_id, row['event_id'], link['batch_id']))

    def _link_human(self, db, batch, subscription, operation, receipt=None):
        """Validate private WorkJournal proof; retain no prompt/question/answer.

        The caller reads this proof under its WorkJournal scope lock. Scope
        alone never identifies work: the exact batch request and immutable
        configuration must also match, including the locally chosen recipient.
        """
        try:
            if (not isinstance(operation, dict) or not batch['dispatched']
                    or operation.get('scope') != subscription['scope']
                    or operation.get('request_id') != batch['request_id']
                    or _configuration(operation.get('configuration')) != _configuration(subscription['configuration'])
                    or operation.get('stage') not in STAGES):
                raise ValueError
            _uuid(operation.get('id'), 'operation_proof_mismatch')
            _integer(operation.get('revision'), 'operation_proof_mismatch', 1)
            if receipt is not None and (receipt.get('operation_id') != operation['id']
                    or receipt['stage'] != operation['stage']):
                raise ValueError
            if batch['receipt'] and batch['receipt'].get('operation_id', operation['id']) != operation['id']:
                raise ValueError
            payload = operation.get('payload')
            if not isinstance(payload, dict):
                raise ValueError
            request = payload.get('human_request')
            if request is None:
                if payload.get('human_used') and operation['stage'] not in ('asking_human', 'needs_review', 'abandoned'):
                    raise ValueError
                return
            intent = payload.get('human_intent')
            scope, config = subscription['scope'], subscription['configuration']
            if (payload.get('human_used') is not True or not isinstance(request, dict)
                    or not isinstance(intent, dict) or not config.get('human_owner_id')
                    or intent.get('owner_id') != config['human_owner_id']
                    or intent.get('idempotency_key') != 'board-human-' + operation['id']
                    or request.get('owner_id') != config['human_owner_id']
                    or request.get('ticket_id') != scope['ticket_id']
                    or request.get('question') != intent.get('question')
                    or request.get('status') not in ('open', 'resolved')):
                raise ValueError
            _identifier(request.get('id'), 'operation_proof_mismatch')
            question = request.get('question')
            if (not isinstance(question, str) or not question.strip() or '\x00' in question
                    or len(question.encode('utf-8')) > 6000):
                raise ValueError
            author = request.get('requested_by')
            if (not isinstance(author, dict) or author.get('kind') != 'agent'
                    or any(author.get(key) != scope[key] for key in ('owner_id', 'agent_id'))
                    or author.get('session_id') != scope['board_session_id']):
                raise ValueError
            _identifier(author.get('device_id'), 'operation_proof_mismatch')
            resolved = request['status'] == 'resolved'
            if resolved and (not _time(request.get('resolved_at')) or request['resolved_at'] <= 0
                    or not isinstance(request.get('answer'), str) or not request['answer'].strip()):
                raise ValueError
            link = (batch['id'], subscription['id'], operation['id'], request['id'], request['owner_id'],
                    hashlib.sha256(request['question'].encode('utf-8')).hexdigest(), int(resolved))
        except (EventJournalError, ValueError, TypeError, KeyError, UnicodeError):
            raise EventJournalError('operation_proof_mismatch') from None
        existing = db.execute('SELECT * FROM human_links WHERE batch_id=?', (batch['id'],)).fetchone()
        if existing is not None:
            if tuple(existing)[:-1] != link[:-1] or existing['resolved'] > link[-1]:
                raise EventJournalError('operation_proof_mismatch')
            db.execute('UPDATE human_links SET resolved=? WHERE batch_id=?', (link[-1], batch['id']))
        else:
            db.execute('INSERT INTO human_links VALUES(?,?,?,?,?,?,?)', link)
        self._consume_responses(db, subscription['id'])

    def finish_batch(self, batch_id, receipt, *, operation=None):
        encoded = _receipt(receipt)
        batch = self.get_batch(batch_id)
        subscription = self.get_subscription(batch['subscription_id'])
        self._require_lock(_scope(subscription['scope'])[1])
        status = 'needs_review' if receipt['status'] == 'abandoned' else receipt['status']
        with self._transaction() as db:
            batch = self.get_batch(batch_id)
            if operation is not None:
                self._link_human(db, batch, subscription, operation, receipt)
            elif subscription['configuration'].get('human_owner_id') and receipt.get('operation_id'):
                raise EventJournalError('operation_proof_required')
            if batch['status'] in ('complete', 'needs_review'):
                if batch['receipt'] != receipt:
                    raise EventJournalError('batch_receipt_conflict')
                return batch
            if not batch['dispatched']:
                raise EventJournalError('batch_not_started')
            if (batch['receipt'] is not None and batch['receipt'].get('operation_id') is not None
                    and receipt.get('operation_id') != batch['receipt']['operation_id']):
                raise EventJournalError('batch_receipt_conflict')
            if batch['receipt'] == receipt:
                return batch
            db.execute('UPDATE batches SET status=?,receipt_json=?,updated_at=? WHERE id=?',
                       (status, encoded, time.time(), batch_id))
            return self.get_batch(batch_id)

    def resolve_batch(self, batch_id, operation, *, verified_human_request=None):
        """Reconcile only an explicit local WorkJournal complete/abandoned proof.

        The caller must load ``operation`` from its private WorkJournal. This
        never abandons work or performs remote reconciliation on the caller's
        behalf. WorkJournal abandonment has its own human acknowledgement and
        active-process checks. The original receipt remains untouched for audit.

        A question whose creation was ambiguous cannot be closed until its ID
        is known. ``verified_human_request`` must come from an explicit exact
        lookup by the caller, never an inferred request or a negative lookup.
        It enriches only this proof; the WorkJournal remains unchanged.
        """
        batch = self.get_batch(batch_id)
        subscription = self.get_subscription(batch['subscription_id'])
        self._require_lock(_scope(subscription['scope'])[1])
        try:
            if (not isinstance(operation, dict)
                    or operation.get('scope') != subscription['scope']
                    or operation.get('request_id') != batch['request_id']
                    or _configuration(operation.get('configuration')) != _configuration(subscription['configuration'])
                    or operation.get('stage') not in ('complete', 'abandoned')):
                raise ValueError
            _uuid(operation.get('id'), 'resolution_mismatch')
            _integer(operation.get('revision'), 'resolution_mismatch', 1)
            resolution = {'decision': operation['stage'], 'operation_id': operation['id'],
                          'revision': operation['revision']}
        except (EventJournalError, ValueError, TypeError, KeyError):
            raise EventJournalError('resolution_mismatch') from None
        if not batch['dispatched']:
            raise EventJournalError('batch_not_started')
        payload = operation.get('payload')
        if not isinstance(payload, dict):
            raise EventJournalError('resolution_mismatch')
        known_request = payload.get('human_request')
        if verified_human_request is not None:
            if not isinstance(verified_human_request, dict) or not verified_human_request.get('id'):
                raise EventJournalError('human_request_unresolved')
            if known_request is not None and known_request != verified_human_request:
                raise EventJournalError('resolution_mismatch')
            operation = {**operation, 'payload': {**payload, 'human_request': verified_human_request}}
        elif payload.get('human_used') and payload.get('human_intent') and not (
                isinstance(known_request, dict) and known_request.get('id')):
            raise EventJournalError('human_request_unresolved')
        with self._transaction() as db:
            batch = self.get_batch(batch_id)
            if not batch['dispatched']:
                raise EventJournalError('batch_not_started')
            if batch['receipt'] is not None and batch['receipt'].get('operation_id', operation['id']) != operation['id']:
                raise EventJournalError('resolution_mismatch')
            self._link_human(db, batch, subscription, operation)
            if batch['resolution'] is not None:
                if batch['resolution'] != resolution:
                    raise EventJournalError('resolution_mismatch')
                return batch
            if batch['status'] == 'complete' and operation['stage'] != 'complete':
                raise EventJournalError('resolution_mismatch')
            db.execute("UPDATE batches SET status='complete',resolution_json=?,updated_at=? WHERE id=?",
                       (_encode(resolution, 1024, 'resolution_mismatch'), time.time(), batch_id))
            return self.get_batch(batch_id)
