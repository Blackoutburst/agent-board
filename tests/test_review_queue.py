"""Reviews reach an eligible reviewer, stale ones are reminded once, repeated blocks escalate."""
import sqlite3
import time

from test_coordination import new, reject, space  # noqa: F401  (fixture)


def reviewer(space):
    """A separately bound agent profile on its own device."""
    board, human, _, project, *_ = space
    profile = board.create_agent_profile(human, 'Reviewer', project_ids=[project['id']], policy={
        'allow_read': True, 'allow_messages': True, 'allow_requests': True, 'actions': [], 'resources': [],
        'autonomy': 'queue'})
    device = board.create_device(human, 'Review Mac', [])
    current = board.get_agent_profile(human, profile['id'])
    board.bind_agent_profile(human, profile['id'], current['version'], device['id'], 'reviewer', 'claude-code')
    raw = board.authenticate(device['token'])
    board.register_agent(raw, 'reviewer', 'Reviewer', ['python'], 'claude-code', True, 'reviewer')
    return board.actor_session(raw, 'reviewer')


def deliver(board, agent, task):
    claimed = board.claim_ticket(agent, task['id'], task['version'])
    return board.update_ticket(agent, task['id'], claimed['version'], status='review',
                               outcome='Delivered', evidence='Tests', release_claim=True)


def age_review(board, ticket_id, seconds):
    with sqlite3.connect(board.db_path) as db:
        db.execute("UPDATE ticket_events SET created_at=created_at-? WHERE ticket_id=? AND event='review_requested'",
                   (seconds, ticket_id))


def test_review_is_offered_to_other_agents_but_never_its_author(space):
    board, human, _, project, first, second, _ = space
    other = reviewer(space)
    task = deliver(board, first, new(space))
    assert [t['id'] for t in board.get_work(other, project['id'])['review']] == [task['id']]
    # Same persistent identity as the author, even in another session: never offered.
    assert board.get_work(first, project['id'])['review'] == []
    assert board.get_work(second, project['id'])['review'] == []
    offered = board.get_work(other, project['id'])['review'][0]
    assert offered['review_author_agent_id'] and offered['review_since'] <= time.time()


def test_reviewer_claim_keeps_review_status_and_can_accept_or_return(space):
    board, human, _, project, first, second, _ = space
    other = reviewer(space)
    task = deliver(board, first, new(space))
    claimed = board.claim_ticket(second, task['id'], task['version'])
    assert claimed['status'] == 'review'
    assert board.get_work(other, project['id'])['review'] == []  # claimed reviews leave the queue
    done = board.update_ticket(second, task['id'], claimed['version'], status='done',
                               outcome='Accepted', evidence='Reviewed diff and tests')
    assert done['status'] == 'done'
    other = deliver(board, first, new(space, 'Other'))
    claimed = board.claim_ticket(second, other['id'], other['version'])
    returned = board.update_ticket(second, other['id'], claimed['version'], status='todo',
                                   progress='Missing test for the empty case', release_claim=True)
    assert returned['status'] == 'todo' and returned['claimant'] is None


def test_review_queue_respects_required_skills_and_oldest_first(space):
    board, human, _, project, first, second, _ = space
    other = reviewer(space)
    older = deliver(board, first, new(space, 'Older'))
    newer = deliver(board, first, new(space, 'Newer'))
    age_review(board, older['id'], 100)
    assert [t['id'] for t in board.get_work(other, project['id'])['review']] == [older['id'], newer['id']]
    board.update_ticket(human, newer['id'], newer['version'], required_skills=['rust'])
    assert [t['id'] for t in board.get_work(other, project['id'])['review']] == [older['id']]


def test_stale_review_is_reminded_once_per_cycle(space):
    board, human, _, project, first, second, _ = space
    task = deliver(board, first, new(space))
    board.get_work(second, project['id'])
    events = lambda: [e for e in board.list_activity(human, ticket_id=task['id']) if e['event'] == 'review_reminder']
    assert events() == []
    age_review(board, task['id'], 25 * 3600)
    board.get_work(second, project['id'])
    board.get_work(first, project['id'])
    assert len(events()) == 1
    assert board.get_thread(human, task['thread_id'])['messages'][-1]['body'].startswith('Board reminder:')
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM notification_events WHERE kind='ready_for_verification' AND event_key LIKE ?",
                          ('review-reminder:' + task['id'] + '%',)).fetchone()[0] == 1


def test_repeated_agent_blocks_escalate_once_and_stop_automatic_retries(space):
    board, human, _, project, first, *_ = space
    task = new(space)
    for attempt in range(3):
        row = board.get_ticket(human, task['id'])['ticket']
        if row['status'] == 'blocked':
            with sqlite3.connect(board.db_path) as db:  # simulate the recheck having fired
                db.execute("UPDATE tickets SET recheck_at=? WHERE id=?", (time.time() - 1, task['id']))
            board.get_work(first, project['id'])
            row = board.get_ticket(human, task['id'])['ticket']
        claimed = board.claim_ticket(first, task['id'], row['version'])
        blocked = board.update_ticket(first, task['id'], claimed['version'], status='blocked',
                                      blocked_reason=f'Attempt {attempt} failed', recheck_after_seconds=300,
                                      release_claim=True)
    assert blocked['recheck_at'] is None and blocked['unblock_owner'] == 'human'
    assert blocked['block_reasons'] == ['explicit']
    board.get_work(first, project['id'])
    assert board.get_ticket(human, task['id'])['ticket']['status'] == 'blocked'
    assert len([e for e in board.list_activity(human, ticket_id=task['id']) if e['event'] == 'block_escalated']) == 1
    reopened = board.update_ticket(human, task['id'], blocked['version'], status='todo')
    claimed = board.claim_ticket(first, task['id'], reopened['version'])
    again = board.update_ticket(first, task['id'], claimed['version'], status='blocked', blocked_reason='Once more',
                                release_claim=True)
    assert again['recheck_at'] is not None  # a human decision restarts the count
