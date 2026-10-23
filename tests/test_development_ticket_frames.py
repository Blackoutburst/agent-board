"""Real Board and MCP HTTP observations; no model or native execution."""
from copy import deepcopy
from unittest.mock import patch
import json

from fastapi.testclient import TestClient
import pytest

from board import BoardError
from development_ticket_frames import (get_development_ticket_frame,
    wire_ticket_frame, MAX_DATA_BYTES, MAX_WIRE_BYTES)
from development_frames import encoded
from test_api import application
from test_delegation_families_api import family_api, opened
from test_runtime_api import browser


@pytest.fixture
def ticket_frame(application):
    board = application.state.board
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    actions, resources = ['repo.read_file', 'repo.run_test'], ['repo:frame']
    board.set_policy(human, True, True, True, True, actions, resources)
    project = board.create_project(human, 'Frame project', ['blackoutburst'],
                                   context='Exact project context', idempotency_key='frame-project')
    ticket = board.create_ticket(human, project['id'], 'Ordinary development',
        description='Exact source, not a preview', resource=resources[0], idempotency_key='frame-ticket')
    policy = {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
              'actions': actions, 'resources': resources, 'autonomy': 'requested'}
    profile = board.create_agent_profile(human, 'Developer', policy=policy,
                                         project_ids=[project['id']], idempotency_key='frame-profile')
    device = board.create_device(human, 'Synthetic frame device', [])
    profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'frame', 'codex')
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'ticket-frame', 'Frame observation', [], 'codex', False, 'frame')
    with TestClient(application) as client:
        yield dict(board=board, human=human, project=project, ticket=ticket, profile=profile,
                   device=device, raw=raw, registration=registration, client=client,
                   arguments={'session_id': 'ticket-frame', 'ticket_id': ticket['id']})


def http_frame(fixture, changes=None, *, error=False):
    response = fixture['client'].post('/mcp', headers={
        'Authorization': 'Bearer ' + fixture['device']['token'],
        'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'},
        content=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'get_development_ticket_frame',
                       'arguments': {**fixture['arguments'], **(changes or {})}}}, ensure_ascii=True).encode())
    assert response.status_code == 200
    result = response.json()['result']
    assert bool(result.get('isError')) is error, result
    return result if error else result['structuredContent']


def local_frame(fixture):
    board = fixture['board']
    return get_development_ticket_frame(board, lambda session: board.actor_session(
        board.authenticate(fixture['device']['token']), session), **fixture['arguments'])


def test_mcp_closed_schema_exact_sources_and_no_queue_or_lease_effects(ticket_frame):
    f = ticket_frame
    board = f['board']
    response = f['client'].post('/mcp', headers={'Authorization': 'Bearer ' + f['device']['token'],
        'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    schema = next(item['inputSchema'] for item in response.json()['result']['tools']
                  if item['name'] == 'get_development_ticket_frame')
    assert schema['additionalProperties'] is False
    assert set(schema['required']) == {'session_id', 'ticket_id'}
    before = board.get_ticket(f['human'], f['ticket']['id'])
    with patch.object(board, 'get_work', side_effect=AssertionError('No queue read')), \
         patch.object(board, '_expire', side_effect=AssertionError('No legacy lease write')), \
         patch.object(board, 'sweep_blocked', side_effect=AssertionError('No sweep')), \
         patch.object(board, 'actor_session', wraps=board.actor_session) as actors, \
         patch.object(board, 'get_thread', wraps=board.get_thread) as history:
        frame = http_frame(f)
    assert actors.call_count == 7
    assert history.call_count == 1 and history.call_args.kwargs['_expire'] is False
    assert set(frame) == {'schema', 'selection', 'selected'}
    assert frame['schema'] == 'blackoutburst-development-ticket-frame-v1'
    assert frame['selection'] == {'owner_id': f['human']['owner_id'], 'agent_id': f['profile']['id'],
        'device_id': f['device']['id'], 'profile_version': f['profile']['version'],
        'project_id': f['project']['id'], 'ticket_id': f['ticket']['id'], 'session_id': 'ticket-frame'}
    selected = frame['selected']
    assert set(selected) == {'policy_first', 'first', 'pages', 'last', 'policy_last'}
    assert set(selected['policy_first']) == set(selected['policy_last']) == {'policy'}
    assert selected['first'] == selected['last'] == before
    assert selected['first']['ticket']['description'] == 'Exact source, not a preview'
    assert board.get_ticket(f['human'], f['ticket']['id']) == before
    assert board.get_runtime(f['human'])['active_count'] == 0


@pytest.mark.parametrize('changes', [
    {'extra': 'PRIVATE_TEST_VALUE'}, {'family_id': 'fake'}, {'view': 'full'}, {'limit': 200},
    {'session_id': True}, {'session_id': 'x' * 201}, {'ticket_id': 42},
    {'ticket_id': '\ud800'}, {'ticket_id': 'a\x00b'}, {'session_id': None},
])
def test_mcp_raw_arguments_refuse_without_partial_data(ticket_frame, changes):
    result = http_frame(ticket_frame, changes, error=True)
    assert 'development_ticket_frame_invalid_arguments' in json.dumps(result)
    assert 'PRIVATE_TEST_VALUE' not in json.dumps(result) and 'selection' not in result


@pytest.mark.parametrize('change', ['device', 'profile', 'scope', 'owner_read', 'membership', 'session_version'])
def test_authority_rechecked_after_history_and_partial_results_never_escape(ticket_frame, change):
    f = ticket_frame
    board, human = f['board'], f['human']
    original = board.get_thread
    def history(*args, **kwargs):
        result = original(*args, **kwargs)
        if change == 'device':
            board.revoke_device(human, f['device']['id'])
        elif change in ('profile', 'scope'):
            board.update_agent_profile(human, f['profile']['id'], f['profile']['version'],
                **({'status': 'paused'} if change == 'profile' else {'project_ids': []}))
        elif change == 'owner_read':
            board.set_policy(human, True, False, True, True, [], [])
        elif change == 'membership':
            with board._tx() as db:
                db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',
                           (f['project']['id'], human['owner_id']))
        else:
            with board._tx() as db:
                db.execute('UPDATE sessions SET profile_version=profile_version+1 WHERE id=?', ('ticket-frame',))
        return result
    with patch.object(board, 'get_thread', side_effect=history):
        result = http_frame(f, error=True)
    assert 'development_ticket_frame_unavailable' in json.dumps(result)
    assert 'selection' not in result and 'Exact source' not in json.dumps(result)


