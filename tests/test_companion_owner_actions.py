"""Owner status decisions remain distinct from worker execution and delivery."""
import json
import time

import pytest

from test_companion_workflow import home, task, denied, last_audit, assert_companion_author  # noqa: F401
from test_companion_workflow_api import companion_browser, call, ticket, assert_author  # noqa: F401
from test_api import application  # noqa: F401


@pytest.mark.parametrize('claimed', [False, True])
def test_companion_sets_and_updates_owner_hold_without_worker_escalation(home, claimed):
    board = home['board']; actor = home['companion']['actor']
    created = task(home)
    current = board.claim_ticket(home['worker']['actor'], created['id'], created['version']) if claimed else created
    for attempt in range(4):
        current = board.update_ticket(actor, current['id'], current['version'], status='blocked',
            blocked_reason=f'Owner keeps this paused {attempt}', recheck_after_seconds=300,
            unblock_owner='alice', priority='high', title='Owner decision')
        assert current['status'] == 'blocked' and current['unblock_owner'] == 'alice'
        assert current['recheck_at'] is not None
        assert bool(current['claimant']) == claimed
    with board._tx() as db:
        db.execute('UPDATE tickets SET recheck_at=? WHERE id=?', (time.time()-1, current['id']))
        count = db.execute("SELECT COUNT(*) FROM audit WHERE event='agent_block_recorded' AND json_extract(detail,'$.ticket_id')=?", (current['id'],)).fetchone()[0]
    assert count == 0
    board.get_work(home['worker']['actor'])
    held = board.get_ticket(home['humans']['alice'], current['id'])['ticket']
    assert held['status'] == 'blocked' and not held['ready']
    audit, detail = last_audit(board, 'ticket_owner_hold', current['id'])
    assert audit['companion_for'] == 'alice' and detail['active']
    assert_companion_author(detail['holder'], home)
    assert held['outcome'] == held['evidence'] == ''


@pytest.mark.parametrize('release', [False, True])
def test_companion_reopens_claimed_block_with_explicit_release_control(home, release):
    board = home['board']; actor = home['companion']['actor']
    created = task(home)
    claimed = board.claim_ticket(home['worker']['actor'], created['id'], created['version'])
    blocked = board.update_ticket(home['humans']['alice'], created['id'], claimed['version'], status='blocked')
    result = board.update_ticket(actor, created['id'], blocked['version'], status='todo', release_claim=release)
    assert result['status'] == 'todo' and result['blocked_reason'] == ''
    assert bool(result['claimant']) == (not release)
    assert (result['lease_expires_at'] is None) == release
    denied(board.claim_ticket, actor, result['id'], result['version'])


def test_companion_can_block_without_reason_like_owner_and_reassign(home):
    board = home['board']; actor = home['companion']['actor']; created = task(home)
    held = board.update_ticket(actor, created['id'], created['version'], status='blocked')
    assert held['blocked_reason'] == 'Blocked manually by Alice'
    assert held['recheck_at'] is None
    changed = board.update_ticket(actor, created['id'], held['version'],
        blocked_reason='Wait for owner', assignee_agent_id=home['peer']['profile']['id'],
        assignee_owner_id='bob', priority='urgent', description='Owner clarified scope')
    assert changed['status'] == 'blocked' and changed['priority'] == 'urgent'
    assert changed['assignee_owner_id'] == 'bob' and changed['claimant'] is None
    assert changed['description'] == 'Owner clarified scope'


def test_companion_release_can_set_owner_hold_without_worker_timer(home):
    board = home['board']; actor = home['companion']['actor']; created = task(home)
    claimed = board.claim_ticket(home['worker']['actor'], created['id'], created['version'])
    held = board.release_ticket(actor, created['id'], claimed['version'],
        'owner-release-with-hold', 'Owner says leave this paused', status='blocked')
    assert held['status'] == 'blocked' and held['claimant'] is None
    assert held['blocked_reason'] == 'Owner says leave this paused'
    assert held['unblock_owner'] == 'alice' and held['recheck_at'] is None
    audit, detail = last_audit(board, 'ticket_owner_hold', created['id'])
    assert audit['companion_for'] == 'alice' and detail['active']
    assert board.release_ticket(actor, created['id'], claimed['version'],
        'owner-release-with-hold', 'Owner says leave this paused', status='blocked') == held


def test_companion_can_edit_unblock_owner_without_resetting_hold_date(home):
    board = home['board']; actor = home['companion']['actor']; created = task(home)
    held = board.update_ticket(actor, created['id'], created['version'], status='blocked',
        blocked_reason='Wait for a project member', recheck_after_seconds=300)
    changed = board.update_ticket(actor, created['id'], held['version'], unblock_owner='bob')
    assert changed['unblock_owner'] == 'bob' and changed['recheck_at'] == held['recheck_at']
    assert changed['status'] == 'blocked'


