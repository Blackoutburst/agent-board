"""Fenced messages-only catalogue append. Stored private hashes attest no private I/O.

Every mutation is one IMMEDIATE transaction. Prepared reserves reconciliation,
not a process, login, feed, Runtime slot or proof of private application.
"""
from __future__ import annotations
import hashlib
import json
import secrets
import uuid

from projects import _fail, _json as audit_json
from server_instances import _json, _bound, _opaque, _number, _sha, catalog_fence, validate_entries

PURPOSE = 'messages_append_v1'
PREFIX = 'server_messages_append_'
REQUEST_SCHEMA = 'server-messages-append-request-v1'
GRANT_SCHEMA = 'server-messages-append-grant-v1'
CANCEL_SCHEMA = 'server-messages-append-cancel-v1'
PROOF_SCHEMA = 'server-messages-append-proof-v1'
REQUEST_KEYS = frozenset('schema expected_host_version expected_catalog_version expected_instances_sha256 grant_version profile_id profile_version profile_sha256 provider_id requested_model slot_id idempotency_key'.split())
HASH_KEYS = tuple('source_release_sha256 old_configuration_sha256 new_configuration_sha256 old_catalog_sha256 new_catalog_sha256 bindings_sha256 children_roster_sha256'.split())
PROOF_KEYS = frozenset(('schema operation_id request_id request_sha256 owner_id host_id device_id grant_version request_revision expected_host_version expected_catalog_version expected_instances_sha256 phase instance_id catalog_version instances_sha256'.split())) | frozenset(HASH_KEYS)
SLOT_KEYS = tuple('profile_id profile_version device_id session_id agent_kind identity_key'.split())
PHASES = {'prepared': 2, 'precommit': 3, 'committed': 4, 'complete': 5}
ACTIVE = ('requested', 'prepared', 'precommit', 'committed')
MAX_REQUESTS = 32
SQL = {
 'server_messages_append_grants': 'CREATE TABLE server_messages_append_grants(host_id TEXT PRIMARY KEY REFERENCES server_hosts(id),owner_id TEXT NOT NULL REFERENCES owners(id),device_id TEXT NOT NULL REFERENCES devices(id),purpose TEXT NOT NULL,version INTEGER NOT NULL,enabled INTEGER NOT NULL)',
 'server_messages_append_requests': 'CREATE TABLE server_messages_append_requests(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES owners(id),host_id TEXT NOT NULL REFERENCES server_hosts(id),device_id TEXT NOT NULL REFERENCES devices(id),revision INTEGER NOT NULL,state TEXT NOT NULL,request_sha256 TEXT NOT NULL,body TEXT NOT NULL,slot TEXT NOT NULL,proof TEXT)',
 'server_messages_append_barriers': 'CREATE TABLE server_messages_append_barriers(host_id TEXT PRIMARY KEY REFERENCES server_hosts(id),owner_id TEXT NOT NULL REFERENCES owners(id),request_id TEXT UNIQUE NOT NULL REFERENCES server_messages_append_requests(id))',
 'server_messages_append_receipts': 'CREATE TABLE server_messages_append_receipts(owner_id TEXT NOT NULL REFERENCES owners(id),host_id TEXT NOT NULL REFERENCES server_hosts(id),operation TEXT NOT NULL,key TEXT NOT NULL,payload_sha256 TEXT NOT NULL,response TEXT NOT NULL,PRIMARY KEY(owner_id,host_id,operation,key))',
 'server_messages_append_one_active': "CREATE UNIQUE INDEX server_messages_append_one_active ON server_messages_append_requests(host_id) WHERE state IN ('requested','prepared','precommit','committed')",
}
SENSITIVE = tuple(SQL)[:4] + ('server_hosts','server_instances','server_instance_commands','server_control_receipts','audit',
 'server_provisioning_grants','server_provisioning_requests','server_provisioning_barriers','server_provisioning_receipts',
 'owners','devices','agent_profiles','agent_profile_versions','agent_profile_bindings','sessions')


def migrate_server_messages_append(db):
    for statement in SQL.values(): db.execute(statement)


def _hash(value): return hashlib.sha256(_json(value).encode('ascii')).hexdigest()


def _exact(value, keys, bound=8192):
    if type(value) is not dict or set(value) != set(keys): _fail('invalid_server_messages_append_payload')
    _bound(value, bound)


def _uuid(value):
    if type(value) is not str:
        _fail('invalid_server_messages_append_operation')
    try:
        if str(uuid.UUID(value)) != value: raise ValueError
    except ValueError: _fail('invalid_server_messages_append_operation')


def validate_request(value):
    _exact(value, REQUEST_KEYS)
    if value['schema'] != REQUEST_SCHEMA or value['provider_id'] != 'codex_chatgpt':
        _fail('invalid_server_messages_append_payload')
    for key in ('expected_host_version','expected_catalog_version','grant_version','profile_version'): _number(value[key],1,2**63-2)
    for key in ('expected_instances_sha256','profile_sha256','slot_id'): _sha(value[key])
    for key in ('profile_id','requested_model','idempotency_key'): _opaque(value[key])


