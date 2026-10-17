"""Project creation is an explicit grant, never an implicit scope expansion."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from board import BoardError
from test_agent_profiles import home, granted, running


def creator(home, *, allow=True):
    board, human, project = home
    policy = {**granted(), 'allow_create_projects': allow}
    profile = board.create_agent_profile(human, 'Project creator', policy=policy, project_ids=[project['id']])
    device = board.create_device(human, 'Creator device', [])
    profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'creator', 'codex')
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'creator', 'Creator', [], 'codex', False, 'creator')
    actor = board.actor_session(raw, 'creator')
    return profile, device, raw, registration, actor


def create(board, actor, key='create', **changes):
    values = dict(name='Created project', participants=[actor['owner_id']],
                  description='Original description', context='Original context', idempotency_key=key)
    return board.create_project(actor, **{**values, **changes})


def snapshot(board):
    with sqlite3.connect(board.db_path) as db:
        return tuple(db.iterdump())


@pytest.mark.parametrize('allow', ['missing', False])
def test_scoped_profile_needs_explicit_creation_permission_without_side_effects(home, allow):
    board, _, _ = home
    *_, actor = running(home) if allow == 'missing' else creator(home, allow=False)
    before = snapshot(board)
    with pytest.raises(BoardError):
        create(board, actor)
    assert snapshot(board) == before


def test_granted_creation_returns_receipt_without_scope_or_active_claim_changes(home):
    board, human, followed = home
    profile, _, _, registration, actor = creator(home)
    ticket = board.create_ticket(human, followed['id'], 'Existing work', idempotency_key='existing')
    claim = board.claim_ticket(actor, ticket['id'], ticket['version'])
    profile_before = board.get_agent_profile(human, profile['id'])
    with board._connection() as db:
        session_before = dict(db.execute('SELECT * FROM sessions WHERE id=?', (actor['session_id'],)).fetchone())
        schema_before = db.execute('PRAGMA user_version').fetchone()[0]
    receipt = create(board, actor)
    assert set(receipt) == {'id', 'url', 'profile_scope_access', 'owner_scope_grant_required'}
    assert receipt['url'].endswith('/?project=' + receipt['id'])
    assert not receipt['profile_scope_access'] and receipt['owner_scope_grant_required']
    assert registration['policy']['allow_create_projects'] is True
    assert board.get_work(actor)['policy']['allow_create_projects'] is True
    assert board.get_agent_profile(human, profile['id']) == profile_before
    assert board.get_ticket(actor, ticket['id'])['ticket'] == claim
    with board._connection() as db:
        assert dict(db.execute('SELECT * FROM sessions WHERE id=?', (actor['session_id'],)).fetchone()) == session_before
        assert db.execute('PRAGMA user_version').fetchone()[0] == schema_before
    assert [item['id'] for item in board.list_projects(actor)] == [followed['id']]
    for operation in (
        lambda: board.get_project(actor, receipt['id']),
        lambda: board.update_project(actor, receipt['id'], 1, description='Implicit write'),
        lambda: board.create_ticket(actor, receipt['id'], 'Implicit task', idempotency_key='implicit'),
    ):
        with pytest.raises(BoardError):
            operation()
    stored = board.get_project(human, receipt['id'])
    assert stored['name'] == 'Created project' and stored['participants'] == [human['owner_id']]


def test_creation_retry_does_not_read_later_private_edits_or_create_duplicates(home):
    board, human, _ = home
    *_, actor = creator(home)
    receipt = create(board, actor)
    project = board.get_project(human, receipt['id'])
    board.update_project(human, receipt['id'], project['version'], name='Private replacement',
                         context='Private follow-up context', environment='Private environment')
    board.create_ticket(human, receipt['id'], 'Private follow-up task', idempotency_key='private-task')
    before = snapshot(board)
    assert create(board, actor) == receipt
    assert snapshot(board) == before
    assert 'Private' not in json.dumps(receipt)
    with pytest.raises(BoardError):
        create(board, actor, context='Changed payload')
    assert snapshot(board) == before


def test_creation_grant_works_with_empty_scope_without_exposing_existing_projects(home):
    board, human, existing = home
    profile, _, raw, _, actor = creator(home)
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[])
    board.register_agent(raw, actor['session_id'], 'Creator', [], 'codex', False, 'creator')
    receipt = create(board, actor)
    assert not receipt['profile_scope_access'] and board.list_projects(actor) == []
    for project_id in (existing['id'], receipt['id']):
        with pytest.raises(BoardError):
            board.get_project(actor, project_id)
        with pytest.raises(BoardError):
            board.list_tickets(actor, project_id)


def test_owner_follow_is_separate_versioned_action_and_receipt_remains_bounded(home):
    board, human, followed = home
    profile, _, raw, _, actor = creator(home)
    receipt = create(board, actor)
    changed = board.update_agent_profile(human, profile['id'], profile['version'],
                                         project_ids=[followed['id'], receipt['id']])
    with pytest.raises(BoardError):
        create(board, actor)
    refreshed = board.register_agent(raw, actor['session_id'], 'Creator', [], 'codex', False, 'creator')
    assert refreshed['profile_version'] == changed['version']
    retry = create(board, actor)
    assert set(retry) == set(receipt) and retry['id'] == receipt['id']
    assert retry['profile_scope_access'] and not retry['owner_scope_grant_required']
    assert board.get_project(actor, receipt['id'])['name'] == 'Created project'


def test_grant_revocation_rechecks_profile_version_and_denies_old_retry(home):
    board, human, _ = home
    profile, _, raw, _, actor = creator(home)
    create(board, actor)
    changed = board.update_agent_profile(human, profile['id'], profile['version'],
                                         policy={**profile['policy'], 'allow_create_projects': False})
    with pytest.raises(BoardError) as stale:
        create(board, actor)
    assert stale.value.status == 409
    board.register_agent(raw, actor['session_id'], 'Creator', [], 'codex', False, 'creator')
    before = snapshot(board)
    for key in ('create', 'new-after-revocation'):
        with pytest.raises(BoardError):
            create(board, actor, key)
    assert snapshot(board) == before
    assert board.get_agent_profile(human, profile['id'])['version'] == changed['version']


@pytest.mark.parametrize('value', [None, 'true', 'false', 1, 0, [], {}])
def test_creation_flag_accepts_only_real_booleans(home, value):
    board, human, project = home
    before = snapshot(board)
    with pytest.raises(BoardError):
        board.create_agent_profile(human, 'Invalid grant', project_ids=[project['id']],
                                   policy={**granted(), 'allow_create_projects': value})
    assert snapshot(board) == before


@pytest.mark.parametrize('allow', [False, True])
def test_older_whole_policy_edit_preserves_creation_grant(home, allow):
    board, human, _ = home
    profile, *_ = creator(home, allow=allow)
    changed = board.update_agent_profile(human, profile['id'], profile['version'], policy=granted())
    assert changed['policy']['allow_create_projects'] is allow


def test_profile_duplication_resets_creation_permission(home):
    board, human, _ = home
    profile, *_ = creator(home)
    clone = board.duplicate_agent_profile(human, profile['id'], profile['version'], idempotency_key='clone')
    assert not clone['policy'].get('allow_create_projects', False)
    assert clone['project_ids'] == [] and clone['bindings'] == [] and clone['status'] == 'paused'


def test_agent_and_different_owner_cannot_enable_creation_permission(home):
    board, human, _ = home
    profile, _, _, _, actor = creator(home, allow=False)
    before = snapshot(board)
    for intruder in (actor, {'kind': 'human', 'owner_id': 'other'}):
        with pytest.raises(BoardError):
            board.update_agent_profile(intruder, profile['id'], profile['version'],
                                         policy={**profile['policy'], 'allow_create_projects': True})
    assert snapshot(board) == before


@pytest.mark.parametrize('control', ['owner_disabled', 'owner_work_disabled', 'profile_paused', 'profile_archived', 'responses_only'])
def test_creation_permission_does_not_override_existing_controls(home, control):
    board, human, _ = home
    profile, _, raw, _, actor = creator(home)
    if control.startswith('owner_'):
        board.set_policy(human, control != 'owner_disabled', True, True,
                         control != 'owner_work_disabled', ['inspect'], ['repo:test'])
    else:
        changes = {'status': control.removeprefix('profile_')} if control != 'responses_only' else {
            'policy': {**profile['policy'], 'autonomy': 'responses'}}
        board.update_agent_profile(human, profile['id'], profile['version'], **changes)
        if control == 'responses_only':
            board.register_agent(raw, actor['session_id'], 'Creator', [], 'codex', False, 'creator')
    before = snapshot(board)
    with pytest.raises(BoardError):
        create(board, actor)
    assert snapshot(board) == before


def test_legacy_unscoped_creation_remains_compatible_but_explicit_false_revokes(home):
    board, human, _ = home
    device = board.create_device(human, 'Legacy', [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'legacy', 'Legacy', [], 'codex', False, 'legacy')
    actor = board.actor_session(raw, 'legacy')
    assert registration['profile']['project_ids'] is None
    assert 'allow_create_projects' not in registration['policy']
    project = create(board, actor)
    assert project['name'] == 'Created project' and project['context'] == 'Original context'
    assert board.get_project(actor, project['id']) == project
    profile = board.get_agent_profile(human, registration['identity']['id'])
    board.update_agent_profile(human, profile['id'], profile['version'],
                               policy={**profile['policy'], 'allow_create_projects': False})
    board.register_agent(raw, 'legacy', 'Legacy', [], 'codex', False, 'legacy')
    with pytest.raises(BoardError):
        create(board, actor, 'explicitly-revoked')


@pytest.mark.parametrize('legacy', [False, True])
def test_untouched_profile_effective_policy_retains_exact_previous_shape(home, legacy):
    board, human, _ = home
    if legacy:
        device = board.create_device(human, 'Unchanged legacy', [])
        raw = board.authenticate(device['token'])
        registration = board.register_agent(raw, 'unchanged', 'Unchanged', [], 'codex', False, 'unchanged')
        actor = board.actor_session(raw, 'unchanged')
        autonomy = 'queue'
    else:
        _, _, _, registration, actor = running(home)
        autonomy = 'requested'
    # These fixtures have matching owner/profile read, message and work grants.
    # Before the new optional capability, their complete effective policy was
    # the owner policy plus autonomy. Preserve its exact shape for pinned hosts.
    previous_policy = {**board.get_settings(human)['policy'], 'autonomy': autonomy}
    assert registration['policy'] == previous_policy
    assert board.get_work(actor)['policy'] == previous_policy
    assert 'allow_create_projects' not in registration['profile']['policy']


def test_legacy_profile_restricted_to_finite_scope_does_not_inherit_creation_grant(home):
    board, human, project = home
    device = board.create_device(human, 'Legacy', [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'legacy', 'Legacy', [], 'codex', False, 'legacy')
    profile = board.get_agent_profile(human, registration['identity']['id'])
    assert 'allow_create_projects' not in profile['policy']
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[project['id']])
    board.register_agent(raw, 'legacy', 'Legacy', [], 'codex', False, 'legacy')
    before = snapshot(board)
    with pytest.raises(BoardError):
        create(board, board.actor_session(raw, 'legacy'))
    assert snapshot(board) == before


def test_creation_preserves_participant_validation_and_owner_membership(home):
    board, human, _ = home
    *_, actor = creator(home)
    before = snapshot(board)
    with pytest.raises(BoardError):
        create(board, actor, participants=['unknown-owner'])
    assert snapshot(board) == before
    receipt = create(board, actor, participants=['other', 'other'])
    assert board.get_project(human, receipt['id'])['participants'] == ['other', 'owner']
    # Membership is immutable through the public API, but an administrative
    # removal must also prevent a creation retry from acting as an alternate read.
    with board._tx() as db:
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?', (receipt['id'], human['owner_id']))
    with pytest.raises(BoardError):
        create(board, actor, participants=['other', 'other'])


def test_creation_retry_is_bound_to_exact_session(home):
    board, _, _ = home
    _, _, raw, _, actor = creator(home)
    first = create(board, actor)
    board.register_agent(raw, 'second', 'Second', [], 'codex', False, 'creator')
    second = create(board, board.actor_session(raw, 'second'))
    assert first['id'] != second['id']
    assert create(board, actor)['id'] == first['id']


def test_duplicate_creation_retry_is_atomic_across_callers(home):
    board, _, _ = home
    *_, actor = creator(home)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: create(board, actor), range(2)))
    assert results[0] == results[1]
    with board._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM projects WHERE name='Created project'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM idempotency WHERE operation='create_project' AND key='create'").fetchone()[0] == 1


def test_creation_failure_rolls_back_project_membership_and_retry_record(home, monkeypatch):
    board, _, _ = home
    *_, actor = creator(home)
    original = board._audit
    def failed_audit(db, author, event, detail):
        if event == 'project_created':
            raise RuntimeError('Synthetic audit failure')
        return original(db, author, event, detail)
    before = snapshot(board)
    monkeypatch.setattr(board, '_audit', failed_audit)
    with pytest.raises(RuntimeError, match='Synthetic audit failure'):
        create(board, actor)
    assert snapshot(board) == before
    monkeypatch.setattr(board, '_audit', original)
    assert create(board, actor)['id']
