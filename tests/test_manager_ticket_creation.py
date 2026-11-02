"""Explicit bounded manager assignment uses live grants before creation receipts."""
import pytest

from board import BoardError
from profile_management import MANAGEMENT_ACTIONS, TICKET_CREATION_ACTION, management_resource
from test_companion_workflow import running_profile
from test_profile_management import lead, policy
from test_queue_selection import snapshot


def profile(s):
    return s['b'].get_agent_profile(s['human'], s['profile']['id'])


def refresh(s):
    s['b'].register_agent(s['raw'], 'lead-session', 'Lead', [], 'codex', False, 'lead')
    s['actor'] = s['b'].actor_session(s['raw'], 'lead-session')


def grant(s, enabled=True, *, repin=True):
    current = profile(s)
    s['profile'] = s['b'].set_agent_profile_ticket_creation_management(
        s['human'], current['id'], current['version'], enabled)
    if repin:
        refresh(s)
    return s['profile']


def worker(s, **changes):
    return s['b'].create_agent_profile(s['human'], 'Ordinary worker',
        project_ids=[s['project']['id']], policy=policy(autonomy='requested'), **changes)


def creation(s, target, **changes):
    return dict(project_id=s['project']['id'], title='Bounded assigned task',
        description='Only the enrolled ordinary worker should receive this work.',
        acceptance_criteria='Provide the explicit result.', assignee_owner_id='alice',
        assignee_agent_id=target['id'], resource='repo:fixture', idempotency_key='create-assigned') | changes


def refused(s, run, statuses=(400, 403, 404, 409)):
    before = snapshot(s['b'])
    with pytest.raises(BoardError) as error:
        run()
    assert error.value.status in statuses
    assert snapshot(s['b']) == before


def forbid_receipts(monkeypatch, board):
    def forbidden(*args, **kwargs):
        raise AssertionError('Creation receipt reached before current assignment authority')
    monkeypatch.setattr(board, '_retry', forbidden)
    monkeypatch.setattr(board, '_remember', forbidden)


def owner_policy(s, *, remove_action=None, remove_resource=None, **changes):
    stored = s['b'].get_settings(s['human'])['policy']
    values = {key: stored[key] for key in ('enabled', 'allow_read', 'allow_reply', 'allow_requests', 'actions', 'resources')}
    values['actions'] = [value for value in values['actions'] if value != remove_action]
    values['resources'] = [value for value in values['resources'] if value != remove_resource]
    s['b'].set_policy(s['human'], **(values | changes))


def test_profile_management_alone_never_implies_assigned_ticket_creation(lead, monkeypatch):
    s = lead; target = worker(s)
    assert TICKET_CREATION_ACTION not in MANAGEMENT_ACTIONS
    assert TICKET_CREATION_ACTION not in profile(s)['policy']['actions']
    assert TICKET_CREATION_ACTION not in s['b'].get_settings(s['human'])['policy']['actions']
    forbid_receipts(monkeypatch, s['b'])
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target)), (403,))


def test_separate_human_grant_adds_only_creation_and_requires_a_fresh_profile_pin(lead):
    s = lead; target = worker(s)
    before = profile(s); owner_before = s['b'].get_settings(s['human'])['policy']
    current = grant(s, repin=False)
    assert current['version'] == before['version'] + 1
    assert set(current['policy']['actions']) == set(before['policy']['actions']) | {TICKET_CREATION_ACTION}
    assert current['policy']['resources'] == before['policy']['resources']
    assert current['bindings'] == before['bindings']
    owner_after = s['b'].get_settings(s['human'])['policy']
    assert set(owner_after['actions']) == set(owner_before['actions']) | {TICKET_CREATION_ACTION}
    assert owner_after['resources'] == owner_before['resources']
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target)), (409,))
    refresh(s)
    created = s['b'].create_ticket(s['actor'], **creation(s, target))
    assert created['status'] == 'todo' and created['claimant'] is None
    assert created['assignee_agent_id'] == target['id']
    assert s['b'].get_runtime(s['human'])['executions'] == []


@pytest.mark.parametrize('owner', [None, 'alice'])
@pytest.mark.parametrize('self_slot', [False, True])
def test_explicit_creation_grant_assigns_worker_or_exact_self_without_rewriting_payload(lead, owner, self_slot):
    s = lead; target = worker(s); grant(s)
    if self_slot:
        target = profile(s)
    values = creation(s, target, assignee_owner_id=owner)
    created = s['b'].create_ticket(s['actor'], **values)
    assert created['assignee_owner_id'] == owner
    assert created['assignee_agent_id'] == target['id'] and created['resource'] == values['resource']
    assert created['status'] == 'todo' and created['claimant'] is None and created['depends_on'] == []
    before = snapshot(s['b'])
    assert s['b'].create_ticket(s['actor'], **values) == created
    assert snapshot(s['b']) == before
    assert s['b'].get_runtime(s['human'])['executions'] == []


