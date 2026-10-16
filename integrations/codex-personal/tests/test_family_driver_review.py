"""Independent ordinary cache/path regressions; all execution uses fixture models."""
from copy import deepcopy
import json
import contextlib
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

import test_family_audit as audit_fixture
import test_family_journal as journal_fixture
import test_family_worker as worker_fixture
from blackoutburst_agents import family_cli
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import canonical
from blackoutburst_agents.family_audit import audit_workflow
from blackoutburst_agents.family_journal import FamilyJournal, SLOTS
from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.dev_planner import _directory_pin
from blackoutburst_agents.dev_runtime import development_work_id
from blackoutburst_agents.runtime_journal import RuntimeJournal


class FamilyJournalReviewTests(unittest.TestCase):
    def setUp(self):
        journal_fixture.FamilyJournalTests.setUp(self)
        self.config.setdefault('provisional_scopes', {
            slot: {**self.config['scope'], 'board_session_id': self.config['scope']['board_session_id'] + '-' + slot}
            if slot != 'parent' else dict(self.config['scope']) for slot in SLOTS
        })
        if 'pins' not in self.config:
            self.config['pins'] = {}
            for slot in SLOTS:
                self.config['pins'][slot] = {}
                for kind in ('journal', 'broker_directory'):
                    path = self.base / ('review-' + slot + '-' + kind)
                    path.mkdir(mode=0o700)
                    self.config['pins'][slot][kind] = _directory_pin(path)

    def test_missing_stop_does_not_hide_changed_journal_directory(self):
        self.journal.prepare(self.config)
        path = self.journal.directory
        path.rename(self.base / 'old-family')
        path.mkdir(mode=0o700)
        with self.assertRaises(DevelopmentError):
            self.journal.stopped()

    def test_family_cannot_reserve_checkout_already_bound_to_ordinary_work(self):
        anchor = self.base / 'global-anchor'
        directory = self.base / 'ordinary-journal'
        directory.mkdir(mode=0o700)
        binding = {
            'scope': self.config['scope'],
            'root_identity': self.config['roots']['child_0']['identity'],
            'journal': _directory_pin(directory), 'configuration_hash': 'a' * 64,
            'child_scope': {'work_id': str(uuid.uuid4()), **self.config['child_stores']['child_0']},
        }
        fence = DevelopmentFence(_test_anchor=anchor)
        self.addCleanup(fence.close)
        operation = str(uuid.uuid4())
        fence.bind(binding, operation)
        family = FamilyJournal(self.base / 'reserved-family', _test_anchor=anchor)
        self.addCleanup(family.close)
        with self.assertRaises(DevelopmentError):
            family.prepare(self.config)
        self.assertEqual(fence.inspect_checkout(binding['root_identity'])['operation_id'], operation)


