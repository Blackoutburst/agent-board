"""Owner invitations grant one standalone thread without expanding project scope."""
import pytest

from mcp_views import MCPViews
from test_companion_workflow import home, denied  # noqa: F401
from test_agent_profiles_security import guarded  # noqa: F401


def standalone(home):
    return home['board'].create_thread(home['humans']['alice'], 'Standalone discussion',
                                       ['alice', 'bob'], 'standalone-invitation')


def test_owner_picker_offers_scoped_agents_without_granting_access(home):
    board = home['board']; thread = standalone(home)
    target = 'agent:' + home['worker']['profile']['id']
    assert target in {item['id'] for item in board.list_mention_targets(home['humans']['alice'], thread['id'])}
    assert target not in {item['id'] for item in board.list_mention_targets(home['humans']['bob'], thread['id'])}
    denied(board.get_thread, home['worker']['actor'], thread['id'], statuses=(404,))


@pytest.mark.parametrize('companion', [False, True])
@pytest.mark.parametrize('typed', [False, True])
def test_owner_mention_delivers_and_unlocks_message_thread_and_text(home, companion, typed):
    board = home['board']; thread = standalone(home); worker = home['worker']
    author = home['companion']['actor'] if companion else home['humans']['alice']
    body = '@Coder Please inspect this standalone discussion' if typed else 'Please inspect this standalone discussion'
    arguments = dict(mentions=[] if typed else ['agent:' + worker['profile']['id']],
                     idempotency_key='one-owner-invitation')
    mention = board.post_message(author, thread['id'], body, **arguments)
    assert board.post_message(author, thread['id'], body, **arguments)['id'] == mention['id']
    with board._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='standalone_agent_invited'").fetchone()[0] == 1
    # The decision survives reopening; it is not an in-memory read exception.
    board = type(board)(board.data_dir, base_url=board.base_url)
    assert [message['id'] for message in board.read_inbox(worker['actor'])['messages']] == [mention['id']]
    assert board.get_thread(worker['actor'], thread['id'])['messages'][0]['id'] == mention['id']
    assert board.get_message(worker['actor'], thread['id'], mention['id'])['message']['body'] == mention['body']
    text = MCPViews(board).get_text(worker['actor'], 'message', mention['id'], '/body')
    assert text['text'] == mention['body']
    reply = board.post_message(worker['actor'], thread['id'], 'Reply after the owner invitation', reply_to=mention['id'])
    assert reply['body'] == 'Reply after the owner invitation'
    denied(board.get_thread, home['second']['actor'], thread['id'], statuses=(404,))


def test_peer_and_ordinary_agent_cannot_invite_an_owner_scoped_agent(home):
    board = home['board']; thread = standalone(home); target = 'agent:' + home['worker']['profile']['id']
    device = board.create_device(home['humans']['alice'], 'Unscoped helper', [])
    raw = board.authenticate(device['token'])
    board.register_agent(raw, 'unscoped-helper', 'Unscoped helper', [], 'codex', False, 'default')
    ordinary = board.actor_session(raw, 'unscoped-helper')
    for author in (home['humans']['bob'], ordinary):
        denied(board.post_message, author, thread['id'], 'Unauthorized invitation', mentions=[target], statuses=(400,))
    denied(board.get_thread, home['worker']['actor'], thread['id'], statuses=(404,))


def test_invitation_does_not_cross_threads_or_survive_owner_membership_removal(home):
    board = home['board']; thread = standalone(home); worker = home['worker']
    mention = board.post_message(home['humans']['alice'], thread['id'], 'Owner invitation',
                                  mentions=['agent:' + worker['profile']['id']])
    other = board.create_thread(home['humans']['alice'], 'Not invited', ['alice'], 'other-standalone')
    denied(board.get_thread, worker['actor'], other['id'], statuses=(404,))
    with board._connection() as db:
        db.execute('DELETE FROM thread_members WHERE thread_id=? AND owner_id=?', (thread['id'], 'alice'))
    denied(board.get_thread, worker['actor'], thread['id'], statuses=(404,))
    denied(board.get_message, worker['actor'], thread['id'], mention['id'], statuses=(404,))
    assert board.read_inbox(worker['actor'])['messages'] == []


