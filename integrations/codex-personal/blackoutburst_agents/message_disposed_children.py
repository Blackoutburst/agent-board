"""Closed two-child history, never current source/process or launch authority.

The old sealed child and the executed-blocked child keep separate proofs. Only
their terminal children and normal feed progress are checked here; the enclosing
source guard owns live source/unit/catalogue/command fences. No RPC occurs.
"""
from contextlib import ExitStack
import json
import re

from .host_config import HostError,digest
from .message_abandon_protocol import guard_sql,hashed,uuid_id,sha,MESSAGE_REASON
from .message_admission_abandonment import MessageAdmissionAbandonment
from .message_journal import APPLICATION_ID as MESSAGE_APP,MAX_DELIVERIES
from .runtime_journal import APPLICATION_ID as RUNTIME_APP,COLUMNS as RUNTIME_COLUMNS

SCHEMA='message-disposed-children-v1'


def _fail(code='conflict'):
    raise HostError('message_disposed_children_'+code)


def _ids(db,table,expected):
    rows=db.execute('SELECT id FROM main.'+table+' LIMIT ?',(len(expected)+1,)).fetchall()
    if len(rows)!=len(expected) or {row['id'] for row in rows}!=set(expected):_fail()


def _guard(host,messages,runs,runtime):
    from . import message_executed_disposition as executed
    host._require_lock()
    if any(db.in_transaction for db in (host._db,messages.db,runs._db,runtime.db)):_fail('transaction_active')
    if (messages.directory!=host.directory/'messages' or runs.state_dir!=host.directory/'message-runs'
            or runtime.directory!=host.directory/'message-runtime'):_fail('identity')
    guard_sql(host._db,executed.HOST_TABLES)
    host._check_abandonments();host._check_executed_dispositions()
    guard_sql(messages.db,('readers','deliveries','operations'))
    guard_sql(runs._db,('works','turns','context_guards'));guard_sql(runtime.db,('executions',))
    for db,version,app in ((host._db,4,0x4242484A),(messages.db,3,MESSAGE_APP),
            (runs._db,2,0),(runtime.db,4,RUNTIME_APP)):
        if (db.execute('PRAGMA main.user_version').fetchone()[0]!=version
                or db.execute('PRAGMA main.application_id').fetchone()[0]!=app):_fail('schema')
    runs._check_existing_schema()
    if tuple(row['name'] for row in runtime.db.execute('PRAGMA main.table_info(executions)'))!=RUNTIME_COLUMNS:_fail('schema')
    control=host.control()
    if (control['enabled'] or control['configuration']['schema_version']!=2 or not host.managed()
            or any(host.managed_target(t['id'])['enabled'] for t in control['configuration']['targets'])):
        _fail('requires_stopped')
    return executed


def _records(host,abandonment_intent_id,executed_intent_id):
    from .message_abandon_journal import TABLE as LEGACY_TABLE
    from .message_executed_disposition import TABLE as EXECUTED_TABLE
    uuid_id(abandonment_intent_id);uuid_id(executed_intent_id)
    if abandonment_intent_id==executed_intent_id:_fail()
    _ids(host._db,LEGACY_TABLE,[abandonment_intent_id]);_ids(host._db,EXECUTED_TABLE,[executed_intent_id])
    old=host._abandon_record(intent_id=abandonment_intent_id)
    new=host._executed_record(intent_id=executed_intent_id)
    if old is None or new is None or old['phase']!='complete' or new['phase']!='complete':_fail('unsettled')
    return old,new


