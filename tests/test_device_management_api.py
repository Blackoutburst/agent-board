"""Owner-only device rename and profile connection removal."""
from fastapi.testclient import TestClient

from test_api import application, login
from test_coordination_api import browser


def test_device_rename_is_owner_only_trimmed_and_audited(application):
    other = TestClient(application)
    with TestClient(application) as client:
        owner = login(client)
        login(other, 'thescreamer')
        board = application.state.board
        device = board.create_device(owner, 'Home Server', [])
        path = '/api/settings/devices/' + device['id']
        response = client.patch(path, json={'label': '  Linux server  '})
        assert response.status_code == 200, response.text
        assert response.json()['label'] == 'Linux server'
        assert 'token' not in response.text
        labels = {d['id']: d['label'] for d in client.get('/api/settings').json()['devices']}
        assert labels[device['id']] == 'Linux server'
        for body in ({'label': ''}, {'label': 'x' * 101}, {'label': 'ok', 'owner_id': 'thescreamer'}):
            assert client.patch(path, json=body).status_code == 422
        assert client.patch(path, json={'label': '   '}).status_code == 400
        assert other.patch(path, json={'label': 'Stolen'}).status_code == 404
        assert client.patch('/api/settings/devices/missing', json={'label': 'Nope'}).status_code == 404
        csrf = client.headers.pop('X-CSRF-Token')
        assert client.patch(path, json={'label': 'No CSRF'}).status_code == 403
        client.headers['X-CSRF-Token'] = csrf
        # Revoked devices may still be relabelled; the label is cosmetic.
        assert client.delete(path).status_code == 200
        assert client.patch(path, json={'label': 'Old server'}).json()['label'] == 'Old server'
        with board._tx() as db:
            events = [r['event'] for r in db.execute("SELECT event FROM audit WHERE event='device_renamed'")]
        assert len(events) == 2


def test_pairing_label_is_trimmed(application):
    with TestClient(application) as client:
        owner = login(client)
        pairing = application.state.board.create_pairing(owner, 'Home Server ')
        assert pairing['label'] == 'Home Server'


def _bound_worker(browser, key='worker'):
    from test_coordination_api import rpc
    client, board = browser
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    project = board.create_project(human, 'Unbind', ['blackoutburst'], idempotency_key='unbind-' + key)
    profile = client.post('/api/agent-profiles', json={
        'name': 'Server worker', 'idempotency_key': 'profile-' + key, 'project_ids': [project['id']],
        'policy': {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
                   'actions': [], 'resources': [], 'autonomy': 'requested'}}).json()
    device = board.create_device(human, 'PC Blackout', [])
    response = client.post('/api/agent-profiles/' + profile['id'] + '/bindings', json={
        'expected_version': profile['version'], 'device_id': device['id'], 'identity_key': key, 'agent_kind': 'codex'})
    assert response.status_code == 200, response.text
    registration = rpc(client, device['token'], 'register_agent', {
        'session_id': 'chat-' + key, 'label': 'Chat', 'agent_kind': 'codex', 'identity_key': key})
    assert registration['identity']['id'] == profile['id']
    return project, response.json(), device


def test_profile_connection_removal_refuses_reregistration(browser):
    from test_coordination_api import rpc
    client, board = browser
    project, profile, device = _bound_worker(browser)
    path = '/api/agent-profiles/' + profile['id'] + '/bindings/remove'
    body = {'expected_version': profile['version'], 'device_id': device['id'], 'agent_kind': 'codex', 'identity_key': 'worker'}
    assert client.post(path, json={**body, 'expected_version': profile['version'] - 1}).status_code == 409
    assert client.post(path, json={**body, 'identity_key': 'other'}).status_code == 404
    assert client.post(path, json={**body, 'owner_id': 'x'}).status_code == 422
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.post(path, json=body).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    response = client.post(path, json=body)
    assert response.status_code == 200, response.text
    removed = response.json()
    assert removed['bindings'] == [] and removed['version'] == profile['version'] + 1
    assert client.post(path, json={**body, 'expected_version': removed['version']}).status_code == 404
    # Existing and new chats on that connection are refused, not given a legacy profile.
    for session in ('chat-worker', 'fresh-chat'):
        result = client.post('/mcp', headers={'Authorization': 'Bearer ' + device['token'], 'Accept': 'application/json, text/event-stream'},
            json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': 'register_agent', 'arguments': {
                'session_id': session, 'label': 'Chat', 'agent_kind': 'codex', 'identity_key': 'worker'}}}).json()['result']
        assert result.get('isError') and 'not bound' in str(result)
    # The device credential itself is kept.
    devices = {d['id']: d for d in client.get('/api/settings').json()['devices']}
    assert not devices[device['id']]['revoked']


def test_profile_connection_removal_requires_released_work_and_owner(browser):
    from test_coordination_api import rpc
    client, board = browser
    project, profile, device = _bound_worker(browser, 'busy')
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    ticket = board.create_ticket(human, project['id'], 'Busy work', idempotency_key='busy')
    rpc(client, device['token'], 'claim_ticket', {'session_id': 'chat-busy', 'ticket_id': ticket['id'], 'expected_version': ticket['version']})
    path = '/api/agent-profiles/' + profile['id'] + '/bindings/remove'
    body = {'expected_version': profile['version'], 'device_id': device['id'], 'agent_kind': 'codex', 'identity_key': 'busy'}
    refused = client.post(path, json=body)
    assert refused.status_code == 409 and 'active work' in refused.text
    board.ensure_owner('thescreamer', 'TheScreamer', 'other-fixture-password')
    other = TestClient(client.app)
    login_response = other.post('/api/login', json={'username': 'thescreamer', 'password': 'other-fixture-password'})
    other.headers['X-CSRF-Token'] = login_response.json()['csrf_token']
    assert other.post(path, json=body).status_code == 404