def test_other_owner_and_unknown_session_cannot_read(ticket_frame):
    f = ticket_frame
    other = {'kind': 'human', 'owner_id': 'thescreamer'}
    project = f['board'].create_project(other, 'Private project', ['thescreamer'], idempotency_key='other')
    ticket = f['board'].create_ticket(other, project['id'], 'PRIVATE_OTHER_OWNER', idempotency_key='other')
    for changes in ({'ticket_id': ticket['id']}, {'session_id': 'unknown'}):
        result = http_frame(f, changes, error=True)
        assert 'PRIVATE_OTHER_OWNER' not in json.dumps(result)


def test_storage_failure_is_bounded_and_never_exports_partial_source(ticket_frame):
    with patch.object(ticket_frame['board'], 'get_thread',
                      side_effect=RuntimeError('PRIVATE_STORAGE_PAYLOAD')):
        result = http_frame(ticket_frame, error=True)
    assert 'development_ticket_frame_unavailable' in json.dumps(result)
    assert 'PRIVATE_STORAGE_PAYLOAD' not in json.dumps(result)
    assert 'Exact source' not in json.dumps(result) and 'selection' not in result


def test_late_policy_change_is_a_fresh_observation_not_a_grant(ticket_frame):
    f = ticket_frame
    original = f['board'].get_thread
    def history(*args, **kwargs):
        result = original(*args, **kwargs)
        f['board'].set_policy(f['human'], True, True, True, False, [], [])
        return result
    with patch.object(f['board'], 'get_thread', side_effect=history):
        frame = http_frame(f)
    assert frame['selected']['policy_first']['policy']['allow_requests'] is True
    assert frame['selected']['policy_last']['policy']['allow_requests'] is False


def test_history_uses_real_cursors_and_exact_forty_message_bound(ticket_frame):
    f = ticket_frame
    for i in range(45):
        f['board'].post_message(f['human'], f['ticket']['thread_id'], 'message ' + str(i), idempotency_key=str(i))
    with patch.object(f['board'], 'get_thread', wraps=f['board'].get_thread) as history:
        frame = local_frame(f)
    pages = frame['selected']['pages']
    assert history.call_count == 2
    assert [page['arguments']['limit'] for page in pages] == [20, 20]
    assert pages[1]['arguments']['before'] == pages[0]['response']['next_before']
    assert len({message['id'] for page in pages for message in page['response']['messages']}) == 40


def test_oversized_unicode_source_refuses_and_wire_preserves_exact_body(ticket_frame):
    f = ticket_frame
    body = '\U0001f600' * 33000
    f['board'].post_message(f['human'], f['ticket']['thread_id'], body, idempotency_key='oversize')
    result = http_frame(f, error=True)
    assert 'development_ticket_frame_too_large' in json.dumps(result)
    assert body not in json.dumps(result, ensure_ascii=False)
    with pytest.raises(BoardError):
        wire_ticket_frame({'data': 'x' * MAX_DATA_BYTES})
    small = {'schema': 'fixture', 'body': '😀\\"\n' * 1000}
    wire = wire_ticket_frame(small)
    assert wire.structuredContent == small
    assert json.loads(wire.content[0].text) == small
    assert len(encoded(wire.model_dump(mode='json', exclude_none=True))) < MAX_WIRE_BYTES


def test_delegation_family_requires_its_existing_protocol(family_api):
    current = opened(family_api)
    old_browser, _ = family_api
    board, token = old_browser[1], old_browser[3]
    with pytest.raises(BoardError, match='development_ticket_frame_unavailable'):
        get_development_ticket_frame(board,
            lambda session: board.actor_session(board.authenticate(token), session),
            session_id='runtime-api', ticket_id=current['parent_ticket_id'])
