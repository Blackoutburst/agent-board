"""N-slot standing queue passes over the real durable admission boundary.

Each slot is a separate private root, coder profile and discovery session.
`DevelopmentQueueAdmission.tick`, its intents/receipts/pointers, the owner
lease and `QueueSlotsService` are real. The Board discovery transport and the
generated Core pass are simulated in-process: no model, subscription, Git,
Runtime, systemd unit or network is used. These tests do NOT show four real
simultaneous subscription models; that run remains a separate human recipe.
"""
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from blackoutburst_agents import continuation_pool_service as core_module
from blackoutburst_agents import development_queue_slots as module
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.development_queue_slots import QueueSlotsService, queue_slots_plan
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import Budgets, DevelopmentError, canonical, digest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.development_queue import DevelopmentQueueAdmission, queue_tick_plan
from blackoutburst_agents.development_queue_client import DevelopmentQueueDiscoveryClient

ENDPOINT = 'https://board.test/mcp'


def write(path, value):
    with PrivateDirectory(path.parent) as private:
        raw = canonical(value) if not isinstance(value, bytes) else value
        (private.replace if path.exists() else private.create)(path.name, raw)


class Board:
    """Shared fake queue: tickets, per-ticket claims and Core launches."""
    def __init__(self):
        self.mutex = threading.Lock(); self.tickets = {}; self.calls = []
        self.claims = {}; self.launches = {}; self.expired = set(); self.behaviour = {}
        self.running = 0; self.peak = 0; self.started = []; self.stops = []
        self.barrier = None; self.release = threading.Event(); self.inside = threading.Event()

    def add(self, ticket_id, agent, resource, version=3):
        self.tickets[ticket_id] = dict(id=ticket_id, project_id='project', version=version, status='todo',
            claimant=None, resource=resource, delegation=None, claim_expired=False, blocked_by=[],
            waiting_on_human=False, assignee_owner_id='owner', assignee_agent_id=agent)


class Discovery:
    endpoint = ENDPOINT

    def __init__(self, board, slot, path, session_id):
        self.board, self.slot, self.session_id = board, slot, session_id
        self.connection_path = str(path); self.connection_pin = file_document(path)[1]

    def __enter__(self): return self
    def __exit__(self, *_): pass

    def registration(self, *, autonomous=False):
        with self.board.mutex: self.board.calls.append((self.slot['name'], 'register', autonomous))
        if self.slot['agent'] in self.board.expired: raise BoardReadError('authentication_failed')
        agent = self.slot['agent']; resources = sorted(self.slot['resources'])
        policy = dict(enabled=True, allow_read=True, allow_requests=True, autonomy='queue',
                      actions=['repo.read_file', 'repo.run_test'], resources=resources)
        return dict(identity=dict(id=agent, owner_id='owner', device_id='device-' + agent, profile_version=5),
                    profile_version=5, profile=dict(id=agent, owner_id='owner', version=5, status='active',
                    policy=policy, companion_for=None),
                    session=dict(id=self.session_id, agent_id=agent, owner_id='owner', device_id='device-' + agent,
                                 profile_version=5, autonomous=autonomous), policy=policy)

    def call(self, name, args):
        assert name in ('get_work', 'get_ticket', 'get_project'), 'Discovery has no effects'
        with self.board.mutex:
            self.board.calls.append((self.slot['name'], name, deepcopy(args)))
            if name == 'get_project':
                return dict(id='project', version=2, name='Project', description='', context='', repository_url='',
                            environment='', participants=['owner'], archived=False)
            if name == 'get_ticket':
                return dict(ticket=deepcopy(self.board.tickets[args['ticket_id']]))
            # Like the Board: only fresh Todo work visible to this profile.
            return dict(ready=[deepcopy(t) for t in self.board.tickets.values() if t['status'] == 'todo'
                and t['claimant'] is None and t['assignee_agent_id'] == self.slot['agent']])


