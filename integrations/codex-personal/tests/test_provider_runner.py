"""Opt-in runner handshakes: real temporary stores/Board, fake CLI only."""
import copy
from functools import partial
import json
import pickle
import threading

import pytest

from blackoutburst_agents import codex_turn, provider_disposition as disposition
from blackoutburst_agents.run_store import RunStore, StoreError
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.runtime_guard import RuntimeGuard
import test_codex_turn as turn_helpers
import test_runtime_guard_security as runtime_helpers

GENERATION = '11111111-2222-4333-8444-111111111111'
CYCLE = '11111111-2222-4333-8444-222222222222'
ATTEMPT = '11111111-2222-4333-8444-333333333333'


@pytest.fixture
def context():
    if runtime_helpers.Board is None:
        pytest.skip('Board sources not bundled')
    turn = turn_helpers.CodexTurnTests(methodName='runTest')
    runtime = runtime_helpers.RuntimeGuardSecurityTests(methodName='runTest')
    turn.setUp()
    runtime.setUp()
    store = RunStore(runtime.root / 'provider-runs')
    executable = turn.fake_cli("started()\nevent({'type':'turn.failed','error':{'message':'quota PRIVATE'}})\nraise SystemExit(1)")
    work = store.create_work(**{key: runtime.work[key] for key in
        ('owner_id', 'agent_id', 'project_id', 'ticket_id', 'workspace', 'model')}, executable=str(executable))
    guard = runtime.guard()
    receipts = []
    runner = PersonalRunner(store, codex_turn.execute_turn, runtime_guard=guard)
    def attempt(request='request', **changes):
        return disposition.enroll_provider_attempt(**{'task_id': 'task-1', 'generation_id': GENERATION, 'cycle_id': CYCLE, 'attempt_id': ATTEMPT,
            'work_id': work['id'], 'request_id': request, 'scope_sha256': disposition._sha(guard.scope),
            'on_receipt': receipts.append, **changes})
    try:
        yield {'turn': turn, 'runtime': runtime, 'store': store, 'work': work,
            'guard': guard, 'runner': runner, 'receipts': receipts, 'attempt': attempt}
    finally:
        store.close()
        runtime.doCleanups()
        turn.doCleanups()


def run(context, *, attempt=None):
    return context['runner'].run(context['work']['id'], 'request', 'PRIVATE prompt', timeout=3,
        provider_attempt=attempt or context['attempt']())


def bindings(context, response, receipt):
    return {'task_id': 'task-1', 'generation_id': GENERATION, 'cycle_id': CYCLE, 'attempt_id': ATTEMPT, 'work_id': context['work']['id'],
        'turn_id': response['turn_id'], 'request_id': 'request',
        'scope_sha256': disposition._sha(context['guard'].scope),
        'result_sha256': disposition._sha(response['result']),
        'work_state_sha256': disposition._sha(context['store'].status(context['work']['id']))}


def test_receipt_is_issued_under_lock_after_durable_result_and_fresh_runtime_finish(context):
    checks = []
    def received(receipt):
        store, guard = context['store'], context['guard']
        store._require_lock(context['work']['id'])
        turn_id = json.loads(receipt.metadata_json)['turn_id']
        assert store.turn_result(turn_id)['status'] == 'blocked'
        assert guard.operation['phase'] == 'finished'
        assert context['runtime'].client.calls[-1][0] == 'lookup_execution'
        checks.append(receipt)
    response = run(context, attempt=context['attempt'](on_receipt=received))
    assert len(checks) == 1
    receipt = checks[0]
    metadata = disposition.consume_provider_receipt(receipt, **bindings(context, response, receipt))
    assert metadata['classification'] == 'provider_unavailable' and metadata['cause'] == 'unknown'
    assert metadata['action_accepted'] is False and metadata['cleanup_confirmed'] is True
    assert 'PRIVATE' not in receipt.metadata_json and str(context['runtime'].root) not in receipt.metadata_json
    assert context['store'].get_work(context['work']['id'])['status'] == 'needs_review'
    assert context['store']._turn(response['turn_id'])['requires_review']
    assert context['guard'].operation['receipt']['state'] == 'finished'
    with pytest.raises(ValueError, match='provider_receipt_unavailable'):
        disposition.consume_provider_receipt(receipt, **bindings(context, response, receipt))


