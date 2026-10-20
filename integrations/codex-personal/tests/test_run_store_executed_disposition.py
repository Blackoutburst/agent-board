"""Finite human disposition: real temporary journals, no process or RPC."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

from blackoutburst_agents.message_scope import local_agent
from blackoutburst_agents.run_store import RunStore, StoreError
from blackoutburst_agents.runtime_journal import RuntimeJournal


@pytest.fixture
def case(tmp_path):
    store = RunStore(tmp_path / 'runs')
    runtime = RuntimeJournal(tmp_path / 'runtime')
    scope = {'board_endpoint': 'https://board.example.invalid/mcp',
        'board_session_id': 'original-board-session', 'owner_id': 'owner',
        'agent_id': 'profile', 'scope_kind': 'message', 'thread_id': 'thread',
        'message_id': 'message', 'source_sha256': 'a' * 64}
    work = store.create_work(owner_id=scope['owner_id'], agent_id=local_agent(scope),
        project_id=scope['thread_id'], ticket_id=scope['message_id'],
        workspace=tmp_path, executable=sys.executable, model='requested-model')
    session = str(uuid.uuid4())
    result = {'status': 'blocked', 'reason': 'protocol_error', 'session_id': session,
        'text': None, 'verification': {'cleanup_confirmed': True, 'exit_zero': False,
            'session_verified': True, 'tool_free': True, 'turn_completed': False,
            'authentication': 'chatgpt', 'cli_version': 'synthetic'}}
    with store.work_lock(work['id']):
        turn_id = store.begin_turn(work['id'], 'original-request', 'PRIVATE ORIGINAL PROMPT')['turn']['id']
        store.on_process(turn_id, os.getpid())
        store.on_session(turn_id, session)
        store.finish_turn(turn_id, result)
    config = RuntimeJournal.configuration(scope, work['id'], turn_id, work['model'])
    core = {**{k: scope[k] for k in ('owner_id', 'agent_id', 'scope_kind',
            'thread_id', 'message_id', 'source_sha256')},
        'session_id': scope['board_session_id'], 'device_id': 'original-device',
        'profile_version': 7, 'work_id': work['id'], 'turn_id': turn_id,
        'provider': 'codex_chatgpt', 'model': work['model'], 'id': 'remote-runtime',
        'revision': 4, 'state': 'finished', 'outcome': 'blocked',
        'reason': 'protocol_error', 'cleanup_confirmed': True}
    operation, created = runtime.begin(config)
    assert created
    for phase in ('reserved', 'starting', 'active', 'finished'):
        operation = runtime.update(operation['id'], operation['revision'], phase,
            core, start_confirmed=phase == 'active')
    turn = store._turn(turn_id)
    initial = store.interruption_state_sha256(work['id'])
    intent_id = str(uuid.uuid4())
    proof = {'schema': 'message-executed-runstore-proof-v1', 'intent_id': intent_id,
        'state_sha256': initial,
        'result_sha256': hashlib.sha256(turn['result_json'].encode()).hexdigest(),
        'configuration': config,
        'identity': {k: core[k] for k in ('owner_id', 'agent_id', 'device_id',
            'profile_version', 'session_id')},
        'local_runtime': {**{k: operation[k] for k in ('id', 'key', 'phase', 'revision')},
            'start_confirmed': operation['start_confirmed'] == 1,
            'receipt': copy.deepcopy(core)},
        'lookup': copy.deepcopy(core), 'control': copy.deepcopy(core)}
    data = {'store': store, 'runtime': runtime, 'work': store.get_work(work['id']),
        'turn': turn, 'proof': proof, 'initial': initial, 'intent_id': intent_id,
        'database': tmp_path / 'runs' / 'runs.sqlite3',
        'runtime_database': tmp_path / 'runtime' / 'runtime.sqlite3'}
    yield data
    runtime.close()
    store.close()


def dispose(case, **changes):
    values = {'expected_state_sha256': case['initial'], 'intent_id': case['intent_id'],
              'proof': case['proof']}
    values.update(changes)
    return case['store']._dispose_executed_blocked(case['work']['id'], **values)


def digest_rows(store):
    return {table: [dict(row) for row in store._db.execute('SELECT * FROM ' + table + ' ORDER BY rowid')]
            for table in ('works', 'turns', 'context_guards')}


def test_positive_cleanup_closes_only_work_and_preserves_original_turn(case, monkeypatch):
    store = case['store']
    before = digest_rows(store)
    runtime_bytes = case['runtime_database'].read_bytes()
    # A currently live/reused PID must not be signalled or probed at all.
    def forbidden(*args, **kwargs):
        pytest.fail('disposition probed a PID/PGID')
    monkeypatch.setattr('blackoutburst_agents.run_store.os.kill', forbidden)
    monkeypatch.setattr('blackoutburst_agents.run_store.os.killpg', forbidden)
    with store.work_lock(case['work']['id'], inspection_only=True):
        result = dispose(case)
    after = digest_rows(store)
    assert result['status'] == 'closed'
    assert after['works'] == [{**before['works'][0], 'status': 'closed',
        'updated_at': after['works'][0]['updated_at']}]
    assert after['turns'] == [{**before['turns'][0], 'resolution': 'abandon',
        'resolution_reason': 'message_executed_blocked_disposed:' + case['intent_id'],
        'updated_at': after['turns'][0]['updated_at']}]
    assert after['turns'][0]['requires_review'] == 1
    assert after['turns'][0]['result_json'] == case['turn']['result_json']
    assert after['context_guards'] == before['context_guards']
    assert case['runtime_database'].read_bytes() == runtime_bytes
    assert store._db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert 'PRIVATE ORIGINAL PROMPT' not in case['database'].read_text(errors='ignore')
    with store.work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError, match='work_closed'):
            store.begin_turn(case['work']['id'], 'different-request', 'not replayed')


def test_real_lock_and_original_cas_required(case):
    store = case['store']
    before = case['database'].read_bytes()
    with pytest.raises(StoreError, match='work_lock_required'):
        dispose(case)
    with store.work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError, match='interruption_state_conflict'):
            dispose(case, expected_state_sha256='0' * 64)
    assert case['database'].read_bytes() == before


@pytest.mark.parametrize(('path', 'value'), [
    (('schema',), 'message-local-abandonment-v1'),
    (('intent_id',), 'other-intent'),
    (('state_sha256',), 'f' * 64),
    (('result_sha256',), 'f' * 64),
    (('configuration', 'model'), 'other-model'),
    (('configuration', 'work_id'), str(uuid.uuid4())),
    (('configuration', 'scope', 'source_sha256'), 'f' * 64),
    (('configuration', 'provider'), 'local_broker'),
    (('identity', 'profile_version'), True),
    (('identity', 'device_id'), 'other-device'),
    (('local_runtime', 'id'), 'not-a-uuid'),
    (('local_runtime', 'key'), 'runtime-turn-wrong'),
    (('local_runtime', 'phase'), 'needs_review'),
    (('local_runtime', 'start_confirmed'), 1),
    (('local_runtime', 'revision'), True),
    (('lookup', 'revision'), True),
    (('control', 'revision'), 5),
    (('control', 'cleanup_confirmed'), 1),
    (('control', 'state'), 'running'),
    (('control', 'outcome'), 'completed'),
    (('control', 'session_id'), 'other-board-session'),
    (('control', 'reason'), 'cleanup_failed'),
    (('control', 'unexpected'), 'SECRET SENTINEL'),
    (('unexpected',), 'SECRET SENTINEL'),
])
def test_modified_or_malformed_evidence_refused_without_writes(case, path, value):
    proof = copy.deepcopy(case['proof'])
    target = proof
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    before = case['database'].read_bytes()
    with case['store'].work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError) as caught:
            dispose(case, proof=proof)
    assert 'SECRET' not in str(caught.value)
    assert case['database'].read_bytes() == before


@pytest.mark.parametrize('flag', ['cleanup_confirmed', 'exit_zero', 'session_verified',
                                 'tool_free', 'turn_completed'])
def test_transport_cleanup_and_diagnostic_flags_are_independent_gates(case, flag):
    store = case['store']
    result = store.turn_result(case['turn']['id'])
    result['verification'][flag] = not result['verification'][flag]
    raw = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
    store._db.execute('UPDATE turns SET result_json=? WHERE id=?', (raw, case['turn']['id']))
    proof = copy.deepcopy(case['proof'])
    proof['state_sha256'] = store.interruption_state_sha256(case['work']['id'])
    proof['result_sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    before = case['database'].read_bytes()
    with store.work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError, match='executed_disposition_proof_invalid'):
            dispose(case, expected_state_sha256=proof['state_sha256'], proof=proof)
    assert case['database'].read_bytes() == before


@pytest.mark.parametrize(('column', 'value'), [
    ('status', 'completed'), ('pid', None), ('provider_session_id', None),
    ('requires_review', 0), ('resolution', 'abandon'),
])
def test_never_admitted_completed_or_previously_resolved_turns_not_adopted(case, column, value):
    store = case['store']
    store._db.execute('UPDATE turns SET ' + column + '=? WHERE id=?', (value, case['turn']['id']))
    proof = copy.deepcopy(case['proof'])
    proof['state_sha256'] = store.interruption_state_sha256(case['work']['id'])
    before = case['database'].read_bytes()
    with store.work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError, match='executed_disposition_predecessor_invalid'):
            dispose(case, expected_state_sha256=proof['state_sha256'], proof=proof)
    assert case['database'].read_bytes() == before


def test_cold_replay_cannot_reuse_proof_or_adopt_closed_successor(case):
    store = case['store']
    with store.work_lock(case['work']['id'], inspection_only=True):
        dispose(case)
    before = case['database'].read_bytes()
    second = RunStore(store.state_dir, existing_only=True)
    try:
        with second.work_lock(case['work']['id'], inspection_only=True):
            with pytest.raises(StoreError, match='interruption_state_conflict'):
                second._dispose_executed_blocked(case['work']['id'],
                    expected_state_sha256=case['initial'], intent_id=case['intent_id'], proof=case['proof'])
            proof = copy.deepcopy(case['proof'])
            current = second.interruption_state_sha256(case['work']['id'])
            proof['state_sha256'] = current
            with pytest.raises(StoreError, match='executed_disposition_predecessor_invalid'):
                second._dispose_executed_blocked(case['work']['id'],
                    expected_state_sha256=current, intent_id=case['intent_id'], proof=proof)
        assert case['database'].read_bytes() == before
    finally:
        second.close()


def test_second_connection_mutation_is_not_rebased(case):
    store = case['store']
    second = RunStore(store.state_dir, existing_only=True)
    try:
        with store.work_lock(case['work']['id'], inspection_only=True):
            second._db.execute('UPDATE turns SET cancel_requested=1 WHERE id=?', (case['turn']['id'],))
            before = case['database'].read_bytes()
            with pytest.raises(StoreError, match='interruption_state_conflict'):
                dispose(case)
            assert case['database'].read_bytes() == before
    finally:
        second.close()


@pytest.mark.parametrize('schema', ['main', 'temp'])
def test_triggers_cannot_mutate_or_suppress_review(case, schema):
    store = case['store']
    store._db.execute('CREATE ' + ('TEMP ' if schema == 'temp' else '') + '''TRIGGER malicious
        AFTER UPDATE OF resolution ON turns BEGIN
        UPDATE turns SET requires_review=0;
        END''')
    before = digest_rows(store)
    with store.work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError, match='state_schema_invalid'):
            dispose(case)
    assert digest_rows(store) == before


def test_postcondition_and_schema_marker_failure_roll_back_both_updates(case, monkeypatch):
    store = case['store']
    before = case['database'].read_bytes()
    def clock():
        # Pure in-process fault injection after evidence validation, before writes.
        store._db.execute('PRAGMA user_version=1')
        return 2000000000.0
    monkeypatch.setattr('blackoutburst_agents.run_store.time.time', clock)
    with store.work_lock(case['work']['id'], inspection_only=True):
        with pytest.raises(StoreError, match='state_schema_invalid'):
            dispose(case)
    assert case['database'].read_bytes() == before


def test_other_historical_children_remain_exact(case):
    store = case['store']
    old = store.create_work(owner_id='owner', agent_id='historical-agent', project_id='old-thread',
        ticket_id='old-message', workspace=store.state_dir, executable=sys.executable, model='old-model')
    with store.work_lock(old['id']):
        old_turn = store.begin_turn(old['id'], 'old-request', 'old private input')['turn']['id']
        store.finish_turn(old_turn, {'status': 'blocked', 'reason': 'runtime_start_requires_review'})
    old_work, old_turn_row = store._work(old['id']), store._turn(old_turn)
    with store.work_lock(case['work']['id'], inspection_only=True):
        dispose(case)
    assert store._work(old['id']) == old_work
    assert store._turn(old_turn) == old_turn_row