class FamilyAuditReviewTests(unittest.TestCase):
    setUp = audit_fixture.FamilyAuditTests.setUp
    backend_factory = audit_fixture.FamilyAuditTests.backend_factory
    make_worker = audit_fixture.FamilyAuditTests.make_worker
    prepare = audit_fixture.FamilyAuditTests.prepare
    transport = audit_fixture.FamilyAuditTests.transport
    action = audit_fixture.FamilyAuditTests.action
    successful_actions = audit_fixture.FamilyAuditTests.successful_actions
    ready = audit_fixture.FamilyAuditTests.ready

    def test_ordinary_worker_refuses_checkout_already_reserved_by_family(self):
        roots = {'child_0': _directory_pin(self.root)}
        stores = {'child_0': {'runs': _directory_pin(self.store.state_dir),
                             'runtime': _directory_pin(self.runtime.directory)}}
        for slot in ('child_1', 'parent'):
            root = self.base / slot
            root.mkdir(mode=0o700)
            roots[slot] = _directory_pin(root)
            stores[slot] = {}
            for kind in ('runs', 'runtime'):
                path = self.base / (slot + '-' + kind)
                path.mkdir(mode=0o700)
                stores[slot][kind] = _directory_pin(path)
        config = {'scope': self.scope, 'roots': roots, 'child_stores': stores,
                  'provisional_scopes': {
                      slot: {**self.scope, 'board_session_id': self.scope['board_session_id'] + '-' + slot}
                      if slot != 'parent' else dict(self.scope) for slot in SLOTS},
                  'pins': {'child_0': {'journal': _directory_pin(self.journal.directory),
                                      'broker_directory': _directory_pin(self.broker_dir)}}}
        for slot in ('child_1', 'parent'):
            config['pins'][slot] = {}
            for kind in ('journal', 'broker_directory'):
                path = self.base / (slot + '-' + kind)
                path.mkdir(mode=0o700)
                config['pins'][slot][kind] = _directory_pin(path)
        with FamilyJournal(self.base / 'family', _test_anchor=self.base / 'anchor') as journal:
            journal.prepare(config)
            with self.assertRaises(DevelopmentError):
                self.prepare()
        self.assertFalse(self.inferences)
        self.assertFalse(self.backend.calls)
        self.assertFalse(self.control.calls)

    def test_finished_tool_receipt_must_match_exact_runtime_configuration(self):
        operation, manifest = self.ready()
        record = next(self.runtime.get(row['id']) for row in self.runtime.list_status()
                      if self.runtime.get(row['id'])['configuration']['provider'] == 'local_broker')
        original = deepcopy(record['receipt'])
        replacements = {
            'owner_id': 'other-owner', 'agent_id': 'other-agent',
            'project_id': 'other-project', 'ticket_id': 'other-ticket',
            'session_id': 'other-session',
            'work_id': 'a645ae9c-120a-4c40-abd0-3d0958377e8c',
            'turn_id': '35eec362-f760-4664-bacb-693154c634a5',
            'provider': 'codex_chatgpt', 'model': 'different-action',
        }
        try:
            for field, value in replacements.items():
                with self.subTest(field=field):
                    receipt = {**original, field: value}
                    with self.runtime.transaction():
                        self.runtime.db.execute('UPDATE executions SET receipt=? WHERE id=?',
                            (json.dumps(receipt), record['id']))
                    with self.assertRaises(DevelopmentError):
                        audit_workflow(self.worker, operation, manifest_hash=manifest, authorize=lambda: True)
        finally:
            with self.runtime.transaction():
                self.runtime.db.execute('UPDATE executions SET receipt=? WHERE id=?',
                    (json.dumps(original), record['id']))

    def test_cached_test_request_must_still_match_pinned_manifest(self):
        operation, manifest = self.ready()
        with PrivateDirectory(self.broker_dir) as private:
            state = decode(private.read('state.json', 2 * 1024 * 1024))
            test = next(entry for entry in state['operations'].values() if entry['kind'] == 'test')
            test['detail']['backend_id'] = 'other-qualified-backend'
            private.replace('state.json', canonical(state))
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker, operation, manifest_hash=manifest, authorize=lambda: True)
    def test_steps_must_reference_the_exact_verified_model_turns(self):
        operation, manifest = self.ready()
        with PrivateDirectory(self.broker_dir) as private:
            original = decode(private.read('planner.json', 2 * 1024 * 1024))
            try:
                for slot in ('history', 'final'):
                    with self.subTest(slot=slot):
                        state = deepcopy(original)
                        selected = state['history'][0] if slot == 'history' else state['step']
                        selected['model_turn_id'] = str(uuid.uuid4())
                        private.replace('planner.json', canonical(state))
                        with self.assertRaises(DevelopmentError):
                            audit_workflow(self.worker, operation, manifest_hash=manifest, authorize=lambda: True)
            finally:
                private.replace('planner.json', canonical(original))

    def test_cancelled_runtime_receipt_cannot_attest_a_completed_review_workflow(self):
        operation, manifest = self.ready()
        record = next(self.runtime.get(row['id']) for row in self.runtime.list_status()
                      if self.runtime.get(row['id'])['configuration']['provider'] == 'codex_chatgpt')
        receipt = {**record['receipt'], 'outcome': 'cancelled', 'reason': 'cancelled'}
        with self.runtime.transaction():
            self.runtime.db.execute('UPDATE executions SET receipt=? WHERE id=?',
                (json.dumps(receipt), record['id']))
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker, operation, manifest_hash=manifest, authorize=lambda: True)

    def test_identity_cleanup_requires_real_exact_broker_receipt_for_test(self):
        class InterruptedController(BaseException):
            pass
        self.successful_actions()
        prepared = self.prepare()
        with patch.object(self.backend, 'run', side_effect=InterruptedController):
            with self.assertRaises(InterruptedController):
                self.worker.run(prepared['operation_id'])
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 1)
        with PrivateDirectory(self.broker_dir) as private:
            planner = decode(private.read('planner.json', 2 * 1024 * 1024))
            self.assertEqual(planner['step']['action']['kind'], 'test')
            planner['step']['receipt'] = {'receipt': {'status': 'complete', 'cleanup_confirmed': True}}
            private.replace('planner.json', canonical(planner))
        refused = False
        try:
            self.worker.reconcile_cleanup(prepared['operation_id'])
        except DevelopmentError:
            refused = True
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 1)
        self.assertTrue(refused, 'An unverified test receipt must remain in review.')

    def test_audit_refuses_unreferenced_runtime_of_the_same_work(self):
        operation, manifest = self.ready()
        configuration = RuntimeJournal.configuration(self.scope, development_work_id(operation),
            str(uuid.uuid4()), 'unit', provider='local_broker')
        self.runtime.begin(configuration)
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker, operation, manifest_hash=manifest, authorize=lambda: True)