@pytest.mark.parametrize('field,change', [('task_id', 'other-task'), ('generation_id', ATTEMPT), ('cycle_id', ATTEMPT), ('attempt_id', CYCLE),
    ('work_id', '11111111-2222-4333-8444-555555555555'), ('request_id', 'other-request'),
    ('turn_id', '11111111-2222-4333-8444-555555555555'), ('scope_sha256', '0' * 64),
    ('result_sha256', '0' * 64), ('work_state_sha256', '0' * 64)])
def test_receipt_refuses_cross_binding_without_consuming(context, field, change):
    response = run(context)
    receipt = context['receipts'][0]
    expected = bindings(context, response, receipt)
    with pytest.raises(ValueError, match='provider_receipt_mismatch'):
        disposition.consume_provider_receipt(receipt, **{**expected, field: change})
    assert disposition.consume_provider_receipt(receipt, **expected)['work_id'] == context['work']['id']


def test_serialized_receipt_is_not_authority(context):
    response = run(context)
    receipt = context['receipts'][0]
    with pytest.raises(ValueError, match='provider_receipt_unavailable'):
        disposition.consume_provider_receipt(json.loads(receipt.metadata_json), **bindings(context, response, receipt))


def test_attempt_is_one_use_and_cached_turn_cannot_issue_a_new_receipt(context):
    attempt = context['attempt']()
    response = run(context, attempt=attempt)
    assert len(context['receipts']) == 1
    with pytest.raises(ValueError, match='provider_attempt_mismatch'):
        run(context, attempt=attempt)
    again = run(context, attempt=context['attempt']())
    assert again['cached'] is True and again['turn_id'] == response['turn_id']
    assert len(context['receipts']) == 1


@pytest.mark.parametrize('change', [{'work_id': '11111111-2222-4333-8444-555555555555'},
    {'request_id': 'different'}, {'scope_sha256': '0' * 64}])
def test_wrong_enrollment_is_rejected_before_begin_or_model(context, change):
    with pytest.raises(ValueError, match='provider_attempt_mismatch'):
        run(context, attempt=context['attempt'](**change))
    assert context['store'].status(context['work']['id'])['turns'] == []
    assert context['runtime'].client.calls == []


def test_unknown_runtime_or_transport_is_not_a_producer(context):
    context['runner'].transport = lambda *a, **k: pytest.fail('must not run')
    with pytest.raises(ValueError, match='provider_transport_not_supported'):
        run(context)
    context['runner'].transport = codex_turn.execute_turn
    context['runner'].runtime_guard = None
    with pytest.raises(ValueError, match='provider_attempt_mismatch'):
        run(context)
    assert context['store'].status(context['work']['id'])['turns'] == []


def test_qualified_partial_keeps_model_policy_optin(context):
    context['runner'].transport = partial(codex_turn.execute_turn, reasoning_effort='low')
    run(context)
    assert len(context['receipts']) == 1


def test_legacy_transport_gets_no_new_kwargs_by_omission(context):
    callbacks_seen = []
    def transport(settings, prompt, workspace, **callbacks):
        callbacks_seen.append(set(callbacks))
        callbacks['on_started'](999999999)
        callbacks['on_session'](turn_helpers.SESSION)
        return {'status': 'completed', 'reason': 'verified', 'session_id': turn_helpers.SESSION,
            'text': 'PRIVATE', 'verification': {'cleanup_confirmed': True}}
    context['runner'].transport = transport
    context['runner'].run(context['work']['id'], 'request', 'PRIVATE')
    assert callbacks_seen == [{'resume_session_id', 'on_started', 'on_session', 'should_cancel'}]
    assert context['receipts'] == []


