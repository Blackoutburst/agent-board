"""Live, bounded and readonly message-child closure for an explicit append.

The returned summary is data. Only the live context keeps the real owner and
inspection work locks; neither it nor a checksum authenticates Board authority.
"""
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
import re
import threading

from .host_config import HostError, digest
from .message_abandon_protocol import guard_sql, hashed, uuid_id, sha, MESSAGE_REASON
from .message_admission_abandonment import MessageAdmissionAbandonment as Abandon
from .message_scope import validate_scope, local_agent
from .message_runtime import MessageRuntimeGuard
from .runtime_journal import RuntimeJournal
from .message_launch_bindings import structural_configuration
from .work_journal import _encode

SCHEMA = 'message-append-children-v1'
MAX_ROWS = 256
MAX_ROW_BYTES = 300 * 1024
CHILD_SQL={
    'readers':'''CREATE TABLE readers(id TEXT PRIMARY KEY,target_id TEXT UNIQUE NOT NULL,
        configuration_hash TEXT NOT NULL,opening_key TEXT NOT NULL,start_at TEXT NOT NULL,
        page_limit INTEGER NOT NULL,feed_id TEXT NOT NULL,checkpoint INTEGER NOT NULL)''',
    'deliveries':'''CREATE TABLE deliveries(id TEXT PRIMARY KEY,reader_id TEXT NOT NULL REFERENCES readers(id),
        remote_id TEXT NOT NULL,checkpoint INTEGER NOT NULL,payload_hash TEXT NOT NULL,
        state TEXT NOT NULL,ack_json TEXT NOT NULL,UNIQUE(reader_id,remote_id),UNIQUE(reader_id,checkpoint))''',
    'operations':'''CREATE TABLE operations(id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL REFERENCES deliveries(id),
        message_id TEXT NOT NULL,thread_id TEXT NOT NULL,message_hash TEXT NOT NULL,
        worker_id TEXT NOT NULL,work_id TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL,UNIQUE(delivery_id,message_id))''',
    'executions':'''CREATE TABLE executions (
        id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, configuration TEXT NOT NULL,
        phase TEXT NOT NULL, receipt TEXT, revision INTEGER NOT NULL, start_confirmed INTEGER NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)'''}


def _child_schema(db,tables):
    normalize=lambda sql:re.sub(r'\s+','',sql).lower()
    seen=set()
    for row in db.execute('SELECT type,name,tbl_name,sql FROM main.sqlite_master'):
        if row['type']=='table' and row['name'] in tables:
            if normalize(row['sql'])!=normalize(CHILD_SQL[row['name']]):_fail()
            seen.add(row['name'])
        elif not (row['type']=='index' and row['tbl_name'] in tables
                and row['name'].startswith('sqlite_autoindex_'+row['tbl_name']+'_') and row['sql'] is None):_fail()
    if seen!=set(tables):_fail()


def _fail():
    raise HostError('message_append_children_invalid')


def _rows(db, table, limit=MAX_ROWS):
    columns = [r['name'] for r in db.execute('PRAGMA main.table_info('+table+')')]
    if not columns:_fail()
    quoted=['"'+c.replace('"','""')+'"' for c in columns]
    sizes = '+'.join('COALESCE(length(CAST('+c+' AS BLOB)),0)' for c in quoted)
    # The CASE applies at the actual read, not just an earlier size check.
    fields=','.join('CASE WHEN ('+sizes+')<='+str(MAX_ROW_BYTES)+' THEN '+c+' END AS '+c for c in quoted)
    rows=[]
    for raw in db.execute('SELECT '+fields+',('+sizes+') AS _bytes FROM main.'+table+' ORDER BY rowid LIMIT ?', (limit+1,)):
        row=dict(raw);size=row.pop('_bytes')
        if size>MAX_ROW_BYTES or len(rows)>=limit:_fail()
        rows.append(row)
    return rows


def _same(a,b):
    return _encode(a,MAX_ROW_BYTES,'message_append_children_invalid') == _encode(b,MAX_ROW_BYTES,'message_append_children_invalid')


def _saved_context(op, payload, target, config):
    scope=validate_scope(payload['scope'])
    if (scope['owner_id']!=config['owner_id'] or scope['agent_id']!=target['agent_id']
            or scope['board_endpoint']!=config['board_endpoint'] or scope['board_session_id']!=target['board_session_id']
            or scope['thread_id']!=op['thread_id'] or scope['message_id']!=op['message_id']
            or payload['configuration_sha256']!=digest(target)):_fail()
    ident=dict(owner_id=config['owner_id'],agent_id=target['agent_id'],session_id=target['board_session_id'],
        profile_version=target['profile_version'])
    p={'identity':ident,'message':{'scope':scope,'row':op,'context_sha256':payload['context_sha256']}}
    Abandon._saved_context(p,payload,target)
    expected='host-message-reply-'+digest({'endpoint':config['board_endpoint'],'owner_id':config['owner_id'],
        'agent_id':target['agent_id'],'thread_id':op['thread_id'],'message_id':op['message_id']})
    if payload['reply_key']!=expected:_fail()
    return scope


