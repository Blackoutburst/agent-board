"""Own-task maintenance without bypassing claims, recovery or human gates."""
import sqlite3

import pytest

from test_coordination import space, new, reject, database_snapshot


def release(board, actor, task, key='release', **options):
    return board.release_ticket(actor, task['id'], task['version'], key, 'Waiting for prerequisite', **options)


def test_release_pins_identity_and_restart_can_correct_manual_block(space):
    board, human, _, project, first, second, _ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    progress = board.renew_ticket(first, task['id'], claimed['version'], 'Implementation preserved')
    blocked = release(board, first, progress, status='blocked')
    assert blocked['status'] == 'blocked' and blocked['claimant'] is None
    assert blocked['assignee_agent_id'] == claimed['claimant']['agent_id']
    assert blocked['progress'] == 'Implementation preserved'
    reject(board.claim_ticket, second, task['id'], blocked['version'], expected_status=409)
    ready = release(board, second, blocked, 'correct', status='todo')
    assert ready['ready'] and ready['version'] == blocked['version'] + 1
    assert board.claim_next_ticket(second, 'resume', project['id'])['ticket']['id'] == task['id']
    assert any(e['event'] == 'ticket_released' for e in board.list_activity(human, ticket_id=task['id']))


def test_self_unassignment_and_retry_never_erase_later_human_edit(space):
    board, human, _, _, first, second, _ = space
    task = new(space)
    claim = board.claim_ticket(first, task['id'], task['version'])
    released = release(board, first, claim, unassign_self=True)
    assert released['claimant'] is None and released['assignee_agent_id'] is None
    changed = board.update_ticket(human, task['id'], released['version'], assignee_owner_id='thescreamer')
    before = database_snapshot(board)
    assert release(board, first, claim, unassign_self=True) == changed
    assert database_snapshot(board) == before
    reject(release, board, second, changed, 'new-release', status='todo', expected_status=403)


def test_dependency_and_human_request_survive_status_correction(space):
    board, human, _, _, first, *_ = space
    prerequisite = new(space, 'Prerequisite')
    task = new(space)
    claim = board.claim_ticket(first, task['id'], task['version'])
    request = board.request_human_input(first, task['id'], human['owner_id'], 'Choose value', 'input')
    waiting = board.get_ticket(human, task['id'])['ticket']
    waiting = board.update_ticket(human, task['id'], waiting['version'], depends_on=[prerequisite['id']])
    corrected = release(board, first, waiting, status='todo')
    assert corrected['status'] == 'todo' and corrected['work_state'] == 'waiting_on_human'
    assert corrected['blocked_by'][0]['id'] == prerequisite['id']
    assert corrected['human_requests'][0]['id'] == request['id']
    reject(board.claim_ticket, first, task['id'], corrected['version'], expected_status=409)
    reject(release, board, first, corrected, 'review', status='review', expected_status=409)
    board.resolve_human_request(human, request['id'], 'Chosen')
    still_waiting = board.get_ticket(human, task['id'])['ticket']
    assert still_waiting['work_state'] == 'blocked' and still_waiting['status'] == 'todo'


@pytest.mark.parametrize('expired', [False, True])
def test_other_session_claim_is_fenced_even_for_same_profile(space, expired):
    board, _, _, _, first, second, _ = space
    task = new(space)
    claim = board.claim_ticket(first, task['id'], task['version'])
    if expired:
        with sqlite3.connect(board.db_path) as db:
            db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?', (task['id'],))
    before = database_snapshot(board)
    reject(release, board, second, claim, expected_status=403)
    if expired:
        reject(release, board, first, claim, expected_status=409)
    assert database_snapshot(board) == before


def test_version_conflicts_unowned_tasks_and_done_are_rejected(space):
    board, human, _, _, first, second, _ = space
    task = new(space)
    reject(release, board, first, task, expected_status=403)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    released = release(board, first, claimed, status='blocked')
    reject(release, board, second, claimed, 'stale', status='todo', expected_status=409)
    before = database_snapshot(board)
    reject(release, board, first, released, 'finish', status='done', expected_status=400)
    assert database_snapshot(board) == before
    done = board.update_ticket(human, task['id'], released['version'], status='done', outcome='Done', evidence='Tested')
    reject(release, board, second, done, 'reopen', status='todo', expected_status=409)


@pytest.mark.parametrize('restriction', ['paused', 'scope', 'permission', 'device'])
def test_current_permission_and_scope_gate_maintenance(space, restriction):
    board, human, _, project, first, _, device = space
    task = new(space)
    claim = board.claim_ticket(first, task['id'], task['version'])
    released = release(board, first, claim, status='blocked')
    profile = board.list_agent_profiles(human)[0]
    if restriction == 'paused':
        board.update_agent_profile(human, profile['id'], profile['version'], status='paused')
    elif restriction == 'device':
        board.revoke_device(human, device['id'])
    else:
        changes = {'project_ids': []} if restriction == 'scope' else {'policy': {**profile['policy'], 'allow_requests': False}}
        board.update_agent_profile(human, profile['id'], profile['version'], **changes)
        # Inspect/re-register the changed configuration before testing its scope.
        board.register_agent(board.authenticate(device['token']), first['session_id'], 'Changed', ['python'], 'codex', True, 'installation')
    before = database_snapshot(board)
    expected = {'scope': 404, 'device': 401}.get(restriction, 403)
    reject(release, board, first, released, 'restricted', status='todo', expected_status=expected)
    assert database_snapshot(board) == before


def test_key_reuse_with_changed_action_is_rejected(space):
    board, _, _, _, first, *_ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    released = release(board, first, claimed, status='blocked')
    before = database_snapshot(board)
    reject(release, board, first, released, status='todo', expected_status=409)
    assert database_snapshot(board) == before