def test_lost_final_lookup_or_callback_never_reopens_work(context):
    original_call = context['runtime'].client.call
    def call(name, args):
        if name == 'lookup_execution' and context['guard'].operation['phase'] == 'finished':
            raise TimeoutError('PRIVATE_RUNTIME_DIAGNOSTIC')
        return original_call(name, args)
    context['runtime'].client.call = call
    with pytest.raises(StoreError, match='provider_disposition_requires_review') as error:
        run(context)
    assert 'PRIVATE' not in str(error.value)
    assert context['store'].get_work(context['work']['id'])['status'] == 'needs_review'
    assert context['receipts'] == []


def test_fresh_runtime_stop_prevents_receipt(context):
    original_call = context['runtime'].client.call
    def call(name, args):
        value = original_call(name, args)
        if name == 'lookup_execution' and context['guard'].operation['phase'] == 'finished':
            value = copy.deepcopy(value)
            value['execution']['stop_requested'] = True
        return value
    context['runtime'].client.call = call
    run(context)
    assert context['receipts'] == []


def test_late_cancel_after_runtime_finish_prevents_receipt(context):
    context['runner'].run(context['work']['id'], 'request', 'PRIVATE', timeout=3,
        provider_attempt=context['attempt'](),
        should_cancel=lambda: bool(context['guard'].operation and context['guard'].operation['phase'] == 'finished'))
    assert context['receipts'] == []


def test_receipt_clock_and_process_change_refused(context, monkeypatch):
    response = run(context)
    receipt = context['receipts'][0]
    expected = bindings(context, response, receipt)
    with monkeypatch.context() as patch:
        patch.setattr(disposition.time, 'monotonic', lambda: receipt.monotonic_at + 121)
        with pytest.raises(ValueError, match='provider_receipt_mismatch'):
            disposition.consume_provider_receipt(receipt, **expected)
    with monkeypatch.context() as patch:
        patch.setattr(disposition.os, 'getpid', lambda: receipt._pid + 1)
        with pytest.raises(ValueError, match='provider_receipt_unavailable'):
            disposition.consume_provider_receipt(receipt, **expected)


def test_stale_enrollment_refused_without_runtime_or_turn(context, monkeypatch):
    attempt = context['attempt']()
    monkeypatch.setattr(disposition.time, 'monotonic', lambda: attempt.monotonic_at + 121)
    with pytest.raises(ValueError, match='provider_attempt_mismatch'):
        run(context, attempt=attempt)
    assert context['store'].status(context['work']['id'])['turns'] == []


@pytest.mark.parametrize('changes', [{'generation_id': CYCLE}, {'cycle_id': ATTEMPT},
    {'attempt_id': 'not-a-uuid'}])
def test_generation_cycle_attempt_ids_must_be_canonical_distinct(context, changes):
    with pytest.raises(ValueError):
        context['attempt'](**changes)


@pytest.mark.parametrize('copier', [copy.copy, copy.deepcopy, pickle.dumps])
def test_one_use_enrollment_and_receipt_cannot_be_duplicated(context, copier):
    attempt = context['attempt']()
    with pytest.raises(TypeError, match='provider_ephemeral_copy_forbidden'):
        copier(attempt)
    response = run(context, attempt=attempt)
    receipt = context['receipts'][0]
    with pytest.raises(TypeError, match='provider_ephemeral_copy_forbidden'):
        copier(receipt)
    disposition.consume_provider_receipt(receipt, **bindings(context, response, receipt))


def test_one_use_objects_cannot_cross_threads(context):
    attempt = context['attempt']()
    errors = []
    def claim():
        try:
            disposition._claim_provider_attempt(attempt, context['work'], 'request',
                codex_turn.execute_turn, context['guard'])
        except ValueError as error:
            errors.append(str(error))
    thread = threading.Thread(target=claim)
    thread.start()
    thread.join()
    assert errors == ['provider_attempt_mismatch']
    response = run(context, attempt=attempt)
    receipt = context['receipts'][0]
    expected = bindings(context, response, receipt)
    def consume():
        try:
            disposition.consume_provider_receipt(receipt, **expected)
        except ValueError as error:
            errors.append(str(error))
    thread = threading.Thread(target=consume)
    thread.start()
    thread.join()
    assert errors == ['provider_attempt_mismatch', 'provider_receipt_unavailable']
    disposition.consume_provider_receipt(receipt, **expected)
