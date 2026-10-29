"""Opt-in provider observations, never permission to retry or reopen work.

Only local producers issue the ephemeral objects below. Their JSON projections
are candidates: a controller still needs a fresh, one-use scope/source/effects
and Runtime boundary under the work lock. Saved dictionaries cannot supply that
boundary. No provider error prose is used to establish a cause or reset time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import re
import threading
import time
import uuid

from .usage_observation import TTL_SECONDS, validate_snapshot, validate_time
from .usage_probe import read_usage

_ISSUER = object()


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False, allow_nan=False).encode('utf-8')).hexdigest()


def _uuid(value):
    if type(value) is not str or str(uuid.UUID(value)) != value:
        raise ValueError('invalid_provider_observation')
    return value


def _identifier(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value):
        raise ValueError('invalid_provider_binding')
    return value


def _digest(value):
    if type(value) is not str or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('invalid_provider_binding')
    return value


def _fresh(monotonic_at, observed_at, *, maximum=TTL_SECONDS):
    age, wall_age = time.monotonic() - monotonic_at, time.time() - observed_at
    return math.isfinite(age) and 0 <= age <= maximum and 0 <= wall_age <= maximum


@dataclass(frozen=True, init=False, slots=True)
class ProviderTurnObservation:
    """A local terminal-stream fact, not a durable authority or quota claim."""
    session_id: str
    result_sha256: str
    terminal_event_sha256: str
    observed_at: float
    monotonic_at: float
    _issuer: object

    def __init__(self, *, _issuer, session_id, result_sha256,
                 terminal_event_sha256, observed_at, monotonic_at):
        if _issuer is not _ISSUER:
            raise ValueError('provider_observation_not_issued')
        for key, value in locals().copy().items():
            if key not in ('self',):
                object.__setattr__(self, key, value)


def _issue_terminal_observation(result, terminal_event_sha256):
    """Called only after the transport's natural EOF/exit/cleanup checks."""
    return ProviderTurnObservation(_issuer=_ISSUER, session_id=_uuid(result['session_id']),
        result_sha256=_sha(result), terminal_event_sha256=terminal_event_sha256,
        observed_at=time.time(), monotonic_at=time.monotonic())


def classify_provider_result(result, observation=None, *, enabled=False):
    """Return a closed candidate; even a positive candidate cannot resume work.

    A completed tool-free JSON stream ending in turn.failed can establish a
    clean provider failure without identifying its cause. CLI prose categories
    (including legacy quota_or_rate_limit) never establish quota/context facts.
    """
    if type(enabled) is not bool:
        raise ValueError('invalid_provider_disposition_configuration')
    if not enabled:
        return None
    candidate = {'schema_version': 1, 'classification': 'needs_review',
        'reason': 'provider_evidence_unavailable', 'cause': 'unknown',
        'pause_candidate': False, 'resumable': False, 'reset_at': None,
        'evidence_sha256': None}
    if type(observation) is not ProviderTurnObservation or observation._issuer is not _ISSUER:
        return candidate
    try:
        age = time.monotonic() - observation.monotonic_at
        now = time.time()
        if (not math.isfinite(age) or not 0 <= age <= TTL_SECONDS
                or not 0 <= now - observation.observed_at <= TTL_SECONDS
                or observation.result_sha256 != _sha(result)
                or result.get('session_id') != observation.session_id
                or result.get('status') != 'blocked'
                or result.get('text') is not None
                or type(result.get('verification')) is not dict
                or any(result['verification'].get(key) is not True for key in
                    ('session_verified', 'tool_free', 'cleanup_confirmed'))):
            return candidate
    except (ValueError, TypeError, OverflowError):
        return candidate
    evidence = {'schema_version': 1, 'session_id': observation.session_id,
        'result_sha256': observation.result_sha256,
        'terminal_event_sha256': observation.terminal_event_sha256,
        'observed_at': observation.observed_at, 'terminal_kind': 'turn.failed',
        'stream_complete': True, 'tool_free': True, 'cleanup_confirmed': True}
    return {**candidate, 'classification': 'provider_unavailable',
        'reason': 'verified_terminal_provider_failure', 'pause_candidate': True,
        'evidence_sha256': _sha(evidence)}


@dataclass(frozen=True, init=False, slots=True)
class ProviderAvailabilityObservation:
    """An in-memory observation from the existing local usage probe."""
    ordinary_usage_allowed: bool | None
    data_status: str
    observed_at: float
    monotonic_at: float
    snapshot_sha256: str
    binding_sha256: str
    _pid: int
    _uid: int
    _issuer: object

    def __init__(self, *, _issuer, ordinary_usage_allowed, data_status,
                 observed_at, monotonic_at, snapshot_sha256, binding_sha256):
        if _issuer is not _ISSUER:
            raise ValueError('provider_observation_not_issued')
        for key, value in locals().copy().items():
            if key != 'self':
                object.__setattr__(self, key, value)
        object.__setattr__(self, '_pid', os.getpid())
        object.__setattr__(self, '_uid', os.geteuid())


