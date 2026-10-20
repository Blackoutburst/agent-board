"""Closed private administrative proposal; no network or recovery entry point."""
import copy
import hashlib
import json
import re

from .host_config import HostError
from .work_journal import _encode
from .message_admission_seal import validate_seal

SCHEMA='message-local-abandonment-v1'
PHASES=('intent','sealed','runtime_abandoned','work_abandoned','message_abandoned','complete')
LEGACY_RESULT={'status':'blocked','reason':'runtime_start_requires_review','text':'','uncertain':True,
    'verification':{'cleanup_confirmed':True}}
MESSAGE_REASON='message_admission_sealed_abandoned'


def encoded(value,maximum=32768):
    try:return _encode(value,maximum,'message_abandon_invalid')
    except Exception:raise HostError('message_abandon_invalid') from None


def hashed(value):return hashlib.sha256(encoded(value,2*1024*1024).encode()).hexdigest()


def opaque(value):
    if type(value) is not str or re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}',value) is None:
        raise HostError('message_abandon_invalid')
    return value


def guard_sql(db,tables):
    """Refuse side effects and alternate resolution of known sensitive names."""
    for table in tables:
        for schema in ('main','temp'):
            if db.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE type='trigger' AND tbl_name COLLATE NOCASE=? LIMIT 1",(table,)).fetchone():
                raise HostError('message_abandon_schema')
        if (db.execute('SELECT 1 FROM temp.sqlite_master WHERE name COLLATE NOCASE=? LIMIT 1',(table,)).fetchone()
                or db.execute("SELECT 1 FROM main.sqlite_master WHERE type='view' AND name COLLATE NOCASE=? LIMIT 1",(table,)).fetchone()):
            raise HostError('message_abandon_schema')


def identity(value):
    if type(value) is not dict or set(value)!={'owner_id','agent_id','device_id','profile_version','session_id'}:
        raise HostError('message_abandon_invalid')
    for key in ('owner_id','agent_id','device_id','session_id'):opaque(value[key])
    if type(value['profile_version']) is not int or not 1<=value['profile_version']<2**63:
        raise HostError('message_abandon_invalid')
    return dict(value)


def reason(intent_id):return 'message-admission-sealed-'+opaque(intent_id)


def seal_wire(runtime):
    c=runtime['configuration'];scope=c['scope']
    return {'idempotency_key':runtime['key'],**{k:scope[k] for k in ('thread_id','message_id','source_sha256')},
        **{k:c[k] for k in ('work_id','turn_id','provider','model')},'session_id':scope['board_session_id']}


def proof(runtime,value,pinned):
    pinned=identity(pinned)
    scope=runtime['configuration']['scope']
    if (pinned['session_id']!=scope['board_session_id']
            or pinned['owner_id']!=scope['owner_id'] or pinned['agent_id']!=scope['agent_id']):
        raise HostError('message_abandon_identity')
    try:return validate_seal({'reserved':False,'refusal':value},seal_wire(runtime),
        **{k:v for k,v in pinned.items() if k!='session_id'})
    except Exception:raise HostError('message_abandon_proof') from None


def _proposal_shape(value):
    fields={'schema','request_key','host','identity','dispatch','message','reader','delivery',
        'work','turn','runtime','proposal_sha256'}
    if type(value) is not dict or set(value)!=fields or value['schema']!=SCHEMA:
        raise HostError('message_abandon_invalid')
    normalized=json.loads(encoded(value))
    body={k:v for k,v in normalized.items() if k!='proposal_sha256'}
    if normalized['proposal_sha256']!=hashed(body):raise HostError('message_abandon_invalid')
    opaque(normalized['request_key']);identity(normalized['identity'])
    shapes={'host':{'instance_id','configuration_sha256','catalog_sha256','revision','target_id','fingerprint'},
        'message':{'row','payload_sha256','scope','configuration_sha256','context_sha256'}}
    for name,keys in shapes.items():
        if type(normalized[name]) is not dict or set(normalized[name])!=keys:
            raise HostError('message_abandon_invalid')
    for name in ('dispatch','reader','delivery','work','turn','runtime'):
        if type(normalized[name]) is not dict:raise HostError('message_abandon_invalid')
    return copy.deepcopy(normalized)


def uuid_id(value):
    import uuid
    try:
        if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError
    except (ValueError,TypeError,AttributeError):raise HostError('message_abandon_invalid') from None
    return value


