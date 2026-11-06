"""Local synthetic real Board + FastMCP HTTP + actual SDK Runtime client.

No live endpoint, owner database, credential file or model is accessed.
The opener bridge retains the SDK's actual JSON-RPC/HTTP decoding path.
"""
import io
import json
import uuid

import pytest
import httpx

from test_runtime_api import browser  # noqa: F401
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.runtime_client import RuntimeBoardClient
from blackoutburst_agents.runtime_guard import RuntimeControlError
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.review_worker import ReviewRuntimeGuard, _agent_id


SESSION = 'synthetic-review-runtime'
SERVER_CODES = frozenset({'owner_paused', 'capacity_exhausted', 'review_policy_history_unavailable'})


class Response:
    def __init__(self, response, url):
        self.body = io.BytesIO(response.content)
        self.status, self.headers, self.url = response.status_code, response.headers, url

    def __enter__(self): return self
    def __exit__(self, *_): pass
    def geturl(self): return self.url
    def read(self, size): return self.body.read(size)
    read1 = read


class HTTPBridge:
    """Only networking is replaced: real HTTP app /mcp handlers do the work."""
    def __init__(self, http):
        self.http, self.calls, self.errors = http, [], []

    def open(self, request, timeout):
        assert timeout > 0
        payload = json.loads(request.data)
        self.calls.append(payload)
        response = self.http.post('/mcp', headers=dict(request.header_items()), content=request.data)
        if response.status_code == 200:
            result = response.json().get('result', {})
            if isinstance(result, dict) and result.get('isError'):
                self.errors.append(result)
        return Response(response, request.full_url)


@pytest.fixture
def review_client(browser):
    http, board, human, _, project, task, _ = browser
    # The ordinary fixture author has a live in_progress claim already.
    with board._connection() as db:
        device = db.execute("SELECT * FROM devices WHERE label='Fixture host'").fetchone()
        session = db.execute("SELECT * FROM sessions WHERE id='runtime-api'").fetchone()
    author = {'kind': 'agent', 'owner_id': human['owner_id'],
              'device_id': device['id'], 'session_id': session['id']}
    current = board.get_ticket(human, task['id'])['ticket']
    delivered = board.update_ticket(author, task['id'], current['version'], status='review',
                                   outcome='Synthetic delivery', evidence='Synthetic tests', release_claim=True)
    board.set_policy(human, True, True, True, True, ['repo.read_file'], ['repo:agent-board-cloud-pilot'])
    profile = board.create_agent_profile(human, 'Synthetic cloud reviewer', project_ids=[project['id']], policy={
        'allow_read': True, 'allow_messages': True, 'allow_requests': True,
        'actions': ['repo.read_file'], 'resources': ['repo:agent-board-cloud-pilot'],
        'autonomy': 'requested', 'allow_create_projects': False})
    device = board.create_device(human, 'Synthetic reviewer device', [])
    board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'reviewer-key', 'codex')
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, SESSION, 'Synthetic review session', ['review'], 'codex',
                                        False, 'reviewer-key')
    actor = board.actor_session(raw, SESSION)
    claim = board.claim_ticket(actor, delivered['id'], delivered['version'])
    client = RuntimeBoardClient({'mcp_url': 'https://board.test/mcp', 'token': device['token']}, SESSION, 3)
    client._opener = HTTPBridge(http)
    scope = {'owner_id': human['owner_id'], 'agent_id': registration['identity']['id'],
             'project_id': project['id'], 'ticket_id': task['id'],
             'board_endpoint': client.endpoint, 'board_session_id': SESSION}
    yield client, board, human, actor, scope, claim
    client.close()


def args(scope, key='synthetic-reservation'):
    return {'idempotency_key': key, 'project_id': scope['project_id'], 'ticket_id': scope['ticket_id'],
            'work_id': str(uuid.uuid4()), 'turn_id': str(uuid.uuid4()),
            'provider': 'codex_chatgpt', 'model': 'gpt-6.1-sol'}


def test_actual_review_arguments_cross_real_mcp_boundary(review_client):
    client, board, human, _, scope, _ = review_client
    values = args(scope)
    result = client.call('reserve_execution', values)
    assert result['reserved'] is True and result['retried'] is False
    execution = result['execution']
    running = client.call('start_execution', {'execution_id': execution['id'], 'expected_revision': execution['revision']})
    assert running['state'] == 'running'
    finished = client.call('finish_execution', {'execution_id': running['id'], 'expected_revision': running['revision'],
                           'status': 'completed', 'cleanup_confirmed': True, 'reason': 'completed'})
    assert finished['state'] == 'finished' and board.get_runtime(human)['active_count'] == 0
