"""Preparation reflects actual owner metadata, never connection/entitlement."""
import copy
import json

import pytest

from board import BoardError
from provider_onboarding import overview, provider_catalog
from test_server_instances_api import browser, observation, rpc, setup


def test_closed_catalog_is_detached_and_promises_no_models_or_board_login():
    first = provider_catalog()
    assert {item['id'] for item in first} == {'codex_chatgpt', 'claude_code'}
    assert first[0]['text_adapter_implemented'] is True
    assert first[1]['text_adapter_implemented'] is False
    for provider in first:
        assert provider['billing'] == 'personal_subscription'
        assert provider['connection_status'] == 'not_attested'
        assert provider['model_catalog_status'] == 'not_attested'
        assert provider['confirmed_models'] == []
        assert provider['board_login_implemented'] is False
    first[0]['confirmed_models'].append('fake-model')
    first[0]['board_login_implemented'] = True
    assert provider_catalog()[0]['confirmed_models'] == []
    assert provider_catalog()[0]['board_login_implemented'] is False


def test_empty_preparation_and_host_id_are_owner_human_only(browser):
    client, board, human, device, _ = browser
    response = client.get('/api/provider-onboarding')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    empty = response.json()
    assert empty['host'] is None and empty['requested'] is None
    assert empty['target_registration'] == 'no_profile'
    host, instance = setup(browser)
    value = client.get('/api/provider-onboarding').json()
    assert value['host']['id'] == host['id']
    assert value['host']['owner_id'] == human['owner_id']
    assert value['host']['catalog_version'] == 1
    assert value['host']['published_targets'][0]['id'] == instance['id']
    raw = board.authenticate(device['token'])
    with pytest.raises(BoardError):
        overview(board, raw)
    client.cookies.clear()
    assert client.get('/api/provider-onboarding').status_code == 401
    assert client.get('/api/provider-onboarding', headers={'Authorization': 'Bearer '+device['token']}).status_code == 401


def test_ready_instance_and_matching_preference_are_not_auth_or_model_proof(browser):
    client, board, human, _, profile = browser
    host, instance = setup(browser)
    instance = rpc(browser, 'report_instance_state', observation(host, instance))['instance']
    command = client.post('/api/server-instances/'+instance['id']+'/commands', json={
        'action': 'start', 'expected_version': instance['version'], 'expected_generation': 0,
        'launchspec_sha256': instance['launchspec_sha256'], 'budget': 1, 'idempotency_key': 'start'}).json()
    accepted = rpc(browser, 'ack_instance_command', {'host_id': host['id'], 'command_id': command['id'],
        'expected_revision': command['revision'], 'idempotency_key': 'ack'})
    # Ready is a daemon observation; a provider model need not have run.
    rpc(browser, 'report_instance_state', observation(host, accepted['instance'],
        state='ready', cleanup_confirmed=False, idempotency_key='ready'))
    path = '/api/provider-onboarding?profile_id='+profile['id']
    value = client.get(path).json()
    assert value['target_registration'] == 'published'
    assert value['host']['published_targets'][0]['observed_state'] == 'ready'
    assert value['connection'] == {'status': 'not_attested', 'provider_id': None, 'observed_at': None}
    assert value['models'] == {'catalog_status': 'not_attested', 'access_status': 'not_attested', 'models': []}
    saved = board.update_agent_profile(human, profile['id'], profile['version'],
        preferred_provider='codex_chatgpt', preferred_model='requested-not-verified',
        instructions='SENTINEL_PRIVATE_INSTRUCTIONS')
    updated = client.get(path)
    assert updated.json()['requested']['model'] == saved['preferred_model']
    assert updated.json()['target_registration'] == 'profile_version_changed'
    assert updated.json()['connection'] == value['connection']
    assert 'SENTINEL_PRIVATE_INSTRUCTIONS' not in updated.text


def test_foreign_profile_host_and_provider_state_never_bleed(browser):
    client, board, human, _, _ = browser
    board.ensure_owner('foreign', 'Foreign')
    other = {'kind': 'human', 'owner_id': 'foreign'}
    profile = board.create_agent_profile(other, 'SENTINEL_FOREIGN', preferred_model='SECRET_OTHER_MODEL')
    device = board.create_device(other, 'SENTINEL_OTHER_DEVICE', [])
    board.create_server_host(other, device['id'], 'SENTINEL_OTHER_HOST', 'foreign-host')
    assert client.get('/api/provider-onboarding?profile_id='+profile['id']).status_code == 404
    value = client.get('/api/provider-onboarding')
    assert value.json()['host'] is None and 'SENTINEL' not in value.text and 'SECRET_OTHER_MODEL' not in value.text
    assert overview(board, other)['host']['owner_id'] != human['owner_id']


def test_preparation_is_read_only_and_does_not_create_target_or_runtime(browser):
    client, board, _, _, profile = browser
    setup(browser)
    with board._connection() as db:
        before = tuple(db.iterdump())
    for _ in range(3):
        value = client.get('/api/provider-onboarding?profile_id='+profile['id'])
        assert value.status_code == 200
    with board._connection() as db:
        assert tuple(db.iterdump()) == before
    assert value.json()['provisioning'] == {'board_provisioning_implemented': False,
        'board_login_implemented': False, 'isolated_owner_required': True, 'api_fallback': False}


def test_revoked_host_disappears_but_request_is_retained(browser):
    client, board, human, _, profile = browser
    host, _ = setup(browser)
    board.revoke_server_host(human, host['id'], host['version'], 'revoke')
    result = client.get('/api/provider-onboarding?profile_id='+profile['id']).json()
    assert result['host'] is None
    assert result['requested']['profile_id'] == profile['id']
    assert result['target_registration'] == 'not_published'


@pytest.mark.parametrize('query', [
    '?token=SENTINEL_SECRET', '?profile_id=x&profile_id=y', '?profile_id=',
    '?profile_id='+('x'*201), '?profile_id=%00SENTINEL_SECRET',
])
def test_invalid_query_is_bounded_and_never_echoes_input(browser, query):
    response = browser[0].get('/api/provider-onboarding'+query)
    assert response.status_code in (400, 422)
    assert len(response.content) < 200 and 'SENTINEL_SECRET' not in response.text


@pytest.mark.parametrize('identifier', [True, 1, {}, [], '\ud800', 'x'*201, '\x00private'])
def test_direct_projection_rejects_nonopaque_profile_ids(browser, identifier):
    with pytest.raises(BoardError):
        overview(browser[1], browser[2], identifier)


def test_no_model_tool_or_login_route_added_and_profile_creation_stays_idempotent(browser):
    client, board, human, device, _ = browser
    response = client.post('/mcp', headers={'Authorization': 'Bearer '+device['token'],
        'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})
    names = {tool['name'] for tool in response.json()['result']['tools']}
    assert len(names) == 62 and not any('provider' in name or 'login' in name for name in names)
    values = {'name': 'New prepared profile', 'preferred_provider': 'claude_code',
        'preferred_model': 'a-request-only', 'idempotency_key': 'create-prepared'}
    created = client.post('/api/agent-profiles', json=values)
    assert created.status_code == 200
    repeated = client.post('/api/agent-profiles', json=copy.deepcopy(values))
    assert repeated.json()['id'] == created.json()['id']
    value = client.get('/api/provider-onboarding?profile_id='+created.json()['id']).json()
    assert value['requested']['provider'] == 'claude_code'
    assert value['target_registration'] == 'not_published'
    assert board.get_runtime(human)['active_count'] == 0
    assert len(json.dumps(value).encode()) < 65536