@pytest.fixture
def slots(tmp_path, monkeypatch):
    root = tmp_path.resolve() / 'pool'; root.mkdir(mode=0o700)
    (root / 'config').mkdir(mode=0o700); (root / 'slots').mkdir(mode=0o700)
    board = Board(); made = {}
    monkeypatch.setattr(module, '_executable_pin', lambda value: {'path': str(value), 'sha256': 'c' * 64})
    monkeypatch.setattr(module, 'linux_source_pins', lambda: {'synthetic': 'sources'})

    def slot(name, resources):
        base = root / 'slots' / name; base.mkdir(mode=0o700)
        for child in ('config', 'connections', 'repositories', 'checkouts', 'verifiers'):
            (base / child).mkdir(mode=0o700)
        agent = 'coder-' + name
        write(base / 'connections' / 'coder.json', {'synthetic': 'no credentials'})
        verifier = base / 'verifiers' / 'fixed.py'; write(verifier, b'assert True\n')
        entries = [dict(entry_id='code-' + str(i), resource=resource,
            repository=dict(repository_id='approved', repository=str(base / 'repositories' / 'approved'),
                            base_commit='a' * 40),
            paths=['main.py'], actions=['repo.read_file', 'repo.run_test'], verifier_path=str(verifier),
            verifier_sha256=digest(verifier.read_bytes()), test_name='fixed', test_timeout_seconds=2,
            test_max_output_bytes=100, instruction='Inspect the authorized file and run the fixed test.',
            model='explicit-model', planner_budgets=PlannerBudgets().document(), broker_budgets=Budgets().document())
            for i, resource in enumerate(resources)]
        catalog = dict(schema='development-queue-catalog-v1', owner_id='owner', agent_id=agent, profile_version=5,
            board_endpoint=ENDPOINT, project_id='project', connection_file=str(base / 'connections' / 'coder.json'),
            timeout_seconds=2000, entries=entries)
        write(base / 'config' / 'catalog.json', catalog)
        workflow = dict(schema='linux-cloud-workflow-v1', batch_id=str(uuid.uuid4()), jobs=[dict(name=e['entry_id'],
            paths=['main.py'], commit_message='Approved source', pr_title='Approved PR',
            pr_body='Approved review required', reviewer=dict(agent_id='reviewer-' + name,
            connection_file=str(base / 'connections' / 'reviewer.json'), instruction='Review the sealed evidence',
            model='review-model', timeout=120, skills=['review'])) for e in entries])
        write(base / 'config' / 'workflow.json', workflow)
        plan = queue_tick_plan(base, str(base / 'config' / 'catalog.json'), str(base / 'config' / 'workflow.json'))
        standing = base / 'config' / 'standing.json'
        write(standing, dict(schema='linux-cloud-queue-continuation-v1', catalog_file=str(base / 'config' / 'catalog.json'),
            workflow_file=str(base / 'config' / 'workflow.json'), approved_sha256=plan['approval_sha256'],
            checkpoint_after_actions=3, provider_backoff_seconds=60))
        made[name] = dict(name=name, agent=agent, resources=set(resources), root=base, standing=standing,
                          catalog=catalog)
        return made[name]

    def configure(concurrency, names):
        path = root / 'config' / 'slots.json'
        write(path, dict(schema='linux-cloud-queue-slots-v1', concurrency=concurrency,
                         slots=[dict(name=n, continuation_file=str(made[n]['standing'])) for n in names]))
        return path

    def from_connection(path, *, session_id, timeout=20):
        owner = next(s for s in made.values() if str(s['root'] / 'connections' / 'coder.json') == str(path))
        return Discovery(board, owner, path, session_id)
    monkeypatch.setattr(DevelopmentQueueDiscoveryClient, 'from_connection_file', from_connection)

    class Core:
        """The generated Core pass, simulated: claims once per logical task."""
        def __init__(self, owner_root, *, configuration_path, **kwargs):
            self.config = decode(Path(configuration_path).read_bytes())
            self.batch = decode(Path(self.config['batch_file']).read_bytes())
            self.ticket = self.batch['jobs'][0]['ticket_id']; self.root = owner_root
            assert owner_root in [s['root'] for s in made.values()]
            assert self.batch['jobs'][0]['repository']['workspace_root'].startswith(str(owner_root / 'checkouts'))

        def run_once(self, *, should_stop):
            task = self.config['tasks'][0]['logical_task_id']
            with board.mutex:
                board.launches.setdefault(task, 0); board.launches[task] += 1
                if board.launches[task] == 1:  # One selected claim per logical task.
                    board.claims[self.ticket] = board.claims.get(self.ticket, 0) + 1
                    board.tickets[self.ticket].update(status='in_progress', claimant={'agent': self.ticket})
                board.running += 1; board.peak = max(board.peak, board.running); board.started.append(self.ticket)
            try:
                step = board.behaviour.get(self.ticket, ['paused_cycle'])
                phase = step.pop(0) if len(step) > 1 else step[0]
                if phase == 'barrier':
                    board.barrier.wait(timeout=10); phase = 'paused_cycle'
                elif phase in ('model', 'test'):
                    board.inside.set()
                    limit = time.monotonic() + 10
                    while not should_stop():
                        assert time.monotonic() < limit, 'Stop did not reach this slot'
                        time.sleep(.01)
                    with board.mutex: board.stops.append((self.ticket, phase))
                    phase = 'stopped'
                elif phase == 'hold':
                    board.inside.set(); assert board.release.wait(timeout=10); phase = 'paused_cycle'
                elif phase == 'crash':
                    raise OSError('synthetic controller loss after the claim')
                row = dict(name=self.batch['jobs'][0]['name'], logical_task_id=task, phase=phase,
                    operation_id=str(uuid.uuid4()), state_sha256='a' * 64, reason='quota' if phase == 'paused_quota' else '',
                    next_observation_at=1234 if phase in ('paused_quota', 'paused_provider') else None, launchable=False)
                if phase == 'independent_review_published':
                    row.update(review_operation_id=str(uuid.uuid4()), review_receipt_sha256='b' * 64,
                               accepted=False, historical=False)
                    with board.mutex: board.tickets[self.ticket].update(status='review')
                return dict(schema='continuation-pool-pass-v1', status='pass_complete' if phase != 'stopped' else 'stopped',
                            tasks=[row], activated=True, pass_complete_is_project_completion=False)
            finally:
                with board.mutex: board.running -= 1

    # Each slot's tick constructs its generated Core: the simulated part.
    monkeypatch.setattr(core_module, 'ContinuationPoolService', Core)
    monkeypatch.setattr('blackoutburst_agents.finite_pool_batch.FiniteLinuxPoolBatch.run',
        lambda *_a, **_k: pytest.fail('slots must never replay finite work'))

    def service(path):
        return QueueSlotsService(root, configuration_path=path, owner_id='owner', board_endpoint=ENDPOINT,
            executable='/synthetic/codex', cli_version='codex-cli 1.2.3', interpreter='/synthetic/python',
            unshare='/synthetic/unshare', gh_executable='/synthetic/gh', max_seconds=3000)

    class Case: pass
    case = Case(); case.root, case.board, case.slot, case.configure, case.service, case.made = root, board, slot, configure, service, made
    return case


