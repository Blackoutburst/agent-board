"""Owner-wide execution reservations; metadata only, never a process supervisor.

All admissions are serialized in Board._tx. A lost host/heartbeat never releases
capacity: only an authenticated exact execution's cleanup confirmation does.
"""
from __future__ import annotations

import json
import re
import secrets
import time

from projects import _fail, _json, _same_claim, _version

STALE_AFTER_SECONDS = 60
OUTCOMES = frozenset({'completed', 'blocked', 'cancelled'})
REASONS = frozenset({'', 'completed', 'cancelled', 'quota_or_rate_limit',
    'authentication_failed', 'execution_failed', 'unavailable', 'timeout',
    'cleanup_unconfirmed', 'cleanup_failed', 'runner_error', 'protocol_error',
    'policy_changed', 'claim_lost', 'stop_requested', 'owner_paused'})
_OPAQUE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$')
_MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$')
_SHA256 = re.compile(r'^[0-9a-f]{64}$')


def _identifier(value, name, *, model=False):
    if type(value) is not str or not (_MODEL if model else _OPAQUE).fullmatch(value):
        _fail(f'invalid_{name}')
    return value


def _bool(value, name):
    if type(value) is not bool:
        _fail(f'invalid_{name}')
    return value


def migrate_runtime(db):
    for statement in (
        """CREATE TABLE owner_execution_limits(
            owner_id TEXT PRIMARY KEY REFERENCES owners(id),
            max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent BETWEEN 1 AND 8),
            paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0,1)),
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0), blocked_reason TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE runtime_executions(
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
            agent_id TEXT NOT NULL REFERENCES agent_profiles(id), profile_version INTEGER NOT NULL,
            device_id TEXT NOT NULL REFERENCES devices(id), session_id TEXT NOT NULL REFERENCES sessions(id),
            project_id TEXT NOT NULL REFERENCES projects(id), ticket_id TEXT NOT NULL REFERENCES tickets(id),
            work_id TEXT NOT NULL, turn_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
            idempotency_key TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','running','finished','needs_review')),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
            stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0,1)),
            cleanup_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_confirmed IN (0,1)),
            outcome TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
            start_revision INTEGER, heartbeat_revision INTEGER, finish_revision INTEGER,
            created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL, last_heartbeat_at REAL, finished_at REAL,
            UNIQUE(owner_id,device_id,session_id,idempotency_key),
            UNIQUE(owner_id,work_id,turn_id))""",
        "CREATE INDEX runtime_owner_state ON runtime_executions(owner_id,state)",
    ):
        db.execute(statement)


def migrate_message_runtime(db):
    """Schema 10: nullable ticket references and a separate exact message scope.

    SQLite cannot remove NOT NULL in place. Rebuild inside Board's existing
    transaction, preserving all legacy values, indexes and table triggers.
    """
    columns = db.execute('PRAGMA table_info(runtime_executions)').fetchall()
    names = [row['name'] for row in columns]
    extra = {'scope_kind', 'thread_id', 'message_id', 'source_sha256'}
    if extra.intersection(names):
        if (not extra.issubset(names) or any(row['notnull'] for row in columns
                if row['name'] in ('project_id','ticket_id'))):
            raise RuntimeError('Unsupported message execution schema')
        return
    objects = [row['sql'] for row in db.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='runtime_executions' "
        "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name")]
    db.execute('''CREATE TABLE runtime_executions_v10(
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
        agent_id TEXT NOT NULL REFERENCES agent_profiles(id), profile_version INTEGER NOT NULL,
        device_id TEXT NOT NULL REFERENCES devices(id), session_id TEXT NOT NULL REFERENCES sessions(id),
        project_id TEXT REFERENCES projects(id), ticket_id TEXT REFERENCES tickets(id),
        work_id TEXT NOT NULL, turn_id TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
        idempotency_key TEXT NOT NULL, payload TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('reserved','running','finished','needs_review')),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
        stop_requested INTEGER NOT NULL DEFAULT 0 CHECK(stop_requested IN (0,1)),
        cleanup_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(cleanup_confirmed IN (0,1)),
        outcome TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
        start_revision INTEGER, heartbeat_revision INTEGER, finish_revision INTEGER,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, started_at REAL,
        last_heartbeat_at REAL, finished_at REAL,
        scope_kind TEXT NOT NULL DEFAULT 'ticket' CHECK(scope_kind IN ('ticket','message')),
        thread_id TEXT REFERENCES threads(id), message_id TEXT REFERENCES messages(id), source_sha256 TEXT,
        UNIQUE(owner_id,device_id,session_id,idempotency_key), UNIQUE(owner_id,work_id,turn_id),
        CHECK((scope_kind='ticket' AND project_id IS NOT NULL AND ticket_id IS NOT NULL
            AND thread_id IS NULL AND message_id IS NULL AND source_sha256 IS NULL)
          OR (scope_kind='message' AND thread_id IS NOT NULL AND message_id IS NOT NULL
            AND source_sha256 IS NOT NULL AND length(source_sha256)=64
            AND source_sha256 NOT GLOB '*[^0-9a-f]*'
            AND ((project_id IS NULL AND ticket_id IS NULL)
              OR (project_id IS NOT NULL AND ticket_id IS NOT NULL)))))''')
    fields = ','.join('"' + name + '"' for name in names)
    db.execute(f'INSERT INTO runtime_executions_v10({fields}) SELECT {fields} FROM runtime_executions')
    db.execute('DROP TABLE runtime_executions')
    db.execute('ALTER TABLE runtime_executions_v10 RENAME TO runtime_executions')
    for statement in objects:
        db.execute(statement)