class FamilyWorkerReviewTests(unittest.TestCase):
    setUp = worker_fixture.FamilyWorkerTests.setUp
    make_manifest = worker_fixture.FamilyWorkerTests.make_manifest
    ready = worker_fixture.FamilyWorkerTests.ready
    transport = worker_fixture.FamilyWorkerTests.transport
    ticket = worker_fixture.FamilyWorkerTests.ticket

    def test_stop_opened_family_propagates_server_stop_without_releasing_claim(self):
        self.worker.prepare(self.request)
        self.worker.open()
        state = self.journal.read()
        before = self.ticket()
        result = self.worker.stop()
        family = self.workflows['parent'].client.call('get_delegation_family',
            {'family_id': state['payload']['family_id']})
        self.assertTrue(result['stop_requested'])
        self.assertEqual(family['state'], 'cancelled')
        self.assertEqual(self.ticket()['claimant'], before['claimant'])
        self.assertFalse(self.inferences)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 0)

    def test_stop_bound_family_propagates_server_stop_before_any_model(self):
        self.ready()
        state = self.journal.read()
        result = self.worker.stop()
        family = self.workflows['parent'].client.call('get_delegation_family',
            {'family_id': state['payload']['family_id']})
        self.assertTrue(result['stop_requested'])
        self.assertEqual(family['state'], 'cancelled')
        self.assertFalse(self.inferences)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 0)

    def test_cli_rejects_private_store_inside_checkout_before_creating_files(self):
        configuration = self.base / 'cli-config'
        configuration.mkdir(mode=0o700)
        forbidden = self.roots['parent'] / 'unexpected-run-store'
        stores = {slot: {'journal': str(worker.journal.directory), 'runs': str(worker.store.state_dir),
                         'runtime': str(worker.runtime.directory), 'registration': self.registrations[slot]}
                  for slot, worker in self.workflows.items()}
        stores['child_0']['runs'] = str(forbidden)
        specification = {'schema': 1, 'request': self.request, 'workflows': stores}
        plan = configuration / 'plan.json'
        plan.write_bytes(canonical(specification))
        plan.chmod(0o600)
        connection = configuration / 'connection.json'
        connection.write_bytes(canonical({'mcp_url': self.scope['board_endpoint'], 'token': 'synthetic',
                                         'client': 'codex', 'identity_key': 'fixture', 'name': 'Fixture'}))
        connection.chmod(0o600)
        args = SimpleNamespace(journal=self.base / 'cli-family', family_command='prepare',
                               config_file=plan, connection_file=connection)
        def journal(path):
            return FamilyJournal(path, _test_anchor=self.base / 'cli-anchor')
        with patch.object(family_cli, 'FamilyJournal', side_effect=journal), \
             patch.object(family_cli.FamilyBoardClient, 'from_connection_file') as connection_factory, \
             patch.object(family_cli.family_journal, 'canonical_anchor', return_value=self.base / 'cli-anchor'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(family_cli.run(args), 2)
        connection_factory.assert_not_called()
        self.assertFalse(forbidden.exists(), 'Invalid private configuration must not create a RunStore inside a checkout.')

    def test_cli_resume_validates_private_and_root_pins_before_creating_files(self):
        self.ready()
        runtime_path = self.workflows['child_0'].runtime.directory
        runtime_path.rename(self.base / 'previous-child-runtime')
        self.roots['child_0'].rename(runtime_path)
        forbidden = runtime_path / 'runtime.sqlite3'
        connection = self.base / 'connection.json'
        connection.write_bytes(canonical({'synthetic': True}))
        connection.chmod(0o600)
        args = SimpleNamespace(journal=self.journal.directory, family_command='cleanup',
                               connection_file=connection)
        def journal(path):
            return FamilyJournal(path, _test_anchor=self.base / 'anchor')
        with patch.object(family_cli, 'FamilyJournal', side_effect=journal), \
             patch.object(family_cli.FamilyBoardClient, 'from_connection_file') as connection_factory, \
             patch.object(family_cli.family_journal, 'canonical_anchor', return_value=self.base / 'anchor'), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(family_cli.run(args), 2)
        connection_factory.assert_not_called()
        self.assertFalse(forbidden.exists(), 'Resume must not initialize a database in the moved checkout.')

    def test_accept_retry_reaudits_changed_candidate_before_remote_effect(self):
        self.ready()
        client = self.workflows['parent'].client
        original = client.call
        def crash(name, arguments):
            if name == 'accept_delegation_result':
                raise worker_fixture.Crash()
            return original(name, arguments)
        client.call = crash
        with self.assertRaises(worker_fixture.Crash):
            self.worker.run()
        client.call = original
        (self.roots['child_0'] / 'left.py').write_text('value = 9\n')
        before = len(client.calls)
        result = self.worker.run()
        self.assertEqual(result['reason'], 'family_source_snapshot_changed', result)
        self.assertFalse(any(name == 'accept_delegation_result' for name, _ in client.calls[before:]))
        self.assertEqual([slot for slot, _ in self.inferences], ['child_0'] * 3)

    def test_finalize_retry_reaudits_changed_parent_before_remote_effect(self):
        self.ready()
        client = self.workflows['parent'].client
        original = client.call
        def crash(name, arguments):
            if name == 'finalize_delegation_family':
                raise worker_fixture.Crash()
            return original(name, arguments)
        client.call = crash
        with self.assertRaises(worker_fixture.Crash):
            self.worker.run()
        client.call = original
        (self.roots['parent'] / 'left.py').write_text('value = 9\n')
        before = len(client.calls)
        count = len(self.inferences)
        result = self.worker.run()
        self.assertEqual(result['reason'], 'family_source_snapshot_changed', result)
        self.assertEqual(len(self.inferences), count)
        self.assertFalse(any(name == 'finalize_delegation_family' for name, _ in client.calls[before:]))

    def test_stop_and_status_remain_available_after_source_compatibility_changes(self):
        self.ready()
        with patch('blackoutburst_agents.family_worker.controller_sources',
                   return_value={'changed': True}) as source_check:
            self.assertEqual(self.worker.status()['status'], 'bound')
            self.assertEqual(self.worker.stop()['server_stop'], 'confirmed')
            self.assertEqual(self.worker.cleanup()['cleanup'], {})
        source_check.assert_not_called()
        self.assertFalse(self.inferences)


if __name__ == '__main__':
    unittest.main()
