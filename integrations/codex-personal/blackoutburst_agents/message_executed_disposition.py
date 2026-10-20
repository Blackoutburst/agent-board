"""Finite, explicit local disposition of an executed blocked message.

Only lookup_execution/get_execution_control observe an already-finished Runtime.
No scheduler, registration, PID probe, model, publication, ACK or Runtime write.
The caller opens EXISTING journals and holds the Host owner lock throughout.
"""
import copy
import hashlib
import json
import math
import re
import uuid
from functools import wraps

from .host_config import HostError,digest
from .message_abandon_protocol import (ROW_FIELDS,identity,uuid_id,opaque,sha,guard_sql)
from .message_abandon_journal import HOST_TABLES as LEGACY_HOST_TABLES
from .message_admission_abandonment import MessageAdmissionAbandonment
from .message_scope import validate_scope,local_agent
from .runtime_journal import RuntimeJournal

SCHEMA='message-executed-disposition-proposal-v1'
PROOF_SCHEMA='message-executed-runstore-proof-v1'
PHASES=('intent','runtime_verified','work_disposed','message_disposed','complete')
TABLE='host_message_dispositions'
MAX_PROPOSAL=2*1024*1024
MAX_PROOF=16384
MAX_RECORDS=16
SQL=f'''CREATE TABLE {TABLE}(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
    revision INTEGER NOT NULL,phase TEXT NOT NULL,proposal_sha256 TEXT NOT NULL,
    proposal_json TEXT NOT NULL,proof_json TEXT NOT NULL,checkpoints_json TEXT NOT NULL)'''
COLUMNS=('id','request_key','revision','phase','proposal_sha256','proposal_json','proof_json','checkpoints_json')
HOST_TABLES=(*LEGACY_HOST_TABLES,TABLE)
CORE_FIELDS=frozenset(('id revision state owner_id agent_id device_id profile_version session_id scope_kind '
 'thread_id message_id source_sha256 work_id turn_id provider model outcome reason cleanup_confirmed').split())
WIRE_EXTRAS=frozenset(('project_id ticket_id stop_requested should_stop control_reason created_at updated_at '
 'started_at last_heartbeat_at finished_at stale').split())
MESSAGE_REASON='message_executed_blocked_disposed'


def encoded(value,maximum=MAX_PROPOSAL):
    try:
        out=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        if len(out.encode())>maximum:raise ValueError
        return out
    except Exception:raise HostError('message_executed_invalid') from None


def hashed(value):return hashlib.sha256(encoded(value).encode()).hexdigest()
def exact(a,b):return encoded(a)==encoded(b)
def raw_sha(value):
    if type(value) is not str:raise HostError('message_executed_invalid')
    return hashlib.sha256(value.encode()).hexdigest()