def _normal(op, target, config, work, turn, runtime):
    payload=op['payload']
    if set(payload)!=set('scope prompt context_sha256 context_refs configuration_sha256 reply_key body body_sha256 turn_id reply_id reason'.split()):_fail()
    scope=_saved_context(op,payload,target,config)
    if (work['id']!=op['work_id'] or work['owner_id']!=config['owner_id'] or work['agent_id']!=local_agent(scope)
            or work['project_id']!=op['thread_id'] or work['ticket_id']!=op['message_id'] or work['status']!='open'
            or any(work[k]!=target[k] for k in ('workspace','executable','model'))
            or not work['provider_session_id'] or turn['work_id']!=work['id'] or turn['id']!=payload['turn_id']
            or turn['status']!='completed' or turn['requires_review']!=0 or turn['resolution'] is not None
            or turn['provider_session_id']!=work['provider_session_id']
            or turn['prompt_sha256']!=hashlib.sha256(payload['prompt'].encode('utf-8')).hexdigest()):_fail()
    result=json.loads(turn['result_json'])
    if (type(payload['body']) is not str or not payload['body'].strip() or payload['body_sha256']!=digest(payload['body'])
            or payload['reason']!='message_reply_confirmed' or type(payload['reply_id']) is not str or not payload['reply_id']
            or result.get('status')!='completed' or result.get('text')!=payload['body']
            or result.get('session_id',result.get('provider_session_id'))!=work['provider_session_id']
            or result.get('verification',{}).get('cleanup_confirmed') is not True):_fail()
    expected=RuntimeJournal.configuration(scope,work['id'],turn['id'],target['model'])
    if (not _same(runtime['configuration'],expected) or runtime['phase']!='finished'
            or type(runtime['start_confirmed']) is not int or runtime['start_confirmed']!=1):_fail()
    # Reuse the real metadata validator without a client or a network operation.
    guard=MessageRuntimeGuard.__new__(MessageRuntimeGuard);guard.operation=runtime
    receipt=guard.metadata(runtime['receipt'])
    if (not _same(receipt,runtime['receipt']) or receipt['state']!='finished'
            or receipt['cleanup_confirmed'] is not True or receipt['outcome']!='completed'
            or receipt['reason']!='completed' or receipt['profile_version']!=target['profile_version']):_fail()
    return {'status':'complete','reason':'message_reply_confirmed',
        'worker_operation_id':op['worker_id'],'message_operation_id':op['id']}


