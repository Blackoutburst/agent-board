"""Actual queued generations with synthetic host/CLI qualification seams.

The timed probes call the real backend qualification authorizer, preparer and
Board authorization. Only their clock is synthetic; no native process or model
is launched by a probe, and the production 1.5-second TTL stays unchanged.
"""
from copy import deepcopy
import json

import pytest

from blackoutburst_agents import continuation_pool_service as service_module
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.dev_manifest import DevelopmentError, digest
from blackoutburst_agents.dev_test_supervisor import _fresh_control_window
from blackoutburst_agents.linux_pool_preparation import LinuxPoolJobPreparer
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend, _QualificationContext
from test_queued_continuation_service import (
    queued_service, actual_service, linux_lock_observation, target, metadata)


def followup(sequence):
    sequence.write_text(json.dumps([
        dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 2\n'), content='value = 3\n'),
        dict(schema=1, kind='read', path='one.py')]))


def window(backend, clock, *, heartbeat=False):
    context = _QualificationContext(backend, 'normal')
    def authorize():
        backend._pins()
        return context.authorize()
    nonce = 'a' * 32
    previous = (dict(schema=1, nonce=nonce, sequence=1, started_at=999.,
        validated_at=999.9, deadline=1000.5) if heartbeat else None)
    return _fresh_control_window(authorize, nonce=nonce, sequence=2 if heartbeat else 1,
        previous=previous, clock=clock)


def test_native_qualification_windows_do_not_repeat_old_generation_callbacks(queued_service, monkeypatch):
    f, root, task_id, ticket, construct, calls, sequence, admission = queued_service
    original_qualify = SupervisedLinuxNamedPythonBackend.qualify
    original_snapshot = DevelopmentAuthorization.snapshot
    original_scope = service_module._LiveChecks.scope
    probe = [None]
    observed = []
    boundary_counts = [0]
    snapshots_at_qualification = []

    def snapshot(self, *args, **kwargs):
        if probe[0] is not None:
            probe[0]['snapshots'] += 1
            probe[0]['clock'] += probe[0]['cost']
        return original_snapshot(self, *args, **kwargs)

    def scope(self, context):
        if probe[0] is not None:
            probe[0]['scope_callbacks'] += 1
        else:
            boundary_counts[0] += 1
        return original_scope(self, context)

    def qualify(backend):
        snapshots_at_qualification.append(boundary_counts[0])
        generation = len(snapshots_at_qualification)
        for cost, heartbeat, expected in ((0., False, None), (.8, False, None),
                (.303, True, None), (1.5, False, 'supervisor_authorization_expired')):
            value = dict(generation=generation, cost=cost, heartbeat=heartbeat,
                clock=1000., snapshots=0, scope_callbacks=0, code=None)
            probe[0] = value
            try:
                window(backend, lambda: value['clock'], heartbeat=heartbeat)
            except DevelopmentError as error:
                value['code'] = error.code
            finally:
                probe[0] = None
            observed.append(value)
            assert value['code'] == expected, value
            assert value['snapshots'] == 1 and value['scope_callbacks'] == 0, value
        return original_qualify(backend)

    monkeypatch.setattr(DevelopmentAuthorization, 'snapshot', snapshot)
    monkeypatch.setattr(service_module._LiveChecks, 'scope', scope)
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', qualify)
    first = construct().run_once()
    assert first['tasks'][0]['phase'] == 'paused_cycle', first
    directory = target(root, task_id)
    first_root, first_meta = metadata(directory, first['tasks'][0]['operation_id'])
    old = {path: path.read_bytes() for path in first_root.rglob('*') if path.is_file()}
    released = f.board.get_ticket(f.human, ticket['id'])['ticket']
    followup(sequence)
    second = construct().run_once()
    assert second['tasks'][0]['phase'] == 'paused_cycle', second
    _, second_meta = metadata(directory, second['tasks'][0]['operation_id'])
    assert second_meta['queue_admission'] == {**admission, 'ticket_version': released['version']}
    assert second_meta['submitted']['session_id'] != first_meta['submitted']['session_id']
    assert [item['expected_version'] for item in f.queue_claims] == [
        admission['ticket_version'], released['version']]
    assert len(observed) == 8 and len(calls.read_text().splitlines()) == 4
    assert all(path.read_bytes() == raw for path, raw in old.items())
    # Full logical observations still bracket next preparation outside the
    # native control windows, and must also run after qualification returns.
    assert snapshots_at_qualification[1] > snapshots_at_qualification[0]
    assert boundary_counts[0] > snapshots_at_qualification[1]
    with ContinuationJournal.open(directory / 'logical') as journal:
        state = journal.snapshot()['state']
    assert state['phase'] == 'paused_cycle' and state['cycle_seq'] == 2
    assert state['used_total']['turns'] == state['used_total']['actions'] == 4
    assert f.board.get_runtime(f.human)['active_count'] == 0


