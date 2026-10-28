"""Synthetic local CLI streams and usage snapshots; no model or network."""
import copy
from dataclasses import asdict
import json
import time

import pytest

from blackoutburst_agents import codex_turn, provider_disposition as disposition
from blackoutburst_agents.subscription_probe import ProbeSettings
import test_codex_turn as turn_helpers


@pytest.fixture
def turn():
    helper = turn_helpers.CodexTurnTests(methodName='runTest')
    helper.setUp()
    try:
        yield helper
    finally:
        helper.doCleanups()


def failed(message='Usage limit reached'):
    return "started()\nevent({'type':'turn.failed','error':{'message':" + repr(message) + "}})\nraise SystemExit(1)\n"


def observed_turn(turn, body=None, **kwargs):
    observations = []
    result = turn.run_turn(body or failed(), provider_disposition=True,
        on_provider_observation=observations.append, **kwargs)
    return result, observations


@pytest.mark.parametrize('message', ['quota CANARY_PRIVATE_ERROR', 'context window full',
    'authentication required', 'unknown provider failure'])
def test_clean_terminal_failure_is_general_candidate_without_claiming_cause(turn, message):
    result, observations = observed_turn(turn, failed(message))
    assert len(observations) == 1
    candidate = disposition.classify_provider_result(result, observations[0], enabled=True)
    assert candidate['classification'] == 'provider_unavailable'
    assert candidate['cause'] == 'unknown'
    assert candidate['pause_candidate'] is True and candidate['resumable'] is False
    assert candidate['reset_at'] is None
    assert message not in json.dumps(candidate)
    assert 'provider' not in result and result['text'] is None


def test_omitted_and_false_keep_legacy_result_and_arguments(turn):
    first = turn.run_turn(failed())
    second = turn.run_turn(failed(), provider_disposition=False)
    assert first == second
    assert set(first) == {'status', 'reason', 'session_id', 'text', 'verification'}
    assert disposition.classify_provider_result(first) is None
    assert disposition.classify_provider_availability() is None


@pytest.mark.parametrize('body', [
    "event({'type':'turn.failed','error':{'message':'quota'}})",
    "started()\nevent({'type':'error','message':'quota'})",
    "started()\nevent({'type':'turn.failed','error':{'message':'quota','code':'rate_limit'}})",
    "started()\nevent({'type':'turn.failed','error':'quota'})",
    "started()\nevent({'type':'item.completed','item':{'type':'command_execution','command':'PRIVATE'}})",
    "started()\nmessage('PRIVATE action')\nevent({'type':'turn.failed','error':{'message':'quota'}})",
    failed() .replace('raise SystemExit(1)', "event({'type':'turn.completed'})"),
    failed().replace('raise SystemExit(1)', "event({'type':'turn.failed','error':{'message':'quota'}})"),
    failed().replace('raise SystemExit(1)', "os.write(1,b'invalid json\\n')"),
    "success()\nevent({'type':'turn.failed','error':{'message':'quota'}})",
])
def test_ambiguous_or_tool_or_nonterminal_stream_has_no_observation(turn, body):
    result, observations = observed_turn(turn, body)
    assert observations == []
    assert disposition.classify_provider_result(result, enabled=True)['classification'] == 'needs_review'


def test_resume_session_mismatch_never_issues_observation(turn):
    result, observations = observed_turn(turn, resume_session_id=turn_helpers.OTHER_SESSION)
    assert result['reason'] == 'session_mismatch' and not observations


def test_cleanup_failure_never_issues_observation(turn, monkeypatch):
    original = codex_turn._stop_group
    def cleanup(process):
        original(process)
        return False
    monkeypatch.setattr(codex_turn, '_stop_group', cleanup)
    result, observations = observed_turn(turn)
    assert result['reason'] == 'cleanup_failed' and not observations


def test_process_group_must_be_observed_absent_after_cleanup(turn, monkeypatch):
    monkeypatch.setattr(codex_turn, '_group_absent', lambda _: False)
    result, observations = observed_turn(turn)
    assert result['verification']['cleanup_confirmed'] is True
    assert observations == []  # Cleanup intent alone is insufficient.


def test_terminal_stream_followed_by_signal_exit_is_not_natural_failure(turn):
    result, observations = observed_turn(turn,
        failed().replace('raise SystemExit(1)', 'os.kill(os.getpid(), signal.SIGTERM)'))
    assert result['status'] == 'blocked' and not observations


def test_successful_optin_turn_does_not_issue_failure_observation(turn):
    result, observations = observed_turn(turn, 'success()')
    assert result['status'] == 'completed' and not observations


def test_failed_terminal_stream_drain_is_bounded_without_eof(turn):
    settings = ProbeSettings(turn.fake_cli(failed().replace('raise SystemExit(1)', 'time.sleep(5)')),
        timeout_seconds=2, metadata_timeout_seconds=2)
    started = time.monotonic()
    result, observations = observed_turn(turn, settings=settings)
    assert time.monotonic() - started < 3
    assert result['reason'] == 'timeout' and not observations


def test_late_cancel_after_terminal_eof_prevents_observation(turn):
    observations = []
    original_issue = disposition._issue_terminal_observation
    # Last cancellation check runs after exit/cleanup and before issuing proof.
    original_stop = codex_turn._stop_group
    cleaned = []
    def stop(process):
        result = original_stop(process)
        cleaned.append(True)
        return result
    from unittest.mock import patch
    with patch.object(codex_turn, '_stop_group', stop):
        result = turn.run_turn(failed(), provider_disposition=True,
            on_provider_observation=observations.append, should_cancel=lambda: bool(cleaned))
    assert result['reason'] == 'cancelled' and not observations
    assert disposition._issue_terminal_observation is original_issue