def _target(host,old,new,executed):
    p0,p1=old['proposal'],new['proposal'];control=host.control();config=control['configuration']
    target=next((t for t in config['targets'] if t['id']==p1['host']['target_id']),None)
    if (target is None or len(config['targets'])!=1
            or not executed.exact(p0['identity'],p1['identity'])
            or p0['host']['instance_id']!=p1['host']['instance_id']
            or p0['host']['target_id']!=p1['host']['target_id']
            or p0['host']['configuration_sha256']!=p1['host']['configuration_sha256']
            or control['instance_id']!=p1['host']['instance_id'] or control['hash']!=p1['host']['configuration_sha256']
            or config['owner_id']!=p1['identity']['owner_id']):_fail('identity')
    for p in (p0,p1):
        ident=p['identity']
        if (target['agent_id']!=ident['agent_id'] or type(target['profile_version']) is not int
                or target['profile_version']!=ident['profile_version'] or target['board_session_id']!=ident['session_id']
                or digest(target)!=p['message']['configuration_sha256']
                or config['board_endpoint']!=p['message']['scope']['board_endpoint']
                or any(not executed.exact(p['work'][k],target[k]) for k in ('workspace','executable','model'))):_fail('identity')
        reader_hash=digest({'target':target,'owner_id':ident['owner_id'],
            'board_endpoint':config['board_endpoint'],'host_instance':control['instance_id']})
        if p['reader']['configuration_hash']!=reader_hash or p['reader']['page_limit']!=config['page_limit']:_fail('identity')
    return target


def _children(host,messages,runs,runtime,old,new,target,executed):
    p0,p1=old['proposal'],new['proposal']
    dispatches=host._db.execute('SELECT batch_id FROM main.dispatches LIMIT 3').fetchall()
    if (len(dispatches)!=2 or {row['batch_id'] for row in dispatches}
            !={p0['dispatch']['batch_id'],p1['dispatch']['batch_id']}):_fail()
    for table,key,db in (('works','work',runs._db),('turns','turn',runs._db),
            ('executions','runtime',runtime.db),('operations','message',messages.db)):
        ids=[p['message']['row']['id'] if key=='message' else p[key]['id'] for p in (p0,p1)]
        if len(set(ids))!=2:_fail()
        _ids(db,table,ids)
    if runs._db.execute('SELECT 1 FROM main.context_guards LIMIT 1').fetchone():_fail()
    records=[]
    for record in (old,new):
        p=record['proposal']
        rows={'runtime':runtime.get(p['runtime']['id']),
            'work':{'work':runs._work(p['work']['id']),'turn':runs._turn(p['turn']['id'])},
            'message':messages.operation(p['message']['row']['id'])}
        if record is old:
            if (not MessageAdmissionAbandonment._runtime_after(p,record['id'],record['proof'],rows['runtime'])
                    or not MessageAdmissionAbandonment._work_after(p,record['id'],rows['work'])
                    or not MessageAdmissionAbandonment._message_after(p,record['id'],record['proof'],rows['message'])):_fail()
            payload=dict(rows['message']['payload']);payload.pop('reason');payload.pop('abandonment')
            receipt={'status':'abandoned','reason':MESSAGE_REASON,'worker_operation_id':p['dispatch']['worker_id'],
                'message_operation_id':p['dispatch']['batch_id'],'abandonment_intent_id':record['id'],
                'seal_sha256':hashed(record['proof'])}
            hash_value=hashed;kind='sealed_never_started'
        else:
            executed.validate_proof(p,record['proof'],record['id'])
            if (not executed.exact(rows['runtime'],p['runtime'])
                    or not executed.MessageExecutedDisposition._work_after(p,record['id'],rows['work'])
                    or not executed.MessageExecutedDisposition._message_after(p,record['id'],record['proof'],rows['message'])):_fail()
            payload=dict(rows['message']['payload']);payload.pop('disposition')
            receipt=executed.disposition_receipt(p,record['id'],record['proof'])
            hash_value=executed.hashed;kind='executed_blocked_disposed'
        MessageAdmissionAbandonment._saved_context(p,payload,target)
        if any(hash_value(value)!=record['checkpoints'][key] for key,value in rows.items()):_fail()
        dispatch=next((d for d in host.dispatches() if d['batch_id']==p['dispatch']['batch_id']),None)
        if not executed.exact(dispatch,{**p['dispatch'],'state':'complete','receipt':receipt}):_fail()
        records.append({'kind':kind,'intent_id':record['id'],'proposal_sha256':p['proposal_sha256'],
            'proof_sha256':hash_value(record['proof']),
            'children':{key:record['checkpoints'][key] for key in ('runtime','work','message')}})
    return records