def decode(value,maximum=MAX_PROPOSAL):
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError
            out[k]=v
        return out
    try:
        if type(value) is not str or len(value.encode())>maximum:raise ValueError
        out=json.loads(value,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
        if encoded(out,maximum)!=value:raise ValueError
        return out
    except Exception:raise HostError('message_executed_corrupt') from None


def closed(method):
    @wraps(method)
    def call(*args,**kwargs):
        try:return method(*args,**kwargs)
        except HostError as error:
            if error.code.startswith('message_executed_'):raise
            raise HostError('message_executed_conflict') from None
        except Exception:raise HostError('message_executed_conflict') from None
    return call


def terminal_core(value,*,wire=False):
    if (type(value) is not dict or not CORE_FIELDS<=set(value)
            or (set(value)-CORE_FIELDS if not wire else set(value)-CORE_FIELDS-WIRE_EXTRAS)):
        raise HostError('message_executed_proof')
    core={k:value[k] for k in CORE_FIELDS}
    for k in CORE_FIELDS-{'revision','profile_version','cleanup_confirmed'}:
        if type(core[k]) is not str or not core[k] or len(core[k].encode())>512 or any(ord(c)<32 for c in core[k]):
            raise HostError('message_executed_proof')
    if (any(type(core[k]) is not int or not 1<=core[k]<2**63 for k in ('revision','profile_version'))
            or core['state']!='finished' or core['scope_kind']!='message' or core['outcome']!='blocked'
            or core['reason']!='protocol_error' or type(core['cleanup_confirmed']) is not bool
            or core['cleanup_confirmed'] is not True):raise HostError('message_executed_proof')
    for k in ('work_id','turn_id'):uuid_id(core[k])
    sha(core['source_sha256'])
    if wire:
        for k in ('stop_requested','should_stop','stale'):
            if k in value and type(value[k]) is not bool:raise HostError('message_executed_proof')
        for k in ('created_at','updated_at','started_at','last_heartbeat_at','finished_at'):
            if k in value and value[k] is not None and (type(value[k]) not in (int,float) or not math.isfinite(value[k])):
                raise HostError('message_executed_proof')
        if 'control_reason' in value and value['control_reason']!='finished':raise HostError('message_executed_proof')
        for k in ('project_id','ticket_id'):
            if k in value and value[k] is not None and type(value[k]) is not str:raise HostError('message_executed_proof')
    return core


def validate_proof(p,value,intent_id):
    if (type(value) is not dict or set(value)!={'schema','intent_id','state_sha256','result_sha256',
            'configuration','identity','local_runtime','lookup','control'} or value['schema']!=PROOF_SCHEMA):
        raise HostError('message_executed_proof')
    uuid_id(intent_id)
    rt=p['runtime'];local={'id':rt['id'],'key':rt['key'],'phase':'finished','revision':rt['revision'],
        'start_confirmed':True,'receipt':terminal_core(rt['receipt'],wire=True)}
    if (value['intent_id']!=intent_id or value['state_sha256']!=hashed({'work':p['work'],'turn':p['turn']})
            or value['result_sha256']!=raw_sha(p['turn']['result_json'])
            or not exact(value['configuration'],rt['configuration']) or not exact(value['identity'],p['identity'])
            or not exact(value['local_runtime'],local)):
        raise HostError('message_executed_proof')
    if not exact(terminal_core(value['lookup']),local['receipt']) or not exact(terminal_core(value['control']),local['receipt']):
        raise HostError('message_executed_proof')
    encoded(value,MAX_PROOF)
    return copy.deepcopy(value)


def proposal(value):
    fields={'schema','request_key','identity','host','dispatch','message','reader','delivery','work','turn','runtime','proposal_sha256'}
    if type(value) is not dict or set(value)!=fields or value['schema']!=SCHEMA:raise HostError('message_executed_invalid')
    p=decode(encoded(value));body={k:v for k,v in p.items() if k!='proposal_sha256'}
    if p['proposal_sha256']!=hashed(body):raise HostError('message_executed_invalid')
    opaque(p['request_key']);identity(p['identity'])
    if (type(p['host']) is not dict or set(p['host'])!={'instance_id','configuration_sha256','catalog_sha256','revision','target_id','fingerprint'}
            or type(p['message']) is not dict or set(p['message'])!={'row','payload_sha256','scope','configuration_sha256','context_sha256'}):
        raise HostError('message_executed_invalid')
    for name,fields in ROW_FIELDS.items():
        row=p['message']['row'] if name=='message_row' else p[name]
        if type(row) is not dict or set(row)!=fields:raise HostError('message_executed_invalid')
    h=p['host'];m=p['message'];d=p['dispatch'];r=p['reader'];delivery=p['delivery'];w=p['work'];t=p['turn'];rt=p['runtime'];ident=p['identity'];row=m['row']
    for v in (h['configuration_sha256'],h['catalog_sha256'],h['fingerprint'],m['payload_sha256'],m['configuration_sha256'],m['context_sha256'],row['message_hash'],r['configuration_hash'],delivery['payload_hash'],t['prompt_sha256']):sha(v)
    for v in (h['instance_id'],d['batch_id'],d['worker_id'],r['id'],delivery['id'],row['id'],w['id'],t['id'],rt['id']):uuid_id(v)
    if any(type(v) is not int or not 1<=v<2**63 for v in (h['revision'],row['revision'],rt['revision'],r['page_limit'])):raise HostError('message_executed_invalid')
    scope=validate_scope(m['scope']);config=RuntimeJournal.configuration(scope,w['id'],t['id'],w['model'])
    if (not exact(config,rt['configuration']) or rt['key']!=RuntimeJournal.key(config) or rt['phase']!='finished'
            or type(rt['start_confirmed']) is not int or rt['start_confirmed']!=1):raise HostError('message_executed_invalid')
    core=terminal_core(rt['receipt'],wire=True)
    expected={**{k:scope[k] for k in ('owner_id','agent_id','scope_kind','thread_id','message_id','source_sha256')},
        'session_id':scope['board_session_id'],**{k:config[k] for k in ('work_id','turn_id','provider','model')},
        **{k:ident[k] for k in ('device_id','profile_version')}}
    if any(not exact(core[k],v) for k,v in expected.items()):raise HostError('message_executed_invalid')
    if any(scope[k]!=ident[k] for k in ('owner_id','agent_id')) or scope['board_session_id']!=ident['session_id']:raise HostError('message_executed_invalid')
    if (w['owner_id']!=ident['owner_id'] or w['agent_id']!=local_agent(scope) or w['project_id']!=scope['thread_id']
            or w['ticket_id']!=scope['message_id'] or w['status']!='needs_review'
            or type(t['pid']) is not int or t['pid']<1 or t['work_id']!=w['id'] or t['status']!='blocked'
            or type(t['requires_review']) is not int or t['requires_review']!=1
            or type(t['cancel_requested']) is not int or t['cancel_requested']!=0
            or t['resolution'] is not None or t['resolution_reason'] is not None):raise HostError('message_executed_invalid')
    uuid_id(w['provider_session_id']);uuid_id(t['provider_session_id'])
    if w['provider_session_id']!=t['provider_session_id']:raise HostError('message_executed_invalid')
    def result_pairs(items):
        out={}
        for key,item in items:
            if key in out:raise ValueError
            out[key]=item
        return out
    try:
        if type(t['result_json']) is not str or len(t['result_json'].encode())>1024*1024:raise ValueError
        result=json.loads(t['result_json'],object_pairs_hook=result_pairs,
            parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
    except Exception:raise HostError('message_executed_invalid') from None
    flags={'cleanup_confirmed':True,'exit_zero':False,'session_verified':True,'tool_free':True,'turn_completed':False}
    if (type(result) is not dict or result.get('status')!='blocked' or result.get('reason')!='protocol_error'
            or result.get('session_id')!=w['provider_session_id'] or type(result.get('verification')) is not dict
            or any(type(result['verification'].get(k)) is not bool or result['verification'][k] is not v for k,v in flags.items())
            or result.get('cleanup_failed') is True):raise HostError('message_executed_invalid')
    for obj in (w,t,rt):
        if any(type(obj[k]) not in (int,float) or not math.isfinite(obj[k]) for k in ('created_at','updated_at')):raise HostError('message_executed_invalid')
    if (type(r['checkpoint']) is not int or r['checkpoint']<0 or type(delivery['checkpoint']) is not int
            or delivery['checkpoint']!=r['checkpoint'] or r['start_at']!='now' or not 1<=r['page_limit']<=200
            or re.fullmatch('[0-9a-f]{32}',r['feed_id']) is None
            or r['opening_key']!='host-message-feed-'+r['id'] or r['target_id']!=h['target_id']
            or delivery['reader_id']!=r['id'] or delivery['id']!=row['delivery_id'] or delivery['state']!='pending' or delivery['ack_json']!='{}'
            or row['state']!='needs_review' or row['id']!=d['batch_id'] or row['work_id']!=w['id']
            or row['worker_id']!=d['worker_id'] or row['thread_id']!=scope['thread_id'] or row['message_id']!=scope['message_id']
            or d['target_id']!=h['target_id'] or d['state']!='needs_review' or d['subscription_id']!=r['id']
            or d['request_id']!='host-message-'+row['id'] or t['request_id']!='host-message-'+d['worker_id']
            or not exact(d['receipt'],{'status':'needs_review','reason':'message_result_requires_review',
                'worker_operation_id':d['worker_id'],'message_operation_id':d['batch_id']})):
        raise HostError('message_executed_invalid')
    return p


class HostExecutedDispositionMixin:
    """Host4 ledger only. HostJournal/manager gates are wired by the integrator."""
    def _executed_schema(self,db,*,create=False):
        guard_sql(db,HOST_TABLES)
        if db.execute('PRAGMA application_id').fetchone()[0]!=0x4242484A:raise HostError('message_executed_schema')
        row=db.execute('SELECT type,sql FROM main.sqlite_master WHERE name=?',(TABLE,)).fetchone()
        if row is None and create:
            db.execute(SQL);row=db.execute('SELECT type,sql FROM main.sqlite_master WHERE name=?',(TABLE,)).fetchone()
        if row is None or tuple(row)!=('table',SQL):raise HostError('message_executed_schema')
        if tuple(r['name'] for r in db.execute('PRAGMA main.table_info('+TABLE+')'))!=COLUMNS:raise HostError('message_executed_schema')
        bad=db.execute(f'''SELECT 1 FROM main.{TABLE} WHERE typeof(proposal_json)!='text'
            OR length(CAST(proposal_json AS BLOB))>? OR typeof(proof_json)!='text'
            OR length(CAST(proof_json AS BLOB))>? OR typeof(checkpoints_json)!='text'
            OR length(CAST(checkpoints_json AS BLOB))>1024 LIMIT 1''',(MAX_PROPOSAL,MAX_PROOF)).fetchone()
        if bad or len(db.execute(f'SELECT id FROM main.{TABLE} LIMIT ?', (MAX_RECORDS+1,)).fetchall())>MAX_RECORDS:
            raise HostError('message_executed_corrupt')

    def _check_executed_dispositions(self):
        version=self._db.execute('PRAGMA user_version').fetchone()[0]
        exists=self._db.execute('SELECT 1 FROM main.sqlite_master WHERE name=?',(TABLE,)).fetchone()
        if version!=4:
            if exists:raise HostError('message_executed_schema')
            return
        self._executed_schema(self._db)
        for row in self._db.execute(f'SELECT id FROM main.{TABLE}'):
            self._executed_record(intent_id=row['id'])

    def _require_no_unsettled_executed_disposition(self):
        self._check_executed_dispositions()
        if self._db.execute('PRAGMA user_version').fetchone()[0]==4 and self._db.execute(f"SELECT 1 FROM main.{TABLE} WHERE phase!='complete' LIMIT 1").fetchone():
            raise HostError('message_executed_unsettled')

    def _executed_record(self,*,request_key=None,intent_id=None):
        if (request_key is None)==(intent_id is None):raise HostError('message_executed_invalid')
        if self._db.execute('PRAGMA user_version').fetchone()[0]!=4:return None
        self._executed_schema(self._db)
        row=self._db.execute(f'SELECT * FROM main.{TABLE} WHERE '+('request_key' if request_key is not None else 'id')+'=?',
            (request_key if request_key is not None else intent_id,)).fetchone()
        if row is None:return None
        value=dict(row);uuid_id(value['id']);p=proposal(decode(value['proposal_json']))
        if (p['request_key']!=value['request_key'] or p['proposal_sha256']!=value['proposal_sha256']
                or type(value['revision']) is not int or value['phase'] not in PHASES
                or value['revision']!=PHASES.index(value['phase'])+1):raise HostError('message_executed_corrupt')
        proof=decode(value['proof_json'],MAX_PROOF);checks=decode(value['checkpoints_json'],1024);index=PHASES.index(value['phase'])
        if type(checks) is not dict or set(checks)!=set(('runtime','work','message','host')[:index]):raise HostError('message_executed_corrupt')
        for v in checks.values():sha(v)
        if index==0:
            if proof is not None:raise HostError('message_executed_corrupt')
        else:validate_proof(p,proof,value['id'])
        value.update(proposal=p,proof=proof,checkpoints=checks)
        return value

    def _executed_fingerprint(self,target_id,batch_id):
        # Existing exact Host fingerprint deliberately excludes the intent table.
        return self._abandon_host_fingerprint(target_id,batch_id)

    def _executed_create(self,p):
        self._require_lock();p=proposal(p)
        with self._tx() as db:
            guard_sql(db,HOST_TABLES)
            if self._executed_record(request_key=p['request_key']) is not None:raise HostError('message_executed_exists')
            if self._executed_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])!=p['host']['fingerprint']:raise HostError('message_executed_conflict')
            self._check_abandonments();self._require_no_unsettled_abandonment();self._check_executed_dispositions()
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (2,3,4):raise HostError('message_executed_schema')
            # Host4 retains the empty legacy ledger even when this Host2 never used seal47.
            self._abandon_schema(db,create=version==2)
            self._executed_schema(db,create=version!=4)
            if db.execute(f"SELECT 1 FROM main.{TABLE} WHERE phase!='complete' OR json_extract(proposal_json,'$.dispatch.batch_id')=? LIMIT 1",(p['dispatch']['batch_id'],)).fetchone():raise HostError('message_executed_unsettled')
            if len(db.execute(f'SELECT id FROM main.{TABLE} LIMIT ?', (MAX_RECORDS,)).fetchall())>=MAX_RECORDS:raise HostError('message_executed_full')
            schema_version=db.execute('PRAGMA schema_version').fetchone()[0]
            changes=db.total_changes;identifier=str(uuid.uuid4())
            db.execute(f"INSERT INTO main.{TABLE} VALUES(?,?,1,'intent',?,?,'null','{{}}')",(identifier,p['request_key'],p['proposal_sha256'],encoded(p)))
            db.execute('PRAGMA user_version=4')
            self._executed_record(intent_id=identifier);self._executed_schema(db)
            if (db.total_changes-changes!=1 or db.execute('PRAGMA schema_version').fetchone()[0]!=schema_version
                    or db.execute('PRAGMA user_version').fetchone()[0]!=4):raise HostError('message_executed_conflict')
        return self._executed_record(intent_id=identifier)

    def _executed_advance(self,record,phase,*,proof=None,checkpoints=None):
        self._require_lock()
        with self._tx() as db:
            guard_sql(db,HOST_TABLES);changes=db.total_changes
            schema_version=db.execute('PRAGMA schema_version').fetchone()[0]
            current=self._executed_record(intent_id=record['id'])
            if not exact(current,record) or PHASES.index(phase)!=PHASES.index(current['phase'])+1:raise HostError('message_executed_conflict')
            p=current['proposal']
            if self._executed_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])!=p['host']['fingerprint']:raise HostError('message_executed_conflict')
            actual=validate_proof(p,current['proof'] if proof is None else proof,current['id'])
            checks=dict(current['checkpoints'] if checkpoints is None else checkpoints)
            if phase=='complete':
                receipt=disposition_receipt(p,current['id'],actual)
                db.execute("UPDATE dispatches SET state='complete',receipt=? WHERE batch_id=?",(encoded(receipt,8192),p['dispatch']['batch_id']))
                db.execute('UPDATE targets SET receipt=? WHERE id=?',(encoded(receipt,8192),p['host']['target_id']))
                checks['host']=self._executed_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])
            db.execute(f'UPDATE main.{TABLE} SET revision=revision+1,phase=?,proof_json=?,checkpoints_json=? WHERE id=?',
                (phase,encoded(actual,MAX_PROOF),encoded(checks,1024),current['id']))
            after=self._executed_record(intent_id=current['id']);self._executed_schema(db)
            if (db.total_changes-changes!=(3 if phase=='complete' else 1)
                    or db.execute('PRAGMA schema_version').fetchone()[0]!=schema_version
                    or db.execute('PRAGMA user_version').fetchone()[0]!=4):raise HostError('message_executed_conflict')
        return after


