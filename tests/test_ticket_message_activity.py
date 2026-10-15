"""Message activity shares message transaction/idempotence and existing scope."""
import json

import pytest

from board import Board, BoardError


@pytest.fixture
def messages_space(tmp_path):
    board = Board(tmp_path / 'board', base_url='https://board.test')
    for owner in ('alice', 'bob', 'outsider'):
        board.ensure_owner(owner, owner.title())
    human = {'kind': 'human', 'owner_id': 'alice'}
    other = {'kind': 'human', 'owner_id': 'bob'}
    board.set_policy(human, True, True, True, True, [], [])
    project = board.create_project(human, 'Shared', ['alice', 'bob'], idempotency_key='project')
    ticket = board.create_ticket(human, project['id'], 'Task', idempotency_key='ticket')
    device = board.create_device(human, 'Test device', [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'test-session', 'Test agent', [], 'codex', False, 'test-install')
    agent = board.actor_session(raw, 'test-session')
    return board, human, other, project, ticket, agent, device, registration


def message_events(board, actor, ticket):
    return [event for event in board.list_activity(actor, ticket_id=ticket['id'], order='asc')
            if event['event'] == 'ticket_message_posted']


@pytest.mark.parametrize('kind', ['human', 'agent'])
def test_new_message_emits_one_reference_event_with_actual_author(messages_space, kind):
    board, human, other, project, ticket, agent, device, registration = messages_space
    actor = human if kind == 'human' else agent
    message = board.post_message(actor, ticket['thread_id'], 'PRIVATE_BODY_漢字', idempotency_key='post')
    events = message_events(board, other, ticket)
    assert len(events) == 1
    event = events[0]
    assert event['ticket_id'] == ticket['id']
    assert event['project_id'] == project['id']
    assert event['detail'] == {'ticket_id': ticket['id'], 'thread_id': ticket['thread_id'], 'message_id': message['id']}
    assert event['actor'] == message['author']
    assert event['actor']['kind'] == kind
    assert event['actor']['owner_id'] == human['owner_id']
    if kind == 'agent':
        assert event['actor']['agent_id'] == registration['identity']['id']
        assert event['actor']['device_id'] == device['id']
        assert event['actor']['session_id'] == 'test-session'
    assert 'PRIVATE_BODY' not in json.dumps(event)
    current = board.get_ticket(human, ticket['id'])['ticket']
    assert (current['version'], current['status'], current['claimant']) == (ticket['version'], 'todo', None)


def test_same_post_key_is_one_message_one_event_and_conflicting_retry_is_rejected(messages_space):
    board, human, other, _, ticket, *_ = messages_space
    first = board.post_message(human, ticket['thread_id'], 'A message', idempotency_key='post')
    second = board.post_message(human, ticket['thread_id'], 'A message', idempotency_key='post')
    assert first == second
    assert len(message_events(board, other, ticket)) == 1
    with pytest.raises(BoardError):
        board.post_message(human, ticket['thread_id'], 'Different message', idempotency_key='post')
    assert len(message_events(board, other, ticket)) == 1
    assert len(board.get_thread(human, ticket['thread_id'])['messages']) == 1


def test_standalone_thread_produces_no_ticket_event(messages_space):
    board, human, other, _, ticket, *_ = messages_space
    thread = board.create_thread(human, 'Standalone', ['alice', 'bob'], 'standalone')
    before = board.list_activity(other, order='asc')
    board.post_message(human, thread['id'], 'Discussion without ticket', idempotency_key='post')
    assert board.list_activity(other, order='asc') == before
    assert message_events(board, other, ticket) == []


def test_message_and_event_rollback_together_and_retry_can_then_succeed(messages_space, monkeypatch):
    board, human, other, _, ticket, *_ = messages_space
    original = board._audit
    def fail_after_event(db, actor, event, detail):
        result = original(db, actor, event, detail)
        if event == 'ticket_message_posted':
            raise RuntimeError('injected transaction interruption')
        return result
    monkeypatch.setattr(board, '_audit', fail_after_event)
    with pytest.raises(RuntimeError):
        board.post_message(human, ticket['thread_id'], 'Atomic post', idempotency_key='post')
    assert board.get_thread(human, ticket['thread_id'])['messages'] == []
    assert message_events(board, other, ticket) == []
    monkeypatch.setattr(board, '_audit', original)
    board.post_message(human, ticket['thread_id'], 'Atomic post', idempotency_key='post')
    assert len(board.get_thread(human, ticket['thread_id'])['messages']) == 1
    assert len(message_events(board, other, ticket)) == 1


@pytest.mark.parametrize('denial', ['reply_disabled', 'device_revoked', 'outsider'])
def test_denied_post_creates_neither_message_nor_event(messages_space, denial):
    board, human, other, _, ticket, agent, device, _ = messages_space
    actor = agent
    if denial == 'reply_disabled':
        board.set_policy(human, True, True, False, True, [], [])
    elif denial == 'device_revoked':
        board.revoke_device(human, device['id'])
    else:
        actor = {'kind': 'human', 'owner_id': 'outsider'}
    with pytest.raises(BoardError):
        board.post_message(actor, ticket['thread_id'], 'Denied', idempotency_key='post')
    assert board.get_thread(other, ticket['thread_id'])['messages'] == []
    assert message_events(board, other, ticket) == []


def test_private_ticket_events_keep_project_membership_and_live_read_policy(messages_space):
    board, human, other, _, _, agent, _, _ = messages_space
    project = board.create_project(human, 'Private', ['alice'], idempotency_key='private')
    ticket = board.create_ticket(human, project['id'], 'Private task', idempotency_key='private-ticket')
    board.post_message(human, ticket['thread_id'], 'Private message', idempotency_key='post')
    assert len(message_events(board, agent, ticket)) == 1
    assert all(event['ticket_id'] != ticket['id'] for event in board.list_activity(other, order='asc'))
    with pytest.raises(BoardError) as error:
        board.list_activity(other, ticket_id=ticket['id'])
    assert error.value.status == 404
    board.set_policy(human, True, False, True, True, [], [])
    with pytest.raises(BoardError) as error:
        board.list_activity(agent, ticket_id=ticket['id'])
    assert error.value.status == 403


def test_structured_human_handoff_retains_only_semantic_events(messages_space):
    board, human, _, _, ticket, agent, *_ = messages_space
    board.claim_ticket(agent, ticket['id'], ticket['version'])
    request = board.request_human_input(agent, ticket['id'], 'alice', 'Which value?', 'request')
    board.resolve_human_request(human, request['id'], 'Chosen value')
    kinds = [event['event'] for event in board.list_activity(human, ticket_id=ticket['id'], order='asc')]
    assert kinds.count('human_input_requested') == 1
    assert kinds.count('human_input_resolved') == 1
    assert 'ticket_message_posted' not in kinds
    assert len(board.get_thread(human, ticket['thread_id'])['messages']) == 2