def pointer(slot):
    key = digest(canonical({k: slot['catalog'][k] for k in ('owner_id', 'agent_id')}))
    path = slot['root'] / 'admissions' / ('queue-' + key) / 'tick.json'
    return decode(path.read_bytes()) if path.exists() else None


def admissions(slot):
    return sorted(p.name for p in (slot['root'] / 'config').glob('queue-admission-*'))


def by_name(result):
    return {row['name']: row for row in result['slots']}


def model_calls(board):
    return sum(board.launches.values())


def test_four_slots_admit_four_distinct_tickets_with_overlapping_lifetimes(slots):
    names = ['a', 'b', 'c', 'd']
    for n in names:
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
        slots.board.behaviour['t-' + n] = ['barrier']
    slots.board.barrier = threading.Barrier(4)
    result = slots.service(slots.configure(4, names)).run_once()
    assert result['status'] == 'pass_complete' and result['concurrency'] == 4, result
    rows = by_name(result)
    assert all(rows[n]['phase'] == 'paused_cycle' for n in names)
    # All four simulated Core passes were inside run_once simultaneously.
    assert slots.board.peak == 4 and not slots.board.barrier.broken
    assert slots.board.claims == {'t-' + n: 1 for n in names}
    requests = {rows[n]['request_id'] for n in names}; assert len(requests) == 4
    for n in names:
        slot = slots.made[n]; state = pointer(slot)
        assert state['phase'] == 'active' and state['active']['request_id'] == rows[n]['request_id']
        batch = decode((slot['root'] / 'config' / admissions(slot)[0]).read_bytes())
        assert batch['jobs'][0]['ticket_id'] == 't-' + n and batch['jobs'][0]['resource'] == 'repo:' + n
        assert batch['jobs'][0]['agent_id'] == 'coder-' + n
    assert result['activated'] is True and result['pass_complete_is_project_completion'] is False
    assert all(row['launchable'] is False for row in result['slots'])


