"""A browser owner's hold stays authoritative through MCP worker maintenance."""
import time

from test_api import application, call_tool, tool_data  # noqa: F401
from test_companion_workflow_api import companion_browser  # noqa: F401


def test_browser_hold_fences_mcp_readiness_and_reopen_until_explicit_owner_action(companion_browser):
    home = companion_browser; board = home['board']; worker = home['worker']
    session = 'owner-hold-api-worker'
    board.register_agent(worker['raw'], session, 'Owner hold API worker', ['python'], 'codex', True, 'API Coder')
    actor = board.actor_session(worker['raw'], session)
    task = board.create_ticket(home['human'], home['project']['id'], 'Owner-held API task',
                                assignee_agent_id=worker['profile']['id'])
    claimed = board.claim_ticket(actor, task['id'], task['version'])
    response = home['client'].patch('/api/tickets/' + task['id'], json={
        'expected_version': claimed['version'], 'status': 'blocked',
        'blocked_reason': 'Nobody take this task', 'recheck_after_seconds': 300})
    assert response.status_code == 200, response.text
    held = response.json()
    with board._tx() as db:
        db.execute('UPDATE tickets SET recheck_at=? WHERE id=?', (time.time()-1, task['id']))

    def call(name, values, *, error=False):
        result = call_tool(home['client'], worker['device']['token'], name,
                           {'session_id': session, **values})
        assert bool(result.get('isError')) == error, result
        return result if error else tool_data(result)

    work = call('get_work', {'project_id': home['project']['id'], 'view': 'full'})
    assert task['id'] not in {item['id'] for item in work['ready']}
    call('update_ticket', {'ticket_id': task['id'], 'expected_version': held['version'],
                           'changes': {'status': 'todo'}}, error=True)
    released = call('release_ticket', {'ticket_id': task['id'], 'expected_version': held['version'],
                                      'idempotency_key': 'release-held-api', 'reason': 'Stopping without reopening', 'view': 'full'})
    assert released['status'] == 'blocked' and released['claimant'] is None
    response = home['client'].patch('/api/tickets/' + task['id'], json={
        'expected_version': released['version'], 'status': 'todo'})
    assert response.status_code == 200, response.text
    reopened = response.json()
    selected = call('claim_selected_queue_ticket', {'ticket_id': task['id'],
                    'expected_version': reopened['version'], 'idempotency_key': 'owner-reopened-api'})
    assert selected['claimed'] and selected['ticket']['id'] == task['id']
