"""Pure, explicit model selection for one owner's personal connection.

This module neither discovers models nor invokes a provider. A policy is an
owner declaration, not a signed provider attestation. A dated successful test
can support selection but cannot guarantee current access, quota or capability.
The caller must authenticate the owner/connection independently and durably pin
the decision before inference. There is no fallback or model change on resume.

Only the existing Codex/ChatGPT transport is supported. No model names are
built in. This routing adapter does not forward reasoning preferences, so its
default permits only a null setting. The development planner's explicit option
is separate and does not enable reasoning preferences in this router.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

TASK_CLASSES = ('tri', 'simple', 'difficult', 'review')
PROVIDER = 'codex_chatgpt'
MAX_POLICY_BYTES = 65536
MAX_MODELS = 100
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_EFFORTS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})
_REASONS = frozenset({'manual_override', 'profile_preference', 'class_route'})


class PolicyError(Exception):
    """Fixed safe code: never includes the path, policy contents or a model error."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _fail(code='invalid_policy'):
    raise PolicyError(code)


def _keys(value, expected, code='invalid_policy'):
    if not isinstance(value, dict) or set(value) != set(expected):
        _fail(code)


def _identifier(value, code='invalid_policy'):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        _fail(code)
    return value


def _positive_integer(value, code='invalid_policy'):
    if type(value) is not int or not 1 <= value <= 2**53 - 1:
        _fail(code)
    return value


def _timestamp(value, code='invalid_policy'):
    if (type(value) not in (int, float) or not 0 < value <= 2**53 - 1
            or not math.isfinite(value)):
        _fail(code)
    return value


def _effort(value, code='invalid_policy'):
    if value is not None and (not isinstance(value, str) or value not in _EFFORTS):
        _fail(code)
    return value


def _canonical(value, code='invalid_policy'):
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail(code)
    if len(encoded) > MAX_POLICY_BYTES:
        _fail(code)
    return encoded.decode('utf-8')


