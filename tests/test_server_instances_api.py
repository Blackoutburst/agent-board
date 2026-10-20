"""Actual ASGI/SDK routing: manager role, strict raw arguments and human CSRF."""
import json

from fastapi.testclient import TestClient
import pytest

from app import create_app
from server_instances import MANAGER_TOOLS

SHA = 'a' * 64


@pytest.fixture
def browser(tmp_path):
    app = create_app(tmp_path / 'data', base_url='http://testserver', filebrowser_url='', allow_local_passwords=True)
    b = app.state.board
    b.ensure_owner('blackoutburst', 'Blackoutburst', 'fixture-password-only')
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    b.set_policy(human, True, True, True, True, [], [])
    device = b.create_device(human, 'Controller', [])
    profile = b.create_agent_profile(human, 'Responder', policy={'allow_read': True, 'allow_messages': True,
        'allow_requests': False, 'actions': [], 'resources': [], 'autonomy': 'responses'})
    profile = b.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'server', 'codex')
    with TestClient(app) as client:
        login = client.post('/api/login', json={'username': 'blackoutburst', 'password': 'fixture-password-only'})
        client.headers['X-CSRF-Token'] = login.json()['csrf_token']
        yield client, b, human, device, profile


def rpc(browser, name, arguments, error=False, token=None):
    client, _, _, device, _ = browser
    response = client.post('/mcp', headers={'Authorization': 'Bearer ' + (token or device['token']),
        'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'},
        content=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments}}, ensure_ascii=True).encode('ascii'))
    assert response.status_code == 200, response.text
    envelope = response.json()
    if error and 'error' in envelope:
        return envelope
    result = envelope['result']
    if error:
        assert result.get('isError'), result
        return result
    assert not result.get('isError'), result
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def setup(browser):
    client, _, _, device, profile = browser
    response = client.post('/api/server-hosts', json={'device_id': device['id'], 'name': 'My server', 'idempotency_key': 'grant'})
    assert response.status_code == 200, response.text
    host = response.json()
    catalog = rpc(browser, 'publish_server_catalog', {'host_id': host['id'], 'expected_catalog_version': 0,
        'entries': [{'catalog_key': 'target-a', 'profile_id': profile['id'], 'profile_version': profile['version'],
            'launchspec_sha256': SHA, 'mode': 'messages'}], 'idempotency_key': 'catalog'})
    return host, catalog['instances'][0]


def observation(host, i, **changes):
    values = {'host_id': host['id'], 'instance_id': i['id'], 'expected_observation_revision': i['observation_revision'],
        'generation': i['generation'], 'launchspec_sha256': i['launchspec_sha256'], 'command_id': i['command_id'],
        'state': 'stopped', 'cleanup_confirmed': True, 'reason': '', 'idempotency_key': 'initial'}
    values.update(changes)
    return values


def test_real_rest_and_sdk_complete_roundtrip_without_session_or_process(browser):
    client, b, human, *_ = browser
    host, i = setup(browser)
    assert client.get('/api/server-hosts').json()['hosts'][0]['id'] == host['id']
    assert client.get('/api/server-hosts/' + host['id']).json()['instances'][0]['id'] == i['id']
    i = rpc(browser, 'report_instance_state', observation(host, i))['instance']
    cmd = client.post('/api/server-instances/' + i['id'] + '/commands', json={'action': 'start', 'expected_version': i['version'],
        'expected_generation': 0, 'launchspec_sha256': SHA, 'budget': 1, 'idempotency_key': 'start'}).json()
    poll = rpc(browser, 'poll_instance_commands', {'host_id': host['id']})
    assert poll['commands'] == [cmd] and poll['instances'][0]['catalog_key'] == 'target-a'
    accepted = rpc(browser, 'ack_instance_command', {'host_id': host['id'], 'command_id': cmd['id'],
        'expected_revision': cmd['revision'], 'idempotency_key': 'ack'})
    ready = rpc(browser, 'report_instance_state', observation(host, accepted['instance'], state='ready',
        cleanup_confirmed=False, idempotency_key='ready'))
    assert ready['command']['status'] == 'complete'
    assert b.get_runtime(human)['active_count'] == 0
    assert client.get('/health').json()['schema_version'] == 14


def test_manager_roles_not_given_to_ordinary_agent_or_foreign_owner(browser):
    _, b, human, *_ = browser
    host, _ = setup(browser)
    other = b.create_device(human, 'Ordinary', [])
    rpc(browser, 'poll_instance_commands', {'host_id': host['id']}, error=True, token=other['token'])
    b.ensure_owner('foreign', 'Foreign')
    other = b.create_device({'kind': 'human', 'owner_id': 'foreign'}, 'Other', [])
    rpc(browser, 'poll_instance_commands', {'host_id': host['id']}, error=True, token=other['token'])
    # The grant operation is deliberately not an MCP tool.
    rpc(browser, 'create_server_host', {'device_id': other['id'], 'name': 'Self', 'idempotency_key': 'bad'}, error=True)