def test_prior_owner_mentions_do_not_override_revoked_ticket_project_scope(home):
    board = home['board']; human = home['humans']['alice']; worker = home['worker']
    profile = board.get_agent_profile(human, worker['profile']['id'])
    profile = board.update_agent_profile(human, profile['id'], profile['version'],
                                         project_ids=[home['shared']['id'], home['private']['id']])
    board.register_agent(worker['raw'], worker['actor']['session_id'], 'Coder', ['python'], 'codex', True, 'Coder')
    ticket = board.create_ticket(human, home['private']['id'], 'Later excluded project')
    mention = board.post_message(human, ticket['thread_id'], 'Earlier valid mention', mentions=['agent:' + profile['id']])
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[home['shared']['id']])
    board.register_agent(worker['raw'], worker['actor']['session_id'], 'Coder', ['python'], 'codex', True, 'Coder')
    denied(board.get_thread, worker['actor'], ticket['thread_id'], statuses=(404,))
    denied(board.get_message, worker['actor'], ticket['thread_id'], mention['id'], statuses=(404,))
    assert board.read_inbox(worker['actor'])['messages'] == []


def test_companion_without_request_permission_cannot_grant_a_new_invitation(home):
    board = home['board']; thread = standalone(home); target = 'agent:' + home['worker']['profile']['id']
    board.set_policy(home['humans']['alice'], True, True, True, False, [], [])
    assert target not in {item['id'] for item in board.list_mention_targets(home['companion']['actor'], thread['id'])}
    denied(board.post_message, home['companion']['actor'], thread['id'], 'Cannot dispatch an invitation',
           mentions=[target], statuses=(400,))


@pytest.mark.parametrize('peer_author', [False, True])
def test_old_self_or_peer_mentions_do_not_become_owner_invitations_after_narrowing(home, peer_author):
    board = home['board']; human = home['humans']['alice']; thread = standalone(home)
    device = board.create_device(human, 'Old unscoped fixture', [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'old-unscoped', 'Old unscoped fixture', [], 'codex', False, 'default')
    actor = board.actor_session(raw, 'old-unscoped')
    profile = registration['profile']
    author = home['humans']['bob'] if peer_author else actor
    board.post_message(author, thread['id'], 'Earlier mention without owner invitation', mentions=['agent:' + profile['id']])
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[home['shared']['id']])
    board.register_agent(raw, 'old-unscoped', 'Old unscoped fixture', [], 'codex', False, 'default')
    denied(board.get_thread, actor, thread['id'], statuses=(404,))
    assert board.read_inbox(actor)['messages'] == []


def test_invitation_does_not_authorize_legacy_task_work_outside_project_scope(guarded):
    s = guarded; board = s['board']; human = s['alice']; actor = s['actor']
    thread = board.create_thread(human, 'Conversation access only', ['alice'], 'legacy-boundary')
    legacy = board.create_task(human, thread['id'], 'Standalone legacy work', 'Inspect synthetic state', '',
                               'group', 'alice', action='inspect', resource='fixture:read')
    board.post_message(human, thread['id'], 'Invitation to discuss, without new work scope',
                         mentions=['agent:' + s['profile']['id']])
    assert board.get_thread(actor, thread['id'])['thread']['id'] == thread['id']
    denied(board.claim_task, actor, legacy['id'])
    denied(board.create_task, actor, thread['id'], 'Outside work scope', 'Do not create', '',
           'group', 'alice', action='inspect', resource='fixture:read')
    allowed = board.create_task(human, s['ticket']['thread_id'], 'Allowed legacy work', 'Inspect scoped state', '',
                                'group', 'alice', action='inspect', resource='fixture:read')
    assert board.claim_task(actor, allowed['id'])['claimed']


@pytest.mark.parametrize('notification', ['reply', 'companion-without-requests'])
def test_historical_notifications_never_become_invitations_after_scope_narrowing(home, notification):
    board = home['board']; human = home['humans']['alice']; thread = standalone(home)
    device = board.create_device(human, 'Historical notification fixture', [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'historical-unscoped', 'Historical fixture', [], 'codex', False, 'default')
    actor = board.actor_session(raw, 'historical-unscoped'); profile = registration['profile']
    target = 'agent:' + profile['id']
    if notification == 'reply':
        original = board.post_message(actor, thread['id'], 'Earlier worker message')
        message = board.post_message(human, thread['id'], 'Acknowledged.', reply_to=original['id'])
    else:
        board.set_policy(human, True, True, True, False, [], [])
        message = board.post_message(home['companion']['actor'], thread['id'], 'Ordinary notification', mentions=[target])
    assert target in message['mentions']
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[home['shared']['id']])
    board.register_agent(raw, 'historical-unscoped', 'Historical fixture', [], 'codex', False, 'default')
    denied(board.get_thread, actor, thread['id'], statuses=(404,))
    denied(board.get_message, actor, thread['id'], message['id'], statuses=(404,))
    denied(MCPViews(board).get_text, actor, 'message', message['id'], '/body', statuses=(404,))
    assert board.read_inbox(actor)['messages'] == []
