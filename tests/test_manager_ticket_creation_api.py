"""Cookie/Origin/CSRF/CAS boundary for the separate assigned-create grant."""
from fastapi.testclient import TestClient

from profile_management import TICKET_CREATION_ACTION
from test_api import application, login
from test_profile_management_api import lead_browser


def test_grant_requires_current_human_cookie_origin_csrf_and_closed_body(lead_browser):
    client, board, human, device, _, profile = lead_browser
    path = '/api/agent-profiles/' + profile['id'] + '/ticket-creation-management'
    body = dict(expected_version=profile['version'], enabled=True)
    before = board.get_agent_profile(human, profile['id'])
    agent = TestClient(client.app)
    assert agent.put(path, headers={'Authorization': 'Bearer ' + device['token']}, json=body).status_code == 401
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.put(path, json=body).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    assert client.put(path, json=body, headers={'Origin': 'https://attacker.invalid'}).status_code == 403
    assert client.put(path, json=body | {'expected_version': profile['version'] - 1}).status_code == 409
    assert client.put(path, json=body | {'enabled': 'true'}).status_code == 422
    assert client.put(path, json=body | {'force': True}).status_code == 422
    other = TestClient(client.app)
    login(other, 'thescreamer')
    assert other.put(path, json=body).status_code == 404
    assert board.get_agent_profile(human, profile['id']) == before


def test_human_explicit_grant_and_revocation_do_not_enable_or_start_other_work(lead_browser):
    client, board, human, _, _, profile = lead_browser
    path = '/api/agent-profiles/' + profile['id'] + '/ticket-creation-management'
    assert TICKET_CREATION_ACTION not in profile['policy']['actions']
    before_runtime = board.get_runtime(human)
    response = client.put(path, json=dict(expected_version=profile['version'], enabled=True))
    assert response.status_code == 200, response.text
    granted = response.json()
    assert granted['version'] == profile['version'] + 1
    assert set(granted['policy']['actions']) == set(profile['policy']['actions']) | {TICKET_CREATION_ACTION}
    assert granted['policy']['resources'] == profile['policy']['resources']
    assert granted['status'] == profile['status']
    # Exact stale grant replay is refused; callers reload rather than applying twice.
    assert client.put(path, json=dict(expected_version=profile['version'], enabled=True)).status_code == 409
    response = client.put(path, json=dict(expected_version=granted['version'], enabled=False))
    assert response.status_code == 200, response.text
    assert TICKET_CREATION_ACTION not in response.json()['policy']['actions']
    assert board.get_runtime(human) == before_runtime
