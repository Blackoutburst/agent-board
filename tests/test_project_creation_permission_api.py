"""Project-creation grants across the human API and authenticated MCP boundary."""
import json

from fastapi.testclient import TestClient

from test_coordination_api import browser, rpc


HUMAN = {'kind': 'human', 'owner_id': 'blackoutburst'}


def _policy(**changes):
    return {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
            'actions': [], 'resources': [], 'autonomy': 'requested', **changes}


def _patch(client, profile, **changes):
    response = client.patch('/api/agent-profiles/' + profile['id'], json={
        'expected_version': profile['version'], **changes})
    assert response.status_code == 200, response.text
    return response.json()


def _register(client, token, session='creator'):
    return rpc(client, token, 'register_agent', {
        'session_id': session, 'label': 'Project creator', 'agent_kind': 'codex',
        'identity_key': 'creator', 'view': 'full'})


def _worker(browser, grant=None):
    client, board = browser
    policy = _policy() if grant is None else _policy(allow_create_projects=grant)
    response = client.post('/api/agent-profiles', json={
        'name': 'Project creator', 'idempotency_key': 'creator-profile',
        'project_ids': [], 'policy': policy})
    assert response.status_code == 200, response.text
    profile = response.json()
    device = board.create_device(HUMAN, 'Creator API fixture', [])
    response = client.post('/api/agent-profiles/' + profile['id'] + '/bindings', json={
        'expected_version': profile['version'], 'device_id': device['id'],
        'identity_key': 'creator', 'agent_kind': 'codex'})
    assert response.status_code == 200, response.text
    _register(client, device['token'])
    return device['token'], response.json()


def _mcp_error(client, token, name, arguments, contains=''):
    response = client.post('/mcp', headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream'},
        json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
              'params': {'name': name, 'arguments': arguments}})
    assert response.status_code == 200, response.text
    result = response.json()['result']
    assert result.get('isError'), result
    assert contains.lower() in json.dumps(result).lower()


def _creation(**changes):
    return {'session_id': 'creator', 'name': 'Requested project',
            'participants': ['blackoutburst'], 'idempotency_key': 'create-project',
            'view': 'full', **changes}


def test_project_creation_grant_requires_owner_csrf_and_strict_bool(browser):
    client, board = browser
    token, profile = _worker(browser)
    path = '/api/agent-profiles/' + profile['id']
    grant = {'expected_version': profile['version'],
             'policy': _policy(allow_create_projects=True)}
    csrf = client.headers.pop('X-CSRF-Token')
    assert client.patch(path, json=grant).status_code == 403
    client.headers['X-CSRF-Token'] = csrf
    agent = TestClient(client.app)
    assert agent.patch(path, headers={'Authorization': 'Bearer ' + token},
                       json=grant).status_code == 401
    board.ensure_owner('thescreamer', 'TheScreamer', 'other-fixture-password')
    other = TestClient(client.app)
    login = other.post('/api/login', json={
        'username': 'thescreamer', 'password': 'other-fixture-password'})
    assert login.status_code == 200, login.text
    other.headers['X-CSRF-Token'] = login.json()['csrf_token']
    assert other.patch(path, json=grant).status_code == 404
    for invalid in (None, 0, 1, 'true', 'false', [], {}):
        policy = _policy(allow_create_projects=invalid)
        assert client.patch(path, json={
            'expected_version': profile['version'], 'policy': policy}).status_code == 422
        assert client.post('/api/agent-profiles', json={
            'name': 'Invalid', 'idempotency_key': 'invalid', 'policy': policy}).status_code == 422
    unchanged = client.get(path).json()
    assert unchanged['version'] == profile['version']
    assert unchanged['policy'] == profile['policy']
    for value in (True, False):
        previous_version = profile['version']
        profile = _patch(client, profile, policy=_policy(allow_create_projects=value))
        assert profile['version'] == previous_version + 1
        assert profile['policy']['allow_create_projects'] is value
        # An older client sends all original policy fields, omitting the new grant.
        profile = _patch(client, profile, policy=_policy(allow_messages=False))
        assert profile['policy']['allow_create_projects'] is value
        assert profile['policy']['allow_messages'] is False


