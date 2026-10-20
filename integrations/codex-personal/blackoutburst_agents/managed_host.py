"""Per-target controls inside the existing single owner host journal.

This module does not launch a process or create a Runtime proof. A managed
journal is prepared only while stopped, clean and holding its original owner
lock. Legacy journals retain their original behavior until that explicit step.
"""
from __future__ import annotations
import json
import re
import uuid

from .host_config import HostError, digest


_TABLES = {
    'managed_settings': ('id','host_id','catalog_sha256'),
    'managed_targets': ('target_id','instance_id','launchspec_sha256','enabled','activation',
                        'generation','remaining','reason'),
    'managed_commands': ('id','payload','payload_sha256','target_id','generation','action','state'),
}


def _opaque(value):
    if (type(value) is not str or not 1 <= len(value) <= 200
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise HostError('managed_command_invalid')
    try:
        if len(value.encode('utf-8','strict')) > 800:raise ValueError()
    except (UnicodeError, ValueError):raise HostError('managed_command_invalid') from None
    return value


def _sha(value):
    if type(value) is not str or re.fullmatch('[0-9a-f]{64}',value) is None:
        raise HostError('managed_command_invalid')
    return value


class ManagedHostJournalMixin:
    def managed(self):
        return self._db.execute('PRAGMA user_version').fetchone()[0] == 2

    def _check_managed(self):
        if not self.managed():return
        for name,columns in _TABLES.items():
            if tuple(r['name'] for r in self._db.execute('PRAGMA table_info('+name+')')) != columns:
                raise HostError('host_corrupt')
        if self._db.execute('SELECT COUNT(*) FROM managed_settings').fetchone()[0] != 1:
            raise HostError('host_corrupt')
        config_ids={t['id'] for t in self.control()['configuration']['targets']}
        if {r['target_id'] for r in self._db.execute('SELECT target_id FROM managed_targets')} != config_ids:
            raise HostError('host_corrupt')

    def prepare_managed(self, host_id, catalog):
        self._require_lock();_opaque(host_id)
        config=self.control()['configuration']
        if type(catalog) is not list or len(catalog)!=len(config['targets']):
            raise HostError('managed_catalog_invalid')
        expected={t['id']:(t['agent_id'],t['profile_version']) for t in config['targets']}
        entries={}
        for e in catalog:
            if (type(e) is not dict or set(e)!={'catalog_key','profile_id','profile_version','launchspec_sha256','mode'}
                    or e.get('catalog_key') in entries
                    or e.get('mode') != ('messages' if config['schema_version']==2 else 'ticket_text')
                    or type(e.get('profile_version')) is not int
                    or expected.get(e.get('catalog_key')) != (e.get('profile_id'),e.get('profile_version'))):
                raise HostError('managed_catalog_invalid')
            _sha(e['launchspec_sha256']);entries[e['catalog_key']]=dict(e)
        if set(entries)!=set(expected):raise HostError('managed_catalog_invalid')
        hashed=digest(sorted(entries.values(),key=lambda e:e['catalog_key']))
        with self._tx() as db:
            if self.managed():
                row=db.execute('SELECT * FROM managed_settings WHERE id=1').fetchone()
                if row is None or row['host_id']!=host_id or row['catalog_sha256']!=hashed:
                    raise HostError('managed_catalog_conflict')
                return
            control=self.control()
            if control['enabled'] or any(d['state']!='complete' for d in self.dispatches()):
                raise HostError('managed_requires_clean_stopped_host')
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables!={'control','targets','dispatches'}:raise HostError('host_corrupt')
            db.execute('CREATE TABLE managed_settings(id INTEGER PRIMARY KEY CHECK(id=1),host_id TEXT NOT NULL,catalog_sha256 TEXT NOT NULL)')
            db.execute('''CREATE TABLE managed_targets(target_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL,
                launchspec_sha256 TEXT NOT NULL,enabled INTEGER NOT NULL,activation TEXT NOT NULL,
                generation INTEGER NOT NULL,remaining INTEGER NOT NULL,reason TEXT NOT NULL)''')
            db.execute('''CREATE TABLE managed_commands(id TEXT PRIMARY KEY,payload TEXT NOT NULL,payload_sha256 TEXT NOT NULL,
                target_id TEXT NOT NULL,generation INTEGER NOT NULL,action TEXT NOT NULL,state TEXT NOT NULL)''')
            db.execute('INSERT INTO managed_settings VALUES(1,?,?)',(host_id,hashed))
            db.executemany("INSERT INTO managed_targets VALUES(?,'',?,0,'',0,0,'not_enabled')",
                           [(k,e['launchspec_sha256']) for k,e in entries.items()])
            db.execute('PRAGMA user_version=2')
        self._check_managed()

    def bind_managed_instances(self, bindings):
        if type(bindings) is not dict:raise HostError('managed_catalog_invalid')
        with self._tx() as db:
            rows=db.execute('SELECT * FROM managed_targets').fetchall()
            if set(bindings)!={r['target_id'] for r in rows}:raise HostError('managed_catalog_invalid')
            if len(set(bindings.values()))!=len(bindings):raise HostError('managed_catalog_invalid')
            for row in rows:
                identity=_opaque(bindings[row['target_id']])
                if row['instance_id'] and row['instance_id']!=identity:raise HostError('managed_catalog_conflict')
                db.execute('UPDATE managed_targets SET instance_id=? WHERE target_id=?',(identity,row['target_id']))

    def managed_target(self, target_id):
        row=self._db.execute('SELECT * FROM managed_targets WHERE target_id=?',(target_id,)).fetchone()
        if row is None:raise HostError('host_target_unknown')
        value=dict(row)
        if (value['enabled'] not in (0,1) or type(value['generation']) is not int or value['generation']<0
                or type(value['remaining']) is not int or not 0<=value['remaining']<=20):
            raise HostError('host_corrupt')
        _sha(value['launchspec_sha256'])
        if value['activation']:
            try:
                if str(uuid.UUID(value['activation']))!=value['activation']:raise ValueError()
            except (TypeError,ValueError,AttributeError):raise HostError('host_corrupt') from None
        if value['enabled'] and not value['activation']:raise HostError('host_corrupt')
        if value['reason'] not in ('not_enabled','enabled','stopped','needs_review','budget_exhausted',
                                  'authorization_changed','configuration_changed'):
            raise HostError('host_corrupt')
        value['enabled']=bool(value['enabled'])
        return value

    def target_token(self, target_id):
        if not self.managed():return None
        t=self.managed_target(target_id)
        return (t['generation'],t['activation'])

    def target_cancelled(self, target_id, token):
        if not self.managed():return False
        target=self.managed_target(target_id)
        return not target['enabled'] or token!=(target['generation'],target['activation'])

    def target_selected(self, target_id):
        if not self.managed():return True
        t=self.managed_target(target_id)
        return t['enabled'] and t['remaining']>0

    def _managed_stop_all(self, db, reason):
        if self.managed():
            db.execute('UPDATE managed_targets SET enabled=0,reason=?',(reason,))

    def stop_target(self, target_id, reason='stopped'):
        if reason not in ('stopped','authorization_changed','needs_review','budget_exhausted'):
            raise HostError('invalid_host_reason')
        with self._tx() as db:
            self.managed_target(target_id)
            db.execute('UPDATE managed_targets SET enabled=0,reason=? WHERE target_id=?',(reason,target_id))
            if not db.execute('SELECT 1 FROM managed_targets WHERE enabled=1').fetchone():
                db.execute('UPDATE control SET enabled=0,revision=revision+1,reason=? WHERE enabled=1',(reason,))

    def queue_managed_command(self, command):
        """Durably accept metadata. Only stop may have an immediate effect."""
        keys={'id','target_id','instance_id','generation','action','launchspec_sha256','budget'}
        if type(command) is not dict or set(command)!=keys:raise HostError('managed_command_invalid')
        for k in ('id','target_id','instance_id'):_opaque(command[k])
        _sha(command['launchspec_sha256'])
        if (command['action'] not in ('start','stop','restart') or type(command['generation']) is not int
                or not 0<=command['generation']<2**53 or type(command['budget']) is not int
                or not (command['budget']==0 if command['action']=='stop' else 1<=command['budget']<=20)):
            raise HostError('managed_command_invalid')
        payload=json.dumps(command,sort_keys=True,separators=(',',':'));hashed=digest(command)
        with self._tx() as db:
            old=db.execute('SELECT * FROM managed_commands WHERE id=?',(command['id'],)).fetchone()
            if old:
                if old['payload_sha256']!=hashed or old['payload']!=payload:raise HostError('managed_command_conflict')
                return dict(old)
            target=self.managed_target(command['target_id'])
            if target['instance_id']!=command['instance_id'] or target['launchspec_sha256']!=command['launchspec_sha256']:
                raise HostError('managed_catalog_conflict')
            if command['generation']<target['generation']:raise HostError('managed_command_stale')
            if command['action']!='stop' and command['generation']<=target['generation']:
                raise HostError('managed_command_stale')
            db.execute("UPDATE managed_commands SET state='superseded' WHERE target_id=? AND state='queued'",(command['target_id'],))
            db.execute("INSERT INTO managed_commands VALUES(?,?,?,?,?,?,'queued')",(command['id'],payload,hashed,
                command['target_id'],command['generation'],command['action']))
            # Restart also revokes the old activation immediately. Its start is
            # separate and requires the original owner lock and clean children.
            if command['action'] in ('stop','restart'):
                db.execute("UPDATE managed_targets SET enabled=0,generation=?,reason='stopped' WHERE target_id=?",
                           (command['generation'],command['target_id']))
                if not db.execute('SELECT 1 FROM managed_targets WHERE enabled=1').fetchone():
                    db.execute("UPDATE control SET enabled=0,revision=revision+1,reason='stopped' WHERE enabled=1")
            return dict(db.execute('SELECT * FROM managed_commands WHERE id=?',(command['id'],)).fetchone())

    def apply_managed_command(self, command_id):
        self._require_lock()
        with self._tx() as db:
            row=db.execute('SELECT * FROM managed_commands WHERE id=?',(command_id,)).fetchone()
            if row is None:raise HostError('managed_command_unknown')
            if row['state']!='queued':return dict(row)
            try:
                if len(row['payload'].encode('utf-8'))>4096:raise ValueError()
                command=json.loads(row['payload'])
                if (type(command) is not dict or set(command)!={'id','target_id','instance_id','generation','action','launchspec_sha256','budget'}
                        or row['payload_sha256']!=digest(command)
                        or any(command[k]!=row[k] for k in ('id','target_id','generation','action'))
                        or command['action'] not in ('start','stop','restart')
                        or type(command['generation']) is not int or not 0<=command['generation']<2**53
                        or type(command['budget']) is not int
                        or not (command['budget']==0 if command['action']=='stop' else 1<=command['budget']<=20)):
                    raise ValueError()
            except (ValueError,TypeError,KeyError,UnicodeError,RecursionError):raise HostError('host_corrupt') from None
            target=self.managed_target(row['target_id'])
            if (command['instance_id']!=target['instance_id']
                    or command['launchspec_sha256']!=target['launchspec_sha256']
                    or command['generation']<target['generation']):raise HostError('managed_command_stale')
            pending=[d for d in self.dispatches() if d['state']!='complete']
            if pending:raise HostError('host_work_pending')
            if command['action'] in ('start','restart'):
                if target['enabled']:raise HostError('host_target_active')
                control=self.control()
                if command['budget']>control['configuration']['dispatch_budget']:raise HostError('managed_budget_exceeds_configuration')
                if not control['enabled']:
                    db.execute("UPDATE control SET enabled=1,revision=revision+1,activation=?,remaining=?,reason='enabled'",
                               (str(uuid.uuid4()),control['configuration']['dispatch_budget']))
                db.execute("UPDATE managed_targets SET enabled=1,activation=?,generation=?,remaining=?,reason='enabled' WHERE target_id=?",
                           (str(uuid.uuid4()),command['generation'],command['budget'],command['target_id']))
            else:
                db.execute("UPDATE managed_targets SET generation=? WHERE target_id=?",(command['generation'],command['target_id']))
            db.execute("UPDATE managed_commands SET state='applied' WHERE id=?",(command_id,))
            return dict(db.execute('SELECT * FROM managed_commands WHERE id=?',(command_id,)).fetchone())

    def _charge_managed_dispatch(self, db, target_id):
        if not self.managed():return
        target=self.managed_target(target_id)
        if not target['enabled']:raise HostError('host_target_stopped')
        if target['remaining']<1:raise HostError('host_budget_exhausted')
        db.execute('UPDATE managed_targets SET remaining=remaining-1 WHERE target_id=?',(target_id,))

    def settle_managed_budgets(self):
        self._require_lock()
        if not self.managed():return
        pending={d['target_id'] for d in self.dispatches() if d['state']!='complete'}
        targets=[r['target_id'] for r in self._db.execute(
            'SELECT target_id FROM managed_targets WHERE enabled=1 AND remaining=0')]
        for target in targets:
            if target not in pending:self.stop_target(target,'budget_exhausted')
