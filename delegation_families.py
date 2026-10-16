"""Bounded temporary workers of one coordinator; never an account dispatcher.

The coordinator keeps its live claim. Runtime hooks are called in the same
admission transaction, after retry lookup and before inserting a reservation.
No method launches a model, accesses a checkout, or infers successful cleanup.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import time
import unicodedata

from projects import _fail, _json, _same_claim, _text, _version

_MODEL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,199}$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
_ACTIONS = {'repo.read_file', 'repo.patch_file', 'repo.run_test'}


def migrate_delegation_families(db):
    expected = {
        'delegation_families': ('id','owner_id','agent_id','profile_version','device_id','session_id',
            'project_id','parent_ticket_id','plan','plan_sha256','state','revision','created_at','updated_at',
            'stop_reason','final_snapshot_sha256','final_verification'),
        'delegation_members': ('family_id','ticket_id','session_id','role','model','files','state',
            'result_sha256','accepted_version','verification_evidence'),
    }
    existing = {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('delegation_families','delegation_members')")}
    if existing:
        if existing != set(expected) or any(tuple(row['name'] for row in db.execute(
                'PRAGMA table_info('+table+')')) != columns for table,columns in expected.items()):
            raise RuntimeError('Unsupported delegation family schema')
        return
    for statement in (
        """CREATE TABLE delegation_families(
            id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
            agent_id TEXT NOT NULL REFERENCES agent_profiles(id),
            profile_version INTEGER NOT NULL, device_id TEXT NOT NULL REFERENCES devices(id),
            session_id TEXT NOT NULL REFERENCES sessions(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            parent_ticket_id TEXT UNIQUE NOT NULL REFERENCES tickets(id),
            plan TEXT NOT NULL, plan_sha256 TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('running','integrating','review_ready','cancelled')),
            revision INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
            updated_at REAL NOT NULL, stop_reason TEXT NOT NULL DEFAULT '',
            final_snapshot_sha256 TEXT NOT NULL DEFAULT '', final_verification TEXT NOT NULL DEFAULT '')""",
        """CREATE TABLE delegation_members(
            family_id TEXT NOT NULL REFERENCES delegation_families(id),
            ticket_id TEXT UNIQUE NOT NULL REFERENCES tickets(id),
            session_id TEXT NOT NULL REFERENCES sessions(id),
            role TEXT NOT NULL, model TEXT NOT NULL, files TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','accepted')),
            result_sha256 TEXT NOT NULL DEFAULT '', accepted_version INTEGER,
            verification_evidence TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(family_id,ticket_id))""",
        'CREATE INDEX delegation_owner ON delegation_families(owner_id,created_at)',
    ):
        db.execute(statement)


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _result_hash(ticket):
    return _hash({key: ticket[key] for key in ('outcome', 'evidence')})


def _plan(children, limits, actions, resource, manifest_sha256):
    if type(children) is not list or len(children) != 2:
        _fail('Exactly two temporary workers are required')
    if type(limits) is not dict or set(limits) != {
            'max_model_turns', 'max_tool_actions', 'max_concurrent', 'deadline'}:
        _fail('Invalid delegation limits')
    for key, low, high in [('max_model_turns', 2, 64), ('max_tool_actions', 0, 128),
                           ('max_concurrent', 1, 4)]:
        if type(limits[key]) is not int or not low <= limits[key] <= high:
            _fail('Invalid delegation limits')
    if type(limits['deadline']) not in (int, float) or not math.isfinite(limits['deadline']) or limits['deadline'] <= 0:
        _fail('Invalid delegation deadline')
    if (type(actions) is not list or any(type(a) is not str or a not in _ACTIONS for a in actions)
            or len(actions) != len(set(actions))):
        _fail('Invalid delegation action bounds')
    _text(resource, 'resource', 0, 200)
    if actions and not resource:
        _fail('A development resource is required')
    if type(manifest_sha256) is not str or not _SHA.fullmatch(manifest_sha256):
        _fail('A parent manifest digest is required')
    selected, sessions, allocated = [], set(), set()
    for child in children:
        if type(child) is not dict or set(child) != {'session_id', 'role', 'model', 'title', 'description', 'acceptance_criteria', 'files'}:
            _fail('Invalid temporary worker')
        for key, maximum in [('session_id', 200), ('role', 100), ('title', 200), ('description', 20000), ('acceptance_criteria', 20000)]:
            _text(child[key], key, 1, maximum)
        if type(child['model']) is not str or not _MODEL.fullmatch(child['model']):
            _fail('Invalid child model')
        if child['session_id'] in sessions:
            _fail('Temporary workers require separate sessions')
        sessions.add(child['session_id'])
        files = child['files']
        if type(files) is not list or len(files) > 64:
            _fail('Invalid child file bounds')
        for path in files:
            if (type(path) is not str or len(path) > 200
                    or not path or path.startswith('/') or '\\' in path
                    or any(part in ('', '.', '..') or part.startswith('.') for part in path.split('/'))
                    or any(ord(c) < 32 or ord(c) == 127 for c in path)
                    or any(0xD800 <= ord(c) <= 0xDFFF for c in path)):
                _fail('Child file bounds must be relative and disjoint')
            normalized = unicodedata.normalize('NFC', path).casefold()
            if len(path.encode('utf-8')) > 200 or normalized in allocated:
                _fail('Child file bounds must be relative and disjoint')
            allocated.add(normalized)
        selected.append(dict(child, files=sorted(files)))
    return {'schema': 1, 'depth': 1, 'children': selected, 'limits': limits,
            'actions': sorted(actions), 'resource': resource,
            'parent_manifest_sha256': manifest_sha256}


class DelegationFamiliesMixin:
    def _delegation_ticket_family(self, db, ticket_id):
        return db.execute('''SELECT f.* FROM delegation_families f LEFT JOIN delegation_members m ON m.family_id=f.id
            WHERE f.parent_ticket_id=? OR m.ticket_id=? LIMIT 1''',(ticket_id,ticket_id)).fetchone()

    def _delegation_claim_check(self, db, actor, ticket):
        self._delegation_claim_scope(db,actor,ticket['id'])
        family = self._delegation_ticket_family(db,ticket['id'])
        if family is None:return
        member = db.execute('SELECT * FROM delegation_members WHERE family_id=? AND ticket_id=?',
            (family['id'],ticket['id'])).fetchone()
        if member:
            if (actor['kind'] != 'agent' or any(actor.get(k) != family[k] for k in ('owner_id','device_id'))
                    or actor['session_id'] != member['session_id']):
                _fail('Only the exact temporary session may claim its delegated ticket',403)
            if member['state'] != 'pending':_fail('Delegated result was already accepted',409)
        elif actor['kind'] == 'agent' and any(actor.get(k) != family[k] for k in ('owner_id','device_id','session_id')):
            _fail('Only the exact coordinator may claim its parent',403)
        reason = self._delegation_stop_reason(db,family)
        if reason:_fail(reason,409)

    def _resource_claim_conflict(self, db, ticket):
        if not ticket['resource']:return False
        family = self._delegation_ticket_family(db,ticket['id'])
        if self._delegation_resource_held(db,ticket['resource'],family['id'] if family else None):return True
        for other in db.execute("SELECT id FROM tickets WHERE resource=? AND id!=? AND claimant IS NOT NULL AND status!='done'",
                (ticket['resource'],ticket['id'])):
            peer = self._delegation_ticket_family(db,other['id'])
            if (family is None or peer is None or family['id'] != peer['id']
                    or json.loads(family['plan'])['resource'] != ticket['resource']):return True
        return False

    @staticmethod
    def _delegation_resource_held(db, resource, own_family_id=None):
        """A family fence survives stop, release and uncertain process cleanup."""
        if not resource:return False
        for family in db.execute("SELECT id,parent_ticket_id FROM delegation_families WHERE json_extract(plan,'$.resource')=?",
                (resource,)):
            if family['id']==own_family_id:continue
            tickets=[family['parent_ticket_id']]+[row[0] for row in db.execute(
                'SELECT ticket_id FROM delegation_members WHERE family_id=?',(family['id'],))]
            placeholders=','.join('?' for _ in tickets)
            if db.execute(f'SELECT 1 FROM tickets WHERE id IN ({placeholders}) AND claimant IS NOT NULL',tickets).fetchone():
                return True
            if db.execute(f"SELECT 1 FROM runtime_executions WHERE ticket_id IN ({placeholders}) AND (state!='finished' OR cleanup_confirmed!=1)",tickets).fetchone():
                return True
        return False

    def _ticket(self, row, db=None):
        if db is None:
            with self._connection() as connection:return self._ticket(row,connection)
        value = super()._ticket(row,db)
        family = self._delegation_ticket_family(db,row['id'])
        if family is not None:
            member = db.execute('SELECT role,model FROM delegation_members WHERE family_id=? AND ticket_id=?',
                (family['id'],row['id'])).fetchone()
            # Every family ticket belongs to this already-authorized project.
            # Publish only card summaries, never the private bounded plan or
            # session/device/file/hash details attached to its execution.
            parent = db.execute('SELECT id,title,number,status FROM tickets WHERE id=? AND project_id=?',
                (family['parent_ticket_id'],family['project_id'])).fetchone()
            children = db.execute('''SELECT t.id,t.title,t.number,t.status,m.role,m.model,m.state
                FROM delegation_members m JOIN tickets t ON t.id=m.ticket_id
                WHERE m.family_id=? AND t.project_id=? ORDER BY t.number,t.id''',
                (family['id'],family['project_id'])).fetchall()
            if parent is None or len(children)!=2:
                _fail('Delegation family not found',404)
            profile = db.execute('SELECT * FROM agent_profiles WHERE id=? AND owner_id=?',
                (family['agent_id'],family['owner_id'])).fetchone()
            agent_name = self._profile_public(profile)['name'] if profile else ''
            coordinator = {'ticket_id':parent['id'],'title':parent['title'],'number':parent['number'],
                'status':parent['status'],'agent_name':agent_name,'role':'coordinator','model':None}
            summaries = [{'ticket_id':child['id'],'title':child['title'],'number':child['number'],
                'status':child['status'],'agent_name':agent_name,'role':child['role'],
                'model':child['model'],'result_state':child['state']} for child in children]
            value['delegation'] = {'family_id':family['id'],'parent_ticket_id':family['parent_ticket_id'],
                'owner_id':family['owner_id'],'agent_id':family['agent_id'],'state':family['state'],
                'revision':family['revision'],'role':'coordinator' if member is None else member['role'],
                'model':None if member is None else member['model'],
                'should_stop':bool(self._delegation_stop_reason(db,family)),
                'coordinator':coordinator,'children':summaries}
        return value

    @staticmethod
    def _delegation_session_busy(db, owner_id, device_id, session_id, parent_ticket_id=''):
        for table, terminal in [('tickets', 'done'), ('tasks', 'complete')]:
            if db.execute(f'''SELECT 1 FROM {table} WHERE status!=?
                AND json_extract(claimant,'$.owner_id')=?
                AND json_extract(claimant,'$.device_id')=?
                AND json_extract(claimant,'$.session_id')=? AND id!=?''',
                (terminal, owner_id, device_id, session_id, parent_ticket_id)).fetchone():
                return True
        return bool(db.execute('''SELECT 1 FROM runtime_executions WHERE owner_id=?
            AND device_id=? AND session_id=? AND (state!='finished' OR cleanup_confirmed!=1)''',
            (owner_id, device_id, session_id)).fetchone())

    def _family_row(self, db, family_id):
        _text(family_id, 'family_id', 1, 200)
        row = db.execute('SELECT * FROM delegation_families WHERE id=?', (family_id,)).fetchone()
        if row is None:
            _fail('Delegation family not found', 404)
        return row

    def _family_parent(self, db, actor, row):
        actor, profile = self._runtime_authorize(db, actor, row['project_id'],
                                               row['parent_ticket_id'], row['profile_version'])
        if any(actor.get(k) != row[k] for k in ('owner_id', 'device_id', 'session_id')) or profile['id'] != row['agent_id']:
            _fail('Only the exact coordinator may manage this family', 403)
        return actor, profile

    def _family_view(self, db, row, *, private=False):
        result = {key: row[key] for key in ('id', 'owner_id', 'agent_id', 'project_id',
                  'parent_ticket_id', 'state', 'revision', 'created_at', 'updated_at', 'stop_reason')}
        result['children'] = [{key: m[key] for key in ('ticket_id', 'role', 'model', 'state',
                              'result_sha256', 'accepted_version')}
                              for m in db.execute('SELECT * FROM delegation_members WHERE family_id=? ORDER BY ticket_id', (row['id'],))]
        result['plan_sha256'] = row['plan_sha256']
        reason = self._delegation_stop_reason(db, row)
        result.update(should_stop=bool(reason), control_reason=reason,
                      final_snapshot_sha256=row['final_snapshot_sha256'])
        if private:
            result['plan'] = json.loads(row['plan'])
        return result

    def get_delegation_family(self, actor, family_id):
        with self._connection() as db:
            actor = self._actor(db, actor, 'allow_read')
            row = self._family_row(db, family_id)
            self._project_member(db, actor, row['project_id'])
            private = actor['owner_id'] == row['owner_id'] and (
                actor['kind'] == 'human' or self._profile_for_actor(db, actor)['id'] == row['agent_id'])
            return self._family_view(db, row, private=private)

    def lookup_delegation_family(self, actor, parent_ticket_id, idempotency_key):
        """Recover only this exact session's opening; never create or claim."""
        _text(idempotency_key, 'idempotency_key', 1, 200)
        with self._connection() as db:
            actor = self._actor(db, actor, 'allow_read')
            if actor['kind'] != 'agent':
                _fail('An agent session is required', 403)
            parent = self._ticket_member(db, actor, parent_ticket_id)
            record = db.execute('SELECT record_id FROM idempotency WHERE actor=? AND operation=? AND key=?',
                                (self._actor_key(actor), 'open_delegation_family', idempotency_key)).fetchone()
            if not record:
                return {'found': False}
            row = self._family_row(db, record['record_id'])
            if row['parent_ticket_id'] != parent['id']:
                _fail('Delegation opening key conflict', 409)
            return {'found': True, 'family': self._family_view(db, row, private=True)}

    def open_delegation_family(self, actor, parent_ticket_id, expected_version, children,
                               limits, actions, resource, manifest_sha256, idempotency_key):
        _version(expected_version)
        _text(idempotency_key, 'idempotency_key', 1, 200)
        plan = _plan(children, limits, actions, resource, manifest_sha256)
        payload = {'parent_ticket_id': parent_ticket_id, 'plan': plan}
        with self._tx() as db:
            actor = self._actor(db, actor, 'allow_requests')
            self._actor(db, actor, 'allow_read')
            parent = self._ticket_member(db, actor, parent_ticket_id)
            actor, profile = self._runtime_authorize(db, actor, parent['project_id'], parent_ticket_id)
            previous = self._retry(db, actor, 'open_delegation_family', idempotency_key, payload)
            if previous:
                return self._family_view(db, self._family_row(db, previous), private=True)
            if not time.time() < plan['limits']['deadline'] <= time.time() + 86400:
                _fail('Invalid delegation deadline')
            if parent['version'] != expected_version:
                _fail('Parent changed; reread before delegating', 409)
            if db.execute('SELECT 1 FROM delegation_members WHERE ticket_id=?', (parent_ticket_id,)).fetchone():
                _fail('Temporary workers cannot delegate again', 409)
            if db.execute('SELECT 1 FROM delegation_families WHERE parent_ticket_id=?', (parent_ticket_id,)).fetchone():
                _fail('This parent already has a delegation family', 409)
            if db.execute('SELECT 1 FROM delegation_families WHERE owner_id=? AND device_id=? AND session_id=?',
                          (actor['owner_id'], actor['device_id'], actor['session_id'])).fetchone():
                _fail('A coordinator session belongs to only one family', 409)
            if self._delegation_session_busy(db, actor['owner_id'], actor['device_id'], actor['session_id'], parent_ticket_id):
                _fail('Coordinator has other unfinished work', 409)
            if db.execute('SELECT COUNT(*) FROM delegation_families WHERE owner_id=?', (actor['owner_id'],)).fetchone()[0] >= 256:
                _fail('Delegation history capacity reached', 409)
            if db.execute("SELECT 1 FROM runtime_executions WHERE ticket_id=? AND (state!='finished' OR cleanup_confirmed!=1)",
                          (parent_ticket_id,)).fetchone():
                _fail('Finish and confirm coordinator cleanup before delegation', 409)
            grants = self._effective_profile_policy(db, actor, profile)
            if (resource != parent['resource'] or not set(actions).issubset(grants['actions'])
                    or actions and resource not in grants['resources']):
                _fail('Delegation exceeds current grants', 403)
            if resource and db.execute('SELECT 1 FROM resource_locks WHERE resource=?',(resource,)).fetchone():
                _fail('Delegation resource is held by a legacy task',409)
            for child in plan['children']:
                session = db.execute('SELECT * FROM sessions WHERE id=?', (child['session_id'],)).fetchone()
                if (not session or child['session_id'] == actor['session_id']
                        or any(session[k] != actor[k] for k in ('owner_id', 'device_id'))
                        or session['agent_id'] != profile['id'] or session['profile_version'] != profile['version']):
                    _fail('Temporary session scope mismatch', 403)
                if db.execute('SELECT 1 FROM delegation_members WHERE session_id=?', (child['session_id'],)).fetchone():
                    _fail('Temporary session is already bound', 409)
                if db.execute('SELECT 1 FROM delegation_families WHERE session_id=?', (child['session_id'],)).fetchone():
                    _fail('A coordinator cannot become a temporary worker', 409)
                if self._delegation_session_busy(db, actor['owner_id'], actor['device_id'], child['session_id']):
                    _fail('Temporary session has unfinished work', 409)
            family_id, now = secrets.token_hex(16), time.time()
            db.execute('''INSERT INTO delegation_families(id,owner_id,agent_id,profile_version,device_id,
                session_id,project_id,parent_ticket_id,plan,plan_sha256,state,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'running',?,?)''',
                (family_id, actor['owner_id'], profile['id'], profile['version'], actor['device_id'],
                 actor['session_id'], parent['project_id'], parent_ticket_id, _json(plan), _hash(plan), now, now))
            project = self._project_member(db, actor, parent['project_id'])
            for offset, child in enumerate(plan['children']):
                ticket_id, thread_id = secrets.token_hex(16), secrets.token_hex(16)
                number = project['next_ticket_number'] + offset
                db.execute('INSERT INTO threads(id,title,created_at,updated_at) VALUES(?,?,?,?)',
                           (thread_id, self._ticket_thread_title(project['name'], number, child['title']), now, now))
                db.execute('INSERT INTO thread_members SELECT ?,owner_id FROM project_members WHERE project_id=?',
                           (thread_id, parent['project_id']))
                db.execute('''INSERT INTO tickets(id,project_id,number,thread_id,title,description,acceptance_criteria,priority,resource,
                    status,assignee_owner_id,assignee_agent_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,'todo',?,?,?,?)''',
                    (ticket_id, parent['project_id'], number, thread_id, child['title'], child['description'], child['acceptance_criteria'],
                     parent['priority'], parent['resource'], actor['owner_id'], profile['id'], now, now))
                db.execute('INSERT INTO delegation_members(family_id,ticket_id,session_id,role,model,files) VALUES(?,?,?,?,?,?)',
                           (family_id, ticket_id, child['session_id'], child['role'], child['model'], _json(child['files'])))
                db.execute('INSERT INTO ticket_dependencies VALUES(?,?)', (parent_ticket_id, ticket_id))
            db.execute('UPDATE projects SET next_ticket_number=next_ticket_number+2,updated_at=? WHERE id=?', (now, parent['project_id']))
            db.execute('UPDATE tickets SET assignee_agent_id=COALESCE(assignee_agent_id,?),version=version+1,updated_at=? WHERE id=?', (profile['id'], now, parent_ticket_id))
            self._remember(db, actor, 'open_delegation_family', idempotency_key, payload, family_id)
            self._audit(db, actor, 'delegation_family_opened', {'family_id': family_id, 'parent_ticket_id': parent_ticket_id})
            return self._family_view(db, self._family_row(db, family_id), private=True)

    def _delegation_claim_scope(self, db, actor, ticket_id):
        """Fence a dedicated session before it can immobilize other work."""
        if actor['kind'] != 'agent':
            return
        bound = db.execute('''SELECT m.ticket_id FROM delegation_members m JOIN delegation_families f ON f.id=m.family_id
            WHERE f.owner_id=? AND f.device_id=? AND m.session_id=?''',
            (actor['owner_id'], actor['device_id'], actor['session_id'])).fetchone()
        if bound and bound['ticket_id'] != ticket_id:
            _fail('Temporary session cannot execute outside its delegated ticket', 403)
        coordinator = db.execute('''SELECT parent_ticket_id FROM delegation_families
            WHERE owner_id=? AND device_id=? AND session_id=?''',
            (actor['owner_id'], actor['device_id'], actor['session_id'])).fetchone()
        if coordinator and coordinator['parent_ticket_id'] != ticket_id:
            _fail('Coordinator session cannot execute outside its family', 403)

    def _delegation_runtime_check(self, db, actor, ticket_id, provider, model, *, admission=True):
        """Admission-only hook, inside the Runtime insert transaction."""
        self._delegation_claim_scope(db, actor, ticket_id)
        row = db.execute('''SELECT f.* FROM delegation_families f LEFT JOIN delegation_members m ON m.family_id=f.id
            WHERE f.parent_ticket_id=? OR m.ticket_id=? LIMIT 1''', (ticket_id, ticket_id)).fetchone()
        if row is None:
            return
        reason = self._delegation_stop_reason(db, row)
        if reason:
            _fail(reason, 409)
        parent = ticket_id == row['parent_ticket_id']
        if parent and row['state'] != 'integrating':
            _fail('Coordinator must wait for verified child results', 409)
        if not parent:
            member = db.execute('SELECT * FROM delegation_members WHERE family_id=? AND ticket_id=?', (row['id'], ticket_id)).fetchone()
            if member['session_id'] != actor['session_id'] or member['state'] != 'pending':
                _fail('Temporary worker session mismatch', 403)
            if provider == 'codex_chatgpt' and model != member['model']:
                _fail('Temporary worker model mismatch', 403)
        elif actor['session_id'] != row['session_id']:
            _fail('Coordinator session mismatch', 403)
        plan = json.loads(row['plan'])
        if provider not in ('codex_chatgpt', 'local_broker'):
            _fail('Unapproved delegation provider', 403)
        if provider == 'local_broker':
            action = {'read_file': 'repo.read_file', 'patch_file': 'repo.patch_file'}.get(model, 'repo.run_test')
            if action not in plan['actions']:
                _fail('Local action exceeds delegation bounds', 403)
        tickets = [row['parent_ticket_id']] + [m[0] for m in db.execute('SELECT ticket_id FROM delegation_members WHERE family_id=?', (row['id'],))]
        placeholders = ','.join('?' for _ in tickets)
        executions = db.execute(f'SELECT provider,state FROM runtime_executions WHERE owner_id=? AND ticket_id IN ({placeholders}) AND created_at>=?',
                                (row['owner_id'], *tickets, row['created_at'])).fetchall()
        key = 'max_model_turns' if provider == 'codex_chatgpt' else 'max_tool_actions'
        if admission and sum(e['provider'] == provider for e in executions) >= plan['limits'][key]:
            _fail('Delegation budget exhausted', 409)
        count = sum(e['state'] != 'finished' for e in executions)
        if (count >= plan['limits']['max_concurrent'] if admission else count > plan['limits']['max_concurrent']):
            _fail('Delegation capacity exhausted', 409)

    def _delegation_stop_reason(self, db, row):
        if row['state'] in ('cancelled', 'review_ready'):
            return 'delegation_stopped'
        if time.time() >= json.loads(row['plan'])['limits']['deadline']:
            return 'delegation_deadline'
        actor = {k: row[k] for k in ('owner_id', 'device_id', 'session_id')} | {'kind': 'agent'}
        from board import BoardError
        try:
            actor, profile = self._family_parent(db, actor, row)
            plan = json.loads(row['plan'])
            parent = db.execute('SELECT resource FROM tickets WHERE id=?', (row['parent_ticket_id'],)).fetchone()
            if not parent or parent['resource'] != plan['resource']:
                return 'delegation_resource_changed'
            grants = self._effective_profile_policy(db, actor, profile)
            if (not set(plan['actions']).issubset(grants['actions'])
                    or plan['actions'] and plan['resource'] not in grants['resources']):
                return 'delegation_grants_changed'
        except BoardError:
            return 'delegation_parent_authorization_changed'
        plan = json.loads(row['plan'])
        children = {child['session_id']:child for child in plan['children']}
        for member in db.execute('SELECT * FROM delegation_members WHERE family_id=?',(row['id'],)):
            ticket = db.execute('SELECT * FROM tickets WHERE id=?', (member['ticket_id'],)).fetchone()
            if member['state'] == 'accepted':
                if (not ticket or ticket['status'] != 'done' or ticket['version'] != member['accepted_version']
                        or _result_hash(ticket) != member['result_sha256']):return 'delegation_result_changed'
                continue
            source = children[member['session_id']]
            session = db.execute('SELECT * FROM sessions WHERE id=?',(member['session_id'],)).fetchone()
            if (not ticket or not session or ticket['project_id'] != row['project_id']
                    or ticket['assignee_owner_id'] != row['owner_id'] or ticket['assignee_agent_id'] != row['agent_id']
                    or ticket['resource'] != plan['resource'] or ticket['status'] not in ('todo','in_progress','review')
                    or any(ticket[key] != source[key] for key in ('title','description','acceptance_criteria'))
                    or any(session[key] != row[key] for key in ('owner_id','device_id','agent_id','profile_version'))):
                return 'delegation_child_scope_changed'
            claimant = json.loads(ticket['claimant']) if ticket['claimant'] else None
            if claimant is not None and (ticket['status'] != 'in_progress' or claimant.get('kind') != 'agent'
                    or claimant.get('session_id') != member['session_id']
                    or any(claimant.get(key) != row[key] for key in ('owner_id','device_id','agent_id'))
                    or (ticket['lease_expires_at'] or 0) <= time.time()):return 'delegation_child_claim_changed'
            if ticket['status'] == 'in_progress' and claimant is None:return 'delegation_child_claim_changed'
            if db.execute("SELECT 1 FROM human_requests WHERE ticket_id=? AND status='open'",(ticket['id'],)).fetchone():
                return 'delegation_child_scope_changed'
            if db.execute("SELECT 1 FROM ticket_dependencies d JOIN tickets t ON t.id=d.depends_on_id WHERE d.ticket_id=? AND t.status!='done'",
                    (ticket['id'],)).fetchone():return 'delegation_child_scope_changed'
        return ''

    def _delegation_execution_control(self, db, execution):
        row = db.execute('''SELECT f.* FROM delegation_families f LEFT JOIN delegation_members m ON m.family_id=f.id
            WHERE f.parent_ticket_id=? OR m.ticket_id=? LIMIT 1''', (execution['ticket_id'], execution['ticket_id'])).fetchone()
        return self._delegation_stop_reason(db, row) if row else ''

    def stop_delegation_family(self, actor, family_id, expected_revision):
        _version(expected_revision)
        with self._tx() as db:
            human = isinstance(actor,dict) and actor.get('kind') == 'human'
            actor = self._human(db,actor) if human else self._runtime_identity(db,actor)[0]
            row = self._family_row(db, family_id)
            if human and actor['owner_id'] != row['owner_id']:_fail('Delegation family not found',404)
            if not human and any(actor[k] != row[k] for k in ('owner_id', 'device_id', 'session_id')):
                _fail('Only the exact coordinator may stop this family', 403)
            if row['state'] == 'cancelled' and row['revision'] == expected_revision + 1:
                return self._family_view(db, row)
            if row['revision'] != expected_revision or row['state'] == 'review_ready':
                _fail('Delegation revision conflict', 409)
            db.execute("UPDATE delegation_families SET state='cancelled',stop_reason='operator_requested',revision=revision+1,updated_at=? WHERE id=?", (time.time(), family_id))
            self._audit(db, actor, 'delegation_family_stopped', {'family_id': family_id})
            return self._family_view(db, self._family_row(db, family_id))

    def accept_delegation_result(self, actor, family_id, expected_revision, child_ticket_id,
                                 expected_child_version, result_sha256, verification_evidence, idempotency_key):
        _version(expected_revision);_version(expected_child_version)
        _text(idempotency_key, 'idempotency_key', 1, 200)
        _text(verification_evidence, 'verification_evidence', 1, 20000)
        if type(result_sha256) is not str or not _SHA.fullmatch(result_sha256):
            _fail('Invalid delegated result digest')
        with self._tx() as db:
            row = self._family_row(db, family_id)
            actor, _ = self._family_parent(db, actor, row)
            payload = {'family_id': family_id, 'child_ticket_id': child_ticket_id,
                       'result_sha256': result_sha256, 'verification_evidence': verification_evidence}
            previous = self._retry(db, actor, 'accept_delegation_result', idempotency_key, payload)
            if previous:
                return self._family_view(db, self._family_row(db, previous), private=True)
            if row['state'] != 'running' or row['revision'] != expected_revision or self._delegation_stop_reason(db, row):
                _fail('Delegation state changed', 409)
            member = db.execute('SELECT * FROM delegation_members WHERE family_id=? AND ticket_id=?', (family_id, child_ticket_id)).fetchone()
            if not member or member['state'] != 'pending':
                _fail('Delegated result is not pending', 409)
            child = self._ticket_member(db, actor, child_ticket_id)
            detail = self._ticket(child, db)
            if (child['version'] != expected_child_version or child['status'] != 'review'
                    or child['claimant'] is not None or detail['waiting_on_human'] or detail['blocked_by']
                    or not child['outcome'].strip() or not child['evidence'].strip()
                    or _result_hash(child) != result_sha256):
                _fail('Delegated result requires current released review evidence', 409)
            if db.execute("SELECT 1 FROM runtime_executions WHERE owner_id=? AND ticket_id=? AND (state!='finished' OR cleanup_confirmed!=1)",
                          (row['owner_id'], child_ticket_id)).fetchone():
                _fail('Delegated cleanup is not confirmed', 409)
            now = time.time()
            db.execute("UPDATE tickets SET status='done',version=version+1,updated_at=? WHERE id=?", (now, child_ticket_id))
            db.execute("UPDATE delegation_members SET state='accepted',result_sha256=?,accepted_version=?,verification_evidence=? WHERE family_id=? AND ticket_id=?",
                       (result_sha256, child['version']+1, verification_evidence, family_id, child_ticket_id))
            pending = db.execute("SELECT 1 FROM delegation_members WHERE family_id=? AND state='pending'", (family_id,)).fetchone()
            db.execute('UPDATE delegation_families SET state=?,revision=revision+1,updated_at=? WHERE id=?', ('running' if pending else 'integrating', now, family_id))
            db.execute('UPDATE tickets SET version=version+1,updated_at=? WHERE id=?', (now, row['parent_ticket_id']))
            self._unblock_dependents(db,actor,child_ticket_id)
            self._audit(db, actor, 'delegated_result_accepted', {'family_id': family_id, 'ticket_id': child_ticket_id, 'result_sha256': result_sha256})
            self._remember(db, actor, 'accept_delegation_result', idempotency_key, payload, family_id)
            return self._family_view(db, self._family_row(db, family_id), private=True)

    def finalize_delegation_family(self, actor, family_id, expected_revision, snapshot_sha256,
                                   verification_evidence, idempotency_key):
        """Seal the coordinator's audit receipt; never complete its ticket."""
        _version(expected_revision)
        _text(idempotency_key, 'idempotency_key', 1, 200)
        _text(verification_evidence, 'verification_evidence', 1, 20000)
        if type(snapshot_sha256) is not str or not _SHA.fullmatch(snapshot_sha256):
            _fail('Invalid integration snapshot digest')
        payload = {'family_id': family_id, 'snapshot_sha256': snapshot_sha256,
                   'verification_evidence': verification_evidence}
        with self._tx() as db:
            row = self._family_row(db, family_id)
            actor, _ = self._family_parent(db, actor, row)
            previous = self._retry(db, actor, 'finalize_delegation_family', idempotency_key, payload)
            if previous:
                return self._family_view(db, self._family_row(db, previous), private=True)
            if row['revision'] != expected_revision or row['state'] != 'integrating' or self._delegation_stop_reason(db, row):
                _fail('Delegation is not ready for integration review', 409)
            tickets = [row['parent_ticket_id']] + [m[0] for m in db.execute('SELECT ticket_id FROM delegation_members WHERE family_id=?', (family_id,))]
            placeholders = ','.join('?' for _ in tickets)
            if db.execute(f"SELECT 1 FROM runtime_executions WHERE owner_id=? AND ticket_id IN ({placeholders}) AND (state!='finished' OR cleanup_confirmed!=1)",
                          (row['owner_id'], *tickets)).fetchone():
                _fail('Family cleanup is not confirmed', 409)
            db.execute("UPDATE delegation_families SET state='review_ready',revision=revision+1,updated_at=?,final_snapshot_sha256=?,final_verification=? WHERE id=?",
                       (time.time(), snapshot_sha256, verification_evidence, family_id))
            self._remember(db, actor, 'finalize_delegation_family', idempotency_key, payload, family_id)
            self._audit(db, actor, 'delegation_family_review_ready', {'family_id': family_id, 'snapshot_sha256': snapshot_sha256})
            return self._family_view(db, self._family_row(db, family_id), private=True)
