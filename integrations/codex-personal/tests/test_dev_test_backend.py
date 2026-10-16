"""No subprocess tests for the explicit native test backend controller."""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_broker import TestRequest
from blackoutburst_agents.dev_manifest import DevelopmentError, NamedTest, digest, prepare_manifest
from blackoutburst_agents.dev_test_backend import (NativeNamedPythonBackend, REQUIRED_CHECKS,
    _receipt, backend_id, permission_document, pin_test_plan, prepare_backend)


class Interrupted(BaseException):
    pass


class NativeTestBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'checkout'
        self.root.mkdir(mode=0o700)
        (self.root / 'case.py').write_text("print('synthetic')\n")
        self.cli = self.base / 'fake-codex'
        self.cli.write_bytes(b'synthetic executable, never launched')
        self.cli.chmod(0o700)
        self.python = self.base / 'fake-python'
        self.python.write_bytes(b'synthetic interpreter, never launched')
        self.python.chmod(0o700)
        self.platform = patch('blackoutburst_agents.dev_test_backend._host', return_value={
            'system': 'Darwin', 'release': 'fixture-release', 'machine': 'fixture-machine'})
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.plan = pin_test_plan(root=str(self.root), codex=str(self.cli), cli_version='fixture-cli-1',
            executable=str(self.python), scripts={'unit': {
                'script': 'case.py', 'timeout_seconds': 5, 'max_output_bytes': 64}})
        self.scope = dict(owner_id='owner', agent_id='agent', device_id='device', session_id='session',
                          project_id='project', ticket_id='ticket', profile_version=1)
        self.allowed = True
        self.manifest_dir = self.base / 'manifest'
        prepare_manifest(self.manifest_dir, root=str(self.root), base_sha='a' * 40, scope=self.scope,
            paths=['case.py'], resource='repo:fixture', authorize_scope=self.authorize,
            tests=[NamedTest('unit', backend_id=backend_id(self.plan), timeout_seconds=5, max_output_bytes=64)])
        self.manifest = json.loads((self.manifest_dir / 'manifest.json').read_text())
        self.directory = self.base / 'backend'
        prepare_backend(self.directory, manifest=self.manifest, plan=self.plan)
        self.backend = self.open()
        self.request = TestRequest(digest(b'operation'), digest(json.dumps(self.manifest, sort_keys=True,
            separators=(',', ':')).encode()), str(self.root), tuple(self.plan['root_identity']), 'unit', 5, 64)
        self.suite = self.manifest['tests'][0]

    def authorize(self, scope, action, resource):
        self.assertEqual(scope, self.scope)
        self.assertEqual(action, 'repo.run_test' if self.manifest_dir.exists() else 'repo.read_file')
        self.assertEqual(resource, 'repo:fixture')
        return self.allowed

    def open(self):
        backend = NativeNamedPythonBackend(self.directory, manifest=self.manifest, authorize_scope=self.authorize)
        self.addCleanup(backend.close)
        return backend

    def qualified(self):
        value = {'schema': 1, 'plan_hash': self.backend.document['plan_hash'],
            'checks': {key: True for key in REQUIRED_CHECKS}, 'cleanup_confirmed': True,
            'result': 'qualified_single_process_fixture'}
        self.backend.private.replace('qualification.json', json.dumps(value).encode())
        return value

    def test_no_qualification_refuses_before_any_launch_or_intent(self):
        with patch.object(self.backend, '_execute') as execute:
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.suite))
            with self.assertRaises(DevelopmentError):
                self.backend.run(self.request)
            execute.assert_not_called()
        self.assertFalse((self.directory / (self.request.operation_id + '.json')).exists())

    def test_qualification_must_include_every_exact_check_and_cleanup(self):
        value = self.qualified()
        self.assertTrue(self.backend.qualified_for(self.request.manifest_hash, self.suite))
        for key in REQUIRED_CHECKS:
            with self.subTest(key=key):
                broken = json.loads(json.dumps(value))
                broken['checks'][key] = False
                self.backend.private.replace('qualification.json', json.dumps(broken).encode())
                self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.suite))
        for changed in ({**value, 'cleanup_confirmed': False}, {**value, 'schema': True},
                        {**value, 'plan_hash': '0' * 64}, {**value, 'checks': {key: 1 for key in REQUIRED_CHECKS}}):
            self.backend.private.replace('qualification.json', json.dumps(changed).encode())
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.suite))

    def test_named_execution_is_intended_before_effect_and_idempotent_after_restart(self):
        self.qualified()
        result = _receipt('passed', cleanup=True, output=b'fixture result')
        def effect(request, **kwargs):
            state = json.loads((self.directory / (request.operation_id + '.json')).read_text())
            self.assertEqual(state['phase'], 'intent')
            return result, {key: True for key in REQUIRED_CHECKS}, 'completed'
        with patch.object(self.backend, '_execute', side_effect=effect) as execute:
            self.assertEqual(self.backend.run(self.request), result)
            self.assertEqual(self.backend.run(self.request), result)
            self.assertEqual(execute.call_count, 1)
        reopened = self.open()
        with patch.object(reopened, '_execute') as execute:
            self.assertEqual(reopened.lookup(self.request), result)
            self.assertEqual(reopened.run(self.request), result)
            execute.assert_not_called()
        raw = (self.directory / (self.request.operation_id + '.json')).read_text()
        self.assertNotIn('fixture result', raw)

    def test_interruption_never_reexecutes_uncertain_intent(self):
        self.qualified()
        with patch.object(self.backend, '_execute', side_effect=Interrupted), self.assertRaises(Interrupted):
            self.backend.run(self.request)
        reopened = self.open()
        with patch.object(reopened, '_execute') as execute:
            self.assertEqual(reopened.lookup(self.request), _receipt())
            self.assertEqual(reopened.run(self.request), _receipt())
            execute.assert_not_called()
            with self.assertRaises(DevelopmentError):
                reopened.run(replace(self.request, operation_id=digest(b'another-operation')))

    def test_unconfirmed_cleanup_blocks_other_operations(self):
        self.qualified()
        with patch.object(self.backend, '_execute', return_value=(_receipt(), {}, 'native_launch_uncertain')):
            self.backend.run(self.request)
        with patch.object(self.backend, '_execute') as execute, self.assertRaises(DevelopmentError):
            self.backend.run(replace(self.request, operation_id=digest(b'next-operation')))
        execute.assert_not_called()

    def test_backend_also_enforces_durable_manifest_test_budget(self):
        self.qualified()
        with patch.object(self.backend, '_execute', return_value=(_receipt('passed', cleanup=True), {}, 'completed')) as execute:
            for index in range(self.manifest['budgets']['max_tests']):
                self.backend.run(replace(self.request, operation_id=digest(str(index).encode())))
            self.assertEqual(execute.call_count, self.manifest['budgets']['max_tests'])
            with self.assertRaises(DevelopmentError):
                self.backend.run(replace(self.request, operation_id=digest(b'exhausted')))
        reopened = self.open()
        with patch.object(reopened, '_execute') as execute, self.assertRaises(DevelopmentError):
            reopened.run(replace(self.request, operation_id=digest(b'after-restart')))
        execute.assert_not_called()

    def test_unknown_operation_lookup_has_no_effect(self):
        with patch.object(self.backend, '_execute') as execute:
            self.assertIsNone(self.backend.lookup(self.request))
            execute.assert_not_called()

    def test_shared_backend_and_other_openers_cannot_overlap_effects(self):
        with self.backend.locked():
            for instance in (self.backend, self.open()):
                with self.assertRaises(DevelopmentError):
                    instance.lookup(self.request)

    def test_revocation_prevents_qualification_execution_and_cached_recovery(self):
        self.qualified()
        self.allowed = False
        with patch.object(self.backend, '_execute') as execute:
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.suite))
            for method, args in ((self.backend.qualify, ()), (self.backend.run, (self.request,)),
                                 (self.backend.lookup, (self.request,))):
                with self.assertRaises(DevelopmentError):
                    method(*args)
            execute.assert_not_called()

    def test_scope_and_budgets_cannot_be_changed_by_test_request(self):
        self.qualified()
        changes = [dict(name='shell'), dict(root='/unapproved'), dict(manifest_hash='0' * 64),
            dict(root_identity=(0, 0)), dict(timeout_seconds=6), dict(max_output_bytes=65),
            dict(timeout_seconds=True), dict(operation_id='../path')]
        with patch.object(self.backend, '_execute') as execute:
            for change in changes:
                with self.subTest(change=change), self.assertRaises(DevelopmentError):
                    self.backend.run(replace(self.request, **change))
            execute.assert_not_called()

    def test_same_operation_with_another_request_is_refused(self):
        self.qualified()
        with patch.object(self.backend, '_execute', return_value=(_receipt('passed', cleanup=True), {}, 'completed')):
            self.backend.run(self.request)
        state_file = self.directory / (self.request.operation_id + '.json')
        state = json.loads(state_file.read_text())
        state['request_hash'] = '0' * 64
        state_file.write_text(json.dumps(state))
        with self.assertRaises(DevelopmentError):
            self.backend.lookup(self.request)

    def test_binary_or_policy_changes_invalidate_qualification(self):
        self.qualified()
        self.cli.write_bytes(b'changed binary')
        self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.suite))
        with patch.object(self.backend, '_execute') as execute, self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        execute.assert_not_called()

    def test_private_plan_modification_and_lock_swap_are_refused(self):
        value = json.loads((self.directory / 'plan.json').read_text())
        value['plan']['environment']['SECRET'] = 'synthetic only'
        (self.directory / 'plan.json').write_text(json.dumps(value))
        with self.assertRaises(DevelopmentError):
            self.backend.lookup(self.request)
        self.backend.close()
        lock = self.directory / 'backend.lock'
        lock.rename(self.base / 'old-lock')
        lock.symlink_to(self.base / 'old-lock')
        with self.assertRaises(DevelopmentError):
            self.open()

    def test_controller_policy_has_exact_root_deny_and_no_network(self):
        policy = permission_document(str(self.root))
        self.assertEqual(policy['filesystem'], {':root': 'deny', ':minimal': 'read', ':tmpdir': 'deny',
            ':slash_tmp': 'deny', str(self.root): 'write'})
        self.assertEqual(policy['network'], {'enabled': False})
        command = self.backend._command({'synthetic': 'fixture'})
        self.assertIn('-P', command)
        self.assertNotIn('danger-full-access', command)
        self.assertEqual(self.plan['environment'], {'LANG': 'C', 'PATH': '/usr/bin:/bin'})

    def test_unsupported_host_is_not_qualified(self):
        self.qualified()
        with patch('blackoutburst_agents.dev_test_backend._host', return_value={
                'system': 'Linux', 'release': 'fixture', 'machine': 'fixture'}):
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.suite))
            with self.assertRaises(DevelopmentError):
                pin_test_plan(root=str(self.root), codex=str(self.cli), cli_version='fixture', scripts={})


if __name__ == '__main__':
    unittest.main()
