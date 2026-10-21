"""Strict executable Linux journal profile; never accepts input-only records.

This module validates data. Only the live broker can admit a native observation
as a test of its current snapshot. A disk observation alone never gains authority.
"""
from __future__ import annotations

import uuid
from .dev_manifest import DevelopmentError, canonical, digest, sha256

PROFILE = 'blackoutburst-linux-python-execution-v1'
FIELDS = {'schema', 'manifest_hash', 'used', 'operations', 'linux_execution_origin',
          'linux_execution_plan_sha256', 'linux_execution'}
RESERVATION_FIELDS = {'workflow_id', 'planner_snapshot_sha256', 'fence_sha256',
    'input_plan_sha256', 'execution_plan_sha256', 'action_sha256', 'action_key_sha256',
    'prior_state_sha256', 'capture_passes', 'reserved_read_bytes'}
PHASES = ('reserved', 'captured', 'dispatched', 'broker_admitted', 'needs_review')


def fail():
    raise DevelopmentError('linux_execution_state_changed')


def identifier(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value:
            fail()
    except (ValueError, AttributeError):
        fail()


def validate_record(key, record, state, manifest):
    if (type(record) is not dict or set(record) != {'phase', 'reservation', 'request',
            'native_receipt_sha256', 'admitted_snapshot_sha256'}
            or record['phase'] not in PHASES or key not in state['operations']):
        fail()
    operation = state['operations'][key]
    if operation['kind'] != 'test':
        fail()
    reservation = record['reservation']
    if type(reservation) is not dict or set(reservation) != RESERVATION_FIELDS:
        fail()
    identifier(reservation['workflow_id'])
    for name in RESERVATION_FIELDS - {'workflow_id', 'capture_passes', 'reserved_read_bytes'}:
        sha256(reservation[name])
    expected_reads = 5 * (min(manifest['budgets']['max_file_bytes'], 1048576) + 1) * len(manifest['files'])
    if (type(reservation['capture_passes']) is not int or reservation['capture_passes'] != 5
            or type(reservation['reserved_read_bytes']) is not int
            or reservation['reserved_read_bytes'] != expected_reads
            or reservation['execution_plan_sha256'] != state['linux_execution_plan_sha256']
            or reservation['action_key_sha256'] != key
            or reservation['action_sha256'] != digest(canonical({'schema':1,'kind':'test','name':operation['detail']['name']}))):
        fail()
    request = record['request']
    if request is not None:
        from .linux_test_protocol import validate_execution_request_shape
        validate_execution_request_shape(request)
        if (request['operation_id'] != key
                or request['workflow_id'] != reservation['workflow_id']
                or request['plan_sha256'] != reservation['execution_plan_sha256']
                or request['input_plan_sha256'] != reservation['input_plan_sha256']
                or request['fence_sha256'] != reservation['fence_sha256']
                or request['planner_snapshot_sha256'] != reservation['planner_snapshot_sha256']
                or request['manifest_sha256'] != state['manifest_hash']
                or request['scope_sha256'] != digest(canonical(manifest['scope']))
                or request['root'] != manifest['root'] or request['root_identity'] != manifest['root_identity']
                or any(request[name] != operation['detail'][name]
                       for name in ('name', 'timeout_seconds', 'max_output_bytes'))):
            fail()
    if (record['phase'] in ('captured', 'dispatched', 'broker_admitted') and request is None
            or record['phase'] in ('dispatched', 'broker_admitted')
            and operation.get('admission', {}).get('dispatch') != 'dispatched'):
        fail()
    for name in ('native_receipt_sha256', 'admitted_snapshot_sha256'):
        if record[name] is not None:
            sha256(record[name])
    if record['phase'] == 'broker_admitted':
        if (record['native_receipt_sha256'] is None
                or record['admitted_snapshot_sha256'] != reservation['planner_snapshot_sha256']
                or operation['phase'] != 'complete'
                or operation['receipt'].get('cleanup_confirmed') is not True):
            fail()
    elif record['admitted_snapshot_sha256'] is not None:
        fail()
    if operation['phase'] == 'complete' and operation['receipt']['result'] == 'passed' \
            and record['phase'] != 'broker_admitted':
        fail()


def validate_state(journal, state):
    if type(state) is not dict or set(state) != FIELDS or type(state['schema']) is not int or state['schema'] != 3:
        fail()
    identifier(state['linux_execution_origin'])
    sha256(state['linux_execution_plan_sha256'])
    projection = {name: state[name] for name in ('manifest_hash', 'used', 'operations')}
    projection['schema'] = 1
    journal.validate_state(projection)
    records = state['linux_execution']
    if type(records) is not dict or len(records) > 256:
        fail()
    reserved = 0
    for key, record in records.items():
        sha256(key)
        validate_record(key, record, state, journal.manifest)
        reserved += record['reservation']['reserved_read_bytes']
    if state['used']['read_bytes'] < reserved or state['used']['tests'] < len(records):
        fail()
    for key, operation in state['operations'].items():
        if operation['kind'] == 'test':
            from .linux_test_protocol import linux_execution_backend_id
            if operation['detail']['backend_id'] != linux_execution_backend_id(state['linux_execution_plan_sha256']):
                fail()
        if operation['kind'] == 'test' and operation.get('admission', {}).get('dispatch') == 'dispatched' \
                and key not in records:
            fail()


def guard_save(journal, state):
    journal.require_lock_lease(journal.held_lock_lease())
    if state is not journal._lock_state:
        fail()
    from .dev_journal import MAX_STATE_BYTES, decode
    old = decode(journal.private.read('state.json', MAX_STATE_BYTES))
    validate_state(journal, old)
    if digest(canonical(old)) != journal._state_hash:
        fail()
    if any(state[name] != old[name] for name in
           ('schema', 'manifest_hash', 'linux_execution_origin', 'linux_execution_plan_sha256')):
        fail()
    if any(state['used'][name] < amount for name, amount in old['used'].items()):
        fail()
    previous, current = old['operations'], state['operations']
    if not set(previous) <= set(current) or len(set(current) - set(previous)) > 1:
        fail()
    added = set(current) - set(previous)
    if added:
        journal.require_settled(old)
        if current[next(iter(added))]['phase'] != 'intent':
            fail()
    changed = []
    for key, before in previous.items():
        after = current[key]
        if before == after:
            continue
        changed.append(key)
        if (before['phase'] != 'intent' or after['phase'] not in ('intent', 'complete', 'needs_review')
                or any(before.get(name) != after.get(name) for name in ('id', 'kind', 'request_hash', 'detail'))):
            fail()
        admission = before.get('admission')
        if admission is not None:
            later = after.get('admission')
            if (type(later) is not dict
                    or any(later.get(name) != value for name, value in admission.items() if name != 'dispatch')
                    or admission['dispatch'] == 'dispatched' and later['dispatch'] != 'dispatched'):
                fail()
    if len(changed) > 1 or added and changed:
        fail()
    old_records, records = old['linux_execution'], state['linux_execution']
    if not set(old_records) <= set(records) or len(set(records) - set(old_records)) > 1:
        fail()
    for key, before in old_records.items():
        after = records[key]
        if before == after:
            continue
        if (before['phase'] in ('broker_admitted', 'needs_review')
                or before['reservation'] != after['reservation']
                or before['request'] is not None and before['request'] != after['request']
                or before['native_receipt_sha256'] is not None
                   and before['native_receipt_sha256'] != after['native_receipt_sha256']
                or after['phase'] not in ({'reserved': {'captured', 'needs_review'},
                    'captured': {'dispatched', 'needs_review'},
                    'dispatched': {'broker_admitted', 'needs_review'}}.get(before['phase'], set()))):
            fail()
    if sum(old_records[key] != records[key] for key in old_records) > 1:
        fail()
    for key in set(records) - set(old_records):
        record = records[key]
        if (record['phase'] != 'reserved'
                or record['reservation']['prior_state_sha256'] != journal._state_hash
                or state['used']['read_bytes'] < old['used']['read_bytes'] + record['reservation']['reserved_read_bytes']):
            fail()
