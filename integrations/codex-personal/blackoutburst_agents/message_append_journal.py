"""Private Host5 transition data, never a Board grant or a launch entry point."""
import copy
from functools import wraps
import hashlib
import json
import re
import sqlite3

from .host_config import HostError, digest
from .managed_host import _opaque, _sha
from .message_abandon_protocol import guard_sql, uuid_id
from .message_append_children import _AppendChildrenLease, _rows
from .message_launch_bindings import (structural_configuration, seed_legacy_anchor,
    check_binding_set, append_binding, binding_catalog)
from .work_journal import _encode

META='host_message_bindings'
TABLE='host_message_appends'
SQL={META:f'CREATE TABLE {META}(id INTEGER PRIMARY KEY CHECK(id=1),state_json TEXT NOT NULL,state_sha256 TEXT NOT NULL)',
    TABLE:f'''CREATE TABLE {TABLE}(operation_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL,
        revision INTEGER NOT NULL,phase TEXT NOT NULL,plan_sha256 TEXT NOT NULL,
        plan_json TEXT NOT NULL,proofs_json TEXT NOT NULL)'''}
BASE=('control','targets','dispatches','managed_settings','managed_targets','managed_commands','managed_effects')
LEGACY=('host_message_abandonments','host_message_dispositions')
STAGE=('host_append_stage_meta','host_append_stages')
PHASES=('intent','local_committed','instance_bound','complete')
REMOTE={'prepared':2,'precommit':3,'committed':4,'complete':5}
PROOF_KEYS=frozenset('schema operation_id request_id request_sha256 owner_id host_id device_id grant_version request_revision expected_host_version expected_catalog_version expected_instances_sha256 source_release_sha256 old_configuration_sha256 new_configuration_sha256 old_catalog_sha256 new_catalog_sha256 bindings_sha256 children_roster_sha256 phase instance_id catalog_version instances_sha256'.split())
PIN_KEYS=frozenset('source_release_sha256 old_configuration_sha256 new_configuration_sha256 old_catalog_sha256 new_catalog_sha256 bindings_sha256 children_roster_sha256'.split())
PLAN_KEYS=frozenset('schema operation_id anchor_operation_id owner_id board_endpoint host_id host_instance_id expected_host_revision old_configuration new_configuration old_catalog new_catalog old_bindings new_bindings children_roster preserved history_sha256 plan_sha256'.split())|PIN_KEYS
MAX_PLAN=16*1024*1024
MAX_RECORDS=16


def _fail(code='invalid'):
    raise HostError('messages_append_'+code)


def _closed(function):
    @wraps(function)
    def call(*args,**kwargs):
        try:return function(*args,**kwargs)
        except HostError:raise
        except Exception:raise HostError('messages_append_invalid') from None
    return call


def _encoded(value, maximum=MAX_PLAN):
    return _encode(value,maximum,'messages_append_invalid')


def _exact(a,b):
    return _encoded(a)==_encoded(b)


def _hash(value,maximum=MAX_PLAN):
    return hashlib.sha256(_encoded(value,maximum).encode('utf-8')).hexdigest()


def _decode(text, maximum=MAX_PLAN):
    if type(text) is not str or len(text.encode('utf-8'))>maximum:_fail()
    def pairs(items):
        value={}
        for k,v in items:
            if k in value:_fail()
            value[k]=v
        return value
    value=json.loads(text,object_pairs_hook=pairs,parse_constant=lambda _: _fail())
    if _encoded(value,maximum)!=text:_fail()
    return value


def validate_append_proof(value):
    if type(value) is not dict or set(value)!=PROOF_KEYS or value['schema']!='server-messages-append-proof-v1':_fail('proof')
    if type(value['phase']) is not str or value['phase'] not in REMOTE:_fail('proof')
    uuid_id(value['operation_id'])
    if type(value['request_id']) is not str or re.fullmatch('[0-9a-f]{32}',value['request_id']) is None:_fail('proof')
    for k in ('owner_id','host_id','device_id'):_opaque(value[k])
    for k in (*PIN_KEYS,'request_sha256','expected_instances_sha256'):_sha(value[k])
    for k in ('grant_version','expected_host_version','expected_catalog_version','request_revision'):
        if type(value[k]) is not int or not 1<=value[k]<2**63:_fail('proof')
    if value['request_revision']!=REMOTE[value['phase']]:_fail('proof')
    if REMOTE[value['phase']]<4:
        if any(value[k] is not None for k in ('instance_id','catalog_version','instances_sha256')):_fail('proof')
    else:
        _opaque(value['instance_id']);_sha(value['instances_sha256'])
        if type(value['catalog_version']) is not int or value['catalog_version']!=value['expected_catalog_version']+1:_fail('proof')
    return copy.deepcopy(value)