class _AppendChildrenLease:
    def __reduce_ex__(self, protocol):
        raise TypeError('message_append_children_not_serializable')

    @property
    def proof(self):
        self._live()
        return copy.deepcopy(self._proof)

    def _live(self):
        self.host._require_lock()
        if (not self.active or self.pid!=os.getpid() or self.thread!=threading.get_ident()
                or self.token is None or self.host._owner_lock_token is not self.token
                or any(getattr(self,k) is not v for k,v in self.objects.items())):_fail()
        for work in self.work_ids:self.runs._require_lock(work)

    def recheck(self):
        try:
            self._live()
            if self._collect()!=self._proof:_fail()
            return self.proof
        except Exception:raise HostError('message_append_children_invalid') from None

    def _collect(self):
        from . import message_executed_disposition as executed
        host,messages,runs,runtime=self.host,self.messages,self.runs,self.runtime
        if any(db.in_transaction for db in (messages.db,runs._db,runtime.db)):_fail()
        if (messages.directory!=host.directory/'messages' or runs.state_dir!=host.directory/'message-runs'
                or runtime.directory!=host.directory/'message-runtime'):_fail()
        for db,versions,appid,tables in (
            (messages.db,(1,2,3),0x42424D46,('readers','deliveries','operations')),
            (runs._db,(2,),0,('works','turns','context_guards')),
            (runtime.db,(1,3,4),0x42425254,('executions',))):
            if (db.execute('PRAGMA main.user_version').fetchone()[0] not in versions
                    or db.execute('PRAGMA main.application_id').fetchone()[0]!=appid):_fail()
            guard_sql(db,tables)
            if db is not runs._db:_child_schema(db,tables)
        runs._check_existing_schema()
        control=host.control()
        if control['instance_id']!=self.instance or control['enabled']:_fail()
        current=control['configuration'];original=self.configuration
        if (any(current[k]!=v for k,v in original.items() if k!='targets')
                or not _same(current['targets'][:len(original['targets'])],original['targets'])
                or any(host.managed_target(t['id'])['enabled'] for t in current['targets'])):_fail()
        raw={}
        total=0
        for db,names in ((messages.db,('readers','deliveries','operations')),
                (runs._db,('works','turns','context_guards')),(runtime.db,('executions',))):
            for name in names:
                raw[name]=_rows(db,name,16 if name=='readers' else MAX_ROWS)
                total+=len(_encode(raw[name],80*1024*1024,'message_append_children_invalid').encode())
                if total>80*1024*1024:_fail()
        if raw['context_guards']:_fail()
        by={name:{r['id']:r for r in rows} for name,rows in raw.items() if name!='context_guards'}
        if set(by['works'])!=set(self.work_ids):_fail()
        targets={t['id']:t for t in original['targets']};readers=by['readers'];deliveries=by['deliveries']
        if len({r['target_id'] for r in readers.values()})!=len(readers):_fail()
        for reader in readers.values():
            uuid_id(reader['id'])
            target=targets.get(reader['target_id'])
            if (target is None or reader['configuration_hash']!=digest({'target':target,'owner_id':original['owner_id'],
                    'board_endpoint':original['board_endpoint'],'host_instance':self.instance})
                    or reader['start_at']!=target['start_at'] or reader['page_limit']!=original['page_limit']
                    or reader['opening_key']!='host-message-feed-'+reader['id']
                    or type(reader['checkpoint']) is not int or reader['checkpoint']<0):_fail()
            own=[d for d in deliveries.values() if d['reader_id']==reader['id']]
            # Normal ACK prunes older empty/ignored-only pages. Gaps are
            # legitimate, while the current acknowledged page must remain.
            if (len({d['checkpoint'] for d in own})!=len(own)
                    or (reader['checkpoint']>0 and not any(d['checkpoint']==reader['checkpoint']-1 for d in own))
                    or (reader['feed_id']!='' and re.fullmatch('[0-9a-f]{32}',reader['feed_id']) is None)
                    or (own and not reader['feed_id'])):_fail()
        for delivery in deliveries.values():
            uuid_id(delivery['id']);sha(delivery['payload_hash'])
            reader=readers.get(delivery['reader_id'])
            if (reader is None or delivery['state']!='acked' or type(delivery['checkpoint']) is not int
                    or not 0<=delivery['checkpoint']<reader['checkpoint']
                    or re.fullmatch('[0-9a-f]{32}',delivery['remote_id']) is None):_fail()
            items=[{k:op[k] for k in ('message_id','thread_id','message_hash')}
                for op in raw['operations'] if op['delivery_id']==delivery['id']]
            if len(items)>reader['page_limit'] or digest(items)!=delivery['payload_hash']:_fail()
            ack={'feed_id':reader['feed_id'],'delivery_id':delivery['remote_id'],
                'checkpoint':delivery['checkpoint']+1,'acknowledged':True}
            if delivery['ack_json']!=json.dumps(ack,sort_keys=True):_fail()
        records={}
        for method in ('_abandon_record','_executed_record'):
            table='host_message_abandonments' if method=='_abandon_record' else 'host_message_dispositions'
            if not host._db.execute('SELECT 1 FROM main.sqlite_master WHERE name=?',(table,)).fetchone():continue
            for row in _rows(host._db,table,32):
                record=getattr(host,method)(intent_id=row['id'])
                if record['phase']!='complete':_fail()
                opid=record['proposal']['message']['row']['id']
                if opid in records:_fail()
                records[opid]=(method,record)
        seen_work=set();seen_turn=set();seen_runtime=set();seen_dispatch=set();seen_records=set()
        _rows(host._db,'dispatches')
        dispatches={d['batch_id']:d for d in host.dispatches()}
        runtime_by_work={}
        for raw_runtime in raw['executions']:
            value=runtime.get(raw_runtime['id'])
            runtime_by_work.setdefault(value['configuration']['work_id'],[]).append(value)
        for opraw in raw['operations']:
            op=messages.operation(opraw['id']);delivery=deliveries.get(op['delivery_id'])
            if delivery is None:_fail()
            reader=readers[delivery['reader_id']];target=targets[reader['target_id']]
            if op['state'] not in ('complete','abandoned','executed_disposed'):_fail()
            work=by['works'].get(op['work_id']);turns=[t for t in raw['turns'] if t['work_id']==op['work_id']]
            runtimes=runtime_by_work.get(op['work_id'],[])
            if work is None or len(turns)!=1 or len(runtimes)!=1:_fail()
            turn,rt=turns[0],runtimes[0]
            if op['state']=='complete':
                if op['id'] in records:_fail()
                receipt=_normal(op,target,original,work,turn,rt)
            else:
                kind,record=records.get(op['id'],(None,None))
                if record is None:_fail()
                p=record['proposal'];ident=p['identity']
                if (p['host']['instance_id']!=self.instance or p['host']['target_id']!=target['id']
                        or ident['owner_id']!=original['owner_id'] or ident['agent_id']!=target['agent_id']
                        or ident['session_id']!=target['board_session_id'] or ident['profile_version']!=target['profile_version']
                        or work['id']!=p['work']['id'] or turn['id']!=p['turn']['id'] or rt['id']!=p['runtime']['id']):_fail()
                children={'runtime':rt,'work':{'work':work,'turn':turn},'message':op}
                payload=dict(op['payload'])
                if kind=='_abandon_record':
                    if op['state']!='abandoned' or not Abandon._runtime_after(p,record['id'],record['proof'],rt) or not Abandon._work_after(p,record['id'],children['work']) or not Abandon._message_after(p,record['id'],record['proof'],op):_fail()
                    payload.pop('reason');payload.pop('abandonment');hash_value=hashed
                    receipt={'status':'abandoned','reason':MESSAGE_REASON,'worker_operation_id':op['worker_id'],
                        'message_operation_id':op['id'],'abandonment_intent_id':record['id'],'seal_sha256':hashed(record['proof'])}
                else:
                    executed.validate_proof(p,record['proof'],record['id'])
                    if op['state']!='executed_disposed' or not executed.exact(rt,p['runtime']) or not executed.MessageExecutedDisposition._work_after(p,record['id'],children['work']) or not executed.MessageExecutedDisposition._message_after(p,record['id'],record['proof'],op):_fail()
                    payload.pop('disposition');hash_value=executed.hashed
                    receipt=executed.disposition_receipt(p,record['id'],record['proof'])
                Abandon._saved_context(p,payload,target)
                if any(hash_value(value)!=record['checkpoints'][k] for k,value in children.items()):_fail()
                seen_records.add(op['id'])
            dispatch=dispatches.get(op['id'])
            if (dispatch is None or dispatch['state']!='complete' or dispatch['target_id']!=target['id']
                    or dispatch['worker_id']!=op['worker_id'] or dispatch['subscription_id']!=reader['id']
                    or dispatch['request_id']!='host-message-'+op['id'] or not _same(dispatch['receipt'],receipt)):_fail()
            seen_dispatch.add(op['id']);seen_work.add(work['id']);seen_turn.add(turn['id']);seen_runtime.add(rt['id'])
        if (seen_records!=set(records) or seen_dispatch!=set(dispatches) or seen_work!=set(by['works'])
                or seen_turn!=set(by['turns']) or seen_runtime!=set(by['executions'])):_fail()
        # Hash actual bounded rows, including empty-feed receipts and all legacy
        # ledgers. No model text, prompt or ID is returned in the summary.
        raw['dispatches']=[dispatches[k] for k in sorted(dispatches)]
        raw['ledgers']=[r for _,r in sorted((k,v[1]) for k,v in records.items())]
        digest_value=hashlib.sha256(_encode(raw,80*1024*1024,'message_append_children_invalid').encode()).hexdigest()
        return dict(schema=SCHEMA,host_instance_id=self.instance,configuration_sha256=digest(original),roster_sha256=digest_value)


