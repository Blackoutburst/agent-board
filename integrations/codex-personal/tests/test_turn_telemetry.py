"""Bounded turn telemetry (#51): fake Codex processes and local files only.

No model, authentication, network or real `codex` process is involved.
"""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import time
import uuid

import pytest

from blackoutburst_agents import turn_telemetry as telemetry
from blackoutburst_agents.codex_turn import execute_turn
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.linux_planner_policy import LINUX_EXECUTION_SOURCES
from blackoutburst_agents.run_store import RunStore, StoreError
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.subscription_probe import ProbeSettings

SESSION = '11111111-2222-4333-8444-555555555555'
PRIVATE = ('Private prompt', 'Final private reply', 'private scratch', 'PRIVATE_ERROR_TEXT')
PACKAGE = Path(telemetry.__file__).parent
EVENTS = ('process_started', 'thread.started', 'turn.started', 'turn.completed', 'cleanup_confirmed')

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX process groups and O_NOFOLLOW')


@pytest.fixture
def cli(tmp_path):
    workspace = tmp_path / 'workspace'; workspace.mkdir()

    def make(body='success()\n', timeout=3):
        executable = tmp_path / 'fake-codex'
        executable.write_text(
            '#!' + sys.executable + '\n'
            'import json, os, sys, time\n'
            'args = sys.argv[1:]\n'
            "if args == ['--version']:\n"
            "    print('codex-cli 0.162.0-alpha.17.2'); raise SystemExit(0)\n"
            "if args == ['login', 'status']:\n"
            "    print('Logged in using ChatGPT', file=sys.stderr); raise SystemExit(0)\n"
            'prompt = sys.stdin.read()\n'
            'def event(value):\n'
            '    print(json.dumps(value), flush=True)\n'
            'def started():\n'
            f"    event({{'type': 'thread.started', 'thread_id': {SESSION!r}}})\n"
            "    event({'type': 'turn.started'})\n"
            'def message(text):\n'
            "    event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})\n"
            'def success():\n'
            '    started()\n'
            "    event({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private scratch'}})\n"
            "    message('Final private reply')\n"
            "    event({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}})\n"
            + textwrap.dedent(body), encoding='utf-8')
        executable.chmod(0o700)
        return ProbeSettings(executable, timeout_seconds=timeout, metadata_timeout_seconds=2)

    def run(body='success()\n', *, timeout=3, **kwargs):
        return execute_turn(make(body, timeout), 'Private prompt', workspace, **kwargs)
    run.make, run.workspace = make, workspace
    return run


def collect(fail_on=None, error=RuntimeError):
    events = []

    def sink(event):
        events.append(event)
        if event['kind'] == fail_on:
            raise error('PRIVATE_ERROR_TEXT')
    return events, sink


def kinds(events):
    return [event['kind'] for event in events]


def test_success_reports_closed_boundaries_without_any_text(cli):
    events, sink = collect()
    result = cli(on_turn_event=sink)
    assert result['status'] == 'completed' and result['text'] == 'Final private reply'
    assert kinds(events) == list(EVENTS)
    for event in events:
        assert set(event) == {'kind', 'wall', 'monotonic', 'session_id'}
        assert type(event['wall']) is float and type(event['monotonic']) is float
    assert events[0]['session_id'] is None
    assert all(event['session_id'] == SESSION for event in events[1:])
    monotonic = [event['monotonic'] for event in events]
    assert monotonic == sorted(monotonic)
    dumped = json.dumps(events)
    assert not any(text in dumped for text in PRIVATE)


def test_non_callable_callback_refuses_before_any_launch(cli, tmp_path):
    marker = tmp_path / 'launched'
    result = cli(f'open({str(marker)!r}, "w").close()\nsuccess()\n', on_turn_event='not callable')
    assert (result['status'], result['reason']) == ('blocked', 'invalid_input')
    assert not marker.exists()


