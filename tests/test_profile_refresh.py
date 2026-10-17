"""Live same-session refresh, cleanup, revision fencing and lost-reply races."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier
import time

import pytest

from board import BoardError
from test_agent_profiles import home, running, granted
from test_delegation_families import family, open_family, member


EVIDENCE = 'Stopped the prior operation, inspected local processes and external effects; no pending write remains.'


@pytest.fixture
def active(home):
    b, h, project = home
    profile, device, raw, registration, actor = running(home)
    ticket = b.create_ticket(h, project['id'], 'Retain this task', resource='repo:test',
        assignee_owner_id=h['owner_id'], assignee_agent_id=profile['id'])
    ticket = b.claim_ticket(actor, ticket['id'], ticket['version'])
    ticket = b.update_ticket(actor, ticket['id'], ticket['version'], progress='Prior progress',
        outcome='Prior result', evidence='Prior verification')
    return dict(b=b, h=h, project=project, profile=profile, device=device, raw=raw,
        actor=actor, ticket=ticket, registration=registration)


def revise(s, **changes):
    s['profile'] = s['b'].update_agent_profile(s['h'], s['profile']['id'], s['profile']['version'], **changes)
    return s['profile']


def inspect(s):
    return s['b'].inspect_profile_refresh(s['raw'], 'chat')


def arguments(plan):
    return dict(session_id='chat', from_profile_version=plan['from_profile_version'],
        expected_profile_version=plan['to_profile_version'], configuration_sha256=plan['configuration_sha256'],
        expected_claims=plan['claims'], idempotency_key='refresh-once', recovery_evidence=EVIDENCE)


def refresh(s, plan=None, **changes):
    return s['b'].refresh_agent_session(s['raw'], **(arguments(plan or inspect(s)) | changes))


def snapshot(b):
    with sqlite3.connect(b.db_path) as db:
        return tuple(db.iterdump())


def reject(s, plan, **changes):
    before = snapshot(s['b'])
    with pytest.raises(BoardError):
        refresh(s, plan, **changes)
    assert snapshot(s['b']) == before


def current(s):
    return s['b'].get_ticket(s['h'], s['ticket']['id'])['ticket']


def test_unrelated_scope_addition_refreshes_live_claim_without_wait_or_human_release(active):
    s, b = active, active['b']
    second = b.create_project(s['h'], 'Added project', ['owner'])
    revise(s, project_ids=[s['project']['id'], second['id']])
    plan = inspect(s)
    assert plan['mode'] == 'preserve_claims' and not plan['blocked_reasons']
    before = current(s)
    assert before['lease_expires_at'] > time.time()
    result = refresh(s, plan)
    after = current(s)
    assert result['requires_new_model_operation'] is True
    assert after['claimant']['session_id'] == before['claimant']['session_id']
    assert after['claimant']['profile_version'] == s['profile']['version']
    assert after['version'] == before['version'] + 1
    for key in ('lease_expires_at', 'status', 'progress', 'outcome', 'evidence',
                'assignee_agent_id', 'assignee_owner_id', 'depends_on', 'human_requests', 'resource'):
        assert after[key] == before[key]
    assert result['session']['autonomous'] is False
    with pytest.raises(BoardError):
        b.update_ticket(s['actor'], before['id'], before['version'], progress='Stale pending write')
    assert b.renew_ticket(s['actor'], after['id'], after['version'])['version'] == after['version'] + 1
    unchanged = snapshot(b)
    assert refresh(s, plan)['claims'] == result['claims']
    assert snapshot(b) == unchanged
    with b._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='agent_session_profile_refreshed'").fetchone()[0] == 1


@pytest.mark.parametrize('changes', [dict(name='New display name'), dict(avatar='🧭'), dict(description='New description')])
def test_cosmetic_edits_preserve_claims_after_explicit_evidence(active, changes):
    revise(active, **changes)
    assert inspect(active)['mode'] == 'preserve_claims'
    refresh(active)
    assert current(active)['claimant'] is not None


@pytest.mark.parametrize('changes', [dict(instructions='New instructions'), dict(role='Reviewer'),
    dict(preferred_model='New model'), dict(preferred_provider='New provider'), dict(language='fr'),
    dict(response_style='New style'), dict(policy=granted() | {'actions': ['inspect', 'new-action']})])
def test_semantic_or_authority_edits_release_for_new_operation_without_wait(active, changes):
    s = active
    revise(s, **changes)
    before = current(s)
    plan = inspect(s)
    assert plan['mode'] == 'release_claims' and not plan['blocked_reasons']
    result = refresh(s, plan)
    after = current(s)
    assert result['requires_new_model_operation'] is True
    assert after['claimant'] is None and after['lease_expires_at'] is None and after['status'] == 'todo'
    for key in ('progress', 'outcome', 'evidence', 'assignee_owner_id', 'assignee_agent_id', 'resource'):
        assert after[key] == before[key]
    # The normal, separately authorized claim remains necessary.
    reclaimed = s['b'].claim_ticket(s['actor'], after['id'], after['version'])
    unchanged = snapshot(s['b'])
    refresh(s, plan)
    assert current(s) == reclaimed and snapshot(s['b']) == unchanged


def test_intermediate_revocation_and_restoration_cannot_be_hidden_by_final_diff(active):
    s = active
    original = s['profile']['instructions']
    revise(s, instructions='Temporary but material instruction change')
    revise(s, instructions=original)
    assert inspect(s)['mode'] == 'release_claims'
    revise(s, status='paused')
    with pytest.raises(BoardError):
        inspect(s)
    revise(s, status='active')
    assert inspect(s)['mode'] == 'release_claims'


@pytest.mark.parametrize('state,cleanup', [('reserved', 0), ('running', 0), ('needs_review', 0),
    ('needs_review', 1), ('finished', 0)])
def test_unresolved_runtime_blocks_and_cannot_be_repinned(active, state, cleanup):
    s, b = active, active['b']
    execution = b.reserve_execution(s['actor'], 'old-execution', s['project']['id'], s['ticket']['id'],
        'old-work', 'old-turn', 'codex_chatgpt', 'fixture-model')['execution']
    with b._tx() as db:
        db.execute('UPDATE runtime_executions SET state=?,cleanup_confirmed=? WHERE id=?', (state, cleanup, execution['id']))
    revise(s, avatar='🌱')
    plan = inspect(s)
    assert 'runtime_cleanup_required' in plan['blocked_reasons']
    reject(s, plan)
    with b._connection() as db:
        row = db.execute('SELECT * FROM runtime_executions WHERE id=?', (execution['id'],)).fetchone()
        assert row['profile_version'] == execution['profile_version'] and row['state'] == state


def test_original_cleanup_channel_works_then_new_operation_gets_fresh_pin(active):
    s, b = active, active['b']
    old = b.reserve_execution(s['actor'], 'old-execution', s['project']['id'], s['ticket']['id'],
        'old-work', 'old-turn', 'codex_chatgpt', 'fixture-model')['execution']
    revise(s, preferred_model='replacement-model')
    assert b.get_execution_control(s['actor'], old['id'])['should_stop']
    finished = b.finish_execution(s['actor'], old['id'], old['revision'], 'cancelled', True, 'policy_changed')
    refresh(s)
    ticket = current(s)
    b.claim_ticket(s['actor'], ticket['id'], ticket['version'])
    fresh = b.reserve_execution(s['actor'], 'new-execution', s['project']['id'], ticket['id'],
        'new-work', 'new-turn', 'codex_chatgpt', 'replacement-model')['execution']
    assert fresh['profile_version'] == s['profile']['version'] > finished['profile_version']
    with b._connection() as db:
        retained = db.execute('SELECT * FROM runtime_executions WHERE id=?', (old['id'],)).fetchone()
        assert retained['profile_version'] == old['profile_version'] and retained['state'] == 'finished'


def test_active_family_cannot_refresh_parent_or_child_pins(family):
    f, b = family, family['b']
    opened = open_family(f)
    profile = b.get_agent_profile(f['h'], opened['agent_id'])
    b.update_agent_profile(f['h'], profile['id'], profile['version'], avatar='🌱')
    raw = b.authenticate(f['device']['token'])
    before = snapshot(b)
    for actor in f['actors']:
        plan = b.inspect_profile_refresh(raw, actor['session_id'])
        assert 'delegation_cleanup_required' in plan['blocked_reasons']
        with pytest.raises(BoardError):
            b.refresh_agent_session(raw, **(arguments(plan) | {'session_id': actor['session_id']}))
    assert snapshot(b) == before


def test_stopped_family_still_requires_child_runtime_cleanup(family):
    f, b = family, family['b']
    opened = open_family(f)
    child = b.get_ticket(f['actors'][1], member(f, opened)['ticket_id'])['ticket']
    b.claim_ticket(f['actors'][1], child['id'], child['version'])
    b.reserve_execution(f['actors'][1], 'child-op', f['project']['id'], child['id'],
        'work', 'turn', 'codex_chatgpt', 'fixture-model')
    with b._tx() as db:
        db.execute("UPDATE delegation_families SET state='cancelled' WHERE id=?", (opened['id'],))
    profile = b.get_agent_profile(f['h'], opened['agent_id'])
    b.update_agent_profile(f['h'], profile['id'], profile['version'], avatar='🌱')
    raw = b.authenticate(f['device']['token'])
    plan = b.inspect_profile_refresh(raw, 'coordinator')
    assert 'delegation_cleanup_required' in plan['blocked_reasons']
    before = snapshot(b)
    with pytest.raises(BoardError):
        b.refresh_agent_session(raw, **(arguments(plan) | {'session_id': 'coordinator'}))
    assert snapshot(b) == before


@pytest.mark.parametrize('change', ['scope', 'membership', 'archived_project', 'requests', 'read',
    'owner_disabled', 'owner_requests', 'device', 'paused', 'archived'])
def test_revocation_is_immediate_and_refresh_cannot_bypass_it(active, change):
    s, b = active, active['b']
    revise(s, avatar='🌱')
    plan = inspect(s)
    if change == 'scope':
        revise(s, project_ids=[])
        narrowed = inspect(s)
        assert narrowed['claims'] == [] and s['ticket']['id'] not in json.dumps(narrowed)
    elif change == 'membership':
        with b._tx() as db:
            db.execute('UPDATE tickets SET assignee_owner_id=NULL WHERE id=?', (s['ticket']['id'],))
            db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?', (s['project']['id'], 'owner'))
        assert inspect(s)['claims'] == []
    elif change == 'archived_project':
        b.update_project(s['h'], s['project']['id'], s['project']['version'], archived=True)
    elif change in ('requests', 'read'):
        revise(s, policy=granted() | {'allow_' + change: False})
    elif change in ('owner_disabled', 'owner_requests'):
        b.set_policy(s['h'], change != 'owner_disabled', True, True, change != 'owner_requests', ['inspect'], ['repo:test'])
    elif change == 'device':
        b.revoke_device(s['h'], s['device']['id'])
    else:
        revise(s, status=change)
    reject(s, plan)
    if change != 'archived_project':
        with pytest.raises(BoardError):
            b.renew_ticket(s['actor'], s['ticket']['id'], s['ticket']['version'])


@pytest.mark.parametrize('field,value', [('from_profile_version', True), ('expected_profile_version', 0),
    ('configuration_sha256', 'a' * 63), ('configuration_sha256', None), ('recovery_evidence', ''),
    ('recovery_evidence', '  '), ('recovery_evidence', '\ud800'), ('recovery_evidence', 'x' * 20001),
    ('idempotency_key', ''), ('idempotency_key', 'x\nsecret'), ('expected_claims', []),
    ('expected_claims', [{}]), ('expected_claims', None)])
def test_invalid_or_unacknowledged_payload_has_no_effect(active, field, value):
    revise(active, avatar='🌱')
    reject(active, inspect(active), **{field: value})


def test_ticket_revision_race_and_changed_profile_require_reinspection(active):
    s, b = active, active['b']
    revise(s, avatar='🌱')
    plan = inspect(s)
    ticket = current(s)
    b.update_ticket(s['h'], ticket['id'], ticket['version'], progress='Human updated task')
    reject(s, plan)
    plan = inspect(s)
    revise(s, avatar='🔎')
    reject(s, plan)


@pytest.mark.parametrize('expiry', [None, 0, float('inf'), 'invalid'])
def test_expired_or_invalid_claim_cannot_be_revived(active, expiry):
    s = active
    revise(s, avatar='🌱')
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (expiry, s['ticket']['id']))
    plan = inspect(s)
    assert 'claim_expired_use_existing_recovery' in plan['blocked_reasons']
    reject(s, plan)


@pytest.mark.parametrize('same_key', [True, False])
def test_parallel_refresh_has_one_effect_and_no_second_claim_release(active, same_key):
    s = active
    revise(s, instructions='Changed instructions')
    plan, barrier = inspect(s), Barrier(2)
    def invoke(index):
        barrier.wait()
        try:
            return refresh(s, plan, idempotency_key='same' if same_key else 'key-' + str(index))
        except BoardError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert sum(r is not None for r in results) == (2 if same_key else 1)
    assert current(s)['version'] == s['ticket']['version'] + 1


def test_another_device_and_unregistered_session_cannot_refresh(active):
    s, b = active, active['b']
    revise(s, avatar='🌱')
    plan = inspect(s)
    other = b.create_device(s['h'], 'Second device', [])
    b.bind_agent_profile(s['h'], s['profile']['id'], s['profile']['version'], other['id'], 'second', 'codex')
    raw = b.authenticate(other['token'])
    before = snapshot(b)
    with pytest.raises(BoardError):
        b.refresh_agent_session(raw, **arguments(plan))
    with pytest.raises(BoardError):
        b.inspect_profile_refresh(s['raw'], 'unregistered')
    assert snapshot(b) == before


def test_replayed_refresh_still_checks_project_membership(active):
    s = active
    revise(s, instructions='Changed instructions')
    plan = inspect(s)
    refresh(s, plan)
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET assignee_owner_id=NULL WHERE id=?', (s['ticket']['id'],))
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?', (s['project']['id'], 'owner'))
    reject(s, plan)


@pytest.mark.parametrize('semantic', [False, True])
def test_legacy_claim_and_resource_lock_follow_explicit_handoff(active, semantic):
    s, b = active, active['b']
    ticket = current(s)
    b.update_ticket(s['h'], ticket['id'], ticket['version'], release_claim=True)
    task = b.create_task(s['h'], ticket['thread_id'], 'Legacy bounded work', 'Inspect fixture', '',
        'group', 'owner', action='inspect', resource='repo:test')
    task = b.claim_task(s['actor'], task['id'])['task']
    revise(s, **({'instructions': 'New instructions'} if semantic else {'avatar': '🌱'}))
    result = refresh(s)
    after = b.get_thread(s['h'], ticket['thread_id'])['tasks'][0]
    assert after['claim_version'] == task['claim_version'] + 1
    for key in ('progress', 'outcome', 'evidence', 'prerequisites', 'resource', 'destination_id'):
        assert after[key] == task[key]
    with b._connection() as db:
        locked = db.execute('SELECT 1 FROM resource_locks WHERE task_id=?', (task['id'],)).fetchone()
    if semantic:
        assert not locked and after['claimant'] is None and after['status'] == 'recovery'
        assert b.claim_task(s['actor'], task['id'], recovery_evidence=EVIDENCE)['claimed']
    else:
        assert locked and after['claimant'] == task['claimant']
        assert after['lease_expires_at'] == task['lease_expires_at']
        b.update_task(s['actor'], task['id'], after['claim_version'], progress='Fresh version')
    assert result['requires_new_model_operation']


def test_refresh_preserves_blocked_dependencies_and_human_requests(active):
    s, b = active, active['b']
    dependency = b.create_ticket(s['h'], s['project']['id'], 'Dependency')
    ticket = current(s)
    b.update_ticket(s['h'], ticket['id'], ticket['version'], status='blocked', depends_on=[dependency['id']])
    # Simulate a historical retained claim on an outstanding request. A normal
    # new request releases its claim, so this must remain unavailable afterward.
    with b._tx() as db:
        db.execute('''INSERT INTO human_requests(id,ticket_id,owner_id,question,kind,requested_by,status,created_at)
            VALUES('pending-fixture',?,'owner','Pending question','question',?,'open',?)''',
            (ticket['id'], json.dumps(s['actor']), time.time()))
    before = current(s)
    revise(s, avatar='🌱')
    refresh(s)
    after = current(s)
    assert after['status'] == 'blocked' and after['waiting_on_human'] and not after['ready']
    assert after['depends_on'] == before['depends_on'] and after['human_requests'] == before['human_requests']


def test_optional_project_creation_authority_requires_handoff(active):
    revise(active, policy=granted() | {'allow_create_projects': True})
    assert inspect(active)['mode'] == 'release_claims'
    result = refresh(active)
    assert result['policy']['allow_create_projects'] is True
    assert current(active)['claimant'] is None