def test_revoking_creation_fences_old_pin_and_existing_receipt_after_refresh(lead, monkeypatch):
    s = lead; target = worker(s); grant(s)
    values = creation(s, target); s['b'].create_ticket(s['actor'], **values)
    grant(s, False, repin=False)
    assert TICKET_CREATION_ACTION not in profile(s)['policy']['actions']
    refused(s, lambda: s['b'].create_ticket(s['actor'], **values), (409,))
    refresh(s); forbid_receipts(monkeypatch, s['b'])
    refused(s, lambda: s['b'].create_ticket(s['actor'], **values), (403,))


@pytest.mark.parametrize('removed', ['creation', 'profile_read', 'management_resource', 'ordinary_resource', 'read_switch', 'request_switch'])
def test_existing_creation_receipt_never_substitutes_for_the_current_owner_ceiling(lead, monkeypatch, removed):
    s = lead; target = worker(s); grant(s)
    values = creation(s, target); s['b'].create_ticket(s['actor'], **values)
    if removed == 'creation':
        owner_policy(s, remove_action=TICKET_CREATION_ACTION)
    elif removed == 'profile_read':
        owner_policy(s, remove_action='agent.profile.read')
    elif removed == 'management_resource':
        owner_policy(s, remove_resource=management_resource('alice'))
    elif removed == 'ordinary_resource':
        owner_policy(s, remove_resource='repo:fixture')
    elif removed == 'read_switch':
        owner_policy(s, allow_read=False)
    else:
        owner_policy(s, allow_requests=False)
    forbid_receipts(monkeypatch, s['b'])
    refused(s, lambda: s['b'].create_ticket(s['actor'], **values), (403,))


@pytest.mark.parametrize('resource', ['', '*', 'repo:*', 'runtime:all', 'repo:outside'])
def test_empty_wildcard_reserved_or_ungranted_resource_never_authorizes_assignment(lead, resource):
    s = lead; target = worker(s); grant(s)
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target, resource=resource)))


def test_existing_resource_normalization_keeps_exact_scope_and_canonical_retry_without_duplicate_effects(lead):
    s = lead; target = worker(s); grant(s)
    # Board has always stripped surrounding whitespace before checking/storing resources.
    values = creation(s, target, resource=' repo:fixture ')
    created = s['b'].create_ticket(s['actor'], **values)
    assert created['resource'] == 'repo:fixture'
    assert created['assignee_agent_id'] == target['id'] and created['claimant'] is None
    before = snapshot(s['b'])
    assert s['b'].create_ticket(s['actor'], **(values | {'resource': 'repo:fixture'})) == created
    assert snapshot(s['b']) == before
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(
        s, target, resource=' repo:outside ', idempotency_key='outside-normalized-resource')), (403,))
    assert s['b'].get_runtime(s['human'])['executions'] == []


@pytest.mark.parametrize('field', ['actions', 'resources'])
def test_stored_wildcards_cannot_substitute_for_exact_creation_or_resource_grants(lead, monkeypatch, field):
    s = lead; target = worker(s); grant(s)
    current = profile(s); changed = dict(current['policy'])
    if field == 'actions':
        changed[field] = [action for action in changed[field] if action != TICKET_CREATION_ACTION] + ['*']
    else:
        changed[field] = [management_resource('alice'), '*']
    s['b'].update_agent_profile(s['human'], current['id'], current['version'], policy=changed)
    refresh(s); forbid_receipts(monkeypatch, s['b'])
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target)), (403,))


@pytest.mark.parametrize('removed', ['profile_read', 'management_resource'])
def test_current_stored_read_delegation_is_required_beside_creation_permission(lead, monkeypatch, removed):
    s = lead; target = worker(s); grant(s)
    current = profile(s); changed = dict(current['policy'])
    field, value = ('actions', 'agent.profile.read') if removed == 'profile_read' else ('resources', management_resource('alice'))
    changed[field] = [item for item in changed[field] if item != value]
    s['b'].update_agent_profile(s['human'], current['id'], current['version'], policy=changed)
    refresh(s); forbid_receipts(monkeypatch, s['b'])
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target)), (403,))