@pytest.mark.parametrize('kind', EVENTS)
def test_callback_failure_at_each_boundary_blocks_and_stops_the_group(cli, kind):
    events, sink = collect(fail_on=kind)
    started = time.monotonic()
    # After the stream, the fake hangs: only the stopped group ends it quickly.
    result = cli('success()\ntime.sleep(30)\n' if kind != 'cleanup_confirmed' else 'success()\n',
                 timeout=20, on_turn_event=sink)
    assert time.monotonic() - started < 10
    assert (result['status'], result['reason']) == ('blocked', 'callback_failed')
    assert result['text'] is None and result['verification']['cleanup_confirmed'] is True
    assert kinds(events)[:EVENTS.index(kind) + 1] == list(EVENTS[:EVENTS.index(kind) + 1])
    assert 'PRIVATE_ERROR_TEXT' not in json.dumps(result)


@pytest.mark.parametrize('kind', EVENTS[:4])
def test_stop_at_each_boundary_is_cancelled_without_a_fabricated_terminal(cli, kind):
    events, sink = collect()
    seen = threading.Event()

    def record(event):
        sink(event)
        if event['kind'] == kind:
            seen.set()
    # Each boundary is followed by a pause, so the stop lands right after it.
    body = textwrap.dedent(f"""
        time.sleep(.3)
        event({{'type': 'thread.started', 'thread_id': {SESSION!r}}}); time.sleep(.5)
        event({{'type': 'turn.started'}}); time.sleep(.5)
        message('Final private reply')
        event({{'type': 'turn.completed'}}); time.sleep(30)
    """)
    result = cli(body, timeout=20, on_turn_event=record, should_cancel=seen.is_set)
    assert (result['status'], result['reason']) == ('cancelled', 'cancelled')
    observed = kinds(events)
    assert observed[-1] == 'cleanup_confirmed' and kind in observed
    later = EVENTS[EVENTS.index(kind) + 1:-1]
    assert not any(name in observed for name in later)


def test_timeout_after_turn_started_has_no_terminal(cli):
    events, sink = collect()
    result = cli('started()\ntime.sleep(30)\n', timeout=1, on_turn_event=sink)
    assert result['reason'] == 'timeout'
    assert kinds(events) == ['process_started', 'thread.started', 'turn.started', 'cleanup_confirmed']


@pytest.mark.parametrize('body, expected', [
    ("started()\nevent({'type': 'turn.started'})\n", ['process_started', 'thread.started', 'turn.started']),
    (f"event({{'type': 'thread.started', 'thread_id': {SESSION!r}}})\nevent({{'type': 'turn.completed'}})\n",
     ['process_started', 'thread.started']),
    (f"event({{'type': 'thread.started', 'thread_id': {SESSION!r}}})\n"
     f"event({{'type': 'thread.started', 'thread_id': {SESSION!r}}})\n", ['process_started', 'thread.started']),
    ("event({'type': 'thread.started', 'thread_id': 'not-a-uuid'})\n", ['process_started']),
    ("print('{not json', flush=True)\n", ['process_started']),
    ("event({'type': 'turn.started'})\n", ['process_started']),
    ("success()\nevent({'type': 'turn.completed'})\n", list(EVENTS[:4])),
], ids=['duplicate-turn-started', 'completed-before-started', 'duplicate-thread', 'invalid-session',
        'malformed-line', 'turn-before-session', 'duplicate-completed'])
def test_malformed_duplicate_and_out_of_order_events_are_never_accepted(cli, body, expected):
    events, sink = collect()
    result = cli(body, on_turn_event=sink)
    assert result['status'] == 'blocked' and result['reason'] in ('protocol_error', 'session_mismatch')
    assert kinds(events) == expected + ['cleanup_confirmed']


def test_error_terminal_is_recorded_and_its_category_survives_a_telemetry_failure(cli):
    body = "started()\nevent({'type': 'error', 'message': 'usage limit PRIVATE_ERROR_TEXT'})\n"
    events, sink = collect()
    assert cli(body, on_turn_event=sink)['reason'] == 'quota_or_rate_limit'
    assert kinds(events) == ['process_started', 'thread.started', 'turn.started', 'error', 'cleanup_confirmed']
    assert 'PRIVATE_ERROR_TEXT' not in json.dumps(events)
    events, sink = collect(fail_on='error')
    result = cli(body, on_turn_event=sink)
    assert (result['status'], result['reason']) == ('blocked', 'quota_or_rate_limit')