def sha(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:
        raise HostError('message_abandon_invalid')
    return value


def validate_runtime_abandonment(value):
    """A disposition is not a remote execution/cleanup receipt."""
    try:
        receipt=value['receipt'];seal=receipt['seal']
        if (type(receipt) is not dict or set(receipt)!={'schema','intent_id','seal'}
                or receipt['schema']!='message-runtime-local-abandonment-v1'
                or value['start_confirmed']!=0):raise ValueError
        uuid_id(receipt['intent_id'])
        pinned={k:seal[k] for k in ('owner_id','agent_id','device_id','profile_version','session_id')}
        if proof(value,seal,pinned)!=seal:raise ValueError
    except Exception:raise ValueError('runtime_journal_corrupt') from None


def validate_message_abandonment(value):
    try:
        payload=value['payload'];marker=payload['abandonment']
        if (type(marker) is not dict or set(marker)!={'intent_id','seal_sha256','reason'}
                or marker['reason']!=MESSAGE_REASON or payload['reason']!=MESSAGE_REASON):raise ValueError
        uuid_id(marker['intent_id']);sha(marker['seal_sha256'])
    except Exception:raise HostError('message_journal_corrupt') from None


# Raw row shapes are deliberately closed. No prompt or provider session is
# copied into the proposal; the exact saved payload is pinned by its hash.
ROW_FIELDS={
 'dispatch':{'batch_id','target_id','request_id','subscription_id','worker_id','state','receipt','activation'},
 'reader':{'id','target_id','configuration_hash','opening_key','start_at','page_limit','feed_id','checkpoint'},
 'delivery':{'id','reader_id','remote_id','checkpoint','payload_hash','state','ack_json'},
 'work':{'id','owner_id','agent_id','project_id','ticket_id','workspace','executable','model','provider_session_id','status','created_at','updated_at'},
 'turn':{'id','work_id','request_id','prompt_sha256','status','pid','provider_session_id','cancel_requested','requires_review','result_json','resolution','resolution_reason','created_at','updated_at'},
 'runtime':{'id','key','configuration','phase','receipt','revision','start_confirmed','created_at','updated_at'},
 'message_row':{'id','delivery_id','message_id','thread_id','message_hash','worker_id','work_id','state','revision'}}


def proposal(value):
    import math
    from .message_scope import validate_scope,local_agent
    from .runtime_journal import RuntimeJournal
    p=_proposal_shape(value)
    try:
        for name,fields in ROW_FIELDS.items():
            row=p['message']['row'] if name=='message_row' else p[name]
            if set(row)!=fields:raise ValueError
        h=p['host'];m=p['message'];d=p['dispatch'];r=p['reader'];delivery=p['delivery']
        w=p['work'];t=p['turn'];runtime=p['runtime'];ident=p['identity'];row=m['row']
        for v in (h['configuration_sha256'],h['catalog_sha256'],h['fingerprint'],m['payload_sha256'],
                m['context_sha256'],m['configuration_sha256'],row['message_hash'],r['configuration_hash'],delivery['payload_hash'],t['prompt_sha256']):sha(v)
        for v in (h['instance_id'],d['batch_id'],d['worker_id'],r['id'],delivery['id'],row['id'],row['work_id'],w['id'],t['id'],runtime['id']):uuid_id(v)
        if any(type(v) is not int or v<1 for v in (h['revision'],row['revision'],runtime['revision'],r['page_limit'])):raise ValueError
        if type(r['checkpoint']) is not int or r['checkpoint']<0:raise ValueError
        if (type(delivery['checkpoint']) is not int or delivery['checkpoint']!=r['checkpoint']
                or r['start_at']!='now' or not 1<=r['page_limit']<=200):raise ValueError
        scope=validate_scope(m['scope']);c=runtime['configuration']
        if (c!=RuntimeJournal.configuration(scope,w['id'],t['id'],w['model'])
                or runtime['key']!=RuntimeJournal.key(c) or runtime['phase']!='needs_review'
                or runtime['receipt'] is not None or type(runtime['start_confirmed']) is not int or runtime['start_confirmed']!=0):raise ValueError
        if any(scope[k]!=ident[k] for k in ('owner_id','agent_id')) or scope['board_session_id']!=ident['session_id']:raise ValueError
        if (w['owner_id']!=ident['owner_id'] or w['agent_id']!=local_agent(scope)
                or w['project_id']!=scope['thread_id'] or w['ticket_id']!=scope['message_id']
                or w['provider_session_id'] is not None or w['status']!='needs_review'):raise ValueError
        if (t['work_id']!=w['id'] or t['status']!='blocked' or t['pid'] is not None or t['provider_session_id'] is not None
                or type(t['requires_review']) is not int or t['requires_review']!=1
                or type(t['cancel_requested']) is not int or t['cancel_requested']!=0
                or t['resolution'] is not None or t['resolution_reason'] is not None
                or json.loads(t['result_json'])!=LEGACY_RESULT or t['request_id']!='host-message-'+d['worker_id']):raise ValueError
        for v in (w,t,runtime):
            if any(type(v[k]) not in (int,float) or not math.isfinite(v[k]) for k in ('created_at','updated_at')):raise ValueError
        if (row['state']!='running' or row['id']!=d['batch_id'] or row['work_id']!=w['id'] or row['worker_id']!=d['worker_id']
                or row['thread_id']!=scope['thread_id'] or row['message_id']!=scope['message_id']
                or d['target_id']!=h['target_id'] or d['state']!='pending'
                or d['request_id']!='host-message-'+row['id'] or d['subscription_id']!=r['id']
                or d['receipt']!={'status':'pending','reason':'message_runtime_reconciliation',
                    'worker_operation_id':d['worker_id'],'message_operation_id':d['batch_id']}
                or r['target_id']!=h['target_id'] or r['opening_key']!='host-message-feed-'+r['id']
                or re.fullmatch('[0-9a-f]{32}',r['feed_id']) is None
                or delivery['reader_id']!=r['id'] or delivery['id']!=row['delivery_id']
                or delivery['state']!='pending' or delivery['ack_json']!='{}'):raise ValueError
        return p
    except Exception:raise HostError('message_abandon_invalid') from None
