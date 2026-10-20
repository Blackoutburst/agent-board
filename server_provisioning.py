"""Human-only metadata requests. No manager, filesystem, login or execution API."""
from __future__ import annotations

import hashlib
import json
import secrets

from projects import _fail, _json as audit_json
from server_instances import _json, _opaque, _number, _sha, _bound, catalog_fence

PURPOSE = 'metadata_request_only_v1'
GRANT_SCHEMA = 'server-provision-draft-grant-v1'
REQUEST_SCHEMA = 'server-provision-draft-request-v1'
CANCEL_SCHEMA = 'server-provision-draft-cancel-v1'
MAX_REQUESTS = 32
SQL = {
    'server_provisioning_grants': '''CREATE TABLE server_provisioning_grants(host_id TEXT PRIMARY KEY REFERENCES server_hosts(id),owner_id TEXT NOT NULL REFERENCES owners(id),device_id TEXT NOT NULL REFERENCES devices(id),purpose TEXT NOT NULL,version INTEGER NOT NULL,enabled INTEGER NOT NULL)''',
    'server_provisioning_requests': '''CREATE TABLE server_provisioning_requests(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),host_id TEXT NOT NULL REFERENCES server_hosts(id),device_id TEXT NOT NULL REFERENCES devices(id),revision INTEGER NOT NULL,state TEXT NOT NULL,request_sha256 TEXT NOT NULL,body TEXT NOT NULL)''',
    'server_provisioning_barriers': '''CREATE TABLE server_provisioning_barriers(host_id TEXT PRIMARY KEY REFERENCES server_hosts(id),owner_id TEXT NOT NULL REFERENCES owners(id),request_id TEXT UNIQUE NOT NULL REFERENCES server_provisioning_requests(id))''',
    'server_provisioning_receipts': '''CREATE TABLE server_provisioning_receipts(owner_id TEXT NOT NULL REFERENCES owners(id),purpose TEXT NOT NULL,operation TEXT NOT NULL,key TEXT NOT NULL,payload_sha256 TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(owner_id,purpose,operation,key))''',
    'server_provisioning_one_requested': "CREATE UNIQUE INDEX server_provisioning_one_requested ON server_provisioning_requests(host_id) WHERE state='requested'",
}
SENSITIVE = tuple(SQL)[:4] + ('server_hosts', 'server_instances', 'server_instance_commands', 'server_control_receipts', 'audit')
REQUEST_KEYS = frozenset('schema expected_host_version expected_catalog_version expected_instances_sha256 grant_version profile_id profile_version profile_sha256 provider_id requested_model idempotency_key'.split())
VIEW_KEYS = frozenset('schema request_id request_sha256 owner_id host_id device_id revision state reason barrier_held reserved_slots profile_id profile_version provider_id requested_model apply_implemented connection_status model_catalog_status model_access_status can_cancel'.split())


def migrate_server_provisioning(db):
    for statement in SQL.values():
        db.execute(statement)


def _hash(value):
    return hashlib.sha256(_json(value).encode('ascii')).hexdigest()


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        _fail('invalid_server_provisioning_payload')
    _bound(value, 8192)


def _model(value):
    if value is None:
        return
    _opaque(value)


def validate_request(value):
    _exact(value, REQUEST_KEYS)
    if value['schema'] != REQUEST_SCHEMA:
        _fail('invalid_server_provisioning_payload')
    for key in ('expected_host_version', 'grant_version', 'profile_version'):
        _number(value[key])
    _number(value['expected_catalog_version'], 1, 2**63-2)
    for key in ('expected_instances_sha256', 'profile_sha256'):
        _sha(value[key])
    for key in ('profile_id', 'provider_id', 'idempotency_key'):
        _opaque(value[key])
    _model(value['requested_model'])


def _grant_view(host, row):
    return _bound(dict(schema='server-provision-draft-grant-view-v1', owner_id=host['owner_id'],
        host_id=host['id'], device_id=host['device_id'], purpose=PURPOSE,
        version=0 if row is None else row['version'], enabled=False if row is None else bool(row['enabled'])), 16384)


def _request_hash(host, body):
    return _hash(dict(purpose=PURPOSE, owner_id=host['owner_id'], host_id=host['id'],
        device_id=host['device_id'], body=body))