def test_owner_cap_four_leaves_the_fifth_slot_waiting_without_discovery(slots):
    names = ['a', 'b', 'c', 'd', 'e']
    for n in names:
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
        slots.board.behaviour['t-' + n] = ['barrier']
    slots.board.barrier = threading.Barrier(4)
    path = slots.configure(4, names)
    result = slots.service(path).run_once()
    rows = by_name(result)
    assert [rows[n]['phase'] for n in names] == ['paused_cycle'] * 4 + ['waiting_capacity'], result
    assert slots.board.peak == 4 and 't-e' not in slots.board.claims
    assert pointer(slots.made['e']) is None and admissions(slots.made['e']) == []
    assert not any(slot == 'e' for slot, *_ in slots.board.calls)
    # Next pass: the four held slots continue first; still no fifth admission.
    for n in names: slots.board.behaviour['t-' + n] = ['paused_cycle']
    again = by_name(slots.service(path).run_once())
    assert again['e']['phase'] == 'waiting_capacity' and pointer(slots.made['e']) is None
    assert all(again[n]['request_id'] == rows[n]['request_id'] for n in names[:4])
    # A finished slot frees its capacity through its own durable archive; the
    # waiting slot may then discover within the same bounded pass.
    slots.board.behaviour['t-a'] = ['independent_review_published']
    third = by_name(slots.service(path).run_once())
    assert third['a']['phase'] == 'independent_review_published'
    assert third['e']['phase'] == 'paused_cycle' and slots.board.claims['t-e'] == 1
    assert slots.board.peak == 4
    fourth = by_name(slots.service(path).run_once())
    assert fourth['a']['phase'] == 'waiting_capacity' and fourth['e']['request_id'] == third['e']['request_id']
    assert slots.board.claims == {'t-' + n: 1 for n in names}


@pytest.mark.parametrize('cap', [1, 2])
def test_same_resource_is_never_admitted_into_a_second_slot(slots, cap):
    for n in ('a', 'b'):
        slots.slot(n, ['repo:shared']); slots.board.add('t-' + n, 'coder-' + n, 'repo:shared')
    path = slots.configure(cap, ['a', 'b'])
    first = by_name(slots.service(path).run_once())
    phases = sorted(row['phase'] for row in first.values())
    assert phases == (['paused_cycle', 'resource_busy'] if cap == 2 else ['paused_cycle', 'waiting_capacity'])
    winner = next(n for n, row in first.items() if row['phase'] == 'paused_cycle'); loser = ({'a', 'b'} - {winner}).pop()
    assert slots.board.claims == {'t-' + winner: 1}
    assert admissions(slots.made[loser]) == []
    assert not [p for p in (slots.made[loser]['root'] / 'admissions').glob('*') if not p.name.startswith('queue-')] \
        if (slots.made[loser]['root'] / 'admissions').exists() else True
    loser_state = pointer(slots.made[loser])
    assert loser_state is None or (loser_state['phase'] == 'idle' and loser_state['active'] is None)
    # The paused holder keeps its resource even though its Board claim is released.
    slots.board.tickets['t-' + winner].update(status='todo', claimant=None, version=4)
    slots.board.behaviour['t-' + winner] = ['paused_cycle']
    second = by_name(slots.service(path).run_once())
    assert second[winner]['request_id'] == first[winner]['request_id']
    assert second[loser]['phase'] in ('resource_busy', 'waiting_capacity') and 't-' + loser not in slots.board.claims
    assert len(admissions(slots.made[winner])) == 1


def test_resource_guard_sees_an_admitting_slot_without_intent_as_holding_its_catalog(slots):
    for n in ('a', 'b'):
        slots.slot(n, ['repo:shared'])
    slots.board.add('t-b', 'coder-b', 'repo:shared')
    a = slots.made['a']; key = digest(canonical({k: a['catalog'][k] for k in ('owner_id', 'agent_id')}))
    directory = a['root'] / 'admissions' / ('queue-' + key)
    directory.parent.mkdir(mode=0o700); directory.mkdir(mode=0o700)
    # Crash evidence: the pointer reached admitting before the admission intent.
    write(directory / 'tick.json', dict(schema='development-queue-tick-state-v1', approval_sha256='0' * 64,
        session_id='x', phase='admitting', active=dict(request_id=str(uuid.uuid4()), logical_task_id=str(uuid.uuid4()),
        preview_sha256='1' * 64, configuration_pin=None, admission_sha256=None), last_receipt=None))
    result = by_name(slots.service(slots.configure(2, ['a', 'b'])).run_once())
    assert result['b']['phase'] == 'resource_busy' and admissions(slots.made['b']) == []
    assert result['a']['phase'] == 'needs_you'  # Its own tick refuses the mismatched evidence.
    assert model_calls(slots.board) == 0


