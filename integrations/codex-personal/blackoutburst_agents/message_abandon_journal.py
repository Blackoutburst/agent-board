"""Host intent ledger for explicit one-message abandonment; no RPC."""
import json
import uuid

from .host_config import HostError
from .message_abandon_protocol import PHASES,encoded,hashed,proposal,proof,guard_sql

TABLE='host_message_abandonments'
SQL=f'''CREATE TABLE {TABLE}(id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,
    revision INTEGER NOT NULL,phase TEXT NOT NULL,proposal_sha256 TEXT NOT NULL,
    proposal_json TEXT NOT NULL,proof_json TEXT NOT NULL,checkpoints_json TEXT NOT NULL)'''
COLUMNS=('id','request_key','revision','phase','proposal_sha256','proposal_json','proof_json','checkpoints_json')
HOST_TABLES=('control','targets','dispatches','managed_settings','managed_targets','managed_commands','managed_effects',TABLE)


class HostMessageAbandonMixin:
    def _abandon_schema(self,db,*,create=False):
        guard_sql(db,HOST_TABLES)
        row=db.execute("SELECT type,sql FROM main.sqlite_master WHERE name=?",(TABLE,)).fetchone()
        if row is None and create:
            db.execute(SQL);row=db.execute("SELECT type,sql FROM main.sqlite_master WHERE name=?",(TABLE,)).fetchone()
        if row is None or tuple(row)!=('table',SQL):raise HostError('message_abandon_schema')
        if tuple(r['name'] for r in db.execute('PRAGMA main.table_info('+TABLE+')'))!=COLUMNS:
            raise HostError('message_abandon_schema')
        rows=db.execute(f'''SELECT id FROM main.{TABLE} WHERE typeof(proposal_json)!='text'
            OR length(CAST(proposal_json AS BLOB))>32768 OR typeof(proof_json)!='text'
            OR length(CAST(proof_json AS BLOB))>4096 OR typeof(checkpoints_json)!='text'
            OR length(CAST(checkpoints_json AS BLOB))>1024 LIMIT 1''').fetchone()
        if rows or len(db.execute(f'SELECT id FROM main.{TABLE} LIMIT 33').fetchall())>32:
            raise HostError('message_abandon_corrupt')

    def _check_abandonments(self):
        version=self._db.execute('PRAGMA user_version').fetchone()[0]
        exists=self._db.execute("SELECT 1 FROM main.sqlite_master WHERE name=?",(TABLE,)).fetchone()
        if version not in (3,4):
            if exists:raise HostError('message_abandon_schema')
            return
        self._abandon_schema(self._db)
        for row in self._db.execute(f'SELECT id FROM main.{TABLE}'):
            self._abandon_record(intent_id=row['id'])

    def _abandon_record(self,*,request_key=None,intent_id=None):
        if (request_key is None)==(intent_id is None):raise HostError('message_abandon_invalid')
        if self._db.execute('PRAGMA user_version').fetchone()[0] not in (3,4):return None
        self._abandon_schema(self._db)
        row=self._db.execute(f'SELECT * FROM main.{TABLE} WHERE '+('request_key' if request_key is not None else 'id')+'=?',
            (request_key if request_key is not None else intent_id,)).fetchone()
        if row is None:return None
        try:
            value=dict(row)
            if (str(uuid.UUID(value['id']))!=value['id'] or type(value['revision']) is not int
                    or value['revision']<1 or value['phase'] not in PHASES):raise ValueError
            p=proposal(json.loads(value['proposal_json']))
            if (p['request_key']!=value['request_key'] or p['proposal_sha256']!=value['proposal_sha256']
                    or encoded(p)!=value['proposal_json']):raise ValueError
            seal=json.loads(value['proof_json']);checks=json.loads(value['checkpoints_json'])
            index=PHASES.index(value['phase'])
            if value['revision']!=index+1 or encoded(seal,4096)!=value['proof_json'] or encoded(checks,1024)!=value['checkpoints_json']:
                raise ValueError
            expected=set(('runtime','work','message','host')[:max(0,index-1)])
            if type(checks) is not dict or set(checks)!=expected:raise ValueError
            if any(type(v) is not str or len(v)!=64 or any(c not in '0123456789abcdef' for c in v) for v in checks.values()):raise ValueError
            if index==0:
                if seal is not None:raise ValueError
            elif proof(p['runtime'],seal,p['identity'])!=seal:raise ValueError
            value.update(proposal=p,proof=seal,checkpoints=checks)
            return value
        except Exception:raise HostError('message_abandon_corrupt') from None

    def _abandon_host_fingerprint(self,target_id,batch_id):
        self._require_lock();guard_sql(self._db,HOST_TABLES)
        if not self.managed():raise HostError('message_abandon_managed_required')
        self._check_managed()
        control=self.control();targets=[self.managed_target(t['id']) for t in control['configuration']['targets']]
        if control['enabled'] or any(t['enabled'] for t in targets):raise HostError('message_abandon_requires_stopped')
        if self._db.execute("SELECT 1 FROM managed_commands WHERE state NOT IN ('applied','superseded') LIMIT 1").fetchone():
            raise HostError('message_abandon_unsettled')
        if self._db.execute("SELECT 1 FROM managed_effects WHERE state NOT IN ('complete','superseded') LIMIT 1").fetchone():
            raise HostError('message_abandon_unsettled')
        dispatches=self.dispatches()
        if any(d['state']!='complete' and d['batch_id']!=batch_id for d in dispatches):raise HostError('message_abandon_unsettled')
        chosen=next((d for d in dispatches if d['batch_id']==batch_id),None)
        if chosen is None or chosen['target_id']!=target_id:raise HostError('message_abandon_identity')
        return hashed({'control':control,'target':self.target(target_id),'managed_targets':targets,
            'settings':self.managed_settings(),'dispatch':chosen})

    def _require_no_unsettled_abandonment(self):
        self._require_no_unsettled_executed_disposition()
        if self._db.execute('PRAGMA user_version').fetchone()[0] in (3,4):
            self._abandon_schema(self._db)
            if self._db.execute(f"SELECT 1 FROM main.{TABLE} WHERE phase!='complete' LIMIT 1").fetchone():
                raise HostError('message_abandon_unsettled')

    def _abandon_create(self,p):
        self._require_lock();p=proposal(p)
        self._require_no_unsettled_executed_disposition()
        with self._tx() as db:
            if self._abandon_record(request_key=p['request_key']) is not None:raise HostError('message_abandon_exists')
            if self._abandon_host_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])!=p['host']['fingerprint']:
                raise HostError('message_abandon_conflict')
            version=db.execute('PRAGMA user_version').fetchone()[0]
            self._check_abandonments()
            if version not in (2,3,4):raise HostError('message_abandon_schema')
            self._abandon_schema(db,create=version==2)
            if db.execute(f"SELECT 1 FROM main.{TABLE} WHERE phase!='complete' LIMIT 1").fetchone():raise HostError('message_abandon_unsettled')
            if db.execute(f'SELECT 1 FROM main.{TABLE} WHERE json_extract(proposal_json,\'$.dispatch.batch_id\')=? LIMIT 1',
                    (p['dispatch']['batch_id'],)).fetchone():raise HostError('message_abandon_exists')
            if len(db.execute(f'SELECT id FROM main.{TABLE} LIMIT 32').fetchall())>=32:
                raise HostError('message_abandon_full')
            identifier=str(uuid.uuid4())
            db.execute(f'INSERT INTO main.{TABLE} VALUES(?,?,1,\'intent\',?,?,\'null\',\'{{}}\')',
                (identifier,p['request_key'],p['proposal_sha256'],encoded(p)))
            if version<3:db.execute('PRAGMA user_version=3')
            self._abandon_record(intent_id=identifier)
        return self._abandon_record(intent_id=identifier)

    def _abandon_advance(self,record,phase,*,seal=None,checkpoints=None):
        self._require_lock()
        with self._tx() as db:
            current=self._abandon_record(intent_id=record['id'])
            if current!=record or PHASES.index(phase)!=PHASES.index(current['phase'])+1:
                raise HostError('message_abandon_conflict')
            p=current['proposal']
            if self._abandon_host_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])!=p['host']['fingerprint']:
                raise HostError('message_abandon_conflict')
            actual=seal if seal is not None else current['proof']
            proof(p['runtime'],actual,p['identity'])
            checks=dict(current['checkpoints'] if checkpoints is None else checkpoints)
            if phase=='complete':
                receipt={'status':'abandoned','reason':'message_admission_sealed_abandoned',
                    'worker_operation_id':p['dispatch']['worker_id'],'message_operation_id':p['dispatch']['batch_id'],
                    'abandonment_intent_id':current['id'],'seal_sha256':hashed(actual)}
                db.execute("UPDATE dispatches SET state='complete',receipt=? WHERE batch_id=?",(encoded(receipt,8192),p['dispatch']['batch_id']))
                db.execute('UPDATE targets SET receipt=? WHERE id=?',(encoded(receipt,8192),p['host']['target_id']))
                checks['host']=self._abandon_host_fingerprint(p['host']['target_id'],p['dispatch']['batch_id'])
            db.execute(f'UPDATE main.{TABLE} SET revision=revision+1,phase=?,proof_json=?,checkpoints_json=? WHERE id=?',
                (phase,encoded(actual,4096),encoded(checks,1024),current['id']))
            self._abandon_record(intent_id=current['id'])
        return self._abandon_record(intent_id=record['id'])
