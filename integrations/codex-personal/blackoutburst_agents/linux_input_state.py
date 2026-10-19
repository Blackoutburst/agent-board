"""Strict private input-only journal data. No executable result or authority."""
from __future__ import annotations
import uuid
from .dev_manifest import DevelopmentError, canonical, digest, opaque, sha256

PROFILE = 'blackoutburst-linux-input-only-v1'
PASSES = 5
DETAIL_FIELDS = {'profile', 'origin', 'workflow_id', 'plan_sha256', 'scope_sha256',
    'planner_sha256', 'fence_sha256', 'prior_state_sha256', 'nonce', 'name',
    'timeout_seconds', 'max_output_bytes', 'reserved_read_bytes', 'capture_passes',
    'directory_identity', 'directory_chain_sha256'}


def invalid():
    raise DevelopmentError('journal_corrupt')


def unique_id(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value: invalid()
    except (ValueError, AttributeError): invalid()
    return value


def validate_operation(key, operation, *, origin, manifest):
    if (set(operation) != {'id', 'kind', 'phase', 'request_hash', 'detail', 'receipt'}
            or operation['id'] != key or operation['kind'] != 'linux_input'
            or operation['phase'] not in ('intent', 'complete', 'needs_review')): invalid()
    sha256(key); sha256(operation['request_hash'])
    detail = operation['detail']
    if (type(detail) is not dict or set(detail) != DETAIL_FIELDS
            or detail['profile'] != PROFILE or detail['origin'] != origin
            or type(detail['capture_passes']) is not int or detail['capture_passes'] != PASSES
            or type(detail['reserved_read_bytes']) is not int
            or detail['reserved_read_bytes'] != PASSES * min(1048576, manifest['budgets']['max_file_bytes']) * len(manifest['files'])
            or type(detail['timeout_seconds']) is not int or not 1 <= detail['timeout_seconds'] <= 60
            or type(detail['max_output_bytes']) is not int or not 0 <= detail['max_output_bytes'] <= 1048576
            or type(detail['directory_identity']) is not list or len(detail['directory_identity']) != 2
            or any(type(item) is not int or item < (1 if index else 0)
                   for index, item in enumerate(detail['directory_identity']))): invalid()
    unique_id(detail['workflow_id']); unique_id(detail['nonce']); opaque(detail['name'], maximum=64)
    for name in ('plan_sha256', 'scope_sha256', 'planner_sha256', 'fence_sha256',
                 'prior_state_sha256', 'directory_chain_sha256'): sha256(detail[name])
    suite = next((item for item in manifest['tests'] if item['name'] == detail['name']), None)
    # Full plan hash, not its shortened identifier alone, remains in the intent.
    if (suite is None or suite['backend_id'] != 'linux-python-v1-' + detail['plan_sha256'][:40]
            or any(suite[name] != detail[name] for name in ('timeout_seconds', 'max_output_bytes'))
            or detail['scope_sha256'] != digest(canonical(manifest['scope']))
            or operation['request_hash'] != digest(canonical({'kind': 'linux_input', **detail}))): invalid()
    receipt = operation['receipt']
    if operation['phase'] == 'intent':
        if receipt is not None: invalid()
        return
    common = {'operation_id': key, 'kind': 'linux_input', 'status': operation['phase'],
              'execution_supported': False, 'adoptable': False}
    if (type(receipt) is not dict or any(type(receipt.get(name)) is not type(value)
            or receipt.get(name) != value for name, value in common.items())): invalid()
    if operation['phase'] == 'needs_review':
        if receipt != {**common, 'result': 'needs_review', 'reason': 'preparation_interrupted'}: invalid()
        return
    if (type(receipt) is not dict or set(receipt) != set(common) | {'result', 'request_sha256',
            'input_snapshot_sha256', 'packet_sha256', 'preparation_sha256', 'boundaries'}
            or any(type(receipt.get(name)) is not type(value) or receipt.get(name) != value
                   for name, value in common.items())
            or receipt['result'] != 'prepared_input'
            or receipt['boundaries'] != ['before_source', 'after_result', 'context_exit']): invalid()
    for name in ('request_sha256', 'input_snapshot_sha256', 'packet_sha256', 'preparation_sha256'):
        sha256(receipt[name])
