"""Independent adversarial checks for narrowly scoped stale-profile release.

Runtime cleanup is a recorded client assertion, not an OS attestation. Legacy
unmanaged work may have no Runtime row; explicit evidence remains mandatory.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier
import time

import pytest

from board import Board, BoardError


EVIDENCE = 'Inspected the prior operation; no pending external effect remains.'


def policy(**changes):
    return dict(allow_read=True, allow_messages=True, allow_requests=True,
                actions=[], resources=[], autonomy='requested') | changes


@pytest.fixture
def stale(tmp_path):
    b = Board(tmp_path / 'board')
    people = {}
    for owner in ('alice', 'bob'):
        b.ensure_owner(owner, owner.title())
        people[owner] = {'kind': 'human', 'owner_id': owner}
        b.set_policy(people[owner], True, True, True, True, [], [])
    human = people['alice']
    project = b.create_project(human, 'Shared fixture', ['alice', 'bob'], idempotency_key='project')
    p = b.create_agent_profile(human, 'Fixture profile', project_ids=[project['id']], policy=policy())
    device = b.create_device(human, 'Fixture device', [])
    p = b.bind_agent_profile(human, p['id'], p['version'], device['id'], 'fixture', agent_kind='codex')
    raw = b.authenticate(device['token'])
    b.register_agent(raw, 'prior-session', 'Prior', [], 'codex', False, 'fixture')
    old = b.actor_session(raw, 'prior-session')
    ticket = b.create_ticket(human, project['id'], 'Prior task', assignee_owner_id='alice',
                            assignee_agent_id=p['id'], resource='fixture:exclusive')
    ticket = b.claim_ticket(old, ticket['id'], ticket['version'])
    execution = b.reserve_execution(old, 'prior-execution', project['id'], ticket['id'],
                                    'prior-work', 'prior-turn', 'codex_chatgpt', 'fixture-model')['execution']
    execution = b.finish_execution(old, execution['id'], execution['revision'], 'cancelled', True, 'cancelled')
    p = b.update_agent_profile(human, p['id'], p['version'], instructions='Updated local instructions')
    b.register_agent(raw, 'current-session', 'Current', [], 'codex', False, 'fixture')
    actor = b.actor_session(raw, 'current-session')
    with b._tx() as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (time.time() - 100, ticket['id']))
    return dict(b=b, human=human, bob=people['bob'], project=project, profile=p,
                device=device, raw=raw, old=old, actor=actor, ticket=ticket, execution=execution)


def current(s):
    return s['b'].get_ticket(s['human'], s['ticket']['id'])['ticket']


def reconcile(s, *, actor=None, ticket_id=None, version=None, key='reconcile', evidence=EVIDENCE):
    if version is None:
        with s['b']._connection() as db:
            version = db.execute('SELECT version FROM tickets WHERE id=?', (s['ticket']['id'],)).fetchone()[0]
    return s['b'].reconcile_profile_claim(actor or s['actor'], ticket_id or s['ticket']['id'],
        version, key, evidence)


def snapshot(b):
    with sqlite3.connect(b.db_path) as db:
        return tuple(db.iterdump())


def reject(s, **kwargs):
    before = snapshot(s['b'])
    with pytest.raises(BoardError) as exc:
        reconcile(s, **kwargs)
    assert exc.value.status in (400, 401, 403, 404, 409)
    assert snapshot(s['b']) == before
    return str(exc.value)


def revise(s, **changes):
    b = s['b']
    p = b.get_agent_profile(s['human'], s['profile']['id'])
    p = b.update_agent_profile(s['human'], p['id'], p['version'], **changes)
    s['profile'] = p
    return p


def refresh(s, *, raw=None, session='current-session', identity='fixture'):
    s['b'].register_agent(raw or s['raw'], session, 'Current fixture', [], 'codex', False, identity)
    return s['b'].actor_session(raw or s['raw'], session)


def test_release_is_bounded_and_preserves_runtime_configuration_and_assignment(stale):
    s, b = stale, stale['b']
    before = current(s)
    runtime = b.get_runtime(s['human'])
    profile = b.get_agent_profile(s['human'], s['profile']['id'])
    released = reconcile(s)
    assert released['claimant'] is None
    assert released['lease_expires_at'] is None
    assert released['status'] == 'todo'
    assert released['version'] == before['version'] + 1
    for key in ('assignee_owner_id', 'assignee_agent_id', 'resource', 'outcome', 'evidence', 'progress'):
        assert released[key] == before[key]
    assert b.get_runtime(s['human']) == runtime
    # The private profile's active_work projection legitimately loses this claim.
    after_profile = b.get_agent_profile(s['human'], s['profile']['id'])
    for key in ('id', 'version', 'instructions', 'project_ids', 'policy', 'bindings'):
        assert after_profile[key] == profile[key]


def test_legacy_unmanaged_work_requires_declaration_but_not_runtime_history(stale):
    s = stale
    with s['b']._tx() as db:
        db.execute('DELETE FROM runtime_executions')
    reject(s, evidence='  ')
    assert reconcile(s)['claimant'] is None
    assert s['b'].get_runtime(s['human'])['executions'] == []


@pytest.mark.parametrize('missing', [('agent_id',), ('profile_version',), ('agent_id', 'profile_version')])
def test_legacy_claim_identity_is_proved_by_persisted_session(stale, missing):
    s = stale
    claim = current(s)['claimant']
    for field in missing:
        claim.pop(field)
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET claimant=? WHERE id=?', (json.dumps(claim), s['ticket']['id']))
    assert reconcile(s)['claimant'] is None


@pytest.mark.parametrize('field,value', [
    ('agent_id', 'different-profile'), ('profile_version', 999), ('profile_version', None),
    ('profile_version', True), ('owner_id', 'bob'), ('device_id', 'other-device'),
    ('kind', 'human'), ('session_id', 'missing-session'), ('session_id', 'current-session'),
])
def test_contradictory_or_missing_claim_provenance_is_refused(stale, field, value):
    s = stale
    claim = current(s)['claimant'] | {field: value}
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET claimant=? WHERE id=?', (json.dumps(claim), s['ticket']['id']))
    reject(s)


@pytest.mark.parametrize('lease', [None, float('inf'), float('-inf'), 'invalid', time.time() + 86400])
def test_only_finite_expired_lease_is_eligible(stale, lease):
    with stale['b']._tx() as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (lease, stale['ticket']['id']))
    reject(stale)


@pytest.mark.parametrize('source_version', [None, 0, 999, 'invalid'])
def test_source_session_version_must_be_provably_older(stale, source_version):
    with stale['b']._tx() as db:
        db.execute('UPDATE sessions SET profile_version=? WHERE id=?', (source_version, 'prior-session'))
    reject(stale)


def test_same_version_other_session_is_not_a_profile_change_recovery(stale):
    s = stale
    with s['b']._tx() as db:
        claim = current(s)['claimant'] | {'profile_version': s['profile']['version']}
        db.execute('UPDATE sessions SET profile_version=? WHERE id=?', (s['profile']['version'], 'prior-session'))
        db.execute('UPDATE tickets SET claimant=? WHERE id=?', (json.dumps(claim), s['ticket']['id']))
    reject(s)


def test_legacy_claim_without_agent_metadata_cannot_use_an_unbound_source_session(stale):
    s = stale
    claim = current(s)['claimant']
    claim.pop('agent_id')
    claim.pop('profile_version')
    with s['b']._tx() as db:
        db.execute('UPDATE sessions SET agent_id=NULL WHERE id=?', ('prior-session',))
        db.execute('UPDATE tickets SET claimant=? WHERE id=?', (json.dumps(claim), s['ticket']['id']))
    reject(s)


def test_another_device_bound_to_same_profile_cannot_release(stale):
    s, b = stale, stale['b']
    device = b.create_device(s['human'], 'Other device', [])
    p = b.get_agent_profile(s['human'], s['profile']['id'])
    b.bind_agent_profile(s['human'], p['id'], p['version'], device['id'], 'other', agent_kind='codex')
    raw = b.authenticate(device['token'])
    actor = refresh(s, raw=raw, session='other-device-session', identity='other')
    reject(s, actor=actor)


def test_another_profile_on_same_device_cannot_release(stale):
    s, b = stale, stale['b']
    p = b.create_agent_profile(s['human'], 'Unrelated', project_ids=[s['project']['id']], policy=policy())
    b.bind_agent_profile(s['human'], p['id'], p['version'], s['device']['id'], 'unrelated', agent_kind='codex')
    actor = refresh(s, session='unrelated-profile-session', identity='unrelated')
    reject(s, actor=actor)


def test_foreign_owner_human_raw_actor_and_stale_session_cannot_release(stale):
    s, b = stale, stale['b']
    foreign = b.create_device(s['bob'], 'Bob device', [])
    raw = b.authenticate(foreign['token'])
    b.register_agent(raw, 'bob-session', 'Bob', [], 'codex', False, 'bob')
    for actor in (s['human'], s['bob'], s['raw'], s['old'], b.actor_session(raw, 'bob-session')):
        reject(s, actor=actor)


@pytest.mark.parametrize('state,cleanup', [
    ('reserved', 0), ('running', 0), ('needs_review', 0), ('needs_review', 1), ('finished', 0),
])
def test_every_incomplete_runtime_state_blocks_without_changing_capacity(stale, state, cleanup):
    s = stale
    with s['b']._tx() as db:
        db.execute('UPDATE runtime_executions SET state=?,cleanup_confirmed=?', (state, cleanup))
    before = s['b'].get_runtime(s['human'])
    reject(s)
    assert s['b'].get_runtime(s['human']) == before


def test_runtime_on_other_device_same_profile_ticket_still_blocks(stale):
    s, b = stale, stale['b']
    other = b.create_device(s['human'], 'Other runtime device', [])
    p = b.get_agent_profile(s['human'], s['profile']['id'])
    b.bind_agent_profile(s['human'], p['id'], p['version'], other['id'], 'other', agent_kind='codex')
    raw = b.authenticate(other['token'])
    refresh(s, raw=raw, session='other-runtime-session', identity='other')
    refresh(s)
    with b._tx() as db:
        db.execute("UPDATE runtime_executions SET device_id=?,session_id=?,state='running',cleanup_confirmed=0",
                   (other['id'], 'other-runtime-session'))
    reject(s)


@pytest.mark.parametrize('status', ['blocked', 'review', 'todo'])
def test_preserves_non_done_status_and_existing_results(stale, status):
    s, b = stale, stale['b']
    prior = current(s)
    b.update_ticket(s['human'], prior['id'], prior['version'], status=status,
                    progress='Prior progress', outcome='Prior outcome', evidence='Prior evidence')
    prior = current(s)
    released = reconcile(s)
    assert released['status'] == status
    for key in ('outcome', 'progress', 'evidence', 'human_requests', 'depends_on', 'assignee_agent_id'):
        assert released[key] == prior[key]


def test_preserves_open_human_question_and_dependencies(stale):
    s, b = stale, stale['b']
    dependency = b.create_ticket(s['human'], s['project']['id'], 'Pending dependency')
    old_claim = current(s)['claimant']
    request = b.request_human_input(s['human'], s['ticket']['id'], 'alice', 'Pending human question', 'question')
    t = current(s)
    b.update_ticket(s['human'], t['id'], t['version'], status='blocked', depends_on=[dependency['id']], progress='Keep progress')
    # Represent an older persisted ticket that still retained its prior claim.
    with b._tx() as db:
        db.execute('UPDATE tickets SET claimant=?,lease_expires_at=?,waiting_for_dependencies=1 WHERE id=?',
                   (json.dumps(old_claim), time.time() - 100, t['id']))
    prior = current(s)
    released = reconcile(s)
    assert released['status'] == 'blocked'
    assert released['waiting_on_human'] is True
    assert released['human_requests'] == prior['human_requests']
    assert released['human_requests'][0]['id'] == request['id']
    assert released['depends_on'] == [dependency['id']]
    assert released['progress'] == 'Keep progress'
    with b._connection() as db:
        assert db.execute('SELECT waiting_for_dependencies FROM tickets WHERE id=?', (t['id'],)).fetchone()[0] == 1


def test_completed_ticket_and_reassigned_ticket_are_refused(stale):
    s, b = stale, stale['b']
    with b._tx() as db:
        db.execute("UPDATE tickets SET status='done',outcome='Complete',evidence='Verified' WHERE id=?", (s['ticket']['id'],))
    reject(s)
    with b._tx() as db:
        db.execute("UPDATE tickets SET status='in_progress',assignee_owner_id='bob' WHERE id=?", (s['ticket']['id'],))
    reject(s)


def test_reassignment_to_another_profile_cannot_be_overwritten(stale):
    s = stale
    other = s['b'].create_agent_profile(s['human'], 'Another profile',
        project_ids=[s['project']['id']], policy=policy())
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET assignee_agent_id=? WHERE id=?', (other['id'], s['ticket']['id']))
    reject(s)


@pytest.mark.parametrize('owner', [None, 'alice'])
def test_unassigned_profile_retains_old_profile_responsibility_without_changing_owner(stale, owner):
    s = stale
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET assignee_owner_id=?,assignee_agent_id=NULL WHERE id=?', (owner, s['ticket']['id']))
    result = reconcile(s)
    assert result['assignee_agent_id'] == s['profile']['id']
    assert result['assignee_owner_id'] == owner


def test_retry_returns_current_ticket_without_releasing_new_claim(stale):
    s, b = stale, stale['b']
    old_version = current(s)['version']
    released = reconcile(s)
    reclaimed = b.claim_ticket(s['actor'], released['id'], released['version'])
    before = snapshot(b)
    retried = reconcile(s, version=old_version)
    assert retried == reclaimed
    assert snapshot(b) == before
    assert retried['claimant']['session_id'] == 'current-session'
    reject(s, version=old_version, evidence='Changed declaration')


def test_parallel_reconcile_with_same_key_has_exactly_one_effect(stale):
    s = stale
    version = current(s)['version']
    barrier = Barrier(2)
    def invoke(_):
        barrier.wait()
        return reconcile(s, version=version)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert results[0] == results[1]
    assert current(s)['version'] == version + 1


def test_parallel_different_keys_cannot_apply_the_release_twice(stale):
    s = stale
    version = current(s)['version']
    barrier = Barrier(2)
    def invoke(n):
        barrier.wait()
        try:
            return reconcile(s, version=version, key='concurrent-' + str(n))
        except BoardError:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(invoke, range(2)))
    assert sum(result is not None for result in results) == 1
    assert current(s)['version'] == version + 1


def test_removal_from_project_membership_blocks_even_an_idempotent_retry(stale):
    s = stale
    version = current(s)['version']
    reconcile(s)
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET assignee_owner_id=NULL WHERE id=?', (s['ticket']['id'],))
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?', (s['project']['id'], 'alice'))
    reject(s, version=version)


@pytest.mark.parametrize('change', ['owner_disabled', 'owner_requests', 'profile_paused', 'profile_archived',
                                   'profile_requests', 'profile_responses', 'scope', 'device_revoked', 'profile_stale'])
def test_live_authorization_blocks_first_call_and_previously_successful_retry(stale, change):
    s, b = stale, stale['b']
    version = current(s)['version']
    reconcile(s)
    if change == 'owner_disabled':
        b.set_policy(s['human'], False, True, True, True, [], [])
    elif change == 'owner_requests':
        b.set_policy(s['human'], True, True, True, False, [], [])
    elif change.startswith('profile_'):
        if change in ('profile_paused', 'profile_archived'):
            revise(s, status=change.removeprefix('profile_'))
        elif change == 'profile_requests':
            revise(s, policy=policy(allow_requests=False))
            refresh(s)
        elif change == 'profile_responses':
            revise(s, policy=policy(autonomy='responses'))
            refresh(s)
        else:
            revise(s, description='New profile revision without registration')
    elif change == 'scope':
        revise(s, project_ids=[])
        refresh(s)
    else:
        b.revoke_device(s['human'], s['device']['id'])
    reject(s, version=version)
    reject(s, version=version, key='new-after-revocation')


@pytest.mark.parametrize('field,value', [
    ('evidence', ''), ('evidence', ' \n '), ('evidence', None), ('evidence', True),
    ('evidence', {}), ('evidence', 'x' * 20001), ('key', ''), ('key', ' '),
    ('key', 'x' * 201), ('key', None), ('version', True), ('version', 0), ('version', -1),
])
def test_payload_types_and_bounds_refuse_without_side_effects(stale, field, value):
    reject(stale, **{field: value})


def test_stale_ticket_version_and_missing_claim_cannot_mutate(stale):
    s = stale
    reject(s, version=current(s)['version'] + 1)
    with s['b']._tx() as db:
        db.execute('UPDATE tickets SET claimant=NULL WHERE id=?', (s['ticket']['id'],))
    reject(s)