def observe_provider_availability(executable, *, binding_sha256, timeout=30):
    """Inspect the local account without inference, login, or board changes."""
    _digest(binding_sha256)
    started_wall, started_mono = time.time(), time.monotonic()
    value = read_usage(executable, timeout=timeout)
    if type(value) is not dict or set(value) != {'observed_at', 'snapshot'}:
        raise ValueError('invalid_provider_observation')
    snapshot = validate_snapshot(value['snapshot'])
    observed_at = validate_time(value['observed_at'])
    finished_wall, finished_mono = time.time(), time.monotonic()
    if (not 0 <= finished_mono - started_mono <= 65
            or not started_wall <= observed_at <= finished_wall
            or not 0 <= finished_wall - observed_at <= TTL_SECONDS):
        raise ValueError('stale_provider_observation')
    return ProviderAvailabilityObservation(_issuer=_ISSUER,
        ordinary_usage_allowed=snapshot['ordinary_usage_allowed'],
        data_status=snapshot['data_status'], observed_at=observed_at,
        monotonic_at=finished_mono, snapshot_sha256=_sha(snapshot), binding_sha256=binding_sha256)


def classify_provider_availability(observation=None, *, binding_sha256=None, enabled=False):
    """A fresh positive reading is a wake candidate, never permission to run."""
    if type(enabled) is not bool:
        raise ValueError('invalid_provider_disposition_configuration')
    if not enabled:
        return None
    candidate = {'schema_version': 1, 'classification': 'needs_review',
        'pause_candidate': False, 'wake_candidate': False, 'resumable': False,
        'reset_at': None, 'observed_at': None, 'snapshot_sha256': None}
    if (type(observation) is not ProviderAvailabilityObservation
            or observation._issuer is not _ISSUER or observation._pid != os.getpid()
            or observation._uid != os.geteuid() or observation.binding_sha256 != binding_sha256):
        return candidate
    age = time.monotonic() - observation.monotonic_at
    wall_age = time.time() - observation.observed_at
    if (not math.isfinite(age) or not 0 <= age <= TTL_SECONDS
            or not 0 <= wall_age <= TTL_SECONDS or observation.data_status != 'observed'
            or type(observation.ordinary_usage_allowed) is not bool):
        return candidate
    allowed = observation.ordinary_usage_allowed
    return {**candidate, 'classification': 'available' if allowed else 'provider_unavailable',
        'pause_candidate': not allowed, 'wake_candidate': allowed,
        'observed_at': observation.observed_at, 'snapshot_sha256': observation.snapshot_sha256}


class _OneUse:
    __slots__ = ()

    def __copy__(self):
        raise TypeError('provider_ephemeral_copy_forbidden')

    def __deepcopy__(self, memo):
        raise TypeError('provider_ephemeral_copy_forbidden')

    def __reduce_ex__(self, protocol):
        raise TypeError('provider_ephemeral_copy_forbidden')


@dataclass(frozen=True, init=False, slots=True)
class ProviderAttempt(_OneUse):
    """One explicit new-attempt enrollment; no old result can be enrolled later."""
    task_id: str
    generation_id: str
    cycle_id: str
    attempt_id: str
    work_id: str
    request_id: str
    scope_sha256: str
    on_receipt: object
    observed_at: float
    monotonic_at: float
    _pid: int
    _thread: object
    _claimed: bool
    _turn_id: str | None
    _receipt_issued: bool
    _issuer: object = field(init=False, repr=False)

    def __init__(self, *, _issuer, **fields):
        if _issuer is not _ISSUER:
            raise ValueError('provider_attempt_not_enrolled')
        for key, value in fields.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, '_issuer', _issuer)


def enroll_provider_attempt(*, task_id, generation_id, cycle_id, attempt_id,
                            work_id, request_id, scope_sha256, on_receipt):
    """Bind a controller's opt-in before PersonalRunner creates its turn.

    scope_sha256 is the canonical SHA256 of the exact RuntimeGuard.scope. This
    enrollment grants no permissions and does not change stored work state.
    """
    _identifier(task_id)
    for value in (generation_id, cycle_id, attempt_id):
        _uuid(value)
    _uuid(work_id)
    _identifier(request_id)
    _digest(scope_sha256)
    if len({generation_id, cycle_id, attempt_id}) != 3 or not callable(on_receipt):
        raise ValueError('invalid_provider_binding')
    return ProviderAttempt(_issuer=_ISSUER, task_id=task_id, generation_id=generation_id,
        cycle_id=cycle_id, attempt_id=attempt_id,
        work_id=work_id, request_id=request_id, scope_sha256=scope_sha256, on_receipt=on_receipt,
        observed_at=time.time(), monotonic_at=time.monotonic(), _pid=os.getpid(), _claimed=False,
        _thread=threading.current_thread(), _turn_id=None, _receipt_issued=False)