def test_turn_failed_terminal_under_provider_disposition_is_recorded_once(cli):
    events, sink = collect()
    result = cli("started()\nevent({'type': 'turn.failed', 'error': {'message': 'PRIVATE_ERROR_TEXT'}})\n",
                 on_turn_event=sink, provider_disposition=True)
    assert result['status'] == 'blocked'
    assert kinds(events) == ['process_started', 'thread.started', 'turn.started', 'turn.failed', 'cleanup_confirmed']
    assert 'PRIVATE_ERROR_TEXT' not in json.dumps(events)


# Journal --------------------------------------------------------------------

def event(kind, session=SESSION):
    return {'kind': kind, 'wall': time.time(), 'monotonic': time.monotonic(),
            'session_id': None if kind == 'process_started' else session}


def turn(record, request='dev-model-1', kinds=EVENTS, ticket='ticket-1'):
    for kind in kinds:
        record(event(kind), ticket, request)


@pytest.fixture
def journal(tmp_path):
    root = tmp_path / 'owner'; root.mkdir(mode=0o700)
    made = []

    def make(journal_id=None, **options):
        value = telemetry.TurnTelemetryJournal(root / 'telemetry', journal_id or str(uuid.uuid4()), **options)
        made.append(value)
        return value
    yield make
    for value in made: value.close()


def records_of(value):
    return telemetry.read_journal(value.directory, value.name[len('turns-'):-len('.jsonl')])


def test_journal_is_private_append_only_and_lazy(journal):
    value = journal()
    assert not value.directory.exists()  # Nothing until the first record.
    record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    turn(record)
    info = os.stat(value.path)
    assert oct(info.st_mode & 0o777) == '0o600' and oct(os.stat(value.directory).st_mode & 0o777) == '0o700'
    records, torn = records_of(value)
    assert not torn and [r['seq'] for r in records] == [1, 2, 3, 4, 5]
    assert {r['slot'] for r in records} == {'a'} and {r['operation'] for r in records} == {'coder'}
    assert records[0]['session_id'] is None and records[-1]['session_id'] == SESSION
    assert all(set(r) == telemetry.TURN_FIELDS for r in records)
    [summary] = telemetry.turns(records)
    assert summary['outcome'] == 'completed' and summary['ticket_id'] == 'ticket-1'
    assert value.state() == {'schema': telemetry.SCHEMA, 'file': value.name, 'records': 5,
                             'bytes': info.st_size, 'full': False, 'unavailable': None}


def test_recorder_refuses_text_fields_unknown_kinds_and_invalid_identities(journal):
    record = journal().recorder(operation='coder', slot='a', agent_id='coder-a')
    for bad in ({**event('turn.started'), 'text': 'Final private reply'}, {**event('turn.started'), 'kind': 'item'},
                {**event('turn.started'), 'session_id': 'not-a-uuid'}, {**event('turn.started'), 'wall': float('nan')},
                {**event('turn.started'), 'wall': 1}, ['turn.started']):
        with pytest.raises(DevelopmentError):
            record(bad, 'ticket-1', 'dev-model-1')
    for ticket, request in (('../ticket', 'dev-model-1'), ('ticket-1', ''), (None, 'dev-model-1')):
        with pytest.raises(DevelopmentError):
            record(event('turn.started'), ticket, request)
    for options in ({'operation': 'shell', 'slot': 'a', 'agent_id': 'x'}, {'operation': 'coder', 'slot': 'a/b', 'agent_id': 'x'}):
        with pytest.raises(DevelopmentError):
            journal().recorder(**options)


def test_symlinked_journal_file_is_refused_and_its_target_untouched(journal, tmp_path):
    value = journal(); value.directory.mkdir(mode=0o700)
    target = tmp_path / 'elsewhere'; target.write_bytes(b'')
    os.symlink(target, value.path)
    record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    with pytest.raises(DevelopmentError, match='turn_telemetry_insecure'):
        record(event('process_started'), 'ticket-1', 'dev-model-1')
    assert target.read_bytes() == b''
    with pytest.raises(DevelopmentError, match='turn_telemetry_insecure'):  # Sticky.
        record(event('process_started'), 'ticket-1', 'dev-model-1')


