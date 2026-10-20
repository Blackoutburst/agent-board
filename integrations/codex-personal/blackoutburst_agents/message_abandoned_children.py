"""Read-only child proof after a consumed message-abandonment closure.

A source transition may legitimately change Host outbox/catalog/generation and
therefore cannot reuse the old complete Host fingerprint. This helper checks
only the unchanged configured identity and exact terminal children. It does not
attest a catalogue, source epoch, system service or absence of arbitrary PIDs.
"""
import json

from .host_config import HostError,digest
from .message_abandon_journal import HOST_TABLES
from .message_abandon_protocol import guard_sql,hashed,uuid_id,sha,MESSAGE_REASON
from .message_admission_abandonment import MessageAdmissionAbandonment
from .message_journal import APPLICATION_ID as MESSAGE_APP,MAX_DELIVERIES
from .runtime_journal import APPLICATION_ID as RUNTIME_APP,COLUMNS as RUNTIME_COLUMNS

SCHEMA='message-abandonment-children-v1'
MESSAGE_TABLES=('readers','deliveries','operations')
WORK_TABLES=('works','turns','context_guards')


def _exact_child(db,table,identifier):
    rows=db.execute('SELECT id FROM main.'+table+' LIMIT 2').fetchall()
    if len(rows)!=1 or rows[0]['id']!=identifier:
        raise HostError('message_abandon_children_conflict')


def _guard(host,messages,runs,runtime):
    host._require_lock()
    if any(db.in_transaction for db in (host._db,messages.db,runs._db,runtime.db)):
        raise HostError('message_abandon_children_transaction_active')
    if (messages.directory!=host.directory/'messages' or runs.state_dir!=host.directory/'message-runs'
            or runtime.directory!=host.directory/'message-runtime'):
        raise HostError('message_abandon_children_identity')
    guard_sql(host._db,HOST_TABLES);host._check_abandonments()
    guard_sql(messages.db,MESSAGE_TABLES);guard_sql(runs._db,WORK_TABLES);guard_sql(runtime.db,('executions',))
    for db,version,application in ((host._db,3,0x4242484A),(messages.db,2,MESSAGE_APP),
            (runs._db,2,0),(runtime.db,4,RUNTIME_APP)):
        if (db.execute('PRAGMA main.user_version').fetchone()[0]!=version
                or db.execute('PRAGMA main.application_id').fetchone()[0]!=application):
            raise HostError('message_abandon_children_schema')
    runs._check_existing_schema()
    if tuple(r['name'] for r in runtime.db.execute('PRAGMA main.table_info(executions)'))!=RUNTIME_COLUMNS:
        raise HostError('message_abandon_children_schema')


def _delivery_history(messages,p):
    """The original reader/delivery remain; unrelated history must be empty ACKs."""
    reader_rows=messages.db.execute('SELECT * FROM main.readers LIMIT 17').fetchall()
    if len(reader_rows)>16:raise HostError('message_abandon_children_history')
    readers={r['id']:dict(r) for r in reader_rows}
    original=readers.get(p['reader']['id'])
    if original not in (p['reader'],{**p['reader'],'checkpoint':p['reader']['checkpoint']+1}):
        raise HostError('message_abandon_children_history')
    rows=messages.db.execute('SELECT * FROM main.deliveries LIMIT ?',(MAX_DELIVERIES+1,)).fetchall()
    if len(rows)>MAX_DELIVERIES:raise HostError('message_abandon_children_history')
    chosen=None
    for raw in rows:
        row=dict(raw);reader=readers.get(row['reader_id'])
        if reader is None:raise HostError('message_abandon_children_history')
        if row['id']==p['delivery']['id']:chosen=row;continue
        # _exact_child already proves no operation belongs to these rows.
        ack={'feed_id':reader['feed_id'],'delivery_id':row['remote_id'],
            'checkpoint':row['checkpoint']+1,'acknowledged':True}
        sha(row['payload_hash'])
        if (row['state']!='acked'
                or type(row['checkpoint']) is not int or row['checkpoint']<0
                or row['checkpoint']>=reader['checkpoint']
                or row['ack_json']!=json.dumps(ack,sort_keys=True)):
            raise HostError('message_abandon_children_history')
    if chosen is None:raise HostError('message_abandon_children_history')
    ack={'feed_id':p['reader']['feed_id'],'delivery_id':p['delivery']['remote_id'],
        'checkpoint':p['reader']['checkpoint']+1,'acknowledged':True}
    if original['checkpoint']==p['reader']['checkpoint']:
        expected=(p['delivery'],{**p['delivery'],'state':'acking'})
    else:expected=({**p['delivery'],'state':'acked','ack_json':json.dumps(ack,sort_keys=True)},)
    if chosen not in expected:raise HostError('message_abandon_children_history')


