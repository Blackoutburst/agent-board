"""Closed value contracts for future human-rooted profile delegations.

This module grants no authority, exposes no API, and performs no database or
Runtime effects. Callers must obtain provenance from authenticated storage and
apply current owner/profile/session intersections in the admission transaction.
"""
from copy import deepcopy

from projects import _fail

OPERATIONS = frozenset({'create_project_lead', 'create_team_profile',
                        'manage_team_profile', 'delegate_project'})
AUTONOMY = {'responses': 0, 'requested': 1, 'queue': 2}
FIELDS = frozenset({'operations', 'project_ids', 'allow_new_projects', 'actions',
                    'resources', 'autonomy', 'max_depth', 'quotas'})
KINDS = ('projects', 'profiles', 'delegations')
RESERVED = ('agent.', 'runtime.', 'server.', 'provider.', 'device.', 'credential.', 'policy.')
RESOURCE_RESERVED = ('agent-profiles:', 'runtime:', 'server:', 'provider:', 'device:', 'credential:', 'policy:')


def require(value, code='hierarchy_policy_invalid'):
    if not value:
        _fail(code)


def exact_string(value):
    require(type(value) is str and 0 < len(value) <= 200 and value == value.strip()
            and not any(ord(c) < 32 or ord(c) == 127 for c in value) and '*' not in value)
    return value


def exact_list(value):
    require(type(value) is list and len(value) <= 128)
    result = [exact_string(item) for item in value]
    require(len(set(result)) == len(result))
    return sorted(result)


def normalize_policy(value):
    """Validate a proposed ceiling, never an owner grant or effective policy."""
    require(type(value) is dict and set(value) == FIELDS)
    result = {key: exact_list(value[key]) for key in ('operations', 'project_ids', 'actions', 'resources')}
    require(set(result['operations']) <= OPERATIONS)
    require(all(not item.startswith(RESERVED) for item in result['actions']))
    require(all(not item.startswith(RESOURCE_RESERVED) for item in result['resources']))
    require(type(value['allow_new_projects']) is bool)
    require(type(value['autonomy']) is str and value['autonomy'] in AUTONOMY)
    require(type(value['max_depth']) is int and 0 <= value['max_depth'] <= 4)
    quotas = value['quotas']
    require(type(quotas) is dict and set(quotas) == set(KINDS))
    for kind in KINDS:
        require(type(quotas[kind]) is dict and set(quotas[kind]) == {'live', 'created'})
        require(all(type(n) is int and 0 <= n <= 1000000 for n in quotas[kind].values()))
        require(quotas[kind]['live'] <= quotas[kind]['created'])
    result.update(allow_new_projects=value['allow_new_projects'], autonomy=value['autonomy'],
                  max_depth=value['max_depth'], quotas=deepcopy(quotas))
    if result['max_depth'] == 0:
        require('delegate_project' not in result['operations'])
    if result['allow_new_projects']:
        require('create_project_lead' in result['operations'] and quotas['projects']['created'] > 0)
    return result


def check_child_policy(parent, child, *, created_project_id=None):
    """Bound an edge; created_project_id must come from the same transaction.

    A new project is an exact persisted binding, not a wildcard expansion of
    the parent's static project list. Authentication/transactionality are the
    storage layer's responsibility; this argument must never come from an RPC.
    """
    parent, child = normalize_policy(parent), normalize_policy(child)
    require(parent['max_depth'] > 0, 'hierarchy_depth_exhausted')
    require(child['max_depth'] < parent['max_depth'], 'hierarchy_child_exceeds_parent')
    for key in ('operations', 'actions', 'resources'):
        require(set(child[key]) <= set(parent[key]), 'hierarchy_child_exceeds_parent')
    require(AUTONOMY[child['autonomy']] <= AUTONOMY[parent['autonomy']], 'hierarchy_child_exceeds_parent')
    require(not child['allow_new_projects'], 'hierarchy_child_exceeds_parent')
    allowed_projects = set(parent['project_ids'])
    if created_project_id is not None:
        exact_string(created_project_id)
        require(parent['allow_new_projects'] and 'create_project_lead' in parent['operations'],
                'hierarchy_child_exceeds_parent')
        require(child['project_ids'] == [created_project_id], 'hierarchy_child_exceeds_parent')
        allowed_projects.add(created_project_id)
    require(set(child['project_ids']) <= allowed_projects, 'hierarchy_child_exceeds_parent')
    for kind in KINDS:
        require(all(child['quotas'][kind][key] <= parent['quotas'][kind][key]
                    for key in ('live', 'created')), 'hierarchy_child_exceeds_parent')
    return child


def require_capacity(policy, counters, increments):
    """Check one ancestor's ledger; never debit or refund outside its transaction.

    Every ancestor and the owner ledger must pass, and all debits must commit
    with created objects and the idempotent receipt. Archived objects may free
    live occupancy; cumulative creation counters must never be decremented.
    """
    policy = normalize_policy(policy)
    require(type(counters) is dict and set(counters) == set(KINDS))
    require(type(increments) is dict and set(increments) <= set(KINDS))
    for kind in KINDS:
        counter = counters[kind]
        require(type(counter) is dict and set(counter) == {'live', 'created'})
        require(all(type(n) is int and 0 <= n <= 2**63 - 1 for n in counter.values()))
        require(counter['live'] <= counter['created'])
        increment = increments.get(kind, 0)
        require(type(increment) is int and 0 <= increment <= 1000000)
        if increment:
            require(all(counter[key] + increment <= policy['quotas'][kind][key]
                        for key in ('live', 'created')), 'hierarchy_quota_exhausted')
    return True