def _decode_request(row):
    try:
        if row is None or type(row['body']) is not str or len(row['body'].encode('utf-8')) > 8192:
            raise ValueError
        body = json.loads(row['body'])
        validate_request(body)
        if _json(body) != row['body']:
            raise ValueError
        for key in ('id', 'owner_id', 'host_id', 'device_id'):
            _opaque(row[key])
        if (type(row['revision']) is not int or (row['state'], row['revision']) not in
                (('requested', 1), ('cancelled', 2))):
            raise ValueError
        expected = _request_hash({'owner_id': row['owner_id'], 'id': row['host_id'], 'device_id': row['device_id']}, body)
        if row['request_sha256'] != expected:
            raise ValueError
        return body
    except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
        _fail('server_provisioning_corrupt', 409)


def _request_view(row, body):
    active = row['state'] == 'requested'
    return _bound(dict(schema='server-provision-draft-view-v1', request_id=row['id'],
        request_sha256=row['request_sha256'], owner_id=row['owner_id'], host_id=row['host_id'],
        device_id=row['device_id'], revision=row['revision'], state=row['state'],
        reason='requested' if active else 'cancelled_by_owner', barrier_held=active,
        reserved_slots=int(active), profile_id=body['profile_id'], profile_version=body['profile_version'],
        provider_id=body['provider_id'], requested_model=body['requested_model'],
        apply_implemented=False, connection_status='not_attested', model_catalog_status='not_attested',
        model_access_status='not_attested', can_cancel=active), 16384)


