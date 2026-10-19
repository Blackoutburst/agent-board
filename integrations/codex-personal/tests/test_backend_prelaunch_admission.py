"""Owned admission boundaries with filesystem/model/control doubles only."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_broker import DevelopmentBroker, validate_backend_not_started
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_test_backend import _receipt
from blackoutburst_agents.dev_test_supervisor import supervise_packet
import test_dev_test_backend as native_fixture
import test_dev_test_supervisor as supervisor_fixture
import test_dev_planner as planner_fixture


class Crash(BaseException):
    pass


class BackendPrelaunchAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.NativeTestBackendTests('runTest')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.backend = self.fixture.backend
        self.request = self.fixture.request
        self.fixture.qualified()
        self.broker = DevelopmentBroker(self.fixture.manifest_dir, enabled=True,
            authorize_scope=self.fixture.authorize, test_backend=self.backend)
        self.addCleanup(self.broker.close)

    def document(self, operation_id=None):
        return json.loads((self.fixture.directory / ((operation_id or self.request.operation_id) + '.json')).read_text())

    def refusal(self, error=None):
        with patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_authorize', side_effect=error or DevelopmentError('permission_revoked')), \
                patch('subprocess.Popen', side_effect=AssertionError('no launch')):
            with self.assertRaises(DevelopmentError):
                self.backend.run(self.request)
        return self.backend.lookup_not_started(self.request)

    def test_admission_precedes_first_fallible_callback(self):
        def deny():
            value = self.document()
            self.assertEqual((value['schema'], value['phase'], value['admission']['launch']), (2, 'intent', 'pending'))
            raise DevelopmentError('permission_revoked')
        with patch.object(self.backend, '_authorize', side_effect=deny), \
                patch.object(self.backend, '_execute', side_effect=AssertionError('no test')):
            with self.assertRaises(DevelopmentError):
                self.backend.run(self.request)
        proof = self.backend.lookup_not_started(self.request)
        self.assertIs(proof['started'], False)
        self.assertIs(proof['cleanup_confirmed'], True)
        self.assertEqual(proof['reason'], 'permission_revoked')

    def test_post_broker_dispatch_refusal_has_exact_durable_proof(self):
        with patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_authorize', side_effect=DevelopmentError('permission_revoked')), \
                patch.object(self.backend, '_execute', side_effect=AssertionError('no native')):
            with self.assertRaises(DevelopmentError) as caught:
                self.broker.run_test('new-test', 'unit')
        self.assertEqual(caught.exception.code, 'permission_revoked')
        with self.broker.journal.lock() as state:
            operation = deepcopy(state['operations'][digest(b'new-test')])
        self.assertEqual(operation['admission']['dispatch'], 'dispatched')
        self.assertEqual(operation['receipt']['result'], 'not_started')
        self.assertIs(operation['receipt']['cleanup_confirmed'], True)
        self.assertIs(operation['receipt']['started'], False)
        self.assertIn('backend_admission', operation['receipt'])
        before = self.document(operation['id'])
        with patch.object(self.backend, 'run', side_effect=AssertionError('no replay')), \
                patch.object(self.backend, 'lookup', side_effect=AssertionError('no generic lookup')):
            self.assertEqual(self.broker.reconcile('new-test'), operation['receipt'])
            with self.assertRaises(DevelopmentError):
                self.broker.run_test('new-test', 'unit')
        self.assertEqual(self.document(operation['id']), before)

    def test_crash_pending_is_never_promoted_after_reopen(self):
        with patch.object(self.backend, '_authorize', side_effect=Crash), self.assertRaises(Crash):
            self.backend.run(self.request)
        before = self.document()
        self.assertEqual(before['admission']['launch'], 'pending')
        reopened = self.fixture.open()
        with patch.object(reopened, '_execute', side_effect=AssertionError('no replay')):
            self.assertEqual(reopened.run(self.request), _receipt())
            self.assertIsNone(reopened.lookup_not_started(self.request))
        self.assertEqual(self.document(), before)

    def test_lost_broker_receipt_is_reconciled_from_exact_terminal_backend_only(self):
        with patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_authorize', side_effect=DevelopmentError('permission_revoked')), \
                patch.object(self.broker, '_finish_backend_not_started', side_effect=Crash), self.assertRaises(Crash):
            self.broker.run_test('lost-terminal', 'unit')
        with self.broker.journal.lock() as state:
            self.assertIsNone(state['operations'][digest(b'lost-terminal')]['receipt'])
        with patch.object(self.backend, 'qualified_for', side_effect=AssertionError('no qualification')), \
                patch.object(self.backend, 'run', side_effect=AssertionError('no execution')), \
                patch.object(self.backend, 'lookup', side_effect=AssertionError('no generic lookup')):
            receipt = self.broker.reconcile('lost-terminal')
        self.assertEqual(receipt['result'], 'not_started')
        self.assertIs(receipt['cleanup_confirmed'], True)
        self.assertIn('backend_admission', receipt)

    def test_missing_or_corrupt_observation_cannot_release_broker_capacity(self):
        with patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_execute', side_effect=Crash), self.assertRaises(Crash):
            self.broker.run_test('crashed-native', 'unit')
        with patch.object(self.backend, 'lookup_not_started', return_value={'schema': True}), \
                patch.object(self.backend, 'run', side_effect=AssertionError('no retry')):
            result = self.broker.reconcile('crashed-native')
        self.assertEqual(result['result'], 'needs_review')
        self.assertIsNot(result.get('cleanup_confirmed'), True)

    def test_legacy_intent_without_admission_stays_unknown(self):
        self.backend.private.create(self.request.operation_id + '.json', canonical({
            'schema': 1, 'request_hash': self.backend._request(self.request),
            'phase': 'intent', 'result': None, 'reason': None}))
        with patch.object(self.backend, '_authorize', side_effect=DevelopmentError('permission_revoked')), \
                self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        self.assertIsNone(self.backend.lookup_not_started(self.request))
        self.assertNotIn('admission', self.document())

    def test_entered_launch_or_popen_failure_is_never_nonlaunch_proof(self):
        def ambiguous(*args, **kwargs):
            self.backend._mark_launch()
            raise DevelopmentError('permission_revoked')
        with patch.object(self.backend, '_execute', side_effect=ambiguous), self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        self.assertEqual(self.document()['admission']['launch'], 'entered')
        self.assertIsNone(self.backend.lookup_not_started(self.request))
        with patch.object(self.backend, '_execute', side_effect=AssertionError('no retry')):
            self.assertEqual(self.backend.run(self.request), _receipt())

    def test_uncertain_marker_write_cannot_be_recast_as_nonlaunch(self):
        original = self.backend.private.replace
        def uncertain(name, raw):
            original(name, raw)
            if name == self.request.operation_id + '.json':
                raise DevelopmentError('private_write_failed')
        def launch_boundary(*args, **kwargs):
            self.backend._mark_launch()
            raise AssertionError('marker must refuse')
        with patch.object(self.backend.private, 'replace', side_effect=uncertain), \
                patch.object(self.backend, '_execute', side_effect=launch_boundary), self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        self.assertEqual(self.document()['admission']['launch'], 'entered')
        self.assertIsNone(self.backend.lookup_not_started(self.request))

    def test_failed_terminal_write_retains_unknown(self):
        with patch.object(self.backend.private, 'replace', side_effect=DevelopmentError('private_write_failed')), \
                patch.object(self.backend, '_authorize', side_effect=DevelopmentError('permission_revoked')), \
                self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        self.assertEqual(self.document()['phase'], 'intent')
        self.assertIsNone(self.backend.lookup_not_started(self.request))

    def test_terminal_observation_is_exact_and_never_calls_authority(self):
        proof = self.refusal()
        with patch.object(self.backend, '_authorize', side_effect=AssertionError('no RPC')), \
                patch.object(self.backend, '_execute', side_effect=AssertionError('no execution')):
            self.assertEqual(self.backend.lookup_not_started(self.request), proof)
            self.assertIsNone(self.backend.lookup_not_started(replace(self.request, operation_id=digest(b'other'))))
            with self.assertRaises(DevelopmentError):
                self.backend.lookup_not_started(replace(self.request, timeout_seconds=self.request.timeout_seconds + 1))
        changed = {**proof, 'request_hash': '0' * 64}
        with self.assertRaises(DevelopmentError):
            validate_backend_not_started(self.request, self.backend.backend_id, changed)

    def test_diagnostics_never_persist_exception_text_or_unknown_code(self):
        sensitive = 'do_not_retain_private_diagnostic_value'
        error = DevelopmentError(sensitive)
        proof = self.refusal(error)
        self.assertEqual(proof['reason'], 'native_prelaunch_failed')
        self.assertNotIn(sensitive, json.dumps(self.document()))

    def test_strict_terminal_schema_rejects_types_extra_keys_and_stale_pins(self):
        self.refusal(); original = self.document()
        changes = [lambda d: d.update(schema=True), lambda d: d.update(extra=True),
            lambda d: d['admission'].update(schema=1.0),
            lambda d: d['admission'].update(operation_id=True),
            lambda d: d['admission'].update(nonce='wrong'),
            lambda d: d['admission'].update(controller_sha256='0' * 64),
            lambda d: d['admission'].update(plan_hash='0' * 64),
            lambda d: d['admission'].update(launch='entered'),
            lambda d: d['result'].update(output_bytes=False),
            lambda d: d['result'].update(cleanup_confirmed=1),
            lambda d: d.update(reason='a_private_unregistered_reason')]
        for change in changes:
            value = deepcopy(original); change(value)
            self.backend.private.replace(self.request.operation_id + '.json', canonical(value))
            with self.subTest(change=change), self.assertRaises(DevelopmentError):
                self.backend.lookup_not_started(self.request)
        self.backend.private.replace(self.request.operation_id + '.json', canonical(original))
        proof = self.backend.lookup_not_started(self.request)
        for key, value in [('schema', True), ('started', 0), ('cleanup_confirmed', 1), ('extra', True)]:
            with self.subTest(key=key), self.assertRaises(DevelopmentError):
                validate_backend_not_started(self.request, self.backend.backend_id, {**proof, key: value})


class SupervisorPrelaunchAdmissionTests(unittest.TestCase):
    setUp = supervisor_fixture.SupervisorTests.setUp
    qualify = supervisor_fixture.SupervisorTests.qualify

    def test_each_prelaunch_authorization_refusal_is_durable_without_helper(self):
        self.qualify()
        for denied in (1, 2, 3):
            request = replace(self.request, operation_id=digest(('denied-' + str(denied)).encode()))
            calls = []
            def authorize():
                calls.append(True)
                if len(calls) == denied:
                    raise DevelopmentError('permission_revoked')
            with patch.object(self.backend, 'qualified_for', return_value=True), \
                    patch.object(self.backend, '_authorize', side_effect=authorize), \
                    patch.object(self.backend, '_launch', side_effect=AssertionError('no helper')), \
                    self.assertRaises(DevelopmentError):
                self.backend.run(request)
            proof = self.backend.lookup_not_started(request)
            self.assertEqual(proof['reason'], 'permission_revoked')
            self.assertEqual(len(calls), denied)
            if denied == 3:
                packet = self.fixture.directory / ('supervision-' + request.operation_id + '.job.json')
                self.assertFalse(packet.exists())  # Initial fresh collection precedes packet creation.

    def test_popen_entry_is_marked_before_an_uncertain_launch_error(self):
        self.qualify()
        with patch('subprocess.Popen', side_effect=OSError('synthetic launch uncertainty')), self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        value = json.loads((self.fixture.directory / (self.request.operation_id + '.json')).read_text())
        self.assertEqual(value['admission']['launch'], 'entered')
        self.assertIsNone(self.backend.lookup_not_started(self.request))


class PlannerBackendNonlaunchTests(unittest.TestCase):
    setUp = planner_fixture.PlannerTests.setUp
    create = planner_fixture.PlannerTests.create
    transport = planner_fixture.PlannerTests.transport
    action = planner_fixture.PlannerTests.action

    def composed_callback(self, *, identical):
        from unittest.mock import Mock
        base = Mock(wraps=self.broker.authorize_scope)
        self.broker.authorize_scope = base
        other = base if identical else Mock(wraps=base)
        self.backend.authorize_scope = other
        observed = []
        original = self.backend.run
        def run(request):
            before = base.call_count
            allowed = self.backend.authorize_scope(self.broker.manifest['scope'],
                'repo.run_test', self.broker.manifest['resource'])
            observed.append((allowed, base.call_count - before))
            return original(request)
        self.backend.run = run
        self.actions = [self.action('test', name='unit'), self.action('final', summary='fixture result')]
        planner = self.create()
        result = planner.run()
        self.assertEqual(result['status'], 'review_ready')
        self.assertEqual(observed, [(True, 1 if identical else 2)])
        self.assertIs(self.backend.authorize_scope, other)

    def test_same_callback_object_is_checked_once_by_the_combined_boundary(self):
        self.composed_callback(identical=True)

    def test_distinct_backend_callback_is_preserved_alongside_base_callback(self):
        self.composed_callback(identical=False)

    def test_exact_backend_proof_finishes_runtime_blocked_clean_without_acceptance(self):
        self.actions = [self.action('test', name='unit')]
        planner = self.create()
        observed = {}
        def refuse(request):
            observed['request'] = request
            raise DevelopmentError('permission_revoked')
        def proof(request):
            self.assertEqual(request, observed['request'])
            return {'schema': 1, 'operation_id': request.operation_id,
                'request_hash': digest(canonical(vars(request))), 'manifest_hash': request.manifest_hash,
                'backend_id': self.backend.backend_id, 'plan_hash': 'a' * 64,
                'controller_sha256': 'b' * 64, 'nonce': 'c' * 32,
                'started': False, 'cleanup_confirmed': True, 'reason': 'permission_revoked',
                'receipt_sha256': 'd' * 64}
        self.backend.run = refuse
        self.backend.lookup_not_started = proof
        result = planner.run()
        self.assertEqual((result['status'], result['reason']), ('needs_review', 'permission_revoked'))
        state = planner.journal.read()
        self.assertEqual(state['tests'], {})
        self.assertEqual(state['history'], [])
        self.assertTrue(state['step']['tool_finished'])
        self.assertEqual(state['step']['receipt']['receipt']['result'], 'not_started')
        tool = next(row for row in self.control.rows.values() if row['provider'] == 'local_broker')
        self.assertEqual((tool['state'], tool['outcome'], tool['cleanup_confirmed']), ('finished', 'blocked', True))
        before = deepcopy(state)
        self.assertEqual(planner.run(), result)
        self.assertEqual(planner.journal.read(), before)
        self.assertEqual(len(self.transports), 1)
