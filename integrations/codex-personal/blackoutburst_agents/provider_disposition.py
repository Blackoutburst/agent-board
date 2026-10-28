"""Opt-in provider observations, never permission to retry or reopen work.

Only local producers issue the ephemeral objects below. Their JSON projections
are candidates: a controller still needs a fresh, one-use scope/source/effects
and Runtime boundary under the work lock. Saved dictionaries cannot supply that
boundary. No provider error prose is used to establish a cause or reset time.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
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


@dataclass(frozen=True, init=False)
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


@dataclass(frozen=True, init=False)
class ProviderAvailabilityObservation:
    """An in-memory observation from the existing local usage probe."""
    ordinary_usage_allowed: bool | None
    data_status: str
    observed_at: float
    monotonic_at: float
    snapshot_sha256: str
    _issuer: object

    def __init__(self, *, _issuer, ordinary_usage_allowed, data_status,
                 observed_at, monotonic_at, snapshot_sha256):
        if _issuer is not _ISSUER:
            raise ValueError('provider_observation_not_issued')
        for key, value in locals().copy().items():
            if key != 'self':
                object.__setattr__(self, key, value)


def observe_provider_availability(executable, *, timeout=30):
    """Inspect the local account without inference, login, or board changes."""
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
        monotonic_at=finished_mono, snapshot_sha256=_sha(snapshot))


def classify_provider_availability(observation=None, *, enabled=False):
    """A fresh positive reading is a wake candidate, never permission to run."""
    if type(enabled) is not bool:
        raise ValueError('invalid_provider_disposition_configuration')
    if not enabled:
        return None
    candidate = {'schema_version': 1, 'classification': 'needs_review',
        'pause_candidate': False, 'wake_candidate': False, 'resumable': False,
        'reset_at': None, 'observed_at': None, 'snapshot_sha256': None}
    if (type(observation) is not ProviderAvailabilityObservation
            or observation._issuer is not _ISSUER):
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
