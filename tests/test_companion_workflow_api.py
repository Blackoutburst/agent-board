"""Real HTTP/MCP companion powers retain authentication and attribution."""
from fastapi.testclient import TestClient
import pytest

from test_api import application, login, rpc, call_tool, tool_data
from test_companion_workflow import policy, running_profile


@pytest.fixture
def companion_browser(application):
    board = application.state.board
    with TestClient(application) as client:
        human = login(client)
        peer = {'kind': 'human', 'owner_id': 'thescreamer'}
        for owner in (human, peer):
            board.set_policy(owner, True, True, True, True, [], [])
        project = board.create_project(human, 'Companion API', ['blackoutburst', 'thescreamer'])
        response = client.post('/api/agent-profiles', json={
            'name': 'Personal companion', 'kind': 'companion', 'idempotency_key': 'api-companion',
            'policy': policy(), 'project_ids': []})
        assert response.status_code == 200, response.text
        profile = response.json()
        device = board.create_device(human, 'Companion API host', [])
        response = client.post('/api/agent-profiles/' + profile['id'] + '/bindings', json={
            'expected_version': profile['version'], 'device_id': device['id'],
            'identity_key': 'companion', 'agent_kind': 'codex'})
        assert response.status_code == 200, response.text
        profile = response.json()
        raw = board.authenticate(device['token'])
        board.register_agent(raw, 'companion-api', 'Companion API', [], 'codex', True, 'companion')
        worker = running_profile(board, human, 'API Coder', [project['id']])
        second = running_profile(board, human, 'API Second', [project['id']])
        yield dict(client=client, board=board, human=human, peer=peer, project=project,
                   profile=profile, device=device, worker=worker, second=second)


def call(home, name, values=None, *, error=False):
    result = call_tool(home['client'], home['device']['token'], name,
                       {'session_id': 'companion-api', **(values or {})})
    if error:
        assert result.get('isError'), result
        return result
    return tool_data(result)


def ticket(home, title='API work', **fields):
    return home['board'].create_ticket(home['human'], home['project']['id'], title, **fields)


def review(home):
    board = home['board']; worker = home['worker']
    created = ticket(home, 'Review result', assignee_agent_id=worker['profile']['id'])
    claimed = board.claim_ticket(worker['actor'], created['id'], created['version'])
    result = board.update_ticket(worker['actor'], created['id'], claimed['version'],
                                  status='review', outcome='Implemented result', evidence='Synthetic proof')
    return board.release_ticket(worker['actor'], created['id'], result['version'],
                                  'api-review-release', 'Ready for owner review', status='review')


def assert_author(author, home):
    assert author['kind'] == 'agent'
    assert author['owner_id'] == author['companion_for'] == author['acts_for'] == 'blackoutburst'
    assert author['profile_kind'] == 'companion'
    assert author['agent_id'] == home['profile']['id']
    assert author['agent_name'] == 'Personal companion'
    assert author['display_name'] == 'Blackoutburst'


def test_human_companion_creation_is_idempotent_unique_and_kind_is_immutable(application):
    with TestClient(application) as client:
        login(client)
        payload = {'name': 'My companion', 'kind': 'companion', 'idempotency_key': 'one-companion'}
        created = client.post('/api/agent-profiles', json=payload)
        assert created.status_code == 200, created.text
        profile = created.json()
        assert profile['kind'] == 'companion' and profile['acts_for'] == 'blackoutburst'
        assert client.post('/api/agent-profiles', json=payload).json() == profile
        duplicate = client.post('/api/agent-profiles', json=payload | {'idempotency_key': 'second-companion'})
        assert duplicate.status_code == 409
        path = '/api/agent-profiles/' + profile['id']
        changed = client.patch(path, json={'expected_version': profile['version'], 'kind': 'agent'})
        assert changed.status_code == 422
        assert client.get(path).json() == profile


