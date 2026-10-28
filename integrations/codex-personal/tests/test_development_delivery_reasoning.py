"""Optional reasoning through real Worker/Git/Runtime and synthetic Linux docs.

No model, native Linux execution, network or live account is used. The existing
delivery fixture supplies explicit archived Linux observations for validators.
"""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.development_delivery import DevelopmentDeliveryProof
import test_development_delivery as fixtures


@contextmanager
def ready(effort=None):
    case = fixtures.DevelopmentDeliveryTests(methodName='runTest')
    case.setUp()
    try:
        prepare = case.prepare
        if effort is not None:
            case.prepare = lambda **options: prepare(reasoning_effort=effort, **options)
        case.ready()
        yield case
    finally:
        case.doCleanups()


def planner_document(case):
    with PrivateDirectory(case.broker_dir) as private:
        return decode(private.read('planner.json', 2 * 1024 * 1024))


def write_planner(case, planner):
    planner['configuration_hash'] = digest(canonical(planner['configuration']))
    with PrivateDirectory(case.broker_dir) as private:
        private.replace('planner.json', canonical(planner))


def finalize(case, evidence):
    return case.deliveries.finalize(operation_id=case.operation,
        snapshot_sha256=evidence['snapshot_sha256'], test_evidence=evidence,
        message='Explicit synthetic reasoning delivery')


@pytest.mark.parametrize('effort', [None, 'low'], ids=['absent', 'low'])
def test_optional_reasoning_supports_evidence_and_normal_delivery(effort):
    with ready(effort) as case:
        configuration = case.journal.get(case.operation)['configuration']
        assert ('reasoning_effort' in configuration) is (effort is not None)
        assert configuration.get('reasoning_effort') == effort
        assert planner_document(case)['configuration'].get('reasoning_effort') == effort
        before = (case.broker_dir / 'state.json').read_bytes()
        head = fixtures.git(case.root, 'rev-parse', 'HEAD')
        index = (case.root / '.git/index').read_bytes()
        model_calls, test_calls = len(case.inferences), len(case.backend.calls)
        evidence = case.proof.evidence(case.snapshot)
        seal = finalize(case, evidence)
        assert seal['test_evidence'] == evidence
        assert seal['base_commit'] == head and seal['delivery_sha'] != head
        assert fixtures.git(case.root, 'rev-parse', 'HEAD') == head
        assert (case.root / '.git/index').read_bytes() == index
        assert (case.broker_dir / 'state.json').read_bytes() == before
        assert (len(case.inferences), len(case.backend.calls)) == (model_calls, test_calls)


@pytest.mark.parametrize('changed', ['medium', None, False], ids=['other-effort', 'removed', 'wrong-type'])
def test_planner_reasoning_drift_cannot_reuse_prior_test_evidence(changed):
    with ready('low') as case:
        evidence = case.proof.evidence(case.snapshot)
        planner = planner_document(case)
        if changed is None:
            del planner['configuration']['reasoning_effort']
        else:
            planner['configuration']['reasoning_effort'] = changed
        # Recompute the self-hash: the worker's exact stored option must still
        # constrain the planner, rather than trusting its own new fingerprint.
        write_planner(case, planner)
        before = (len(case.inferences), len(case.backend.calls))
        with pytest.raises(DevelopmentError):
            case.proof.evidence(case.snapshot)
        with pytest.raises(DevelopmentError):
            finalize(case, evidence)
        assert (len(case.inferences), len(case.backend.calls)) == before
        assert not fixtures.git(case.root, 'for-each-ref', '--format=%(refname)',
                                'refs/heads/codex/delivery-')


@pytest.mark.parametrize('value', [None, True, 3, 'low '],
                         ids=['present-none', 'boolean', 'number', 'untrimmed'])
def test_malformed_stored_optional_reasoning_is_not_normalized(value):
    with ready() as case:
        evidence = case.proof.evidence(case.snapshot)
        operation = case.journal.get(case.operation)
        configuration = deepcopy(operation['configuration'])
        payload = deepcopy(operation['payload'])
        configuration['reasoning_effort'] = value
        parent = payload['parent_binding']
        parent['parent_scope']['configuration_hash'] = digest(canonical({
            'configuration': configuration,
            'initial_context_hash': payload['initial_context']['fingerprint']}))
        parent['configuration_hash'] = parent['parent_scope']['configuration_hash']
        planner = planner_document(case)
        planner['configuration']['parent_operation'] = deepcopy(parent['parent_scope'])
        if value is not None:
            planner['configuration']['reasoning_effort'] = value
        write_planner(case, planner)
        # This is only the same synthetic archived-document construction used
        # by the existing Linux fixture, never a production state migration.
        case.journal._db.execute('UPDATE operations SET configuration_json=?,payload_json=? WHERE id=?',
            (canonical(configuration).decode(), canonical(payload).decode(), case.operation))
        case.proof = DevelopmentDeliveryProof(case.worker, case.workspaces, case.operation)
        case.deliveries.test_guard = case.proof
        before = (len(case.inferences), len(case.backend.calls))
        with pytest.raises(DevelopmentError):
            case.proof.evidence(case.snapshot)
        with pytest.raises(DevelopmentError):
            finalize(case, evidence)
        assert (len(case.inferences), len(case.backend.calls)) == before
