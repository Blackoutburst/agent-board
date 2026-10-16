"""Independent script-integrity regressions; fake launch, no model or native test."""
from copy import deepcopy
import base64
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import blackoutburst_agents.dev_test_backend as native
from blackoutburst_agents.dev_broker import DevelopmentBroker, TestRequest as BackendRequest
from blackoutburst_agents.dev_manifest import (DevelopmentError, NamedTest, canonical,
    digest, prepare_manifest)
from blackoutburst_agents.dev_planner import parse_action
from test_dev_test_backend_review import _ExitedCLI, _Listener
import test_dev_test_supervisor as supervisor_fixture


class ScriptPinsReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / 'checkout'
        self.root.mkdir(mode=0o700)
        self.script = self.root / 'test_case.py'
        self.original = b'VALUE = 1\n'
        self.script.write_bytes(self.original)
        (self.root / 'main.json').write_text('{"value":1}\n')
        self.cli = self.base / 'fake-codex'
        self.executable = self.base / 'fake-python'
        for path in (self.cli, self.executable):
            path.write_bytes(b'Synthetic binary; never executed.')
            path.chmod(0o700)
        platform = patch.object(native, '_host', return_value={
            'system': 'Darwin', 'release': 'fixture', 'machine': 'fixture'})
        platform.start()
        self.addCleanup(platform.stop)
        self.scope = dict(owner_id='owner', agent_id='agent', device_id='device', session_id='session',
                          project_id='project', ticket_id='ticket', profile_version=1)
        self.plan = self.pin()
        self.manifest_directory = self.base / 'manifest'
        prepare_manifest(self.manifest_directory, root=str(self.root), base_sha='a' * 40,
            scope=self.scope, paths=['main.json'], resource='repo:fixture', authorize_scope=lambda *_: True,
            tests=[NamedTest('unit', backend_id=native.backend_id(self.plan), timeout_seconds=5, max_output_bytes=64)])
        self.manifest = json.loads((self.manifest_directory / 'manifest.json').read_text())
        self.directory = self.base / 'backend'
        native.prepare_backend(self.directory, manifest=self.manifest, plan=self.plan)
        self.backend = native.NativeNamedPythonBackend(self.directory, manifest=self.manifest,
                                                      authorize_scope=lambda *_: True)
        self.addCleanup(self.backend.close)
        self.request = BackendRequest(digest(b'review-operation'), digest(canonical(self.manifest)),
            str(self.root), tuple(self.plan['root_identity']), 'unit', 5, 64)

    def pin(self, script='test_case.py'):
        return native.pin_test_plan(root=str(self.root), codex=str(self.cli), executable=str(self.executable),
            cli_version='fixture-cli', scripts={'unit': {
                'script': script, 'timeout_seconds': 5, 'max_output_bytes': 64}})

    def qualify(self):
        with patch.object(self.backend, '_execute', return_value=(
                native._receipt('passed', cleanup=True), {key: True for key in native.REQUIRED_CHECKS}, 'completed')):
            self.backend.qualify()

    def assert_refused_before_launch(self):
        with patch.object(self.backend, '_execute') as execute:
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.manifest['tests'][0]))
            with self.assertRaises(DevelopmentError):
                self.backend.run(self.request)
            execute.assert_not_called()
        self.assertFalse((self.directory / (self.request.operation_id + '.json')).exists())

    def test_changed_script_refuses_before_launch(self):
        self.qualify()
        self.script.write_bytes(b'VALUE = 2\n')
        self.assert_refused_before_launch()

    def test_identical_bytes_at_replacement_inode_do_not_adopt_new_script(self):
        self.qualify()
        replacement = self.root / 'replacement.py'
        replacement.write_bytes(self.original)
        replacement.replace(self.script)
        self.assert_refused_before_launch()

    def test_replaced_parent_directory_refuses_even_when_script_inode_is_preserved(self):
        scripts = self.root / 'tests'
        scripts.mkdir(mode=0o700)
        selected = scripts / 'case.py'
        selected.write_bytes(self.original)
        plan = self.pin('tests/case.py')
        original_pin = plan['tests']['unit']['script_pin']
        scripts.rename(self.root / 'old-tests')
        scripts.mkdir(mode=0o700)
        (self.root / 'old-tests/case.py').rename(selected)
        self.assertEqual([selected.stat().st_dev, selected.stat().st_ino], original_pin['identity'])
        changed = self.pin('tests/case.py')
        self.assertNotEqual(changed['tests']['unit']['script_pin']['parents'], original_pin['parents'])
        backend = object.__new__(native.NativeNamedPythonBackend)
        backend.plan = plan
        with self.assertRaises(DevelopmentError):
            backend._script_payload('unit')

    def test_symlink_and_hardlink_scripts_are_refused_at_pin_and_run(self):
        self.qualify()
        alias = self.root / 'alias.py'
        alias.symlink_to(self.script.name)
        with self.assertRaises(DevelopmentError):
            self.pin('alias.py')
        alias.unlink()
        os.link(self.script, alias)
        with self.assertRaises(DevelopmentError):
            self.pin()
        self.assert_refused_before_launch()

    def test_manifest_may_not_include_script_or_case_alias(self):
        for name in ('test_case.py', 'TEST_CASE.PY'):
            with self.subTest(path=name):
                if not (self.root / name).exists():
                    (self.root / name).write_bytes(self.original)
                manifest = deepcopy(self.manifest)
                manifest['files'][name] = {'sha256': digest(self.original), 'bytes': len(self.original)}
                target = self.base / ('forbidden-' + name)
                with self.assertRaises(DevelopmentError):
                    native.prepare_backend(target, manifest=manifest, plan=self.plan)
                self.assertFalse(target.exists())

    def test_unicode_normalized_script_alias_is_outside_model_caps(self):
        script = self.root / 'caf\u00e9.py'
        script.write_bytes(self.original)
        plan = self.pin(script.name)
        alias = self.root / 'cafe\u0301.py'
        if not alias.exists():
            alias.write_bytes(self.original)
        manifest = deepcopy(self.manifest)
        manifest['files']['cafe\u0301.py'] = {'sha256': digest(self.original), 'bytes': len(self.original)}
        manifest['tests'][0]['backend_id'] = native.backend_id(plan)
        with self.assertRaises(DevelopmentError):
            native.prepare_backend(self.base / 'unicode-alias', manifest=manifest, plan=plan)

    def test_model_patch_cannot_modify_pinned_script(self):
        broker = DevelopmentBroker(self.manifest_directory, enabled=True, authorize_scope=lambda *_: True,
                                   test_backend=self.backend)
        self.addCleanup(broker.close)
        with self.assertRaises(DevelopmentError) as caught:
            broker.patch_file('model-patch', self.script.name, digest(self.original), 'VALUE = 2\n')
        self.assertEqual(caught.exception.code, 'path_not_allowed')
        with self.assertRaises(DevelopmentError):
            parse_action(json.dumps({'schema': 1, 'kind': 'patch', 'path': self.script.name,
                'expected_sha256': digest(self.original), 'content': 'VALUE = 2\n'}), self.manifest, 65536)
        self.assertEqual(self.script.read_bytes(), self.original)

    def test_legacy_unpinned_plan_is_not_accepted(self):
        plan = deepcopy(self.plan)
        plan['schema'] = 1
        plan['profile_version'] = 1
        plan['tests']['unit'].pop('script_pin')
        with self.assertRaises(DevelopmentError):
            native.prepare_backend(self.base / 'legacy-plan', manifest=self.manifest, plan=plan)

    def test_pin_read_rejects_change_during_the_read(self):
        original_read = os.read
        changed = []
        def read(descriptor, maximum):
            data = original_read(descriptor, maximum)
            if data == self.original and not changed:
                changed.append(True)
                self.script.write_bytes(b'VALUE = 2\n')
            return data
        with patch.object(os, 'read', side_effect=read), self.assertRaises(DevelopmentError):
            self.pin()
        self.assertTrue(changed)

    def test_launch_payload_holds_verified_bytes_despite_later_path_change(self):
        self.qualify()
        captured = []
        child = _ExitedCLI(999999995)
        command = self.backend._command
        def mutate_after_payload(payload):
            captured.append(deepcopy(payload))
            self.script.write_bytes(b'VALUE = 2\n')
            return command(payload)
        with patch.object(self.backend, '_command', side_effect=mutate_after_payload), \
             patch.object(native.subprocess, 'Popen', return_value=child), \
             patch.object(native.socket, 'socket', return_value=_Listener()), \
             patch.object(self.backend, '_gone', return_value=True), \
             patch.object(self.backend, '_signal'):
            self.backend._execute(self.request, qualify=False)
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertEqual(base64.b64decode(payload['script_base64'], validate=True), self.original)
        self.assertEqual(payload['script_sha256'], digest(self.original))
        self.assertNotEqual(self.script.read_bytes(), self.original)

    def test_cached_receipt_remains_exact_recovery_and_never_launches_changed_script(self):
        self.qualify()
        result = native._receipt('passed', cleanup=True)
        with patch.object(self.backend, '_execute', return_value=(result, {}, 'completed')):
            self.backend.run(self.request)
        self.script.write_bytes(b'VALUE = 2\n')
        with patch.object(self.backend, '_execute') as execute:
            self.assertEqual(self.backend.lookup(self.request), result)
            self.assertEqual(self.backend.run(self.request), result)
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.manifest['tests'][0]))
            execute.assert_not_called()


class SupervisedScriptPinsReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = supervisor_fixture.SupervisorTests('test_default_supervision_is_unqualified_and_starts_nothing')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.qualify()
        self.fixture = fixture

    def test_changed_script_refuses_before_supervisor_launch_or_packet(self):
        fixture = self.fixture
        (fixture.fixture.root / 'case.py').write_bytes(b'VALUE = 2\n')
        before = set(fixture.fixture.directory.iterdir())
        with patch.object(fixture.backend, '_launch') as launch, self.assertRaises(DevelopmentError):
            fixture.backend._run_supervised(fixture.request, qualify=False)
        launch.assert_not_called()
        self.assertEqual(set(fixture.fixture.directory.iterdir()), before)


if __name__ == '__main__':
    unittest.main()
