"""Closed evidence for one refused message admission, never process cleanup."""
import hashlib
import json
import re


REFUSAL_MODE = 'capacity-v1'
REFUSAL_SCHEMA = 'message-runtime-admission-refusal-v1'
ARGUMENT_FIELDS = frozenset({'idempotency_key', 'thread_id', 'message_id', 'source_sha256',
    'work_id', 'turn_id', 'provider', 'model', 'session_id', 'refusal_mode'})
REFUSAL_FIELDS = frozenset({'schema', 'reason', 'request_sha256', 'owner_id', 'agent_id',
    'device_id', 'profile_version', 'session_id'})


def request_sha256(arguments):
    if (type(arguments) is not dict or set(arguments) != ARGUMENT_FIELDS
            or arguments.get('refusal_mode') != REFUSAL_MODE):
        raise ValueError('runtime_refusal_invalid')
    raw = json.dumps({'method': 'reserve_message_execution', 'arguments': arguments},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 8192:
        raise ValueError('runtime_refusal_invalid')
    return hashlib.sha256(raw).hexdigest()


def refusal_arguments(configuration, key):
    scope = configuration['scope']
    return {'idempotency_key': key,
        **{name: scope[name] for name in ('thread_id', 'message_id', 'source_sha256')},
        **{name: configuration[name] for name in ('work_id', 'turn_id', 'provider', 'model')},
        'session_id': scope['board_session_id'], 'refusal_mode': REFUSAL_MODE}


def validate_refusal(value, arguments, *, owner_id=None, agent_id=None, device_id=None, profile_version=None):
    """Return only a fully bound negative proof. No free remote diagnostics."""
    if (type(value) is not dict or set(value) != {'reserved', 'refusal'}
            or value['reserved'] is not False):
        raise ValueError('runtime_refusal_invalid')
    proof = value['refusal']
    if (type(proof) is not dict or set(proof) != REFUSAL_FIELDS
            or proof['schema'] != REFUSAL_SCHEMA or proof['reason'] != 'capacity_exhausted'
            or type(proof['request_sha256']) is not str
            or not re.fullmatch('[0-9a-f]{64}', proof['request_sha256'])
            or proof['request_sha256'] != request_sha256(arguments)
            or type(proof['profile_version']) is not int or not 1 <= proof['profile_version'] < 2**63):
        raise ValueError('runtime_refusal_invalid')
    for field in ('owner_id', 'agent_id', 'device_id', 'session_id'):
        if type(proof[field]) is not str or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', proof[field]):
            raise ValueError('runtime_refusal_invalid')
    if (proof['session_id'] != arguments['session_id']
            or (owner_id is not None and proof['owner_id'] != owner_id)
            or (agent_id is not None and proof['agent_id'] != agent_id)
            or (device_id is not None and proof['device_id'] != device_id)
            or (profile_version is not None and proof['profile_version'] != profile_version)):
        raise ValueError('runtime_refusal_invalid')
    return dict(proof)