def _plan(value):
    if type(value) is not dict or set(value)!=PLAN_KEYS or value['schema']!='message-local-append-plan-v1':_fail('plan')
    uuid_id(value['operation_id'])
    if value['anchor_operation_id'] is not None:
        uuid_id(value['anchor_operation_id'])
        if value['anchor_operation_id']==value['operation_id']:_fail('plan')
    for key in ('owner_id','host_id'):_opaque(value[key])
    uuid_id(value['host_instance_id'])
    if type(value['expected_host_revision']) is not int or not 1<=value['expected_host_revision']<2**63-1:_fail('plan')
    for key in (*PIN_KEYS,'history_sha256','plan_sha256'):_sha(value[key])
    old=structural_configuration(value['old_configuration']);new=structural_configuration(value['new_configuration'])
    if old['owner_id']!=value['owner_id'] or old['board_endpoint']!=value['board_endpoint']:_fail('plan')
    r=value['source_release_sha256']
    before=check_binding_set(value['old_bindings'],current_configuration=old,expected_release_sha256=r)
    if value['anchor_operation_id'] is not None and (len(before['origins'])!=1 or before['origins'][0]['operation_id']!=value['anchor_operation_id']):_fail('plan')
    after=append_binding(before,new,previous_configuration=old,expected_release_sha256=r,
        expected_predecessor_entries=value['old_catalog'],append_operation_id=value['operation_id'])
    if not _exact(after,value['new_bindings']):_fail('plan')
    if not _exact(binding_catalog(after,current_configuration=new,expected_release_sha256=r),value['new_catalog']):_fail('plan')
    if (value['old_configuration_sha256']!=digest(old) or value['new_configuration_sha256']!=digest(new)
            or value['old_catalog_sha256']!=digest(sorted(value['old_catalog'],key=lambda e:e['catalog_key']))
            or value['new_catalog_sha256']!=digest(sorted(value['new_catalog'],key=lambda e:e['catalog_key']))
            or value['bindings_sha256']!=after['bindings_sha256']):_fail('plan')
    children=value['children_roster']
    if (type(children) is not dict or set(children)!={'schema','host_instance_id','configuration_sha256','roster_sha256'}
            or children['schema']!='message-append-children-v1' or children['host_instance_id']!=value['host_instance_id']
            or children['configuration_sha256']!=value['old_configuration_sha256']
            or children['roster_sha256']!=value['children_roster_sha256']):_fail('plan')
    if (type(value['preserved']) is not dict or not set(BASE+LEGACY)<=set(value['preserved'])
            or set(value['preserved'])-set(BASE+LEGACY+STAGE)):_fail('plan')
    if value['plan_sha256']!=_hash({k:v for k,v in value.items() if k!='plan_sha256'}):_fail('plan')
    _encoded(value)
    return copy.deepcopy(value)


def _proof_for(plan,proof,phase,first=None):
    proof=validate_append_proof(proof)
    if proof['phase']!=phase or any(proof[k]!=plan[k] for k in (*PIN_KEYS,'operation_id','owner_id','host_id')):_fail('proof')
    if first is not None:
        variable={'phase','request_revision','instance_id','catalog_version','instances_sha256'}
        if not _exact({k:v for k,v in proof.items() if k not in variable},
                {k:v for k,v in first.items() if k not in variable}):_fail('proof')
        if first['phase'] in ('committed','complete') and any(proof[k]!=first[k] for k in ('instance_id','catalog_version','instances_sha256')):_fail('proof')
    return proof


