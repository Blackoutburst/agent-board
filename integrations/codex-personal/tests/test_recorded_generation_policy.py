"""Real queued cycles across distinct, synthetic Linux namespace observations.

Board, Git, Worker, Core, Runtime, policy objects, records and file pins are
real temporary components. Existing host/native qualification and CLI seams
remain synthetic; no actual namespace, native test or provider is launched.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from blackoutburst_agents import development_cycle_driver as driver_module
from blackoutburst_agents import linux_planner_policy as policy_module
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
import test_development_cycle_linux_preparation as boundaries
from test_continuation_pool_service_integration import actual_service as _original_actual_service
from test_queued_continuation_service import queued_service, linux_lock_observation, target, metadata
from test_linux_planner_policy import environment


@pytest.fixture
def actual_service(tmp_path, monkeypatch, linux_lock_observation):
    # Both the existing backend plan seam and policy observation capture this
    # same object. Mutating its values models a new invocation, without making
    # either validator ignore the new environment or changing stored records.
    current = environment()
    monkeypatch.setattr(boundaries, 'environment', lambda: current)
    for value in _original_actual_service.__wrapped__(tmp_path, monkeypatch, linux_lock_observation):
        value[0].generation_environment = current
        yield value


def next_actions(sequence):
    sequence.write_text(json.dumps([
        dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 2\n'), content='value = 3\n'),
        dict(schema=1, kind='read', path='one.py')]))


def policy_path(generation, operation):
    return generation / 'preparation' / operation / 'policy'


def test_new_generation_qualifies_its_own_current_policy_after_namespace_change(queued_service, monkeypatch):
    f, root, task_id, ticket, construct, calls, sequence, admission = queued_service
    qualifications = []
    backend_qualify = SupervisedLinuxNamedPythonBackend.qualify
    policy_qualify = LinuxPlannerPolicy.qualify

    def qualify_backend(backend):
        result = backend_qualify(backend)
        assert result['cleanup_confirmed'] is True
        assert result['checks'] and all(result['checks'].values())
        qualifications.append(('backend', deepcopy(backend.environment)))
        return result

    def qualify_policy(policy):
        result = policy_qualify(policy)
        assert result['cleanup_confirmed'] is True
        assert result['checks'] and all(result['checks'].values())
        qualifications.append(('policy', deepcopy(policy.environment)))
        return result

    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', qualify_backend)
    monkeypatch.setattr(LinuxPlannerPolicy, 'qualify', qualify_policy)
    first = construct().run_once()
    assert first['tasks'][0]['phase'] == 'paused_cycle', first
    first_op = first['tasks'][0]['operation_id']
    directory = target(root, task_id)
    first_root, first_meta = metadata(directory, first_op)
    old_policy = policy_path(first_root, first_op)
    old_plan = decode(file_document(old_policy / 'planner-plan.json')[0])
    old_files = {path: path.read_bytes() for path in first_root.rglob('*') if path.is_file()}
    released = f.board.get_ticket(f.human, ticket['id'])['ticket']
    current = f.generation_environment
    for key in current['namespaces']:
        current['namespaces'][key] += 1000
    assert current != old_plan['environment']
    # The execution-policy constructor remains strict for current execution.
    with pytest.raises(DevelopmentError, match='linux_planner_policy_changed'):
        with LinuxPlannerPolicy(old_policy):
            pytest.fail('Historical policy became a current execution handle')
    recorded = driver_module._recorded_linux_policy(old_policy, digest(canonical(old_plan)))
    assert recorded == old_plan
    next_actions(sequence)
    second = construct().run_once()
    assert second['tasks'][0]['phase'] == 'paused_cycle', second
    second_op = second['tasks'][0]['operation_id']
    second_root, second_meta = metadata(directory, second_op)
    assert second_op != first_op
    assert second_meta['submitted']['session_id'] != first_meta['submitted']['session_id']
    assert second_meta['queue_admission'] == {**admission, 'ticket_version': released['version']}
    assert [row['expected_version'] for row in f.queue_claims] == [admission['ticket_version'], released['version']]
    assert [kind for kind, _ in qualifications] == ['backend', 'policy', 'backend', 'policy']
    assert [value for _, value in qualifications[:2]] == [old_plan['environment']] * 2
    assert [value for _, value in qualifications[2:]] == [current] * 2
    with LinuxPlannerPolicy(policy_path(second_root, second_op)) as policy:
        policy.validate_for(policy.executable, policy.workspace)
        assert policy.environment == current
        assert policy.fingerprint != digest(canonical(old_plan))
    assert all(path.read_bytes() == value for path, value in old_files.items())
    assert len(calls.read_text().splitlines()) == 4
    assert f.board.get_runtime(f.human)['active_count'] == 0
    with ContinuationJournal.open(directory / 'logical') as journal:
        state = journal.snapshot()['state']
    assert state['cycle_seq'] == 2 and state['used_total']['turns'] == state['used_total']['actions'] == 4


@pytest.mark.parametrize('drift', ['namespace_keys', 'uid', 'capabilities', 'codex_pin', 'source_pins'])
def test_recorded_and_fresh_controls_reject_other_drift_before_new_inference(queued_service, monkeypatch, drift):
    f, root, task_id, ticket, construct, calls, sequence, admission = queued_service
    first = construct().run_once()
    assert first['tasks'][0]['phase'] == 'paused_cycle', first
    directory = target(root, task_id)
    first_root, _ = metadata(directory, first['tasks'][0]['operation_id'])
    old_policy = policy_path(first_root, first['tasks'][0]['operation_id'])
    old_plan = decode(file_document(old_policy / 'planner-plan.json')[0])
    fingerprint = digest(canonical(old_plan))
    assert driver_module._recorded_linux_policy(old_policy, fingerprint) == old_plan
    old_files = {path: path.read_bytes() for path in first_root.rglob('*') if path.is_file()}
    current = f.generation_environment
    for key in current['namespaces']:
        current['namespaces'][key] += 1000
    if drift == 'namespace_keys':
        del current['namespaces']['mnt']
    elif drift == 'uid':
        current['uid'] += 1
        current['euid'] += 1
    elif drift == 'capabilities':
        current['capabilities']['CapEff'] = 1
    elif drift == 'codex_pin':
        cli = Path(f.tools['codex'])
        cli.write_bytes(cli.read_bytes() + b'\n# changed executable pin\n')
    else:
        monkeypatch.setattr(policy_module, 'linux_source_pins', lambda: {'synthetic_test_only': 'changed source pins'})
    # Reach the exact checker before the service's earlier/different guards
    # can mask whether the new historical/environment boundary is strict.
    if drift in ('namespace_keys', 'uid', 'capabilities'):
        with pytest.raises(DevelopmentError):
            driver_module._generation_environment(old_plan['environment'], current)
    else:
        with pytest.raises(DevelopmentError, match='development_cycle_tools_changed'):
            driver_module._recorded_linux_policy(old_policy, fingerprint)
    next_actions(sequence)
    second = construct().run_once()
    assert second['status'] == 'needs_you', second
    assert len(f.queue_claims) == 1
    assert len(calls.read_text().splitlines()) == 2
    assert f.board.get_ticket(f.human, ticket['id'])['ticket']['claimant'] is None
    assert f.board.get_runtime(f.human)['active_count'] == 0
    assert all(path.read_bytes() == value for path, value in old_files.items())
