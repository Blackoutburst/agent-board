"""Closed hierarchy contracts reject authority expansion and quota recycling."""
from copy import deepcopy

import pytest

from board import BoardError
from hierarchy_policy import normalize_policy, check_child_policy, require_capacity


def policy(**changes):
    return dict(operations=['create_project_lead', 'create_team_profile', 'manage_team_profile', 'delegate_project'],
        project_ids=['project'], allow_new_projects=True, actions=['repo.read_file', 'repo.patch_file', 'repo.run_test'],
        resources=['repo:work'], autonomy='queue', max_depth=2,
        quotas={kind: dict(live=4, created=8) for kind in ('projects', 'profiles', 'delegations')}) | changes


def child(**changes):
    return policy(operations=['create_team_profile', 'manage_team_profile'], allow_new_projects=False,
                  max_depth=1) | changes


def test_normalization_is_stable_and_does_not_mutate_the_owner_ceiling():
    value = policy(); before = deepcopy(value)
    normalized = normalize_policy(value)
    assert normalized == normalize_policy(normalized)
    normalized['quotas']['profiles']['created'] = 1
    assert value == before


@pytest.mark.parametrize('changes', [
    {'max_depth': True}, {'max_depth': 5}, {'allow_new_projects': 1},
    {'actions': ['*']}, {'resources': ['repo:*']}, {'actions': ['agent.profile.create']},
    {'resources': ['agent-profiles:owner']}, {'project_ids': ['project', 'project']},
    {'project_ids': [' project']}, {'operations': ['launch_model']},
    {'autonomy': 'unbounded'}, {'operations': ['delegate_project'], 'max_depth': 0},
    {'allow_create_projects': True},
])
def test_malformed_or_reserved_authority_is_refused(changes):
    with pytest.raises(BoardError): normalize_policy(policy(**changes))


@pytest.mark.parametrize('changes', [
    {'project_ids': ['other']}, {'actions': ['repo.delete']}, {'resources': ['repo:other']},
    {'max_depth': 2}, {'allow_new_projects': True},
    {'quotas': {kind: dict(live=5, created=8) for kind in ('projects', 'profiles', 'delegations')}},
])
def test_child_cannot_expand_any_parent_dimension(changes):
    with pytest.raises(BoardError): check_child_policy(policy(), child(**changes))


def test_exact_new_project_binding_is_distinct_from_ordinary_scope_inheritance():
    new = child(project_ids=['new-project'])
    with pytest.raises(BoardError): check_child_policy(policy(), new)
    assert check_child_policy(policy(), new, created_project_id='new-project') == normalize_policy(new)
    with pytest.raises(BoardError):
        check_child_policy(policy(), child(project_ids=['project', 'new-project']), created_project_id='new-project')
    with pytest.raises(BoardError):
        check_child_policy(policy(allow_new_projects=False), new, created_project_id='new-project')


def test_lower_parent_autonomy_and_depth_are_not_restored_by_a_child_snapshot():
    with pytest.raises(BoardError): check_child_policy(policy(autonomy='requested'), child())
    leaf = child(max_depth=0)
    with pytest.raises(BoardError): check_child_policy(leaf, child(max_depth=0))


def counters(live=0, created=0):
    return {kind: dict(live=live, created=created) for kind in ('projects', 'profiles', 'delegations')}


def test_archiving_cannot_recycle_cumulative_creation_capacity():
    previous = counters(live=0, created=8)
    with pytest.raises(BoardError, match='hierarchy_quota_exhausted'):
        require_capacity(policy(), previous, {'profiles': 1})
    assert previous == counters(live=0, created=8)
    assert require_capacity(policy(), counters(live=3, created=7), {'profiles': 1})
    with pytest.raises(BoardError): require_capacity(policy(), counters(live=4, created=7), {'profiles': 1})


@pytest.mark.parametrize('increment', [-1, True, 1.5])
def test_a_creation_delta_cannot_refund_or_coerce_counters(increment):
    with pytest.raises(BoardError): require_capacity(policy(), counters(), {'profiles': increment})


def test_a_reduced_ceiling_does_not_force_deletion_of_existing_objects():
    low = policy(quotas={kind: dict(live=1, created=2) for kind in ('projects', 'profiles', 'delegations')})
    existing = counters(live=4, created=8)
    assert require_capacity(low, existing, {})
    with pytest.raises(BoardError): require_capacity(low, existing, {'delegations': 1})


@pytest.mark.parametrize('projects', [[], ['project', 'other']])
def test_existing_project_child_requires_exactly_one_project(projects):
    with pytest.raises(BoardError):
        check_child_policy(policy(project_ids=['project', 'other']), child(project_ids=projects))


@pytest.mark.parametrize('field', ['project_ids', 'actions', 'resources'])
def test_unencodable_unicode_is_refused_before_persistence(field):
    with pytest.raises(BoardError, match='hierarchy_policy_invalid'):
        normalize_policy(policy(**{field: ['value' + chr(0xD800)]}))