@pytest.mark.parametrize('bound', ['foreign_owner', 'foreign_target', 'other_manager', 'companion', 'legacy', 'target_project', 'target_resource', 'target_paused', 'manager_project'])
def test_assignment_stays_with_owned_scoped_ordinary_workers_and_exact_shared_resource(lead, bound):
    s = lead; target = worker(s); grant(s); changes = {}
    if bound == 'foreign_owner':
        changes['assignee_owner_id'] = 'bob'
    elif bound == 'foreign_target':
        target = s['b'].create_agent_profile(s['other'], 'Other owner worker',
            project_ids=[s['project']['id']], policy=policy(autonomy='requested'))
    elif bound == 'other_manager':
        target = s['b'].set_agent_profile_management(s['human'], target['id'], target['version'], True)
    elif bound == 'companion':
        target = s['b'].create_agent_profile(s['human'], 'Companion target', kind='companion',
            project_ids=[s['project']['id']], policy=policy())
    elif bound == 'legacy':
        device = s['b'].create_device(s['human'], 'Legacy worker', [])
        registration = s['b'].register_agent(s['b'].authenticate(device['token']), 'legacy-worker',
            'Legacy worker', [], 'codex', False, 'legacy-worker')
        target = s['b'].get_agent_profile(s['human'], registration['profile']['id'])
        assert target['legacy']
    elif bound == 'target_project':
        target = s['b'].update_agent_profile(s['human'], target['id'], target['version'], project_ids=[s['hidden']['id']])
    elif bound == 'target_resource':
        target = s['b'].update_agent_profile(s['human'], target['id'], target['version'],
            policy=policy(autonomy='requested', resources=[]))
    elif bound == 'target_paused':
        target = s['b'].update_agent_profile(s['human'], target['id'], target['version'], status='paused')
    else:
        changes['project_id'] = s['hidden']['id']
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target, **changes)))


@pytest.mark.parametrize('caller', ['manager', 'companion'])
def test_only_a_human_can_enable_or_revoke_the_separate_creation_grant(lead, caller):
    s = lead; actor = s['actor']
    if caller == 'companion':
        actor = running_profile(s['b'], s['human'], 'Companion caller', [], kind='companion')['actor']
    current = profile(s)
    for enabled in (True, False):
        refused(s, lambda: s['b'].set_agent_profile_ticket_creation_management(
            actor, current['id'], current['version'], enabled), (403,))


@pytest.mark.parametrize('target_kind', ['unmanaged', 'companion', 'legacy', 'archived'])
def test_human_creation_grant_does_not_implicitly_create_management_or_enlarge_special_profiles(lead, target_kind):
    s = lead; target = worker(s)
    if target_kind == 'companion':
        target = s['b'].create_agent_profile(s['human'], 'Companion', kind='companion', policy=policy())
    elif target_kind == 'legacy':
        device = s['b'].create_device(s['human'], 'Old executor', [])
        registered = s['b'].register_agent(s['b'].authenticate(device['token']), 'old-executor',
            'Old executor', [], 'codex', False, 'old-executor')
        target = s['b'].get_agent_profile(s['human'], registered['profile']['id'])
    elif target_kind == 'archived':
        target = s['b'].update_agent_profile(s['human'], target['id'], target['version'], status='archived')
    refused(s, lambda: s['b'].set_agent_profile_ticket_creation_management(
        s['human'], target['id'], target['version'], True), (403,))


def test_revoking_profile_management_removes_creation_authority_without_granting_other_controls(lead, monkeypatch):
    s = lead; target = worker(s); grant(s)
    current = profile(s)
    revoked = s['b'].set_agent_profile_management(s['human'], current['id'], current['version'], False)
    assert TICKET_CREATION_ACTION not in revoked['policy']['actions']
    assert not any(action.startswith('agent.') for action in revoked['policy']['actions'])
    assert management_resource('alice') not in revoked['policy']['resources']
    refresh(s); forbid_receipts(monkeypatch, s['b'])
    refused(s, lambda: s['b'].create_ticket(s['actor'], **creation(s, target)), (403,))


def test_human_can_remove_creation_rights_from_an_archived_owned_ordinary_profile(lead):
    s = lead; current = grant(s)
    archived = s['b'].update_agent_profile(s['human'], current['id'], current['version'], status='archived')
    assert TICKET_CREATION_ACTION in archived['policy']['actions']
    revoked = s['b'].set_agent_profile_ticket_creation_management(
        s['human'], archived['id'], archived['version'], False)
    assert revoked['status'] == 'archived' and revoked['version'] == archived['version'] + 1
    assert set(revoked['policy']['actions']) == set(archived['policy']['actions']) - {TICKET_CREATION_ACTION}
    assert revoked['policy']['resources'] == archived['policy']['resources']
    assert revoked['bindings'] == archived['bindings']


def test_human_can_remove_creation_grant_after_base_management_is_already_revoked(lead):
    s = lead; current = grant(s)
    ordinary = s['b'].set_agent_profile_management(s['human'], current['id'], current['version'], False)
    revoked = s['b'].set_agent_profile_ticket_creation_management(
        s['human'], ordinary['id'], ordinary['version'], False)
    assert revoked['policy'] == ordinary['policy']
    assert revoked['version'] == ordinary['version'] + 1
    assert TICKET_CREATION_ACTION not in revoked['policy']['actions']
    assert management_resource('alice') not in revoked['policy']['resources']