def test_actual_sdk_schema_and_raw_override_unknown_keys(browser):
    client, _, _, device, _ = browser
    response = client.post('/mcp', headers={'Authorization': 'Bearer ' + device['token'], 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})
    tools = {item['name']: item for item in response.json()['result']['tools']}
    assert MANAGER_TOOLS <= tools.keys()
    assert all(tools[name]['inputSchema']['additionalProperties'] is False for name in MANAGER_TOOLS)
    host, i = setup(browser)
    sentinel = 'SENTINEL_SECRET_RAW_DO_NOT_ECHO'
    result = rpc(browser, 'poll_instance_commands', {'host_id': host['id'], 'token': sentinel}, error=True)
    assert sentinel not in json.dumps(result)
    result = rpc(browser, 'report_instance_state', {**observation(host, i), 'command': sentinel}, error=True)
    assert sentinel not in json.dumps(result)


@pytest.mark.parametrize('value', [True, 1.0, '1', None, 0, 21])
def test_actual_sdk_rejects_coerced_poll_limit(browser, value):
    host, _ = setup(browser)
    rpc(browser, 'poll_instance_commands', {'host_id': host['id'], 'limit': value}, error=True)


@pytest.mark.parametrize('changes', [{'cleanup_confirmed': 'true'}, {'cleanup_confirmed': 1}, {'cleanup_confirmed': None},
    {'generation': False}, {'generation': '0'}, {'expected_observation_revision': 0.0}, {'state': 'running'},
    {'instance_id': '\ud800'}, {'reason': 'secret arbitrary text'}, {'command_id': 1}])
def test_actual_sdk_rejects_report_scalar_types_unicode_and_free_diagnostics(browser, changes):
    host, i = setup(browser)
    before = browser[1].get_server_host(browser[2], host['id'])
    rpc(browser, 'report_instance_state', observation(host, i, **changes), error=True)
    assert browser[1].get_server_host(browser[2], host['id']) == before


def test_rest_grant_requires_cookie_csrf_and_exact_owner_device(browser):
    client, _, _, device, _ = browser
    csrf = client.headers.pop('X-CSRF-Token')
    values = {'device_id': device['id'], 'name': 'Server', 'idempotency_key': 'grant'}
    assert client.post('/api/server-hosts', json=values).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert client.post('/api/server-hosts', headers={'Origin': 'https://foreign.invalid'}, json=values).status_code == 403
    assert client.post('/api/server-hosts', json={**values, 'owner_id': 'thescreamer'}).status_code == 422
    client.cookies.clear()
    assert client.get('/api/server-hosts').status_code == 401


def test_rest_error_never_echoes_invalid_secrets_and_bounds_before_parse(browser):
    client, *_ = browser
    sentinel = 'SENTINEL_PRIVATE_SECRET'
    bad = client.post('/api/server-hosts', json={'name': sentinel, 'credentials': sentinel})
    assert bad.status_code == 422 and sentinel not in bad.text
    bad = client.post('/api/server-hosts', content=b'{' + b'x' * 4096, headers={'Content-Type': 'application/json'})
    assert bad.status_code == 413
    bad = client.post('/api/server-hosts', content=b'\xff', headers={'Content-Type': 'application/json'})
    assert bad.status_code == 400 or bad.status_code == 422


def test_mcp_cleanup_poll_stays_available_after_owner_policy_disabled(browser):
    _, b, human, *_ = browser
    host, i = setup(browser)
    b.set_policy(human, False, False, False, False, [], [])
    assert rpc(browser, 'poll_instance_commands', {'host_id': host['id']})['enabled'] is False
    assert rpc(browser, 'report_instance_state', observation(host, i))['instance']['cleanup_confirmed']
    rpc(browser, 'publish_server_catalog', {'host_id': host['id'], 'expected_catalog_version': 1,
        'entries': [], 'idempotency_key': 'bad'}, error=True)


@pytest.mark.parametrize('changes', [{'expected_version': True}, {'expected_generation': '0'}, {'budget': True},
    {'budget': -1}, {'action': 'shell'}, {'argv': ['sh', '-c', 'SENTINEL_PRIVATE']}, {'launchspec_sha256': 'not-hash'}])
def test_rest_command_strict_types_and_no_free_execution(browser, changes):
    client, *_ = browser
    host, i = setup(browser)
    i = rpc(browser, 'report_instance_state', observation(host, i))['instance']
    payload = {'action': 'start', 'expected_version': i['version'], 'expected_generation': i['generation'],
        'launchspec_sha256': SHA, 'budget': 1, 'idempotency_key': 'strict', **changes}
    result = client.post('/api/server-instances/' + i['id'] + '/commands', json=payload)
    assert result.status_code in (400, 422), result.text
    assert 'SENTINEL_PRIVATE' not in result.text
    assert rpc(browser, 'poll_instance_commands', {'host_id': host['id']})['commands'] == []


def test_chunked_rest_limit_and_actual_mcp_json_string_arguments(browser):
    client, *_ = browser
    def chunks():
        yield b'{' + b' ' * 2048
        yield b' ' * 2048 + b'}'
    response = client.post('/api/server-hosts', content=chunks(), headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    host, _ = setup(browser)
    rpc(browser, 'poll_instance_commands', json.dumps({'host_id': host['id']}), error=True)
