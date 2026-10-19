"""Stored blocks need a verifiable reason and are released when it clears."""
import sqlite3
import time

import pytest

from test_coordination import new, reject, space  # noqa: F401  (fixture)


def blocked_state(board, ticket_id):
    with sqlite3.connect(board.db_path) as db:
        return db.execute('SELECT status,blocked_reason,unblock_owner,recheck_at,waiting_for_dependencies FROM tickets WHERE id=?',
                          (ticket_id,)).fetchone()


def ghost(board, ticket_id, reason='', recheck_at=None):
    """Write a block the way older releases or agents could leave it."""
    with sqlite3.connect(board.db_path) as db:
        db.execute("UPDATE tickets SET status='blocked',claimant=NULL,lease_expires_at=NULL,blocked_reason=?,recheck_at=? WHERE id=?",
                   (reason, recheck_at, ticket_id))


def test_agent_cannot_store_a_reasonless_block(space):
    board, human, _, _, first, *_ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    reject(board.update_ticket, first, task['id'], claimed['version'], status='blocked', expected_status=400)
    reject(board.update_ticket, first, task['id'], claimed['version'], status='blocked',
           blocked_reason='Waiting for the vendor', recheck_after_seconds=10, expected_status=400)
    before = time.time()
    blocked = board.update_ticket(first, task['id'], claimed['version'], status='blocked',
                                  blocked_reason='Waiting for the vendor', recheck_after_seconds=3600)
    assert blocked['blocked_reason'] == 'Waiting for the vendor' and blocked['block_reasons'] == ['explicit']
    assert before + 3600 <= blocked['recheck_at'] <= time.time() + 3600
    assert blocked['unblock_owner'] == board.get_ticket(human, task['id'])['ticket']['claimant']['agent_id']


def test_agent_block_defaults_to_a_one_day_recheck_and_reasons_only_apply_to_blocks(space):
    board, _, _, _, first, *_ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    reject(board.update_ticket, first, task['id'], claimed['version'], progress='x',
           blocked_reason='Not blocked', expected_status=400)
    blocked = board.update_ticket(first, task['id'], claimed['version'], status='blocked', blocked_reason='Vendor')
    assert 23 * 3600 < blocked['recheck_at'] - time.time() <= 24 * 3600