def validate_proof(value):
    _exact(value, PROOF_KEYS)
    if value['schema'] != PROOF_SCHEMA or type(value['phase']) is not str or value['phase'] not in PHASES:
        _fail('invalid_server_messages_append_proof')
    if (value['old_configuration_sha256']==value['new_configuration_sha256']
        or value['old_catalog_sha256']==value['new_catalog_sha256']): _fail('invalid_server_messages_append_proof')
    _uuid(value['operation_id'])
    for key in ('request_id','owner_id','host_id','device_id'): _opaque(value[key])
    for key in ('grant_version','request_revision','expected_host_version','expected_catalog_version'): _number(value[key])
    if value['request_revision'] != PHASES[value['phase']]: _fail('invalid_server_messages_append_proof')
    for key in HASH_KEYS + ('request_sha256','expected_instances_sha256'): _sha(value[key])
    if value['phase'] in ('prepared','precommit'):
        if any(value[k] is not None for k in ('instance_id','catalog_version','instances_sha256')): _fail('invalid_server_messages_append_proof')
    else:
        _opaque(value['instance_id']); _number(value['catalog_version']); _sha(value['instances_sha256'])


def validate_messages_append_arguments(name, args):
    common = {'host_id','request_id','expected_revision','idempotency_key'}
    if name == 'poll_server_messages_appends':
        _exact(args, {'host_id','limit'}); _opaque(args['host_id']); _number(args['limit'],1,16); return
    if name == 'prepare_server_messages_append':
        _exact(args, common | {'operation_id'} | set(HASH_KEYS))
        _uuid(args['operation_id'])
        for key in HASH_KEYS: _sha(args[key])
        if (args['old_configuration_sha256']==args['new_configuration_sha256']
            or args['old_catalog_sha256']==args['new_catalog_sha256']): _fail('invalid_server_messages_append_payload')
        if args['expected_revision'] != 1 or type(args['expected_revision']) is not int: _fail('invalid_server_messages_append_revision')
    elif name in ('precommit_server_messages_append','commit_server_messages_append','complete_server_messages_append'):
        _exact(args, common | {'proof'} | ({'entry'} if name == 'commit_server_messages_append' else set()),16384)
        validate_proof(args['proof'])
        if name == 'commit_server_messages_append': validate_entries([args['entry']])
    else: _fail('invalid_server_messages_append_method')
    for key in ('host_id','request_id','idempotency_key'): _opaque(args[key])
    _number(args['expected_revision'],1,5)


def _grant_view(host, row):
    return dict(schema='server-messages-append-grant-view-v1',owner_id=host['owner_id'],host_id=host['id'],
        device_id=host['device_id'],purpose=PURPOSE,version=0 if row is None else row['version'],
        enabled=False if row is None else bool(row['enabled']))


def _request_hash(host, body):
    return _hash(dict(purpose=PURPOSE,owner_id=host['owner_id'],host_id=host['id'],device_id=host['device_id'],body=body))


def _load(raw, maximum):
    if type(raw) is not str or not 0 < len(raw.encode('utf-8')) <= maximum: _fail('server_messages_append_corrupt',409)
    try:
        value=json.loads(raw)
        if _json(value)!=raw: raise ValueError
        return value
    except (ValueError,TypeError,RecursionError,UnicodeError): _fail('server_messages_append_corrupt',409)


def _decode(row):
    body=_load(row['body'],8192); validate_request(body)
    slot=_load(row['slot'],8192); _exact(slot, {'slot_id',*SLOT_KEYS})
    _number(slot['profile_version'])
    for key in SLOT_KEYS:
        if key!='profile_version': _opaque(slot[key])
    if (slot['slot_id']!=_hash({k:slot[k] for k in SLOT_KEYS}) or slot['slot_id']!=body['slot_id']
        or slot['profile_id']!=body['profile_id'] or slot['profile_version']!=body['profile_version']
        or row['request_sha256']!=_request_hash({'id':row['host_id'],'owner_id':row['owner_id'],'device_id':row['device_id']},body)):
        _fail('server_messages_append_corrupt',409)
    for k in ('id','owner_id','host_id','device_id'): _opaque(row[k])
    expected=1 if row['state']=='requested' else 2 if row['state']=='cancelled' else PHASES.get(row['state'])
    if type(row['revision']) is not int or expected is None or row['revision']!=expected: _fail('server_messages_append_corrupt',409)
    proof=None if row['proof'] is None else _load(row['proof'],8192)
    if row['state'] in ('requested','cancelled'):
        if proof is not None: _fail('server_messages_append_corrupt',409)
    else:
        validate_proof(proof)
        expected_identity=dict(request_id=row['id'],request_sha256=row['request_sha256'],owner_id=row['owner_id'],
            host_id=row['host_id'],device_id=row['device_id'],grant_version=body['grant_version'],
            expected_host_version=body['expected_host_version'],expected_catalog_version=body['expected_catalog_version'],
            expected_instances_sha256=body['expected_instances_sha256'],phase=row['state'],request_revision=row['revision'])
        if any(proof[k]!=v for k,v in expected_identity.items()): _fail('server_messages_append_corrupt',409)
        if proof['phase'] in ('committed','complete') and proof['catalog_version']!=body['expected_catalog_version']+1:
            _fail('server_messages_append_corrupt',409)
    return body,slot,proof


