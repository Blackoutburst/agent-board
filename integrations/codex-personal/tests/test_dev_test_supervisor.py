"""Supervisor state/control tests with fake helpers; no subprocess or model."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_test_backend import _receipt
from blackoutburst_agents.dev_test_supervisor import (SUPERVISED_CHECKS, SupervisedNativeNamedPythonBackend,
    BOOTSTRAP, CONTROLLER_FILES, _ControlPipe, _ancestor_identities, prepare_supervision, supervise_packet)
import test_dev_test_backend as backend_fixture


class _Pipe:
    def __init__(self, callback=None):
        self.read, self.write = os.pipe()
        self.closed = False
        self.callback = callback

    def fileno(self):
        return self.write

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.write)
            os.close(self.read)
            if self.callback:
                self.callback()


class _Helper:
    def __init__(self, callback=None, exited=False):
        self.stdin = _Pipe(callback)
        self.exited = exited

    def poll(self):
        return 78 if self.exited else None

    def wait(self, timeout):
        return 0


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = backend_fixture.NativeTestBackendTests('test_controller_policy_has_exact_root_deny_and_no_network')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        prepare_supervision(self.fixture.directory)
        self.backend = SupervisedNativeNamedPythonBackend(self.fixture.directory,
            manifest=self.fixture.manifest, authorize_scope=self.fixture.authorize)
        self.addCleanup(self.backend.close)
        self.request = self.fixture.request

    def qualify(self):
        self.fixture.qualified()
        value = {'schema': 1, 'supervision_hash': digest(canonical(self.backend.supervision)),
            'plan_hash': self.backend.document['plan_hash'], 'checks': {key: True for key in SUPERVISED_CHECKS},
            'cleanup_confirmed': True, 'result': 'qualified_supervised_fixture'}
        self.backend.private.replace('supervision-qualification.json', canonical(value))

    def complete(self, packet_name, result=None, checks=None):
        result = _receipt('passed', cleanup=True) if result is None else result
        checks = {key: True for key in SUPERVISED_CHECKS} if checks is None else checks
        packet = json.loads((self.fixture.directory / packet_name).read_text())
        state = {'schema': 1, 'nonce': packet['nonce'], 'request_hash': packet['request_hash'],
            'phase': 'complete', 'supervisor_identity': {'pid': 999999991, 'pgid': 999999991, 'uid': os.geteuid()},
            'result': result, 'reason': 'completed', 'checks': checks}
        self.backend.private.replace(packet['state_name'], canonical(state))
        if not packet['qualify']:
            original_hash = self.backend._request(self.request)
            self.backend.private.replace(self.request.operation_id + '.json', canonical({
                'schema': 1, 'request_hash': original_hash, 'phase': 'complete', 'result': result, 'reason': 'completed'}))
        return state

    def test_default_supervision_is_unqualified_and_starts_nothing(self):
        self.fixture.qualified()
        with patch.object(self.backend, '_launch') as launch:
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.fixture.suite))
            with self.assertRaises(DevelopmentError):
                self.backend.run(self.request)
            launch.assert_not_called()

    def test_completed_helper_receipt_is_reused_without_another_helper(self):
        self.qualify()
        def launch(packet):
            self.complete(packet)
            helper = _Helper()
            self.addCleanup(helper.stdin.close)
            return helper
        with patch.object(self.backend, '_launch', side_effect=launch) as launch:
            result = self.backend.run(self.request)
            self.assertEqual(result, _receipt('passed', cleanup=True))
            self.assertEqual(self.backend.run(self.request), result)
            self.assertEqual(self.backend.lookup(self.request), result)
            self.assertEqual(launch.call_count, 1)

    def test_lost_helper_leaves_intent_and_blocks_new_attempts(self):
        self.qualify()
        helper = _Helper(exited=True)
        with patch.object(self.backend, '_launch', return_value=helper) as launch:
            self.assertEqual(self.backend.run(self.request), _receipt())
            self.assertEqual(self.backend.lookup(self.request), _receipt())
            self.assertEqual(self.backend.run(self.request), _receipt())
            with self.assertRaises(DevelopmentError):
                self.backend.run(replace(self.request, operation_id=digest(b'another-operation')))
            self.assertEqual(launch.call_count, 1)
        self.assertTrue(helper.stdin.closed)

    def test_revocation_closes_owned_control_pipe_and_retains_cleanup_result(self):
        self.qualify()
        def launch(packet):
            self.fixture.allowed = False
            helper = _Helper(callback=lambda:self.complete(packet, _receipt('cancelled', cleanup=True), {}))
            self.addCleanup(helper.stdin.close)
            return helper
        with patch.object(self.backend, '_launch', side_effect=launch):
            self.assertEqual(self.backend.run(self.request), _receipt('cancelled', cleanup=True))

    def test_nonce_mismatch_is_not_adopted(self):
        self.qualify()
        helper = _Helper()
        def launch(packet):
            value = json.loads((self.fixture.directory / packet).read_text())
            state = json.loads((self.fixture.directory / value['state_name']).read_text())
            state['nonce'] = '0' * 32
            self.backend.private.replace(value['state_name'], canonical(state))
            return helper
        with patch.object(self.backend, '_launch', side_effect=launch), self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        self.assertTrue(helper.stdin.closed)

    def test_current_controller_pins_are_required(self):
        self.qualify()
        with patch('blackoutburst_agents.dev_test_supervisor._controller_pin', return_value={}):
            self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.fixture.suite))
            with patch.object(self.backend, '_launch') as launch, self.assertRaises(DevelopmentError):
                self.backend.run(self.request)
            launch.assert_not_called()

    def test_passed_job_without_all_guards_is_refused(self):
        self.qualify()
        helper = _Helper()
        def launch(packet):
            self.complete(packet, checks={})
            return helper
        with patch.object(self.backend, '_launch', side_effect=launch), self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        self.assertTrue(helper.stdin.closed)

    def test_control_pipe_requires_fresh_heartbeat_and_eof_overrides_queued_data(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        control = _ControlPipe(read)
        self.assertFalse(control.authorized())
        os.write(write, b'G')
        self.assertTrue(control.authorized())
        os.write(write, b'G')
        os.close(write)
        self.assertFalse(control.authorized())
        self.assertFalse(control.authorized())

    def test_first_heartbeat_is_required_even_at_monotonic_origin(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        control = _ControlPipe(read)
        with patch('blackoutburst_agents.dev_test_supervisor.time.monotonic', return_value=0):
            self.assertFalse(control.authorized())
            os.write(write, b'G')
            self.assertTrue(control.authorized())

    def test_workspace_containing_controller_or_connection_state_is_refused(self):
        package = self.fixture.root / 'control' / 'blackoutburst_agents'
        package.mkdir(parents=True)
        with patch('blackoutburst_agents.dev_test_supervisor.__file__', str(package / 'dev_test_supervisor.py')):
            with self.assertRaises(DevelopmentError) as error:
                self.backend._supervision_pins()
            self.assertEqual(error.exception.code, 'supervisor_workspace_overlap')
        protected = self.fixture.root / 'connection-state'
        protected.mkdir(mode=0o700)
        with patch.dict(os.environ, {'CODEX_HOME': str(protected)}):
            with self.assertRaises(DevelopmentError) as error:
                self.backend._supervision_pins()
            self.assertEqual(error.exception.code, 'supervisor_workspace_overlap')

    def test_workspace_inside_controller_or_connection_state_is_refused(self):
        with patch('blackoutburst_agents.dev_test_supervisor.__file__',
                   str(self.fixture.base / 'blackoutburst_agents' / 'dev_test_supervisor.py')):
            with self.assertRaises(DevelopmentError):
                self.backend._supervision_pins()

    def test_workspace_containing_toolchain_or_private_journal_is_refused(self):
        tool = self.fixture.root / 'ordinary-tool'
        tool.write_text('ordinary fixture, never executed')
        with patch.dict(self.backend.plan, {'codex': str(tool)}):
            with self.assertRaises(DevelopmentError):
                self.backend._supervision_pins()
        journal = self.fixture.root / 'journal'
        journal.mkdir(mode=0o700)
        with patch.object(self.backend.private, 'path', journal):
            with self.assertRaises(DevelopmentError):
                self.backend._supervision_pins()
        with patch.dict(os.environ, {'CODEX_HOME': str(self.fixture.base)}):
            with self.assertRaises(DevelopmentError):
                self.backend._supervision_pins()

    def test_directory_alias_comparison_uses_filesystem_identities(self):
        alias = self.fixture.root.with_name(self.fixture.root.name.upper())
        if not alias.exists():
            self.skipTest('filesystem has case-sensitive directory names')
        self.assertEqual(_ancestor_identities(alias), _ancestor_identities(self.fixture.root))

    def test_corrupt_supervision_document_has_only_a_fixed_error(self):
        for document in (None, [], 'text', {'schema': True}):
            with self.subTest(document=document), patch.object(self.backend, 'supervision', document):
                with self.assertRaises(DevelopmentError):
                    self.backend._supervision_pins()

    def test_user_owned_hardlinked_tool_is_not_qualified(self):
        alias = self.fixture.root / 'tool-alias'
        os.link(self.fixture.cli, alias)
        self.qualify()
        self.assertFalse(self.backend.qualified_for(self.request.manifest_hash, self.fixture.suite))

    def test_bootstrap_executes_exact_source_bytes_without_opening_cache_files(self):
        root = self.fixture.base / 'ordinary-controller'
        package = root / 'blackoutburst_agents'
        package.mkdir(parents=True)
        pins = {}
        for name in CONTROLLER_FILES:
            source = ("def supervise_packet(packet):\n    return 73\n" if name == 'dev_test_supervisor.py'
                      else "fixture_constant = 'ordinary source'\n")
            (package / name).write_text(source)
            pins[name] = digest(source.encode())
        cache = package / '__pycache__'
        cache.mkdir()
        (cache / 'ordinary-cache-note.txt').write_text('unrelated ordinary cache metadata')
        original_open = open
        opened = []
        def observe(path, *args, **kwargs):
            opened.append(Path(path))
            return original_open(path, *args, **kwargs)
        argv = ['bootstrap', str(root), canonical(pins).decode(), 'ordinary-packet']
        with patch.dict(sys.modules), patch.object(sys, 'argv', argv), patch('builtins.open', side_effect=observe):
            with self.assertRaises(SystemExit) as exit_code:
                exec(BOOTSTRAP, {})
        self.assertEqual(exit_code.exception.code, 73)
        self.assertEqual(set(opened), {package / name for name in CONTROLLER_FILES})

    def test_garbage_control_data_cannot_authorize(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        control = _ControlPipe(read)
        os.write(write, b'G!')
        self.assertFalse(control.authorized())

    def test_invalid_helper_packet_path_does_not_open_anything(self):
        with patch('blackoutburst_agents.dev_test_supervisor.PrivateDirectory') as private:
            self.assertEqual(supervise_packet('/untrusted/anything.json'), 78)
            private.assert_not_called()