def _history(messages,p0,p1,executed):
    old_reader,new_reader=p0['reader'],p1['reader']
    if (old_reader['id']!=new_reader['id'] or old_reader['checkpoint']>=new_reader['checkpoint']
            or not executed.exact({k:v for k,v in old_reader.items() if k!='checkpoint'},
                {k:v for k,v in new_reader.items() if k!='checkpoint'})):_fail('history')
    _ids(messages.db,'readers',[new_reader['id']])
    reader=dict(messages.db.execute('SELECT * FROM main.readers WHERE id=?',(new_reader['id'],)).fetchone())
    advanced={**new_reader,'checkpoint':new_reader['checkpoint']+1}
    if not (executed.exact(reader,new_reader) or executed.exact(reader,advanced)):_fail('history')
    rows=messages.db.execute('SELECT * FROM main.deliveries LIMIT ?',(MAX_DELIVERIES+1,)).fetchall()
    if len(rows)>MAX_DELIVERIES:_fail('history')
    found=set();checkpoints=set()
    for raw in rows:
        row=dict(raw);uuid_id(row['id']);sha(row['payload_hash'])
        if (row['reader_id']!=reader['id'] or type(row['remote_id']) is not str
                or re.fullmatch('[0-9a-f]{32}',row['remote_id']) is None
                or type(row['checkpoint']) is not int or row['checkpoint']<0
                or row['checkpoint'] in checkpoints):_fail('history')
        checkpoints.add(row['checkpoint'])
        ack={'feed_id':reader['feed_id'],'delivery_id':row['remote_id'],
            'checkpoint':row['checkpoint']+1,'acknowledged':True}
        if row['id']==p1['delivery']['id']:
            found.add('new')
            if reader['checkpoint']==new_reader['checkpoint']:
                expected=(p1['delivery'],{**p1['delivery'],'state':'acking'})
            else:expected=({**p1['delivery'],'state':'acked','ack_json':json.dumps(ack,sort_keys=True)},)
            if not any(executed.exact(row,v) for v in expected):_fail('history')
        elif row['id']==p0['delivery']['id']:
            found.add('old')
            if not executed.exact(row,{**p0['delivery'],'state':'acked','ack_json':json.dumps(ack,sort_keys=True)}):_fail('history')
        elif (row['state']!='acked' or row['checkpoint']>=reader['checkpoint']
                or row['ack_json']!=json.dumps(ack,sort_keys=True)):_fail('history')
    if found!={'old','new'}:_fail('history')


def verify_disposed_message_children(host,messages,runs,runtime,*,abandonment_intent_id,executed_intent_id):
    """Verify both real ledgers/children under owner and inspection work locks."""
    try:
        executed=_guard(host,messages,runs,runtime)
        old,new=_records(host,abandonment_intent_id,executed_intent_id)
        initial_old,initial_new=old,new
        work_ids=[record['proposal']['work']['id'] for record in (old,new)]
        if len(set(work_ids))!=2:_fail()
        with ExitStack() as locks:
            for work_id in sorted(work_ids):locks.enter_context(runs.work_lock(work_id,inspection_only=True))
            executed=_guard(host,messages,runs,runtime)
            old,new=_records(host,abandonment_intent_id,executed_intent_id)
            if not executed.exact(old,initial_old) or not executed.exact(new,initial_new):_fail()
            target=_target(host,old,new,executed)
            roster=_children(host,messages,runs,runtime,old,new,target,executed)
            _history(messages,old['proposal'],new['proposal'],executed)
            return {'schema':SCHEMA,'roster_sha256':executed.hashed(roster)}
    except HostError as error:
        if error.code.startswith('message_disposed_children_'):raise
        raise HostError('message_disposed_children_conflict') from None
    except Exception:raise HostError('message_disposed_children_conflict') from None
