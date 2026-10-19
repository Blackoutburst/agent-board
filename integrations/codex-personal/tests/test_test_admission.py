"""Durable non-dispatch proofs; model, control and backend are local doubles."""
from contextlib import nullcontext
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import Mock, patch

from blackoutburst_agents.development_cleanup import _effect_receipt, reconcile_cleanup
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.dev_manifest import Budgets, DevelopmentError, NamedTest, canonical, digest, prepare_manifest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.dev_test_backend import NativeNamedPythonBackend
import test_development_broker as broker_fixture
import test_dev_planner as planner_fixture


class TestAdmissionBrokerTests(unittest.TestCase):
    setUp = broker_fixture.DevelopmentBrokerTests.setUp
    authorized = broker_fixture.DevelopmentBrokerTests.authorized
    prepare = broker_fixture.DevelopmentBrokerTests.prepare
    open = broker_fixture.DevelopmentBrokerTests.open
    state = broker_fixture.DevelopmentBrokerTests.state
    error = broker_fixture.DevelopmentBrokerTests.error

    def operation(self, key='test-key'):
        return self.state()['operations'][digest(key.encode('ascii'))]

    def test_unqualified_refusal_is_exact_durable_and_reconciles_without_backend(self):
        self.backend.qualified = False
        self.error('test_backend_unqualified', self.broker.run_test, 'test-key', 'unit')
        operation = self.operation()
        admission, receipt = operation['admission'], operation['receipt']
        self.assertEqual(operation['phase'], 'complete')
        self.assertEqual(admission, {'schema': 1, 'operation_id': digest(b'test-key'),
            'manifest_hash': self.broker.journal.manifest_hash,
            'request_hash': operation['request_hash'], 'backend_id': self.backend.backend_id,
            'root_identity': self.broker.manifest['root_identity'], 'dispatch': 'pending'})
        self.assertEqual(receipt, {'operation_id': operation['id'], 'kind': 'test', 'status': 'complete',
            'result': 'not_started', 'name': 'unit', 'cleanup_confirmed': True, 'started': False,
            'reason': 'test_backend_unqualified', 'manifest_hash': self.broker.journal.manifest_hash,
            'request_hash': operation['request_hash'], 'admission_sha256': digest(canonical(admission))})
        self.assertEqual(self.state()['used']['tests'], 0)
        self.assertEqual(self.state()['used']['operations'], 1)
        forbidden = Mock()
        forbidden.qualified_for.side_effect = AssertionError('qualification forbidden during recovery')
        forbidden.run.side_effect = AssertionError('run forbidden during recovery')
        forbidden.lookup.side_effect = AssertionError('lookup forbidden for known refusal')
        reopened = self.open(test_backend=forbidden)
        before = self.state()
        self.assertEqual(reopened.reconcile('test-key'), receipt)
        self.error('test_backend_unqualified', reopened.run_test, 'test-key', 'unit')
        self.assertEqual(self.state(), before)
        forbidden.qualified_for.assert_not_called()
        forbidden.run.assert_not_called()
        forbidden.lookup.assert_not_called()
        self.assertEqual(self.backend.calls, [])

    def test_expired_second_qualification_never_crosses_dispatch_boundary(self):
        self.backend.qualified_for = Mock(side_effect=[True, False])
        self.error('test_backend_unqualified', self.broker.run_test, 'test-key', 'unit')
        self.assertEqual(self.backend.qualified_for.call_count, 2)
        self.assertEqual(self.operation()['receipt']['result'], 'not_started')
        self.assertEqual(self.operation()['admission']['dispatch'], 'pending')
        self.assertEqual(self.backend.calls, [])

    def test_test_budget_refusal_has_proof_without_spending_a_test(self):
        directory = self.base / 'limited'
        manifest = self.broker.manifest
        prepare_manifest(directory, root=self.root, base_sha=manifest['base_sha'], scope=self.scope,
            paths=['src/main.py'], resource='repo:fixture', authorize_scope=self.authorized,
            tests=[NamedTest(**suite) for suite in manifest['tests']], budgets=Budgets(max_tests=0))
        with DevelopmentBroker(directory, authorize_scope=self.authorized,
                               enabled=True, test_backend=self.backend) as broker:
            self.error('tests_budget_exhausted', broker.run_test, 'test-key', 'unit')
            receipt = broker.reconcile('test-key')
        self.assertEqual((receipt['result'], receipt['reason']), ('not_started', 'tests_budget_exhausted'))
        self.assertEqual(self.backend.calls, [])

    def test_last_authorization_is_checked_after_qualification_before_dispatch(self):
        calls = []

        def authorize(*args):
            calls.append(args)
            return len(calls) < 3

        self.broker.authorize_scope = authorize
        self.error('permission_revoked', self.broker.run_test, 'test-key', 'unit')
        self.assertEqual(self.operation()['receipt']['reason'], 'permission_revoked')
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.backend.calls, [])

    def test_backend_observes_exact_durable_dispatched_admission(self):
        original = self.backend.run

        def run(request):
            operation = self.operation()
            self.assertEqual(operation['admission']['dispatch'], 'dispatched')
            self.assertEqual(operation['id'], request.operation_id)
            self.assertEqual(operation['admission']['manifest_hash'], request.manifest_hash)
            self.assertEqual(operation['admission']['root_identity'], list(request.root_identity))
            self.assertEqual(operation['phase'], 'intent')
            self.assertIsNone(operation['receipt'])
            return original(request)

        self.backend.run = run
        self.assertEqual(self.broker.run_test('test-key', 'unit')['result'], 'passed')
        self.assertEqual(self.state()['used']['tests'], 1)

    def test_failed_dispatch_save_never_runs_and_pending_without_final_receipt_stays_review(self):
        original = self.broker.journal.save

        def save(state):
            operation = state['operations'].get(digest(b'test-key'))
            if operation is not None and operation['admission']['dispatch'] == 'dispatched':
                raise DevelopmentError('private_write_failed')
            return original(state)

        with patch.object(self.broker.journal, 'save', side_effect=save):
            self.error('private_write_failed', self.broker.run_test, 'test-key', 'unit')
        operation = self.operation()
        self.assertEqual(operation['admission']['dispatch'], 'pending')
        self.assertIsNone(operation['receipt'])
        before = self.state()
        with patch.object(self.backend, 'qualified_for', side_effect=AssertionError('no qualification')), \
                patch.object(self.backend, 'lookup', side_effect=AssertionError('no lookup')):
            self.error('test_admission_requires_review', self.open().reconcile, 'test-key')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.backend.calls, [])

    def test_dispatch_save_uncertainty_is_never_changed_into_not_started(self):
        original = self.broker.journal.save

        def save(state):
            original(state)
            operation = state['operations'].get(digest(b'test-key'))
            if operation is not None and operation['admission']['dispatch'] == 'dispatched':
                raise DevelopmentError('private_write_failed')

        with patch.object(self.broker.journal, 'save', side_effect=save):
            self.error('private_write_failed', self.broker.run_test, 'test-key', 'unit')
        self.assertEqual(self.operation()['admission']['dispatch'], 'dispatched')
        self.assertIsNone(self.broker.reject_test_before_dispatch('test-key', 'private_write_failed'))
        self.assertEqual(self.broker.reconcile('test-key')['result'], 'needs_review')
        self.error('reconciliation_required', self.broker.run_test, 'test-key', 'unit')
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(self.backend.lookups), 1)

    def test_ambiguous_dispatch_uses_only_exact_lookup_and_never_a_new_run(self):
        self.backend.interrupt = True
        with self.assertRaises(broker_fixture.Interrupted):
            self.broker.run_test('test-key', 'unit')
        self.assertIsNone(self.broker.reject_test_before_dispatch('test-key', 'test_admission_failed'))
        self.backend.results.clear()
        self.assertEqual(self.open().reconcile('test-key')['result'], 'needs_review')
        self.error('reconciliation_required', self.broker.run_test, 'test-key', 'unit')
        self.error('reconciliation_required', self.broker.run_test, 'new-key', 'unit')
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.backend.lookups), 1)

    def test_old_intent_without_admission_cannot_gain_non_dispatch_proof(self):
        suite = self.broker.manifest['tests'][0]
        with self.broker.journal.lock() as state:
            self.broker.journal.charge(state, tests=1)
            self.broker._intent(state, 'test-key', 'test', {'kind': 'test', **suite}, dict(suite))
        before = self.state()
        self.assertIsNone(self.broker.reject_test_before_dispatch('test-key', 'test_backend_unqualified'))
        self.backend.qualified = False
        self.error('test_backend_unqualified', self.broker.reconcile, 'test-key')
        self.assertEqual(self.state(), before)
        self.assertIsNone(self.operation()['receipt'])
        self.assertEqual(self.backend.calls, [])

    def test_pending_admission_cannot_be_dispatched_or_rejected_after_reopen(self):
        self.broker.admit_test('test-key', 'unit')
        before = self.state()
        reopened = self.open()
        self.error('reconciliation_required', reopened.run_test, 'test-key', 'unit')
        self.assertIsNone(reopened.reject_test_before_dispatch('test-key', 'test_admission_failed'))
        self.error('test_admission_requires_review', reopened.reconcile, 'test-key')
        self.assertEqual(self.state(), before)
        self.assertEqual(self.backend.calls, [])

    def test_non_dispatch_receipt_identity_or_dispatch_tampering_is_refused(self):
        self.backend.qualified = False
        self.error('test_backend_unqualified', self.broker.run_test, 'test-key', 'unit')
        original = self.state()
        mutations = [lambda op: op['admission'].update(manifest_hash='0' * 64),
            lambda op: op['admission'].update(request_hash='0' * 64),
            lambda op: op['admission'].update(backend_id='other'),
            lambda op: op['admission'].update(root_identity=[1, 2]),
            lambda op: op['admission'].update(root_identity=[True, 2]),
            lambda op: op['admission'].update(dispatch='dispatched'),
            lambda op: op.update(admission=None),
            lambda op: op.pop('admission'),
            lambda op: op['receipt'].update(started=True),
            lambda op: op['receipt'].update(admission_sha256='0' * 64),
            lambda op: op['receipt'].update(reason='PRIVATE diagnostic / unbounded text')]
        try:
            for mutate in mutations:
                state = deepcopy(original)
                mutate(state['operations'][digest(b'test-key')])
                (self.directory / 'state.json').write_text(json.dumps(state))
                self.error('journal_corrupt', self.broker.status, 'test-key')
        finally:
            (self.directory / 'state.json').write_text(json.dumps(original))


