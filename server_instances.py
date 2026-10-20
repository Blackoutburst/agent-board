"""Durable owner-controlled host commands; observations never release Runtime.

The explicitly granted device role is independent of work/profile permissions so
an already-owned controller can stop and report cleanup after those are removed.
A revoked device has no channel. No process, path, credential or model is handled.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from projects import _fail

MODES = frozenset({'messages', 'ticket_text'})
STATES = frozenset({'unknown', 'stopped', 'starting', 'ready', 'busy', 'stopping', 'needs_review'})
REASONS = frozenset({'', 'stopped', 'not_started', 'launch_refused', 'configuration_changed',
    'authorization_changed', 'host_revoked', 'cleanup_unconfirmed', 'connection_lost',
    'budget_exhausted', 'execution_failed', 'authentication_required', 'quota_unavailable'})
MANAGER_TOOLS = frozenset({'publish_server_catalog', 'poll_instance_commands',
    'ack_instance_command', 'report_instance_state'})


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _bound(value, maximum=4096):
    try:
        # Validate Unicode independently: ASCII JSON otherwise accepts surrogates.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(_json(value).encode('ascii')) > maximum:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail('invalid_server_payload')
    return value


def _opaque(value):
    if (type(value) is not str or not 1 <= len(value) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        _fail('invalid_server_identifier')
    try:
        if len(value.encode('utf-8')) > 800:
            raise ValueError
    except (UnicodeError, ValueError):
        _fail('invalid_server_identifier')
    return value


def _number(value, minimum=1, maximum=2**63-1):
    if type(value) is not int or not minimum <= value <= maximum:
        _fail('invalid_server_number')
    return value


def _sha(value):
    if type(value) is not str or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
        _fail('invalid_server_hash')
    return value


def _wire(value):
    return _bound(value, 65536)


def validate_manager_arguments(name, arguments):
    """Run before FastMCP can discard keys or coerce JSON strings/scalars."""
    fields = {
        'publish_server_catalog': {'host_id', 'expected_catalog_version', 'entries', 'idempotency_key'},
        'poll_instance_commands': {'host_id', 'limit'},
        'ack_instance_command': {'host_id', 'command_id', 'expected_revision', 'idempotency_key'},
        'report_instance_state': {'host_id', 'instance_id', 'expected_observation_revision', 'generation',
            'launchspec_sha256', 'command_id', 'state', 'cleanup_confirmed', 'reason', 'idempotency_key'},
    }
    required = fields[name] - ({'limit'} if name == 'poll_instance_commands' else set())
    if type(arguments) is not dict or not required <= arguments.keys() or arguments.keys() - fields[name]:
        _fail('invalid_server_payload')
    _bound(arguments, 16384 if name == 'publish_server_catalog' else 4096)
    for key in ('host_id', 'instance_id', 'command_id', 'idempotency_key'):
        if key in arguments and not (key == 'command_id' and arguments[key] is None):
            _opaque(arguments[key])
    for key in ('expected_catalog_version', 'expected_observation_revision', 'generation'):
        if key in arguments:
            _number(arguments[key], 0)
    if 'expected_revision' in arguments:
        _number(arguments['expected_revision'])
    if 'limit' in arguments:
        _number(arguments['limit'], 1, 20)
    if 'cleanup_confirmed' in arguments and type(arguments['cleanup_confirmed']) is not bool:
        _fail('invalid_server_payload')
    if 'launchspec_sha256' in arguments:
        _sha(arguments['launchspec_sha256'])
    if 'state' in arguments and (type(arguments['state']) is not str or arguments['state'] not in STATES):
        _fail('invalid_server_payload')
    if 'reason' in arguments and (type(arguments['reason']) is not str or arguments['reason'] not in REASONS):
        _fail('invalid_server_payload')
    if 'entries' in arguments:
        validate_entries(arguments['entries'])


def validate_entries(entries):
    fields = {'catalog_key', 'profile_id', 'profile_version', 'launchspec_sha256', 'mode'}
    if type(entries) is not list or len(entries) > 16:
        _fail('invalid_server_catalog')
    keys, profiles = set(), set()
    for entry in entries:
        if type(entry) is not dict or entry.keys() != fields:
            _fail('invalid_server_catalog')
        _opaque(entry['catalog_key']); _opaque(entry['profile_id'])
        _number(entry['profile_version']); _sha(entry['launchspec_sha256'])
        if type(entry['mode']) is not str or entry['mode'] not in MODES:
            _fail('invalid_server_catalog')
        if entry['catalog_key'] in keys or entry['profile_id'] in profiles:
            _fail('invalid_server_catalog')
        keys.add(entry['catalog_key']); profiles.add(entry['profile_id'])


def migrate_server_instances(db):
    for sql in (
        '''CREATE TABLE server_hosts(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
            device_id TEXT NOT NULL REFERENCES devices(id), name TEXT NOT NULL, version INTEGER NOT NULL,
            catalog_version INTEGER NOT NULL, revoked_at REAL, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, last_seen_at REAL)''',
        'CREATE UNIQUE INDEX server_host_active_owner ON server_hosts(owner_id) WHERE revoked_at IS NULL',
        '''CREATE TABLE server_instances(id TEXT PRIMARY KEY, host_id TEXT NOT NULL REFERENCES server_hosts(id),
            catalog_key TEXT NOT NULL, profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
            profile_version INTEGER NOT NULL, launchspec_sha256 TEXT NOT NULL, mode TEXT NOT NULL,
            active INTEGER NOT NULL, version INTEGER NOT NULL, generation INTEGER NOT NULL,
            desired_state TEXT NOT NULL, observed_state TEXT NOT NULL, observation_revision INTEGER NOT NULL,
            cleanup_confirmed INTEGER NOT NULL, reason TEXT NOT NULL, last_seen_at REAL, command_id TEXT,
            UNIQUE(host_id,catalog_key))''',
        '''CREATE TABLE server_instance_commands(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            instance_id TEXT NOT NULL REFERENCES server_instances(id), action TEXT NOT NULL,
            generation INTEGER NOT NULL, launchspec_sha256 TEXT NOT NULL, budget INTEGER NOT NULL, instance_version INTEGER NOT NULL,
            revision INTEGER NOT NULL, status TEXT NOT NULL, stop_confirmed INTEGER NOT NULL, retired INTEGER NOT NULL,
            created_at REAL NOT NULL, updated_at REAL NOT NULL)''',
        'CREATE INDEX server_commands_instance ON server_instance_commands(instance_id,seq)',
        '''CREATE TABLE server_control_receipts(owner_id TEXT NOT NULL, operation TEXT NOT NULL,
            key TEXT NOT NULL, payload_sha256 TEXT NOT NULL, response TEXT NOT NULL,
            PRIMARY KEY(owner_id,operation,key))''',
    ):
        db.execute(sql)


class ServerInstancesMixin:
    def _server_host(self, db, actor, host_id):
        row = db.execute('SELECT * FROM server_hosts WHERE id=? AND owner_id=?',
            (host_id, actor['owner_id'])).fetchone()
        if row is None:
            _fail('server_host_not_found', 404)
        return row

    def _server_manager(self, db, actor, host_id):
        # Deliberately not _actor(): it rejects work permission withdrawal and
        # requires a model session. This narrower role grants no work authority.
        if type(actor) is not dict or actor.get('kind') != 'agent':
            _fail('server_manager_required', 403)
        device = db.execute('SELECT * FROM devices WHERE id=? AND owner_id=? AND revoked_at IS NULL',
            (actor.get('device_id'), actor.get('owner_id'))).fetchone()
        if device is None:
            _fail('server_manager_unavailable', 401)
        host = self._server_host(db, actor, host_id)
        if host['device_id'] != device['id']:
            _fail('server_host_not_found', 404)
        return host

    def _server_retry(self, db, owner, operation, key, payload):
        _opaque(key)
        row = db.execute('SELECT * FROM server_control_receipts WHERE owner_id=? AND operation=? AND key=?',
            (owner, operation, key)).fetchone()
        hashed = hashlib.sha256(_json(payload).encode('ascii')).hexdigest()
        if row is not None:
            if row['payload_sha256'] != hashed:
                _fail('server_idempotency_conflict', 409)
            return json.loads(row['response'])
        return None

    def _server_remember(self, db, owner, operation, key, payload, response):
        _wire(response)
        db.execute('INSERT INTO server_control_receipts VALUES(?,?,?,?,?)',
            (owner, operation, key, hashlib.sha256(_json(payload).encode('ascii')).hexdigest(), _json(response)))
        return response

    def _server_owner_enabled(self, db, owner_id):
        return json.loads(self._owner(db, owner_id)['policy']).get('enabled') is True

    def _server_start_reason(self, db, host, instance):
        if host['revoked_at'] is not None:
            return 'host_revoked'
        if not instance['active']:
            return 'configuration_changed'
        if not db.execute('SELECT 1 FROM devices WHERE id=? AND owner_id=? AND revoked_at IS NULL',
                (host['device_id'], host['owner_id'])).fetchone():
            return 'authorization_changed'
        if self._runtime_limits(db, host['owner_id'])['paused']:
            return 'authorization_changed'
        policy = json.loads(self._owner(db, host['owner_id'])['policy'])
        required = ('enabled', 'allow_read', 'allow_reply') + (('allow_requests',) if instance['mode'] == 'ticket_text' else ())
        if any(policy.get(key) is not True for key in required):
            return 'authorization_changed'
        profile = db.execute('SELECT * FROM agent_profiles WHERE id=? AND owner_id=?',
            (instance['profile_id'], host['owner_id'])).fetchone()
        if profile is None or profile['status'] != 'active' or profile['version'] != instance['profile_version']:
            return 'configuration_changed'
        if db.execute('SELECT 1 FROM agent_profile_bindings WHERE profile_id=? AND device_id=?',
                (profile['id'], host['device_id'])).fetchone() is None:
            return 'authorization_changed'
        policy = json.loads(profile['policy'])
        if (policy.get('allow_read') is not True or policy.get('allow_messages') is not True
                or (instance['mode'] == 'ticket_text' and (policy.get('allow_requests') is not True
                    or policy.get('autonomy') == 'responses'))):
            return 'authorization_changed'
        return ''

    def _server_instance_view(self, db, host, row):
        value = {key: row[key] for key in ('id', 'host_id', 'catalog_key', 'profile_id', 'profile_version',
            'launchspec_sha256', 'mode', 'version', 'generation', 'desired_state', 'observed_state',
            'observation_revision', 'reason', 'last_seen_at', 'command_id')}
        reason = self._server_start_reason(db, host, row)
        value.update(active=bool(row['active']), cleanup_confirmed=bool(row['cleanup_confirmed']),
            can_start=not bool(reason), control_reason=reason)
        command = None if row['command_id'] is None else db.execute(
            'SELECT * FROM server_instance_commands WHERE id=?', (row['command_id'],)).fetchone()
        value['current_command'] = None if command is None else self._server_command_view(command)
        return value

    def _server_host_view(self, db, row, *, instances=False):
        value = {key: row[key] for key in ('id', 'owner_id', 'device_id', 'name', 'version',
            'catalog_version', 'revoked_at', 'created_at', 'updated_at', 'last_seen_at')}
        value['enabled'] = bool(row['revoked_at'] is None and self._server_owner_enabled(db, row['owner_id'])
            and db.execute('SELECT 1 FROM devices WHERE id=? AND revoked_at IS NULL', (row['device_id'],)).fetchone())
        if instances:
            value['instances'] = [self._server_instance_view(db, row, i) for i in db.execute(
                'SELECT * FROM server_instances WHERE host_id=? AND active=1 ORDER BY catalog_key LIMIT 16', (row['id'],))]
        return _wire(value)

    @staticmethod
    def _server_command_view(row):
        return {key: row[key] for key in ('id', 'seq', 'instance_id', 'action', 'generation',
            'launchspec_sha256', 'budget', 'instance_version', 'revision', 'status', 'created_at', 'updated_at')} | {
                'stop_confirmed': bool(row['stop_confirmed'])}

    def list_server_hosts(self, actor):
        with self._connection() as db:
            actor = self._human(db, actor)
            return _wire({'hosts': [self._server_host_view(db, row) for row in db.execute(
                'SELECT * FROM server_hosts WHERE owner_id=? ORDER BY created_at LIMIT 16', (actor['owner_id'],))]})

    def get_server_host(self, actor, host_id):
        _opaque(host_id)
        with self._connection() as db:
            actor = self._human(db, actor)
            return self._server_host_view(db, self._server_host(db, actor, host_id), instances=True)

    def create_server_host(self, actor, device_id, name, idempotency_key):
        payload = _bound(dict(device_id=_opaque(device_id), name=name))
        if type(name) is not str or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
            _fail('invalid_server_name')
        with self._tx() as db:
            actor = self._human(db, actor)
            prior = self._server_retry(db, actor['owner_id'], 'create_host', idempotency_key, payload)
            if prior is not None:
                return prior
            if not db.execute('SELECT 1 FROM devices WHERE id=? AND owner_id=? AND revoked_at IS NULL',
                    (device_id, actor['owner_id'])).fetchone():
                _fail('server_device_not_found', 404)
            if db.execute('SELECT 1 FROM server_hosts WHERE owner_id=? AND revoked_at IS NULL', (actor['owner_id'],)).fetchone():
                _fail('server_owner_host_exists', 409)
            if db.execute('''SELECT 1 FROM server_instances i JOIN server_hosts h ON h.id=i.host_id
                    LEFT JOIN server_instance_commands c ON c.id=i.command_id WHERE h.owner_id=?
                    AND (i.observed_state!='stopped' OR i.cleanup_confirmed!=1 OR c.status IN ('queued','accepted')) LIMIT 1''',
                    (actor['owner_id'],)).fetchone():
                _fail('server_previous_host_not_settled', 409)
            if db.execute('SELECT COUNT(*) FROM server_hosts WHERE owner_id=?', (actor['owner_id'],)).fetchone()[0] >= 16:
                _fail('server_host_limit', 409)
            identifier, now = secrets.token_hex(16), time.time()
            db.execute('INSERT INTO server_hosts VALUES(?,?,?,?,1,0,NULL,?,?,NULL)',
                (identifier, actor['owner_id'], device_id, name, now, now))
            self._audit(db, actor, 'server_host_granted', {'host_id': identifier, 'device_id': device_id})
            return self._server_remember(db, actor['owner_id'], 'create_host', idempotency_key, payload,
                self._server_host_view(db, self._server_host(db, actor, identifier), instances=True))

    def publish_server_catalog(self, actor, host_id, expected_catalog_version, entries, idempotency_key):
        args = dict(host_id=host_id, expected_catalog_version=expected_catalog_version, entries=entries,
            idempotency_key=idempotency_key)
        validate_manager_arguments('publish_server_catalog', args)
        payload = {key: value for key, value in args.items() if key != 'idempotency_key'}
        with self._tx() as db:
            host = self._server_manager(db, actor, host_id)
            if host['revoked_at'] is not None or not self._server_owner_enabled(db, host['owner_id']):
                _fail('server_catalog_not_authorized', 403)
            prior = self._server_retry(db, host['owner_id'], 'catalog:' + host_id, idempotency_key, payload)
            if prior is not None:
                return prior
            if host['catalog_version'] != expected_catalog_version:
                _fail('server_catalog_changed', 409)
            incoming = {entry['catalog_key']: entry for entry in entries}
            placeholders = ','.join('?' for _ in incoming) or 'NULL'
            # At most sixteen active + sixteen explicitly selected historical
            # rows. Preserve history without scanning/materializing all of it.
            current = {row['catalog_key']: row for row in db.execute(
                f'SELECT * FROM server_instances WHERE host_id=? AND (active=1 OR catalog_key IN ({placeholders}))',
                (host_id, *incoming))}
            for key, row in current.items():
                changed = key not in incoming or any(row[field] != incoming[key][field]
                    for field in ('profile_version', 'launchspec_sha256', 'profile_id', 'mode')) or not row['active']
                if changed:
                    if row['command_id'] and db.execute('SELECT status FROM server_instance_commands WHERE id=?',
                            (row['command_id'],)).fetchone()['status'] in ('queued', 'accepted'):
                        _fail('server_instance_not_settled', 409)
                    if row['observed_state'] != 'stopped' or not row['cleanup_confirmed']:
                        _fail('server_instance_not_settled', 409)
                    if key in incoming and any(row[field] != incoming[key][field] for field in ('profile_id', 'mode')):
                        _fail('server_catalog_identity_changed', 409)
            for entry in entries:
                candidate = {**entry, 'active': 1}
                if self._server_start_reason(db, host, candidate):
                    _fail('server_catalog_not_authorized', 403)
                old = current.get(entry['catalog_key'])
                if old:
                    if any(old[field] != entry[field] for field in ('profile_version', 'launchspec_sha256')) or not old['active']:
                        db.execute('''UPDATE server_instances SET profile_version=?,launchspec_sha256=?,active=1,
                            version=version+1,observation_revision=observation_revision+1,
                            observed_state='unknown',cleanup_confirmed=0,reason='configuration_changed' WHERE id=?''',
                            (entry['profile_version'], entry['launchspec_sha256'], old['id']))
                else:
                    db.execute('''INSERT INTO server_instances VALUES(?,?,?,?,?,?,?,1,1,0,'stopped','unknown',0,0,'',NULL,NULL)''',
                        (secrets.token_hex(16), host_id, entry['catalog_key'], entry['profile_id'], entry['profile_version'],
                            entry['launchspec_sha256'], entry['mode']))
            for key, old in current.items():
                if key not in incoming and old['active']:
                    db.execute('UPDATE server_instances SET active=0,version=version+1 WHERE id=?', (old['id'],))
            now = time.time()
            db.execute('UPDATE server_hosts SET catalog_version=catalog_version+1,last_seen_at=?,updated_at=? WHERE id=?', (now, now, host_id))
            host = self._server_host(db, actor, host_id)
            self._audit(db, actor, 'server_catalog_published', {'host_id': host_id, 'catalog_version': host['catalog_version'], 'count': len(entries)})
            result = {'host_id': host_id, 'catalog_version': host['catalog_version'],
                'instances': self._server_host_view(db, host, instances=True)['instances']}
            return self._server_remember(db, host['owner_id'], 'catalog:' + host_id, idempotency_key, payload, result)

    def _server_instance(self, db, actor, instance_id):
        row = db.execute('''SELECT i.* FROM server_instances i JOIN server_hosts h ON h.id=i.host_id
            WHERE i.id=? AND h.owner_id=?''', (instance_id, actor['owner_id'])).fetchone()
        if row is None:
            _fail('server_instance_not_found', 404)
        return row

    def _server_queue(self, db, row, action, budget):
        if row['command_id']:
            db.execute("UPDATE server_instance_commands SET status='superseded',revision=revision+1,updated_at=? WHERE id=? AND status IN ('queued','accepted')", (time.time(), row['command_id']))
        generation = row['generation'] + (action != 'stop')
        _number(generation, 0)
        identifier, now = secrets.token_hex(16), time.time()
        db.execute("INSERT INTO server_instance_commands(id,instance_id,action,generation,launchspec_sha256,budget,instance_version,revision,status,stop_confirmed,retired,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,'queued',0,0,?,?)",
            (identifier, row['id'], action, generation, row['launchspec_sha256'], budget, row['version'] + 1, now, now))
        db.execute('UPDATE server_instances SET version=version+1,generation=?,desired_state=?,command_id=? WHERE id=?',
            (generation, 'stopped' if action == 'stop' else 'running', identifier, row['id']))
        return self._server_command_view(db.execute('SELECT * FROM server_instance_commands WHERE id=?', (identifier,)).fetchone())

    def command_server_instance(self, actor, instance_id, action, expected_version, expected_generation,
                                launchspec_sha256, budget, idempotency_key):
        payload = _bound(dict(instance_id=_opaque(instance_id), action=action, expected_version=_number(expected_version),
            expected_generation=_number(expected_generation, 0), launchspec_sha256=_sha(launchspec_sha256), budget=budget))
        if type(action) is not str or action not in ('start', 'stop', 'restart'):
            _fail('invalid_server_action')
        _number(budget, 0 if action == 'stop' else 1, 0 if action == 'stop' else 20)
        with self._tx() as db:
            actor = self._human(db, actor)
            row = self._server_instance(db, actor, instance_id)
            host = self._server_host(db, actor, row['host_id'])
            prior = self._server_retry(db, actor['owner_id'], 'command', idempotency_key, payload)
            if prior is not None:
                return prior
            if (row['version'] != expected_version or row['generation'] != expected_generation
                    or row['launchspec_sha256'] != launchspec_sha256):
                _fail('server_instance_changed', 409)
            if action != 'stop':
                if self._server_start_reason(db, host, row):
                    _fail('server_start_not_authorized', 403)
                old = db.execute('SELECT status FROM server_instance_commands WHERE id=?', (row['command_id'],)).fetchone()
                if (old and old['status'] in ('queued', 'accepted')) or row['observed_state'] in ('unknown', 'needs_review'):
                    _fail('server_instance_not_settled', 409)
                if action == 'start' and (row['observed_state'] != 'stopped' or not row['cleanup_confirmed']):
                    _fail('server_instance_not_stopped', 409)
            result = self._server_queue(db, row, action, budget)
            self._audit(db, actor, 'server_instance_commanded', {'host_id': host['id'], 'instance_id': instance_id,
                'command_id': result['id'], 'action': action, 'generation': result['generation']})
            return self._server_remember(db, actor['owner_id'], 'command', idempotency_key, payload, result)

    def revoke_server_host(self, actor, host_id, expected_version, idempotency_key):
        payload = _bound(dict(host_id=_opaque(host_id), expected_version=_number(expected_version)))
        with self._tx() as db:
            actor = self._human(db, actor)
            host = self._server_host(db, actor, host_id)
            prior = self._server_retry(db, actor['owner_id'], 'revoke_host', idempotency_key, payload)
            if prior is not None:
                return prior
            if host['version'] != expected_version or host['revoked_at'] is not None:
                _fail('server_host_changed', 409)
            now = time.time()
            db.execute('UPDATE server_hosts SET revoked_at=?,version=version+1,updated_at=? WHERE id=?', (now, now, host_id))
            for row in db.execute('SELECT * FROM server_instances WHERE host_id=? AND active=1', (host_id,)).fetchall():
                self._server_queue(db, row, 'stop', 0)
            self._audit(db, actor, 'server_host_revoked', {'host_id': host_id})
            result = self._server_host_view(db, self._server_host(db, actor, host_id), instances=True)
            return self._server_remember(db, actor['owner_id'], 'revoke_host', idempotency_key, payload, result)

    def poll_instance_commands(self, actor, host_id, limit=20):
        validate_manager_arguments('poll_instance_commands', dict(host_id=host_id, limit=limit))
        with self._tx() as db:
            host = self._server_manager(db, actor, host_id)
            now = time.time()
            db.execute('UPDATE server_hosts SET last_seen_at=? WHERE id=?', (now, host_id))
            host = self._server_host(db, actor, host_id)
            value = self._server_host_view(db, host, instances=True)
            commands = [self._server_command_view(row) for row in db.execute('''SELECT c.* FROM server_instance_commands c
                JOIN server_instances i ON i.command_id=c.id WHERE i.host_id=? AND c.status IN ('queued','accepted')
                ORDER BY c.seq LIMIT ?''', (host_id, limit))]
            return _wire({'host_id': host_id, 'host_version': host['version'], 'enabled': value['enabled'],
                'last_seen_at': now, 'catalog_version': host['catalog_version'], 'instances': value['instances'], 'commands': commands})

    def ack_instance_command(self, actor, host_id, command_id, expected_revision, idempotency_key):
        args = dict(host_id=host_id, command_id=command_id, expected_revision=expected_revision, idempotency_key=idempotency_key)
        validate_manager_arguments('ack_instance_command', args)
        payload = {k: v for k, v in args.items() if k != 'idempotency_key'}
        with self._tx() as db:
            host = self._server_manager(db, actor, host_id)
            cmd = db.execute('SELECT * FROM server_instance_commands WHERE id=?', (command_id,)).fetchone()
            row = None if cmd is None else self._server_instance(db, actor, cmd['instance_id'])
            if row is None or row['host_id'] != host_id or row['command_id'] != command_id:
                _fail('server_command_not_current', 409)
            if cmd['action'] != 'stop' and self._server_start_reason(db, host, row):
                _fail('server_start_not_authorized', 403)
            prior = self._server_retry(db, host['owner_id'], 'ack:' + host_id, idempotency_key, payload)
            if prior is not None:
                return prior
            if cmd['revision'] != expected_revision or cmd['status'] != 'queued':
                _fail('server_command_changed', 409)
            db.execute("UPDATE server_instance_commands SET status='accepted',revision=revision+1,updated_at=? WHERE id=?", (time.time(), command_id))
            result = {'command': self._server_command_view(db.execute('SELECT * FROM server_instance_commands WHERE id=?', (command_id,)).fetchone()),
                'instance': self._server_instance_view(db, host, row)}
            return self._server_remember(db, host['owner_id'], 'ack:' + host_id, idempotency_key, payload, result)

    def report_instance_state(self, actor, host_id, instance_id, expected_observation_revision, generation,
                              launchspec_sha256, command_id, state, cleanup_confirmed, reason, idempotency_key):
        args = dict(host_id=host_id, instance_id=instance_id, expected_observation_revision=expected_observation_revision,
            generation=generation, launchspec_sha256=launchspec_sha256, command_id=command_id,
            state=state, cleanup_confirmed=cleanup_confirmed, reason=reason, idempotency_key=idempotency_key)
        validate_manager_arguments('report_instance_state', args)
        if cleanup_confirmed and state not in ('stopped', 'needs_review'):
            _fail('invalid_server_cleanup')
        payload = {k: v for k, v in args.items() if k != 'idempotency_key'}
        with self._tx() as db:
            host = self._server_manager(db, actor, host_id)
            prior = self._server_retry(db, host['owner_id'], 'report:' + host_id, idempotency_key, payload)
            if prior is not None:
                return prior
            row = self._server_instance(db, actor, instance_id)
            if (row['host_id'] != host_id or row['generation'] != generation
                    or row['launchspec_sha256'] != launchspec_sha256 or row['command_id'] != command_id
                    or row['observation_revision'] != expected_observation_revision):
                _fail('server_observation_changed', 409)
            if command_id is None and generation != 0:
                _fail('server_command_not_current', 409)
            cmd = None if command_id is None else db.execute('SELECT * FROM server_instance_commands WHERE id=?', (command_id,)).fetchone()
            cleanup_only = state in ('unknown', 'stopped', 'stopping', 'needs_review')
            queued_refusal = cmd is not None and cmd['status'] == 'queued' and cmd['action'] != 'stop' and cleanup_only
            if cmd is not None and cmd['status'] not in ('accepted', 'complete', 'rejected') and not queued_refusal:
                _fail('server_command_not_accepted', 409)
            if state in ('starting', 'ready', 'busy'):
                if (cmd is None or cmd['status'] == 'rejected' or cmd['action'] == 'stop' or cmd['retired'] or row['desired_state'] != 'running'
                        or cmd['instance_version'] != row['version'] or cmd['launchspec_sha256'] != row['launchspec_sha256']
                        or self._server_start_reason(db, host, row)):
                    _fail('server_start_not_authorized', 403)
                if cmd['action'] == 'restart' and not cmd['stop_confirmed']:
                    _fail('server_restart_cleanup_required', 409)
            now = time.time()
            db.execute('''UPDATE server_instances SET observed_state=?,cleanup_confirmed=?,reason=?,
                observation_revision=observation_revision+1,last_seen_at=? WHERE id=?''',
                (state, int(cleanup_confirmed), reason, now, instance_id))
            if cmd is not None and (state in ('unknown', 'needs_review')
                    or (cmd['status'] == 'complete' and state in ('stopped', 'stopping'))):
                # A later observation must never resurrect an ended/uncertain
                # activation. A new human command is required after settlement.
                db.execute('UPDATE server_instance_commands SET retired=1 WHERE id=?', (command_id,))
            authorization_refusal = (cmd is not None and cmd['action'] != 'stop' and cleanup_only
                and bool(self._server_start_reason(db, host, row)))
            if queued_refusal or (cmd is not None and cmd['status'] == 'accepted' and authorization_refusal):
                # A withdrawn work permission must not prevent the independent
                # control channel from refusing an unstarted launch. This
                # grants no positive execution authority and never touches Runtime.
                db.execute("UPDATE server_instance_commands SET status='rejected',retired=1,revision=revision+1,updated_at=? WHERE id=?",
                    (now, command_id))
            elif cmd is not None and cmd['status'] == 'accepted':
                if state == 'stopped' and cleanup_confirmed and cmd['action'] == 'restart':
                    db.execute('UPDATE server_instance_commands SET stop_confirmed=1,revision=revision+1,updated_at=? WHERE id=?', (now, command_id))
                elif cmd['action'] == 'start' and state == 'stopped' and cleanup_confirmed:
                    db.execute("UPDATE server_instance_commands SET status='rejected',revision=revision+1,updated_at=? WHERE id=?", (now, command_id))
                elif ((cmd['action'] == 'stop' and state == 'stopped' and cleanup_confirmed)
                        or (cmd['action'] in ('start', 'restart') and state in ('ready', 'busy'))):
                    db.execute("UPDATE server_instance_commands SET status='complete',revision=revision+1,updated_at=? WHERE id=?", (now, command_id))
            db.execute('UPDATE server_hosts SET last_seen_at=? WHERE id=?', (now, host_id))
            row = self._server_instance(db, actor, instance_id)
            result = {'instance': self._server_instance_view(db, host, row),
                'command': None if command_id is None else self._server_command_view(db.execute(
                    'SELECT * FROM server_instance_commands WHERE id=?', (command_id,)).fetchone())}
            return self._server_remember(db, host['owner_id'], 'report:' + host_id, idempotency_key, payload, result)
