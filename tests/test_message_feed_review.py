"""Independent replay/ACK interleavings on temporary boards, no model or network."""
from concurrent.futures import ThreadPoolExecutor

from test_conversation_followups import space
from test_message_feed import ping, ack, feed_row, rejects


def test_read_racing_ack_never_skips_or_clears_the_next_delivery(space):
    board, *_ = space
    actor = space[6][0]
    feed = board.open_message_feed(actor, 'independent-interleaving', limit=1)
    for index in range(5):
        first = ping(space, f'first-{index}')
        old = board.read_message_feed(actor, feed['feed_id'])
        assert [message['id'] for message in old['messages']] == [first['id']]
        second = ping(space, f'second-{index}')
        with ThreadPoolExecutor(2) as pool:
            confirm = pool.submit(ack, board, actor, old)
            read = pool.submit(board.read_message_feed, actor, feed['feed_id'])
            receipt, raced = confirm.result(), read.result()
        assert [m['id'] for m in raced['messages']] in ([first['id']], [second['id']])
        pending = board.read_message_feed(actor, feed['feed_id'])
        assert [m['id'] for m in pending['messages']] == [second['id']]
        state = feed_row(board, feed)
        assert ack(board, actor, old) == receipt
        assert feed_row(board, feed) == state
        ack(board, actor, pending)


def test_even_a_committed_ack_retry_revalidates_owner_read_permission(space):
    board, human, *_ = space
    actor = space[6][0]
    ping(space)
    feed = board.open_message_feed(actor, 'independent-committed-permission')
    delivered = board.read_message_feed(actor, feed['feed_id'])
    ack(board, actor, delivered)
    before = feed_row(board, feed)
    board.set_policy(human, True, False, True, True, [], [])
    rejects(403, ack, board, actor, delivered)
    assert feed_row(board, feed) == before


def test_explicit_profile_refresh_preserves_the_exact_pending_lot(space):
    board, human, _, _, registration, *_ = space
    actor = space[6][0]
    ping(space)
    feed = board.open_message_feed(actor, 'independent-profile-refresh')
    delivery = board.read_message_feed(actor, feed['feed_id'])
    profile = board.get_agent_profile(human, registration['identity']['id'])
    board.update_agent_profile(human, profile['id'], profile['version'], description='Updated purpose')
    rejects(409, board.read_message_feed, actor, feed['feed_id'])
    raw = board.authenticate(space[7]['token'])
    board.register_agent(raw, 'codex', 'Codex', [], 'codex', False, 'default')
    assert board.open_message_feed(actor, 'independent-profile-refresh') == feed
    assert board.read_message_feed(actor, feed['feed_id']) == delivery
    assert ack(board, actor, delivery)['checkpoint'] == 1