def test_second_writer_on_the_same_file_is_refused(journal):
    journal_id = str(uuid.uuid4())
    first = journal(journal_id); turn(first.recorder(operation='coder', slot='a', agent_id='coder-a'), kinds=EVENTS[:1])
    second = journal(journal_id).recorder(operation='coder', slot='b', agent_id='coder-b')
    with pytest.raises(DevelopmentError, match='turn_telemetry_busy'):
        turn(second, kinds=EVENTS[:1])


def test_disk_full_makes_the_journal_unavailable_without_further_records(journal, monkeypatch):
    value = journal(); record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    turn(record, kinds=EVENTS[:2])
    original = os.write

    def full(fd, data):
        if fd == value._fd:
            raise OSError(errno.ENOSPC, 'No space left on device')
        return original(fd, data)
    monkeypatch.setattr(telemetry.os, 'write', full)
    with pytest.raises(DevelopmentError, match='turn_telemetry_unavailable'):
        record(event('turn.started'), 'ticket-1', 'dev-model-1')
    monkeypatch.setattr(telemetry.os, 'write', original)
    with pytest.raises(DevelopmentError, match='turn_telemetry_unavailable'):  # Never after a failed write.
        record(event('turn.completed'), 'ticket-1', 'dev-model-1')
    records, torn = records_of(value)
    assert [r['kind'] for r in records] == ['process_started', 'thread.started'] and not torn
    assert value.state()['unavailable'] == 'turn_telemetry_unavailable'
    assert telemetry.turns(records)[0]['outcome'] == 'uncertain'


def test_short_write_is_a_torn_tail_never_repaired(journal, monkeypatch):
    value = journal(); record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    turn(record, kinds=EVENTS[:1])
    original = os.write
    monkeypatch.setattr(telemetry.os, 'write', lambda fd, data: original(fd, data[:7]) if fd == value._fd else original(fd, data))
    with pytest.raises(DevelopmentError, match='turn_telemetry_unavailable'):
        record(event('thread.started'), 'ticket-1', 'dev-model-1')
    monkeypatch.setattr(telemetry.os, 'write', original)
    records, torn = records_of(value)
    assert len(records) == 1 and torn
    value.close()
    reopened = journal(value.name[6:-6]).recorder(operation='coder', slot='a', agent_id='coder-a')
    with pytest.raises(DevelopmentError, match='turn_telemetry_corrupt'):
        reopened(event('turn.started'), 'ticket-1', 'dev-model-1')
    assert records_of(value) == (records, True)


def test_full_journal_refuses_new_turns_explicitly_but_finishes_started_ones(journal):
    value = journal(max_records=12, headroom=6)
    a = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    turn(a, 'dev-model-1')  # 5 records
    a(event('process_started'), 'ticket-1', 'dev-model-2')  # 6: a second turn in flight.
    with pytest.raises(DevelopmentError, match='turn_telemetry_full'):
        turn(a, 'dev-model-3', kinds=EVENTS[:1])  # 7 > 12 - 6: refused, marker 7 written.
    assert value.state()['full'] is True
    turn(a, 'dev-model-2', kinds=EVENTS[1:])  # The in-flight turn still completes: 8..11.
    with pytest.raises(DevelopmentError, match='turn_telemetry_full'):
        turn(a, 'dev-model-4', kinds=EVENTS[:1])
    a(event('turn.started'), 'ticket-1', 'dev-model-2')  # 12: hard limit reached exactly.
    with pytest.raises(DevelopmentError, match='turn_telemetry_full'):
        a(event('turn.started'), 'ticket-1', 'dev-model-2')
    records, _ = records_of(value)
    assert len(records) == 12 and [r['kind'] for r in records].count('journal_full') == 1
    assert value.state()['records'] == 12