def test_concurrency_one_runs_one_slot_at_a_time(slots):
    for n in ('a', 'b'):
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
    path = slots.configure(1, ['a', 'b'])
    first = by_name(slots.service(path).run_once())
    assert first['a']['phase'] == 'paused_cycle' and first['b']['phase'] == 'waiting_capacity'
    assert slots.board.peak == 1 and list(slots.board.claims) == ['t-a']
    slots.board.behaviour['t-a'] = ['independent_review_published']
    second = by_name(slots.service(path).run_once())
    assert second['a']['phase'] == 'independent_review_published' and second['b']['phase'] == 'paused_cycle'
    assert slots.board.peak == 1 and slots.board.claims == {'t-a': 1, 't-b': 1}


def test_idle_pass_makes_no_model_call_and_no_admission(slots):
    for n in ('a', 'b', 'c'):
        slots.slot(n, ['repo:' + n])
    result = slots.service(slots.configure(3, ['a', 'b', 'c'])).run_once()
    assert result['status'] == 'pass_complete' and [r['phase'] for r in result['slots']] == ['empty'] * 3
    assert model_calls(slots.board) == 0 and slots.board.claims == {}
    assert all(admissions(s) == [] for s in slots.made.values())
    assert {name for _, name, *_ in slots.board.calls} <= {'register', 'get_project', 'get_work'}


def test_restart_after_crash_reopens_each_slot_without_new_claim_or_admission(slots):
    for n in ('a', 'b'):
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
    slots.board.behaviour['t-a'] = ['crash', 'paused_cycle']
    path = slots.configure(2, ['a', 'b'])
    first = by_name(slots.service(path).run_once())
    assert first['a']['phase'] == 'needs_you' and first['b']['phase'] == 'paused_cycle'
    assert first['a']['failure']['stage'] == 'queue_tick'
    before = {n: (pointer(slots.made[n]), admissions(slots.made[n])) for n in ('a', 'b')}
    discovery = [c for c in slots.board.calls if c[1] == 'get_work']
    # A new process: fresh service objects, only durable state survives.
    second = by_name(slots.service(path).run_once())
    assert second['a']['phase'] == 'paused_cycle' and second['b']['phase'] == 'paused_cycle'
    for n in ('a', 'b'):
        assert second[n]['request_id'] == before[n][0]['active']['request_id']
        assert (pointer(slots.made[n]), admissions(slots.made[n])) == before[n]
    assert [c for c in slots.board.calls if c[1] == 'get_work'] == discovery  # No rediscovery.
    assert slots.board.claims == {'t-a': 1, 't-b': 1}
    assert all(count == 2 for count in slots.board.launches.values())  # Same Core reopened, no new logical task.


def test_stop_reaches_every_slot_during_model_and_test_phases(slots):
    for n, phase in (('a', 'model'), ('b', 'test'), ('c', 'model')):
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
        slots.board.behaviour['t-' + n] = [phase, 'paused_cycle']
    stop = threading.Event()
    def stopper():
        deadline = time.monotonic() + 10
        while len(slots.board.started) < 3 and time.monotonic() < deadline: time.sleep(.01)
        stop.set()
    helper = threading.Thread(target=stopper); helper.start()
    path = slots.configure(3, ['a', 'b', 'c'])
    result = slots.service(path).run_once(should_stop=stop.is_set); helper.join()
    assert result['status'] == 'stopped', result
    assert sorted(slots.board.stops) == [('t-a', 'model'), ('t-b', 'test'), ('t-c', 'model')]
    assert all(row['phase'] == 'stopped' for row in result['slots'])
    # Stop is not a replay request; the next pass reopens the same Cores.
    resumed = by_name(slots.service(path).run_once())
    assert all(resumed[n]['request_id'] == by_name(result)[n]['request_id'] for n in ('a', 'b', 'c'))
    assert slots.board.claims == {'t-a': 1, 't-b': 1, 't-c': 1}


def test_stop_before_start_runs_no_slot(slots):
    slots.slot('a', ['repo:a']); slots.board.add('t-a', 'coder-a', 'repo:a')
    result = slots.service(slots.configure(1, ['a'])).run_once(should_stop=lambda: True)
    assert result['status'] == 'stopped' and result['slots'][0]['phase'] == 'not_started'
    assert slots.board.calls == [] and pointer(slots.made['a']) is None


