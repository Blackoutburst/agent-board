"""Independent adversarial checks for owner-wide capacity and cleanup authority."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from backup import create_backup, restore_backup
from board import Board, BoardError
from test_runtime import runtime, reserve, snapshot
from test_runtime_api import browser, reservation, rpc


@pytest.mark.parametrize('reason', ['quota_or_rate_limit', 'authentication_failed'])
def test_cleanup_confirmation_does_not_reobserve_cleared_provider_failure(runtime, reason):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    started = board.start_execution(actor, execution['id'], 1)
    pending = board.finish_execution(actor, execution['id'], started['revision'], 'blocked', False, reason)
    observed = board.get_runtime(human)
    assert observed['limits']['paused'] and observed['active_count'] == 1
    resumed = board.update_runtime_limits(human, observed['limits']['version'], 1, False)

    finished = board.finish_execution(actor, execution['id'], pending['revision'], 'blocked', True, reason)

    assert finished['state'] == 'finished'
    current = board.get_runtime(human)
    assert current['active_count'] == 0
    assert current['limits'] == resumed
    assert current['quota'] == {'status': 'unavailable'}


def test_terminal_finish_retry_cannot_repause_after_human_resume(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    started = board.start_execution(actor, execution['id'], 1)
    finished = board.finish_execution(actor, execution['id'], started['revision'], 'blocked', True, 'quota_or_rate_limit')
    limits = board.get_runtime(human)['limits']
    resumed = board.update_runtime_limits(human, limits['version'], 1, False)
    before = snapshot(board)
    retried = board.finish_execution(actor, execution['id'], started['revision'], 'blocked', True, 'quota_or_rate_limit')
    assert retried == finished
    assert board.get_runtime(human)['limits'] == resumed
    assert snapshot(board) == before


def test_later_cleanup_reason_is_not_a_second_provider_observation(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    started = board.start_execution(actor, execution['id'], 1)
    pending = board.finish_execution(actor, execution['id'], started['revision'], 'blocked', False, 'quota_or_rate_limit')
    limits = board.get_runtime(human)['limits']
    resumed = board.update_runtime_limits(human, limits['version'], 1, False)
    board.finish_execution(actor, execution['id'], pending['revision'], 'blocked', True, 'authentication_failed')
    assert board.get_runtime(human)['limits'] == resumed
    assert board.get_runtime(human)['quota'] == {'status': 'unavailable'}


def test_provider_observation_serializes_against_stale_human_limits_edit(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    started = board.start_execution(actor, execution['id'], 1)
    barrier = threading.Barrier(2)

    def finish():
        barrier.wait()
        return board.finish_execution(actor, execution['id'], started['revision'], 'blocked', True, 'quota_or_rate_limit')

    def edit():
        barrier.wait()
        try:
            return board.update_runtime_limits(human, 1, 2, False)
        except BoardError as error:
            return str(error)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(finish)
        second = pool.submit(edit)
        assert first.result()['state'] == 'finished'
        edit_result = second.result()
    assert isinstance(edit_result, dict) or edit_result == 'runtime_limits_version_conflict'
    current = board.get_runtime(human)
    assert current['limits']['paused']
    assert current['quota'] == {'status': 'blocked_observed', 'reason': 'quota_or_rate_limit'}


def test_competing_finish_outcomes_cannot_both_commit(runtime):
    board, human, _, _, agent = runtime
    actor, ticket, *_ = agent()
    execution = reserve(runtime, actor, ticket)
    started = board.start_execution(actor, execution['id'], 1)
    barrier = threading.Barrier(2)

    def finish(outcome):
        barrier.wait()
        try:
            return board.finish_execution(actor, execution['id'], started['revision'], outcome, True)
        except BoardError as error:
            return str(error)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(finish, ['cancelled', 'completed']))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count('execution_revision_conflict') == 1
    assert board.get_runtime(human)['active_count'] == 0
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='execution_finished'").fetchone()[0] == 1


@pytest.mark.parametrize('field', ['owner_id', 'device_id', 'session_id'])
def test_cleanup_identity_rejects_spliced_tuples_without_mutation(runtime, field):
    board, human, _, _, agent = runtime
    victim, ticket, *_ = agent()
    outsider, _, *_ = agent('thescreamer')
    execution = reserve(runtime, victim, ticket)
    spliced = {**victim, field: outsider[field]}
    before = snapshot(board)
    for method, args in [
        (board.lookup_execution, ('reservation',)),
        (board.get_execution_control, (execution['id'],)),
        (board.finish_execution, (execution['id'], 1, 'cancelled', True)),
    ]:
        with pytest.raises(BoardError):
            method(spliced, *args)
    assert snapshot(board) == before
    assert board.get_runtime(human)['active_count'] == 1


def test_mcp_same_owner_device_cannot_select_another_devices_cleanup_session(browser):
    client, board, human, _, _, _, _ = browser
    execution = reservation(browser)['execution']
    other_device = board.create_device(human, 'Other fixture host', [])
    other = list(browser)
    other[3] = other_device['token']
    before = snapshot(board)
    for method, args in [
        ('lookup_execution', {'idempotency_key': 'api-reservation'}),
        ('get_execution_control', {'execution_id': execution['id']}),
        ('finish_execution', {'execution_id': execution['id'], 'expected_revision': 1,
                              'status': 'cancelled', 'cleanup_confirmed': True}),
    ]:
        denied = rpc(other, method, args, error=True)
        assert execution['id'] not in json.dumps(denied)
    assert snapshot(board) == before
    assert client.get('/api/runtime').json()['active_count'] == 1


def test_rest_other_owner_cannot_observe_or_control_even_shared_project_execution(browser):
    client, board, human, token, *_ = browser
    board.ensure_owner('thescreamer', 'Other fixture owner', 'other-fixture-password')
    shared = board.create_project(human, 'Shared fixture', ['blackoutburst', 'thescreamer'], idempotency_key='shared')
    ticket = board.create_ticket(human, shared['id'], 'Shared ticket', idempotency_key='shared-ticket')
    actor = {**board.authenticate(token), 'session_id': 'runtime-api'}
    board.claim_ticket(actor, ticket['id'], ticket['version'])
    execution = reservation(browser, project_id=shared['id'], ticket_id=ticket['id'])['execution']
    login = client.post('/api/login', json={'username': 'thescreamer', 'password': 'other-fixture-password'})
    client.headers['X-CSRF-Token'] = login.json()['csrf_token']
    before = snapshot(board)
    current = client.get('/api/runtime').json()
    assert current['executions'] == [] and current['active_count'] == 0
    assert current['quota'] == {'status': 'unavailable'}
    denied = client.post('/api/runtime/executions/' + execution['id'] + '/stop', json={'expected_revision': 1})
    assert denied.status_code == 404
    assert execution['id'] not in denied.text
    assert snapshot(board) == before


def test_profile_archival_and_reassignment_preserve_only_old_execution_cleanup(runtime):
    board, human, _, _, agent = runtime
    old, ticket, profile_id, _ = agent()
    new, _, *_ = agent()
    execution = reserve(runtime, old, ticket)
    started = board.start_execution(old, execution['id'], 1)
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], status='archived')
    board.update_ticket(human, ticket['id'], ticket['version'], release_claim=True)
    current_ticket = board.get_ticket(human, ticket['id'])['ticket']
    board.claim_ticket(new, ticket['id'], current_ticket['version'])

    assert board.get_execution_control(old, execution['id'])['control_reason'] == 'authorization_changed'
    with pytest.raises(BoardError):
        board.start_execution(old, execution['id'], started['revision'])
    with pytest.raises(BoardError):
        board.get_execution_control(new, execution['id'])
    current = board.heartbeat_execution(old, execution['id'], started['revision'])
    assert current['should_stop']
    finished = board.finish_execution(old, execution['id'], current['revision'], 'cancelled', True, 'policy_changed')
    assert finished['state'] == 'finished'
    assert board.get_ticket(human, ticket['id'])['ticket']['claimant']['session_id'] == new['session_id']


def test_revoked_mcp_device_cannot_finish_its_own_slot(browser):
    client, board, human, token, *_ = browser
    execution = reservation(browser)['execution']
    actor = board.authenticate(token)
    board.revoke_device(human, actor['device_id'])
    before = snapshot(board)
    denied = client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'finish_execution', 'arguments': {
            'session_id': 'runtime-api', 'execution_id': execution['id'], 'expected_revision': 1,
            'status': 'cancelled', 'cleanup_confirmed': True}}})
    assert denied.status_code == 401
    assert snapshot(board) == before
    assert board.get_runtime(human)['active_count'] == 1


def test_schema_six_backup_restores_capacity_cleanup_and_quota_observation(runtime, tmp_path):
    board, human, _, _, agent = runtime
    board.update_runtime_limits(human, 1, 2, False)
    first, one, *_ = agent()
    second, two, *_ = agent()
    r1 = reserve(runtime, first, one, 'one')
    r2 = reserve(runtime, second, two, 'two')
    started = board.start_execution(first, r1['id'], 1)
    board.finish_execution(first, r1['id'], started['revision'], 'blocked', False, 'quota_or_rate_limit')
    expected = board.get_runtime(human)
    archive = tmp_path / 'runtime-backup.zip'
    manifest = create_backup(board.data_dir, archive)
    assert manifest['schema']['user_version'] == 17
    destination = tmp_path / 'restored'
    restore_backup(archive, destination)
    restored = Board(destination)
    assert restored.get_runtime(human) == expected
    assert restored.get_runtime(human)['active_count'] == 2
    assert restored.lookup_execution(second, 'two')['execution']['id'] == r2['id']
    pending = restored.get_execution_control(first, r1['id'])
    restored.finish_execution(first, r1['id'], pending['revision'], 'blocked', True, 'quota_or_rate_limit')
    assert restored.get_runtime(human)['active_count'] == 1
    assert board.get_runtime(human)['active_count'] == 2


def test_future_schema_is_refused_without_touching_existing_runtime_rows(runtime):
    board, _, _, _, agent = runtime
    actor, ticket, *_ = agent()
    reserve(runtime, actor, ticket)
    with sqlite3.connect(board.db_path) as db:
        db.execute('PRAGMA user_version=18')
    before = snapshot(board)
    with pytest.raises(RuntimeError, match='newer'):
        Board(board.data_dir)
    assert snapshot(board) == before
    with sqlite3.connect(board.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 18
