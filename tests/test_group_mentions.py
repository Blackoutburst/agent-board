"""Group recipients are resolved once, under the posting actor's live scope."""
import pytest

from test_companion_workflow import home, denied  # noqa: F401


def standalone(home):
    return home['board'].create_thread(home['humans']['alice'], 'Group conversation',
                                       ['alice', 'bob'], 'groups')


def test_picker_offers_both_groups_with_hints(home):
    targets = home['board'].list_mention_targets(home['humans']['alice'], standalone(home)['id'])
    groups = {target['id']: target for target in targets if target['kind'] == 'group'}
    assert groups['group:human']['name'] == 'human'
    assert groups['group:human']['hint'] == 'everyone human here'
    assert groups['group:agent']['name'] == 'agent'
    assert 'every agent here' in groups['group:agent']['hint']


@pytest.mark.parametrize('selected', [False, True])
def test_human_group_expands_and_excludes_only_the_human_author(home, selected):
    board = home['board']; thread = standalone(home)
    message = board.post_message(home['humans']['alice'], thread['id'],
                                  'Hello everyone' if selected else '@human Hello everyone',
                                  mentions=['group:human'] if selected else [])
    assert message['mentions'] == ['bob']
    assert [item['id'] for item in board.read_inbox(home['humans']['bob'])['messages']] == [message['id']]
    worker = home['worker']; board.post_message(home['humans']['alice'], thread['id'], 'Invite worker',
                                                mentions=['agent:' + worker['profile']['id']])
    reply = board.post_message(worker['actor'], thread['id'], '@human A worker includes its owner')
    assert set(reply['mentions']) == {'alice', 'bob'}


def test_agent_group_invites_only_eligible_agents_and_delivers_to_companion(home):
    board = home['board']; thread = standalone(home)
    message = board.post_message(home['humans']['alice'], thread['id'], '@agent Please review',
                                  idempotency_key='group-ping')
    expected = {'agent:' + home[key]['profile']['id'] for key in ('worker', 'second', 'companion')}
    assert set(message['mentions']) == expected
    assert all(target['kind'] == 'agent' for target in message['mention_targets'])
    for key in ('worker', 'second', 'companion'):
        assert message['id'] in {item['id'] for item in board.read_inbox(home[key]['actor'])['messages']}
    denied(board.get_thread, home['peer']['actor'], thread['id'], statuses=(404,))
    worker = home['worker']
    reply = board.post_message(worker['actor'], thread['id'], '@agent Please acknowledge')
    assert set(reply['mentions']) == expected - {'agent:' + worker['profile']['id']}
    assert board.post_message(home['humans']['alice'], thread['id'], '@agent Please review',
                                idempotency_key='group-ping')['id'] == message['id']


def test_post_time_state_skips_paused_archived_and_foreign_project_agents(home):
    board = home['board']; human = home['humans']['alice']; thread = standalone(home)
    board.list_mention_targets(human, thread['id'])  # stale picker does not grant a recipient
    for key, status in (('worker', 'paused'), ('second', 'archived')):
        profile = board.get_agent_profile(human, home[key]['profile']['id'])
        board.update_agent_profile(human, profile['id'], profile['version'], status=status)
    message = board.post_message(human, thread['id'], 'Selected earlier', mentions=['group:agent'])
    assert message['mentions'] == ['agent:' + home['companion']['profile']['id']]
    ticket = board.create_ticket(human, home['shared']['id'], 'Project boundary')
    message = board.post_message(human, ticket['thread_id'], '@agent Project-only ping')
    assert message['mentions'] == ['agent:' + home['peer']['profile']['id']]


def test_group_cannot_bypass_companion_request_permission(home):
    board = home['board']; thread = standalone(home)
    board.set_policy(home['humans']['alice'], True, True, True, False, [], [])
    message = board.post_message(home['companion']['actor'], thread['id'], '@agent No invitations')
    assert message['mentions'] == []
    denied(board.get_thread, home['worker']['actor'], thread['id'], statuses=(404,))


def test_group_tokens_win_identical_names_but_preserve_longer_named_mentions(home):
    board = home['board']; human = home['humans']['alice']; thread = standalone(home)
    worker = board.get_agent_profile(human, home['worker']['profile']['id'])
    board.update_agent_profile(human, worker['id'], worker['version'], name='human')
    message = board.post_message(human, thread['id'], '@human Reserved group token')
    assert message['mentions'] == ['bob']
    explicit = board.post_message(human, thread['id'], 'Individual named human', mentions=['agent:' + worker['id']])
    assert explicit['mentions'] == ['agent:' + worker['id']]
    profile = board.create_agent_profile(human, 'agent helper', project_ids=[], idempotency_key='long-name')
    message = board.post_message(human, thread['id'], '@agent helper This is one individual')
    assert message['mentions'] == ['agent:' + profile['id']]
    message = board.post_message(human, thread['id'], 'email@agent @agents @agentic @@agent')
    assert message['mentions'] == []


def test_agent_group_cap_refuses_atomically_and_empty_groups_are_allowed(home):
    board = home['board']; human = home['humans']['alice']; thread = standalone(home)
    for index in range(47):
        board.create_agent_profile(human, 'Fleet ' + str(index), project_ids=[], idempotency_key='fleet-' + str(index))
    accepted = board.post_message(human, thread['id'], '@agent Exactly fifty', idempotency_key='fifty')
    assert len(accepted['mentions']) == 50
    board.create_agent_profile(human, 'One more', project_ids=[], idempotency_key='fifty-one')
    with board._connection() as db:
        before = db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        grants = db.execute("SELECT COUNT(*) FROM audit WHERE event='standalone_agent_invited'").fetchone()[0]
    denied(board.post_message, human, thread['id'], '@agent Too many', idempotency_key='over-cap', statuses=(400,))
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == before
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='standalone_agent_invited'").fetchone()[0] == grants
    solo = board.create_thread(home['humans']['outsider'], 'Empty group', ['outsider'], 'solo')
    assert board.post_message(home['humans']['outsider'], solo['id'], '@human @agent')['mentions'] == []