def test_quota_on_one_slot_is_visible_and_recoverable_while_others_continue(slots):
    for n in ('a', 'b', 'c'):
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
    slots.board.behaviour['t-b'] = ['paused_quota', 'paused_cycle']
    path = slots.configure(3, ['a', 'b', 'c'])
    first = by_name(slots.service(path).run_once())
    assert first['b']['phase'] == 'paused_quota' and first['b']['next_observation_at'] == 1234
    assert first['b']['reason'] == 'quota' and first['a']['phase'] == first['c']['phase'] == 'paused_cycle'
    assert pointer(slots.made['b'])['phase'] == 'active'
    second = by_name(slots.service(path).run_once())
    assert second['b']['phase'] == 'paused_cycle' and second['b']['request_id'] == first['b']['request_id']
    assert slots.board.claims == {'t-a': 1, 't-b': 1, 't-c': 1}


def test_expired_connection_is_a_visible_per_slot_state_and_recovers(slots):
    for n in ('a', 'b'):
        slots.slot(n, ['repo:' + n]); slots.board.add('t-' + n, 'coder-' + n, 'repo:' + n)
    slots.board.expired.add('coder-b')
    path = slots.configure(2, ['a', 'b'])
    first = slots.service(path).run_once(); rows = by_name(first)
    assert first['status'] == 'needs_you'
    assert rows['a']['phase'] == 'paused_cycle' and rows['b']['phase'] == 'connection_expired'
    assert rows['b']['failure']['category'] == 'board_read_error'
    assert pointer(slots.made['b'])['phase'] == 'idle' and pointer(slots.made['b'])['active'] is None
    assert admissions(slots.made['b']) == []
    slots.board.expired.clear()
    second = by_name(slots.service(path).run_once())
    assert second['b']['phase'] == 'paused_cycle' and slots.board.claims == {'t-a': 1, 't-b': 1}


def test_overlapping_pass_on_the_same_owner_root_is_refused(slots):
    slots.slot('a', ['repo:a']); slots.board.add('t-a', 'coder-a', 'repo:a')
    slots.board.behaviour['t-a'] = ['hold']
    path = slots.configure(1, ['a']); results = []
    thread = threading.Thread(target=lambda: results.append(slots.service(path).run_once())); thread.start()
    assert slots.board.inside.wait(timeout=10)
    try:
        second = slots.service(path).run_once()
        assert second['status'] == 'needs_you' and second['failure']['code'] == 'continuation_pool_owner_busy'
    finally:
        slots.board.release.set(); thread.join()
    assert results[0]['status'] == 'pass_complete' and slots.board.claims == {'t-a': 1}


def test_plan_is_inert_and_slot_configuration_is_closed(slots):
    for n in ('a', 'b'):
        slots.slot(n, ['repo:' + n])
    path = slots.configure(2, ['a', 'b'])
    before = {str(p): p.read_bytes() for p in slots.root.rglob('*') if p.is_file()}
    plan = queue_slots_plan(str(path), root=slots.root, owner_id='owner', board_endpoint=ENDPOINT, max_seconds=3000)
    assert plan['schema'] == 'linux-cloud-queue-slots-v1' and plan['concurrency'] == 2
    assert [row['agent_id'] for row in plan['slots']] == ['coder-a', 'coder-b']
    assert plan['activated'] is plan['execution_qualified'] is plan['launchable'] is False
    assert 'instruction' not in json.dumps(plan) and slots.board.calls == []
    assert before == {str(p): p.read_bytes() for p in slots.root.rglob('*') if p.is_file()}
    value = decode(path.read_bytes())
    for change in ({'concurrency': 0}, {'concurrency': 3}, {'concurrency': 9}, {'extra': True},
                   {'slots': value['slots'] + [value['slots'][0]]},
                   {'slots': [{**value['slots'][0], 'name': '../a'}]}):
        write(path, {**value, **change})
        with pytest.raises(DevelopmentError):
            slots.service(path)
    # Two slots sharing one coder profile are refused: the Board would only
    # let one of them hold unfinished queue work.
    write(path, value)
    shared = slots.made['b']; catalog = {**shared['catalog'], 'agent_id': 'coder-a'}
    write(shared['root'] / 'config' / 'catalog.json', catalog)
    with pytest.raises(DevelopmentError):
        slots.service(path)
    assert slots.board.calls == []


def test_slot_module_stays_outside_the_native_source_rosters():
    from blackoutburst_agents.family_sources import controller_sources
    from blackoutburst_agents.linux_planner_policy import LINUX_EXECUTION_SOURCES
    assert 'development_queue_slots.py' not in LINUX_EXECUTION_SOURCES
    # The bounded family roster must still be collectable from this source.
    assert controller_sources()['files']
