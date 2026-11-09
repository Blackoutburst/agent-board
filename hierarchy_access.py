"""Current hierarchy ceilings for existing Board authorization boundaries.

No schema creation or grant occurs here. Ordinary pre-hierarchy profiles keep
existing authorization; profiles minted by a hierarchy must retain their exact
creation provenance. No caller-supplied delegation selects a different chain.
"""
import json

from hierarchy_store import HierarchyStore
from projects import _fail


def installed(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='hierarchy_limits'").fetchone() is not None


def creation_delegation(db, profile):
    if not installed(db):
        return None
    rows = db.execute('''SELECT delegation_id FROM hierarchy_project_bindings WHERE owner_id=? AND profile_id=?
        UNION ALL SELECT delegation_id FROM hierarchy_team_profiles WHERE owner_id=? AND profile_id=?''',
        (profile['owner_id'], profile['id'], profile['owner_id'], profile['id'])).fetchall()
    if len(rows) > 1:
        _fail('hierarchy_provenance_invalid', 409)
    return rows[0][0] if rows else None


def profile_ceiling(db, board, actor, profile, permission=None):
    identifier = creation_delegation(db, profile)
    if identifier is None:
        return None
    store = HierarchyStore(board)
    chain = store._chain(db, profile['owner_id'], identifier)
    if chain[-1]['beneficiary_id'] != profile['id']:
        _fail('hierarchy_provenance_invalid', 409)
    mapped = 'allow_messages' if permission == 'allow_reply' else permission
    for row in chain:
        policy = json.loads(db.execute('SELECT policy FROM agent_profiles WHERE id=?',
                                      (row['beneficiary_id'],)).fetchone()[0])
        if not policy['allow_read'] or mapped and not policy.get(mapped, False):
            _fail('hierarchy_ancestor_permission_denied', 403)
        if permission == 'allow_requests' and policy['autonomy'] == 'responses':
            _fail('hierarchy_ancestor_permission_denied', 403)
    return store._effective(db, dict(actor, owner_id=profile['owner_id']), chain)


def project_allowed(db, board, actor, profile, project_id):
    ceiling = profile_ceiling(db, board, actor, profile, 'allow_read')
    if ceiling is not None:
        return project_id in ceiling['project_ids']
    projects = json.loads(profile['project_ids'])
    if projects is None or project_id in projects:
        return True
    if not installed(db):
        return False
    # Root profiles may see only projects created through a currently valid
    # root binding; ordinary owner membership alone is insufficient.
    store = HierarchyStore(board)
    roots = db.execute('''SELECT id FROM hierarchy_delegations
        WHERE owner_id=? AND beneficiary_id=? AND parent_id IS NULL AND status='active' ''',
        (profile['owner_id'], profile['id'])).fetchall()
    for row in roots:
        chain = store._chain(db, profile['owner_id'], row['id'])
        if project_id in store._effective(db, actor, chain)['project_ids']:
            return True
    return False