def _runtime_type(guard):
    from .runtime_guard import RuntimeGuard
    from .dev_runtime import DevelopmentRuntimeGuard, ScopePinnedModelRuntimeGuard
    from .message_runtime import MessageRuntimeGuard
    return type(guard) in (RuntimeGuard, DevelopmentRuntimeGuard, ScopePinnedModelRuntimeGuard,
        MessageRuntimeGuard)


def _claim_provider_attempt(attempt, work, request_id, transport, runtime_guard, *, store=None):
    from functools import partial
    from .codex_turn import execute_turn
    if (type(attempt) is not ProviderAttempt or attempt._issuer is not _ISSUER
            or attempt._pid != os.getpid() or attempt._claimed
            or attempt._thread is not threading.current_thread()
            or not _fresh(attempt.monotonic_at, attempt.observed_at)
            or attempt.work_id != work['id'] or attempt.request_id != request_id
            or not _runtime_type(runtime_guard) or _sha(runtime_guard.scope) != attempt.scope_sha256):
        raise ValueError('provider_attempt_mismatch')
    real_transport = transport
    if type(real_transport) is partial:
        if real_transport.args or set(real_transport.keywords) - {'planner_policy', 'reasoning_effort'}:
            raise ValueError('provider_transport_not_supported')
        real_transport = real_transport.func
    if real_transport is not execute_turn:
        raise ValueError('provider_transport_not_supported')
    if store is not None and store.continuation_v1:
        binding=store.continuation_enrollment(work['id'])
        if binding is None or any(binding[key]!=getattr(attempt,key) for key in
                ('task_id','generation_id','cycle_id','attempt_id','work_id','scope_sha256')):
            raise ValueError('provider_attempt_mismatch')
    object.__setattr__(attempt, '_claimed', True)


def _bind_provider_turn(attempt, turn_id):
    if (type(attempt) is not ProviderAttempt or attempt._issuer is not _ISSUER
            or not attempt._claimed or attempt._turn_id is not None or attempt._receipt_issued):
        raise ValueError('provider_attempt_mismatch')
    object.__setattr__(attempt, '_turn_id', _uuid(turn_id))


@dataclass(frozen=True, init=False, slots=True)
class ProviderDispositionReceipt(_OneUse):
    """Fresh runner facts. A separate continuation boundary remains required."""
    metadata_json: str
    monotonic_at: float
    _pid: int
    _thread: object
    _consumed: bool
    _issuer: object = field(init=False, repr=False)

    def __init__(self, *, _issuer, metadata_json, monotonic_at):
        if _issuer is not _ISSUER:
            raise ValueError('provider_receipt_not_issued')
        object.__setattr__(self, 'metadata_json', metadata_json)
        object.__setattr__(self, 'monotonic_at', monotonic_at)
        object.__setattr__(self, '_pid', os.getpid())
        object.__setattr__(self, '_thread', threading.current_thread())
        object.__setattr__(self, '_consumed', False)
        object.__setattr__(self, '_issuer', _issuer)


