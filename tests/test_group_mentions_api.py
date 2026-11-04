"""Web and MCP posts use the same post-time group expansion and delivery."""
from test_api import application, call_tool, tool_data  # noqa: F401
from test_companion_workflow_api import companion_browser  # noqa: F401


def test_browser_group_picker_and_post_deliver_to_companion_and_worker(companion_browser):
    home = companion_browser; board = home['board']; worker = home['worker']
    thread = board.create_thread(home['human'], 'Browser groups', ['blackoutburst', 'thescreamer'], 'browser-groups')
    targets = home['client'].get('/api/threads/' + thread['id'] + '/mentionables').json()
    assert {'group:human', 'group:agent'} <= {target['id'] for target in targets}
    response = home['client'].post('/api/threads/' + thread['id'] + '/messages', json={
        'body': 'Selected all agents', 'mentions': ['group:agent'], 'idempotency_key': 'web-group'})
    assert response.status_code == 200, response.text
    message = response.json()
    assert set(message['mentions']) == {'agent:' + home['profile']['id'], 'agent:' + worker['profile']['id'],
                                       'agent:' + home['second']['profile']['id']}
    for device, session in ((home['device'], 'companion-api'),
                            (worker['device'], worker['actor']['session_id'])):
        result = call_tool(home['client'], device['token'], 'read_inbox', {
            'session_id': session, 'label': 'Group receiver', 'include_work': False, 'view': 'full'})
        assert not result.get('isError'), result
        assert message['id'] in {item['id'] for item in tool_data(result)['messages']}


def test_mcp_typed_and_selected_groups_expand_under_agent_author(companion_browser):
    home = companion_browser; worker = home['worker']; board = home['board']
    thread = board.create_thread(home['human'], 'MCP groups', ['blackoutburst', 'thescreamer'], 'mcp-groups')
    board.post_message(home['human'], thread['id'], 'Initial invitation', mentions=['agent:' + worker['profile']['id']])
    for suffix, body, mentions in (('typed', '@human @agent Group request', []),
                                    ('selected', 'Selected groups', ['group:human', 'group:agent'])):
        result = call_tool(home['client'], worker['device']['token'], 'post_message', {
            'session_id': worker['actor']['session_id'], 'thread_id': thread['id'],
            'body': body, 'mentions': mentions, 'idempotency_key': 'mcp-group-' + suffix, 'view': 'full'})
        assert not result.get('isError'), result
        assert set(tool_data(result)['mentions']) == {'blackoutburst', 'thescreamer'}
