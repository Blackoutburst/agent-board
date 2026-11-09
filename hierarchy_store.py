"""Transactional hierarchy records, not yet exposed by Board or MCP.

Schema installation is explicit for migration composition tests. No existing
authorization path consults this store yet: records alone grant no authority.
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

    def _target(self, db, actor, beneficiary, policy):
        profile = self.board._profile_owned(db, actor, beneficiary)
        if profile['legacy'] or profile['status'] == 'archived' or json.loads(profile['project_ids']) is None:
            _fail('hierarchy_beneficiary_invalid', 403)
        if not set(policy['project_ids']) <= set(json.loads(profile['project_ids'])):
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
        for field in ('operations', 'project_ids', 'actions', 'resources'):
            result[field] = sorted(set.intersection(*(set(p[field]) for p in policies)))
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
            result['project_ids'] = sorted(set(result['project_ids']) & set(json.loads(profile['project_ids'])))
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
            self._target(db, actor, beneficiary, policy)
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
            # Revoked ancestors are checked before idempotent receipt lookup.
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

    def inspect(self, actor, identifier):
        with self.board._tx() as db:
            actor = self.board._actor(db, actor, 'allow_read')
            if actor['kind'] == 'human':
                return self._view(self._delegation(db, actor['owner_id'], identifier))
            chain, effective = self._authorize(db, actor, identifier)
            return dict(delegation=self._view(chain[-1]), effective=effective,
                        ancestry=[dict(id=row['id'], version=row['version']) for row in chain])
