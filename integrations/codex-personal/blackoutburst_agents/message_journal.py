"""Bounded private confirmed-message state; no network, inference or scheduling."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .host_config import HostError, digest, private_directory
from .work_journal import _private_file, _encode

APPLICATION_ID = 0x42424D46
MAX_DELIVERIES = 256
MAX_OPERATIONS = 256
MAX_PAYLOAD = 256 * 1024
STATES = frozenset({'new','prepared','running','result_ready','posting','complete','ignored','needs_review'})
NEXT = {'new': {'prepared','ignored','needs_review'}, 'prepared': {'running','needs_review'},
    'running': {'result_ready','needs_review'}, 'result_ready': {'posting','needs_review'},
    'posting': {'complete','needs_review'}, 'complete': set(), 'ignored': set(), 'needs_review': set()}


def identifier(value, *, hex32=False, uuid_value=False):
    if (not isinstance(value, str) or not value or len(value) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HostError('message_reference_invalid')
    if hex32 and not re.fullmatch('[0-9a-f]{32}', value):raise HostError('message_reference_invalid')
    if uuid_value:
        try:
            if str(uuid.UUID(value)) != value:raise ValueError
        except ValueError:raise HostError('message_reference_invalid') from None
    return value


class MessageJournal:
    def __init__(self, directory):
        self.db = None
        try:
            self.directory = private_directory(directory)
            path = self.directory/'messages.sqlite3'
            os.close(_private_file(path))
            for suffix in ('-journal','-wal','-shm'):
                sidecar=Path(str(path)+suffix)
                if sidecar.exists() or sidecar.is_symlink():os.close(_private_file(sidecar))
            self.db=sqlite3.connect(path,isolation_level=None,timeout=5)
            self.db.row_factory=sqlite3.Row
            version=self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0,1):raise HostError('unsupported_message_journal')
            if version==0 and self.db.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone():
                raise HostError('message_journal_corrupt')
            self.db.execute('PRAGMA synchronous=FULL');self.db.execute('PRAGMA foreign_keys=ON')
            self.db.execute('PRAGMA journal_mode=DELETE');self.db.execute('PRAGMA secure_delete=ON')
            if version==0:
                with self.transaction() as db:
                    db.execute('''CREATE TABLE readers(id TEXT PRIMARY KEY,target_id TEXT UNIQUE NOT NULL,
                        configuration_hash TEXT NOT NULL,opening_key TEXT NOT NULL,start_at TEXT NOT NULL,
                        page_limit INTEGER NOT NULL,feed_id TEXT NOT NULL,checkpoint INTEGER NOT NULL)''')
                    db.execute('''CREATE TABLE deliveries(id TEXT PRIMARY KEY,reader_id TEXT NOT NULL REFERENCES readers(id),
                        remote_id TEXT NOT NULL,checkpoint INTEGER NOT NULL,payload_hash TEXT NOT NULL,
                        state TEXT NOT NULL,ack_json TEXT NOT NULL,UNIQUE(reader_id,remote_id),UNIQUE(reader_id,checkpoint))''')
                    db.execute('''CREATE TABLE operations(id TEXT PRIMARY KEY,delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                        message_id TEXT NOT NULL,thread_id TEXT NOT NULL,message_hash TEXT NOT NULL,
                        worker_id TEXT NOT NULL,work_id TEXT NOT NULL,state TEXT NOT NULL,revision INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,UNIQUE(delivery_id,message_id))''')
                    db.execute(f'PRAGMA application_id={APPLICATION_ID}');db.execute('PRAGMA user_version=1')
            elif self.db.execute('PRAGMA application_id').fetchone()[0]!=APPLICATION_ID:
                raise HostError('message_journal_corrupt')
            expected={'readers':('id','target_id','configuration_hash','opening_key','start_at','page_limit','feed_id','checkpoint'),
                'deliveries':('id','reader_id','remote_id','checkpoint','payload_hash','state','ack_json'),
                'operations':('id','delivery_id','message_id','thread_id','message_hash','worker_id','work_id','state','revision','payload_json')}
            if any(tuple(r['name'] for r in self.db.execute('PRAGMA table_info('+t+')'))!=cols for t,cols in expected.items()):
                raise HostError('message_journal_corrupt')
        except Exception:
            self.close();raise

    def __enter__(self):return self
    def __exit__(self,*_):self.close()
    def close(self):
        if self.db is not None:self.db.close();self.db=None

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:yield self.db;self.db.execute('COMMIT')
        except BaseException:
            if self.db.in_transaction:self.db.execute('ROLLBACK')
            raise

    def reader(self,target,configuration_hash,start_at,limit):
        identifier(target)
        if start_at not in ('now','beginning') or type(limit) is not int or not 1<=limit<=200:
            raise HostError('message_reference_invalid')
        with self.transaction() as db:
            row=db.execute('SELECT * FROM readers WHERE target_id=?',(target,)).fetchone()
            if row is None:
                if db.execute('SELECT COUNT(*) FROM readers').fetchone()[0]>=16:raise HostError('message_journal_full')
                reader_id=str(uuid.uuid4())
                db.execute('INSERT INTO readers VALUES(?,?,?,?,?,?,?,0)',
                    (reader_id,target,configuration_hash,'host-message-feed-'+reader_id,start_at,limit,''))
                row=db.execute('SELECT * FROM readers WHERE id=?',(reader_id,)).fetchone()
            if (row['configuration_hash'],row['start_at'],row['page_limit'])!=(configuration_hash,start_at,limit):
                raise HostError('message_configuration_conflict')
            return dict(row)

    def bind_feed(self,reader_id,response):
        if (not isinstance(response,dict) or set(response)!={'feed_id','start_at','limit','human_only','checkpoint'}
                or response['human_only'] is not True or type(response['checkpoint']) is not int
                or type(response['limit']) is not int):
            raise HostError('message_feed_invalid')
        identifier(response['feed_id'],hex32=True)
        with self.transaction() as db:
            row=db.execute('SELECT * FROM readers WHERE id=?',(reader_id,)).fetchone()
            if (row is None or response['start_at']!=row['start_at'] or response['limit']!=row['page_limit']
                    or response['checkpoint']!=row['checkpoint'] or row['feed_id'] not in ('',response['feed_id'])):
                raise HostError('message_feed_conflict')
            db.execute('UPDATE readers SET feed_id=? WHERE id=?',(response['feed_id'],reader_id))
        return dict(self.db.execute('SELECT * FROM readers WHERE id=?',(reader_id,)).fetchone())

    def pending(self,reader_id):
        row=self.db.execute("SELECT id FROM deliveries WHERE reader_id=? AND state!='acked'",(reader_id,)).fetchall()
        if len(row)>1:raise HostError('message_journal_corrupt')
        return self.delivery(row[0]['id']) if row else None

    def ingest(self,reader_id,response,messages):
        """messages contains only bounded immutable IDs/core hashes, never body."""
        if (not isinstance(response,dict) or set(response)!={'feed_id','checkpoint','delivery_id','messages','human_only'}
                or response['human_only'] is not True or type(response['checkpoint']) is not int
                or not isinstance(messages,list) or len(messages)>200):raise HostError('message_feed_invalid')
        identifier(response['delivery_id'],hex32=True)
        for item in messages:
            if set(item)!={'message_id','thread_id','message_hash'}:raise HostError('message_feed_invalid')
            identifier(item['message_id']);identifier(item['thread_id'])
            if not isinstance(item['message_hash'],str) or not re.fullmatch('[0-9a-f]{64}',item['message_hash']):raise HostError('message_feed_invalid')
        if len({i['message_id'] for i in messages})!=len(messages):raise HostError('message_feed_invalid')
        with self.transaction() as db:
            reader=db.execute('SELECT * FROM readers WHERE id=?',(reader_id,)).fetchone()
            if (reader is None or reader['feed_id']!=response['feed_id'] or reader['checkpoint']!=response['checkpoint']
                    or len(messages)>reader['page_limit']):raise HostError('message_feed_conflict')
            old=db.execute('SELECT * FROM deliveries WHERE reader_id=? AND remote_id=?',(reader_id,response['delivery_id'])).fetchone()
            hashed=digest(messages)
            if old:
                if old['payload_hash']!=hashed or old['checkpoint']!=response['checkpoint']:raise HostError('message_delivery_changed')
                return self.delivery(old['id'])
            if self.pending(reader_id):raise HostError('message_delivery_pending')
            if (db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0]>=MAX_DELIVERIES
                    or db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]+len(messages)>MAX_OPERATIONS):
                raise HostError('message_journal_full')
            delivery_id=str(uuid.uuid4())
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,\'pending\',\'{}\')',
                (delivery_id,reader_id,response['delivery_id'],response['checkpoint'],hashed))
            for item in messages:
                if db.execute('SELECT 1 FROM operations o JOIN deliveries d ON d.id=o.delivery_id WHERE d.reader_id=? AND o.message_id=?',
                        (reader_id,item['message_id'])).fetchone():raise HostError('message_duplicate_delivery')
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?,\'\',?,\'new\',1,\'{}\')',
                    (str(uuid.uuid4()),delivery_id,item['message_id'],item['thread_id'],item['message_hash'],str(uuid.uuid4())))
            return self.delivery(delivery_id)

    def delivery(self,identifier_value):
        row=self.db.execute('SELECT * FROM deliveries WHERE id=?',(identifier_value,)).fetchone()
        if row is None:raise HostError('message_reference_invalid')
        value=dict(row);value['ack']=json.loads(value.pop('ack_json'))
        if value['state'] not in ('pending','acking','acked'):raise HostError('message_journal_corrupt')
        value['operations']=[self.operation(r['id']) for r in self.db.execute(
            'SELECT id FROM operations WHERE delivery_id=? ORDER BY rowid',(identifier_value,))]
        return value

    def operation(self,identifier_value):
        row=self.db.execute('SELECT * FROM operations WHERE id=?',(identifier_value,)).fetchone()
        if row is None:raise HostError('message_reference_invalid')
        value=dict(row);value['payload']=json.loads(value.pop('payload_json'))
        identifier(value['id'],uuid_value=True);identifier(value['work_id'],uuid_value=True)
        if value['worker_id']:identifier(value['worker_id'],uuid_value=True)
        if value['state'] not in STATES or type(value['revision']) is not int or value['revision']<1:
            raise HostError('message_journal_corrupt')
        return value

    def bind_worker(self,operation_id,worker_id):
        identifier(worker_id,uuid_value=True)
        with self.transaction() as db:
            op=self.operation(operation_id)
            if op['worker_id'] not in ('',worker_id) or (not op['worker_id'] and op['state']!='new'):
                raise HostError('host_reference_conflict')
            db.execute('UPDATE operations SET worker_id=? WHERE id=?',(worker_id,operation_id))
        return self.operation(operation_id)

    def update(self,operation_id,expected_revision,state,**payload):
        encoded=_encode(payload,MAX_PAYLOAD,'message_payload_too_large')
        if state not in STATES:raise HostError('message_state_invalid')
        with self.transaction() as db:
            op=self.operation(operation_id)
            if op['revision']!=expected_revision:raise HostError('message_revision_conflict')
            if state!=op['state'] and state not in NEXT[op['state']]:raise HostError('message_state_invalid')
            db.execute('UPDATE operations SET state=?,revision=revision+1,payload_json=? WHERE id=?',
                (state,encoded,operation_id))
        return self.operation(operation_id)

    def begin_ack(self,delivery_id):
        with self.transaction() as db:
            delivery=self.delivery(delivery_id)
            if any(op['state'] not in ('complete','ignored') for op in delivery['operations']):
                raise HostError('message_delivery_unfinished')
            if delivery['state']=='pending':db.execute("UPDATE deliveries SET state='acking' WHERE id=?",(delivery_id,))
        return self.delivery(delivery_id)

    def finish_ack(self,delivery_id,response):
        with self.transaction() as db:
            delivery=self.delivery(delivery_id)
            reader=dict(db.execute('SELECT * FROM readers WHERE id=?',(delivery['reader_id'],)).fetchone())
            expected={'feed_id':reader['feed_id'],'delivery_id':delivery['remote_id'],
                'checkpoint':delivery['checkpoint']+1,'acknowledged':True}
            if (not isinstance(response,dict) or response!=expected or type(response.get('checkpoint')) is not int
                    or response.get('acknowledged') is not True):
                raise HostError('message_ack_invalid')
            if delivery['state']=='acked':
                if delivery['ack']!=response:raise HostError('message_ack_conflict')
                return delivery
            if delivery['state']!='acking' or reader['checkpoint']!=delivery['checkpoint']:
                raise HostError('message_ack_conflict')
            if any(op['state'] not in ('complete','ignored') for op in delivery['operations']):
                raise HostError('message_delivery_unfinished')
            db.execute("UPDATE deliveries SET state='acked',ack_json=? WHERE id=?",(json.dumps(response,sort_keys=True),delivery_id))
            db.execute('UPDATE readers SET checkpoint=? WHERE id=?',(response['checkpoint'],reader['id']))
            # Confirmed checkpoints replace empty/ignored-only receipt history.
            # Never remove a completed reply or any unacknowledged operation.
            db.execute("DELETE FROM operations WHERE delivery_id=? AND state='ignored'",(delivery_id,))
            db.execute("DELETE FROM deliveries WHERE reader_id=? AND state='acked' AND id!=? AND NOT EXISTS(SELECT 1 FROM operations WHERE delivery_id=deliveries.id)",(reader['id'],delivery_id))
        return self.delivery(delivery_id)