@contextmanager
def locked_message_append_children(host,messages,runs,runtime,*,operation_id=None):
    host._require_lock()
    if host._owner_lock_token is None:_fail()
    if any(db.in_transaction for db in (host._db,messages.db,runs._db,runtime.db)):_fail()
    config=host.control()['configuration']
    if operation_id is not None:
        record=host.messages_append_record(operation_id=operation_id)
        if record is None:_fail()
        config=record['plan']['old_configuration']
    lease=_AppendChildrenLease();lease.host,lease.messages,lease.runs,lease.runtime=host,messages,runs,runtime
    lease.objects=dict(host=host,messages=messages,runs=runs,runtime=runtime)
    lease.pid,lease.thread,lease.token=os.getpid(),threading.get_ident(),host._owner_lock_token
    lease.configuration=structural_configuration(config);lease.instance=host.control()['instance_id'];lease.active=True
    with ExitStack() as stack:
        try:
            lease.work_ids=sorted(r['id'] for r in _rows(runs._db,'works'))
            for work in lease.work_ids:stack.enter_context(runs.work_lock(work,inspection_only=True))
            lease._live();lease._proof=lease._collect()
        except HostError:raise
        except Exception:raise HostError('message_append_children_invalid') from None
        try:yield lease
        finally:lease.active=False