def disposition_receipt(p,intent_id,proof):
    return {'status':'executed_disposed','reason':MESSAGE_REASON,'worker_operation_id':p['dispatch']['worker_id'],
        'message_operation_id':p['dispatch']['batch_id'],'disposition_intent_id':intent_id,'proof_sha256':hashed(proof)}


class MessageExecutedDisposition:
    def __init__(self,host,messages,runs,runtime,*,observer):
        self.host,self.messages,self.runs,self.runtime=host,messages,runs,runtime
        self.observer=observer
        if (not callable(getattr(observer,'call',None)) or messages.directory!=host.directory/'messages'
                or runs.state_dir!=host.directory/'message-runs' or runtime.directory!=host.directory/'message-runtime'):
            raise HostError('message_executed_journal_identity')

    def _guard(self,*,allow_host_tx=False):
        self.host._require_lock()
        if ((self.host._db.in_transaction and not allow_host_tx)
                or any(db.in_transaction for db in (self.messages.db,self.runs._db,self.runtime.db))):
            raise HostError('message_executed_transaction_active')
        self.host._check_abandonments();self.host._check_executed_dispositions()
        guard_sql(self.host._db,HOST_TABLES)
        guard_sql(self.messages.db,('readers','deliveries','operations'))
        guard_sql(self.runs._db,('works','turns','context_guards'))
        guard_sql(self.runtime.db,('executions',))
        for db,versions,app in ((self.host._db,(2,3,4),0x4242484A),(self.messages.db,(1,2,3),0x42424D46),
                (self.runs._db,(2,),0),(self.runtime.db,(1,2,3,4),0x42425254)):
            if db.execute('PRAGMA user_version').fetchone()[0] not in versions or db.execute('PRAGMA application_id').fetchone()[0]!=app:
                raise HostError('message_executed_schema')

    def _target(self,p):
        control=self.host.control();h=p['host'];ident=p['identity']
        target=next((t for t in control['configuration']['targets'] if t['id']==h['target_id']),None)
        if (target is None or control['hash']!=h['configuration_sha256'] or control['revision']!=h['revision']
                or control['instance_id']!=h['instance_id'] or control['configuration']['owner_id']!=ident['owner_id']
                or self.host.managed_settings()['catalog_sha256']!=h['catalog_sha256']
                or target['agent_id']!=ident['agent_id'] or type(target['profile_version']) is not int
                or target['profile_version']!=ident['profile_version'] or target['board_session_id']!=ident['session_id']
                or self.observer.session_id!=ident['session_id'] or self.observer.endpoint!=control['configuration']['board_endpoint']
                or target['start_at']!='now' or digest(target)!=p['message']['configuration_sha256']
                or any(not exact(p['work'][k],target[k]) for k in ('workspace','executable','model'))
                or p['message']['scope']['board_endpoint']!=control['configuration']['board_endpoint']):
            raise HostError('message_executed_identity')
        expected_reader=digest({'target':target,'owner_id':ident['owner_id'],
            'board_endpoint':control['configuration']['board_endpoint'],'host_instance':h['instance_id']})
        if p['reader']['configuration_hash']!=expected_reader or p['reader']['page_limit']!=control['configuration']['page_limit']:
            raise HostError('message_executed_identity')
        return target

    def _rows(self,p,*,allow_host_tx=False):
        self._guard(allow_host_tx=allow_host_tx);w=p['work']['id'];turn=p['turn']['id']
        turns=[dict(r) for r in self.runs._db.execute('SELECT * FROM turns WHERE work_id=? LIMIT 2',(w,))]
        if len(turns)!=1 or turns[0]['id']!=turn or self.runs._db.execute('SELECT 1 FROM context_guards WHERE work_id=?',(w,)).fetchone():
            raise HostError('message_executed_work_conflict')
        reader=self.messages.db.execute('SELECT * FROM readers WHERE id=?',(p['reader']['id'],)).fetchone()
        delivery=self.messages.db.execute('SELECT * FROM deliveries WHERE id=?',(p['delivery']['id'],)).fetchone()
        rt=self.runtime.db.execute("SELECT id FROM executions WHERE json_extract(configuration,'$.work_id')=? OR json_extract(configuration,'$.turn_id')=? LIMIT 2",(w,turn)).fetchall()
        if reader is None or delivery is None or len(rt)!=1 or rt[0]['id']!=p['runtime']['id']:
            raise HostError('message_executed_conflict')
        return {'runtime':self.runtime.get(rt[0]['id']),'work':{'work':self.runs._work(w),'turn':turns[0]},
            'message':self.messages.operation(p['message']['row']['id']),'reader':dict(reader),'delivery':dict(delivery)}

    @staticmethod
    def _work_after(p,intent_id,current):
        work={**p['work'],'status':'closed'}
        turn={**p['turn'],'resolution':'abandon','resolution_reason':MESSAGE_REASON+':'+intent_id}
        for key,expected,original in (('work',work,p['work']),('turn',turn,p['turn'])):
            value=current[key]
            if set(value)!=set(expected) or not exact({k:v for k,v in value.items() if k!='updated_at'},
                    {k:v for k,v in expected.items() if k!='updated_at'}):return False
            if type(value['updated_at']) not in (int,float) or not math.isfinite(value['updated_at']) or value['updated_at']<original['updated_at']:return False
        return exact(current['work']['updated_at'],current['turn']['updated_at'])

    @staticmethod
    def _message_after(p,intent_id,proof,current):
        row={k:v for k,v in current.items() if k!='payload'}
        expected={**p['message']['row'],'state':'executed_disposed','revision':p['message']['row']['revision']+1}
        payload=dict(current['payload']);marker=payload.pop('disposition',None)
        return (exact(row,expected) and exact(marker,{'schema':'message-executed-disposition-v1','intent_id':intent_id,
            'proof_sha256':hashed(proof),'original_payload_sha256':p['message']['payload_sha256'],'reason':MESSAGE_REASON})
            and hashed(payload)==p['message']['payload_sha256'])

    def _verify(self,record,*,allow_host_tx=False):
        p=record['proposal'];target=self._target(p);rows=self._rows(p,allow_host_tx=allow_host_tx);index=PHASES.index(record['phase'])
        payload=dict(rows['message']['payload'])
        if rows['message']['state']=='executed_disposed':payload.pop('disposition',None)
        if payload.get('reason')!='message_result_requires_review':raise HostError('message_executed_context')
        MessageAdmissionAbandonment._saved_context(p,payload,target)
        host_expected=p['host']['fingerprint'] if index<4 else record['checkpoints']['host']
        if self.host._executed_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])!=host_expected:
            raise HostError('message_executed_conflict')
        if not exact(rows['reader'],p['reader']) or not exact(rows['delivery'],p['delivery']):
            reader={**p['reader'],'checkpoint':p['reader']['checkpoint']+1}
            ack={'feed_id':reader['feed_id'],'delivery_id':p['delivery']['remote_id'],'checkpoint':reader['checkpoint'],'acknowledged':True}
            acking={**p['delivery'],'state':'acking'};acked={**p['delivery'],'state':'acked','ack_json':json.dumps(ack,sort_keys=True)}
            if index!=4 or not ((exact(rows['reader'],p['reader']) and exact(rows['delivery'],acking))
                    or (exact(rows['reader'],reader) and exact(rows['delivery'],acked))):raise HostError('message_executed_delivery_conflict')
        if not exact(rows['runtime'],p['runtime']):raise HostError('message_executed_runtime_conflict')
        if index>=1 and record['checkpoints']['runtime']!=hashed(rows['runtime']):raise HostError('message_executed_runtime_conflict')
        originals={'work':exact(rows['work'],{'work':p['work'],'turn':p['turn']}),
            'message':exact({k:v for k,v in rows['message'].items() if k!='payload'},p['message']['row'])
                and hashed(rows['message']['payload'])==p['message']['payload_sha256']}
        after={'work':False,'message':False}
        if record['proof'] is not None:
            validate_proof(p,record['proof'],record['id'])
            after={'work':self._work_after(p,record['id'],rows['work']),
                'message':self._message_after(p,record['id'],record['proof'],rows['message'])}
        for key,threshold in (('work',2),('message',3)):
            if index>=threshold:
                if not after[key] or record['checkpoints'][key]!=hashed(rows[key]):raise HostError('message_executed_conflict')
            elif index==threshold-1:
                if not originals[key] and not after[key]:raise HostError('message_executed_conflict')
            elif not originals[key]:raise HostError('message_executed_conflict')
        if index==4:
            actual=next((d for d in self.host.dispatches() if d['batch_id']==p['dispatch']['batch_id']),None)
            if not exact(actual,{**p['dispatch'],'state':'complete','receipt':disposition_receipt(p,record['id'],record['proof'])}):raise HostError('message_executed_conflict')
        return rows,after

    @closed
    def propose(self,*,request_key,target_id,batch_id,worker_id,work_id,turn_id,identity:dict,
                expected_config_sha256,expected_host_instance):
        self._guard();opaque(request_key);opaque(target_id)
        for v in (batch_id,worker_id,work_id,turn_id):uuid_id(v)
        from .message_abandon_protocol import identity as validate_identity
        pinned=validate_identity(identity)
        if self.host._executed_record(request_key=request_key) is not None:raise HostError('message_executed_exists')
        control=self.host.control()
        if control['hash']!=expected_config_sha256 or control['instance_id']!=expected_host_instance:raise HostError('message_executed_identity')
        dispatch=next((d for d in self.host.dispatches() if d['batch_id']==batch_id),None)
        if dispatch is None or dispatch['target_id']!=target_id or dispatch['worker_id']!=worker_id:raise HostError('message_executed_identity')
        with self.runs.work_lock(work_id,inspection_only=True):
            self._guard();op=self.messages.operation(batch_id)
            turns=[dict(t) for t in self.runs._db.execute('SELECT * FROM turns WHERE work_id=? LIMIT 2',(work_id,))]
            reader=self.messages.db.execute('SELECT * FROM readers WHERE id=?',(dispatch['subscription_id'],)).fetchone()
            delivery=self.messages.db.execute('SELECT * FROM deliveries WHERE id=?',(op['delivery_id'],)).fetchone()
            rt=self.runtime.db.execute("SELECT id FROM executions WHERE json_extract(configuration,'$.work_id')=? OR json_extract(configuration,'$.turn_id')=? LIMIT 2",(work_id,turn_id)).fetchall()
            if op['work_id']!=work_id or len(turns)!=1 or turns[0]['id']!=turn_id or len(rt)!=1 or reader is None or delivery is None:raise HostError('message_executed_conflict')
            payload=op['payload']
            if set(payload)!={'scope','prompt','context_sha256','context_refs','configuration_sha256','reply_key','reason'}:raise HostError('message_executed_context')
            if (type(payload['prompt']) is not str or len(payload['prompt'].encode())>60000
                    or raw_sha(payload['prompt'])!=turns[0]['prompt_sha256']):raise HostError('message_executed_context')
            if payload['reply_key']!='host-message-reply-'+digest({'endpoint':payload['scope']['board_endpoint'],
                    'owner_id':pinned['owner_id'],'agent_id':pinned['agent_id'],'thread_id':op['thread_id'],'message_id':op['message_id']}):
                raise HostError('message_executed_context')
            p={'schema':SCHEMA,'request_key':request_key,'identity':pinned,'host':{
                'instance_id':control['instance_id'],'configuration_sha256':control['hash'],
                'catalog_sha256':self.host.managed_settings()['catalog_sha256'],'revision':control['revision'],
                'target_id':target_id,'fingerprint':self.host._executed_fingerprint(target_id,batch_id)},
                'dispatch':dispatch,'message':{'row':{k:v for k,v in op.items() if k!='payload'},
                    'payload_sha256':hashed(payload),'scope':validate_scope(payload['scope']),
                    'configuration_sha256':payload['configuration_sha256'],'context_sha256':payload['context_sha256']},
                'reader':dict(reader),'delivery':dict(delivery),'work':self.runs._work(work_id),'turn':turns[0],
                'runtime':self.runtime.get(rt[0]['id'])}
            p['proposal_sha256']=hashed(p);p=proposal(p)
            self._verify({'id':str(uuid.uuid4()),'proposal':p,'phase':'intent','proof':None,'checkpoints':{}})
            return p

    @closed
    def proposal(self,request_key):
        self._guard(allow_host_tx=True);opaque(request_key);record=self.host._executed_record(request_key=request_key)
        if record is None:raise HostError('message_executed_absent')
        return proposal(record['proposal'])

    @closed
    def status(self,*,request_key=None,intent_id=None):
        self._guard(allow_host_tx=True);record=self.host._executed_record(request_key=request_key,intent_id=intent_id)
        if record is None:raise HostError('message_executed_absent')
        work=record['proposal']['work']['id']
        if work in self.runs._locks:self._verify(record,allow_host_tx=True)
        else:
            with self.runs.work_lock(work,inspection_only=True):self._verify(record,allow_host_tx=True)
        return {'intent_id':record['id'],'revision':record['revision'],'phase':record['phase'],'reason':'message_executed_'+record['phase']}

    def _observe(self,record):
        p=record['proposal'];rt=p['runtime'];self._verify(record)
        # Both calls are read-only, outside every SQL transaction, on the original
        # endpoint/session. The trusted caller supplies the narrow concrete client.
        lookup=self.observer.call('lookup_execution',{'idempotency_key':rt['key']})
        self._verify(record)
        if type(lookup) is not dict or set(lookup)!={'found','execution'} or lookup['found'] is not True:
            raise HostError('message_executed_observation_unconfirmed')
        seen=terminal_core(lookup['execution'],wire=True)
        expected=terminal_core(rt['receipt'],wire=True)
        if not exact(seen,expected):raise HostError('message_executed_observation_unconfirmed')
        control=self.observer.call('get_execution_control',{'execution_id':expected['id']})
        self._verify(record)
        actual=terminal_core(control,wire=True)
        if not exact(actual,expected):raise HostError('message_executed_observation_unconfirmed')
        value={'schema':PROOF_SCHEMA,'intent_id':record['id'],
            'state_sha256':hashed({'work':p['work'],'turn':p['turn']}),'result_sha256':raw_sha(p['turn']['result_json']),
            'configuration':p['runtime']['configuration'],'identity':p['identity'],
            'local_runtime':{'id':rt['id'],'key':rt['key'],'phase':'finished','revision':rt['revision'],
                'start_confirmed':True,'receipt':expected},'lookup':seen,'control':actual}
        return validate_proof(p,value,record['id'])

    @closed
    def apply(self,value,*,expected_revision=0,retry_observation=False):
        self._guard();p=proposal(value)
        if type(expected_revision) is not int or expected_revision<0 or type(retry_observation) is not bool:raise HostError('message_executed_invalid')
        with self.runs.work_lock(p['work']['id'],inspection_only=True):
            record=self.host._executed_record(request_key=p['request_key']);fresh=record is None
            if fresh:
                if expected_revision!=0 or retry_observation:raise HostError('message_executed_conflict')
                self._verify({'id':str(uuid.uuid4()),'proposal':p,'phase':'intent','proof':None,'checkpoints':{}})
                record=self.host._executed_create(p)
            elif not exact(record['proposal'],p) or record['revision']!=expected_revision:raise HostError('message_executed_conflict')
            self._verify(record)
            if record['phase']=='intent':
                if not fresh and not retry_observation:raise HostError('message_executed_retry_required')
                try:actual=self._observe(record)
                except Exception:raise HostError('message_executed_observation_unconfirmed') from None
                self._verify(record)
                record=self.host._executed_advance(record,'runtime_verified',proof=actual,
                    checkpoints={'runtime':hashed(p['runtime'])})
            while record['phase']!='complete':
                rows,after=self._verify(record);checks=dict(record['checkpoints']);phase=record['phase']
                if phase=='runtime_verified':
                    if not after['work']:
                        self.runs._dispose_executed_blocked(p['work']['id'],expected_state_sha256=record['proof']['state_sha256'],
                            intent_id=record['id'],proof=record['proof'])
                    rows,after=self._verify(record);checks['work']=hashed(rows['work']);next_phase='work_disposed'
                elif phase=='work_disposed':
                    if not after['message']:
                        self.messages._dispose_executed_blocked(p['message']['row'],payload_sha256=p['message']['payload_sha256'],
                            intent_id=record['id'],proof_sha256=hashed(record['proof']))
                    rows,after=self._verify(record);checks['message']=hashed(rows['message']);next_phase='message_disposed'
                elif phase=='message_disposed':next_phase='complete'
                else:raise HostError('message_executed_corrupt')
                record=self.host._executed_advance(record,next_phase,checkpoints=checks)
            return self.status(intent_id=record['id'])

    @closed
    def resume(self,intent_id,*,expected_revision,retry_observation=False):
        self._guard();uuid_id(intent_id);record=self.host._executed_record(intent_id=intent_id)
        if record is None:raise HostError('message_executed_absent')
        return self.apply(record['proposal'],expected_revision=expected_revision,retry_observation=retry_observation)
