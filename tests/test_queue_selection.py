"""Durable one-ticket selections and strictly read-only key reconciliation."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from board import BoardError
from test_coordination import space, new


def snapshot(board):
    with sqlite3.connect(board.db_path) as db:
        return list(db.iterdump())


def test_empty_lookup_never_mutates_or_falls_back_to_claim(space):
    board, _, _, project, first, _, device = space
    task = new(space)
    board.register_agent(board.authenticate(device['token']), first['session_id'], 'Reader', [], 'codex', False, 'installation')
    before = snapshot(board)
    assert board.lookup_ticket_selection(first, 'unknown', project['id']) == {'found': False}
    assert snapshot(board) == before
    assert board.get_ticket(first, task['id'])['ticket']['claimant'] is None


def test_no_candidate_result_is_durable_and_cannot_select_later_ticket(space):
    board, _, _, project, first, *_ = space
    initial = board.claim_next_ticket(first, 'empty', project['id'])
    assert initial == {'claimed': False, 'reason': 'no_ready_ticket', 'retried': False}
    task = new(space)
    expected = {**initial, 'retried': True}
    assert board.claim_next_ticket(first, 'empty', project['id']) == expected
    before = snapshot(board)
    assert board.lookup_ticket_selection(first, 'empty', project['id']) == {'found': True, **expected}
    assert snapshot(board) == before
    assert board.claim_next_ticket(first, 'new-attempt', project['id'])['ticket']['id'] == task['id']


def test_legacy_positive_key_keeps_selected_ticket_and_returns_live_state(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    selected = board.claim_next_ticket(first, 'old-key', project['id'])['ticket']
    with sqlite3.connect(board.db_path) as db:
        record = db.execute("SELECT record_id FROM idempotency WHERE operation='claim_next_ticket' AND key='old-key'").fetchone()[0]
        assert record == task['id']  # Historical storage remains readable.
        assert db.execute('PRAGMA user_version').fetchone()[0] == 10
    board.update_ticket(human, task['id'], selected['version'], title='Updated task', release_claim=True)
    new(space, 'Later urgent work', priority='urgent')
    before = snapshot(board)
    lookup = board.lookup_ticket_selection(first, 'old-key', project['id'])
    assert lookup['found'] and lookup['claimed'] and lookup['retried']
    assert lookup['ticket']['id'] == task['id'] and lookup['ticket']['title'] == 'Updated task'
    assert lookup['ticket']['claimant'] is None
    assert snapshot(board) == before
    assert board.claim_next_ticket(first, 'old-key', project['id'])['ticket'] == lookup['ticket']


def test_same_key_concurrent_requests_claim_only_one_ticket(space):
    board, _, _, project, first, *_ = space
    task = new(space)
    other = new(space, 'Other work')
    barrier = threading.Barrier(2)
    def select():
        barrier.wait()
        return board.claim_next_ticket(first, 'same-key', project['id'])
    with ThreadPoolExecutor(2) as pool:
        values = list(pool.map(lambda _: select(), range(2)))
    assert all(value['ticket']['id'] == task['id'] for value in values)
    assert sorted(value['retried'] for value in values) == [False, True]
    assert board.get_ticket(first, other['id'])['ticket']['claimant'] is None
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM ticket_events WHERE event='ticket_claimed'").fetchone()[0] == 1


def test_unfinished_expired_claim_blocks_new_selection_across_projects(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    board.claim_next_ticket(first, 'original', project['id'])
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?', (task['id'],))
    another = board.create_project(human, 'Another project', [human['owner_id']], idempotency_key='another-project')
    ready = board.create_ticket(human, another['id'], 'Ready', idempotency_key='ready')
    result = board.claim_next_ticket(first, 'blocked-attempt', another['id'])
    assert result == {'claimed': False, 'reason': 'unfinished_work', 'retried': False}
    assert board.get_ticket(first, ready['id'])['ticket']['claimant'] is None
    expired = board.get_ticket(human, task['id'])['ticket']
    board.update_ticket(human, task['id'], expired['version'], release_claim=True)
    assert board.claim_next_ticket(first, 'blocked-attempt', another['id']) == {**result, 'retried': True}
    assert board.claim_next_ticket(first, 'explicit-new-attempt', another['id'])['ticket']['id'] == ready['id']


def test_pending_request_only_blocks_its_ticket_and_negative_key_stays_pinned(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    selected = board.claim_next_ticket(first, 'first', project['id'])['ticket']
    question = board.request_human_input(first, task['id'], human['owner_id'], 'Which input?', 'question',
                                         expected_version=selected['version'])
    result = board.claim_next_ticket(first, 'during-question', project['id'])
    assert result == {'claimed': False, 'reason': 'waiting_on_human', 'retried': False}
    other = new(space, 'Ready other task', priority='urgent')
    assert board.get_ticket(first, other['id'])['ticket']['claimant'] is None
    board.resolve_human_request(human, question['id'], 'This input.')
    assert board.claim_next_ticket(first, 'during-question', project['id']) == {**result, 'retried': True}
    assert board.claim_next_ticket(first, 'after-human-reconciliation', project['id'])['ticket']['id'] == other['id']


def test_ready_work_can_be_selected_while_an_unrelated_human_request_is_open(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    selected = board.claim_next_ticket(first, 'first', project['id'])['ticket']
    board.request_human_input(first, task['id'], human['owner_id'], 'Which input?', 'question',
                              expected_version=selected['version'])
    other = new(space, 'Ready other task')
    result = board.claim_next_ticket(first, 'while-waiting', project['id'])
    assert result['claimed'] and result['ticket']['id'] == other['id']
    assert board.get_ticket(first, task['id'])['ticket']['waiting_on_human']


def test_lookup_key_is_bound_to_actor_session_device_and_project(space):
    board, human, other, project, first, second, _ = space
    new(space)
    board.claim_next_ticket(first, 'private-key', project['id'])
    assert board.lookup_ticket_selection(second, 'private-key', project['id']) == {'found': False}
    device = board.create_device(other, 'Other owner device', [])
    raw = board.authenticate(device['token'])
    board.register_agent(raw, 'other-owner', 'Other owner', [], 'codex', False)
    assert board.lookup_ticket_selection(board.actor_session(raw, 'other-owner'), 'private-key', project['id']) == {'found': False}
    another = board.create_project(human, 'Other project', [human['owner_id']], idempotency_key='other-project')
    with pytest.raises(BoardError) as error:
        board.lookup_ticket_selection(first, 'private-key', another['id'])
    assert error.value.status == 409
    with pytest.raises(BoardError):
        board.lookup_ticket_selection({**first, 'device_id': device['id']}, 'private-key', project['id'])
    with pytest.raises(BoardError) as error:
        board.lookup_ticket_selection(human, 'private-key', project['id'])
    assert error.value.status == 403


def test_lookup_can_reconcile_with_work_disabled_but_never_ignores_read_revocation(space):
    board, human, _, project, first, *_ = space
    new(space)
    selected = board.claim_next_ticket(first, 'selection', project['id'])
    board.set_policy(human, True, True, True, False, [], [])
    before = snapshot(board)
    result = board.lookup_ticket_selection(first, 'selection', project['id'])
    assert result['ticket']['id'] == selected['ticket']['id']
    assert snapshot(board) == before
    with pytest.raises(BoardError) as error:
        board.claim_next_ticket(first, 'selection', project['id'])
    assert error.value.status == 403
    board.set_policy(human, True, False, True, False, [], [])
    with pytest.raises(BoardError) as error:
        board.lookup_ticket_selection(first, 'selection', project['id'])
    assert error.value.status == 403


def test_negative_lookup_rechecks_project_membership(space):
    board, human, _, project, first, *_ = space
    board.claim_next_ticket(first, 'empty', project['id'])
    with sqlite3.connect(board.db_path) as db:
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?', (project['id'], human['owner_id']))
    for method in (board.lookup_ticket_selection, board.claim_next_ticket):
        with pytest.raises(BoardError) as error:
            method(first, 'empty', project['id'])
        assert error.value.status == 404


def test_lookup_rechecks_profile_scope_and_pause(space):
    board, human, _, project, first, _, device = space
    board.claim_next_ticket(first, 'empty', project['id'])
    profile = board.list_agent_profiles(human)[0]
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[])
    raw = board.authenticate(device['token'])
    board.register_agent(raw, first['session_id'], 'Restricted', [], 'codex', False, 'installation')
    with pytest.raises(BoardError) as error:
        board.lookup_ticket_selection(first, 'empty', project['id'])
    assert error.value.status == 404
    profile = board.get_agent_profile(human, profile['id'])
    board.update_agent_profile(human, profile['id'], profile['version'], status='paused')
    with pytest.raises(BoardError) as error:
        board.lookup_ticket_selection(first, 'empty', project['id'])
    assert error.value.status == 403


def test_lookup_does_not_require_queue_autonomy(space):
    board, human, _, project, first, _, device = space
    board.claim_next_ticket(first, 'empty', project['id'])
    profile = board.list_agent_profiles(human)[0]
    board.update_agent_profile(human, profile['id'], profile['version'],
        policy={**profile['policy'], 'autonomy': 'responses', 'allow_requests': False})
    board.register_agent(board.authenticate(device['token']), first['session_id'], 'Read only', [], 'codex', False, 'installation')
    assert board.lookup_ticket_selection(first, 'empty', project['id'])['reason'] == 'no_ready_ticket'
    with pytest.raises(BoardError) as error:
        board.claim_next_ticket(first, 'empty', project['id'])
    assert error.value.status == 403


def test_other_session_fences_expired_claim_but_can_work_after_human_handoff(space):
    board, human, _, project, first, second, _ = space
    task = new(space)
    selected = board.claim_next_ticket(first, 'original', project['id'])['ticket']
    other = new(space, 'Other ready work')
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?', (task['id'],))
        # Migration-compatible fallback for older stored author records.
        db.execute("UPDATE tickets SET claimant=json_remove(claimant,'$.agent_id') WHERE id=?", (task['id'],))
    assert board.claim_next_ticket(second, 'after-restart', project['id'])['reason'] == 'unfinished_work'
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=99999999999 WHERE id=?', (task['id'],))
    board.request_human_input(first, task['id'], human['owner_id'], 'Question?', 'question', expected_version=selected['version'])
    with sqlite3.connect(board.db_path) as db:
        db.execute("UPDATE human_requests SET requested_by=json_remove(requested_by,'$.agent_id')")
    assert board.claim_next_ticket(second, 'waiting-after-restart', project['id'])['ticket']['id'] == other['id']


def test_another_bound_device_cannot_bypass_profile_work_but_distinct_profile_can(space):
    board, human, _, project, first, _, original_device = space
    profile = board.list_agent_profiles(human)[0]
    remote_device = board.create_device(human, 'Remote device', [])
    board.bind_agent_profile(human, profile['id'], profile['version'], remote_device['id'], 'remote', 'codex')
    board.register_agent(board.authenticate(original_device['token']), first['session_id'], 'First', [], 'codex', True, 'installation')
    remote_raw = board.authenticate(remote_device['token'])
    board.register_agent(remote_raw, 'remote-session', 'Remote', [], 'codex', True, 'remote')
    remote = board.actor_session(remote_raw, 'remote-session')
    new(space)
    other = new(space, 'Second work')
    board.claim_next_ticket(first, 'first', project['id'])
    blocked = board.claim_next_ticket(remote, 'remote', project['id'])
    assert blocked == {'claimed': False, 'reason': 'unfinished_work', 'retried': False}
    separate_device = board.create_device(human, 'Separate worker', [])
    separate_raw = board.authenticate(separate_device['token'])
    board.register_agent(separate_raw, 'separate', 'Separate', [], 'codex', True)
    selected = board.claim_next_ticket(board.actor_session(separate_raw, 'separate'), 'parallel', project['id'])
    assert selected['ticket']['id'] == other['id']


def test_legacy_action_claim_without_agent_id_also_blocks_profile_queue(space):
    board, human, _, project, first, second, _ = space
    board.set_policy(human, True, True, True, True, ['fixture_action'], ['fixture_resource'])
    thread = board.create_thread(human, 'Legacy work', [human['owner_id']], 'legacy-thread')
    task = board.create_task(human, thread['id'], 'Legacy task', 'Fixture only', '',
                            'group', human['owner_id'], action='fixture_action', resource='fixture_resource')
    assert board.claim_task(first, task['id'])['claimed']
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tasks SET lease_expires_at=0 WHERE id=?', (task['id'],))
    ready = new(space, 'Ready project task')
    result = board.claim_next_ticket(second, 'new-queue', project['id'])
    assert result == {'claimed': False, 'reason': 'unfinished_work', 'retried': False}
    assert board.get_ticket(first, ready['id'])['ticket']['claimant'] is None