class RuntimeMixin:
    def _runtime_identity(self, db, actor):
        """Identity-only channel for this executor's cleanup, never new work.

        Existing owner/profile grants may be disabled after launch. The original
        authenticated device/session may still stop and attest cleanup; revoked
        devices cannot. Callers must also select an execution by this exact tuple.
        """
        if not isinstance(actor, dict) or actor.get('kind') != 'agent':
            _fail('agent_authentication_required', 403)
        _identifier(actor.get('session_id'), 'session_id')
        row = db.execute('''SELECT s.*,d.revoked_at FROM sessions s JOIN devices d ON d.id=s.device_id
            WHERE s.id=? AND s.owner_id=? AND s.device_id=? AND d.owner_id=s.owner_id''',
            (actor['session_id'], actor.get('owner_id'), actor.get('device_id'))).fetchone()
        if not row or row['revoked_at'] is not None or not row['agent_id']:
            _fail('execution_identity_unavailable', 403)
        return {'kind': 'agent', 'owner_id': row['owner_id'], 'device_id': row['device_id'],
                'session_id': row['id']}, row

    def _runtime_execution(self, db, actor, execution_id):
        actor, session = self._runtime_identity(db, actor)
        row = db.execute('''SELECT * FROM runtime_executions
            WHERE id=? AND owner_id=? AND device_id=? AND session_id=? AND agent_id=?''',
            (_identifier(execution_id, 'execution_id'), actor['owner_id'], actor['device_id'],
             actor['session_id'], session['agent_id'])).fetchone()
        if row is None:
            _fail('execution_not_found', 404)
        return actor, row

    def _runtime_limits(self, db, owner_id):
        row = db.execute('SELECT * FROM owner_execution_limits WHERE owner_id=?', (owner_id,)).fetchone()
        return {key: row[key] for key in ('owner_id', 'max_concurrent', 'version')} | {'paused': bool(row['paused'])} if row else {
            'owner_id': owner_id, 'max_concurrent': 1, 'paused': False, 'version': 1}

    def _runtime_authorize(self, db, actor, project_id, ticket_id, profile_version=None):
        actor = self._actor(db, actor, 'allow_requests')
        self._actor(db, actor, 'allow_read')
        if actor['kind'] != 'agent':
            _fail('agent_authentication_required', 403)
        profile = self._profile_for_actor(db, actor)
        if profile_version is not None and profile['version'] != profile_version:
            _fail('profile_changed', 409)
        project = self._project_member(db, actor, project_id)
        if project['archived']:
            _fail('project_archived', 409)
        ticket = self._ticket_member(db, actor, ticket_id)
        if ticket['project_id'] != project_id:
            _fail('ticket_project_mismatch', 409)
        claimant = json.loads(ticket['claimant']) if ticket['claimant'] else None
        if (not _same_claim(claimant, actor) or ticket['status'] != 'in_progress'
                or (ticket['lease_expires_at'] or 0) <= time.time()):
            _fail('active_ticket_claim_required', 409)
        return actor, profile

    def _runtime_message_authorize(self, db, actor, thread_id, message_id, source_sha256,
                                   profile_version=None):
        actor = self._actor(db, actor, 'allow_read')
        self._actor(db, actor, 'allow_reply')
        if actor['kind'] != 'agent':
            _fail('agent_authentication_required', 403)
        profile = self._profile_for_actor(db, actor)
        if profile_version is not None and profile['version'] != profile_version:
            _fail('profile_changed', 409)
        self._member(db, actor, thread_id)
        message = db.execute('SELECT * FROM messages WHERE id=? AND thread_id=?',
                             (message_id,thread_id)).fetchone()
        if message is None:
            _fail('message_not_found', 404)
        author = json.loads(message['author'])
        if (not isinstance(author,dict) or author.get('kind') != 'human'
                or not set(self._inbox_targets(db,actor)).intersection(json.loads(message['mentions']))):
            _fail('addressed_human_message_required', 403)
        if self._message_source_hash(message) != source_sha256:
            _fail('message_source_changed', 409)
        # Thread membership alone must never reveal a private project's IDs.
        linked = db.execute('''SELECT t.project_id,t.id AS ticket_id FROM tickets t
            JOIN project_members m ON m.project_id=t.project_id
            WHERE t.thread_id=? AND m.owner_id=?''', (thread_id,actor['owner_id'])).fetchone()
        return actor, profile, (dict(linked) if linked else {'project_id':None,'ticket_id':None})

    def _runtime_authorize_row(self, db, actor, row):
        if row['scope_kind'] == 'message':
            actor, profile, linked = self._runtime_message_authorize(db,actor,row['thread_id'],
                row['message_id'],row['source_sha256'],row['profile_version'])
            if any(row[key] != linked[key] for key in ('project_id','ticket_id')):
                _fail('message_context_changed',409)
            return actor, profile
        return self._runtime_authorize(db,actor,row['project_id'],row['ticket_id'],row['profile_version'])

    def _runtime_control_reason(self, db, row):
        if row['state'] == 'finished':
            return 'finished'
        if row['stop_requested']:
            return 'stop_requested'
        if row['state'] == 'needs_review':
            return 'cleanup_unconfirmed'
        actor = {key: row[key] for key in ('owner_id', 'device_id', 'session_id')} | {'kind': 'agent'}
        # This read exposes only a bounded stop instruction, never permission or
        # project details that may have become private since the launch.
        from board import BoardError
        try:
            self._runtime_authorize_row(db, actor, row)
        except BoardError:
            return 'authorization_changed'
        if self._runtime_limits(db, row['owner_id'])['paused']:
            return 'owner_paused'
        return ''

    def _execution_view(self, db, row):
        fields = ('id', 'owner_id', 'agent_id', 'profile_version', 'device_id', 'session_id',
                  'project_id', 'ticket_id', 'work_id', 'turn_id', 'provider', 'model',
                  'state', 'revision', 'outcome', 'reason', 'created_at', 'updated_at',
                  'started_at', 'last_heartbeat_at', 'finished_at')
        result = {key: row[key] for key in fields}
        if row['scope_kind'] == 'message':
            result.update({key:row[key] for key in ('scope_kind','thread_id','message_id','source_sha256')})
        reason = self._runtime_control_reason(db, row)
        result.update(stop_requested=bool(row['stop_requested']), should_stop=bool(reason),
            control_reason=reason, cleanup_confirmed=bool(row['cleanup_confirmed']),
            stale=bool(row['state'] != 'finished' and time.time() - (row['last_heartbeat_at'] or row['created_at']) > STALE_AFTER_SECONDS))
        return result

    def reserve_execution(self, actor, idempotency_key, project_id, ticket_id, work_id, turn_id, provider, model):
        values = {key: _identifier(value, key, model=key in {'model', 'provider'})
            for key, value in locals().copy().items() if key in {
                'idempotency_key', 'project_id', 'ticket_id', 'work_id', 'turn_id', 'provider', 'model'}}
        with self._tx() as db:
            actor, profile = self._runtime_authorize(db, actor, project_id, ticket_id)
            payload = _json(values | {'agent_id': profile['id'], 'profile_version': profile['version']})
            existing = db.execute('''SELECT * FROM runtime_executions
                WHERE owner_id=? AND device_id=? AND session_id=? AND idempotency_key=?''',
                (actor['owner_id'], actor['device_id'], actor['session_id'], idempotency_key)).fetchone()
            if existing:
                if existing['payload'] != payload:
                    _fail('execution_key_conflict', 409)
                return {'reserved': True, 'execution': self._execution_view(db, existing), 'retried': True}
            if db.execute('SELECT 1 FROM runtime_executions WHERE owner_id=? AND work_id=? AND turn_id=?',
                          (actor['owner_id'], work_id, turn_id)).fetchone():
                _fail('execution_turn_conflict', 409)
            limits = self._runtime_limits(db, actor['owner_id'])
            if limits['paused']:
                _fail('owner_paused', 409)
            count = db.execute("SELECT COUNT(*) FROM runtime_executions WHERE owner_id=? AND state!='finished'",
                               (actor['owner_id'],)).fetchone()[0]
            if count >= limits['max_concurrent']:
                _fail('capacity_exhausted', 409)
            identifier, now = secrets.token_hex(16), time.time()
            db.execute('''INSERT INTO runtime_executions(
                id,owner_id,agent_id,profile_version,device_id,session_id,project_id,ticket_id,
                work_id,turn_id,provider,model,idempotency_key,payload,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)''',
                (identifier, actor['owner_id'], profile['id'], profile['version'], actor['device_id'],
                 actor['session_id'], project_id, ticket_id, work_id, turn_id, provider, model,
                 idempotency_key, payload, now, now))
            self._audit(db, actor, 'execution_reserved', {'execution_id': identifier})
            row = db.execute('SELECT * FROM runtime_executions WHERE id=?', (identifier,)).fetchone()
            return {'reserved': True, 'execution': self._execution_view(db, row), 'retried': False}

    def reserve_message_execution(self, actor, idempotency_key, thread_id, message_id,
                                  source_sha256, work_id, turn_id, provider, model):
        values = {key:_identifier(value,key,model=key in {'provider','model'})
            for key,value in locals().copy().items() if key in {
                'idempotency_key','thread_id','message_id','work_id','turn_id','provider','model'}}
        if type(source_sha256) is not str or not _SHA256.fullmatch(source_sha256):
            _fail('invalid_source_sha256')
        with self._tx() as db:
            actor,profile,linked = self._runtime_message_authorize(db,actor,thread_id,message_id,source_sha256)
            payload = _json(values | linked | {'scope_kind':'message','source_sha256':source_sha256,
                'agent_id':profile['id'],'profile_version':profile['version']})
            existing = db.execute('''SELECT * FROM runtime_executions WHERE owner_id=?
                AND device_id=? AND session_id=? AND idempotency_key=?''',
                (actor['owner_id'],actor['device_id'],actor['session_id'],idempotency_key)).fetchone()
            if existing:
                if existing['payload'] != payload:
                    _fail('execution_key_conflict',409)
                return {'reserved':True,'execution':self._execution_view(db,existing),'retried':True}
            if db.execute('SELECT 1 FROM runtime_executions WHERE owner_id=? AND work_id=? AND turn_id=?',
                          (actor['owner_id'],work_id,turn_id)).fetchone():
                _fail('execution_turn_conflict',409)
            limits = self._runtime_limits(db,actor['owner_id'])
            if limits['paused']:
                _fail('owner_paused',409)
            count = db.execute("SELECT COUNT(*) FROM runtime_executions WHERE owner_id=? AND state!='finished'",
                               (actor['owner_id'],)).fetchone()[0]
            if count >= limits['max_concurrent']:
                _fail('capacity_exhausted',409)
            identifier,now = secrets.token_hex(16),time.time()
            db.execute('''INSERT INTO runtime_executions(
                id,owner_id,agent_id,profile_version,device_id,session_id,project_id,ticket_id,
                work_id,turn_id,provider,model,idempotency_key,payload,state,created_at,updated_at,
                scope_kind,thread_id,message_id,source_sha256)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?,'message',?,?,?)''',
                (identifier,actor['owner_id'],profile['id'],profile['version'],actor['device_id'],
                 actor['session_id'],linked['project_id'],linked['ticket_id'],work_id,turn_id,provider,model,
                 idempotency_key,payload,now,now,thread_id,message_id,source_sha256))
            self._audit(db,actor,'message_execution_reserved',{'execution_id':identifier})
            row = db.execute('SELECT * FROM runtime_executions WHERE id=?',(identifier,)).fetchone()
            return {'reserved':True,'execution':self._execution_view(db,row),'retried':False}

    def lookup_execution(self, actor, idempotency_key):
        _identifier(idempotency_key, 'idempotency_key')
        with self._connection() as db:
            actor, session = self._runtime_identity(db, actor)
            row = db.execute('''SELECT * FROM runtime_executions WHERE owner_id=? AND device_id=?
                AND session_id=? AND agent_id=? AND idempotency_key=?''',
                (actor['owner_id'], actor['device_id'], actor['session_id'], session['agent_id'], idempotency_key)).fetchone()
            return {'found': True, 'execution': self._execution_view(db, row)} if row else {'found': False}

    def get_execution_control(self, actor, execution_id):
        with self._connection() as db:
            _, row = self._runtime_execution(db, actor, execution_id)
            return self._execution_view(db, row)

    def start_execution(self, actor, execution_id, expected_revision):
        _version(expected_revision)
        with self._tx() as db:
            actor, row = self._runtime_execution(db, actor, execution_id)
            self._runtime_authorize_row(db, actor, row)
            if self._runtime_limits(db, row['owner_id'])['paused']:
                _fail('owner_paused', 409)
            if row['stop_requested']:
                _fail('stop_requested', 409)
            count = db.execute("SELECT COUNT(*) FROM runtime_executions WHERE owner_id=? AND state!='finished'",
                               (row['owner_id'],)).fetchone()[0]
            if count > self._runtime_limits(db, row['owner_id'])['max_concurrent']:
                _fail('capacity_exhausted', 409)
            if row['state'] == 'running' and row['start_revision'] == expected_revision:
                return self._execution_view(db, row)
            if row['revision'] != expected_revision or row['state'] != 'reserved':
                _fail('execution_revision_conflict', 409)
            now = time.time()
            db.execute("UPDATE runtime_executions SET state='running',revision=revision+1,start_revision=?,started_at=?,last_heartbeat_at=?,updated_at=? WHERE id=?",
                       (expected_revision, now, now, now, execution_id))
            self._audit(db, actor, 'execution_started', {'execution_id': execution_id})
            return self._execution_view(db, db.execute('SELECT * FROM runtime_executions WHERE id=?', (execution_id,)).fetchone())

    def heartbeat_execution(self, actor, execution_id, expected_revision):
        _version(expected_revision)
        with self._tx() as db:
            actor, row = self._runtime_execution(db, actor, execution_id)
            if row['state'] != 'running':
                _fail('execution_not_running', 409)
            if row['heartbeat_revision'] == expected_revision:
                return self._execution_view(db, row)
            if row['revision'] != expected_revision:
                _fail('execution_revision_conflict', 409)
            now = time.time()
            db.execute('UPDATE runtime_executions SET revision=revision+1,heartbeat_revision=?,last_heartbeat_at=?,updated_at=? WHERE id=?',
                       (expected_revision, now, now, execution_id))
            return self._execution_view(db, db.execute('SELECT * FROM runtime_executions WHERE id=?', (execution_id,)).fetchone())

    def finish_execution(self, actor, execution_id, expected_revision, status, cleanup_confirmed, reason=''):
        _version(expected_revision)
        _bool(cleanup_confirmed, 'cleanup_confirmed')
        if type(status) is not str or status not in OUTCOMES:
            _fail('invalid_status')
        if type(reason) is not str or reason not in REASONS:
            _fail('invalid_reason')
        with self._tx() as db:
            actor, row = self._runtime_execution(db, actor, execution_id)
            if (row['finish_revision'] == expected_revision and row['outcome'] == status
                    and bool(row['cleanup_confirmed']) == cleanup_confirmed and row['reason'] == reason):
                return self._execution_view(db, row)
            if row['revision'] != expected_revision or row['state'] == 'finished':
                _fail('execution_revision_conflict', 409)
            if row['state'] == 'needs_review' and row['outcome'] != status:
                _fail('execution_outcome_conflict', 409)
            if status == 'completed' and row['started_at'] is None:
                _fail('execution_not_started', 409)
            now = time.time()
            db.execute('''UPDATE runtime_executions SET state=?,revision=revision+1,finish_revision=?,
                outcome=?,cleanup_confirmed=?,reason=?,updated_at=?,finished_at=? WHERE id=?''',
                ('finished' if cleanup_confirmed else 'needs_review', expected_revision, status,
                 int(cleanup_confirmed), reason, now, now if cleanup_confirmed else None, execution_id))
            # One provider observation per turn. A later cleanup confirmation
            # is not another provider failure and must not undo human resume.
            if row['finish_revision'] is None and reason in {'quota_or_rate_limit', 'authentication_failed'}:
                limits = self._runtime_limits(db, row['owner_id'])
                observed = db.execute('SELECT blocked_reason FROM owner_execution_limits WHERE owner_id=?', (row['owner_id'],)).fetchone()
                if not limits['paused'] or not observed or observed['blocked_reason'] != reason:
                    db.execute('''INSERT INTO owner_execution_limits(owner_id,max_concurrent,paused,version,blocked_reason)
                        VALUES(?,?,1,?,?) ON CONFLICT(owner_id) DO UPDATE SET paused=1,version=excluded.version,blocked_reason=excluded.blocked_reason''',
                        (row['owner_id'], limits['max_concurrent'], limits['version'] + 1, reason))
                    self._audit(db, actor, 'runtime_provider_block_observed', {'reason': reason})
            self._audit(db, actor, 'execution_finished' if cleanup_confirmed else 'execution_needs_review',
                        {'execution_id': execution_id, 'outcome': status, 'reason': reason})
            return self._execution_view(db, db.execute('SELECT * FROM runtime_executions WHERE id=?', (execution_id,)).fetchone())

    def get_runtime(self, actor):
        with self._connection() as db:
            actor = self._human(db, actor)
            owner_id = actor['owner_id']
            active = db.execute("SELECT * FROM runtime_executions WHERE owner_id=? AND state!='finished' ORDER BY created_at,id", (owner_id,)).fetchall()
            recent = db.execute("SELECT * FROM runtime_executions WHERE owner_id=? AND state='finished' ORDER BY updated_at DESC,id LIMIT 50", (owner_id,)).fetchall()
            observed = db.execute('SELECT blocked_reason FROM owner_execution_limits WHERE owner_id=?', (owner_id,)).fetchone()
            usage = self._usage_for_owner(db, owner_id)
            usage_status = 'observed' if any(v['data_status'] == 'observed' for v in usage) else 'stale' if any(v['data_status'] == 'stale' for v in usage) else 'unavailable'
            quota = {'status': 'blocked_observed', 'reason': observed['blocked_reason']} if observed and observed['blocked_reason'] else {'status': usage_status}
            return {'limits': self._runtime_limits(db, owner_id), 'active_count': len(active),
                    'quota': quota, 'usage_observations': usage,
                    'executions': [self._execution_view(db, row) for row in active + recent]}

    def update_runtime_limits(self, actor, expected_version, max_concurrent, paused):
        _version(expected_version)
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 8:
            _fail('invalid_max_concurrent')
        _bool(paused, 'paused')
        with self._tx() as db:
            actor = self._human(db, actor)
            current = self._runtime_limits(db, actor['owner_id'])
            if current['version'] != expected_version:
                _fail('runtime_limits_version_conflict', 409)
            db.execute('''INSERT INTO owner_execution_limits(owner_id,max_concurrent,paused,version) VALUES(?,?,?,?)
                ON CONFLICT(owner_id) DO UPDATE SET max_concurrent=excluded.max_concurrent,paused=excluded.paused,version=excluded.version,
                    blocked_reason=CASE WHEN excluded.paused=0 THEN '' ELSE owner_execution_limits.blocked_reason END''',
                (actor['owner_id'], max_concurrent, int(paused), expected_version + 1))
            self._audit(db, actor, 'runtime_limits_changed', {'version': expected_version + 1, 'max_concurrent': max_concurrent, 'paused': paused})
            return self._runtime_limits(db, actor['owner_id'])

    def stop_execution(self, actor, execution_id, expected_revision):
        _identifier(execution_id, 'execution_id')
        _version(expected_revision)
        with self._tx() as db:
            actor = self._human(db, actor)
            row = db.execute('SELECT * FROM runtime_executions WHERE id=? AND owner_id=?',
                             (execution_id, actor['owner_id'])).fetchone()
            if not row:
                _fail('execution_not_found', 404)
            if row['revision'] != expected_revision or row['state'] == 'finished':
                _fail('execution_revision_conflict', 409)
            if not row['stop_requested']:
                db.execute('UPDATE runtime_executions SET stop_requested=1,revision=revision+1,updated_at=? WHERE id=?',
                           (time.time(), execution_id))
                self._audit(db, actor, 'execution_stop_requested', {'execution_id': execution_id})
            return self._execution_view(db, db.execute('SELECT * FROM runtime_executions WHERE id=?', (execution_id,)).fetchone())