def test_companion_can_manage_archived_project_ticket_like_owner(home):
    board = home['board']; actor = home['companion']['actor']; created = task(home)
    project = home['shared']
    board.update_project(home['humans']['alice'], project['id'], project['version'], archived=True)
    changed = board.update_ticket(actor, created['id'], created['version'], status='blocked',
        blocked_reason='Owner keeps archived work paused', priority='low')
    assert changed['status'] == 'blocked' and changed['priority'] == 'low'


@pytest.mark.parametrize('revocation', ['owner_disabled', 'requests_revoked', 'profile_paused', 'profile_changed'])
def test_companion_status_action_rechecks_current_authority(home, revocation):
    board = home['board']; human = home['humans']['alice']; actor = home['companion']['actor']
    created = task(home)
    if revocation == 'owner_disabled':
        board.set_policy(human, False, True, True, True, [], [])
    elif revocation == 'requests_revoked':
        board.set_policy(human, True, True, True, False, [], [])
    else:
        profile = board.get_agent_profile(human, home['companion']['profile']['id'])
        changes = {'status': 'paused'} if revocation == 'profile_paused' else {'instructions': 'Changed'}
        board.update_agent_profile(human, profile['id'], profile['version'], **changes)
    denied(board.update_ticket, actor, created['id'], created['version'], status='blocked', statuses=(403, 409))
    assert board.get_ticket(human, created['id'])['ticket']['version'] == created['version']


def test_companion_status_actions_preserve_owner_membership_and_ignore_spoofing(home):
    board = home['board']; human = home['humans']['alice']; actor = home['companion']['actor']
    outside = board.create_ticket(home['humans']['outsider'], home['outside']['id'], 'Outside')
    denied(board.update_ticket, actor, outside['id'], outside['version'], status='blocked', statuses=(404,))
    created = task(home)
    spoof = dict(home['worker']['actor'], companion_for='alice', acts_for='alice', profile_kind='companion')
    denied(board.update_ticket, spoof, created['id'], created['version'], status='blocked')
    assert board.get_ticket(human, created['id'])['ticket']['status'] == 'todo'


@pytest.mark.parametrize('resolution', ['superseded', 'wont_do'])
def test_companion_records_unworked_owner_closure_without_verification(home, resolution):
    board = home['board']; actor = home['companion']['actor']; created = task(home)
    claimed = board.claim_ticket(home['worker']['actor'], created['id'], created['version'])
    closed = board.update_ticket(actor, created['id'], claimed['version'], status='done',
        resolution=resolution, outcome='Owner chose a different approach')
    assert closed['resolution'] == resolution and closed['claimant'] is None
    assert closed['evidence'].startswith('No verification:')
    denied(board.update_ticket, actor, created['id'], closed['version'], resolution='completed')
    reopened = board.update_ticket(actor, created['id'], closed['version'], status='todo')
    assert reopened['outcome'] == reopened['evidence'] == reopened['resolution'] == ''


def test_companion_owner_hold_through_mcp_keeps_attribution_and_worker_fences(companion_browser):
    home = companion_browser; board = home['board']; created = ticket(home)
    held = call(home, 'update_ticket', {'ticket_id': created['id'], 'expected_version': created['version'],
        'changes': {'status': 'blocked', 'blocked_reason': 'Owner hold', 'recheck_after_seconds': 300}, 'view': 'full'})
    with board._tx() as db:
        db.execute('UPDATE tickets SET recheck_at=? WHERE id=?', (time.time()-1, created['id']))
    call(home, 'get_work', {'project_id': home['project']['id'], 'view': 'full'})
    assert board.get_ticket(home['human'], created['id'])['ticket']['status'] == 'blocked'
    reopened = call(home, 'update_ticket', {'ticket_id': created['id'], 'expected_version': held['version'],
        'changes': {'status': 'todo', 'priority': 'high', 'assignee_agent_id': home['second']['profile']['id']}, 'view': 'full'})
    assert reopened['status'] == 'todo' and reopened['assignee_agent_id'] == home['second']['profile']['id']
    for changes in ({'status': 'in_progress'}, {'status': 'review'}, {'progress': 'Worker activity'},
                    {'status': 'done'}, {'status': 'done', 'resolution': 'wont_do', 'outcome': 'Reason', 'evidence': 'Fake proof'}):
        call(home, 'update_ticket', {'ticket_id': created['id'], 'expected_version': reopened['version'], 'changes': changes}, error=True)
    call(home, 'claim_next_ticket', {'idempotency_key': 'owner-actions-cannot-claim'}, error=True)
    with board._connection() as db:
        row = db.execute("SELECT actor FROM audit WHERE event='ticket_updated' ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(row['actor'])['companion_for'] == 'blackoutburst'
    event = next(event for event in board.list_activity(home['human'], ticket_id=created['id'])
                 if event['event'] == 'ticket_updated')
    assert_author(event['actor'], home)
    closed = call(home, 'update_ticket', {'ticket_id': created['id'], 'expected_version': reopened['version'],
        'changes': {'status': 'done', 'resolution': 'wont_do', 'outcome': 'Owner cancelled this scope'}, 'view': 'full'})
    assert closed['resolution'] == 'wont_do' and closed['evidence'].startswith('No verification:')