def _finish_provider_attempt(attempt, store, work, turn_id, observation, runtime_guard, should_cancel):
    """Called by the real Runner under its lock, after durable result+finish."""
    from .run_store import RunStore
    if (type(attempt) is not ProviderAttempt or attempt._issuer is not _ISSUER
            or not attempt._claimed or attempt._pid != os.getpid() or type(store) is not RunStore
            or attempt._thread is not threading.current_thread()
            or attempt._turn_id != turn_id or attempt._receipt_issued or not _runtime_type(runtime_guard)):
        return
    store._require_lock(work['id'])
    result = store.turn_result(turn_id)
    candidate = classify_provider_result(result, observation, enabled=True)
    if not candidate['pause_candidate']:
        return
    current = store.get_work(work['id'])
    turn = store._turn(turn_id)
    if (current['id'] != attempt.work_id or turn['work_id'] != current['id']
            or turn['request_id'] != attempt.request_id or turn['status'] != 'blocked'
            or turn['created_at'] < attempt.observed_at
            or not turn['requires_review'] or turn['cancel_requested']
            or current['status'] != 'needs_review'
            or current['provider_session_id'] != observation.session_id
            or turn['provider_session_id'] != observation.session_id
            or (should_cancel is not None and should_cancel())):
        return
    guard = runtime_guard
    operation = guard.operation
    if (not operation or operation['configuration'] != guard.configuration(work, turn_id)
            or operation['phase'] != 'finished'
            or type(operation['start_confirmed']) not in (bool, int) or operation['start_confirmed'] != 1
            or guard.control_failed or _sha(guard.scope) != attempt.scope_sha256):
        return
    found = guard.client.call('lookup_execution', {'idempotency_key': operation['key']})
    if type(found) is not dict or found.get('found') is not True:
        return
    receipt = guard.metadata(found.get('execution'))
    if (receipt['state'] != 'finished' or receipt['cleanup_confirmed'] is not True
            # The Runtime's terminal view deliberately says should_stop=True;
            # it forbids restarting this execution. Fresh owner/scope admission
            # for a future attempt is the separate controller boundary's job.
            or receipt['should_stop'] is not True or receipt['control_reason'] != 'finished'
            or receipt['stop_requested'] or receipt['outcome'] != 'blocked'
            or receipt != operation['receipt'] or store.is_cancelled(turn_id)
            or (should_cancel is not None and should_cancel())
            or not _fresh(observation.monotonic_at, observation.observed_at)):
        return
    metadata = {'schema_version': 1, 'task_id': attempt.task_id, 'generation_id': attempt.generation_id,
        'cycle_id': attempt.cycle_id, 'attempt_id': attempt.attempt_id,
        'work_id': work['id'], 'turn_id': turn_id, 'request_id': attempt.request_id,
        'scope_sha256': attempt.scope_sha256, 'result_sha256': _sha(result),
        'work_state_sha256': (store.continuation_state_sha256(work['id']) if store.continuation_v1
            else _sha(store.status(work['id']))),
        'classification': 'provider_unavailable', 'cause': 'unknown',
        'evidence_sha256': candidate['evidence_sha256'], 'runtime_receipt_sha256': _sha(receipt),
        'runtime_execution_id': receipt['id'], 'runtime_revision': receipt['revision'],
        'cleanup_confirmed': True, 'action_accepted': False, 'observed_at': time.time()}
    proof = ProviderDispositionReceipt(_issuer=_ISSUER,
        metadata_json=json.dumps(metadata, sort_keys=True, separators=(',', ':'), allow_nan=False),
        monotonic_at=time.monotonic())
    object.__setattr__(attempt, '_receipt_issued', True)
    attempt.on_receipt(proof)


def consume_provider_receipt(receipt, *, task_id, generation_id, cycle_id, attempt_id, work_id, turn_id,
                             request_id, scope_sha256, result_sha256, work_state_sha256):
    """One-use correlation only; no work state or permission is changed.

    The caller must obtain its fresh Runtime/source/fence/effects boundary
    independently. The returned dictionary is audit metadata, never authority.
    """
    if (type(receipt) is not ProviderDispositionReceipt or receipt._issuer is not _ISSUER
            or receipt._pid != os.getpid() or receipt._consumed):
        raise ValueError('provider_receipt_unavailable')
    if receipt._thread is not threading.current_thread():
        raise ValueError('provider_receipt_unavailable')
    metadata = json.loads(receipt.metadata_json)
    binding = {'task_id': task_id, 'generation_id': generation_id, 'cycle_id': cycle_id,
        'attempt_id': attempt_id, 'work_id': work_id, 'turn_id': turn_id,
        'request_id': request_id, 'scope_sha256': scope_sha256, 'result_sha256': result_sha256,
        'work_state_sha256': work_state_sha256}
    if (not _fresh(receipt.monotonic_at, metadata['observed_at'])
            or any(type(metadata.get(key)) is not type(value) or metadata[key] != value
                for key, value in binding.items())):
        raise ValueError('provider_receipt_mismatch')
    object.__setattr__(receipt, '_consumed', True)
    return metadata


def _consumed_provider_receipt_metadata(receipt):
    """Recheck a consumed in-memory fact for the exact storage seal, not replay."""
    if (type(receipt) is not ProviderDispositionReceipt or receipt._issuer is not _ISSUER
            or receipt._pid!=os.getpid() or receipt._thread is not threading.current_thread()
            or receipt._consumed is not True):
        raise ValueError('provider_receipt_unavailable')
    metadata=json.loads(receipt.metadata_json)
    if not _fresh(receipt.monotonic_at,metadata['observed_at']):raise ValueError('provider_receipt_mismatch')
    return metadata
