"""Idle assignment handoff preserves explicit recovery and execution fences."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading
import time

import pytest

from board import BoardError
from test_coordination import space, new


def worker(board, human, name, skills=('python',)):
    device = board.create_device(human, name, [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, name, name, list(skills), 'codex', True, name)
    return board.actor_session(raw, name), registration['identity']['id'], device


@pytest.fixture
def idle(space):
    board, human, _, project, original, _, _ = space
    source_id = board.list_agents(human, project['id'])[0]['id']
    successor, successor_id, device = worker(board, human, 'Successor')
    return space, source_id, successor, successor_id, device


def age(board, source_id, ticket=None, *, expired=False):
    old = time.time() - 3600
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE sessions SET last_seen_at=? WHERE agent_id=?', (old, source_id))
        if ticket:
            db.execute('UPDATE tickets SET updated_at=? WHERE id=?', (old, ticket['id']))
            if expired:
                db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (old, ticket['id']))
    return old


def visible(board, actor, project_id, profile_id):
    return next(item for item in board.list_agents(actor, project_id) if item['id'] == profile_id)


def eligible(board, actor, project_id):
    work = board.get_work(actor, project_id)
    return {item['id'] for key in ('ready', 'recovery') for item in work[key]}


def test_shared_presence_aggregates_sessions_without_private_details(idle):
    space, source_id, successor, _, _ = idle
    board, human, _, project, original, second, device = space
    current = visible(board, successor, project['id'], source_id)
    assert current['presence'] == 'online' and current['online']
    assert current['idle_after_seconds'] == 900
    assert not {'sessions', 'tickets', 'device_id', 'capabilities', 'instructions'} & current.keys()
    old = age(board, source_id)
    stale = visible(board, successor, project['id'], source_id)
    assert stale['presence'] == 'idle' and not stale['online']
    assert stale['last_seen_at'] == old
    # A second chat of the same profile keeps that worker present.
    board.read_inbox(second)
    assert visible(board, successor, project['id'], source_id)['presence'] == 'online'
    board.revoke_device(human, device['id'])
    offline = visible(board, successor, project['id'], source_id)
    assert offline['presence'] == 'offline' and not offline['online']
    assert offline['last_seen_at'] is None


def test_idle_unclaimed_assignment_is_selected_once_and_old_writer_fenced(idle):
    space, source_id, successor, successor_id, _ = idle
    board, human, _, project, original, _, _ = space
    ticket = new(space, assignee_agent_id=source_id)
    age(board, source_id, ticket)
    before = board.get_ticket(human, ticket['id'])['ticket']
    assert ticket['id'] in eligible(board, successor, project['id'])
    # Discovery must never erase assignment or advance its optimistic version.
    assert board.get_ticket(human, ticket['id'])['ticket'] == before
    result = board.claim_next_ticket(successor, 'idle-handoff', project['id'])
    claimed = result['ticket']
    assert result['claimed'] and claimed['id'] == ticket['id']
    assert claimed['claimant']['agent_id'] == successor_id
    assert claimed['assignee_agent_id'] is None and claimed['version'] > ticket['version']
    retry = board.claim_next_ticket(successor, 'idle-handoff', project['id'])
    assert retry['retried'] and retry['ticket'] == claimed
    with pytest.raises(BoardError):
        board.claim_ticket(original, ticket['id'], claimed['version'])
    events = board.list_activity(human, ticket_id=ticket['id'])
    assert any('idle' in event['event'] or 'reassign' in event['event'] or 'handoff' in event['event'] for event in events)


@pytest.mark.parametrize('guard', ['fresh_session', 'recent_assignment',
                                  'paused', 'archived', 'blocked', 'review', 'in_progress',
                                  'dependency', 'missing_skill', 'live_claim', 'human_request'])
def test_idle_handoff_does_not_override_existing_gates(idle, guard):
    space, source_id, successor, _, _ = idle
    board, human, _, project, original, _, source_device = space
    options = {'assignee_agent_id': source_id}
    if guard == 'missing_skill':
        options['required_skills'] = ['android']
    if guard == 'dependency':
        options['depends_on'] = [new(space, 'Prerequisite')['id']]
    ticket = new(space, **options)
    if guard in {'live_claim', 'human_request'}:
        ticket = board.claim_ticket(original, ticket['id'], ticket['version'])
        if guard == 'human_request':
            board.request_human_input(original, ticket['id'], human['owner_id'], 'Need an answer', 'question')
            ticket = board.get_ticket(human, ticket['id'])['ticket']
    age(board, source_id, ticket)
    if guard == 'fresh_session':
        board.read_inbox(original)
    elif guard == 'recent_assignment':
        with sqlite3.connect(board.db_path) as db:
            db.execute('UPDATE tickets SET updated_at=? WHERE id=?', (time.time(), ticket['id']))
    elif guard in {'paused', 'archived'}:
        profile = board.get_agent_profile(human, source_id)
        board.update_agent_profile(human, source_id, profile['version'], status=guard)
    elif guard in {'blocked', 'review', 'in_progress'}:
        ticket = board.update_ticket(human, ticket['id'], ticket['version'], status=guard)
        age(board, source_id, ticket)
    assert ticket['id'] not in eligible(board, successor, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'])
    assert board.get_ticket(human, ticket['id'])['ticket']['assignee_agent_id'] == source_id


@pytest.mark.parametrize('offline_reason', ['never_seen', 'revoked'])
def test_offline_assignment_gets_grace_then_can_be_claimed(idle, offline_reason):
    space, source_id, successor, _, _ = idle
    board, human, _, project, _, _, device = space
    ticket = new(space, assignee_agent_id=source_id)
    if offline_reason == 'never_seen':
        with sqlite3.connect(board.db_path) as db:
            db.execute('UPDATE sessions SET last_seen_at=NULL WHERE agent_id=?', (source_id,))
    else:
        board.revoke_device(human, device['id'])
    assert ticket['id'] not in eligible(board, successor, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'])
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET updated_at=? WHERE id=?', (time.time() - 3600, ticket['id']))
    assert board.claim_next_ticket(successor, 'offline-after-grace', project['id'])['ticket']['id'] == ticket['id']


@pytest.mark.parametrize('change', ['source_online', 'human_edit'])
def test_handoff_rechecks_presence_and_version_after_discovery(idle, change):
    space, source_id, successor, _, _ = idle
    board, human, _, project, original, *_ = space
    ticket = new(space, assignee_agent_id=source_id)
    age(board, source_id, ticket)
    assert ticket['id'] in eligible(board, successor, project['id'])
    if change == 'source_online':
        board.read_inbox(original)
    else:
        board.update_ticket(human, ticket['id'], ticket['version'], progress='Human just reviewed this assignment')
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'])
    assert not board.claim_next_ticket(successor, 'rechecked', project['id'])['claimed']
    assert board.get_ticket(human, ticket['id'])['ticket']['assignee_agent_id'] == source_id


@pytest.mark.parametrize('gate', ['resource', 'archived_project'])
def test_idle_handoff_keeps_resource_and_project_gates(idle, gate):
    space, source_id, successor, _, _ = idle
    board, human, _, project, *_ = space
    ticket = new(space, assignee_agent_id=source_id, resource='repo:shared')
    if gate == 'resource':
        holder, _, _ = worker(board, human, 'Resource holder')
        other = new(space, 'Other resource owner', resource='repo:shared')
        board.claim_ticket(holder, other['id'], other['version'])
    else:
        board.update_project(human, project['id'], project['version'], archived=True)
    age(board, source_id, ticket)
    assert ticket['id'] not in eligible(board, successor, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'])


def test_idle_assignment_preserves_owner_and_project_scope(idle):
    space, source_id, _, _, _ = idle
    board, human, other, project, *_ = space
    outsider, outsider_id, outsider_device = worker(board, other, 'Other owner')
    owner_only = new(space, assignee_agent_id=source_id, assignee_owner_id=human['owner_id'])
    age(board, source_id, owner_only)
    assert owner_only['id'] not in eligible(board, outsider, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(outsider, owner_only['id'], owner_only['version'])
    scoped = new(space, 'Shared but out of scope', assignee_agent_id=source_id)
    age(board, source_id, scoped)
    profile = board.get_agent_profile(other, outsider_id)
    board.update_agent_profile(other, outsider_id, profile['version'], project_ids=[])
    board.register_agent(board.authenticate(outsider_device['token']), outsider['session_id'], 'Other owner', ['python'], 'codex', True, 'Other owner')
    with pytest.raises(BoardError):
        board.claim_ticket(outsider, scoped['id'], scoped['version'])
    assert scoped['id'] not in eligible(board, outsider, '')


def test_expired_idle_claim_needs_explicit_evidence_and_fences_original(idle):
    space, source_id, successor, successor_id, _ = idle
    board, human, _, project, original, _, _ = space
    ticket = new(space, assignee_agent_id=source_id)
    claim = board.claim_ticket(original, ticket['id'], ticket['version'])
    age(board, source_id, claim, expired=True)
    work = board.get_work(successor, project['id'])
    assert [item['id'] for item in work['recovery']] == [ticket['id']]
    assert not work['ready']
    assert not board.claim_next_ticket(successor, 'no-silent-recovery', project['id'])['claimed']
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], claim['version'])
    recovered = board.claim_ticket(successor, ticket['id'], claim['version'], 'Inspected checkout and remote effects; no pending writes')
    assert recovered['claimant']['agent_id'] == successor_id and recovered['assignee_agent_id'] is None
    with pytest.raises(BoardError):
        board.update_ticket(original, ticket['id'], recovered['version'], progress='Obsolete worker')
    with pytest.raises(BoardError):
        board.renew_ticket(original, ticket['id'], recovered['version'])


@pytest.mark.parametrize('claimed', [False, True])
@pytest.mark.parametrize('execution_state', ['reserved', 'running', 'needs_review'])
def test_unresolved_runtime_blocks_idle_handoff_even_when_heartbeat_stale(idle, claimed, execution_state):
    space, source_id, successor, _, _ = idle
    board, human, _, project, original, _, _ = space
    ticket = new(space, assignee_agent_id=source_id)
    ticket = board.claim_ticket(original, ticket['id'], ticket['version'])
    execution = board.reserve_execution(original, 'execution', project['id'], ticket['id'], 'work', 'turn', 'codex_chatgpt', 'fixture-model')['execution']
    if execution_state in {'running', 'needs_review'}:
        execution = board.start_execution(original, execution['id'], execution['revision'])
    if execution_state == 'needs_review':
        execution = board.finish_execution(original, execution['id'], execution['revision'], 'blocked', False, 'cleanup_failed')
    if not claimed:
        ticket = board.update_ticket(human, ticket['id'], ticket['version'], release_claim=True)
    age(board, source_id, ticket, expired=claimed)
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE runtime_executions SET last_heartbeat_at=? WHERE id=?', (time.time() - 3600, execution['id']))
    assert ticket['id'] not in eligible(board, successor, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'], 'Lease expired but executor cleanup is unresolved')
    assert board.get_runtime(human)['active_count'] == 1
    assert board.get_ticket(human, ticket['id'])['ticket']['assignee_agent_id'] == source_id


def test_confirmed_runtime_cleanup_allows_explicit_recovery(idle):
    space, source_id, successor, _, _ = idle
    board, _, _, project, original, _, _ = space
    ticket = new(space, assignee_agent_id=source_id)
    ticket = board.claim_ticket(original, ticket['id'], ticket['version'])
    execution = board.reserve_execution(original, 'execution', project['id'], ticket['id'], 'work', 'turn', 'codex_chatgpt', 'fixture-model')['execution']
    execution = board.start_execution(original, execution['id'], execution['revision'])
    board.finish_execution(original, execution['id'], execution['revision'], 'cancelled', True, 'cancelled')
    age(board, source_id, ticket, expired=True)
    recovered = board.claim_ticket(successor, ticket['id'], ticket['version'], 'Prior execution finished with confirmed cleanup; inspected remaining effects')
    assert recovered['claimant']['session_id'] == successor['session_id']


def test_idle_handoff_still_serializes_competing_workers(idle):
    space, source_id, successor, _, _ = idle
    board, human, _, project, *_ = space
    competitor, _, _ = worker(board, human, 'Competitor')
    ticket = new(space, assignee_agent_id=source_id)
    age(board, source_id, ticket)
    barrier = threading.Barrier(2)
    def claim(actor):
        barrier.wait()
        return board.claim_next_ticket(actor, 'competing-handoff', project['id'])
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(claim, [successor, competitor]))
    assert sum(result['claimed'] for result in results) == 1


def test_same_profile_expired_work_still_blocks_unrelated_selection(idle):
    space, source_id, _, _, _ = idle
    board, _, _, project, original, second, _ = space
    ticket = new(space, assignee_agent_id=source_id)
    claim = board.claim_ticket(original, ticket['id'], ticket['version'])
    age(board, source_id, claim, expired=True)
    new(space, 'Unrelated ready work')
    assert board.claim_next_ticket(second, 'restart-cannot-evade', project['id'])['reason'] == 'unfinished_work'
