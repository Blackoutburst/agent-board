"""Actual REST and MCP boundaries for runtime metadata and cleanup controls."""
import json

from fastapi.testclient import TestClient
import pytest

from app import create_app


@pytest.fixture
def browser(tmp_path):
    app = create_app(tmp_path / 'data', base_url='http://testserver', filebrowser_url='', allow_local_passwords=True)
    board = app.state.board
    board.ensure_owner('blackoutburst', 'Blackoutburst', 'fixture-password-only')
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    board.set_policy(human, True, True, True, True, [], [])
    project = board.create_project(human, 'Fixture project', ['blackoutburst'], idempotency_key='project')
    ticket = board.create_ticket(human, project['id'], 'Fixture ticket', idempotency_key='ticket')
    device = board.create_device(human, 'Fixture host', [])
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'runtime-api', 'Fixture', [], 'codex', False, 'fixture')
    actor = {**raw, 'session_id': 'runtime-api'}
    board.claim_ticket(actor, ticket['id'], ticket['version'])
    with TestClient(app) as client:
        login = client.post('/api/login', json={'username': 'blackoutburst', 'password': 'fixture-password-only'})
        client.headers['X-CSRF-Token'] = login.json()['csrf_token']
        yield client, board, human, device['token'], project, ticket, registration['identity']['id']


def rpc(browser, name, arguments, *, error=False):
    client, _, _, token, *_ = browser
    response = client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': {'session_id': 'runtime-api', **arguments}}})
    assert response.status_code == 200
    value = response.json()['result']
    if error:
        assert value.get('isError'), value
        return value
    assert not value.get('isError'), value
    return value.get('structuredContent') or json.loads(value['content'][0]['text'])


def reservation(browser, **changes):
    values = {'idempotency_key': 'api-reservation', 'project_id': browser[4]['id'], 'ticket_id': browser[5]['id'],
              'work_id': 'opaque-work', 'turn_id': 'opaque-turn', 'provider': 'codex_chatgpt', 'model': 'fixture-model'}
    values.update(changes)
    return rpc(browser, 'reserve_execution', values)


def test_six_tools_and_owner_runtime_roundtrip(browser):
    client, _, _, token, *_ = browser
    response = client.post('/mcp', headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    names = {item['name'] for item in response.json()['result']['tools']}
    assert {'reserve_execution', 'lookup_execution', 'start_execution', 'heartbeat_execution', 'finish_execution', 'get_execution_control'} <= names
    assert client.get('/health').json()['schema_version'] == 9
    assert client.get('/health').json()['version'] == '2.7.3'
    assert rpc(browser, 'lookup_execution', {'idempotency_key': 'absent'}) == {'found': False}
    execution = reservation(browser)['execution']
    assert rpc(browser, 'lookup_execution', {'idempotency_key': 'api-reservation'})['execution']['id'] == execution['id']
    started = rpc(browser, 'start_execution', {'execution_id': execution['id'], 'expected_revision': 1})
    live = rpc(browser, 'heartbeat_execution', {'execution_id': execution['id'], 'expected_revision': started['revision']})
    stop = client.post('/api/runtime/executions/' + execution['id'] + '/stop', json={'expected_revision': live['revision']}).json()
    assert stop['stop_requested'] and not stop['cleanup_confirmed']
    control = rpc(browser, 'get_execution_control', {'execution_id': execution['id']})
    finished = rpc(browser, 'finish_execution', {'execution_id': execution['id'], 'expected_revision': control['revision'],
        'status': 'cancelled', 'cleanup_confirmed': True, 'reason': 'stop_requested'})
    assert finished['state'] == 'finished'
    view = client.get('/api/runtime').json()
    assert view['active_count'] == 0 and view['quota'] == {'status': 'unavailable'}
    assert view['executions'][0]['id'] == finished['id']


def test_mcp_cleanup_channel_survives_profile_pause_and_owner_policy_disabled(browser):
    _, board, human, _, _, _, profile_id = browser
    execution = reservation(browser)['execution']
    started = rpc(browser, 'start_execution', {'execution_id': execution['id'], 'expected_revision': 1})
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], status='paused')
    board.set_policy(human, False, False, False, False, [], [])
    control = rpc(browser, 'get_execution_control', {'execution_id': execution['id']})
    assert control['should_stop'] and control['control_reason'] == 'authorization_changed'
    assert rpc(browser, 'lookup_execution', {'idempotency_key': 'api-reservation'})['found']
    heartbeat = rpc(browser, 'heartbeat_execution', {'execution_id': execution['id'], 'expected_revision': started['revision']})
    finished = rpc(browser, 'finish_execution', {'execution_id': execution['id'], 'expected_revision': heartbeat['revision'],
        'status': 'cancelled', 'cleanup_confirmed': True, 'reason': 'policy_changed'})
    assert finished['state'] == 'finished'
    rpc(browser, 'get_ticket', {'ticket_id': browser[5]['id']}, error=True)


@pytest.mark.parametrize('value', [True, 0, -1, 1.0, '1', None])
def test_mcp_revision_types_are_strict(browser, value):
    execution = reservation(browser)['execution']
    rpc(browser, 'start_execution', {'execution_id': execution['id'], 'expected_revision': value}, error=True)
    assert rpc(browser, 'get_execution_control', {'execution_id': execution['id']})['state'] == 'reserved'


@pytest.mark.parametrize('value', ['true', 1, None, []])
def test_mcp_cleanup_type_is_strict(browser, value):
    execution = reservation(browser)['execution']
    rpc(browser, 'finish_execution', {'execution_id': execution['id'], 'expected_revision': 1,
        'status': 'cancelled', 'cleanup_confirmed': value}, error=True)
    assert browser[0].get('/api/runtime').json()['active_count'] == 1


def test_rest_strict_owner_cas_authentication_and_csrf(browser):
    client, *_ = browser
    before = client.get('/api/runtime').json()
    for changes in [{'expected_version': True}, {'max_concurrent': 1.0}, {'paused': 'false'}, {'max_concurrent': 9}, {'secret': 'NO'}]:
        response = client.patch('/api/runtime/limits', json={'expected_version': 1, 'max_concurrent': 2, 'paused': False, **changes})
        assert response.status_code == 422
    assert client.get('/api/runtime').json() == before
    updated = client.patch('/api/runtime/limits', json={'expected_version': 1, 'max_concurrent': 2, 'paused': True})
    assert updated.status_code == 200 and updated.json()['version'] == 2
    assert client.patch('/api/runtime/limits', json={'expected_version': 1, 'max_concurrent': 1, 'paused': False}).status_code == 409
    client.headers.pop('X-CSRF-Token')
    assert client.patch('/api/runtime/limits', json={'expected_version': 2, 'max_concurrent': 1, 'paused': False}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/runtime').status_code == 401


def test_mcp_scope_identifier_does_not_coerce_numbers_or_accept_paths(browser):
    for value in [1, True, None, '/private/secret', 'secret\ntext']:
        rpc(browser, 'lookup_execution', {'idempotency_key': value}, error=True)
    assert browser[0].get('/api/runtime').json()['active_count'] == 0
