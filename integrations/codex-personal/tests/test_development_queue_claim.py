"""Actual worker claim state machine and Board; no native work or models.

Run with the new Board API combined with this SDK. Old standalone sources
explicitly skip these composition cases instead of faking the atomic barrier.
"""
from copy import deepcopy
from contextlib import closing
import threading

import pytest

from board import Board
from test_development_worker import FixtureClient
from test_runtime_integration import ControlClient
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.dev_journal import DevelopmentJournal
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, prepare_manifest
from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.dev_manifest import canonical
from blackoutburst_agents.dev_journal import PrivateDirectory


pytestmark = pytest.mark.skipif(not hasattr(Board, 'claim_selected_queue_ticket'),
                                reason='Requires the combined Board selected-queue API source')


@pytest.fixture
def pair():
    from test_development_worker import DevelopmentWorkerTests
    first = DevelopmentWorkerTests(); first.setUp()
    second = DevelopmentWorkerTests(); second.setUp()
    try:
        profile = first.board.get_agent_profile(first.human, first.scope['agent_id'])
        first.board.set_policy(first.human, True, True, True, True,
            ['repo.read_file', 'repo.patch_file', 'repo.run_test'], ['repo:fixture', 'repo:second'])
        profile = first.board.update_agent_profile(first.human, profile['id'], profile['version'],
                                                  policy={**profile['policy'], 'autonomy': 'queue'})
        first.ticket = first.board.update_ticket(first.human, first.ticket['id'], first.ticket['version'], resource='repo:fixture')
        ticket2 = first.board.create_ticket(first.human, first.project['id'], 'Second queue work',
                                            resource='repo:second', idempotency_key='queue-second')
        for index, (fixture, ticket, resource) in enumerate([(first, first.ticket, 'repo:fixture'),
                                                            (second, ticket2, 'repo:second')]):
            fixture.board = first.board; fixture.raw = first.raw; fixture.ticket = ticket
            fixture.project = first.project; fixture.human = first.human
            client = FixtureClient(first.board, first.raw, 'queue-worker-' + str(index))
            client._queue_admission = True
            def register(client=client):
                value = client.board.register_agent(client.raw, client.session_id, 'Queue fixture', [], 'codex', True, 'worker')
                client.actor = client.board.actor_session(client.raw, client.session_id)
                client.registration = value
                return value
            client.register_initial = register; fixture.client = client
            registration = register()
            fixture.scope = dict(owner_id='owner', agent_id=profile['id'], project_id=first.project['id'],
                ticket_id=ticket['id'], board_endpoint=client.endpoint, board_session_id=client.session_id)
            scope = {key: fixture.scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')}
            scope.update(device_id=registration['identity']['device_id'], session_id=client.session_id,
                         profile_version=profile['version'])
            fixture.broker_dir = fixture.base / 'queue-broker'
            prepare_manifest(fixture.broker_dir, root=fixture.root, base_sha='a' * 40, scope=scope,
                paths=['main.py'], resource=resource, authorize_scope=lambda *_: True,
                tests=[NamedTest('unit', backend_id=fixture.backend.backend_id)])
            fixture.control = ControlClient(first.board, first.raw, client.session_id)
            fixture.worker = fixture.make_worker()
            fixture.admission = dict(schema='development-queue-admission-v1', ticket_version=ticket['version'],
                                     profile_version=profile['version'], catalog_sha256='a' * 64)
        yield first, second
    finally:
        second.doCleanups(); first.doCleanups()


def prepare(fixture):
    prepared = fixture.prepare(queue_admission=fixture.admission)
    return fixture.journal.get(prepared['operation_id'])


def claim(fixture, op):
    # Concrete SQLite handles belong to their caller thread, just like Pool.
    with WorkJournal(fixture.journal.directory) as journal, closing(DevelopmentJournal(fixture.broker_dir)) as broker:
        worker = fixture.make_worker(journal=journal)
        authorization = DevelopmentAuthorization(fixture.client, scope=fixture.scope, manifest=broker.manifest,
                                                  registration=op['payload']['registration'])
        with journal.lock(fixture.scope):
            return worker._claim(journal.get(op['id']), authorization)


def test_queue_worker_uses_only_selected_claim_and_persisted_operation_key(pair):
    first, _ = pair; op = prepare(first)
    assert not first.inferences
    first.client.lost.add('claim_selected_queue_ticket')
    finished = claim(first, op)
    assert finished['stage'] == 'prepared'
    attempts = [(name, args) for name, args in first.client.calls if 'claim' in name]
    assert len(attempts) == 1 and attempts[0][0] == 'claim_selected_queue_ticket'
    assert attempts[0][1]['idempotency_key'] == 'development-queue-claim-' + op['id']
    assert finished['payload']['claim_intent']['idempotency_key'] == attempts[0][1]['idempotency_key']
    assert claim(first, finished)['stage'] == 'prepared'
    assert len([(name, args) for name, args in first.client.calls if 'claim' in name]) == 1
    assert not first.inferences and first.board.get_runtime(first.human)['executions'] == []


def test_two_jobs_same_profile_different_resources_have_only_one_atomic_claim(pair):
    first, second = pair; operations = [prepare(first), prepare(second)]
    barrier = threading.Barrier(2); outcomes = []; guard = threading.Lock()
    for fixture in pair:
        original = fixture.client.call
        def call(name, args, original=original):
            if name == 'claim_selected_queue_ticket': barrier.wait(timeout=5)
            return original(name, args)
        fixture.client.call = call
    def run(fixture, op):
        try: result = claim(fixture, op)['stage']
        except DevelopmentError as error: result = error.code
        with guard: outcomes.append(result)
    threads = [threading.Thread(target=run, args=(fixture, op)) for fixture, op in zip(pair, operations)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=8)
    assert not any(thread.is_alive() for thread in threads)
    assert outcomes.count('prepared') == 1
    assert len(outcomes) == 2 and 'development_queue_unfinished_work' in outcomes
    tickets = [first.board.get_ticket(first.human, fixture.ticket['id'])['ticket'] for fixture in pair]
    assert sum(ticket['claimant'] is not None for ticket in tickets) == 1
    assert all(not fixture.inferences for fixture in pair)
    assert first.board.get_runtime(first.human)['executions'] == []


def test_changed_version_or_companion_metadata_refuses_before_claim(pair):
    first, second = pair
    first.board.update_ticket(first.human, first.ticket['id'], first.ticket['version'], description='Changed after discovery')
    with pytest.raises(DevelopmentError, match='development_queue_admission_changed'): prepare(first)
    original = second.client.register_initial
    def companion():
        value = deepcopy(original()); value['identity']['companion_for'] = 'server-derived-future-companion'
        return value
    second.client.register_initial = companion
    with pytest.raises(DevelopmentError, match='development_queue_admission_changed'): prepare(second)
    assert not any('claim' in name for fixture in pair for name, _ in fixture.client.calls)
    assert all(not fixture.inferences for fixture in pair)


def test_queue_claim_intent_cannot_fall_back_to_manual_claim(pair):
    first, _ = pair; op = prepare(first)
    with first.journal.lock(first.scope):
        op = first.worker._save(op, 'claiming', claim_intent=dict(action='claim_ticket',
            expected_version=first.ticket['version'], previous_lease=None))
    with pytest.raises(DevelopmentError, match='development_queue_claim_intent_changed'): claim(first, op)
    assert not any('claim' in name for name, _ in first.client.calls)


def test_real_narrow_client_registration_tool_schema_and_selected_claim(tmp_path):
    from app import create_app
    from fastapi.testclient import TestClient
    root = tmp_path.resolve(); board = Board(root / 'board', base_url='https://board.test')
    board.ensure_owner('owner', 'Owner'); human = dict(kind='human', owner_id='owner')
    actions = ['repo.read_file', 'repo.run_test']
    board.set_policy(human, True, True, True, True, actions, ['repo:work'])
    project = board.create_project(human, 'Queue', ['owner'], idempotency_key='project')
    profile = board.create_agent_profile(human, 'Coder', 'coder', project_ids=[project['id']], policy=dict(
        allow_read=True, allow_messages=True, allow_requests=True, autonomy='queue', actions=actions, resources=['repo:work']))
    device = board.create_device(human, 'Coder device', [])
    board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'coder', 'codex')
    ticket = board.create_ticket(human, project['id'], 'Selected queue job', resource='repo:work', idempotency_key='ticket')
    private = root / 'private'; private.mkdir(mode=0o700); connection = private / 'connection.json'
    with PrivateDirectory(private) as directory:
        directory.create(connection.name, canonical(dict(mcp_url='https://board.test/mcp', token=device['token'],
            client='codex', identity_key='coder', name='Coder')))
    application = create_app(root / 'board', base_url='https://board.test', filebrowser_url='')
    class Reply:
        def __init__(self, response, url):
            self.status, self.headers, self.raw, self.url = response.status_code, response.headers, response.content, url
            self.offset = 0
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def geturl(self): return self.url
        def read(self, count):
            data = self.raw[self.offset:self.offset + count]; self.offset += len(data); return data
        read1 = read
    with TestClient(application) as browser:
        class Router:
            def open(self, request, timeout):
                response = browser.request(request.get_method(), request.full_url,
                                           headers=dict(request.header_items()), content=request.data)
                return Reply(response, request.full_url)
            def close(self): pass
        with DevelopmentBoardClient.from_connection_file(connection, session_id='real-queue', queue_admission=True) as client:
            client._opener.close(); client._opener = Router()
            assert client.register_initial()['session']['autonomous'] is True
            result = client.call('claim_selected_queue_ticket', dict(ticket_id=ticket['id'], expected_version=ticket['version'],
                idempotency_key='development-queue-claim-11111111-1111-4111-8111-111111111111'))
            assert result['claimed'] is True and result['ticket']['id'] == ticket['id']
            for name in ('claim_ticket', 'renew_ticket'):
                with pytest.raises(BoardReadError, match='development_queue_claim_required'):
                    client.call(name, dict(ticket_id=ticket['id'], expected_version=ticket['version']))
        assert board.get_runtime(human)['executions'] == []
