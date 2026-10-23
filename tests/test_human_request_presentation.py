"""Short human handoffs keep their exact source, retry identity and access rules."""
import hashlib
import json

import pytest

from board import Board, BoardError
from mcp_views import MCPViews, compact, wire
from test_coordination import space, new
from test_coordination_api import browser, rpc


def test_presentation_survives_restart_retry_answer_and_old_client_payloads(space):
    board, human, other, project, agent, *_ = space
    task = new(space)
    board.claim_ticket(agent, task['id'], task['version'])
    source = ('Which rollout?\n\n' + 'Background evidence. ' * 400).rstrip()
    command = "printf '%s\\n' 'été 日本語'\n"
    args = (agent, task['id'], 'thescreamer', source, 'presentation')
    extra = dict(summary='Which rollout?', choices=['Staging first', 'Production now'], command=command)
    request = board.request_human_input(*args, **extra)
    assert request['question'] == source and request['command'] == command
    assert request['choices'] == [{'id': 'A', 'label': 'Staging first'}, {'id': 'B', 'label': 'Production now'}]
    reopened = Board(board.data_dir, base_url=board.base_url)
    assert reopened.lookup_human_request(*args, **extra) == request
    assert reopened.request_human_input(*args, **extra) == request
    assert reopened.get_ticket(human, task['id'])['ticket']['human_requests'][0]['summary'] == extra['summary']
    assert reopened.list_human_requests(other, project['id'])[0]['command'] == command
    with pytest.raises(BoardError) as denied:
        reopened.resolve_human_request(human, request['id'], 'Staging first')
    assert denied.value.status == 403
    answered = reopened.resolve_human_request(other, request['id'], 'A custom answer instead')
    assert answered['answer'] == 'A custom answer instead' and answered['choices'] == request['choices']
    assert reopened.lookup_human_request(*args, **extra) == answered
    with pytest.raises(BoardError) as changed:
        reopened.lookup_human_request(*args, **{**extra, 'choices': ['Different', 'Answers']})
    assert changed.value.status == 409
    # Omitting the new fields keeps the original, pre-release retry identity.
    old = new(space, 'Old client')
    legacy = board.request_human_input(human, old['id'], 'thescreamer', 'Unchanged source', 'legacy')
    assert board.request_human_input(human, old['id'], 'thescreamer', 'Unchanged source', 'legacy',
        summary='', choices=[], command='') == legacy


@pytest.mark.parametrize('extra', [
    {'summary': 'x' * 241}, {'summary': 'Two\nlines'}, {'choices': ['Only one']},
    {'choices': ['Same', 'Same']}, {'choices': [' ', 'Other']}, {'choices': ['A', 3]},
    {'choices': ['A\nB', 'Other']}, {'choices': ['x' * 161, 'Other']},
    {'choices': list('ABCDEFG')}, {'command': 'x' * 20001},
])
def test_invalid_presentation_leaves_claim_and_request_unchanged(space, extra):
    board, human, _, _, agent, *_ = space
    task = new(space)
    claimed = board.claim_ticket(agent, task['id'], task['version'])
    with pytest.raises(BoardError):
        board.request_human_input(agent, task['id'], 'thescreamer', 'Question?', 'invalid', **extra)
    current = board.get_ticket(human, task['id'])['ticket']
    assert current['claimant'] == claimed['claimant'] and current['version'] == claimed['version']
    assert current['human_requests'] == []


def test_large_unicode_presentation_has_bounded_wire_and_exact_authorized_text(space):
    board, human, _, _, _, *_ = space
    task = new(space)
    command = '😀' * 20000
    request = board.request_human_input(human, task['id'], 'thescreamer', 'Question?', 'unicode',
        summary='😀' * 240, choices=[str(i) + '😀' * 159 for i in range(6)], command=command)
    shown = compact(request)
    assert len(json.dumps(wire(shown).model_dump(mode='json')).encode()) < 20000
    assert shown['choices'][0]['id'] == 'A' and 'label' not in shown['choices'][0]
    view = MCPViews(board)
    assert view.get_text(human, 'human_request', request['id'], '/choices/0/label')['text'] == request['choices'][0]['label']
    chunk = view.get_text(human, 'human_request', request['id'], '/command', limit=4096)
    assert chunk['sha256'] == hashlib.sha256(command.encode()).hexdigest()
    assert chunk['next_offset'] is not None
    with pytest.raises(BoardError):
        view.get_text({'kind': 'human', 'owner_id': 'outsider'}, 'human_request', request['id'], '/command')


def test_rest_and_mcp_expose_the_same_request_choices_and_command(browser):
    client, board = browser
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    project = board.create_project(human, 'Requests', ['blackoutburst'], idempotency_key='present-project')
    task = board.create_ticket(human, project['id'], 'REST', idempotency_key='present-rest')
    values = dict(owner_id='blackoutburst', question='Full context', summary='Pick one',
        choices=['One', 'Two'], command='printf ok\n', idempotency_key='rest')
    response = client.post('/api/tickets/' + task['id'] + '/input', json=values)
    assert response.status_code == 200
    request = response.json()
    assert request['summary'] == 'Pick one' and request['command'] == 'printf ok\n'
    token = board.create_device(human, 'Presentation worker', [])['token']
    rpc(client, token, 'register_agent', dict(session_id='presentation-api', label='Presentation', agent_kind='codex'))
    second = board.create_ticket(human, project['id'], 'MCP', idempotency_key='present-mcp')
    claim = rpc(client, token, 'claim_ticket', dict(session_id='presentation-api', ticket_id=second['id'], expected_version=second['version']))
    args = dict(session_id='presentation-api', ticket_id=second['id'], **{**values, 'idempotency_key': 'mcp'})
    created = rpc(client, token, 'request_human_input', dict(**args, expected_version=claim['version'], view='full'))
    assert created['choices'] == request['choices'] and created['command'] == request['command']
    assert rpc(client, token, 'lookup_human_request', dict(**args, view='full')) == created