def _hash(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _json(data):
    if not isinstance(data, (str, bytes)):
        _fail()
    try:
        size = len(data.encode('utf-8')) if isinstance(data, str) else len(data)
    except UnicodeError:
        _fail()
    if size > MAX_POLICY_BYTES:
        _fail()
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _fail()
            result[key] = value
        return result

    try:
        return json.loads(data, object_pairs_hook=unique_pairs,
                          parse_constant=lambda _: _fail())
    except (ValueError, TypeError, UnicodeError, RecursionError):
        _fail()


def _validate(value):
    _keys(value, ('schema_version', 'policy_id', 'version', 'owner_id',
                  'connection_id', 'provider', 'proof_max_age_seconds', 'models', 'routes'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        _fail('unsupported_policy_version')
    for field in ('policy_id', 'owner_id', 'connection_id'):
        _identifier(value[field])
    _positive_integer(value['version'])
    if value['provider'] != PROVIDER:
        _fail('provider_not_supported')
    age = value['proof_max_age_seconds']
    if type(age) is not int or not 60 <= age <= 30 * 86400:
        _fail()
    models = value['models']
    if not isinstance(models, dict) or not 1 <= len(models) <= MAX_MODELS:
        _fail()
    for model, candidate in models.items():
        _identifier(model)
        _keys(candidate, ('reasoning_efforts', 'availability'))
        efforts = candidate['reasoning_efforts']
        if (not isinstance(efforts, list) or len(efforts) > len(_EFFORTS)
                or any(not isinstance(item, str) or item not in _EFFORTS for item in efforts)
                or len(set(efforts)) != len(efforts)):
            _fail()
        observation = candidate['availability']
        _keys(observation, ('status', 'observed_at', 'reference', 'owner_id',
                            'connection_id', 'provider', 'model'))
        if any(observation[field] != expected for field, expected in (
                ('owner_id', value['owner_id']), ('connection_id', value['connection_id']),
                ('provider', PROVIDER), ('model', model))):
            _fail('availability_scope_mismatch')
        if observation['status'] == 'unknown':
            if observation['observed_at'] is not None or observation['reference'] is not None:
                _fail()
        elif observation['status'] in ('verified', 'unavailable'):
            _timestamp(observation['observed_at'])
            _identifier(observation['reference'])
        else:
            _fail()
    _keys(value['routes'], TASK_CLASSES)
    for choice in value['routes'].values():
        _keys(choice, ('model', 'reasoning_effort'))
        _identifier(choice['model'])
        if choice['model'] not in models:
            _fail('model_not_allowed')
        effort = _effort(choice['reasoning_effort'])
        if effort is not None and effort not in models[choice['model']]['reasoning_efforts']:
            _fail('reasoning_not_allowed')
    return value


@dataclass(frozen=True)
class ModelPolicy:
    """Immutable canonical document; to_dict returns a detached editable copy."""

    canonical_json: str

    def to_dict(self):
        return _validate(_json(self.canonical_json))

    @property
    def policy_hash(self):
        return _hash(self.to_dict())


def parse_policy(value, *, owner_id, connection_id):
    """Validate a dictionary against the authenticated local connection scope."""
    _identifier(owner_id, 'invalid_scope')
    _identifier(connection_id, 'invalid_scope')
    value = _validate(_json(_canonical(value)))
    if value['owner_id'] != owner_id or value['connection_id'] != connection_id:
        _fail('policy_scope_mismatch')
    return ModelPolicy(_canonical(value))


def load_policy(path, *, owner_id, connection_id=None):
    """Read a bounded regular 0600 file owned by this OS user; no final symlink.

    If the connection is not known yet, this only validates the owner and file.
    check_routing_connection must validate the live binding before board work.
    """
    if os.name != 'posix' or not hasattr(os, 'O_NOFOLLOW'):
        _fail('unsupported_platform')
    descriptor = None
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            _fail('insecure_policy_file')
        if info.st_size > MAX_POLICY_BYTES:
            _fail('policy_too_large')
        chunks, total = [], 0
        while True:
            part = os.read(descriptor, min(8192, MAX_POLICY_BYTES + 1 - total))
            if not part:
                break
            chunks.append(part)
            total += len(part)
            if total > MAX_POLICY_BYTES:
                _fail('policy_too_large')
        value = _json(b''.join(chunks))
    except (OSError, ValueError, TypeError):
        _fail('policy_unavailable')
    finally:
        if descriptor is not None:
            os.close(descriptor)
    value = _validate(value)
    return parse_policy(value, owner_id=owner_id,
                        connection_id=value['connection_id'] if connection_id is None else connection_id)


def connection_identifier(scope, registration):
    """Stable local binding ID, not an attestation of the active ChatGPT account.

    SHA256 of canonical UTF-8 JSON with exactly board_endpoint, owner_id,
    agent_id and device_id; prefix codex-connection-. No session or credential.
    The caller must first validate the fresh registration's identity/session.
    """
    try:
        fields = {key: scope[key] for key in ('board_endpoint', 'owner_id', 'agent_id')}
        fields['device_id'] = registration['identity']['device_id']
        if any(not isinstance(value, str) or not value for value in fields.values()):
            _fail('invalid_scope')
        if (registration['identity']['id'] != fields['agent_id']
                or registration['identity']['owner_id'] != fields['owner_id']):
            _fail('policy_scope_mismatch')
    except (KeyError, TypeError):
        _fail('invalid_scope')
    return 'codex-connection-' + _hash(fields)


def routing_configuration(model_policy, task_class, *, owner_id):
    """Canonical immutable opt-in configuration shared by all coordinators."""
    if model_policy is None:
        if task_class is not None:
            _fail('model_policy_required')
        return None
    if not isinstance(task_class, str) or task_class not in TASK_CLASSES:
        _fail('invalid_task_class')
    value = model_policy.to_dict() if isinstance(model_policy, ModelPolicy) else model_policy
    value = _validate(_json(_canonical(value)))
    parsed = parse_policy(value, owner_id=owner_id, connection_id=value['connection_id'])
    return {'policy': parsed.to_dict(), 'policy_hash': parsed.policy_hash, 'task_class': task_class}


def validate_routing_configuration(value):
    """Validate an already stored configuration without network or model calls."""
    _keys(value, ('policy', 'policy_hash', 'task_class'), 'invalid_routing_configuration')
    try:
        expected = routing_configuration(value['policy'], value['task_class'],
                                         owner_id=value['policy']['owner_id'])
    except (KeyError, TypeError):
        _fail('invalid_routing_configuration')
    if expected != value:
        _fail('invalid_routing_configuration')
    return value


def check_routing_connection(routing, scope, registration):
    validate_routing_configuration(routing)
    return parse_policy(routing['policy'], owner_id=scope['owner_id'],
                        connection_id=connection_identifier(scope, registration))


@dataclass(frozen=True)
class RouteDecision:
    schema_version: int
    policy_id: str
    policy_version: int
    policy_hash: str
    owner_id: str
    connection_id: str
    provider: str
    task_class: str
    model: str
    reasoning_effort: str | None
    reason: str
    profile_version: int
    context_hash: str
    availability_reference: str
    availability_observed_at: float
    selected_at: float
    decision_hash: str

    def to_dict(self):
        return asdict(self)


def _context(value, profile_version):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        _fail('invalid_context_hash')
    _positive_integer(profile_version, 'invalid_profile_version')


def _capabilities(values):
    if (not isinstance(values, (list, tuple, frozenset, set))
            or any(not isinstance(value, str) or value not in _EFFORTS for value in values)):
        _fail('invalid_transport_capabilities')
    return frozenset(values)


def _available(policy, model, now):
    candidate = policy['models'].get(model)
    if candidate is None:
        _fail('model_not_allowed')
    observed = candidate['availability']
    if observed['status'] == 'unavailable':
        _fail('model_unavailable')
    if observed['status'] != 'verified':
        _fail('availability_unverified')
    if observed['observed_at'] > now:
        _fail('availability_in_future')
    if now - observed['observed_at'] > policy['proof_max_age_seconds']:
        _fail('availability_expired')
    return observed


def select_model(policy, *, owner_id, connection_id, task_class, context_hash,
                 profile_version, override_model=None, override_reasoning_effort=None,
                 preferred_model=None, preferred_provider=None, now=None,
                 supported_reasoning_efforts=()):
    """Select exactly one configured candidate; refusal never tries another.

    A manual override supersedes profile preferences. Otherwise a nonempty
    profile preference supersedes the class route, and an unsupported preference
    is refused rather than silently ignored. Profile preference selects no
    reasoning setting; manual settings must be explicit. The class route carries
    its own setting. Default transport capabilities reject all nonnull settings.
    """
    if not isinstance(policy, ModelPolicy):
        _fail()
    value = parse_policy(policy.to_dict(), owner_id=owner_id,
                         connection_id=connection_id).to_dict()
    _context(context_hash, profile_version)
    if not isinstance(task_class, str) or task_class not in TASK_CLASSES:
        _fail('invalid_task_class')
    now = _timestamp(time.time() if now is None else now, 'invalid_time')
    capabilities = _capabilities(supported_reasoning_efforts)
    if override_model is not None:
        model = _identifier(override_model, 'invalid_model')
        effort = _effort(override_reasoning_effort, 'invalid_reasoning_effort')
        reason = 'manual_override'
    else:
        if override_reasoning_effort is not None:
            _fail('override_model_required')
        if preferred_provider not in (None, '', PROVIDER):
            _fail('preferred_provider_unsupported')
        if preferred_model not in (None, ''):
            model = _identifier(preferred_model, 'invalid_model')
            effort, reason = None, 'profile_preference'
        else:
            model = value['routes'][task_class]['model']
            effort = value['routes'][task_class]['reasoning_effort']
            reason = 'class_route'
    observed = _available(value, model, now)
    if effort is not None:
        if effort not in value['models'][model]['reasoning_efforts']:
            _fail('reasoning_not_allowed')
        if effort not in capabilities:
            _fail('reasoning_not_supported')
    fields = dict(schema_version=1, policy_id=value['policy_id'],
        policy_version=value['version'], policy_hash=policy.policy_hash,
        owner_id=owner_id, connection_id=connection_id, provider=PROVIDER,
        task_class=task_class, model=model, reasoning_effort=effort, reason=reason,
        profile_version=profile_version, context_hash=context_hash,
        availability_reference=observed['reference'],
        availability_observed_at=observed['observed_at'], selected_at=now)
    return RouteDecision(**fields, decision_hash=_hash(fields))


def validate_pinned(decision, policy, *, owner_id, connection_id, context_hash,
                    profile_version, now=None, supported_reasoning_efforts=()):
    """Validate a saved decision without replacing it or choosing a new model.

    Returns the same immutable decision. Intended immediately before a new turn,
    not for read-only inspection of completed work. A policy refresh, even just
    a newer access observation, requires an explicit new operation/decision.
    A hash detects changes; it is not a signature or a grant of permission.
    """
    if not isinstance(decision, RouteDecision) or not isinstance(policy, ModelPolicy):
        _fail('invalid_decision')
    decision_from_dict(decision.to_dict())
    value = parse_policy(policy.to_dict(), owner_id=owner_id,
                         connection_id=connection_id).to_dict()
    _context(context_hash, profile_version)
    if (decision.owner_id != owner_id or decision.connection_id != connection_id
            or decision.provider != PROVIDER):
        _fail('decision_scope_mismatch')
    if (decision.policy_id != value['policy_id'] or decision.policy_version != value['version']
            or decision.policy_hash != policy.policy_hash):
        _fail('policy_changed')
    if decision.context_hash != context_hash or decision.profile_version != profile_version:
        _fail('context_changed')
    capabilities = _capabilities(supported_reasoning_efforts)
    observed = _available(value, decision.model, decision.selected_at)
    if (decision.availability_reference != observed['reference']
            or decision.availability_observed_at != observed['observed_at']):
        _fail('invalid_decision')
    if decision.reason == 'class_route':
        configured = value['routes'][decision.task_class]
        if (decision.model != configured['model']
                or decision.reasoning_effort != configured['reasoning_effort']):
            _fail('invalid_decision')
    elif decision.reason == 'profile_preference' and decision.reasoning_effort is not None:
        _fail('invalid_decision')
    effort = decision.reasoning_effort
    if effort is not None:
        if effort not in value['models'][decision.model]['reasoning_efforts']:
            _fail('reasoning_not_allowed')
        if effort not in capabilities:
            _fail('reasoning_not_supported')
    now = _timestamp(time.time() if now is None else now, 'invalid_time')
    if decision.selected_at > now:
        _fail('decision_in_future')
    _available(value, decision.model, now)
    return decision


def decision_from_dict(value):
    """Decode a typed, bounded receipt; validate_pinned must still check policy."""
    if not isinstance(value, dict):
        _fail('invalid_decision')
    try:
        decision = RouteDecision(**_json(_canonical(value, 'invalid_decision')))
    except TypeError:
        _fail('invalid_decision')
    if type(decision.schema_version) is not int or decision.schema_version != 1:
        _fail('invalid_decision')
    for field in ('policy_id', 'owner_id', 'connection_id', 'model', 'availability_reference'):
        _identifier(getattr(decision, field), 'invalid_decision')
    for field in ('policy_version', 'profile_version'):
        _positive_integer(getattr(decision, field), 'invalid_decision')
    for field in ('availability_observed_at', 'selected_at'):
        _timestamp(getattr(decision, field), 'invalid_decision')
    for field in ('policy_hash', 'context_hash', 'decision_hash'):
        item = getattr(decision, field)
        if not isinstance(item, str) or not _HASH.fullmatch(item):
            _fail('invalid_decision')
    if (decision.provider != PROVIDER or not isinstance(decision.task_class, str)
            or decision.task_class not in TASK_CLASSES or not isinstance(decision.reason, str)
            or decision.reason not in _REASONS):
        _fail('invalid_decision')
    _effort(decision.reasoning_effort, 'invalid_decision')
    fields = decision.to_dict()
    digest = fields.pop('decision_hash')
    if _hash(fields) != digest:
        _fail('decision_hash_mismatch')
    return decision
