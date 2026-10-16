"""Exact source retrieval for pinned decisions; no cursor or read side effects."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from board import BoardError
from test_coordination import space, new
from test_api import application, rpc, tool_data


def dump(board):
    with sqlite3.connect(board.db_path) as db:
        return tuple(db.iterdump())


def reject(status, call, *args):
    with pytest.raises(BoardError) as error:
        call(*args)
    assert error.value.status == status
    return str(error.value)


def test_exact_old_message_is_available_without_scanning_history_or_mutation(space):
    board, human, _, _, agent, *_ = space
    ticket = new(space)
    old = board.post_message(human, ticket['thread_id'], 'Decision before the current window', idempotency_key='old')
    for i in range(45):
        board.post_message(human, ticket['thread_id'], 'Later context', idempotency_key=f'later-{i}')
    assert old['id'] not in {m['id'] for m in board.get_thread(agent, ticket['thread_id'], limit=20)['messages']}
    before = dump(board)
    assert board.get_message(agent, ticket['thread_id'], old['id']) == {'message': old}
    assert dump(board) == before
    assert board.get_message(agent, ticket['thread_id'], old['id']) == {'message': old}
    assert dump(board) == before


def test_exact_message_requires_both_ids_and_current_conversation_membership(space):
    board, human, other, _, agent, *_ = space
    ticket, another = new(space), new(space, 'Another ticket')
    old = board.post_message(human, ticket['thread_id'], 'Private source', idempotency_key='source')
    before = dump(board)
    unknown = reject(404, board.get_message, agent, ticket['thread_id'], 'f'*32)
    assert reject(404, board.get_message, agent, another['thread_id'], old['id']) == unknown
    assert dump(board) == before
    private = board.create_thread(other, 'Not shared', [other['owner_id']], 'private-source-thread')
    foreign = board.post_message(other, private['id'], 'Other owner context', idempotency_key='foreign')
    unknown_thread = reject(404, board.get_message, agent, 'f'*32, foreign['id'])
    assert reject(404, board.get_message, agent, private['id'], foreign['id']) == unknown_thread
    with board._tx() as db:
        db.execute('DELETE FROM thread_members WHERE thread_id=? AND owner_id=?', (ticket['thread_id'], human['owner_id']))
    reject(404, board.get_message, agent, ticket['thread_id'], old['id'])


def test_read_revocation_and_profile_version_are_rechecked(space):
    board, human, _, _, agent, _, device = space
    ticket = new(space)
    old = board.post_message(human, ticket['thread_id'], 'Scoped source', idempotency_key='scoped')
    profile = board.list_agent_profiles(human)[0]
    board.update_agent_profile(human, profile['id'], profile['version'], project_ids=[])
    reject(409, board.get_message, agent, ticket['thread_id'], old['id'])
    raw = board.authenticate(device['token'])
    board.register_agent(raw, agent['session_id'], 'Fresh', [], 'codex', False, 'installation')
    reject(404, board.get_message, agent, ticket['thread_id'], old['id'])
    board.set_policy(human, True, False, True, True, [], [])
    reject(403, board.get_message, agent, ticket['thread_id'], old['id'])


@pytest.mark.parametrize('bad', [None, '', False, 1, 'x'*201])
def test_invalid_exact_ids_do_not_mutate(space, bad):
    board, _, _, _, agent, *_ = space
    ticket = new(space)
    before = dump(board)
    reject(404, board.get_message, agent, ticket['thread_id'], bad)
    reject(404, board.get_message, agent, bad, 'f'*32)
    assert dump(board) == before


def test_oversized_legacy_source_is_refused_without_cursor_or_read_mutation(space):
    board, human, _, _, agent, *_ = space
    ticket = new(space)
    old = board.post_message(human, ticket['thread_id'], '🧪'*100000, idempotency_key='large')
    before = dump(board)
    reject(413, board.get_message, agent, ticket['thread_id'], old['id'])
    assert dump(board) == before


def test_exact_mcp_unicode_source_fits_reader_limit_and_contains_no_extra_context(application):
    board = application.state.board
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    board.set_policy(human, True, True, True, True, [], [])
    device = board.create_device(human, 'Memory source fixture', [])
    raw = board.authenticate(device['token'])
    board.register_agent(raw, 'memory-source', 'Source', [], 'codex', False, 'memory')
    thread = board.create_thread(human, 'Source', ['blackoutburst'], 'source')
    message = board.post_message(human, thread['id'], '🧪'*50000, idempotency_key='unicode-source')
    board.post_message(human, thread['id'], 'UNRELATED_MESSAGE_BODY', idempotency_key='unrelated')
    before = dump(board)
    with TestClient(application) as client:
        response = rpc(client, device['token'], 'tools/call', {'name': 'get_message', 'arguments': {
            'session_id': 'memory-source', 'thread_id': thread['id'], 'message_id': message['id']}})
        assert response.status_code == 200
        assert len(response.content) < 2*1024*1024
        result = tool_data(response.json()['result'])
        assert result == {'message': message}
        assert 'UNRELATED_MESSAGE_BODY' not in response.text
    assert dump(board) == before
