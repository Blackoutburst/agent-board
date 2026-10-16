"""Central capacity, exact execution identity and fail-closed cleanup."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time

import pytest

from board import Board, BoardError


@pytest.fixture
def runtime(tmp_path):
    board = Board(tmp_path / 'board')
    owners = ['blackoutburst', 'thescreamer', 'outsider']
    for owner in owners:
        board.ensure_owner(owner, owner.title())
        board.set_policy({'kind': 'human', 'owner_id': owner}, True, True, True, True, [], [])
    human = {'kind': 'human', 'owner_id': owners[0]}
    other = {'kind': 'human', 'owner_id': owners[1]}
    project = board.create_project(human, 'Shared', owners[:2], idempotency_key='project')
    counter = 0

    def agent(owner='blackoutburst', *, device=None, claim=True):
        nonlocal counter
        counter += 1
        person = {'kind': 'human', 'owner_id': owner}
        device = device or board.create_device(person, 'Fixture device', [])
        raw = board.authenticate(device['token'])
        session_id = 'runtime-session-' + str(counter)
        registration = board.register_agent(raw, session_id, 'Fixture', [], 'codex', False, 'fixture')
        actor = {**raw, 'session_id': session_id}
        ticket = board.create_ticket(person, project['id'], 'Fixture ticket', idempotency_key='ticket-' + str(counter)) if owner != 'outsider' else None
        if claim:
            ticket = board.claim_ticket(actor, ticket['id'], ticket['version'])
        return actor, ticket, registration['identity']['id'], device

    return board, human, other, project, agent


def reserve(runtime, actor, ticket, key='reservation', **changes):
    board, _, _, project, _ = runtime
    values = dict(idempotency_key=key, project_id=project['id'], ticket_id=ticket['id'],
                  work_id='work-' + key, turn_id='turn-' + key, provider='codex_chatgpt', model='fixture-model')
    values.update(changes)
    return board.reserve_execution(actor, **values)['execution']


def rejects(call, *args, message=None, status=None, **kwargs):
    with pytest.raises(BoardError) as error:
        call(*args, **kwargs)
    if message:
        assert str(error.value) == message
    if status:
        assert error.value.status == status


def snapshot(board):
    with sqlite3.connect(board.db_path) as db:
        return tuple(db.iterdump())


def test_migration_default_is_read_only_and_persists(runtime):
    board, human, *_ = runtime
    before = snapshot(board)
    value = board.get_runtime(human)
    assert value == {'limits': {'owner_id': 'blackoutburst', 'max_concurrent': 1, 'paused': False, 'version': 1},
                     'active_count': 0, 'quota': {'status': 'unavailable'}, 'usage_observations': [], 'executions': []}
    assert snapshot(board) == before
    with sqlite3.connect(board.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 11
    assert Board(board.data_dir).get_runtime(human) == value


def test_owner_capacity_serialized_across_profiles_and_devices(runtime):
    board, human, _, _, agent = runtime
    one, t1, p1, d1 = agent()
    two, t2, p2, d2 = agent()
    assert p1 != p2 and d1['id'] != d2['id']
    barrier = threading.Barrier(2)
    def contend(item):
        actor, ticket, key = item
        barrier.wait()
        try:
            return reserve(runtime, actor, ticket, key)
        except BoardError as error:
            return str(error)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(contend, [(one, t1, 'one'), (two, t2, 'two')]))
    assert sum(isinstance(value, dict) for value in results) == 1
    assert results.count('capacity_exhausted') == 1
    assert board.get_runtime(human)['active_count'] == 1


def test_different_owner_capacity_is_independent_and_private(runtime):
    board, human, other, _, agent = runtime
    one, t1, *_ = agent()
    two, t2, *_ = agent('thescreamer')
    e1, e2 = reserve(runtime, one, t1), reserve(runtime, two, t2)
    assert [e['id'] for e in board.get_runtime(human)['executions']] == [e1['id']]
    assert [e['id'] for e in board.get_runtime(other)['executions']] == [e2['id']]
    rejects(board.stop_execution, other, e1['id'], 1, status=404)
    rejects(board.get_runtime, one, status=403)


def test_same_profile_new_session_cannot_read_or_free_execution(runtime):
    board, _, _, _, agent = runtime
    one, t1, p1, device = agent()
    two, t2, p2, _ = agent(device=device)
    assert p1 == p2
    execution = reserve(runtime, one, t1)
    assert board.lookup_execution(two, 'reservation') == {'found': False}
    for method, extra in [(board.get_execution_control, ()), (board.start_execution, (1,)),
                          (board.heartbeat_execution, (1,)), (board.finish_execution, (1, 'cancelled', True))]:
        rejects(method, two, execution['id'], *extra, status=404)
    rejects(reserve, runtime, two, t2, 'other', message='capacity_exhausted')


def test_reservation_idempotence_and_turn_identity(runtime):
    board, _, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    before = snapshot(board)
    retried = reserve(runtime, actor, ticket)
    assert retried['id'] == execution['id']
    assert board.lookup_execution(actor, 'reservation') == {'found': True, 'execution': execution}
    assert board.lookup_execution(actor, 'absent') == {'found': False}
    assert snapshot(board) == before
    for key, value in [('model', 'different'), ('provider', 'different'), ('turn_id', 'different'), ('work_id', 'different')]:
        rejects(reserve, runtime, actor, ticket, **{key: value}, message='execution_key_conflict')
    rejects(reserve, runtime, actor, ticket, 'another', work_id='work-reservation', turn_id='turn-reservation', message='execution_turn_conflict')


def test_current_claim_scope_and_permissions_required(runtime):
    board, human, _, project, agent = runtime
    actor, ticket, profile_id, _ = agent(claim=False)
    rejects(reserve, runtime, actor, ticket, message='active_ticket_claim_required')
    claimed = board.claim_ticket(actor, ticket['id'], ticket['version'])
    other_project = board.create_project(human, 'Other', ['blackoutburst'], idempotency_key='other')
    rejects(reserve, runtime, actor, claimed, project_id=other_project['id'], message='ticket_project_mismatch')
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], policy={**profile['policy'], 'allow_requests': False})
    rejects(reserve, runtime, actor, claimed, status=409)


def test_pause_blocks_admission_and_start_and_instructs_running_stop(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    limits = board.update_runtime_limits(human, 1, 1, True)
    assert limits['version'] == 2 and limits['paused']
    rejects(board.start_execution, actor, execution['id'], 1, message='owner_paused')
    assert board.get_execution_control(actor, execution['id'])['should_stop']
    board.update_runtime_limits(human, 2, 1, False)
    running = board.start_execution(actor, execution['id'], 1)
    board.update_runtime_limits(human, 3, 1, True)
    heartbeat = board.heartbeat_execution(actor, execution['id'], running['revision'])
    assert heartbeat['should_stop'] and heartbeat['control_reason'] == 'owner_paused'
    finished = board.finish_execution(actor, execution['id'], heartbeat['revision'], 'cancelled', True, 'owner_paused')
    assert finished['state'] == 'finished'
    rejects(reserve, runtime, actor, ticket, 'next', message='owner_paused')


def test_control_and_cleanup_survive_profile_change_and_owner_policy_disabled(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, profile_id, _ = agent()
    execution = reserve(runtime, actor, ticket)
    running = board.start_execution(actor, execution['id'], 1)
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], status='paused')
    board.set_policy(human, False, False, False, False, [], [])
    assert board.lookup_execution(actor, 'reservation')['found']
    assert board.get_execution_control(actor, execution['id'])['control_reason'] == 'authorization_changed'
    heartbeat = board.heartbeat_execution(actor, execution['id'], running['revision'])
    assert heartbeat['should_stop']
    finished = board.finish_execution(actor, execution['id'], heartbeat['revision'], 'cancelled', True, 'policy_changed')
    assert finished['state'] == 'finished'
    rejects(board.start_execution, actor, execution['id'], finished['revision'], status=403)
    rejects(board.update_ticket, actor, ticket['id'], ticket['version'], outcome='Not authorized', status=403)


def test_revocation_disables_even_cleanup_and_slot_is_not_released(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, _, device = agent()
    execution = reserve(runtime, actor, ticket)
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE devices SET revoked_at=? WHERE id=?', (time.time(), device['id']))
    for method, args in [(board.lookup_execution, ('reservation',)),
                         (board.get_execution_control, (execution['id'],)),
                         (board.finish_execution, (execution['id'], 1, 'cancelled', True))]:
        rejects(method, actor, *args, status=403)
    assert board.get_runtime(human)['active_count'] == 1


def test_cleanup_uncertain_retains_slot_and_truthful_cleanup_releases(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    running = board.start_execution(actor, execution['id'], 1)
    uncertain = board.finish_execution(actor, execution['id'], running['revision'], 'blocked', False, 'cleanup_failed')
    assert uncertain['state'] == 'needs_review' and uncertain['should_stop']
    assert board.get_runtime(human)['active_count'] == 1
    rejects(reserve, runtime, actor, ticket, 'next', message='capacity_exhausted')
    assert board.finish_execution(actor, execution['id'], running['revision'], 'blocked', False, 'cleanup_failed') == uncertain
    finished = board.finish_execution(actor, execution['id'], uncertain['revision'], 'blocked', True, 'cleanup_failed')
    assert finished['state'] == 'finished' and finished['cleanup_confirmed']
    assert board.get_runtime(human)['active_count'] == 0
    reserve(runtime, actor, ticket, 'next')


def test_stale_heartbeat_does_not_release_capacity_or_mutate_reads(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    running = board.start_execution(actor, execution['id'], 1)
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE runtime_executions SET last_heartbeat_at=? WHERE id=?', (time.time() - 3600, execution['id']))
    before = snapshot(board)
    current = board.get_execution_control(actor, execution['id'])
    assert current['stale'] and current['state'] == 'running'
    assert board.get_runtime(human)['active_count'] == 1
    assert snapshot(board) == before
    rejects(reserve, runtime, actor, ticket, 'next', message='capacity_exhausted')
    heartbeat = board.heartbeat_execution(actor, execution['id'], running['revision'])
    assert not heartbeat['stale']


def test_claim_expiry_and_project_archival_signal_stop(runtime):
    board, human, _, project, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (time.time() - 1, ticket['id']))
    assert board.get_execution_control(actor, execution['id'])['should_stop']
    rejects(board.start_execution, actor, execution['id'], 1, message='active_ticket_claim_required')
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (time.time() + 300, ticket['id']))
    board.update_project(human, project['id'], project['version'], archived=True)
    assert board.get_execution_control(actor, execution['id'])['should_stop']
    rejects(board.start_execution, actor, execution['id'], 1, message='project_archived')


def test_cas_retry_and_stop_revision_race(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    running = board.start_execution(actor, execution['id'], 1)
    assert board.start_execution(actor, execution['id'], 1) == running
    heartbeat = board.heartbeat_execution(actor, execution['id'], running['revision'])
    assert board.heartbeat_execution(actor, execution['id'], running['revision']) == heartbeat
    stop = board.stop_execution(human, execution['id'], heartbeat['revision'])
    assert stop['stop_requested'] and stop['should_stop'] and stop['state'] == 'running'
    assert board.get_runtime(human)['active_count'] == 1
    rejects(board.finish_execution, actor, execution['id'], heartbeat['revision'], 'cancelled', True, status=409)
    finished = board.finish_execution(actor, execution['id'], stop['revision'], 'cancelled', True, 'stop_requested')
    assert board.finish_execution(actor, execution['id'], stop['revision'], 'cancelled', True, 'stop_requested') == finished
    rejects(board.finish_execution, actor, execution['id'], stop['revision'], 'completed', True, status=409)
    rejects(board.start_execution, actor, execution['id'], finished['revision'], status=409)


def test_limits_cas_and_lowering_does_not_release_existing_slots(runtime):
    board, human, _, _, agent = runtime
    board.update_runtime_limits(human, 1, 2, False)
    rejects(board.update_runtime_limits, human, 1, 8, False, status=409)
    one, t1, *_ = agent()
    two, t2, *_ = agent()
    e1, e2 = reserve(runtime, one, t1, 'one'), reserve(runtime, two, t2, 'two')
    board.update_runtime_limits(human, 2, 1, False)
    rejects(board.start_execution, one, e1['id'], 1, message='capacity_exhausted')
    assert board.get_runtime(human)['active_count'] == 2
    board.finish_execution(two, e2['id'], 1, 'cancelled', True)
    assert board.start_execution(one, e1['id'], 1)['state'] == 'running'


@pytest.mark.parametrize('value', [True, 0, 9, 1.0, '1', None])
def test_strict_limit_bounds(runtime, value):
    board, human, *_ = runtime
    rejects(board.update_runtime_limits, human, 1, value, False)


@pytest.mark.parametrize('value', [True, 0, -1, 1.0, '1', None])
def test_strict_revisions(runtime, value):
    board, _, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    rejects(board.start_execution, actor, execution['id'], value)


@pytest.mark.parametrize('field,value', [('work_id', '/private/tmp/secret'), ('turn_id', 'provider session text'),
    ('model', 'model\nsecret'), ('provider', 'https://account@secret'), ('idempotency_key', ''),
    ('work_id', '\ud800'), ('turn_id', 4), ('model', 'x' * 201)])
def test_metadata_identifiers_are_bounded_not_arbitrary_text(runtime, field, value):
    _, _, _, _, agent = runtime
    actor, ticket, *_ = agent()
    if field == 'idempotency_key':
        rejects(reserve, runtime, actor, ticket, key=value)
    else:
        rejects(reserve, runtime, actor, ticket, **{field: value})


def test_no_payload_key_prompt_pid_path_or_raw_error_in_projection(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    rejects(board.finish_execution, actor, execution['id'], 1, 'blocked', True, 'PRIVATE DIAGNOSTIC')
    rejects(board.finish_execution, actor, execution['id'], 1, 'blocked', 'true')
    view = board.get_runtime(human)
    raw = json.dumps(view)
    for forbidden in ['idempotency_key', 'payload', 'prompt', 'token', 'provider_session', 'pid', 'workspace', 'PRIVATE DIAGNOSTIC']:
        assert forbidden not in raw
    assert view['quota'] == {'status': 'unavailable'}


@pytest.mark.parametrize('reason', ['quota_or_rate_limit', 'authentication_failed'])
def test_provider_failure_pauses_owner_until_explicit_human_resume(runtime, reason):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    running = board.start_execution(actor, execution['id'], 1)
    finished = board.finish_execution(actor, execution['id'], running['revision'], 'blocked', True, reason)
    view = board.get_runtime(human)
    assert view['limits']['paused'] and view['limits']['version'] == 2
    assert view['quota'] == {'status': 'blocked_observed', 'reason': reason}
    assert view['active_count'] == 0
    assert board.finish_execution(actor, execution['id'], running['revision'], 'blocked', True, reason) == finished
    assert board.get_runtime(human)['limits']['version'] == 2
    rejects(reserve, runtime, actor, ticket, 'next', message='owner_paused')
    board.update_runtime_limits(human, 2, 1, False)
    assert board.get_runtime(human)['quota'] == {'status': 'unavailable'}
    assert reserve(runtime, actor, ticket, 'next')['state'] == 'reserved'


def test_schema_five_upgrade_preserves_existing_data_and_migrates_once(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    with sqlite3.connect(board.db_path) as db:
        db.execute('DROP TABLE message_feeds')
        db.execute('DROP TABLE ticket_attachments')
        db.execute('DROP TABLE usage_observations')
        db.execute('DROP TABLE runtime_executions')
        db.execute('DROP TABLE owner_execution_limits')
        db.execute('PRAGMA user_version=5')
    upgraded = Board(board.data_dir)
    assert upgraded.get_ticket(human, ticket['id'])['ticket'] == ticket
    assert upgraded.lookup_execution(actor, 'absent') == {'found': False}
    assert upgraded.get_runtime(human)['limits']['max_concurrent'] == 1
    assert Board(board.data_dir).get_ticket(human, ticket['id'])['ticket'] == ticket


def test_same_profile_on_another_device_shares_capacity_not_control(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, profile_id, _ = agent()
    device = board.create_device(human, 'Second host', [])
    profile = board.get_agent_profile(human, profile_id)
    board.bind_agent_profile(human, profile_id, profile['version'], device['id'], 'fixture', 'codex')
    # Binding increments profile version, requiring explicit fresh registration.
    # Re-register only after the human explicitly releases its old-version claim.
    board.update_ticket(human, ticket['id'], ticket['version'], release_claim=True)
    raw = {key: value for key, value in actor.items() if key != 'session_id'}
    board.register_agent(raw, actor['session_id'], 'Fixture', [], 'codex', False, 'fixture')
    claimed = board.claim_ticket(actor, ticket['id'], board.get_ticket(human, ticket['id'])['ticket']['version'])
    second, other_ticket, second_profile, _ = agent(device=device)
    assert second_profile == profile_id and second['device_id'] != actor['device_id']
    execution = reserve(runtime, actor, claimed)
    rejects(reserve, runtime, second, other_ticket, 'other', message='capacity_exhausted')
    rejects(board.get_execution_control, second, execution['id'], status=404)


def test_reserved_execution_cannot_claim_a_completed_model_turn(runtime):
    board, _, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    rejects(board.finish_execution, actor, execution['id'], 1, 'completed', True, message='execution_not_started')
    assert board.finish_execution(actor, execution['id'], 1, 'cancelled', True)['state'] == 'finished'


def test_runtime_migration_failure_is_atomic(runtime, monkeypatch):
    import board as board_module
    board, *_ = runtime
    with sqlite3.connect(board.db_path) as db:
        db.execute('DROP TABLE message_feeds')
        db.execute('DROP TABLE ticket_attachments')
        db.execute('DROP TABLE usage_observations')
        db.execute('DROP TABLE runtime_executions')
        db.execute('DROP TABLE owner_execution_limits')
        db.execute('PRAGMA user_version=5')
    before = snapshot(board)
    original = board_module.migrate_runtime
    def failed_migration(db):
        original(db)
        raise RuntimeError('synthetic migration failure')
    monkeypatch.setattr(board_module, 'migrate_runtime', failed_migration)
    with pytest.raises(RuntimeError, match='synthetic migration failure'):
        Board(board.data_dir)
    assert snapshot(board) == before
    monkeypatch.setattr(board_module, 'migrate_runtime', original)
    assert Board(board.data_dir).db_path == board.db_path