def test_new_mcp_tools_are_advertised_with_bounded_inputs(companion_browser):
    home = companion_browser
    tools = rpc(home['client'], home['device']['token'], 'tools/list').json()['result']['tools']
    tools = {tool['name']: tool for tool in tools}
    assert {'resolve_human_request', 'set_companion_agent_status'} <= tools.keys()
    status = tools['set_companion_agent_status']['inputSchema']['properties']
    assert set(status['status']['enum']) == {'active', 'paused'}
    assert 'expected_version' in tools['set_companion_agent_status']['inputSchema']['required']


def test_mcp_dispatch_review_and_ordinary_claim_refusal(companion_browser):
    home = companion_browser; board = home['board']
    created = call(home, 'create_ticket', {
        'project_id': home['project']['id'], 'title': 'Dispatched through MCP',
        'assignee_agent_id': home['worker']['profile']['id'], 'idempotency_key': 'dispatch', 'view': 'full'})
    changed = call(home, 'update_ticket', {
        'ticket_id': created['id'], 'expected_version': created['version'],
        'changes': {'assignee_agent_id': home['second']['profile']['id']}, 'view': 'full'})
    assert changed['assignee_agent_id'] == home['second']['profile']['id']
    assert changed['claimant'] is None
    call(home, 'claim_ticket', {'ticket_id': created['id'], 'expected_version': changed['version']}, error=True)
    call(home, 'claim_next_ticket', {'idempotency_key': 'companion-queue'}, error=True)
    old = board.create_task(home['human'], created['thread_id'], 'Legacy task',
                              'Inspect source', [], 'group', 'blackoutburst')
    call(home, 'claim_task', {'task_id': old['id']}, error=True)
    delivered = review(home)
    accepted = call(home, 'update_ticket', {'ticket_id': delivered['id'],
        'expected_version': delivered['version'], 'changes': {'status': 'done'}, 'view': 'full'})
    assert accepted['status'] == 'done' and accepted['claimant'] is None
    assert accepted['outcome'] == 'Implemented result' and accepted['evidence'] == 'Synthetic proof'
    call(home, 'update_ticket', {'ticket_id': created['id'], 'expected_version': changed['version'],
        'changes': {'progress': 'Invented implementation'}}, error=True)
    assert board.get_ticket(home['human'], created['id'])['ticket']['version'] == changed['version']


def test_mcp_resolves_only_owner_needs_you_and_keeps_companion_identity(companion_browser):
    home = companion_browser; board = home['board']
    own_ticket = ticket(home, 'Owner input')
    own = board.request_human_input(home['human'], own_ticket['id'], 'blackoutburst',
                                      'Pick an owner preference', 'api-own-request')
    other_ticket = ticket(home, 'Peer input')
    other = board.request_human_input(home['peer'], other_ticket['id'], 'thescreamer',
                                        'Pick a peer preference', 'api-peer-request')
    call(home, 'resolve_human_request', {'request_id': other['id'], 'answer': 'Impersonate peer'}, error=True)
    result = call(home, 'resolve_human_request', {
        'request_id': own['id'], 'answer': 'Use the owner preference', 'view': 'full'})
    assert result['status'] == 'resolved' and result['answer'] == 'Use the owner preference'
    thread = call(home, 'get_thread', {'thread_id': own_ticket['thread_id'], 'view': 'full'})
    answer = next(message for message in thread['messages'] if 'Use the owner preference' in message['body'])
    assert_author(answer['author'], home)
    with board._connection() as db:
        assert db.execute('SELECT status FROM human_requests WHERE id=?', (other['id'],)).fetchone()[0] == 'open'


def test_mcp_agent_status_is_bounded_versioned_and_cannot_edit_policy(companion_browser):
    home = companion_browser; board = home['board']; target = home['worker']['profile']
    values = dict(profile_id=target['id'], expected_version=target['version'],
                  status='paused', idempotency_key='api-pause')
    paused = call(home, 'set_companion_agent_status', values)
    assert paused['status'] == 'paused'
    assert not set(paused) & {'policy', 'instructions', 'bindings'}
    assert call(home, 'set_companion_agent_status', values) == paused
    call(home, 'set_companion_agent_status', values | {'idempotency_key': 'stale', 'status': 'active'}, error=True)
    resumed = call(home, 'set_companion_agent_status', values | {
        'expected_version': paused['version'], 'status': 'active', 'idempotency_key': 'api-resume'})
    assert resumed['status'] == 'active'
    call(home, 'set_companion_agent_status', values | {
        'expected_version': resumed['version'], 'idempotency_key': 'policy-injection',
        'policy': {'allow_requests': True}}, error=True)
    assert board.get_agent_profile(home['human'], target['id'])['policy'] == target['policy']
    assert board.get_agent_profile(home['human'], target['id'])['status'] == 'active'


