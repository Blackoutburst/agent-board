"""Presence and idle assignment recovery survive the actual MCP transport."""
import sqlite3
import time

from test_coordination_api import browser, rpc


def setup_workers(browser):
    client, board = browser
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    project = board.create_project(human, 'Idle API', ['blackoutburst'], idempotency_key='idle-api')
    workers = []
    for name in ('source', 'successor'):
        token = board.create_device(human, name, [])['token']
        registration = rpc(client, token, 'register_agent', {
            'session_id': name, 'label': name, 'agent_kind': 'codex',
            'identity_key': name, 'autonomous': True,
        })
        workers.append((token, registration['identity']['id']))
    ticket = board.create_ticket(human, project['id'], 'Stranded assignment',
                                assignee_agent_id=workers[0][1], idempotency_key='stranded')
    return client, board, human, project, ticket, workers


def make_idle(board, profile_id, ticket_id, *, expired=False):
    old = time.time() - 3600
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE sessions SET last_seen_at=? WHERE agent_id=?', (old, profile_id))
        db.execute('UPDATE tickets SET updated_at=? WHERE id=?', (old, ticket_id))
        if expired:
            db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?', (old, ticket_id))
    return old


def reject_rpc(client, token, name, arguments):
    response = client.post('/mcp', headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/json, text/event-stream',
    }, json={
        'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments},
    })
    assert response.status_code == 200
    result = response.json()['result']
    assert result.get('isError'), result
    return result


def test_mcp_presence_views_mentions_and_idle_queue_handoff(browser):
    client, board, human, project, ticket, workers = setup_workers(browser)
    (_, source_id), (token, successor_id) = workers
    before = rpc(client, token, 'list_agents', {'session_id': 'successor', 'project_id': project['id']})
    assert next(item for item in before['items'] if item['id'] == source_id)['presence'] == 'online'
    old = make_idle(board, source_id, ticket['id'])
    expected = {'presence': 'idle', 'online': False, 'last_seen_at': old, 'idle_after_seconds': 900}
    for view in ('compact', 'full'):
        agents = rpc(client, token, 'list_agents', {
            'session_id': 'successor', 'project_id': project['id'], 'view': view,
        })
        source = next(item for item in agents['items' if view == 'compact' else 'result'] if item['id'] == source_id)
        assert {field: source[field] for field in expected} == expected
        assert not {'sessions', 'tickets', 'device_id', 'instructions', 'policy'} & source.keys()
        mentions = rpc(client, token, 'list_mention_targets', {
            'session_id': 'successor', 'thread_id': ticket['thread_id'], 'view': view,
        })
        target = next(item for item in mentions['items' if view == 'compact' else 'result'] if item['id'] == 'agent:' + source_id)
        assert {field: target[field] for field in expected} == expected
    work = rpc(client, token, 'get_work', {'session_id': 'successor', 'project_id': project['id']})
    assert len(work['ready']) == 1 and not work['recovery']
    summary = work['ready'][0]
    assert summary['id'] == ticket['id'] and summary['assignment_stale']
    assert summary['assignee_presence'] == expected
    assert summary['assignee_agent_id'] == source_id and summary['detail_tool'] == 'get_ticket'
    # Read-only MCP views do not silently unassign anything.
    assert board.get_ticket(human, ticket['id'])['ticket']['assignee_agent_id'] == source_id
    selected = rpc(client, token, 'claim_next_ticket', {
        'session_id': 'successor', 'project_id': project['id'], 'idempotency_key': 'idle-selection',
    })
    assert selected['claimed']
    claimed = selected['ticket']
    assert claimed['id'] == ticket['id'] and claimed['claimant']['agent_id'] == successor_id
    assert claimed['assignee_agent_id'] is None and not claimed['assignment_stale']
    assert claimed['assignee_presence'] is None


def test_mcp_expired_idle_claim_requires_explicit_recovery_evidence(browser):
    client, board, human, project, ticket, workers = setup_workers(browser)
    (source_token, source_id), (token, successor_id) = workers
    original = rpc(client, source_token, 'claim_ticket', {
        'session_id': 'source', 'ticket_id': ticket['id'], 'expected_version': ticket['version'],
    })
    make_idle(board, source_id, ticket['id'], expired=True)
    work = rpc(client, token, 'get_work', {'session_id': 'successor', 'project_id': project['id']})
    assert not work['ready'] and len(work['recovery']) == 1
    recovery = work['recovery'][0]
    assert recovery['id'] == ticket['id'] and recovery['work_state'] == 'recovery'
    assert recovery['claim_expired'] and recovery['assignment_stale']
    assert recovery['assignee_presence']['presence'] == 'idle'
    assert not rpc(client, token, 'claim_next_ticket', {
        'session_id': 'successor', 'project_id': project['id'], 'idempotency_key': 'no-auto-recovery',
    })['claimed']
    arguments = {'session_id': 'successor', 'ticket_id': ticket['id'], 'expected_version': original['version']}
    for evidence in (None, '   '):
        supplied = arguments if evidence is None else {**arguments, 'recovery_evidence': evidence}
        reject_rpc(client, token, 'claim_ticket', supplied)
        current = board.get_ticket(human, ticket['id'])['ticket']
        assert current['version'] == original['version'] and current['claimant']['agent_id'] == source_id
    recovered = rpc(client, token, 'claim_ticket', {
        **arguments, 'recovery_evidence': 'Inspected prior checkout and remote effects; no execution or pending writes remain.',
    })
    assert recovered['claimant']['agent_id'] == successor_id
    assert recovered['assignee_agent_id'] is None and not recovered['claim_expired']
    reject_rpc(client, source_token, 'renew_ticket', {
        'session_id': 'source', 'ticket_id': ticket['id'], 'expected_version': recovered['version'],
    })