def test_concurrent_slot_writers_keep_one_contiguous_ordered_journal(journal):
    value = journal(); errors = []

    def slot(name):
        try:
            record = value.recorder(operation='coder', slot=name, agent_id='coder-' + name)
            for index in range(20):
                turn(record, 'dev-model-%d' % index, ticket='ticket-' + name)
        except BaseException as error:  # pragma: no cover - reported below
            errors.append(error)
    threads = [threading.Thread(target=slot, args=('s%d' % i,)) for i in range(8)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(30)
    assert not errors
    records, torn = records_of(value)
    assert not torn and [r['seq'] for r in records] == list(range(1, 8 * 20 * 5 + 1))
    summaries = telemetry.turns(records)
    assert len(summaries) == 160 and {s['outcome'] for s in summaries} == {'completed'}
    for name in ('s%d' % i for i in range(8)):
        mine = [r['kind'] for r in records if r['slot'] == name]
        assert mine == list(EVENTS) * 20


CHILD = textwrap.dedent("""
    import os, sys, time
    from blackoutburst_agents import turn_telemetry as telemetry
    directory, journal_id, mode = sys.argv[1:]
    journal = telemetry.TurnTelemetryJournal(directory, journal_id)
    record = journal.recorder(operation='coder', slot='a', agent_id='coder-a')
    def event(kind):
        return {'kind': kind, 'wall': time.time(), 'monotonic': time.monotonic(),
                'session_id': None if kind == 'process_started' else '11111111-2222-4333-8444-555555555555'}
    for kind in ('process_started', 'thread.started', 'turn.started'):
        record(event(kind), 'ticket-1', 'dev-model-1')
    if mode == 'before':
        original = os.write
        def crash(fd, data):
            if fd == journal._fd: os._exit(9)  # Dies before the terminal is written.
            return original(fd, data)
        telemetry.os.write = crash
    record(event('turn.completed'), 'ticket-1', 'dev-model-1')
    os._exit(9)  # After the write: no close, no cleanup record.
""")


@pytest.mark.parametrize('mode', ['before', 'after'])
def test_crash_before_or_after_a_journal_write_keeps_only_durable_facts(journal, mode):
    value = journal(); value.directory.parent.mkdir(exist_ok=True)
    journal_id = value.name[6:-6]
    environment = {**os.environ, 'PYTHONPATH': os.pathsep.join([str(PACKAGE.parent)] + sys.path)}
    finished = subprocess.run([sys.executable, '-c', CHILD, str(value.directory), journal_id, mode],
                              env=environment, timeout=60)
    assert finished.returncode == 9
    records, torn = records_of(value)
    expected = ['process_started', 'thread.started', 'turn.started'] + (['turn.completed'] if mode == 'after' else [])
    assert [r['kind'] for r in records] == expected and not torn
    [summary] = telemetry.turns(records)
    assert summary['outcome'] == 'uncertain'  # No cleanup record: never completed.
    # The restarted writer appends after the durable tail; recovery may only correlate.
    restarted = journal(journal_id)
    seq = restarted.correlate(3, 'recovered_uncertain', wall=time.time())
    assert seq == len(expected) + 1
    records, _ = records_of(value)
    [summary] = telemetry.turns(records)
    assert summary['outcome'] == 'uncertain' and summary['correlations'] == [{'seq': seq, 'reason': 'recovered_uncertain'}]
    assert summary['terminals'] == (['turn.completed'] if mode == 'after' else [])


def test_correlation_never_invents_a_terminal_or_references_the_future(journal):
    value = journal(); record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    turn(record, kinds=EVENTS[:3])
    for refers, reason in ((4, 'recovered_uncertain'), (0, 'recovered_uncertain'), (1, 'turn.completed'), (True, 'superseded')):
        with pytest.raises(DevelopmentError):
            value.correlate(refers, reason, wall=time.time())
    value.correlate(1, 'superseded', wall=time.time())
    [summary] = telemetry.turns(records_of(value)[0])
    assert summary['outcome'] == 'uncertain' and summary['terminals'] == []


def test_duplicate_or_out_of_order_records_never_summarize_as_completed(journal):
    value = journal(); record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    turn(record, 'dev-model-1', kinds=('process_started', 'thread.started', 'turn.started', 'turn.started',
                                        'turn.completed', 'cleanup_confirmed'))
    turn(record, 'dev-model-2', kinds=('process_started', 'thread.started', 'turn.completed', 'cleanup_confirmed'))
    turn(record, 'dev-model-3', kinds=('process_started', 'thread.started', 'turn.started', 'turn.completed',
                                        'cleanup_failed'))
    turn(record, 'dev-model-4', kinds=('process_started', 'thread.started', 'turn.started', 'error', 'cleanup_confirmed'))
    outcomes = [s['outcome'] for s in telemetry.turns(records_of(value)[0])]
    assert outcomes == ['uncertain', 'uncertain', 'uncertain', 'failed']


# Runner + RunStore: a telemetry failure never succeeds and never replays ------

class Journaled:
    """Adapter shaped like the planner's binding: one ticket/request per turn."""
    def __init__(self, record, request):
        self.record, self.request = record, request

    def __call__(self, event):
        return self.record(event, 'ticket-1', self.request)


@pytest.mark.parametrize('failing', ['process_started', 'turn.completed', 'cleanup_confirmed'])
def test_journal_failure_through_the_runner_is_uncertain_and_never_replayed(cli, journal, monkeypatch, tmp_path, failing):
    value = journal(); record = value.recorder(operation='coder', slot='a', agent_id='coder-a')
    settings = cli.make('success()\n')
    store = RunStore(tmp_path / 'runs')
    work = store.create_work(owner_id='owner', agent_id='coder-a', project_id='project', ticket_id='ticket-1',
                             workspace=str(cli.workspace), executable=str(settings.executable), model=None)
    calls = []
    original = os.write

    def transport(settings_, prompt, workspace, **callbacks):
        calls.append(prompt)
        adapter = Journaled(record, 'dev-model-1')

        def guarded(event):
            if event['kind'] == failing:
                monkeypatch.setattr(telemetry.os, 'write', lambda fd, data: (_ for _ in ()).throw(
                    OSError(errno.ENOSPC, 'full')) if fd == value._fd else original(fd, data))
            return adapter(event)
        return execute_turn(settings, prompt, workspace, on_turn_event=guarded, **callbacks)
    runner = PersonalRunner(store, transport)
    first = runner.run(work['id'], 'dev-model-1', 'Private prompt', timeout=5)
    monkeypatch.setattr(telemetry.os, 'write', original)
    assert first['result']['status'] == 'blocked' and first['result']['reason'] == 'callback_failed'
    assert not first['result'].get('text')
    assert store.get_work(work['id'])['status'] == 'needs_review'  # Uncertain: a human decides.
    again = runner.run(work['id'], 'dev-model-1', 'Private prompt', timeout=5)
    assert again['cached'] is True and again['result']['status'] == 'blocked' and len(calls) == 1
    with pytest.raises(StoreError, match='work_needs_review'):
        runner.run(work['id'], 'dev-model-2', 'Private prompt', timeout=5)
    assert len(calls) == 1
    records = records_of(value)[0] if value.path.exists() else []
    assert failing not in [r['kind'] for r in records]
    assert all(summary['outcome'] == 'uncertain' for summary in telemetry.turns(records))
    store.close()


def test_telemetry_module_stays_outside_the_native_roster_and_no_roster_module_imports_it():
    assert 'turn_telemetry.py' not in LINUX_EXECUTION_SOURCES
    for name in LINUX_EXECUTION_SOURCES:
        source = (PACKAGE / name).read_text(encoding='utf-8')
        assert 'turn_telemetry' not in source, name


def test_generation_hands_the_service_turn_event_to_its_worker_only():
    from contextlib import ExitStack
    from types import SimpleNamespace
    from blackoutburst_agents.continuation_pool_service import ContinuationPoolService, _Generation
    from blackoutburst_agents.development_worker import DevelopmentBoardWorker
    assert ContinuationPoolService.turn_event is None and DevelopmentBoardWorker.turn_event is None
    sink = object(); opened = []

    class Driver:
        selections = SimpleNamespace(fixed={'owner_id': 'owner'}, connection_pin={'identity': [1, 2]})

        def _open(self, resources, context, bound, *, existing=False, observe=False):
            opened.append(bound); return SimpleNamespace(journal=SimpleNamespace(directory='/synthetic'))
    generation = _Generation()
    generation._worker_open, generation.admission, generation.operation = False, None, 'operation'
    generation.driver, generation.job = Driver(), {'project_id': 'project', 'ticket_id': 'ticket'}
    generation.context = SimpleNamespace(session_id='session')
    generation.control = SimpleNamespace(add_worker=lambda *args: 'record', remove_worker=lambda record: None)
    generation.service = SimpleNamespace(turn_event=sink)
    with ExitStack() as generation.resources:
        generation.open_worker(unprepared=True)
    assert generation.worker.turn_event is sink and len(opened) == 1
