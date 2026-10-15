"""Human profile management through the authenticated, CSRF-protected API."""
from fastapi.testclient import TestClient

from test_api import application, login


def test_profile_management_requires_human_cookie_and_csrf(application):
    with TestClient(application) as client:
        assert client.get('/api/agent-profiles').status_code == 401
        login(client)
        csrf = client.headers.pop('X-CSRF-Token')
        assert client.post('/api/agent-profiles', json={'name': 'Reviewer', 'idempotency_key': 'new'}).status_code == 403
        client.headers['X-CSRF-Token'] = csrf
        response = client.post('/api/agent-profiles', json={'name': 'Reviewer', 'idempotency_key': 'new'})
        assert response.status_code == 200, response.text
        profile = response.json()
        assert profile['bindings'] == []
        assert profile['project_ids'] == []
        assert not profile['policy']['allow_requests']
        assert not profile['policy']['allow_read']
        assert client.post('/api/agent-profiles', json={'name': 'Reviewer', 'idempotency_key': 'new'}).json()['id'] == profile['id']


def test_profile_cas_duplication_and_owner_isolation(application):
    other = TestClient(application)
    with TestClient(application) as client:
        login(client)
        login(other, 'thescreamer')
        original = client.post('/api/agent-profiles', json={'name': 'Builder', 'instructions': 'Private owner preferences',
            'language': 'fr', 'idempotency_key': 'builder'}).json()
        path = '/api/agent-profiles/' + original['id']
        assert other.get(path).status_code == 404
        assert other.patch(path, json={'expected_version': original['version'], 'name': 'Stolen'}).status_code == 404
        profiles = other.get('/api/agent-profiles').json()
        assert original['id'] not in [p['id'] for p in profiles]
        updated = client.patch(path, json={'expected_version': original['version'], 'name': 'Renamed', 'status': 'paused'})
        assert updated.status_code == 200, updated.text
        assert updated.json()['id'] == original['id']
        assert client.patch(path, json={'expected_version': original['version'], 'name': 'Stale'}).status_code == 409
        duplicate = client.post(path + '/duplicate', json={'expected_version': updated.json()['version'],
            'name': 'Another reviewer', 'idempotency_key': 'duplicate'})
        assert duplicate.status_code == 200, duplicate.text
        copy = duplicate.json()
        assert copy['id'] != original['id'] and copy['status'] == 'paused'
        assert copy['bindings'] == [] and copy['active_work'] == []
        assert not any(copy['policy'][k] for k in ('allow_read', 'allow_messages', 'allow_requests'))
        assert copy['policy']['actions'] == copy['policy']['resources'] == []


def test_profile_body_is_strict_and_does_not_accept_authority_or_secrets(application):
    with TestClient(application) as client:
        login(client)
        for changes in ({'owner_id': 'thescreamer'}, {'device_token': 'never-store'},
                        {'project_ids': None}, {'status': 'enabled'}, {'language': 'unknown'},
                        {'policy': {'allow_read': 'true'}}, {'avatar': 'a' * 33}):
            response = client.post('/api/agent-profiles', json={
                'name': 'Strict', 'idempotency_key': 'strict', **changes})
            assert response.status_code == 422, response.text
        profile = client.post('/api/agent-profiles', json={'name': 'Strict', 'idempotency_key': 'strict'}).json()
        path = '/api/agent-profiles/' + profile['id']
        for version in ('1', True, 1.0, 0):
            assert client.patch(path, json={'expected_version': version, 'name': 'Invalid'}).status_code == 422
        assert client.get(path).json()['version'] == profile['version']


def test_profile_binding_requires_own_device_and_exact_version(application):
    other = TestClient(application)
    with TestClient(application) as client:
        owner = login(client)
        stranger = login(other, 'thescreamer')
        board = application.state.board
        mine = board.create_device(owner, 'Own executor', [])
        theirs = board.create_device(stranger, 'Other executor', [])
        profile = client.post('/api/agent-profiles', json={'name': 'Bound', 'idempotency_key': 'bound'}).json()
        path = '/api/agent-profiles/' + profile['id']
        payload = {'expected_version': profile['version'], 'device_id': theirs['id'], 'identity_key': 'reviewer', 'agent_kind': 'codex'}
        assert client.post(path + '/bindings', json=payload).status_code in (400, 403, 404)
        payload['device_id'] = mine['id']
        response = client.post(path + '/bindings', json=payload)
        assert response.status_code == 200, response.text
        assert 'token' not in response.text and 'token' not in client.get(path).text
        assert client.post(path + '/bindings', json={**payload, 'identity_key': 'stale'}).status_code == 409


def test_profile_history_preserves_exact_configuration_and_is_owner_only(application):
    other = TestClient(application)
    with TestClient(application) as client:
        login(client)
        login(other, 'thescreamer')
        original = client.post('/api/agent-profiles', json={'name': 'Versioned', 'instructions': 'First instructions',
            'policy': {'allow_read': True}, 'idempotency_key': 'history'}).json()
        path = '/api/agent-profiles/' + original['id']
        first = client.get(path + '/versions/1').json()
        assert first['instructions'] == 'First instructions'
        assert first['policy']['allow_read'] and not first['policy']['allow_messages']
        assert 'bindings' not in first and 'active_work' not in first
        changed = client.patch(path, json={'expected_version': 1, 'instructions': 'Second instructions', 'status': 'paused'})
        assert changed.status_code == 200, changed.text
        assert client.get(path + '/versions/1').json() == first
        assert client.get(path + '/versions/2').json()['instructions'] == 'Second instructions'
        assert client.get(path + '/versions/3').status_code == 404
        assert other.get(path + '/versions/1').status_code == 404