def test_scoped_creation_receipt_retries_never_expose_project_metadata(browser):
    client, _ = browser
    token, profile = _worker(browser)
    arguments = _creation()
    _mcp_error(client, token, 'create_project', arguments, 'allow project creation')
    assert client.get('/api/projects').json() == []
    profile = _patch(client, profile, policy=_policy(allow_create_projects=True))
    _mcp_error(client, token, 'create_project', arguments, 'profile changed')
    _register(client, token)
    receipt = rpc(client, token, 'create_project', arguments)
    assert receipt == {'id': receipt['id'],
                       'url': 'http://testserver/?project=' + receipt['id'],
                       'profile_scope_access': False, 'owner_scope_grant_required': True}
    current_profile = client.get('/api/agent-profiles/' + profile['id']).json()
    assert current_profile['project_ids'] == []
    assert current_profile['version'] == profile['version']
    _mcp_error(client, token, 'get_project', {
        'session_id': 'creator', 'project_id': receipt['id'], 'view': 'full'}, 'not found')
    project = client.get('/api/projects/' + receipt['id']).json()
    edited = client.patch('/api/projects/' + receipt['id'], json={
        'expected_version': project['version'], 'name': 'Human private project title',
        'description': 'Human private description', 'context': 'Human private context',
        'repository_url': 'https://example.com/private-repository',
        'environment': 'Human private environment'})
    assert edited.status_code == 200, edited.text
    for view in ('compact', 'full'):
        assert rpc(client, token, 'create_project', {**arguments, 'view': view}) == receipt
    assert len(client.get('/api/projects').json()) == 1
    # Even a later explicit scope grant cannot turn the retry into a metadata read.
    profile = _patch(client, profile, project_ids=[receipt['id']])
    _register(client, token)
    assert rpc(client, token, 'create_project', arguments) == {
        **receipt, 'profile_scope_access': True, 'owner_scope_grant_required': False}
    assert rpc(client, token, 'get_project', {
        'session_id': 'creator', 'project_id': receipt['id'], 'view': 'full'})['context'] == 'Human private context'


def test_revocation_fences_existing_session_and_denies_creation_after_registration(browser):
    client, _ = browser
    token, profile = _worker(browser, grant=True)
    arguments = _creation()
    receipt = rpc(client, token, 'create_project', arguments)
    revoked = _patch(client, profile, policy=_policy(allow_create_projects=False))
    assert revoked['version'] == profile['version'] + 1
    _mcp_error(client, token, 'get_work', {'session_id': 'creator'}, 'profile changed')
    registration = _register(client, token)
    assert registration['profile_version'] == revoked['version']
    for key in ('create-project', 'second-project'):
        _mcp_error(client, token, 'create_project', {
            **arguments, 'idempotency_key': key}, 'allow project creation')
    _register(client, token, session='fresh-creator')
    _mcp_error(client, token, 'create_project', {
        **arguments, 'session_id': 'fresh-creator'}, 'allow project creation')
    projects = client.get('/api/projects').json()
    assert [project['id'] for project in projects] == [receipt['id']]
    current = client.get('/api/agent-profiles/' + profile['id']).json()
    assert current['version'] == revoked['version'] and current['project_ids'] == []


def test_legacy_unscoped_creation_survives_omission_until_explicit_revocation(browser):
    client, board = browser
    token = board.create_device(HUMAN, 'Legacy creator fixture', [])['token']
    registration = _register(client, token)
    profile = client.get('/api/agent-profiles/' + registration['identity']['id']).json()
    assert profile['project_ids'] is None
    assert 'allow_create_projects' not in profile['policy']
    project = rpc(client, token, 'create_project', _creation())
    assert project['name'] == 'Requested project'
    assert 'profile_scope_access' not in project
    # A policy edit by an old client also preserves the legacy absent-field meaning.
    profile = _patch(client, profile, policy=_policy())
    assert 'allow_create_projects' not in profile['policy']
    _register(client, token)
    second = rpc(client, token, 'create_project', _creation(idempotency_key='legacy-second'))
    profile = _patch(client, profile, policy=_policy(allow_create_projects=False))
    _register(client, token)
    _mcp_error(client, token, 'create_project', _creation(idempotency_key='legacy-third'),
               'allow project creation')
    assert {item['id'] for item in client.get('/api/projects').json()} == {project['id'], second['id']}
