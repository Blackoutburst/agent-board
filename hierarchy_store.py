"""Transactional human-rooted hierarchy and current authorization ceilings.

Schema installation participates in the Board migration transaction. All
transport callers recheck current authenticated authority; roles grant nothing.
All callers must use the authenticated Board actor and the same transaction
for object creation, provenance, quota debits and receipt.
"""
import hashlib
import json
import secrets
import time

from hierarchy_policy import (AUTONOMY, KINDS, normalize_policy, check_child_policy,
                              require_capacity, exact_string)
from projects import _fail, _json, _version
from profiles import _insert_profile, _record_version


def migrate_hierarchy(db):
    for statement in (
        '''CREATE TABLE hierarchy_limits(owner_id TEXT PRIMARY KEY REFERENCES owners(id),
            version INTEGER NOT NULL CHECK(version>0), policy TEXT NOT NULL, counters TEXT NOT NULL)''',
        '''CREATE TABLE hierarchy_delegations(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES hierarchy_limits(owner_id),
            beneficiary_id TEXT NOT NULL REFERENCES agent_profiles(id), parent_id TEXT,
            version INTEGER NOT NULL CHECK(version>0), status TEXT NOT NULL CHECK(status IN ('active','revoked')),
            policy TEXT NOT NULL, counters TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(owner_id,id), FOREIGN KEY(owner_id,parent_id) REFERENCES hierarchy_delegations(owner_id,id))''',
        '''CREATE TABLE hierarchy_receipts(owner_id TEXT NOT NULL REFERENCES owners(id), principal TEXT NOT NULL,
            operation TEXT NOT NULL, key TEXT NOT NULL, payload_sha256 TEXT NOT NULL, receipt TEXT NOT NULL,
            PRIMARY KEY(owner_id,principal,operation,key))''',
        '''CREATE TABLE hierarchy_project_bindings(owner_id TEXT NOT NULL, project_id TEXT NOT NULL REFERENCES projects(id),
            creator_id TEXT NOT NULL, delegation_id TEXT NOT NULL, profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
            PRIMARY KEY(owner_id,project_id),
            FOREIGN KEY(owner_id,creator_id) REFERENCES hierarchy_delegations(owner_id,id),
            FOREIGN KEY(owner_id,delegation_id) REFERENCES hierarchy_delegations(owner_id,id))''',
        '''CREATE TABLE hierarchy_team_profiles(owner_id TEXT NOT NULL, profile_id TEXT NOT NULL REFERENCES agent_profiles(id),
            parent_id TEXT NOT NULL, delegation_id TEXT NOT NULL, released_at REAL, PRIMARY KEY(owner_id,profile_id),
            FOREIGN KEY(owner_id,parent_id) REFERENCES hierarchy_delegations(owner_id,id),
            FOREIGN KEY(owner_id,delegation_id) REFERENCES hierarchy_delegations(owner_id,id))''',
        'CREATE INDEX hierarchy_parent ON hierarchy_delegations(owner_id,parent_id)',
    ):
        db.execute(statement)


def _zero():
    return {kind: {'live': 0, 'created': 0} for kind in KINDS}


def _bounded(parent, child):
    """Root ceiling/reduction comparison, with no parent edge consumed."""
    parent, child = normalize_policy(parent), normalize_policy(child)
    for key in ('operations', 'project_ids', 'actions', 'resources'):
        if not set(child[key]) <= set(parent[key]):
            _fail('hierarchy_child_exceeds_parent', 403)
    if (child['max_depth'] > parent['max_depth']
            or AUTONOMY[child['autonomy']] > AUTONOMY[parent['autonomy']]
            or child['allow_new_projects'] and not parent['allow_new_projects']
            or any(child['quotas'][kind][key] > parent['quotas'][kind][key]
                   for kind in KINDS for key in ('live', 'created'))):
        _fail('hierarchy_child_exceeds_parent', 403)
    return child