def _view(row):
    body,_,proof=_decode(row)
    return _bound(dict(schema='server-messages-append-view-v1',request_id=row['id'],request_sha256=row['request_sha256'],
        owner_id=row['owner_id'],host_id=row['host_id'],device_id=row['device_id'],revision=row['revision'],
        state=row['state'],reason='cancelled_by_owner' if row['state']=='cancelled' else row['state'],
        barrier_held=row['state'] in ACTIVE,profile_id=body['profile_id'],profile_version=body['profile_version'],
        provider_id=body['provider_id'],requested_model=body['requested_model'],slot_id=body['slot_id'],
        can_cancel=row['state']=='requested',instance_id=None if proof is None else proof['instance_id'],proof=proof,
        connection_status='not_attested',model_catalog_status='not_attested',model_access_status='not_attested'),16384)


class ServerMessagesAppendMixin:
    def _messages_append_schema(self, db):
        if db.execute('PRAGMA user_version').fetchone()[0]!=17: _fail('server_messages_append_schema_invalid',409)
        if any(r['name'] not in SQL for r in db.execute("SELECT name FROM main.sqlite_master WHERE lower(name) GLOB 'server_messages_append_*'")):
            _fail('server_messages_append_schema_invalid',409)
        marks=','.join('?' for _ in SENSITIVE)
        for schema in ('main','temp'):
            if db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='trigger' AND tbl_name COLLATE NOCASE IN ({marks}) LIMIT 1",SENSITIVE).fetchone():
                _fail('server_messages_append_schema_invalid',409)
        if (db.execute(f'SELECT 1 FROM temp.sqlite_master WHERE name COLLATE NOCASE IN ({marks}) LIMIT 1',SENSITIVE).fetchone()
            or db.execute(f"SELECT 1 FROM main.sqlite_master WHERE type='view' AND name COLLATE NOCASE IN ({marks}) LIMIT 1",SENSITIVE).fetchone()):
            _fail('server_messages_append_schema_invalid',409)
        for name,expected in SQL.items():
            row=db.execute('SELECT sql FROM main.sqlite_master WHERE name=?',(name,)).fetchone()
            if row is None or row['sql']!=expected: _fail('server_messages_append_schema_invalid',409)
        tables=tuple(SQL)[:4]
        allowed={f'sqlite_autoindex_{n}_1' for n in tables}|{'sqlite_autoindex_server_messages_append_barriers_2','server_messages_append_one_active'}
        for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='index' AND tbl_name IN (?,?,?,?)",tables):
            if r['name'] not in allowed: _fail('server_messages_append_schema_invalid',409)

    def _messages_append_state(self, db, host):
        self._messages_append_schema(db)
        if db.execute("SELECT 1 FROM server_messages_append_grants WHERE host_id=? AND (typeof(purpose)!='text' OR length(CAST(purpose AS BLOB))>200) LIMIT 1",(host['id'],)).fetchone():
            _fail('server_messages_append_corrupt',409)
        grant=db.execute('SELECT * FROM server_messages_append_grants WHERE host_id=?',(host['id'],)).fetchone()
        if grant is not None and (grant['owner_id']!=host['owner_id'] or grant['device_id']!=host['device_id']
            or grant['purpose']!=PURPOSE or type(grant['version']) is not int or not 1<=grant['version']<2**63
            or type(grant['enabled']) is not int or grant['enabled'] not in (0,1)):
            _fail('server_messages_append_corrupt',409)
        if db.execute('''SELECT 1 FROM server_messages_append_requests WHERE host_id=? AND
            (typeof(id)!='text' OR length(CAST(id AS BLOB))>800 OR typeof(state)!='text' OR length(CAST(state AS BLOB))>32
             OR typeof(request_sha256)!='text' OR length(request_sha256)!=64 OR typeof(body)!='text' OR length(CAST(body AS BLOB))>8192 OR typeof(slot)!='text' OR length(CAST(slot AS BLOB))>8192
             OR (proof IS NOT NULL AND (typeof(proof)!='text' OR length(CAST(proof AS BLOB))>8192))) LIMIT 1''',(host['id'],)).fetchone():
            _fail('server_messages_append_corrupt',409)
        rows=db.execute('SELECT * FROM server_messages_append_requests WHERE host_id=? ORDER BY rowid LIMIT 33',(host['id'],)).fetchall()
        if len(rows)>MAX_REQUESTS: _fail('server_messages_append_corrupt',409)
        active=[];operations=set()
        for row in rows:
            decoded=_decode(row)
            if decoded[2] is not None:
                operation=decoded[2]['operation_id']
                if operation in operations: _fail('server_messages_append_corrupt',409)
                operations.add(operation)
            if row['owner_id']!=host['owner_id'] or row['device_id']!=host['device_id']: _fail('server_messages_append_corrupt',409)
            if row['state'] in ACTIVE: active.append(row['id'])
        barriers=db.execute('SELECT * FROM server_messages_append_barriers WHERE host_id=? LIMIT 2',(host['id'],)).fetchall()
        if (len(active)>1 or len(barriers)!=len(active) or (barriers and
            (barriers[0]['owner_id']!=host['owner_id'] or barriers[0]['request_id']!=active[0]))): _fail('server_messages_append_corrupt',409)
        return grant,rows,bool(active)

    def _messages_append_has_barrier(self, db, host): return self._messages_append_state(db,host)[2]

    def _messages_append_require_no_barrier(self, db, host):
        if self._messages_append_has_barrier(db,host): _fail('server_messages_append_pending',409)

    def _messages_append_publish_identity(self, db, host, entries):
        # After a real append, ordinary publication may refresh only the same
        # identities. It cannot retire/add/rebind a membership behind Host5.
        if any(r['state']=='complete' for r in self._messages_append_state(db,host)[1]):
            old=[tuple(r) for r in db.execute('SELECT catalog_key,profile_id,profile_version,mode FROM server_instances WHERE host_id=? AND active=1 ORDER BY catalog_key LIMIT 17',(host['id'],))]
            new=sorted((e['catalog_key'],e['profile_id'],e['profile_version'],e['mode']) for e in entries)
            if old!=new: _fail('server_messages_append_identity_required',409)

    def _messages_append_host(self, db, actor, host_id, *, manager=False, live=False):
        _opaque(host_id)
        if manager: host=self._server_manager(db,actor,host_id)
        else:
            actor=self._human(db,actor); host=self._server_host(db,actor,host_id)
        self._messages_append_state(db,host)
        if live and (host['revoked_at'] is not None or not self._server_owner_enabled(db,host['owner_id'])
            or db.execute('SELECT 1 FROM devices WHERE id=? AND owner_id=? AND revoked_at IS NULL',(host['device_id'],host['owner_id'])).fetchone() is None):
            _fail('server_messages_append_not_authorized',403)
        return actor,host

    def _messages_append_slots(self, db, host, profile):
        if db.execute('''SELECT 1 FROM agent_profile_bindings b JOIN sessions s
            ON s.device_id=b.device_id AND s.agent_id=b.profile_id AND s.agent_kind=b.agent_kind
            JOIN devices d ON d.id=b.device_id AND d.owner_id=s.owner_id AND d.revoked_at IS NULL
            WHERE b.profile_id=? AND s.owner_id=? AND s.profile_version=? AND b.agent_kind='codex' AND
            (typeof(s.id)!='text' OR length(CAST(s.id AS BLOB))>800 OR typeof(b.agent_kind)!='text'
             OR length(CAST(b.agent_kind AS BLOB))>800 OR typeof(b.identity_key)!='text' OR length(CAST(b.identity_key AS BLOB))>800)
            LIMIT 1''',(profile['id'],host['owner_id'],profile['version'])).fetchone():
            _fail('server_messages_append_corrupt',409)
        rows=db.execute('''SELECT b.profile_id,s.profile_version,b.device_id,s.id AS session_id,b.agent_kind,b.identity_key
            FROM agent_profile_bindings b JOIN sessions s ON s.device_id=b.device_id AND s.agent_id=b.profile_id AND s.agent_kind=b.agent_kind
            JOIN devices d ON d.id=b.device_id AND d.owner_id=s.owner_id AND d.revoked_at IS NULL
            WHERE b.profile_id=? AND s.owner_id=? AND s.profile_version=? AND b.agent_kind='codex' ORDER BY b.device_id,s.id,b.agent_kind,b.identity_key LIMIT 17''',
            (profile['id'],host['owner_id'],profile['version'])).fetchall()
        if len(rows)>16: _fail('server_messages_append_slot_limit',409)
        slots=[]
        for row in rows:
            value=dict(row)
            for key in SLOT_KEYS:
                if key=='profile_version': _number(value[key])
                else: _opaque(value[key])
            slots.append(dict(slot_id=_hash(value),**value))
        return slots

    def _messages_append_authority(self, db, host, body, slot):
        grant=self._messages_append_state(db,host)[0]
        if grant is None or not grant['enabled'] or grant['version']!=body['grant_version']:
            _fail('server_messages_append_not_authorized',403)
        profile,digest,provider,model=self._provisioning_profile(db,host,body['profile_id'])
        if (profile['version']!=body['profile_version'] or digest!=body['profile_sha256'] or provider!='codex_chatgpt'
            or provider!=body['provider_id'] or model is None or model!=body['requested_model']):
            _fail('server_messages_append_profile_changed',409)
        if slot not in self._messages_append_slots(db,host,profile): _fail('server_messages_append_slot_changed',409)
        if self._server_start_reason(db,host,dict(profile_id=profile['id'],profile_version=profile['version'],mode='messages',active=1),_barriers=False):
            _fail('server_messages_append_not_authorized',403)

    def _messages_append_record(self, db, host, request_id):
        _opaque(request_id); self._messages_append_state(db,host)
        row=db.execute('SELECT * FROM server_messages_append_requests WHERE id=? AND host_id=? AND owner_id=?',
            (request_id,host['id'],host['owner_id'])).fetchone()
        if row is None: _fail('server_messages_append_not_found',404)
        return row

    def _messages_append_retry(self, db, host, operation, key, payload):
        row=db.execute('''SELECT payload_sha256,length(CAST(response AS BLOB)) AS size FROM server_messages_append_receipts
            WHERE owner_id=? AND host_id=? AND operation=? AND key=?''',(host['owner_id'],host['id'],operation,key)).fetchone()
        if row is None: return None
        if row['payload_sha256']!=_hash(payload): _fail('server_messages_append_idempotency_conflict',409)
        if type(row['size']) is not int or not 1<=row['size']<=16384: _fail('server_messages_append_corrupt',409)
        raw=db.execute('SELECT response FROM server_messages_append_receipts WHERE owner_id=? AND host_id=? AND operation=? AND key=?',
            (host['owner_id'],host['id'],operation,key)).fetchone()[0]
        value=_load(raw,16384)
        if operation=='grant':
            _exact(value,set(_grant_view(host,None)))
            _number(value['version'])
            if (value['schema']!='server-messages-append-grant-view-v1' or type(value['enabled']) is not bool
                or value['purpose']!=PURPOSE): _fail('server_messages_append_corrupt',409)
        else:
            request=self._messages_append_record(db,host,value.get('request_id') if type(value) is dict else None)
            historical=dict(request)
            phase='requested' if operation=='request' else 'cancelled' if operation=='cancel' else operation
            historical.update(state=phase,revision=1 if phase=='requested' else 2 if phase=='cancelled' else PHASES[phase])
            if phase in ('requested','cancelled'):
                historical['proof']=None
                if value!=_view(historical): _fail('server_messages_append_corrupt',409)
            else:
                validate_proof(value)
                historical['proof']=_json(value)
                if _decode(historical)[2]!=value: _fail('server_messages_append_corrupt',409)
                current=_decode(request)[2]; proof=value
                invariant=PROOF_KEYS-{'phase','request_revision','instance_id','catalog_version','instances_sha256'}
                if current is None or any(proof[k]!=current[k] for k in invariant): _fail('server_messages_append_corrupt',409)
                if phase=='prepared':
                    if any(proof[k]!=payload[k] for k in (*HASH_KEYS,'operation_id')): _fail('server_messages_append_corrupt',409)
                elif any(proof[k]!=payload['proof'][k] for k in invariant): _fail('server_messages_append_corrupt',409)
                if phase in ('committed','complete') and any(proof[k]!=current[k] for k in ('instance_id','catalog_version','instances_sha256')):
                    _fail('server_messages_append_corrupt',409)
        if any(value[k]!=host[v] for k,v in (('owner_id','owner_id'),('host_id','id'),('device_id','device_id'))):
            _fail('server_messages_append_corrupt',409)
        return value

    def _messages_append_snapshot(self, db, host):
        # Bound old membership; total_changes additionally detects any extra
        # command/history/authority write, even outside this host.
        text_fields=('id','host_id','catalog_key','profile_id','launchspec_sha256','mode','desired_state','observed_state','reason')
        invalid=' OR '.join("typeof("+k+")!='text' OR length(CAST("+k+" AS BLOB))>800" for k in text_fields)
        if db.execute('SELECT 1 FROM server_instances WHERE host_id=? AND ('+invalid+') LIMIT 1',(host['id'],)).fetchone():
            _fail('server_messages_append_corrupt',409)
        rows=[dict(r) for r in db.execute('SELECT * FROM server_instances WHERE host_id=? ORDER BY catalog_key LIMIT 257',(host['id'],))]
        if len(rows)>256: _fail('server_messages_append_history_limit',409)
        return dict(host),rows,(db.total_changes,db.execute('PRAGMA schema_version').fetchone()[0],db.execute('PRAGMA application_id').fetchone()[0])

    def _messages_append_remember(self, db, actor, host, operation, key, payload, result, snapshot, changes, *, expected_host=None, expected_rows=None):
        encoded=_json(_bound(result,16384))
        expected=(host['owner_id'],host['id'],operation,key,_hash(payload),encoded)
        db.execute('INSERT INTO server_messages_append_receipts VALUES(?,?,?,?,?,?)',expected)
        detail=dict(host_id=host['id'],operation=operation)
        if operation!='grant': detail['request_id']=result['request_id']
        event=self._audit(db,actor,'server_messages_append_'+operation,detail)
        self._messages_append_state(db,host)
        current=self._server_host(db,actor,host['id'])
        final=self._messages_append_snapshot(db,current)
        saved=db.execute('SELECT * FROM server_messages_append_receipts WHERE owner_id=? AND host_id=? AND operation=? AND key=?',expected[:4]).fetchone()
        audit=db.execute('SELECT actor,event,detail FROM audit WHERE id=?',(event,)).fetchone()
        if (final[0]!=(snapshot[0] if expected_host is None else expected_host)
            or final[1]!=(snapshot[1] if expected_rows is None else expected_rows)
            or final[2][0]-snapshot[2][0]!=changes or final[2][1:]!=snapshot[2][1:]
            or saved is None or tuple(saved)!=expected or audit is None
            or tuple(audit)!=(audit_json(actor),'server_messages_append_'+operation,audit_json(detail))):
            _fail('server_messages_append_postcondition_failed',409)
        # Last operation is a read. Never return a contradicted durable proof.
        if operation=='grant':
            if _grant_view(current,self._messages_append_state(db,current)[0])!=result: _fail('server_messages_append_postcondition_failed',409)
        else:
            record=self._messages_append_record(db,current,result['request_id'])
            final_result=_decode(record)[2] if operation in PHASES else _view(record)
            if final_result!=result: _fail('server_messages_append_postcondition_failed',409)
        return result

    def get_server_messages_append_grant(self, actor, host_id):
        with self._connection() as db:
            _,host=self._messages_append_host(db,actor,host_id)
            return _grant_view(host,self._messages_append_state(db,host)[0])

    def set_server_messages_append_grant(self, actor, host_id, **values):
        _exact(values,'schema expected_host_version expected_grant_version enabled idempotency_key'.split())
        if values['schema']!=GRANT_SCHEMA or type(values['enabled']) is not bool: _fail('invalid_server_messages_append_payload')
        _number(values['expected_host_version']);_number(values['expected_grant_version'],0,2**63-2);_opaque(values['idempotency_key'])
        with self._tx() as db:
            actor,host=self._messages_append_host(db,actor,host_id,live=values['enabled'])
            grant=self._messages_append_state(db,host)[0];payload=dict(host_id=host_id,**values)
            prior=self._messages_append_retry(db,host,'grant',values['idempotency_key'],payload)
            if prior is not None:
                if prior!=_grant_view(host,grant): _fail('server_messages_append_grant_changed',409)
                return prior
            if host['version']!=values['expected_host_version'] or (0 if grant is None else grant['version'])!=values['expected_grant_version']:
                _fail('server_messages_append_grant_changed',409)
            snapshot=self._messages_append_snapshot(db,host)
            if grant is None:
                db.execute('INSERT INTO server_messages_append_grants VALUES(?,?,?,?,?,?)',(host_id,host['owner_id'],host['device_id'],PURPOSE,values['expected_grant_version']+1,int(values['enabled'])))
            else: db.execute('UPDATE server_messages_append_grants SET version=version+1,enabled=? WHERE host_id=?',(int(values['enabled']),host_id))
            result=_grant_view(host,self._messages_append_state(db,host)[0])
            return self._messages_append_remember(db,actor,host,'grant',values['idempotency_key'],payload,result,snapshot,3)

    def get_server_messages_append_context(self, actor, host_id, profile_id):
        _opaque(profile_id)
        with self._connection() as db:
            _,host=self._messages_append_host(db,actor,host_id,live=True)
            profile,digest,provider,model=self._provisioning_profile(db,host,profile_id)
            if provider!='codex_chatgpt' or model is None: _fail('server_messages_append_provider_unavailable',409)
            grant=self._messages_append_state(db,host)[0]
            return _bound(dict(schema='server-messages-append-context-v1',owner_id=host['owner_id'],host_id=host_id,device_id=host['device_id'],
                expected_host_version=host['version'],expected_catalog_version=host['catalog_version'],
                expected_instances_sha256=catalog_fence(self._provisioning_instances(db,host)),grant_version=0 if grant is None else grant['version'],
                profile_id=profile['id'],profile_version=profile['version'],profile_sha256=digest,provider_id=provider,requested_model=model,
                slots=self._messages_append_slots(db,host,profile)),16384)

    def create_server_messages_append(self, actor, host_id, **values):
        validate_request(values)
        with self._tx() as db:
            actor,host=self._messages_append_host(db,actor,host_id,live=True)
            self._provisioning_require_no_barrier(db,host)
            profile=self._provisioning_profile(db,host,values['profile_id'])[0]
            slot=next((s for s in self._messages_append_slots(db,host,profile) if s['slot_id']==values['slot_id']),None)
            if slot is None: _fail('server_messages_append_slot_changed',409)
            self._messages_append_authority(db,host,values,slot)
            payload=dict(host_id=host_id,**values)
            prior=self._messages_append_retry(db,host,'request',values['idempotency_key'],payload)
            if prior is not None: return prior
            _,rows,active=self._messages_append_state(db,host)
            if active: _fail('server_messages_append_pending',409)
            instances=self._provisioning_instances(db,host,stopped=True)
            if len(instances)>=16 or len(rows)>=MAX_REQUESTS: _fail('server_messages_append_limit',409)
            if (host['version']!=values['expected_host_version'] or host['catalog_version']!=values['expected_catalog_version']
                or catalog_fence(instances)!=values['expected_instances_sha256']): _fail('server_messages_append_catalog_changed',409)
            if db.execute('SELECT 1 FROM server_instances WHERE host_id=? AND profile_id=? LIMIT 1',(host_id,values['profile_id'])).fetchone():
                _fail('server_messages_append_profile_already_published',409)
            snapshot=self._messages_append_snapshot(db,host)
            row=dict(id=secrets.token_hex(16),owner_id=host['owner_id'],host_id=host_id,device_id=host['device_id'],revision=1,state='requested',
                request_sha256=_request_hash(host,values),body=_json(values),slot=_json(slot),proof=None)
            db.execute('INSERT INTO server_messages_append_requests VALUES(?,?,?,?,?,?,?,?,?,?)',tuple(row.values()))
            db.execute('INSERT INTO server_messages_append_barriers VALUES(?,?,?)',(host_id,host['owner_id'],row['id']))
            result=self._messages_append_remember(db,actor,host,'request',values['idempotency_key'],payload,_view(row),snapshot,4)
            self._messages_append_authority(db,host,values,slot)
            return result

    def get_server_messages_append(self, actor, host_id, request_id):
        with self._connection() as db:
            _,host=self._messages_append_host(db,actor,host_id)
            return _view(self._messages_append_record(db,host,request_id))

    def list_server_messages_appends(self, actor, host_id, *, before=None, limit=16):
        _number(limit,1,16)
        if before is not None: _opaque(before)
        with self._connection() as db:
            _,host=self._messages_append_host(db,actor,host_id)
            position=2**63-1
            if before is not None:
                row=db.execute('SELECT rowid FROM server_messages_append_requests WHERE id=? AND host_id=?',(before,host_id)).fetchone()
                if row is None: _fail('invalid_server_messages_append_cursor')
                position=row[0]
            rows=db.execute('SELECT * FROM server_messages_append_requests WHERE host_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?',(host_id,position,limit+1)).fetchall()
            selected=[]
            for row in rows[:limit]:
                value=_view(row)
                if len(_json(dict(schema='server-messages-append-list-v1',requests=selected+[value],next_before=row['id'])).encode('ascii'))>16384: break
                selected.append(value)
            if rows and not selected: _fail('server_messages_append_corrupt',409)
            return _bound(dict(schema='server-messages-append-list-v1',requests=selected,next_before=selected[-1]['request_id'] if len(rows)>len(selected) else None),16384)

    def cancel_server_messages_append(self, actor, host_id, request_id, **values):
        _exact(values,'schema expected_revision idempotency_key'.split());_opaque(request_id)
        if values['schema']!=CANCEL_SCHEMA: _fail('invalid_server_messages_append_payload')
        _number(values['expected_revision']);_opaque(values['idempotency_key'])
        with self._tx() as db:
            actor,host=self._messages_append_host(db,actor,host_id)
            payload=dict(host_id=host_id,request_id=request_id,**values)
            prior=self._messages_append_retry(db,host,'cancel',values['idempotency_key'],payload)
            if prior is not None: return prior
            row=self._messages_append_record(db,host,request_id)
            if row['state']!='requested' or row['revision']!=values['expected_revision']: _fail('server_messages_append_changed',409)
            snapshot=self._messages_append_snapshot(db,host)
            db.execute("UPDATE server_messages_append_requests SET state='cancelled',revision=2 WHERE id=?",(request_id,))
            db.execute('DELETE FROM server_messages_append_barriers WHERE host_id=? AND request_id=?',(host_id,request_id))
            return self._messages_append_remember(db,actor,host,'cancel',values['idempotency_key'],payload,
                _view(self._messages_append_record(db,host,request_id)),snapshot,4)

    def poll_server_messages_appends(self, actor, host_id, limit=1):
        validate_messages_append_arguments('poll_server_messages_appends',dict(host_id=host_id,limit=limit))
        with self._connection() as db:
            _,host=self._messages_append_host(db,actor,host_id,manager=True,live=True)
            self._provisioning_require_no_barrier(db,host)
            requests=[]
            for row in self._messages_append_state(db,host)[1]:
                if row['state'] not in ACTIVE: continue
                body,slot,_=_decode(row);self._messages_append_authority(db,host,body,slot)
                item=dict(request=_view(row),body=body,slot=slot)
                requests.append(item)
                if len(requests)>=limit: break
            return _bound(dict(schema='server-messages-append-poll-v1',host_id=host_id,requests=requests),65536)

    def _messages_append_fence(self, db, host, body, proof=None):
        if host['version']!=body['expected_host_version']: _fail('server_messages_append_catalog_changed',409)
        if proof is None or proof['phase'] in ('prepared','precommit'):
            instances=self._provisioning_instances(db,host,stopped=True)
            if (host['catalog_version']!=body['expected_catalog_version'] or catalog_fence(instances)!=body['expected_instances_sha256']):
                _fail('server_messages_append_catalog_changed',409)
        else:
            instances=self._provisioning_instances(db,host)
            if host['catalog_version']!=proof['catalog_version'] or catalog_fence(instances)!=proof['instances_sha256']:
                _fail('server_messages_append_catalog_changed',409)

    def prepare_server_messages_append(self, actor, host_id, request_id, expected_revision, operation_id, source_release_sha256,
            old_configuration_sha256,new_configuration_sha256,old_catalog_sha256,new_catalog_sha256,bindings_sha256,children_roster_sha256,idempotency_key):
        args=dict(host_id=host_id,request_id=request_id,expected_revision=expected_revision,operation_id=operation_id,
            source_release_sha256=source_release_sha256,old_configuration_sha256=old_configuration_sha256,new_configuration_sha256=new_configuration_sha256,
            old_catalog_sha256=old_catalog_sha256,new_catalog_sha256=new_catalog_sha256,bindings_sha256=bindings_sha256,children_roster_sha256=children_roster_sha256,idempotency_key=idempotency_key)
        validate_messages_append_arguments('prepare_server_messages_append',args)
        return self._messages_append_phase(actor,'prepared',args)

    def precommit_server_messages_append(self, actor, host_id, request_id, expected_revision, proof, idempotency_key):
        args=dict(host_id=host_id,request_id=request_id,expected_revision=expected_revision,proof=proof,idempotency_key=idempotency_key)
        validate_messages_append_arguments('precommit_server_messages_append',args)
        return self._messages_append_phase(actor,'precommit',args)

    def commit_server_messages_append(self, actor, host_id, request_id, expected_revision, proof, entry, idempotency_key):
        args=dict(host_id=host_id,request_id=request_id,expected_revision=expected_revision,proof=proof,entry=entry,idempotency_key=idempotency_key)
        validate_messages_append_arguments('commit_server_messages_append',args)
        return self._messages_append_phase(actor,'committed',args)

    def complete_server_messages_append(self, actor, host_id, request_id, expected_revision, proof, idempotency_key):
        args=dict(host_id=host_id,request_id=request_id,expected_revision=expected_revision,proof=proof,idempotency_key=idempotency_key)
        validate_messages_append_arguments('complete_server_messages_append',args)
        return self._messages_append_phase(actor,'complete',args)

    def _messages_append_phase(self, actor, phase, args):
        with self._tx() as db:
            actor,host=self._messages_append_host(db,actor,args['host_id'],manager=True,live=True)
            self._provisioning_require_no_barrier(db,host)
            row=self._messages_append_record(db,host,args['request_id']);body,slot,previous=_decode(row)
            self._messages_append_authority(db,host,body,slot)
            # Read authority before receipt replay; a historical response is not
            # a current permission, fence, or reconciliation proof.
            prior=self._messages_append_retry(db,host,phase,args['idempotency_key'],args)
            if prior is not None: return prior
            wanted=PHASES[phase]-1
            if row['revision']!=args['expected_revision'] or row['revision']!=wanted or row['state']!=('requested' if phase=='prepared' else {3:'prepared',4:'precommit',5:'committed'}[PHASES[phase]]):
                _fail('server_messages_append_changed',409)
            self._messages_append_fence(db,host,body,previous)
            if phase!='prepared' and _json(args['proof'])!=row['proof']: _fail('server_messages_append_proof_changed',409)
            snapshot=self._messages_append_snapshot(db,host)
            if phase=='prepared':
                if any(_decode(other)[2] is not None and _decode(other)[2]['operation_id']==args['operation_id']
                    for other in self._messages_append_state(db,host)[1]): _fail('server_messages_append_operation_conflict',409)
                proof=dict(schema=PROOF_SCHEMA,operation_id=args['operation_id'],request_id=row['id'],request_sha256=row['request_sha256'],
                    owner_id=host['owner_id'],host_id=host['id'],device_id=host['device_id'],grant_version=body['grant_version'],request_revision=2,
                    expected_host_version=body['expected_host_version'],expected_catalog_version=body['expected_catalog_version'],expected_instances_sha256=body['expected_instances_sha256'],
                    **{k:args[k] for k in HASH_KEYS},phase=phase,instance_id=None,catalog_version=None,instances_sha256=None)
            else: proof={**previous,'phase':phase,'request_revision':PHASES[phase]}
            changes=3;expected_host=None;expected_rows=None
            if phase=='committed':
                entry=args['entry']
                if entry['mode']!='messages' or entry['profile_id']!=body['profile_id'] or entry['profile_version']!=body['profile_version']:
                    _fail('server_messages_append_entry_changed',409)
                if (db.execute('SELECT 1 FROM server_instances WHERE host_id=? AND (catalog_key=? OR profile_id=?) LIMIT 1',
                    (host['id'],entry['catalog_key'],entry['profile_id'])).fetchone() or len(self._provisioning_instances(db,host))>=16):
                    _fail('server_messages_append_identity_conflict',409)
                identifier=secrets.token_hex(16)
                db.execute("INSERT INTO server_instances VALUES(?,?,?,?,?,?,?,1,1,0,'stopped','unknown',0,0,'',NULL,NULL)",
                    (identifier,host['id'],entry['catalog_key'],entry['profile_id'],entry['profile_version'],entry['launchspec_sha256'],entry['mode']))
                db.execute('UPDATE server_hosts SET catalog_version=catalog_version+1 WHERE id=?',(host['id'],))
                host_after=self._server_host(db,actor,host['id'])
                proof.update(instance_id=identifier,catalog_version=host_after['catalog_version'],instances_sha256=catalog_fence(self._provisioning_instances(db,host_after)))
                expected_host={**dict(host),'catalog_version':body['expected_catalog_version']+1}
                expected_rows=sorted(snapshot[1]+[dict(db.execute('SELECT * FROM server_instances WHERE id=?',(identifier,)).fetchone())],key=lambda r:r['catalog_key'])
                changes+=2
            if phase=='complete':
                db.execute('DELETE FROM server_messages_append_barriers WHERE host_id=? AND request_id=?',(host['id'],row['id']));changes+=1
            validate_proof(proof)
            db.execute('UPDATE server_messages_append_requests SET revision=?,state=?,proof=? WHERE id=?',
                (PHASES[phase],phase,_json(proof),row['id']))
            result=self._messages_append_remember(db,actor,host,phase,args['idempotency_key'],args,
                _decode(self._messages_append_record(db,host,row['id']))[2],snapshot,changes,expected_host=expected_host,expected_rows=expected_rows)
            current=self._server_host(db,actor,host['id'])
            self._messages_append_authority(db,current,body,slot)
            self._messages_append_fence(db,current,body,proof)
            return result