def test_observation_is_ephemeral_bound_to_result_and_fresh(turn, monkeypatch):
    result, observations = observed_turn(turn)
    proof = observations[0]
    assert disposition.classify_provider_result(result, proof, enabled=True)['pause_candidate']
    changed = copy.deepcopy(result)
    changed['reason'] = 'other'
    assert not disposition.classify_provider_result(changed, proof, enabled=True)['pause_candidate']
    assert not disposition.classify_provider_result(result, asdict(proof), enabled=True)['pause_candidate']
    monkeypatch.setattr(disposition.time, 'monotonic', lambda: proof.monotonic_at + 121)
    assert not disposition.classify_provider_result(result, proof, enabled=True)['pause_candidate']


def test_no_optin_no_callback_or_extra_transport_kwargs(turn):
    with pytest.raises(ValueError):
        disposition.classify_provider_result({}, enabled=1)
    result = turn.run_turn(failed(), on_provider_observation=lambda _: None)
    assert result['reason'] == 'invalid_input'
    result = turn.run_turn(failed(), provider_disposition=1)
    assert result['reason'] == 'invalid_input'


def test_callback_failure_keeps_result_blocked(turn):
    def fail(_):
        raise RuntimeError('PRIVATE')
    result = turn.run_turn(failed(), provider_disposition=True, on_provider_observation=fail)
    assert result['reason'] == 'callback_failed'
    assert 'PRIVATE' not in json.dumps(result)


def usage(allowed, *, status='observed'):
    from blackoutburst_agents.usage_observation import normalize_response, unavailable
    if status != 'observed':
        snapshot = unavailable('transport_error', '0.163.0')
    else:
        snapshot = normalize_response({'ordinaryUsageAllowed': allowed, 'PRIVATE': 'CANARY',
            'rateLimits': {'primary': {'usedPercent': 100, 'windowDurationMins': 300,
                'resetsAt': 2000000000}}}, '0.163.0')
    return {'observed_at': time.time(), 'snapshot': snapshot}


@pytest.mark.parametrize('allowed,classification,pause,wake', [
    (False, 'provider_unavailable', True, False), (True, 'available', False, True),
    (None, 'needs_review', False, False)])
def test_usage_preflight_is_fresh_typed_candidate_only(monkeypatch, allowed, classification, pause, wake):
    calls = []
    def read(executable, *, timeout):
        calls.append((executable, timeout))
        return usage(allowed)
    monkeypatch.setattr(disposition, 'read_usage', read)
    proof = disposition.observe_provider_availability('/synthetic/codex', binding_sha256='a' * 64, timeout=1)
    candidate = disposition.classify_provider_availability(proof, binding_sha256='a' * 64, enabled=True)
    assert calls == [('/synthetic/codex', 1)]
    assert candidate['classification'] == classification
    assert candidate['pause_candidate'] is pause and candidate['wake_candidate'] is wake
    assert candidate['resumable'] is False and candidate['reset_at'] is None
    assert 'CANARY' not in json.dumps(candidate)


@pytest.mark.parametrize('age', [-1, 121])
def test_usage_stale_or_future_observation_cannot_pause_or_wake(monkeypatch, age):
    monkeypatch.setattr(disposition, 'read_usage', lambda *a, **k: usage(True))
    proof = disposition.observe_provider_availability('/synthetic/codex', binding_sha256='a' * 64)
    monkeypatch.setattr(disposition.time, 'monotonic', lambda: proof.monotonic_at + age)
    assert disposition.classify_provider_availability(proof, binding_sha256='a' * 64, enabled=True)['classification'] == 'needs_review'


def test_usage_serialized_unavailable_and_previous_read_cannot_authorize(monkeypatch):
    monkeypatch.setattr(disposition, 'read_usage', lambda *a, **k: usage(False, status='unavailable'))
    proof = disposition.observe_provider_availability('/synthetic/codex', binding_sha256='a' * 64)
    assert disposition.classify_provider_availability(proof, binding_sha256='a' * 64, enabled=True)['classification'] == 'needs_review'
    assert disposition.classify_provider_availability(asdict(proof), binding_sha256='a' * 64, enabled=True)['classification'] == 'needs_review'
    value = usage(False)
    value['observed_at'] -= 121
    monkeypatch.setattr(disposition, 'read_usage', lambda *a, **k: value)
    with pytest.raises(ValueError):
        disposition.observe_provider_availability('/synthetic/codex', binding_sha256='a' * 64)


def test_usage_cannot_cross_context_process_or_owner(monkeypatch):
    monkeypatch.setattr(disposition, 'read_usage', lambda *a, **k: usage(False))
    proof = disposition.observe_provider_availability('/synthetic/codex', binding_sha256='a' * 64)
    assert not disposition.classify_provider_availability(proof, binding_sha256='b' * 64,
        enabled=True)['pause_candidate']
    with monkeypatch.context() as patch:
        patch.setattr(disposition.os, 'getpid', lambda: proof._pid + 1)
        assert not disposition.classify_provider_availability(proof, binding_sha256='a' * 64,
            enabled=True)['pause_candidate']
    with monkeypatch.context() as patch:
        patch.setattr(disposition.os, 'geteuid', lambda: proof._uid + 1)
        assert not disposition.classify_provider_availability(proof, binding_sha256='a' * 64,
            enabled=True)['pause_candidate']