class HierarchyStore:
    def __init__(self, board):
        self.board = board

    def _principal(self, db, actor):
        return 'human' if actor['kind'] == 'human' else 'profile:' + self.board._profile_for_actor(db, actor)['id']

    def _retry(self, db, actor, operation, key, payload):
        exact_string(key)
        hashed = hashlib.sha256(_json(payload).encode()).hexdigest()
        row = db.execute('''SELECT * FROM hierarchy_receipts
            WHERE owner_id=? AND principal=? AND operation=? AND key=?''',
            (actor['owner_id'], self._principal(db, actor), operation, key)).fetchone()
        if row is not None:
            if row['payload_sha256'] != hashed:
                _fail('hierarchy_idempotency_conflict', 409)
            return json.loads(row['receipt'])
        return None

    def _remember(self, db, actor, operation, key, payload, receipt):
        db.execute('INSERT INTO hierarchy_receipts VALUES(?,?,?,?,?,?)',
            (actor['owner_id'], self._principal(db, actor), operation, key,
             hashlib.sha256(_json(payload).encode()).hexdigest(), _json(receipt)))
        self.board._audit(db, actor, 'hierarchy_' + operation,
            {'hierarchy_id': receipt.get('id'), 'version': receipt['version']})
        return receipt

    def _limits(self, db, owner):
        row = db.execute('SELECT * FROM hierarchy_limits WHERE owner_id=?', (owner,)).fetchone()
        if row is None:
            _fail('hierarchy_not_enabled', 403)
        return row

    def _delegation(self, db, owner, identifier):
        exact_string(identifier)
        row = db.execute('SELECT * FROM hierarchy_delegations WHERE owner_id=? AND id=?',
                         (owner, identifier)).fetchone()
        if row is None:
            _fail('hierarchy_delegation_not_found', 404)
        return row

    @staticmethod
    def _view(row):
        return {key: json.loads(row[key]) if key in ('policy', 'counters') else row[key] for key in row.keys()}

    def _target(self, db, actor, beneficiary, policy, *, human_root=False):
        profile = self.board._profile_owned(db, actor, beneficiary)
        projects = json.loads(profile['project_ids'])
        companion_root = human_root and profile['kind'] == 'companion'
        if (profile['legacy'] or profile['status'] == 'archived'
                or profile['kind'] == 'companion' and not companion_root
                or projects is None and not companion_root):
            _fail('hierarchy_beneficiary_invalid', 403)
        # A companion's broad ordinary read scope is not a hierarchy wildcard.
        # Only a human root may supply its finite explicit delegation scope.
        if projects is not None and not set(policy['project_ids']) <= set(projects):
            _fail('hierarchy_profile_project_scope_required', 403)
        for identifier in policy['project_ids']:
            self.board._project_member(db, actor, identifier)
        return profile

    def configure(self, actor, expected_version, policy, key):
        if type(expected_version) is not int or expected_version < 0:
            _fail('hierarchy_version_invalid')
        policy = normalize_policy(policy)
        payload = dict(expected_version=expected_version, policy=policy)
        with self.board._tx() as db:
            actor = self.board._human(db, actor)
            for project in policy['project_ids']:
                self.board._project_member(db, actor, project)
            previous = self._retry(db, actor, 'configure', key, payload)
            if previous is not None:
                return previous
            old = db.execute('SELECT * FROM hierarchy_limits WHERE owner_id=?', (actor['owner_id'],)).fetchone()
            if expected_version != (old['version'] if old else 0):
                _fail('hierarchy_version_conflict', 409)
            counters = json.loads(old['counters']) if old else _zero()
            require_capacity(policy, counters, {})
            if old:
                db.execute('UPDATE hierarchy_limits SET version=version+1,policy=? WHERE owner_id=?',
                           (_json(policy), actor['owner_id']))
            else:
                db.execute('INSERT INTO hierarchy_limits VALUES(?,1,?,?)',
                           (actor['owner_id'], _json(policy), _json(counters)))
            return self._remember(db, actor, 'configure', key, payload,
                                  self._view(self._limits(db, actor['owner_id'])))

    def _chain(self, db, owner, identifier):
        chain, seen, beneficiaries = [], set(), set()
        while identifier is not None:
            row = self._delegation(db, owner, identifier)
            if row['id'] in seen or row['beneficiary_id'] in beneficiaries or len(chain) >= 5:
                _fail('hierarchy_provenance_invalid', 409)
            if row['status'] != 'active':
                _fail('hierarchy_revoked', 403)
            profile = db.execute('SELECT * FROM agent_profiles WHERE id=? AND owner_id=?',
                                 (row['beneficiary_id'], owner)).fetchone()
            if profile is None or profile['status'] != 'active':
                _fail('hierarchy_ancestor_profile_unavailable', 403)
            seen.add(row['id']); beneficiaries.add(row['beneficiary_id']); chain.append(row)
            identifier = row['parent_id']
        return list(reversed(chain))

    def _effective(self, db, actor, chain):
        limits = self._limits(db, actor['owner_id'])
        policies = [normalize_policy(json.loads(limits['policy']))]
        policies += [normalize_policy(json.loads(row['policy'])) for row in chain]
        result = normalize_policy(policies[-1])
        # A created project belongs to this chain only through its immutable
        # transaction binding and the creator's still-current creation ceiling.
        created = set()
        ids = [row['id'] for row in chain]
        for binding in db.execute('SELECT * FROM hierarchy_project_bindings WHERE owner_id=?',
                                  (actor['owner_id'],)):
            if binding['creator_id'] not in ids:
                continue
            index = ids.index(binding['creator_id'])
            ceilings = policies[:index + 2]
            if all(p['allow_new_projects'] and 'create_project_lead' in p['operations'] for p in ceilings):
                created.add(binding['project_id'])
        for field in ('operations', 'project_ids', 'actions', 'resources'):
            scopes = [set(p[field]) for p in policies]
            if field == 'project_ids':
                # Only ancestors through the creator receive binding scope;
                # descendants must explicitly carry the singleton project.
                for i, scope in enumerate(scopes):
                    for project in created:
                        binding = db.execute('SELECT creator_id FROM hierarchy_project_bindings WHERE owner_id=? AND project_id=?',
                                             (actor['owner_id'], project)).fetchone()
                        if i <= ids.index(binding['creator_id']) + 1:
                            scope.add(project)
            result[field] = sorted(set.intersection(*scopes))
        result['allow_new_projects'] = all(p['allow_new_projects'] for p in policies)
        distances = [len(chain) - 1] + [len(chain) - 1 - i for i in range(len(chain))]
        result['max_depth'] = max(0, min(p['max_depth'] - distance
            for p, distance in zip(policies, distances)))
        result['autonomy'] = min((p['autonomy'] for p in policies), key=AUTONOMY.get)
        for kind in KINDS:
            for key in ('live', 'created'):
                result['quotas'][kind][key] = min(p['quotas'][kind][key] for p in policies)
        # Existing owner/profile grants are additional ceilings, never widened.
        owner_policy = json.loads(self.board._owner(db, actor['owner_id'])['policy'])
        for field in ('actions', 'resources'):
            if '*' not in owner_policy[field]:
                result[field] = sorted(set(result[field]) & set(owner_policy[field]))
        for row in chain:
            profile = db.execute('SELECT * FROM agent_profiles WHERE id=?', (row['beneficiary_id'],)).fetchone()
            profile_policy = json.loads(profile['policy'])
            for field in ('actions', 'resources'):
                result[field] = sorted(set(result[field]) & set(profile_policy[field]))
            stored_projects = json.loads(profile['project_ids'])
            if stored_projects is None:
                if profile['kind'] != 'companion' or row['parent_id'] is not None:
                    _fail('hierarchy_beneficiary_invalid', 403)
                stored_projects = json.loads(row['policy'])['project_ids']
            profile_projects = set(stored_projects)
            for project in created:
                binding = db.execute('SELECT creator_id FROM hierarchy_project_bindings WHERE owner_id=? AND project_id=?',
                                     (actor['owner_id'], project)).fetchone()
                if ids.index(row['id']) <= ids.index(binding['creator_id']):
                    profile_projects.add(project)
            result['project_ids'] = sorted(set(result['project_ids']) & profile_projects)
            result['autonomy'] = min(result['autonomy'], profile_policy['autonomy'], key=AUTONOMY.get)
        # Reductions are effective intersections; snapshots of descendants stay intact.
        if result['max_depth'] == 0 and 'delegate_project' in result['operations']:
            result['operations'].remove('delegate_project')
        if not result['allow_new_projects'] and 'create_project_lead' in result['operations']:
            result['operations'].remove('create_project_lead')
        return result

    def _authorize(self, db, actor, identifier, operation=None):
        if actor['kind'] != 'agent':
            _fail('hierarchy_agent_required', 403)
        chain = self._chain(db, actor['owner_id'], identifier)
        profile = self.board._profile_for_actor(db, actor)
        if not chain or chain[-1]['beneficiary_id'] != profile['id']:
            _fail('hierarchy_wrong_beneficiary', 403)
        for row in chain:
            current = json.loads(db.execute('SELECT policy FROM agent_profiles WHERE id=?',
                                           (row['beneficiary_id'],)).fetchone()[0])
            if (not current['allow_read'] or operation is not None and
                    (not current['allow_requests'] or current['autonomy'] == 'responses')):
                _fail('hierarchy_ancestor_permission_denied', 403)
        effective = self._effective(db, actor, chain)
        if operation is not None and effective['autonomy'] == 'responses':
            _fail('hierarchy_ancestor_permission_denied', 403)
        if operation is not None and operation not in effective['operations']:
            _fail('hierarchy_operation_not_authorized', 403)
        return chain, effective

    def _charge(self, db, owner, chain, increments):
        # The Board BEGIN IMMEDIATE transaction serializes the owner and all
        # ancestor ledgers. No public method can supply or reset counters.
        limits = self._limits(db, owner)
        for row in (limits, *chain):
            require_capacity(json.loads(row['policy']), json.loads(row['counters']), increments)
        for table, row, column, identifier in [('hierarchy_limits', limits, 'owner_id', owner)] + [
                ('hierarchy_delegations', row, 'id', row['id']) for row in chain]:
            counters = json.loads(row['counters'])
            for kind, amount in increments.items():
                for key in ('live', 'created'):
                    counters[kind][key] += amount
            db.execute(f'UPDATE {table} SET counters=? WHERE {column}=?', (_json(counters), identifier))

    def _insert(self, db, actor, beneficiary, parent, policy):
        identifier, now = secrets.token_hex(16), time.time()
        db.execute('INSERT INTO hierarchy_delegations VALUES(?,?,?,?,1,?,?,?,?,?)',
            (identifier, actor['owner_id'], beneficiary, parent, 'active', _json(policy), _json(_zero()), now, now))
        return self._view(self._delegation(db, actor['owner_id'], identifier))

    def create_root(self, actor, beneficiary, policy, key):
        policy = normalize_policy(policy)
        payload = dict(beneficiary=beneficiary, policy=policy)
        with self.board._tx() as db:
            actor = self.board._human(db, actor)
            self._target(db, actor, beneficiary, policy, human_root=True)
            previous = self._retry(db, actor, 'create_root', key, payload)
            if previous is not None:
                return previous
            _bounded(json.loads(self._limits(db, actor['owner_id'])['policy']), policy)
            self._charge(db, actor['owner_id'], [], {'delegations': 1})
            result = self._insert(db, actor, beneficiary, None, policy)
            return self._remember(db, actor, 'create_root', key, payload, result)

    def create_child(self, actor, parent_id, beneficiary, policy, expected_version, key):
        _version(expected_version)
        policy = normalize_policy(policy)
        payload = dict(parent_id=parent_id, beneficiary=beneficiary, policy=policy, expected_version=expected_version)
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_requests')
            chain, effective = self._authorize(db, actor, parent_id, 'delegate_project')
            # Current target scope must cover retries as well as first writes.
            check_child_policy(effective, policy)
            self._target(db, actor, beneficiary, policy)
            previous = self._retry(db, actor, 'create_child', key, payload)
            if previous is not None:
                return previous
            if chain[-1]['version'] != expected_version:
                _fail('hierarchy_version_conflict', 409)
            check_child_policy(effective, policy)
            if beneficiary in {row['beneficiary_id'] for row in chain}:
                _fail('hierarchy_provenance_cycle', 409)
            self._target(db, actor, beneficiary, policy)
            self._charge(db, actor['owner_id'], chain, {'delegations': 1})
            result = self._insert(db, actor, beneficiary, parent_id, policy)
            return self._remember(db, actor, 'create_child', key, payload, result)

    def create_project_lead(self, actor, parent_id, name, lead_name, policy, expected_version, key):
        """Internal atomic constructor. No device, binding, grant or Runtime start.

        The client supplies no project ID: the singleton child scope is minted
        with the project and recorded provenance inside the transaction.
        """
        _version(expected_version)
        exact_string(name); exact_string(lead_name)
        policy = normalize_policy(policy)
        if policy['project_ids']:
            _fail('hierarchy_new_project_scope_must_be_empty')
        payload = dict(parent_id=parent_id, name=name, lead_name=lead_name,
                       policy=policy, expected_version=expected_version)
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_requests')
            chain, effective = self._authorize(db, actor, parent_id, 'create_project_lead')
            previous = self._retry(db, actor, 'create_project_lead', key, payload)
            if previous is not None:
                check_child_policy(effective, previous['policy'])
                return previous
            if chain[-1]['version'] != expected_version:
                _fail('hierarchy_version_conflict', 409)
            project_id, profile_id, now = secrets.token_hex(16), secrets.token_hex(16), time.time()
            child_policy = dict(policy, project_ids=[project_id])
            check_child_policy(effective, child_policy, created_project_id=project_id)
            self._charge(db, actor['owner_id'], chain, dict(projects=1, profiles=1, delegations=1))
            db.execute("""INSERT INTO projects(id,name,description,context,repository_url,environment,created_at,updated_at)
                VALUES(?,?,'','','','',?,?)""", (project_id, name, now, now))
            db.execute('INSERT INTO project_members(project_id,owner_id) VALUES(?,?)',
                       (project_id, actor['owner_id']))
            _insert_profile(db, profile_id, actor['owner_id'], lead_name, now=now)
            profile_policy = dict(allow_read=True, allow_messages=False, allow_requests=True,
                actions=child_policy['actions'], resources=child_policy['resources'], autonomy=child_policy['autonomy'])
            db.execute('UPDATE agent_profiles SET policy=?,project_ids=? WHERE id=?',
                       (_json(profile_policy), _json([project_id]), profile_id))
            _record_version(db, profile_id, replace_initial=True)
            result = self._insert(db, actor, profile_id, parent_id, child_policy)
            db.execute('INSERT INTO hierarchy_project_bindings VALUES(?,?,?,?,?)',
                       (actor['owner_id'], project_id, parent_id, result['id'], profile_id))
            result.update(project_id=project_id, profile_id=profile_id)
            self.board._audit(db, actor, 'hierarchy_project_lead_created', dict(
                project_id=project_id, profile_id=profile_id, parent_id=parent_id, delegation_id=result['id']))
            return self._remember(db, actor, 'create_project_lead', key, payload, result)

    def create_team_profile(self, actor, parent_id, name, role, policy, expected_version, key):
        """Create an ordinary leaf profile with single-project provenance."""
        _version(expected_version)
        exact_string(name); exact_string(role)
        policy = normalize_policy(policy)
        if policy['operations'] or policy['max_depth'] != 0 or policy['allow_new_projects']:
            _fail('hierarchy_team_must_be_leaf', 403)
        payload = dict(parent_id=parent_id, name=name, role=role, policy=policy, expected_version=expected_version)
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_requests')
            chain, effective = self._authorize(db, actor, parent_id, 'create_team_profile')
            check_child_policy(effective, policy)
            previous = self._retry(db, actor, 'create_team_profile', key, payload)
            if previous is not None:
                return previous
            if chain[-1]['version'] != expected_version:
                _fail('hierarchy_version_conflict', 409)
            check_child_policy(effective, policy)
            project = db.execute('''SELECT p.* FROM projects p JOIN project_members m ON m.project_id=p.id
                WHERE p.id=? AND m.owner_id=?''', (policy['project_ids'][0], actor['owner_id'])).fetchone()
            if project is None or project['archived']:
                _fail('hierarchy_project_unavailable', 403)
            self._charge(db, actor['owner_id'], chain, dict(profiles=1, delegations=1))
            identifier = secrets.token_hex(16)
            _insert_profile(db, identifier, actor['owner_id'], name)
            profile_policy = dict(allow_read=True, allow_messages=False, allow_requests=True,
                actions=policy['actions'], resources=policy['resources'], autonomy=policy['autonomy'])
            self.board._write_profile(db, identifier, dict(role=role, policy=profile_policy,
                                      project_ids=policy['project_ids']), increment=False)
            result = self._insert(db, actor, identifier, parent_id, policy)
            db.execute('INSERT INTO hierarchy_team_profiles(owner_id,profile_id,parent_id,delegation_id) VALUES(?,?,?,?)',
                       (actor['owner_id'], identifier, parent_id, result['id']))
            result['profile_id'] = identifier
            return self._remember(db, actor, 'create_team_profile', key, payload, result)

    def update_team_profile(self, actor, parent_id, profile_id, expected_version, changes, key):
        """Cosmetic changes and pause/resume; grants and archival stay separate."""
        _version(expected_version)
        allowed = {'name', 'avatar', 'description', 'role', 'instructions', 'language', 'response_style', 'status'}
        if type(changes) is not dict or not changes or set(changes) - allowed:
            _fail('hierarchy_team_changes_invalid')
        if 'status' in changes and changes['status'] not in ('active', 'paused'):
            _fail('hierarchy_team_changes_invalid')
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_requests')
            chain, effective = self._authorize(db, actor, parent_id, 'manage_team_profile')
            link = db.execute('SELECT * FROM hierarchy_team_profiles WHERE owner_id=? AND profile_id=? AND parent_id=?',
                              (actor['owner_id'], profile_id, parent_id)).fetchone()
            if link is None:
                _fail('hierarchy_team_profile_not_found', 404)
            delegation = self._delegation(db, actor['owner_id'], link['delegation_id'])
            if delegation['status'] != 'active':
                _fail('hierarchy_revoked', 403)
            target = self.board._profile_owned(db, actor, profile_id)
            if target['legacy'] or target['kind'] != 'agent' or target['status'] == 'archived':
                _fail('hierarchy_beneficiary_invalid', 403)
            target_policy = json.loads(delegation['policy'])
            check_child_policy(effective, target_policy)
            if json.loads(target['project_ids']) != target_policy['project_ids']:
                _fail('hierarchy_profile_project_scope_required', 403)
            values = self.board._profile_values(db, actor, changes)
            payload = dict(parent_id=parent_id, profile_id=profile_id, expected_version=expected_version, changes=values)
            previous = self._retry(db, actor, 'update_team_profile', key, payload)
            if previous is not None:
                return previous
            if target['version'] != expected_version:
                _fail('hierarchy_version_conflict', 409)
            self.board._write_profile(db, profile_id, values)
            result = dict(id=delegation['id'], profile_id=profile_id, version=expected_version + 1)
            return self._remember(db, actor, 'update_team_profile', key, payload, result)

    def _archive_clear(self, db, owner, profile_id):
        # Lease expiry is not evidence of cleanup. Preserve every old claim and
        # Runtime row; this method may only refuse, never reconcile their state.
        for table in ('tickets', 'tasks'):
            if db.execute(f'''SELECT 1 FROM {table} t WHERE t.claimant IS NOT NULL
                AND t.status NOT IN ('done','complete') AND (
                    json_extract(t.claimant,'$.agent_id')=? OR EXISTS (
                        SELECT 1 FROM sessions s WHERE s.agent_id=? AND s.owner_id=?
                        AND s.id=json_extract(t.claimant,'$.session_id')
                        AND s.device_id=json_extract(t.claimant,'$.device_id'))) LIMIT 1''',
                (profile_id, profile_id, owner)).fetchone():
                _fail('hierarchy_archive_claim_present', 409)
        if db.execute('''SELECT 1 FROM runtime_executions WHERE owner_id=? AND agent_id=?
            AND (state!='finished' OR cleanup_confirmed!=1) LIMIT 1''', (owner, profile_id)).fetchone():
            _fail('hierarchy_archive_cleanup_unconfirmed', 409)
        if db.execute('''SELECT 1 FROM delegation_families f WHERE f.owner_id=?
            AND f.state IN ('running','integrating') AND EXISTS (
                SELECT 1 FROM sessions s WHERE s.owner_id=? AND s.agent_id=? AND (
                    s.id=f.session_id OR EXISTS (SELECT 1 FROM delegation_members m
                        WHERE m.family_id=f.id AND m.session_id=s.id))) LIMIT 1''',
            (owner, owner, profile_id)).fetchone():
            _fail('hierarchy_archive_family_active', 409)

    def archive_team_profile(self, actor, parent_id, profile_id, expected_version, key):
        """Archive a leaf once, only after durable work is reconciled normally."""
        _version(expected_version)
        payload = dict(parent_id=parent_id, profile_id=profile_id, expected_version=expected_version)
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_requests')
            if actor['kind'] == 'human':
                # Human reconciliation remains possible after ancestor revocation.
                chain, seen, current = [], set(), parent_id
                while current is not None:
                    row = self._delegation(db, actor['owner_id'], current)
                    if current in seen or len(chain) >= 5:
                        _fail('hierarchy_provenance_invalid', 409)
                    chain.append(row); seen.add(current); current = row['parent_id']
                chain.reverse()
            else:
                chain, effective = self._authorize(db, actor, parent_id, 'manage_team_profile')
            link = db.execute('SELECT * FROM hierarchy_team_profiles WHERE owner_id=? AND profile_id=? AND parent_id=?',
                              (actor['owner_id'], profile_id, parent_id)).fetchone()
            if link is None:
                _fail('hierarchy_team_profile_not_found', 404)
            target = self.board._profile_owned(db, actor, profile_id)
            if actor['kind'] != 'human':
                leaf_scope = json.loads(self._delegation(db, actor['owner_id'], link['delegation_id'])['policy'])
                check_child_policy(effective, leaf_scope)
                if json.loads(target['project_ids']) != leaf_scope['project_ids']:
                    _fail('hierarchy_profile_project_scope_required', 403)
            previous = self._retry(db, actor, 'archive_team_profile', key, payload)
            if previous is not None:
                return previous
            target = self.board._profile_owned(db, actor, profile_id)
            if target['version'] != expected_version:
                _fail('hierarchy_version_conflict', 409)
            if link['released_at'] is not None:
                _fail('hierarchy_archive_already_released', 409)
            leaf = self._delegation(db, actor['owner_id'], link['delegation_id'])
            if (leaf['parent_id'] != parent_id or leaf['beneficiary_id'] != profile_id
                    or target['legacy'] or target['kind'] != 'agent'
                    or db.execute('SELECT 1 FROM hierarchy_delegations WHERE owner_id=? AND parent_id=?',
                                  (actor['owner_id'], leaf['id'])).fetchone()):
                _fail('hierarchy_provenance_invalid', 409)
            self._archive_clear(db, actor['owner_id'], profile_id)
            limits = self._limits(db, actor['owner_id'])
            ledgers = [('hierarchy_limits', limits, 'owner_id', actor['owner_id'])] + [
                ('hierarchy_delegations', row, 'id', row['id']) for row in chain]
            for table, row, column, identifier in ledgers:
                counters = json.loads(row['counters'])
                for kind in ('profiles', 'delegations'):
                    if counters[kind]['live'] < 1:
                        _fail('hierarchy_ledger_invalid', 409)
                    counters[kind]['live'] -= 1
                db.execute(f'UPDATE {table} SET counters=? WHERE {column}=?', (_json(counters), identifier))
            self.board._write_profile(db, profile_id, dict(status='archived'))
            now = time.time()
            db.execute("UPDATE hierarchy_delegations SET status='revoked',version=version+1,updated_at=? WHERE id=?",
                       (now, leaf['id']))
            db.execute('UPDATE hierarchy_team_profiles SET released_at=? WHERE owner_id=? AND profile_id=?',
                       (now, actor['owner_id'], profile_id))
            return self._remember(db, actor, 'archive_team_profile', key, payload,
                                  dict(id=leaf['id'], profile_id=profile_id, version=expected_version + 1))

    def archive_project_lead(self, actor, project_id, project_version, profile_version, key):
        """Human reconciliation only; archive after team/work cleanup, once."""
        exact_string(project_id); _version(project_version); _version(profile_version)
        payload = dict(project_id=project_id, project_version=project_version, profile_version=profile_version)
        with self.board._tx() as db:
            actor = self.board._human(db, actor)
            binding = db.execute('SELECT * FROM hierarchy_project_bindings WHERE owner_id=? AND project_id=?',
                                 (actor['owner_id'], project_id)).fetchone()
            if binding is None:
                _fail('hierarchy_project_binding_not_found', 404)
            previous = self._retry(db, actor, 'archive_project_lead', key, payload)
            if previous is not None:
                return previous
            if db.execute("SELECT 1 FROM hierarchy_receipts WHERE owner_id=? AND operation='archive_project_lead' "
                          "AND json_extract(receipt,'$.project_id')=?", (actor['owner_id'], project_id)).fetchone():
                _fail('hierarchy_archive_already_released', 409)
            project = self.board._project_member(db, actor, project_id)
            profile = self.board._profile_owned(db, actor, binding['profile_id'])
            leaf = self._delegation(db, actor['owner_id'], binding['delegation_id'])
            if project['version'] != project_version or profile['version'] != profile_version:
                _fail('hierarchy_version_conflict', 409)
            if (profile['legacy'] or profile['kind'] != 'agent'
                    or json.loads(profile['project_ids']) != [project_id]
                    or leaf['beneficiary_id'] != profile['id'] or leaf['parent_id'] != binding['creator_id']):
                _fail('hierarchy_provenance_invalid', 409)
            if db.execute("SELECT 1 FROM hierarchy_delegations d WHERE d.owner_id=? AND d.parent_id=? "
                          "AND NOT EXISTS (SELECT 1 FROM hierarchy_team_profiles t WHERE t.owner_id=d.owner_id "
                          "AND t.delegation_id=d.id AND t.released_at IS NOT NULL)",
                          (actor['owner_id'], leaf['id'])).fetchone():
                _fail('hierarchy_archive_descendants_present', 409)
            self._archive_clear(db, actor['owner_id'], profile['id'])
            if db.execute("SELECT 1 FROM tickets WHERE project_id=? AND claimant IS NOT NULL "
                          "AND status NOT IN ('done','complete') LIMIT 1", (project_id,)).fetchone():
                _fail('hierarchy_archive_claim_present', 409)
            if db.execute("SELECT 1 FROM tasks t JOIN tickets k ON k.thread_id=t.thread_id "
                          "WHERE k.project_id=? AND t.claimant IS NOT NULL "
                          "AND t.status NOT IN ('done','complete') LIMIT 1", (project_id,)).fetchone():
                _fail('hierarchy_archive_claim_present', 409)
            if db.execute("SELECT 1 FROM runtime_executions WHERE project_id=? "
                          "AND (state!='finished' OR cleanup_confirmed!=1) LIMIT 1", (project_id,)).fetchone():
                _fail('hierarchy_archive_cleanup_unconfirmed', 409)
            if db.execute("SELECT 1 FROM delegation_families WHERE project_id=? "
                          "AND state IN ('running','integrating') LIMIT 1", (project_id,)).fetchone():
                _fail('hierarchy_archive_family_active', 409)
            chain, seen, current = [], set(), binding['creator_id']
            while current is not None:
                row = self._delegation(db, actor['owner_id'], current)
                if current in seen or len(chain) >= 5:
                    _fail('hierarchy_provenance_invalid', 409)
                chain.append(row); seen.add(current); current = row['parent_id']
            limits = self._limits(db, actor['owner_id'])
            for table, row, column, identifier in [('hierarchy_limits', limits, 'owner_id', actor['owner_id'])] + [
                    ('hierarchy_delegations', row, 'id', row['id']) for row in chain]:
                counters = json.loads(row['counters'])
                for kind in ('projects', 'profiles', 'delegations'):
                    if counters[kind]['live'] < 1:
                        _fail('hierarchy_ledger_invalid', 409)
                    counters[kind]['live'] -= 1
                db.execute(f'UPDATE {table} SET counters=? WHERE {column}=?', (_json(counters), identifier))
            self.board._write_profile(db, profile['id'], dict(status='archived'))
            db.execute('UPDATE projects SET archived=1,version=version+1,updated_at=? WHERE id=?', (time.time(), project_id))
            db.execute("UPDATE hierarchy_delegations SET status='revoked',version=version+1,updated_at=? WHERE id=?",
                       (time.time(), leaf['id']))
            return self._remember(db, actor, 'archive_project_lead', key, payload,
                dict(id=leaf['id'], project_id=project_id, profile_id=profile['id'],
                     version=project_version + 1, profile_version=profile_version + 1))

    def change(self, actor, identifier, expected_version, key, *, policy=None, revoke=False):
        _version(expected_version)
        if type(revoke) is not bool or (policy is None and not revoke) or (policy is not None and revoke):
            _fail('hierarchy_change_invalid')
        policy = normalize_policy(policy) if policy is not None else None
        payload = dict(id=identifier, expected_version=expected_version, policy=policy, revoke=revoke)
        with self.board._tx() as db:
            actor = self.board._human(db, actor)
            row = self._delegation(db, actor['owner_id'], identifier)
            previous = self._retry(db, actor, 'change', key, payload)
            if previous is not None:
                return previous
            if row['version'] != expected_version or row['status'] != 'active':
                _fail('hierarchy_version_conflict', 409)
            if policy is not None:
                _bounded(json.loads(row['policy']), policy)
            db.execute('''UPDATE hierarchy_delegations SET version=version+1,status=?,policy=?,updated_at=?
                WHERE id=?''', ('revoked' if revoke else 'active', _json(policy) if policy else row['policy'],
                                time.time(), identifier))
            return self._remember(db, actor, 'change', key, payload,
                                  self._view(self._delegation(db, actor['owner_id'], identifier)))

    def list_for_owner(self, actor, *, limit=50, after=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            _fail('hierarchy_page_invalid')
        if after is not None:
            exact_string(after)
        with self.board._tx() as db:
            actor = self.board._human(db, actor)
            limits = db.execute('SELECT * FROM hierarchy_limits WHERE owner_id=?',
                                (actor['owner_id'],)).fetchone()
            rows = db.execute('SELECT * FROM hierarchy_delegations WHERE owner_id=? AND id>? ORDER BY id LIMIT ?',
                              (actor['owner_id'], after or '', limit + 1)).fetchall()
            return dict(limits=self._view(limits) if limits is not None else None,
                        delegations=[self._view(row) for row in rows[:limit]],
                        next_after=rows[limit - 1]['id'] if len(rows) > limit else None)

    def list_for_agent(self, actor, limit=20, after=None):
        """Scan a bounded page of own grants; omit currently denied chains.

        next_after advances across scanned rows, including revoked grants, so a
        page may be empty while more candidates remain. Never scan owner-wide
        history or expose another beneficiary's delegation.
        """
        from board import BoardError
        if type(limit) is not int or not 1 <= limit <= 50:
            _fail('hierarchy_page_invalid')
        if after is not None:
            exact_string(after)
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_read')
            if actor['kind'] != 'agent':
                _fail('hierarchy_agent_required', 403)
            profile = self.board._profile_for_actor(db, actor)
            rows = db.execute('SELECT id FROM hierarchy_delegations WHERE owner_id=? AND beneficiary_id=? '
                              'AND id>? ORDER BY id LIMIT ?',
                              (actor['owner_id'], profile['id'], after or '', limit + 1)).fetchall()
            items = []
            for row in rows[:limit]:
                try:
                    chain, effective = self._authorize(db, actor, row['id'])
                    if not set(json.loads(chain[-1]['policy'])['project_ids']) <= set(effective['project_ids']):
                        continue
                except BoardError as exc:
                    if exc.status != 403:
                        raise
                    continue
                items.append(dict(delegation=self._view(chain[-1]), effective=effective,
                                  ancestry=[dict(id=r['id'], version=r['version']) for r in chain]))
            return dict(items=items, next_after=rows[limit - 1]['id'] if len(rows) > limit else None)

    def inspect(self, actor, identifier):
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_read')
            if actor['kind'] == 'human':
                return self._view(self._delegation(db, actor['owner_id'], identifier))
            chain, effective = self._authorize(db, actor, identifier)
            if not set(json.loads(chain[-1]['policy'])['project_ids']) <= set(effective['project_ids']):
                _fail('hierarchy_project_scope_denied', 403)
            return dict(delegation=self._view(chain[-1]), effective=effective,
                        ancestry=[dict(id=row['id'], version=row['version']) for row in chain])