class TestAdmissionPlannerTests(unittest.TestCase):
    setUp = planner_fixture.PlannerTests.setUp
    create = planner_fixture.PlannerTests.create
    transport = planner_fixture.PlannerTests.transport
    action = planner_fixture.PlannerTests.action

    def failed_test(self, **options):
        self.actions = [self.action('test', name='unit')]
        planner = self.create(**options)
        self.backend.qualified = False
        result = planner.run()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['reason'], 'test_backend_unqualified')
        step = planner.journal.read()['step']
        receipt = self.broker.status(step['action_key'])['receipt']
        self.assertEqual(receipt['result'], 'not_started')
        self.assertFalse(self.backend.calls)
        return planner, step, receipt

    def test_refusal_recovery_finishes_blocked_without_advancing_or_qualifying(self):
        planner, step, receipt = self.failed_test()
        starts = sum(name == 'start_execution' for name, _ in self.control.calls)
        reserved = sum(name == 'reserve_execution' for name, _ in self.control.calls)
        with patch.object(self.backend, 'qualified_for', side_effect=AssertionError('no qualification')), \
                patch.object(self.backend, 'lookup', side_effect=AssertionError('no lookup')), \
                patch.object(self.backend, 'run', side_effect=AssertionError('no launch')), \
                patch.object(self.broker, 'diff', side_effect=AssertionError('no inspection')):
            result = planner.reconcile()
            used = deepcopy(planner.journal.read()['used'])
            finished = sum(name == 'finish_execution' for name, _ in self.control.calls)
            self.assertEqual(planner.reconcile(), result)
            self.assertEqual(planner.journal.read()['used'], used)
            self.assertEqual(sum(name == 'finish_execution' for name, _ in self.control.calls), finished)
        self.assertEqual((result['status'], result['reason']), ('needs_review', receipt['reason']))
        after = planner.journal.read()
        self.assertEqual(after['step']['action_key'], step['action_key'])
        self.assertTrue(after['step']['tool_finished'])
        self.assertEqual(after['history'], [])
        self.assertEqual(after['tests'], {})
        tool = next(row for row in self.control.rows.values() if row['provider'] == 'local_broker')
        self.assertEqual((tool['state'], tool['outcome'], tool['cleanup_confirmed']), ('finished', 'blocked', True))
        self.assertEqual(planner.run(), result)
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(sum(name == 'start_execution' for name, _ in self.control.calls), starts)
        self.assertEqual(sum(name == 'reserve_execution' for name, _ in self.control.calls), reserved)

    def test_preparation_budget_error_is_recorded_before_backend_qualification(self):
        self.actions = [self.action('test', name='unit')]
        planner = self.create()
        with patch.object(self.broker, 'diff', side_effect=DevelopmentError('read_bytes_budget_exhausted')), \
                patch.object(self.backend, 'qualified_for', side_effect=AssertionError('not yet qualified')):
            result = planner.run()
        self.assertEqual(result['reason'], 'read_bytes_budget_exhausted')
        step = planner.journal.read()['step']
        receipt = self.broker.status(step['action_key'])['receipt']
        self.assertEqual((receipt['result'], receipt['reason']), ('not_started', 'read_bytes_budget_exhausted'))
        self.assertEqual(self.backend.calls, [])

    def test_stop_during_preparation_is_preserved_and_never_dispatches(self):
        self.actions = [self.action('test', name='unit')]
        planner = self.create()
        original = self.broker.diff

        def stop_after_snapshot():
            result = original()
            planner.request_stop()
            return result

        with patch.object(self.broker, 'diff', side_effect=stop_after_snapshot):
            result = planner.run()
        self.assertEqual(result['reason'], 'planner_stop_requested')
        step = planner.journal.read()['step']
        receipt = self.broker.status(step['action_key'])['receipt']
        self.assertEqual((receipt['result'], receipt['reason']), ('not_started', 'planner_stop_requested'))
        self.assertEqual(self.backend.calls, [])

    def test_deadline_after_preparation_is_preserved_and_never_dispatches(self):
        self.actions = [self.action('test', name='unit')]
        planner = self.create(budgets=PlannerBudgets(max_wall_seconds=5))
        original = self.broker.diff

        def expire_after_snapshot():
            result = original()
            self.clock += 6
            return result

        with patch.object(self.broker, 'diff', side_effect=expire_after_snapshot):
            result = planner.run()
        self.assertEqual(result['reason'], 'planner_wall_budget_exhausted')
        step = planner.journal.read()['step']
        receipt = self.broker.status(step['action_key'])['receipt']
        self.assertEqual((receipt['result'], receipt['reason']), ('not_started', 'planner_wall_budget_exhausted'))
        self.assertEqual(self.backend.calls, [])

    def test_deadline_expiring_during_qualification_keeps_the_real_refusal_reason(self):
        self.actions = [self.action('test', name='unit')]
        planner = self.create(budgets=PlannerBudgets(max_wall_seconds=5))
        qualifications = []

        def qualify(*args):
            qualifications.append(args)
            if len(qualifications) == 2:
                self.clock += 6
            return True

        with patch.object(self.backend, 'qualified_for', side_effect=qualify):
            result = planner.run()
        self.assertEqual(result['reason'], 'planner_wall_budget_exhausted')
        step = planner.journal.read()['step']
        receipt = self.broker.status(step['action_key'])['receipt']
        self.assertEqual((receipt['result'], receipt['reason']), ('not_started', 'planner_wall_budget_exhausted'))
        self.assertEqual(self.backend.calls, [])

    def test_native_authorization_refusal_preserves_deadline_before_receipt_write(self):
        self.actions = [self.action('test', name='unit')]
        self.backend.authorize_scope = lambda *_: True
        planner = self.create(budgets=PlannerBudgets(max_wall_seconds=5))

        def qualify(manifest_hash, suite):
            self.clock += 6
            # Exercise the native qualification's actual authorization/error
            # handling without constructing a backend, process or sandbox.
            native = object.__new__(NativeNamedPythonBackend)
            native.manifest = self.broker.manifest
            native.authorize_scope = self.backend.authorize_scope
            with patch.object(native, '_pins', side_effect=AssertionError('no qualification after refusal')):
                return native.qualified_for(manifest_hash, suite)

        with patch.object(self.backend, 'qualified_for', side_effect=qualify) as qualification, \
                patch.object(self.broker, '_finish', wraps=self.broker._finish) as finish:
            result = planner.run()
        self.assertEqual((result['status'], result['reason']),
                         ('needs_review', 'planner_wall_budget_exhausted'))
        step = planner.journal.read()['step']
        receipt = self.broker.status(step['action_key'])['receipt']
        self.assertEqual((receipt['result'], receipt['reason']),
                         ('not_started', 'planner_wall_budget_exhausted'))
        self.assertEqual(finish.call_count, 1)
        self.assertEqual(finish.call_args.args[2], 'not_started')
        self.assertEqual(finish.call_args.kwargs['reason'], 'planner_wall_budget_exhausted')
        self.assertEqual(qualification.call_count, 1)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.backend.lookups, [])

    def test_identity_cleanup_consumes_only_exact_not_started_without_any_admission(self):
        operation_id = str(uuid.uuid4())
        parent = {'operation_id': operation_id, 'binding': {'parent_scope': {'operation_id': operation_id}}}
        with patch.object(DevelopmentFence, 'attach', return_value=(operation_id, True)), \
                patch.object(DevelopmentFence, 'verify', return_value=None):
            planner, step, receipt = self.failed_test(parent_operation=parent)
        configuration = {'pins': planner.configuration['pins'],
                         'manifest_hash': self.broker.journal.manifest_hash}
        operation = {'id': operation_id, 'scope': self.scope, 'configuration': configuration}
        journal = SimpleNamespace(get=lambda _: operation, lock=lambda _: nullcontext())
        worker = SimpleNamespace(journal=journal, store=self.store, runtime=self.runtime,
            control=self.control, _pin_controller=Mock())
        before = planner.journal.read()
        self.grant['owner_enabled'] = False
        with patch.object(DevelopmentBroker, '__init__', side_effect=AssertionError('no broker construction')), \
                patch.object(self.backend, 'qualified_for', side_effect=AssertionError('no qualification')), \
                patch.object(self.backend, 'run', side_effect=AssertionError('no run')), \
                patch.object(self.backend, 'lookup', side_effect=AssertionError('no lookup')), \
                patch.object(self.policy, 'validate_for', side_effect=AssertionError('no policy admission')):
            result = reconcile_cleanup(worker, operation_id)
        self.assertEqual(result['status'], 'needs_review')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(planner.journal.read(), before)
        self.assertEqual(self.broker.status(step['action_key'])['receipt'], receipt)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(len(self.transports), 1)
        tool = next(row for row in self.control.rows.values() if row['provider'] == 'local_broker')
        self.assertEqual((tool['state'], tool['outcome'], tool['cleanup_confirmed']), ('finished', 'blocked', True))

    def test_cleanup_refuses_pending_or_old_missing_receipt(self):
        planner, step, _ = self.failed_test()
        manifest = self.broker.manifest
        with self.broker.journal.lock() as broker:
            original = deepcopy(broker)
        operation_id = digest(step['action_key'].encode('ascii'))
        for remove_admission in (False, True):
            broker = deepcopy(original)
            operation = broker['operations'][operation_id]
            operation['phase'], operation['receipt'] = 'intent', None
            if remove_admission:
                operation.pop('admission')
            with self.subTest(legacy=remove_admission), self.assertRaises(DevelopmentError) as caught:
                _effect_receipt(step, manifest, broker)
            self.assertEqual(caught.exception.code, 'development_cleanup_receipt_missing')


if __name__ == '__main__':
    unittest.main()
