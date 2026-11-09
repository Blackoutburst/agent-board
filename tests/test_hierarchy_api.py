"""Actual REST authentication, CSRF, strict bodies and owner-scoped grants."""
from fastapi.testclient import TestClient
import pytest

from test_api import application, login
from test_hierarchy_policy import policy


def values(application):
    b = application.state.board; human = dict(kind='human', owner_id='blackoutburst')
    project = b.create_project(human, 'Fixture', ['blackoutburst'], idempotency_key='project')
    profile = b.create_agent_profile(human, 'Lead', project_ids=[project['id']])
    return policy(project_ids=[project['id']]), profile


def test_hierarchy_admin_requires_human_cookie_and_csrf(application):
    ceiling, profile = values(application)
    with TestClient(application) as client:
        assert client.get('/api/hierarchy').status_code == 401
        login(client)
        assert client.get('/api/hierarchy').json() == dict(limits=None, delegations=[], next_after=None)
        csrf = client.headers.pop('X-CSRF-Token')
        body = dict(expected_version=0, policy=ceiling, idempotency_key='enable')
        assert client.put('/api/hierarchy/limits', json=body).status_code == 403
        client.headers['X-CSRF-Token'] = csrf
        first = client.put('/api/hierarchy/limits', json=body)
        assert first.status_code == 200, first.text
        assert client.put('/api/hierarchy/limits', json=body).json() == first.json()
        assert client.get('/api/hierarchy').json()['delegations'] == []
        # Configuring a ceiling creates no delegation, device or connection.
        with application.state.board._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM hierarchy_delegations').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM devices').fetchone()[0] == 0


def test_human_root_cas_revocation_and_cross_owner_isolation(application):
    ceiling, profile = values(application)
    other = TestClient(application)
    with TestClient(application) as client:
        login(client); login(other, 'thescreamer')
        assert client.put('/api/hierarchy/limits', json=dict(expected_version=0, policy=ceiling, idempotency_key='enable')).status_code == 200
        body = dict(beneficiary_id=profile['id'], policy=ceiling, idempotency_key='root')
        first = client.post('/api/hierarchy/roots', json=body)
        assert first.status_code == 200, first.text
        root = first.json(); path = '/api/hierarchy/delegations/' + root['id']
        assert client.post('/api/hierarchy/roots', json=body).json() == root
        assert other.get(path).status_code == 404
        assert other.get('/api/hierarchy').json()['limits'] is None
        request = dict(expected_version=1, revoke=True, idempotency_key='revoke')
        assert other.post(path + '/change', json=request).status_code == 404
        revoked = client.post(path + '/change', json=request)
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()['status'] == 'revoked'
        assert client.post(path + '/change', json=request).json() == revoked.json()
        assert client.post(path + '/change', json=dict(request, idempotency_key='new-key')).status_code == 409
        assert client.get(path).json()['policy'] == root['policy']
        assert client.get('/api/hierarchy').json()['delegations'][0]['status'] == 'revoked'


@pytest.mark.parametrize('extra', ['owner_id', 'actor', 'companion_for', 'counters', 'status', 'device_id'])
def test_hierarchy_admin_rejects_authority_and_counter_fields(application, extra):
    ceiling, _ = values(application)
    with TestClient(application) as client:
        login(client)
        body = dict(expected_version=0, policy=ceiling, idempotency_key='enable') | {extra: 'forged'}
        assert client.put('/api/hierarchy/limits', json=body).status_code == 422
        assert client.get('/api/hierarchy').json()['limits'] is None


def test_hierarchy_version_types_and_invalid_policy_cannot_be_coerced(application):
    ceiling, _ = values(application)
    with TestClient(application) as client:
        login(client)
        for version in (True, '0', 0.0, -1):
            assert client.put('/api/hierarchy/limits', json=dict(expected_version=version, policy=ceiling, idempotency_key='version')).status_code == 422
        assert client.put('/api/hierarchy/limits', json=dict(expected_version=0, policy=ceiling | {'unknown': True}, idempotency_key='policy')).status_code == 400
        assert client.get('/api/hierarchy').json()['limits'] is None


def test_human_overview_is_bounded_and_preserves_pagination(application):
    ceiling, profile = values(application)
    with TestClient(application) as client:
        login(client)
        client.put('/api/hierarchy/limits', json=dict(expected_version=0, policy=ceiling, idempotency_key='enable'))
        ids = set()
        for key in ('one', 'two'):
            response = client.post('/api/hierarchy/roots', json=dict(beneficiary_id=profile['id'], policy=ceiling, idempotency_key=key))
            ids.add(response.json()['id'])
        first = client.get('/api/hierarchy?limit=1').json()
        second = client.get('/api/hierarchy', params=dict(limit=1, after=first['next_after'])).json()
        assert {first['delegations'][0]['id'], second['delegations'][0]['id']} == ids
        assert second['next_after'] is None
        assert client.get('/api/hierarchy?limit=101').status_code == 422