def test_human_manual_block_is_kept_until_a_human_changes_it(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    blocked = board.update_ticket(human, task['id'], task['version'], status='blocked')
    assert blocked['blocked_reason'].startswith('Blocked manually by') and blocked['recheck_at'] is None
    board.get_work(first, project['id'])
    assert blocked_state(board, task['id'])[0] == 'blocked'
    reopened = board.update_ticket(human, task['id'], blocked['version'], status='todo')
    assert reopened['blocked_reason'] == '' and reopened['unblock_owner'] is None and reopened['recheck_at'] is None


def test_get_work_releases_legacy_ghosts_with_audit_and_message(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    ghost(board, task['id'])
    assert board.get_ticket(human, task['id'])['ticket']['block_reasons'] == []
    ready = board.get_work(first, project['id'])['ready']
    assert [item['id'] for item in ready] == [task['id']]
    assert blocked_state(board, task['id'])[:2] == ('todo', '')
    events = [e for e in board.list_activity(human, ticket_id=task['id']) if e['event'] == 'ticket_unblocked']
    assert len(events) == 1 and events[0]['detail']['previous_reason'] == 'no recorded reason'
    assert board.get_thread(human, task['thread_id'])['messages'][-1]['body'].startswith('Board reevaluation:')
    # Idempotent: a second sweep changes nothing.
    version = board.get_ticket(human, task['id'])['ticket']['version']
    board.get_work(first, project['id'])
    assert board.get_ticket(human, task['id'])['ticket']['version'] == version


def test_expired_explicit_block_is_released_but_an_active_one_is_kept(space):
    board, human, _, project, first, *_ = space
    expired, active = new(space, 'Expired'), new(space, 'Active')
    ghost(board, expired['id'], 'Vendor', time.time() - 1)
    ghost(board, active['id'], 'Vendor', time.time() + 3600)
    board.get_work(first, project['id'])
    assert blocked_state(board, expired['id'])[0] == 'todo'
    assert blocked_state(board, active['id'])[0] == 'blocked'
    detail = [e for e in board.list_activity(human, ticket_id=expired['id']) if e['event'] == 'ticket_unblocked'][0]['detail']
    assert detail['previous_reason'] == 'Vendor'


def test_blocks_with_open_human_requests_or_dependencies_are_never_swept(space):
    board, _, _, project, first, *_ = space
    asked, prerequisite, dependent = new(space, 'Asked'), new(space, 'Prerequisite'), new(space, 'Dependent')
    claimed = board.claim_ticket(first, asked['id'], asked['version'])
    board.request_human_input(first, asked['id'], 'thescreamer', 'Which value?', 'q')
    dependent = board.update_ticket(space[1], dependent['id'], dependent['version'], depends_on=[prerequisite['id']])
    ghost(board, dependent['id'])
    board.get_work(first, project['id'])
    assert blocked_state(board, asked['id'])[0] == 'blocked'
    assert blocked_state(board, dependent['id'])[0] == 'blocked'


def test_dependency_in_review_releases_the_dependent_but_not_its_completion(space):
    board, human, _, project, first, second, _ = space
    child, parent = new(space, 'Child'), new(space, 'Parent')
    parent = board.update_ticket(human, parent['id'], parent['version'], depends_on=[child['id']])
    with sqlite3.connect(board.db_path) as db:
        db.execute("UPDATE tickets SET status='blocked',waiting_for_dependencies=1 WHERE id=?", (parent['id'],))
    work = board.claim_ticket(first, child['id'], child['version'])
    board.update_ticket(first, child['id'], work['version'], status='review', outcome='Delivered', evidence='Tests')
    released = board.get_ticket(human, parent['id'])['ticket']
    assert released['status'] == 'todo' and released['blocked_by'] == []
    assert [item['id'] for item in released['delivered_dependencies']] == [child['id']]
    claimed = board.claim_ticket(second, parent['id'], released['version'])
    reject(board.update_ticket, second, parent['id'], claimed['version'], status='done',
           outcome='Integrated', evidence='Tests', expected_status=409)
    reviewed = board.update_ticket(second, parent['id'], claimed['version'], status='review',
                                   outcome='Integrated', evidence='Tests')
    assert reviewed['status'] == 'review'


def test_human_answer_keeps_other_verifiable_reasons_and_otherwise_resumes_silently(space):
    board, human, other, project, first, *_ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    request = board.request_human_input(first, task['id'], 'thescreamer', 'Which value?', 'answer')
    with sqlite3.connect(board.db_path) as db:
        db.execute("UPDATE tickets SET blocked_reason='Vendor',recheck_at=? WHERE id=?", (time.time() + 3600, task['id']))
    board.resolve_human_request(other, request['id'], 'Use A')
    assert blocked_state(board, task['id'])[0] == 'blocked'
    second = new(space, 'Second')
    claimed = board.claim_ticket(first, second['id'], second['version'])
    request = board.request_human_input(first, second['id'], 'thescreamer', 'Which value?', 'answer-2')
    before = len(board.get_thread(human, second['thread_id'])['messages'])
    board.resolve_human_request(other, request['id'], 'Use B')
    assert blocked_state(board, second['id'])[0] == 'todo'
    assert len(board.get_thread(human, second['thread_id'])['messages']) == before + 1  # only the human answer


def test_released_block_records_its_reason_and_a_recheck(space):
    board, _, _, _, first, *_ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    released = board.release_ticket(first, task['id'], claimed['version'], 'release-blocked',
                                    'Waiting for the platform release', status='blocked')
    status, reason, owner, recheck, _ = blocked_state(board, task['id'])
    assert status == 'blocked' and reason == 'Waiting for the platform release' and owner
    assert 23 * 3600 < recheck - time.time() <= 24 * 3600
    assert released['block_reasons'] == ['explicit']


def test_leaving_blocked_clears_block_metadata(space):
    board, human, _, _, first, *_ = space
    task = new(space)
    claimed = board.claim_ticket(first, task['id'], task['version'])
    blocked = board.update_ticket(first, task['id'], claimed['version'], status='blocked', blocked_reason='Vendor')
    resumed = board.update_ticket(human, task['id'], blocked['version'], status='todo')
    assert (resumed['blocked_reason'], resumed['unblock_owner'], resumed['recheck_at']) == ('', None, None)


def test_mcp_update_ticket_accepts_block_fields(space):
    from app import TicketChanges
    changes = TicketChanges(status='blocked', blocked_reason='Vendor', recheck_after_seconds=3600)
    assert changes.model_dump(exclude_unset=True) == {'status': 'blocked', 'blocked_reason': 'Vendor',
                                                      'recheck_after_seconds': 3600}
    with pytest.raises(Exception):
        TicketChanges(status='blocked', blocked_reason='Vendor', recheck_after_seconds=10)