@pytest.mark.parametrize('view', ['compact', 'full'])
def test_mcp_message_and_activity_views_preserve_distinct_attribution(companion_browser, view):
    home = companion_browser
    created = call(home, 'create_ticket', {'project_id': home['project']['id'],
        'title': 'Attribution', 'idempotency_key': 'attribution', 'view': 'full'})
    posted = call(home, 'post_message', {'thread_id': created['thread_id'],
        'body': 'Companion coordination', 'idempotency_key': 'message-attribution', 'view': view})
    assert_author(posted['author'], home)
    thread = call(home, 'get_thread', {'thread_id': created['thread_id'], 'view': view})
    assert_author(next(message for message in thread['messages'] if message['id'] == posted['id'])['author'], home)
    activity = call(home, 'list_activity', {'ticket_id': created['id'], 'view': view})
    rows = activity['result'] if view == 'full' else activity['items']
    assert_author(next(row for row in rows if row['event'] == 'ticket_created')['actor'], home)


def test_companion_bearer_never_opens_human_settings_profile_or_pairing_routes(companion_browser):
    home = companion_browser
    agent = TestClient(home['client'].app)
    try:
        agent.headers['Authorization'] = 'Bearer ' + home['device']['token']
        assert agent.get('/api/settings').status_code == 401
        assert agent.get('/api/agent-profiles').status_code == 401
        assert agent.post('/api/agent-profiles', json={
            'name': 'Unauthorized', 'kind': 'companion', 'idempotency_key': 'unauthorized'}).status_code == 401
        assert agent.post('/api/settings/pairings', json={
            'label': 'Unauthorized pairing', 'capabilities': []}).status_code == 401
        assert agent.put('/api/settings/policy', json={
            'enabled': True, 'allow_read': True, 'allow_reply': True,
            'allow_requests': True, 'actions': [], 'resources': []}).status_code == 401
    finally:
        agent.close()


def test_mcp_companion_presentation_follows_the_app_and_nothing_else(companion_browser):
    home = companion_browser; board = home['board']
    tools = {tool['name']: tool for tool in rpc(home['client'], home['device']['token'], 'tools/list').json()['result']['tools']}
    assert set(tools['update_companion_profile']['inputSchema']['properties']) == {'session_id', 'name', 'avatar', 'description'}
    updated = call(home, 'update_companion_profile', {'name': 'Claude', 'avatar': '✳️', 'description': "Blackout's companion"})
    assert updated['name'] == 'Claude' and updated['avatar'] == '✳️' and updated['kind'] == 'companion'
    assert updated['version'] == home['profile']['version'] + 1
    stored = board.get_agent_profile(home['human'], home['profile']['id'])
    assert stored['description'] == "Blackout's companion" and stored['policy'] == home['profile']['policy']
    assert stored['project_ids'] == home['profile']['project_ids'] and stored['status'] == 'active'
    # The calling session stays current, and an unchanged update is a no-op.
    again = call(home, 'update_companion_profile', {'name': 'Claude'})
    assert again['version'] == updated['version']
    call(home, 'list_projects')
    # Nothing but presentation can be sent, and ordinary agents cannot use it.
    call(home, 'update_companion_profile', {'status': 'paused'}, error=True)
    call(home, 'update_companion_profile', {}, error=True)
    call(home, 'update_companion_profile', {'avatar': 'https://x'}, error=True)
    worker = home['worker']
    with pytest.raises(Exception, match='Only a companion'):
        board.update_companion_profile(worker['actor'], name='Renamed')
