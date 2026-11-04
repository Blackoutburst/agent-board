"""#56: a new session with a multi-day backlog still sees a new human mention first."""
import pytest

from board import Board
from mcp_views import BUDGET, size


@pytest.fixture
def backlog(tmp_path):
    b = Board(tmp_path / 'data')
    humans = [{'kind': 'human', 'owner_id': owner} for owner in ('blackoutburst', 'thescreamer')]
    for h in humans:
        b.ensure_owner(h['owner_id'], h['owner_id'].title())
        b.set_policy(h, True, True, True, True, [], [])
    human, other = humans
    worker_device = b.create_device(human, 'PC', [])
    worker_raw = b.authenticate(worker_device['token'])
    b.register_agent(worker_raw, 'worker', 'Worker', [], 'codex', False, 'default')
    worker = b.actor_session(worker_raw, 'worker')
    server_device = b.create_device(human, 'Server', [])
    server_raw = b.authenticate(server_device['token'])
    server = b.register_agent(server_raw, 'server-1', 'Server', [], 'claude-code', False, 'default')
    first = b.actor_session(server_raw, 'server-1')
    threads = [b.create_thread(human, f'Ticket {i}', ['blackoutburst', 'thescreamer'], f't{i}') for i in range(6)]

    def new_session(name):
        again = b.register_agent(server_raw, name, 'Server', [], 'claude-code', False, 'default')
        assert again['identity']['id'] == server['identity']['id']
        return b.actor_session(server_raw, name)
    return b, human, other, worker, first, new_session, 'agent:' + server['identity']['id'], threads


def drain(b, actor, cursor=None, **kwargs):
    page = b.read_inbox(actor, cursor=cursor, **kwargs)
    first, ids = page, []
    while page['messages']:
        ids.extend(m['id'] for m in page['messages'])
        page = b.read_inbox(actor, cursor=page['cursor'], **kwargs)
    return first, ids


@pytest.mark.parametrize('compact', [True, False])
def test_new_human_mention_is_on_the_first_page_of_a_fresh_session(backlog, compact):
    b, h, o, worker, first, new_session, target, threads = backlog
    old = [b.post_message(worker, threads[i % 6]['id'], 'handoff %d ' % i + 'x' * 1500, mentions=[target],
                          idempotency_key=f'old{i}')['id'] for i in range(40)]
    ping = b.post_message(h, threads[2]['id'], 'you can close the task', mentions=[target], idempotency_key='ping')
    fresh = new_session('server-2')
    page = b.read_inbox(fresh, limit=4, mcp_compact=compact)
    assert [m['id'] for m in page['messages']] == old[:4]
    assert page['priority'][0]['id'] == ping['id']
    assert page['backlog']['unread'] == 41 and page['backlog']['remaining_after_page'] == 37
    assert page['backlog']['oldest_at'] <= page['backlog']['newest_at']
    if compact:
        assert size(page) <= BUDGET
    # Priority is a preview: acknowledgement still follows the cursor, so draining delivers everything once.
    _, ids = drain(b, fresh, cursor=page['cursor'], mcp_compact=compact)
    assert ids == old[4:] + [ping['id']]
    assert b.read_inbox(fresh, mcp_compact=compact)['messages'] == []


def test_messages_the_identity_already_answered_are_not_replayed(backlog):
    b, h, o, worker, first, new_session, target, threads = backlog
    answered = b.post_message(worker, threads[0]['id'], 'please review', mentions=[target], idempotency_key='a')
    other_thread = b.post_message(worker, threads[1]['id'], 'please merge', mentions=[target], idempotency_key='b')
    # An earlier session of the same identity replied on the thread, without ever reading its inbox.
    b.post_message(first, threads[0]['id'], 'reviewed and merged', reply_to=answered['id'], idempotency_key='r')
    follow_up = b.post_message(worker, threads[0]['id'], 'thanks, one more question', mentions=[target], idempotency_key='c')
    _, ids = drain(b, new_session('server-2'))
    assert ids == [other_thread['id'], follow_up['id']]
    # The same rule holds for the worker: it already posted after the reply addressed to it.
    assert b.read_inbox(worker)['messages'] == []
    late = b.post_message(first, threads[1]['id'], 'merged too', reply_to=other_thread['id'], idempotency_key='r2')
    assert [m['id'] for m in b.read_inbox(worker)['messages']] == [late['id']]


def test_reply_to_the_agents_own_message_is_priority(backlog):
    b, h, o, worker, first, new_session, target, threads = backlog
    mine = b.post_message(first, threads[3]['id'], 'shipped, see evidence', idempotency_key='mine')
    for i in range(10):
        b.post_message(worker, threads[4]['id'], f'fyi {i}', mentions=[target], idempotency_key=f'fyi{i}')
    reply = b.post_message(worker, threads[3]['id'], 'found a regression', reply_to=mine['id'], idempotency_key='reg')
    page = b.read_inbox(new_session('server-2'), limit=3, mcp_compact=True)
    assert [m['id'] for m in page['priority']] == [reply['id']]
    assert page['backlog']['unread'] == 11


def test_small_inbox_and_cursor_pages_keep_the_old_shape(backlog):
    b, h, o, worker, first, new_session, target, threads = backlog
    b.post_message(h, threads[0]['id'], 'hello', mentions=[target], idempotency_key='one')
    page = b.read_inbox(first, mcp_compact=True)
    assert set(page) == {'messages', 'cursor', 'session'}
    for i in range(6):
        b.post_message(h, threads[1]['id'], f'more {i}', mentions=[target], idempotency_key=f'more{i}')
    page = b.read_inbox(first, cursor=page['cursor'], limit=2, mcp_compact=True)
    # Incremental cursor polling: the backlog count is reported, the cursorless-only priority preview is not.
    assert 'priority' not in page and page['backlog']['remaining_after_page'] == 4
    _, ids = drain(b, first, cursor=page['cursor'], limit=2, mcp_compact=True)
    assert len(ids) == 4