class ServerProvisioningMixin:
    def _provisioning_schema(self, db):
        names = tuple(SQL)[:4]
        if any(row['name'] not in SQL for row in db.execute(
                "SELECT name FROM main.sqlite_master WHERE name GLOB 'server_provisioning_*'")):
            _fail('server_provisioning_schema_invalid', 409)
        marks = ','.join('?' for _ in SENSITIVE)
        for schema in ('main', 'temp'):
            if db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='trigger' AND tbl_name COLLATE NOCASE IN ({marks}) LIMIT 1", SENSITIVE).fetchone():
                _fail('server_provisioning_schema_invalid', 409)
        if (db.execute(f"SELECT 1 FROM temp.sqlite_master WHERE name COLLATE NOCASE IN ({marks}) LIMIT 1", SENSITIVE).fetchone()
                or db.execute(f"SELECT 1 FROM main.sqlite_master WHERE type='view' AND name COLLATE NOCASE IN ({marks}) LIMIT 1", SENSITIVE).fetchone()):
            _fail('server_provisioning_schema_invalid', 409)
        for name, expected in SQL.items():
            row = db.execute('SELECT sql FROM main.sqlite_master WHERE name=?', (name,)).fetchone()
            if row is None or row['sql'] != expected:
                _fail('server_provisioning_schema_invalid', 409)
        for row in db.execute('SELECT name,tbl_name,sql FROM main.sqlite_master WHERE type=\'index\' AND tbl_name IN (?,?,?,?)', names):
            allowed = {f'sqlite_autoindex_{name}_1' for name in names} | {'sqlite_autoindex_server_provisioning_barriers_2'}
            if row['name'] not in allowed and row['name'] != 'server_provisioning_one_requested':
                _fail('server_provisioning_schema_invalid', 409)

    def _provisioning_state(self, db, host):
        self._provisioning_schema(db)
        grant = db.execute('SELECT * FROM server_provisioning_grants WHERE host_id=?', (host['id'],)).fetchone()
        if grant is not None and (grant['owner_id'] != host['owner_id'] or grant['device_id'] != host['device_id']
                or grant['purpose'] != PURPOSE or type(grant['version']) is not int or not 1 <= grant['version'] < 2**63
                or type(grant['enabled']) is not int or grant['enabled'] not in (0, 1)):
            _fail('server_provisioning_corrupt', 409)
        # Bound sizes in SQLite before adopting text, and retain all <=32 records.
        if db.execute('''SELECT 1 FROM server_provisioning_requests WHERE host_id=? AND
                (typeof(body)!='text' OR length(CAST(body AS BLOB))>8192) LIMIT 1''', (host['id'],)).fetchone():
            _fail('server_provisioning_corrupt', 409)
        rows = db.execute('SELECT * FROM server_provisioning_requests WHERE host_id=? ORDER BY rowid LIMIT 33', (host['id'],)).fetchall()
        if len(rows) > MAX_REQUESTS:
            _fail('server_provisioning_corrupt', 409)
        active = []
        for row in rows:
            _decode_request(row)
            if row['owner_id'] != host['owner_id'] or row['device_id'] != host['device_id']:
                _fail('server_provisioning_corrupt', 409)
            if row['state'] == 'requested':
                active.append(row['id'])
        barriers = db.execute('SELECT * FROM server_provisioning_barriers WHERE host_id=? LIMIT 2', (host['id'],)).fetchall()
        if (len(active) > 1 or len(barriers) != len(active) or (barriers and
                (barriers[0]['owner_id'] != host['owner_id'] or barriers[0]['request_id'] != active[0]))):
            _fail('server_provisioning_corrupt', 409)
        return grant, rows, bool(active)

    def _provisioning_has_barrier(self, db, host):
        return self._provisioning_state(db, host)[2]

    def _provisioning_require_no_barrier(self, db, host):
        if self._provisioning_has_barrier(db, host):
            _fail('server_provisioning_pending', 409)

    def _provisioning_host(self, db, actor, host_id, *, live=False):
        actor = self._human(db, actor)
        host = self._server_host(db, actor, _opaque(host_id))
        if live and (host['revoked_at'] is not None or db.execute('''SELECT 1 FROM devices
                WHERE id=? AND owner_id=? AND revoked_at IS NULL''', (host['device_id'], host['owner_id'])).fetchone() is None):
            _fail('server_provisioning_not_authorized', 403)
        self._provisioning_state(db, host)
        return actor, host

    def _provisioning_profile(self, db, host, profile_id):
        row = db.execute('SELECT * FROM agent_profiles WHERE id=? AND owner_id=?', (profile_id, host['owner_id'])).fetchone()
        if (row is None or row['status'] != 'active' or db.execute('''SELECT 1 FROM agent_profile_bindings
                WHERE profile_id=? AND device_id=?''', (profile_id, host['device_id'])).fetchone() is None):
            _fail('server_provisioning_profile_unavailable', 409)
        snap = db.execute('''SELECT length(CAST(snapshot AS BLOB)) AS size FROM agent_profile_versions
            WHERE profile_id=? AND version=?''', (row['id'], row['version'])).fetchone()
        if snap is None or type(snap['size']) is not int or not 1 <= snap['size'] <= 262144:
            _fail('server_provisioning_corrupt', 409)
        raw = db.execute('SELECT snapshot FROM agent_profile_versions WHERE profile_id=? AND version=?', (row['id'], row['version'])).fetchone()[0]
        try:
            snapshot = json.loads(raw)
            fields = json.loads(row['fields'])
            expected = {**dict(row), **fields, 'policy': json.loads(row['policy']),
                'project_ids': json.loads(row['project_ids']), 'legacy': bool(row['legacy'])}
            del expected['fields']
            if snapshot != expected:
                raise ValueError
            provider = fields['preferred_provider']
            _opaque(provider)
            model = None if fields['preferred_model'] == '' else fields['preferred_model']
            _model(model)
            return row, _hash(snapshot), provider, model
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            _fail('server_provisioning_corrupt', 409)

    def _provisioning_instances(self, db, host, *, stopped=False):
        rows = db.execute('SELECT * FROM server_instances WHERE host_id=? AND active=1 ORDER BY catalog_key LIMIT 17', (host['id'],)).fetchall()
        if not 1 <= len(rows) <= 16:
            _fail('server_provisioning_catalog_unavailable', 409)
        views = []
        for row in rows:
            cmd = None if row['command_id'] is None else db.execute('SELECT * FROM server_instance_commands WHERE id=?', (row['command_id'],)).fetchone()
            if stopped and (row['mode'] != 'messages' or row['desired_state'] != 'stopped' or row['observed_state'] != 'stopped'
                    or row['cleanup_confirmed'] != 1 or cmd is None or cmd['action'] != 'stop' or cmd['status'] != 'complete'
                    or cmd['instance_id'] != row['id'] or cmd['generation'] != row['generation']
                    or cmd['instance_version'] != row['version'] or cmd['launchspec_sha256'] != row['launchspec_sha256']):
                _fail('server_provisioning_human_stop_required', 409)
            views.append({**dict(row), 'active': bool(row['active']), 'cleanup_confirmed': bool(row['cleanup_confirmed']),
                'current_command': None if cmd is None else self._server_command_view(cmd)})
        return views

    def get_server_provisioning_grant(self, actor, host_id):
        with self._connection() as db:
            _, host = self._provisioning_host(db, actor, host_id)
            return _grant_view(host, self._provisioning_state(db, host)[0])

    def get_server_provisioning_context(self, actor, host_id, profile_id):
        _opaque(profile_id)
        with self._connection() as db:
            _, host = self._provisioning_host(db, actor, host_id, live=True)
            grant = self._provisioning_state(db, host)[0]
            row, hashed, provider, model = self._provisioning_profile(db, host, profile_id)
            return _bound(dict(schema='server-provision-draft-context-v1', owner_id=host['owner_id'], host_id=host['id'],
                device_id=host['device_id'], expected_host_version=host['version'], expected_catalog_version=host['catalog_version'],
                expected_instances_sha256=catalog_fence(self._provisioning_instances(db, host)),
                grant_version=0 if grant is None else grant['version'], profile_id=row['id'], profile_version=row['version'],
                profile_sha256=hashed, provider_id=provider, requested_model=model), 16384)

    def _provisioning_retry(self, db, host, operation, key, payload):
        row = db.execute('''SELECT payload_sha256,length(CAST(response AS BLOB)) AS size FROM server_provisioning_receipts
            WHERE owner_id=? AND purpose=? AND operation=? AND key=?''', (host['owner_id'], PURPOSE, operation, key)).fetchone()
        if row is None:
            return None
        if row['payload_sha256'] != _hash(payload):
            _fail('server_provisioning_idempotency_conflict', 409)
        if type(row['size']) is not int or not 1 <= row['size'] <= 16384:
            _fail('server_provisioning_corrupt', 409)
        raw = db.execute('''SELECT response FROM server_provisioning_receipts WHERE owner_id=? AND purpose=? AND operation=? AND key=?''', (host['owner_id'], PURPOSE, operation, key)).fetchone()[0]
        try:
            result = json.loads(raw)
            if _json(result) != raw:
                raise ValueError
            if operation == 'grant':
                if set(result) != set(_grant_view(host, None)) or result['schema'] != 'server-provision-draft-grant-view-v1':
                    raise ValueError
                _number(result['version'])
                if type(result['enabled']) is not bool or result['purpose'] != PURPOSE:
                    raise ValueError
            else:
                if type(result) is not dict or set(result) != VIEW_KEYS:
                    raise ValueError
                record = db.execute('SELECT * FROM server_provisioning_requests WHERE id=? AND owner_id=? AND host_id=?',
                    (result['request_id'], host['owner_id'], host['id'])).fetchone()
                body = _decode_request(record)
                historical = dict(record)
                historical.update(state='requested' if operation == 'request' else 'cancelled', revision=1 if operation == 'request' else 2)
                if result != _request_view(historical, body):
                    raise ValueError
            if any(result[key] != host[source] for key, source in (('owner_id', 'owner_id'), ('host_id', 'id'), ('device_id', 'device_id'))):
                raise ValueError
            return result
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError):
            _fail('server_provisioning_corrupt', 409)

    def _provisioning_snapshot(self, db, host):
        rows = self._provisioning_instances(db, host)
        return (dict(host), rows)

    def _provisioning_remember(self, db, actor, host, operation, key, payload, result, snapshot):
        encoded = _json(result)
        _bound(result, 16384)
        db.execute('INSERT INTO server_provisioning_receipts VALUES(?,?,?,?,?,?)',
            (host['owner_id'], PURPOSE, operation, key, _hash(payload), encoded))
        detail = {'host_id': host['id'], 'operation': operation}
        if operation != 'grant':
            detail['request_id'] = result['request_id']
        event_id = self._audit(db, actor, 'server_provisioning_' + operation, detail)
        self._provisioning_state(db, host)
        current = self._server_host(db, actor, host['id'])
        if operation == 'request' or (operation == 'grant' and result['enabled']):
            self._provisioning_host(db, actor, host['id'], live=True)
        if self._provisioning_snapshot(db, current) != snapshot:
            _fail('server_provisioning_postcondition_failed', 409)
        expected = (host['owner_id'], PURPOSE, operation, key, _hash(payload), encoded)
        saved = db.execute('SELECT * FROM server_provisioning_receipts WHERE owner_id=? AND purpose=? AND operation=? AND key=?', expected[:4]).fetchone()
        event = db.execute('SELECT actor,event,detail FROM audit WHERE id=?', (event_id,)).fetchone()
        if saved is None or tuple(saved) != expected or event is None or tuple(event) != (audit_json(actor), 'server_provisioning_' + operation, audit_json(detail)):
            _fail('server_provisioning_postcondition_failed', 409)
        return result

    def set_server_provisioning_grant(self, actor, host_id, **values):
        _exact(values, 'schema expected_host_version expected_grant_version enabled idempotency_key'.split())
        if values['schema'] != GRANT_SCHEMA or type(values['enabled']) is not bool:
            _fail('invalid_server_provisioning_payload')
        _number(values['expected_host_version']); _number(values['expected_grant_version'], 0, 2**63-2); _opaque(values['idempotency_key'])
        with self._tx() as db:
            actor, host = self._provisioning_host(db, actor, host_id, live=values['enabled'])
            grant = self._provisioning_state(db, host)[0]
            payload = {'host_id': host_id, **values}
            prior = self._provisioning_retry(db, host, 'grant', values['idempotency_key'], payload)
            if prior is not None:
                if grant is None or prior != _grant_view(host, grant):
                    _fail('server_provisioning_grant_changed', 409)
                return prior
            if host['version'] != values['expected_host_version'] or (0 if grant is None else grant['version']) != values['expected_grant_version']:
                _fail('server_provisioning_grant_changed', 409)
            snapshot = self._provisioning_snapshot(db, host)
            version = values['expected_grant_version'] + 1
            if grant is None:
                db.execute('INSERT INTO server_provisioning_grants VALUES(?,?,?,?,?,?)',
                    (host_id, host['owner_id'], host['device_id'], PURPOSE, version, int(values['enabled'])))
            else:
                db.execute('UPDATE server_provisioning_grants SET version=?,enabled=? WHERE host_id=?', (version, int(values['enabled']), host_id))
            result = _grant_view(host, {'version': version, 'enabled': int(values['enabled'])})
            result = self._provisioning_remember(db, actor, host, 'grant', values['idempotency_key'], payload, result, snapshot)
            if _grant_view(host, self._provisioning_state(db, host)[0]) != result:
                _fail('server_provisioning_postcondition_failed', 409)
            return result

    def _provisioning_request_authority(self, db, host, body):
        grant = self._provisioning_state(db, host)[0]
        if grant is None or not grant['enabled'] or grant['version'] != body['grant_version']:
            _fail('server_provisioning_not_authorized', 403)
        row, hashed, provider, model = self._provisioning_profile(db, host, body['profile_id'])
        if (row['version'] != body['profile_version'] or hashed != body['profile_sha256']
                or provider != body['provider_id'] or model != body['requested_model']):
            _fail('server_provisioning_profile_changed', 409)

    def create_server_provisioning_request(self, actor, host_id, **values):
        validate_request(values)
        with self._tx() as db:
            actor, host = self._provisioning_host(db, actor, host_id, live=True)
            self._provisioning_request_authority(db, host, values)
            payload = {'host_id': host_id, **values}
            prior = self._provisioning_retry(db, host, 'request', values['idempotency_key'], payload)
            if prior is not None:
                return prior
            _, records, active = self._provisioning_state(db, host)
            if active:
                _fail('server_provisioning_pending', 409)
            views = self._provisioning_instances(db, host, stopped=True)
            if len(views) >= 16 or len(records) >= MAX_REQUESTS:
                _fail('server_provisioning_limit', 409)
            if (host['version'] != values['expected_host_version'] or host['catalog_version'] != values['expected_catalog_version']
                    or catalog_fence(views) != values['expected_instances_sha256']):
                _fail('server_provisioning_catalog_changed', 409)
            if db.execute('SELECT 1 FROM server_instances WHERE host_id=? AND profile_id=? LIMIT 1', (host_id, values['profile_id'])).fetchone():
                _fail('server_provisioning_profile_already_published', 409)
            snapshot = self._provisioning_snapshot(db, host)
            identifier = secrets.token_hex(16)
            record = dict(id=identifier, owner_id=host['owner_id'], host_id=host_id, device_id=host['device_id'],
                revision=1, state='requested', request_sha256=_request_hash(host, values), body=_json(values))
            db.execute('INSERT INTO server_provisioning_requests VALUES(?,?,?,?,?,?,?,?)', tuple(record.values()))
            db.execute('INSERT INTO server_provisioning_barriers VALUES(?,?,?)', (host_id, host['owner_id'], identifier))
            result = _request_view(record, values)
            result = self._provisioning_remember(db, actor, host, 'request', values['idempotency_key'], payload, result, snapshot)
            self._provisioning_request_authority(db, host, values)
            if self._provisioning_request_view(db, host, identifier) != result:
                _fail('server_provisioning_postcondition_failed', 409)
            return result

    def _provisioning_request_view(self, db, host, request_id):
        self._provisioning_state(db, host)
        record = db.execute('SELECT * FROM server_provisioning_requests WHERE id=? AND owner_id=? AND host_id=?',
            (request_id, host['owner_id'], host['id'])).fetchone()
        if record is None:
            _fail('server_provisioning_request_not_found', 404)
        return _request_view(record, _decode_request(record))

    def get_server_provisioning_request(self, actor, host_id, request_id):
        _opaque(request_id)
        with self._connection() as db:
            _, host = self._provisioning_host(db, actor, host_id)
            return self._provisioning_request_view(db, host, request_id)

    def list_server_provisioning_requests(self, actor, host_id, *, before=None, limit=16):
        _number(limit, 1, 16)
        if before is not None:
            _opaque(before)
        with self._connection() as db:
            _, host = self._provisioning_host(db, actor, host_id)
            position = 2**63-1
            if before is not None:
                row = db.execute('SELECT rowid FROM server_provisioning_requests WHERE id=? AND owner_id=? AND host_id=?', (before, host['owner_id'], host_id)).fetchone()
                if row is None:
                    _fail('invalid_server_provisioning_cursor')
                position = row[0]
            rows = db.execute('SELECT * FROM server_provisioning_requests WHERE host_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?', (host_id, position, limit+1)).fetchall()
            selected = []
            for row in rows[:limit]:
                value = _request_view(row, _decode_request(row))
                candidate = dict(schema='server-provision-draft-list-v1', requests=selected+[value], next_before=row['id'])
                if len(_json(candidate).encode('ascii')) > 16384:
                    break
                selected.append(value)
            if rows and not selected:
                _fail('server_provisioning_corrupt', 409)
            return _bound(dict(schema='server-provision-draft-list-v1', requests=selected,
                next_before=selected[-1]['request_id'] if len(rows) > len(selected) else None), 16384)

    def cancel_server_provisioning_request(self, actor, host_id, request_id, **values):
        _opaque(request_id)
        _exact(values, 'schema expected_revision idempotency_key'.split())
        if values['schema'] != CANCEL_SCHEMA:
            _fail('invalid_server_provisioning_payload')
        _number(values['expected_revision']); _opaque(values['idempotency_key'])
        with self._tx() as db:
            actor, host = self._provisioning_host(db, actor, host_id)
            payload = {'host_id': host_id, 'request_id': request_id, **values}
            prior = self._provisioning_retry(db, host, 'cancel', values['idempotency_key'], payload)
            if prior is not None:
                return prior
            view = self._provisioning_request_view(db, host, request_id)
            if view['state'] != 'requested' or view['revision'] != values['expected_revision']:
                _fail('server_provisioning_request_changed', 409)
            snapshot = self._provisioning_snapshot(db, host)
            db.execute("UPDATE server_provisioning_requests SET state='cancelled',revision=2 WHERE id=?", (request_id,))
            db.execute('DELETE FROM server_provisioning_barriers WHERE host_id=? AND request_id=?', (host_id, request_id))
            result = self._provisioning_request_view(db, host, request_id)
            result = self._provisioning_remember(db, actor, host, 'cancel', values['idempotency_key'], payload, result, snapshot)
            if self._provisioning_request_view(db, host, request_id) != result:
                _fail('server_provisioning_postcondition_failed', 409)
            return result
