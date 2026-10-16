"""Four real Board profiles, two owners, managed workers and fake models only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import sys
import threading
import uuid

import pytest

from test_board_worker import Board
from test_runtime_integration import ControlClient
from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.queue_journal import QueueJournal
from blackoutburst_agents.queue_worker import QueueWorker
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.runtime_guard import RuntimeGuard
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal

pytestmark = pytest.mark.skipif(Board is None, reason='Repository Board sources required')


class DirectClient:
    endpoint = 'https://board.test/mcp'

    def __init__(self, board, raw, session):
        self.board, self.raw, self.session_id = board, raw, session
        self.calls, self.lost = [], set()
        self.before_select = None
        self.register()

    def _register(self, autonomous):
        result = self.board.register_agent(self.raw, self.session_id, self.session_id,
            [], 'codex', autonomous, 'acceptance')
        self.actor = self.board.actor_session(self.raw, self.session_id)
        self.registration = result
        return result

    def register(self):
        return self._register(False)

    def register_for_selection(self):
        return self._register(True)

    def call(self, name, args):
        if name == 'claim_next_ticket' and self.before_select is not None:
            self.before_select.wait(timeout=10)
        self.calls.append((name, deepcopy(args)))
        values = dict(args)
        if name == 'update_ticket':
            values.update(values.pop('changes'))
        result = getattr(self.board, name)(self.actor, **values)
        if name in self.lost:
            self.lost.remove(name)
            raise TimeoutError('synthetic lost acknowledgement')
        return result


class Executor:
    def __init__(self, setup, owner, number):
        self.setup, self.owner = setup, owner
        self.name = owner + '-' + str(number)
        self.root = setup.root / self.name
        self.root.mkdir()
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        human = setup.humans[owner]
        device = setup.board.create_device(human, self.name, [])
        profile = setup.board.create_agent_profile(human, self.name,
            project_ids=[setup.project['id']], idempotency_key=self.name,
            policy={'allow_read': True, 'allow_messages': True, 'allow_requests': True,
                    'autonomy': 'queue', 'actions': [], 'resources': []})
        setup.board.bind_agent_profile(human, profile['id'], profile['version'],
            device['id'], 'acceptance', 'codex')
        raw = setup.board.authenticate(device['token'])
        self.client = DirectClient(setup.board, raw, self.name)
        self.control = ControlClient(setup.board, raw, self.name)
        self.scope = {'owner_id': owner, 'agent_id': profile['id'],
            'board_endpoint': self.client.endpoint, 'board_session_id': self.name}
        self.before_model = None

    def model(self, settings, prompt, workspace, **callbacks):
        runtime = self.setup.board.get_runtime(self.setup.humans[self.owner])
        active = [entry for entry in runtime['executions'] if entry['state'] == 'running']
        assert len(active) == runtime['active_count'] == 1
        execution = active[0]
        assert execution['owner_id'] == self.owner
        assert execution['agent_id'] == self.scope['agent_id']
        assert execution['session_id'] == self.client.session_id
        ticket = self.setup.board.get_ticket(self.client.actor, execution['ticket_id'])['ticket']
        claimant = ticket['claimant']
        assert ticket['status'] == 'in_progress'
        assert claimant['owner_id'] == self.owner
        assert claimant['agent_id'] == self.scope['agent_id']
        assert claimant['session_id'] == self.client.session_id
        visible = self.setup.board.list_agents(self.setup.humans[self.owner], self.setup.project['id'])
        own = next(profile for profile in visible if profile['id'] == self.scope['agent_id'])
        assert any(session['id'] == self.client.session_id and any(
            value['id'] == ticket['id'] for value in session['tickets']) for session in own['sessions'])
        with self.setup.lock:
            self.setup.models.append((self.name, ticket['id'], execution['id']))
        if self.before_model is not None:
            self.before_model()
        callbacks['on_started'](999999999)
        session = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        return {'status': 'completed', 'reason': 'verified', 'session_id': session,
            'text': 'Synthetic reviewed result',
            'verification': {'cleanup_confirmed': True, 'tool_free': True}}

    def run(self, request='explicit-queue-request', project_id=None):
        # Every invocation opens its own SQLite connections in its current
        # thread. Replays reopen the same private state and workspace.
        with ExitStack() as resources:
            queue = resources.enter_context(QueueJournal(self.root / 'queue'))
            work = resources.enter_context(WorkJournal(self.root / 'work'))
            runtime = resources.enter_context(RuntimeJournal(self.root / 'runtime'))
            runs = RunStore(self.root / 'runs')
            resources.callback(runs.close)
            worker = BoardWorker(work, runs, self.client,
                runner_factory=lambda store, **options: PersonalRunner(store, self.model, **options),
                runtime_guard_factory=lambda scope: RuntimeGuard(runtime, self.control, scope))
            return QueueWorker(queue, worker, self.client).run(scope=self.scope, request_id=request,
                project_id=project_id or self.setup.project['id'], instruction='Return a synthetic review.',
                workspace=self.workspace, executable=sys.executable, model='fixture-model', timeout=10,
                runtime_managed=True)


class Setup:
    def __init__(self, root):
        self.root = root
        self.board = Board(root / 'board', base_url='https://board.test')
        self.humans = {owner: {'kind': 'human', 'owner_id': owner} for owner in ('alpha', 'beta')}
        for owner, human in self.humans.items():
            self.board.ensure_owner(owner, owner.title())
            self.board.set_policy(human, True, True, True, True, [], [])
        self.project = self.board.create_project(self.humans['alpha'], 'Shared acceptance',
            ['alpha', 'beta'], idempotency_key='shared')
        self.models, self.lock = [], threading.Lock()
        self.executors = [Executor(self, owner, number) for owner in self.humans for number in range(2)]

    def ticket(self, title, **values):
        return self.board.create_ticket(self.humans['alpha'], self.project['id'], title,
            idempotency_key=title, **values)

    def counters(self):
        return (len(self.models), sum(client.control.calls.count('reserve_execution') for client in self.executors),
            sum(name == 'update_ticket' for client in self.executors for name, _ in client.client.calls),
            sum(name == 'claim_next_ticket' for client in self.executors for name, _ in client.client.calls))


def test_four_profiles_two_owners_race_one_ticket_with_lost_selection_and_publication_replies(tmp_path):
    setup = Setup(tmp_path)
    ticket = setup.ticket('Only one eligible ticket')
    barrier = threading.Barrier(4)
    for executor in setup.executors:
        executor.client.before_select = barrier
        executor.client.lost.update({'claim_next_ticket', 'update_ticket'})
    with ThreadPoolExecutor(4) as pool:
        first = list(pool.map(lambda executor: executor.run(), setup.executors))
    assert len(setup.models) == 1
    winner = next(executor for executor in setup.executors if executor.name == setup.models[0][0])
    assert setup.models[0][1] == ticket['id']
    assert next(result for executor, result in zip(setup.executors, first) if executor is winner)['status'] == 'pending'
    for executor in setup.executors:
        executor.client.before_select = None
    # Lost publication reply reconciles the exact already completed worker.
    assert winner.run()['status'] == 'complete'
    before = setup.counters()
    for executor in setup.executors:
        replay = executor.run()
        assert replay['cached']
        assert replay['status'] == ('complete' if executor is winner else 'idle')
    assert setup.counters() == before == (1, 1, 1, 4)
    with setup.board._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM ticket_events WHERE event='ticket_claimed'").fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0] == 1
    final = setup.board.get_ticket(setup.humans['alpha'], ticket['id'])['ticket']
    assert final['status'] == 'review' and final['claimant'] is None
    assert all(list(executor.workspace.iterdir()) == [] for executor in setup.executors)


def test_four_managed_queue_workers_complete_in_owner_serial_chains_while_owners_overlap(tmp_path):
    setup = Setup(tmp_path)
    blocked = setup.ticket('Blocked status', priority='urgent')
    setup.board.update_ticket(setup.humans['alpha'], blocked['id'], blocked['version'], status='blocked')
    dependency = setup.ticket('Blocked by dependency', priority='urgent', depends_on=[blocked['id']])
    skill = setup.ticket('Missing required skill', priority='urgent', required_skills=['ungranted-skill'])
    outside = setup.board.create_project(setup.humans['alpha'], 'Outside profile scope',
        ['alpha', 'beta'], idempotency_key='outside')
    forbidden = setup.board.create_ticket(setup.humans['alpha'], outside['id'], 'Not granted', idempotency_key='forbidden')
    expected = {}
    for executor in setup.executors:
        expected[executor.name] = setup.ticket('Assigned to ' + executor.name,
            assignee_agent_id=executor.scope['agent_id'])['id']
    overlap = threading.Barrier(2)
    observations = []
    def meet():
        overlap.wait(timeout=10)
        observations.append({owner: setup.board.get_runtime(human)['active_count']
                             for owner, human in setup.humans.items()})
        overlap.wait(timeout=10)
    setup.executors[0].before_model = setup.executors[2].before_model = meet
    def chain(executors):
        return [executor.run() for executor in executors]
    with ThreadPoolExecutor(2) as pool:
        chains = list(pool.map(chain, (setup.executors[:2], setup.executors[2:])))
    assert all(result['status'] == 'complete' for chain in chains for result in chain)
    assert observations == [{'alpha': 1, 'beta': 1}] * 2
    assert {name: ticket for name, ticket, _ in setup.models} == expected
    assert len({ticket for _, ticket, _ in setup.models}) == 4
    before = setup.counters()
    assert before == (4, 4, 4, 4)
    assert all(executor.run()['cached'] for executor in setup.executors)
    assert setup.counters() == before
    for executor in setup.executors:
        runtime = setup.board.get_runtime(setup.humans[executor.owner])
        assert runtime['active_count'] == 0
        assert all(value['state'] == 'finished' and value['cleanup_confirmed'] for value in runtime['executions'])
    for identifier in (blocked['id'], dependency['id'], skill['id'], forbidden['id']):
        assert setup.board.get_ticket(setup.humans['alpha'], identifier)['ticket']['claimant'] is None
    forbidden_attempt = setup.executors[0].run('explicit-forbidden-project', outside['id'])
    assert forbidden_attempt['status'] in ('pending', 'needs_review')
    assert setup.counters() == before  # No fallback selection or model on denial.


def test_runtime_capacity_blocks_other_profile_before_model_without_cross_owner_blocking(tmp_path):
    setup = Setup(tmp_path)
    capacity_tickets = {executor.name: setup.ticket('Capacity task ' + executor.name,
        assignee_agent_id=executor.scope['agent_id'])['id'] for executor in setup.executors}
    release = threading.Event()
    entered = [threading.Event(), threading.Event()]
    def hold(index):
        entered[index].set()
        assert release.wait(timeout=10)
    setup.executors[0].before_model = lambda: hold(0)
    setup.executors[2].before_model = lambda: hold(1)
    with ThreadPoolExecutor(4) as pool:
        admitted = [pool.submit(setup.executors[index].run) for index in (0, 2)]
        try:
            assert all(event.wait(timeout=10) for event in entered)
            refused = list(pool.map(lambda executor: executor.run(), [setup.executors[1], setup.executors[3]]))
            assert all(result['status'] == 'needs_review' for result in refused)
            assert len(setup.models) == 2
            assert {name for name, _, _ in setup.models} == {'alpha-0', 'beta-0'}
            assert all(setup.board.get_runtime(human)['active_count'] == 1 for human in setup.humans.values())
        finally:
            release.set()
        assert all(future.result()['status'] == 'complete' for future in admitted)
    before = setup.counters()
    assert before == (2, 4, 2, 4)
    for executor in (setup.executors[1], setup.executors[3]):
        held = setup.board.get_ticket(setup.humans[executor.owner], capacity_tickets[executor.name])['ticket']
        assert held['status'] == 'in_progress'
        assert held['claimant']['agent_id'] == executor.scope['agent_id']
        assert held['claimant']['session_id'] == executor.client.session_id
        assert executor.run()['status'] == 'needs_review'
        assert executor.run('new-key-cannot-bypass-review')['reason'] == 'scope_has_unfinished_operation'
    assert setup.counters() == before
    assert all(setup.board.get_runtime(human)['active_count'] == 0 for human in setup.humans.values())