def _verify(host,messages,runs,runtime,intent_id):
    _guard(host,messages,runs,runtime)
    record=host._abandon_record(intent_id=intent_id)
    if record is None or record['phase']!='complete':raise HostError('message_abandon_children_unsettled')
    p=record['proposal'];ident=p['identity'];control=host.control();config=control['configuration']
    target=next((t for t in config['targets'] if t['id']==p['host']['target_id']),None)
    if (control['enabled'] or config['schema_version']!=2 or control['hash']!=p['host']['configuration_sha256']
            or control['instance_id']!=p['host']['instance_id'] or config['owner_id']!=ident['owner_id']
            or target is None or digest(target)!=p['message']['configuration_sha256']
            or target['agent_id']!=ident['agent_id'] or target['profile_version']!=ident['profile_version']
            or target['board_session_id']!=ident['session_id']
            or config['board_endpoint']!=p['message']['scope']['board_endpoint']):
        raise HostError('message_abandon_children_identity')
    if not host.managed() or any(host.managed_target(t['id'])['enabled'] for t in config['targets']):
        raise HostError('message_abandon_children_requires_stopped')
    receipt={'status':'abandoned','reason':MESSAGE_REASON,
        'worker_operation_id':p['dispatch']['worker_id'],'message_operation_id':p['dispatch']['batch_id'],
        'abandonment_intent_id':intent_id,'seal_sha256':hashed(record['proof'])}
    dispatch=next((d for d in host.dispatches() if d['batch_id']==p['dispatch']['batch_id']),None)
    if dispatch!={**p['dispatch'],'state':'complete','receipt':receipt}:
        raise HostError('message_abandon_children_conflict')
    _exact_child(runs._db,'works',p['work']['id']);_exact_child(runs._db,'turns',p['turn']['id'])
    _exact_child(runtime.db,'executions',p['runtime']['id']);_exact_child(messages.db,'operations',p['message']['row']['id'])
    if runs._db.execute('SELECT 1 FROM main.context_guards LIMIT 1').fetchone():
        raise HostError('message_abandon_children_conflict')
    values={'runtime':runtime.get(p['runtime']['id']),
        'work':{'work':runs._work(p['work']['id']),'turn':runs._turn(p['turn']['id'])},
        'message':messages.operation(p['message']['row']['id'])}
    if (not MessageAdmissionAbandonment._runtime_after(p,intent_id,record['proof'],values['runtime'])
            or not MessageAdmissionAbandonment._work_after(p,intent_id,values['work'])
            or not MessageAdmissionAbandonment._message_after(p,intent_id,record['proof'],values['message'])
            or any(hashed(value)!=record['checkpoints'][name] for name,value in values.items())):
        raise HostError('message_abandon_children_conflict')
    _delivery_history(messages,p)
    # The Host checkpoint/catalog/revision/generation/outbox are deliberately
    # not authority here. The enclosing source-transition guard owns their CAS.
    return {'schema':SCHEMA,'intent_id':intent_id,'proof_sha256':hashed(record['proof']),
        'children_sha256':hashed({'intent_id':intent_id,'proposal_sha256':p['proposal_sha256'],
            **{k:record['checkpoints'][k] for k in ('runtime','work','message')}})}


def verify_completed_message_children(host,messages,runs,runtime,*,intent_id):
    """Require an actual owner lock and take a non-mutating inspection work lock."""
    try:
        uuid_id(intent_id);_guard(host,messages,runs,runtime)
        record=host._abandon_record(intent_id=intent_id)
        if record is None or record['phase']!='complete':raise HostError('message_abandon_children_unsettled')
        with runs.work_lock(record['proposal']['work']['id'],inspection_only=True):
            return _verify(host,messages,runs,runtime,intent_id)
    except HostError as error:
        if error.code.startswith('message_abandon_children_'):raise
        raise HostError('message_abandon_children_conflict') from None
    except Exception:raise HostError('message_abandon_children_conflict') from None