class HostMessagesAppendMixin:
    def _messages_append_version(self):
        return self._db.execute('PRAGMA main.user_version').fetchone()[0]

    def _messages_append_schema(self):
        db=self._db
        version=self._messages_append_version()
        present={r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE name IN (?,?)",(META,TABLE))}
        if version!=5:
            if present:_fail('schema')
            return
        allowed=set(BASE+LEGACY+(META,TABLE))
        tables={r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if set(STAGE)&tables:allowed.update(STAGE)
        if tables!=allowed:_fail('schema')
        guard_sql(db,allowed)
        if db.execute("SELECT 1 FROM main.sqlite_master WHERE type='view' OR (type='index' AND sql IS NOT NULL) LIMIT 1").fetchone():_fail('schema')
        for table in (META,TABLE):
            row=db.execute('SELECT type,sql FROM main.sqlite_master WHERE name=?',(table,)).fetchone()
            if row is None or tuple(row)!=('table',SQL[table]):_fail('schema')
        if db.execute('PRAGMA main.application_id').fetchone()[0]!=0x4242484A:_fail('schema')
        if db.execute(f'SELECT COUNT(*) FROM main.{META}').fetchone()[0]!=1:_fail('schema')
        for table,columns,maximum in ((META,('state_json',),5*1024*1024),(TABLE,('plan_json',),MAX_PLAN),(TABLE,('proofs_json',),32768)):
            for column in columns:
                if db.execute(f'SELECT 1 FROM main.{table} WHERE typeof({column})!=\'text\' OR length(CAST({column} AS BLOB))>? LIMIT 1',(maximum,)).fetchone():_fail('bounds')
        if db.execute(f'SELECT COUNT(*) FROM main.{TABLE}').fetchone()[0]>MAX_RECORDS:_fail('bounds')
        size=db.execute(f'SELECT COALESCE(SUM(length(CAST(plan_json AS BLOB))+length(CAST(proofs_json AS BLOB))),0) FROM main.{TABLE}').fetchone()[0]
        if size>64*1024*1024:_fail('bounds')

    def _messages_binding_state(self):
        self._messages_append_schema()
        row=self._db.execute(f'SELECT * FROM main.{META} WHERE id=1').fetchone()
        if row is None:_fail('schema')
        value=_decode(row['state_json'],5*1024*1024)
        if value.get('state_sha256')!=row['state_sha256']:_fail('state')
        return check_binding_set(value,current_configuration=self.control()['configuration'],
            expected_release_sha256=value['source_release_sha256'])

    def _check_messages_appends(self):
        self._messages_append_schema()
        if self._messages_append_version()!=5:return
        self._messages_binding_state()
        rows=self._db.execute(f'SELECT operation_id FROM main.{TABLE} ORDER BY rowid').fetchall()
        if not rows:_fail('state')
        pending=0;previous=None;latest=None
        for row in rows:
            record=self.messages_append_record(operation_id=row[0]);pending+=record['phase']!='complete'
            if previous is not None and (previous['phase']!='complete'
                    or not _exact(record['plan']['old_bindings'],previous['plan']['new_bindings'])):_fail('history')
            previous=latest=record
        if pending>1:_fail('state')
        plan=latest['plan'];committed=latest['revision']>=2
        if self._messages_append_history(plan['operation_id'])!=plan['history_sha256']:_fail('history')
        if (not _exact(self._messages_binding_state(),plan['new_bindings'] if committed else plan['old_bindings'])
                or self.managed_settings()['catalog_sha256']!=plan['new_catalog_sha256' if committed else 'old_catalog_sha256']):_fail('state')
        catalogue=plan['new_catalog'] if committed else plan['old_catalog']
        identities={r['target_id']:r['instance_id'] for r in plan['preserved']['managed_targets']}
        if committed:identities[plan['new_configuration']['targets'][-1]['id']]=latest['proofs'][2]['instance_id'] if latest['revision']>=3 else ''
        for entry in catalogue:
            target=self.managed_target(entry['catalog_key'])
            if target['launchspec_sha256']!=entry['launchspec_sha256'] or target['instance_id']!=identities[entry['catalog_key']]:_fail('state')

    @_closed
    def messages_append_record(self,*,operation_id):
        uuid_id(operation_id);self._messages_append_schema()
        if self._messages_append_version()!=5:return None
        row=self._db.execute(f'SELECT * FROM main.{TABLE} WHERE operation_id=?',(operation_id,)).fetchone()
        if row is None:return None
        record=dict(row);plan=_plan(_decode(record.pop('plan_json')));proofs=_decode(record.pop('proofs_json'),32768)
        if (record['phase'] not in PHASES or type(record['revision']) is not int
                or record['revision']!=PHASES.index(record['phase'])+1
                or record['operation_id']!=plan['operation_id'] or record['plan_sha256']!=plan['plan_sha256']
                or type(proofs) is not list or len(proofs)!=record['revision']):_fail('state')
        for index,proof in enumerate(proofs):
            _proof_for(plan,proof,tuple(REMOTE)[index],proofs[max(0,index-1)] if index else None)
            if proof['request_id']!=record['request_id']:_fail('state')
        record.update(plan=plan,proofs=proofs)
        return record

    def require_no_messages_append(self):
        self._messages_append_schema()
        if self._messages_append_version()==5:
            self._check_messages_appends()
            if self._db.execute(f"SELECT 1 FROM main.{TABLE} WHERE phase!='complete' LIMIT 1").fetchone():_fail('pending')

    @_closed
    def require_messages_append_recovery(self,operation_id):
        """Only let the real owner inspect one exact pending B on startup.

        This does not permit publication, admission or any positive effect;
        those paths retain require_no_catalog_source_transition unchanged.
        """
        self._require_lock()
        if self._owner_lock_token is None or self._messages_append_version()!=5:_fail('pending')
        self._check()
        record=self.messages_append_record(operation_id=operation_id)
        if record is None or record['phase']=='complete':_fail('pending')
        control=self.control()
        if control['enabled'] or any(self.managed_target(t['id'])['enabled'] for t in control['configuration']['targets']):_fail('requires_stopped')
        self._require_no_unsettled_abandonment()
        if any(r['state']!='complete' for r in self.catalog_source_transitions()):raise HostError('catalog_transition_pending')
        return record

    def _messages_append_effect(self,method,arguments):
        if self._messages_append_version()!=5:return
        self._messages_append_schema()
        if method in ('publish_server_catalog','replace_server_catalog'):_fail('legacy_mutation')
        if not self._db.execute(f"SELECT 1 FROM main.{TABLE} WHERE phase!='complete' LIMIT 1").fetchone():return
        if method not in ('ack_instance_command','report_instance_state'):_fail('pending')
        identifier=arguments.get('command_id');row=self.managed_command(identifier) if type(identifier) is str else None
        if row is None or row['action']!='stop' or row['state'] not in ('queued','applied'):_fail('pending')
        payload=_decode(row['payload'],4096)
        if (set(payload)!={'id','target_id','instance_id','generation','action','launchspec_sha256','budget'}
                or payload['id']!=identifier or payload['action']!='stop' or type(payload['budget']) is not int
                or payload['budget']!=0 or digest(payload)!=row['payload_sha256']
                or arguments.get('host_id')!=self.managed_settings()['host_id']):_fail('pending')
        target=self.managed_target(payload['target_id'])
        if (payload['instance_id']!=target['instance_id'] or payload['launchspec_sha256']!=target['launchspec_sha256']
                or type(payload['generation']) is not int or payload['generation']!=target['generation']):_fail('pending')
        if method=='report_instance_state' and (arguments.get('instance_id')!=target['instance_id']
                or arguments.get('generation')!=payload['generation'] or arguments.get('launchspec_sha256')!=payload['launchspec_sha256']
                or arguments.get('state') not in ('stopped','stopping','needs_review','unknown')):_fail('pending')

    def _messages_append_snapshot(self):
        db=self._db
        tables={r[0] for r in db.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        allowed=set(BASE+LEGACY+(META,TABLE)+STAGE)
        if not set(BASE)<=tables or tables-allowed or bool(set(STAGE)&tables)!=bool(set(STAGE)<=tables):_fail('schema')
        guard_sql(db,allowed)
        if db.execute("SELECT 1 FROM main.sqlite_master WHERE type='view' OR (type='index' AND sql IS NOT NULL) LIMIT 1").fetchone():_fail('schema')
        result={table:(_rows(db,table,32 if table in LEGACY+STAGE else 256) if table in tables else []) for table in BASE+LEGACY}
        for table in STAGE:
            if table in tables:result[table]=_rows(db,table,32)
        return result

    def _messages_append_history(self,operation_id):
        if self._messages_append_version()!=5:return digest([])
        self._messages_append_schema()
        rows=[_hash(dict(r),2*MAX_PLAN+65536) for r in self._db.execute(
            f'SELECT * FROM main.{TABLE} WHERE operation_id!=? ORDER BY rowid',(operation_id,))]
        return _hash(rows)

    def _messages_append_stopped(self):
        self._require_lock()
        if self._owner_lock_token is None:_fail('lock')
        self._check()
        if not self.managed() or self.control()['configuration']['schema_version']!=2:_fail('managed_required')
        control=self.control()
        if control['enabled'] or any(self.managed_target(t['id'])['enabled'] for t in control['configuration']['targets']):_fail('requires_stopped')
        for table,terminal in (('dispatches',('complete',)),('managed_commands',('applied','superseded')),('managed_effects',('complete','superseded'))):
            marks=','.join('?' for _ in terminal)
            if self._db.execute(f'SELECT 1 FROM main.{table} WHERE state NOT IN ({marks}) LIMIT 1',terminal).fetchone():_fail('unsettled')
        self._require_no_unsettled_abandonment()

    @_closed
    def propose_messages_append(self,candidate,*,operation_id,expected_host_revision,
            expected_configuration_sha256,expected_catalog_sha256,expected_release_sha256,
            children,anchor_operation_id=None):
        self._messages_append_stopped();self.require_no_messages_append()
        self._messages_children(children);uuid_id(operation_id);_sha(expected_release_sha256)
        c=self.control();old=structural_configuration(c['configuration']);candidate=structural_configuration(candidate)
        instances=[self.managed_target(t['id'])['instance_id'] for t in old['targets']]
        for instance in instances:_opaque(instance)
        if len(set(instances))!=len(instances):_fail('instance')
        if (type(expected_host_revision) is not int or expected_host_revision!=c['revision']
                or c['hash']!=expected_configuration_sha256 or self.managed_settings()['catalog_sha256']!=expected_catalog_sha256):_fail('changed')
        old_catalog=[dict(catalog_key=t['id'],profile_id=t['agent_id'],profile_version=t['profile_version'],mode='messages',
            launchspec_sha256=self.managed_target(t['id'])['launchspec_sha256']) for t in old['targets']]
        if digest(sorted(old_catalog,key=lambda e:e['catalog_key']))!=expected_catalog_sha256:_fail('changed')
        if self._messages_append_version()==5:
            if anchor_operation_id is not None:_fail('plan')
            before=self._messages_binding_state()
        else:
            uuid_id(anchor_operation_id)
            before=seed_legacy_anchor(old,expected_release_sha256=expected_release_sha256,
                expected_predecessor_entries=old_catalog,anchor_operation_id=anchor_operation_id)
        after=append_binding(before,candidate,previous_configuration=old,expected_release_sha256=expected_release_sha256,
            expected_predecessor_entries=old_catalog,append_operation_id=operation_id)
        new_catalog=binding_catalog(after,current_configuration=candidate,expected_release_sha256=expected_release_sha256)
        value=dict(schema='message-local-append-plan-v1',operation_id=operation_id,anchor_operation_id=anchor_operation_id,
            owner_id=old['owner_id'],board_endpoint=old['board_endpoint'],host_id=self.managed_settings()['host_id'],
            host_instance_id=c['instance_id'],expected_host_revision=c['revision'],source_release_sha256=expected_release_sha256,
            old_configuration=old,new_configuration=candidate,old_catalog=old_catalog,new_catalog=new_catalog,
            old_bindings=before,new_bindings=after,children_roster=children.proof,
            old_configuration_sha256=digest(old),new_configuration_sha256=digest(candidate),
            old_catalog_sha256=expected_catalog_sha256,new_catalog_sha256=digest(sorted(new_catalog,key=lambda e:e['catalog_key'])),
            bindings_sha256=after['bindings_sha256'],children_roster_sha256=children.proof['roster_sha256'],
            preserved=self._messages_append_snapshot(),history_sha256=self._messages_append_history(operation_id))
        value['plan_sha256']=_hash(value)
        return _plan(value)

    def _messages_children(self,children,plan=None):
        if type(children) is not _AppendChildrenLease or children.host is not self:_fail('children')
        proof=children.recheck()
        if plan is not None and not _exact(proof,plan['children_roster']):_fail('children')

    def _messages_append_expected(self,record):
        p=record['plan'];expected=copy.deepcopy(p['preserved']);index=record['revision']
        if index>=2:
            row=expected['control'][0]
            row.update(config=_encoded(p['new_configuration'],262144),hash=p['new_configuration_sha256'],revision=p['expected_host_revision']+1)
            target=p['new_configuration']['targets'][-1]
            entry=next(e for e in p['new_catalog'] if e['catalog_key']==target['id'])
            expected['targets'].append(dict(id=target['id'],profile_hash='',subscription='',receipt='{}'))
            expected['managed_targets'].append(dict(target_id=target['id'],instance_id=record['proofs'][2]['instance_id'] if index>=3 else '',
                launchspec_sha256=entry['launchspec_sha256'],enabled=0,activation='',generation=0,remaining=0,reason='not_enabled'))
            expected['managed_settings'][0]['catalog_sha256']=p['new_catalog_sha256']
        if not _exact(self._messages_append_snapshot(),expected):_fail('changed')
        state=self._messages_binding_state()
        if not _exact(state,p['new_bindings'] if index>=2 else p['old_bindings']):_fail('state')
        if self._messages_append_history(p['operation_id'])!=p['history_sha256']:_fail('history')

    def _messages_recheck(self,children,plan,recheck):
        self._messages_append_stopped();self._messages_children(children,plan)
        if not callable(recheck):_fail('guard')
        # The protected source/slot callback is a readonly check, including on
        # the three child journals outside this Host transaction. Refuse the
        # attempted statement before it can leave a child-side partial effect.
        denied=[]
        def readonly(action,first,second,database,trigger):
            if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_RECURSIVE):return sqlite3.SQLITE_OK
            if action==sqlite3.SQLITE_PRAGMA:
                name=first.lower()
                if name in ('table_info','table_xinfo','index_info','index_xinfo','index_list','foreign_key_list'):return sqlite3.SQLITE_OK
                # No-argument PRAGMAs may still write pages (for example
                # incremental_vacuum). Permit only required metadata reads.
                if second is None and name in ('user_version','schema_version','application_id'):return sqlite3.SQLITE_OK
            denied.append(action);return sqlite3.SQLITE_DENY
        connections=(self._db,children.messages.db,children.runs._db,children.runtime.db)
        try:
            for db in connections:db.set_authorizer(readonly)
            result=recheck()
        finally:
            for db in connections:db.set_authorizer(None)
        if result is not None or denied:_fail('guard')
        self._messages_children(children,plan)

    @_closed
    def begin_messages_append(self,plan,barrier_receipt,*,children,recheck):
        p=_plan(plan);proof=_proof_for(p,barrier_receipt,'prepared')
        with self._tx() as db:
            changes=db.total_changes;schema=db.execute('PRAGMA schema_version').fetchone()[0]
            self._messages_recheck(children,p,recheck)
            if db.total_changes!=changes or db.execute('PRAGMA schema_version').fetchone()[0]!=schema:_fail('write_set')
            prior=self.messages_append_record(operation_id=p['operation_id'])
            if prior is not None:
                if not _exact(prior['plan'],p) or not _exact(prior['proofs'][0],proof):_fail('changed')
                self._messages_append_expected(prior);return prior
            self.require_no_messages_append()
            control=self.control();settings=self.managed_settings()
            if (control['revision']!=p['expected_host_revision'] or control['hash']!=p['old_configuration_sha256']
                    or control['instance_id']!=p['host_instance_id'] or settings['host_id']!=p['host_id']
                    or settings['catalog_sha256']!=p['old_catalog_sha256']
                    or not _exact(self._messages_append_snapshot(),p['preserved'])
                    or self._messages_append_history(p['operation_id'])!=p['history_sha256']):_fail('changed')
            fresh=self._messages_append_version()!=5
            if fresh:
                from .message_abandon_journal import TABLE as A
                from .message_executed_disposition import TABLE as E
                self._abandon_schema(db,create=not bool(db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(A,)).fetchone()))
                self._executed_schema(db,create=not bool(db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(E,)).fetchone()))
                for sql in SQL.values():db.execute(sql)
                db.execute(f'INSERT INTO {META} VALUES(1,?,?)',(_encoded(p['old_bindings']),p['old_bindings']['state_sha256']))
                db.execute('PRAGMA user_version=5')
            db.execute(f'INSERT INTO {TABLE} VALUES(?,?,1,\'intent\',?,?,?)',
                (p['operation_id'],proof['request_id'],p['plan_sha256'],_encoded(p),_encoded([proof])))
            result=self.messages_append_record(operation_id=p['operation_id']);self._messages_append_expected(result)
            if db.total_changes-changes!=(2 if fresh else 1):_fail('write_set')
        return result

    def _messages_advance(self,operation_id,expected_revision,proof,phase,*,children,recheck):
        uuid_id(operation_id)
        if type(expected_revision) is not int or expected_revision not in (1,2,3):_fail('revision')
        with self._tx() as db:
            changes=db.total_changes;schema=db.execute('PRAGMA schema_version').fetchone()[0]
            record=self.messages_append_record(operation_id=operation_id)
            if record is None:_fail('absent')
            p=record['plan'];value=_proof_for(p,proof,phase,record['proofs'][-1])
            self._messages_recheck(children,p,recheck)
            if db.total_changes!=changes or db.execute('PRAGMA schema_version').fetchone()[0]!=schema:_fail('write_set')
            self._messages_append_expected(record)
            if record['revision']==expected_revision+1:
                if not _exact(record['proofs'][-1],value):_fail('changed')
                return record
            if record['revision']!=expected_revision or tuple(REMOTE)[expected_revision]!=phase:_fail('revision')
            if expected_revision==1:
                target=p['new_configuration']['targets'][-1]
                entry=next(e for e in p['new_catalog'] if e['catalog_key']==target['id'])
                db.execute('UPDATE control SET config=?,hash=?,revision=revision+1 WHERE id=1',(_encoded(p['new_configuration'],262144),p['new_configuration_sha256']))
                db.execute("INSERT INTO targets VALUES(?,'','','{}')",(target['id'],))
                db.execute("INSERT INTO managed_targets VALUES(?,'',?,0,'',0,0,'not_enabled')",(target['id'],entry['launchspec_sha256']))
                db.execute('UPDATE managed_settings SET catalog_sha256=? WHERE id=1',(p['new_catalog_sha256'],))
                db.execute(f'UPDATE {META} SET state_json=?,state_sha256=? WHERE id=1',(_encoded(p['new_bindings']),p['new_bindings']['state_sha256']))
            elif expected_revision==2:
                if value['instance_id'] in {r['instance_id'] for r in p['preserved']['managed_targets']}:_fail('instance')
                db.execute('UPDATE managed_targets SET instance_id=? WHERE target_id=?',(value['instance_id'],p['new_configuration']['targets'][-1]['id']))
            proofs=record['proofs']+[value]
            db.execute(f'UPDATE {TABLE} SET revision=revision+1,phase=?,proofs_json=? WHERE operation_id=?',
                (PHASES[expected_revision],_encoded(proofs),operation_id))
            result=self.messages_append_record(operation_id=operation_id);self._messages_append_expected(result)
            if (db.total_changes-changes!={1:6,2:2,3:1}[expected_revision]
                    or db.execute('PRAGMA schema_version').fetchone()[0]!=schema or self._messages_append_version()!=5):_fail('write_set')
        return result

    @_closed
    def commit_messages_append(self,intent_id,expected_revision,precommit_receipt,*,children,recheck):
        return self._messages_advance(intent_id,expected_revision,precommit_receipt,'precommit',children=children,recheck=recheck)

    @_closed
    def bind_messages_append_instance(self,intent_id,expected_revision,append_receipt,*,children,recheck):
        return self._messages_advance(intent_id,expected_revision,append_receipt,'committed',children=children,recheck=recheck)

    @_closed
    def complete_messages_append(self,intent_id,expected_revision,completion_receipt,*,children,recheck):
        return self._messages_advance(intent_id,expected_revision,completion_receipt,'complete',children=children,recheck=recheck)


@_closed
def effective_message_catalog(host,expected_release_sha256):
    """Installed-release pin is supplied by the real protected source caller."""
    _sha(expected_release_sha256)
    if host._messages_append_version()!=5:
        from .server_controller import launch_catalog
        return launch_catalog(host.control()['configuration'],expected_release_sha256)
    host._check_messages_appends()
    state=host._messages_binding_state()
    return binding_catalog(state,current_configuration=host.control()['configuration'],expected_release_sha256=expected_release_sha256)
