"""Browser mention selection and authenticated MCP delivery share the invitation."""
from test_api import application, call_tool, tool_data  # noqa: F401
from test_companion_workflow_api import companion_browser  # noqa: F401


def test_browser_owner_can_invite_scoped_worker_and_mcp_can_read(companion_browser):
    home = companion_browser; board = home['board']; worker = home['worker']
    thread = board.create_thread(home['human'], 'Standalone API invitation',
                                 ['blackoutburst', 'thescreamer'], 'api-standalone-invite')
    target = 'agent:' + worker['profile']['id']
    response = home['client'].get('/api/threads/' + thread['id'] + '/mentionables')
    assert response.status_code == 200
    assert target in {item['id'] for item in response.json()}
    session = worker['actor']['session_id']

    def call(name, values):
        result = call_tool(home['client'], worker['device']['token'], name,
                           {'session_id': session, **values})
        assert not result.get('isError'), result
        return tool_data(result)

    before = call_tool(home['client'], worker['device']['token'], 'get_thread',
                       {'session_id': session, 'thread_id': thread['id']})
    assert before.get('isError')
    message = board.post_message(home['human'], thread['id'], 'A real owner invitation through the shared database',
                                  mentions=[target])
    inbox = call('read_inbox', {'label': 'Invited API worker', 'include_work': False, 'view': 'full'})
    assert [item['id'] for item in inbox['messages']] == [message['id']]
    assert call('get_thread', {'thread_id': thread['id'], 'view': 'full'})['messages'][0]['id'] == message['id']
    assert call('get_text', {'kind': 'message', 'identifier': message['id'], 'field': '/body'})['text'] == message['body']