@pytest.mark.parametrize('failure', [
    'stop_during_new_snapshot', 'new_permission_revoked',
    'scope_boundary_before_qualification', 'source_boundary_after_qualification'])
def test_next_preparation_keeps_local_stop_fresh_authority_and_full_boundaries(
        queued_service, monkeypatch, failure):
    f, root, task_id, ticket, construct, calls, sequence, admission = queued_service
    first = construct().run_once()
    assert first['tasks'][0]['phase'] == 'paused_cycle', first
    first_root, first_metadata = metadata(target(root, task_id), first['tasks'][0]['operation_id'])
    old = {path: path.read_bytes() for path in first_root.rglob('*') if path.is_file()}
    current_preparer = [None]
    probe = [False]
    qualified = []
    injected = []
    refused = []
    armed = [False]
    original_prepare = LinuxPoolJobPreparer.prepare
    original_qualify = SupervisedLinuxNamedPythonBackend.qualify
    original_snapshot = DevelopmentAuthorization.snapshot
    original_scope = service_module._LiveChecks.scope
    original_source = service_module._LiveChecks.source

    def prepare(preparer, *args, **kwargs):
        current_preparer[0] = preparer
        try:
            return original_prepare(preparer, *args, **kwargs)
        finally:
            current_preparer[0] = None

    def snapshot(authority, *args, **kwargs):
        result = original_snapshot(authority, *args, **kwargs)
        if probe[0] and failure == 'stop_during_new_snapshot' and not injected:
            # This is the new, unclaimed preparer's concrete authorization;
            # stop only after its RPC/Board observation has already returned.
            task = current_preparer[0]._checkpoint_preparation.task
            assert authority.client.session_id != first_metadata['submitted']['session_id']
            state = task.snapshot()['state']
            task.stop(expected_revision=state['revision'], request_id='human-stop-during-preparation-frame')
            injected.append('stop')
        return result

    def scope(checks, context):
        if failure == 'scope_boundary_before_qualification' and current_preparer[0] is not None:
            injected.append('scope')
            raise DevelopmentError('continuation_pool_binding_changed')
        return original_scope(checks, context)

    def source(checks, context):
        if failure == 'source_boundary_after_qualification' and armed[0]:
            injected.append('source')
            raise DevelopmentError('continuation_pool_binding_changed')
        return original_source(checks, context)

    def qualify(backend):
        qualified.append(backend.manifest['scope']['session_id'])
        if failure in ('stop_during_new_snapshot', 'new_permission_revoked'):
            if failure == 'new_permission_revoked':
                f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
                injected.append('permission')
            probe[0] = True
            try:
                window(backend, lambda: 1000.)
            except DevelopmentError as error:
                refused.append(error.code)
                raise
            else:
                refused.append('unexpected_authorization')
                raise DevelopmentError('test_authorization_should_have_refused')
            finally:
                probe[0] = False
        result = original_qualify(backend)
        armed[0] = True
        return result

    monkeypatch.setattr(LinuxPoolJobPreparer, 'prepare', prepare)
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', qualify)
    monkeypatch.setattr(DevelopmentAuthorization, 'snapshot', snapshot)
    monkeypatch.setattr(service_module._LiveChecks, 'scope', scope)
    monkeypatch.setattr(service_module._LiveChecks, 'source', source)
    followup(sequence)
    second = construct().run_once()
    assert second['status'] == 'needs_you', second
    assert injected
    assert len(qualified) == (0 if failure == 'scope_boundary_before_qualification' else 1)
    if failure in ('stop_during_new_snapshot', 'new_permission_revoked'):
        assert refused == ['permission_revoked']
    assert len(f.queue_claims) == 1 and len(calls.read_text().splitlines()) == 2
    assert f.board.get_ticket(f.human, ticket['id'])['ticket']['claimant'] is None
    assert f.board.get_runtime(f.human)['active_count'] == 0
    assert all(path.read_bytes() == raw for path, raw in old.items())
