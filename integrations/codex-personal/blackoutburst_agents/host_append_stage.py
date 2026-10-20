"""Inert append proposals in the existing owner journal, never execution grants.

No method applies a proposal. A Board provisioning barrier cannot be proven by
this local primitive. Old consumers may ignore this optional table safely:
neither live configuration nor any work/activation/cleanup authority changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid

from .host_config import HostError, digest, validate
from .managed_host import _opaque, _sha
from .work_journal import _encode


MAX_PROPOSALS = 32
MAX_REQUEST = 262144 + 2048
_META = 'host_append_stage_meta'
_ROWS = 'host_append_stages'
_SCHEMA = {
    _META: ('id', 'version'),
    _ROWS: ('id', 'request_key', 'version', 'request_sha256', 'request_json',
            'guard_sha256', 'state', 'result_json'),
}
_SQL = {
    _META: f'CREATE TABLE {_META}(id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER NOT NULL)',
    _ROWS: f'''CREATE TABLE {_ROWS}(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
                version INTEGER NOT NULL,request_sha256 TEXT NOT NULL,request_json TEXT NOT NULL,
                guard_sha256 TEXT NOT NULL,state TEXT NOT NULL,result_json TEXT NOT NULL)''',
}


class _StageAttempt:
    """One live call under one real acquisition, never reconstructed from JSON."""
    __slots__ = ('journal','acquisition','pid','thread','key','id','request_hash','active')

    def __init__(self, journal, key, identifier, request_hash):
        self.journal, self.acquisition = journal, journal._owner_lock_token
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.key, self.id, self.request_hash, self.active = key, identifier, request_hash, True

    def __reduce_ex__(self, protocol):
        raise TypeError('host_append_attempt_not_serializable')


def _encoded(value, maximum=MAX_REQUEST):
    return _encode(value, maximum, 'host_append_invalid')


def _hashed(data):
    return hashlib.sha256(data.encode('utf-8')).hexdigest()


class HostAppendStageMixin:
    def _append_schema(self, db, *, create=False):
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", (_META, _ROWS))}
        if not names and create:
            for sql in _SQL.values():
                db.execute(sql)
            db.execute(f'INSERT INTO {_META} VALUES(1,1)')
            names = set(_SCHEMA)
        if names != set(_SCHEMA):
            raise HostError('host_append_schema_invalid')
        for table, columns in _SCHEMA.items():
            if tuple(row['name'] for row in db.execute('PRAGMA table_info('+table+')')) != columns:
                raise HostError('host_append_schema_invalid')
            if db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0] != _SQL[table]:
                raise HostError('host_append_schema_invalid')
        meta = db.execute(f'SELECT id,version FROM {_META} LIMIT 2').fetchall()
        if len(meta) != 1 or tuple(meta[0]) != (1, 1):
            raise HostError('host_append_schema_invalid')
        if len(db.execute(f'SELECT rowid FROM {_ROWS} LIMIT ?', (MAX_PROPOSALS+1,)).fetchall()) > MAX_PROPOSALS:
            raise HostError('host_append_schema_invalid')
        # Bound before loading/decoding a row, including hostile SQLite values.
        if db.execute(f'''SELECT 1 FROM {_ROWS} WHERE typeof(version)!='integer' OR version!=1
            OR typeof(id)!='text' OR length(CAST(id AS BLOB))!=36
            OR typeof(request_key)!='text' OR length(CAST(request_key AS BLOB)) NOT BETWEEN 1 AND 800
            OR typeof(request_sha256)!='text' OR length(CAST(request_sha256 AS BLOB))!=64
            OR typeof(guard_sha256)!='text' OR length(CAST(guard_sha256 AS BLOB))!=64
            OR typeof(state)!='text' OR state NOT IN ('intent','staged') OR typeof(request_json)!='text'
            OR length(CAST(request_json AS BLOB))>? OR typeof(result_json)!='text'
            OR length(CAST(result_json AS BLOB))>4096 LIMIT 1''', (MAX_REQUEST,)).fetchone():
            raise HostError('host_append_corrupt')
        # At most32 bounded rows; never adopt an unrelated malformed proposal.
        for row in db.execute(f'SELECT request_key FROM {_ROWS} LIMIT ?', (MAX_PROPOSALS,)):
            self._append_row(db, row['request_key'])

    def _append_guard(self, db, config_sha, catalog_sha):
        self._require_lock()
        if not self.managed():
            raise HostError('host_append_managed_required')
        if db.execute("SELECT 1 FROM control WHERE typeof(config)!='text' OR length(CAST(config AS BLOB))>262144").fetchone():
            raise HostError('host_append_corrupt')
        for table in ('targets','managed_targets'):
            if len(db.execute('SELECT rowid FROM '+table+' LIMIT 17').fetchall()) > 16:
                raise HostError('host_append_corrupt')
        self._check_managed()
        control = self.control()
        config = control['configuration']
        settings = self.managed_settings()
        if config['schema_version'] != 2:
            raise HostError('host_append_messages_required')
        if control['hash'] != config_sha or settings['catalog_sha256'] != catalog_sha:
            raise HostError('host_append_predecessor_changed')
        targets = [self.managed_target(t['id']) for t in config['targets']]
        if control['enabled'] or any(t['enabled'] for t in targets):
            raise HostError('host_append_requires_stopped')
        for table, terminal in (('dispatches', ('complete',)),
                                ('managed_commands', ('applied', 'superseded')),
                                ('managed_effects', ('complete', 'superseded'))):
            marks = ','.join('?' for _ in terminal)
            if db.execute(f'SELECT 1 FROM {table} WHERE state NOT IN ({marks}) LIMIT 1', terminal).fetchone():
                raise HostError('host_append_unsettled')
        catalog = [{'catalog_key':old['id'], 'profile_id':old['agent_id'],
            'profile_version':old['profile_version'], 'mode':'messages',
            'launchspec_sha256':current['launchspec_sha256']}
            for old, current in zip(config['targets'], targets)]
        if digest(sorted(catalog, key=lambda e:e['catalog_key'])) != catalog_sha:
            raise HostError('host_append_catalog_conflict')
        if db.execute("SELECT 1 FROM targets WHERE typeof(receipt)!='text' OR length(CAST(receipt AS BLOB))>8192 LIMIT 1").fetchone():
            raise HostError('host_append_corrupt')
        old_targets = [dict(row) for row in db.execute('SELECT * FROM targets ORDER BY id LIMIT 17')]
        if {t['id'] for t in old_targets} != {t['id'] for t in config['targets']}:
            raise HostError('host_append_corrupt')
        for target in old_targets:
            target['receipt'] = _hashed(target['receipt'])
        guard = {'control':{k:v for k,v in control.items() if k!='configuration'},
            'settings':settings, 'managed_targets':targets, 'targets':old_targets,
            'history_highwater':{table:db.execute('SELECT COALESCE(MAX(rowid),0) FROM '+table).fetchone()[0]
                for table in ('dispatches','managed_commands','managed_effects')}}
        return config, _hashed(_encoded(guard))

    def _append_candidate(self, old, candidate):
        # Validate detached bytes; do not bind/read credentials or execute paths.
        encoded = _encoded(candidate, 262144)
        normalized = validate(json.loads(encoded), bind_connections=False)
        if _encoded(normalized, 262144) != encoded:
            raise HostError('host_append_invalid')
        if (normalized['schema_version'] != 2 or len(old['targets']) >= 16
                or len(normalized['targets']) != len(old['targets'])+1
                or _encoded({k:v for k,v in old.items() if k!='targets'}) !=
                   _encoded({k:v for k,v in normalized.items() if k!='targets'})
                or _encoded(old['targets']) != _encoded(normalized['targets'][:-1])):
            raise HostError('host_append_not_append_only')
        added = normalized['targets'][-1]
        if (added['start_at'] != 'now'
                or any(added[k] == t[k] for t in old['targets'] for k in ('id','agent_id','board_session_id','workspace','connection_file'))):
            raise HostError('host_append_target_conflict')
        for field in ('connection_sha256', 'instruction_sha256'):
            _sha(added[field])
        return normalized

    @staticmethod
    def _append_result(row, request):
        added = request['candidate']['targets'][-1]
        return {'status':'staged', 'stage_id':row['id'],
            'previous_configuration_sha256':request['configuration_sha256'],
            'previous_catalog_sha256':request['catalog_sha256'],
            'candidate_configuration_sha256':digest(request['candidate']),
            'target_id':added['id'], 'profile_id':added['agent_id'],
            'target_enabled':False, 'work_budget':0, 'live_configuration_changed':False,
            'board_fence_verified':False, 'activation_allowed':False}

    def _append_row(self, db, key):
        row = db.execute(f'SELECT * FROM {_ROWS} WHERE request_key=?', (key,)).fetchone()
        if row is None:
            return None
        try:
            if str(uuid.UUID(row['id'])) != row['id']:
                raise ValueError
            _opaque(row['request_key']); _sha(row['request_sha256']); _sha(row['guard_sha256'])
            request = json.loads(row['request_json'])
            if (_encoded(request) != row['request_json'] or _hashed(row['request_json']) != row['request_sha256']
                    or set(request) != {'candidate','configuration_sha256','catalog_sha256'}):
                raise ValueError
            result = json.loads(row['result_json'])
            expected = {} if row['state']=='intent' else self._append_result(row, request)
            if _encoded(result,4096) != row['result_json'] or _encoded(expected,4096) != row['result_json']:
                raise ValueError
            return dict(row)
        except (HostError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            raise HostError('host_append_corrupt') from None

    def stage_messages_append(self, candidate, *, expected_configuration_sha256,
                              expected_catalog_sha256, idempotency_key):
        """Stage only. An interrupted intent is never resumed or adopted.

        The owner lock and fresh local guards are required even for replay.
        A terminal receipt is historical metadata, not a permit to commit.
        """
        self._require_lock()
        if type(self._owner_lock_token) is not object:
            raise HostError('host_lock_required')
        if self._append_active_attempt is not None:
            raise HostError('host_append_busy')
        _sha(expected_configuration_sha256); _sha(expected_catalog_sha256); _opaque(idempotency_key)
        attempt = None
        try:
            with self._tx() as db:
                old, guard = self._append_guard(db, expected_configuration_sha256, expected_catalog_sha256)
                proposed = self._append_candidate(old, candidate)
                request = {'candidate':proposed, 'configuration_sha256':expected_configuration_sha256,
                    'catalog_sha256':expected_catalog_sha256}
                encoded = _encoded(request)
                self._append_schema(db, create=True)
                previous = self._append_row(db, idempotency_key)
                if previous is not None:
                    if previous['request_json'] != encoded:
                        raise HostError('host_append_idempotency_conflict')
                    if previous['state'] != 'staged':
                        raise HostError('host_append_needs_review')
                    if previous['guard_sha256'] != guard:
                        raise HostError('host_append_predecessor_changed')
                    return json.loads(previous['result_json'])
                if db.execute(f"SELECT 1 FROM {_ROWS} WHERE state='intent' LIMIT 1").fetchone():
                    raise HostError('host_append_needs_review')
                if db.execute(f'SELECT COUNT(*) FROM {_ROWS}').fetchone()[0] >= MAX_PROPOSALS:
                    raise HostError('host_append_full')
                attempt = _StageAttempt(self, idempotency_key, str(uuid.uuid4()), _hashed(encoded))
                self._append_active_attempt = attempt
                db.execute(f'INSERT INTO {_ROWS} VALUES(?,?,1,?,?,?,\'intent\',\'{{}}\')',
                    (attempt.id, idempotency_key, attempt.request_hash, encoded, guard))
            return self._finish_messages_append_stage(attempt)
        finally:
            if attempt is not None:
                attempt.active = False
                if self._append_active_attempt is attempt:
                    self._append_active_attempt = None

    def _finish_messages_append_stage(self, attempt):
        self._require_lock()
        if (type(attempt) is not _StageAttempt or self._append_active_attempt is not attempt
                or not attempt.active or attempt.journal is not self
                or attempt.acquisition is None or attempt.acquisition is not self._owner_lock_token
                or attempt.pid != os.getpid() or attempt.thread != threading.get_ident()):
            raise HostError('host_append_attempt_expired')
        with self._tx() as db:
            self._append_schema(db)
            row = self._append_row(db, attempt.key)
            if (row is None or row['state'] != 'intent' or row['id'] != attempt.id
                    or row['request_sha256'] != attempt.request_hash):
                raise HostError('host_append_needs_review')
            request = json.loads(row['request_json'])
            old, guard = self._append_guard(db, request['configuration_sha256'], request['catalog_sha256'])
            self._append_candidate(old, request['candidate'])
            if guard != row['guard_sha256']:
                raise HostError('host_append_predecessor_changed')
            result = self._append_result(row, request)
            db.execute(f"UPDATE {_ROWS} SET state='staged',result_json=? WHERE id=? AND state='intent'",
                (_encoded(result,4096),row['id']))
            return result
