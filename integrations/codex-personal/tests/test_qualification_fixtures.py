"""Qualifier fixtures are ordinary files only: no subprocess/native/model call."""
import ast
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import NamedTest, prepare_manifest
from blackoutburst_agents import dev_test_backend


def qualifier(name):
    path = Path(__file__).with_name(name+'.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QualificationFixtureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base/'root'; self.root.mkdir(mode=0o700)

    def check_fixture(self, name, expected_data):
        module = qualifier(name)
        scripts, paths = module.prepare_fixture(self.root)
        self.assertEqual(paths, expected_data)
        self.assertTrue(scripts)
        self.assertFalse(set(paths).intersection(test['script'] for test in scripts.values()))
        for path in paths:
            self.assertEqual((self.root/path).read_text(), '0')
        for suite in scripts.values():
            tree = ast.parse((self.root/suite['script']).read_text())
            # Trusted synthetic tests use only stdlib data/process canaries;
            # they never execute source supplied in the mutable data files.
            forbidden = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id in ('exec', 'eval', 'compile', '__import__')]
            self.assertEqual(forbidden, [])
        scope = dict(owner_id='owner', agent_id='agent', device_id='device', session_id='session',
            project_id='project', ticket_id='ticket', profile_version=1)
        # Fake host/tool metadata only; this ordinary fixture never qualifies or
        # launches a native backend on any operating system.
        with patch.object(dev_test_backend, '_host', return_value={'system':'Darwin','release':'fixture','machine':'fixture'}), \
                patch.object(dev_test_backend, '_binary', return_value='b'*64):
            plan = dev_test_backend.pin_test_plan(root=str(self.root), codex='/synthetic/codex',
                cli_version='codex-cli fixture', scripts=scripts)
        self.assertEqual(plan['schema'], 2)
        self.assertTrue(all(set(suite['script_pin']) == {'sha256','bytes','identity','parents'} for suite in plan['tests'].values()))
        manifest = self.base/'broker'
        prepare_manifest(manifest, root=str(self.root), base_sha='a'*40, scope=scope, paths=paths,
            resource='repo:synthetic', authorize_scope=lambda *_:True,
            tests=[NamedTest(name, backend_id=dev_test_backend.backend_id(plan), timeout_seconds=suite['timeout_seconds'],
                max_output_bytes=suite['max_output_bytes']) for name,suite in scripts.items()])
        manifest_value = json.loads((manifest/'manifest.json').read_text())
        with patch.object(dev_test_backend, '_host', return_value=plan['host']):
            dev_test_backend.prepare_backend(self.base/'backend', manifest=manifest_value, plan=plan)
        self.assertEqual(json.loads((self.base/'backend/plan.json').read_text())['schema'], 2)
        broker = DevelopmentBroker(manifest, enabled=True, authorize_scope=lambda *_:True)
        self.addCleanup(broker.close)
        before = (self.root/scripts['passed']['script']).read_bytes()
        self.assertTrue(module.script_patch_refused(broker, self.root, scripts['passed']['script']))
        self.assertEqual((self.root/scripts['passed']['script']).read_bytes(), before)
        return module, scripts

    def test_named_fixture_scripts_have_no_patch_caps_and_gate_exists_before_pinning(self):
        _, scripts = self.check_fixture('qualify_native_named_tests', ['count.txt'])
        self.assertIn('gate', scripts)
        self.assertTrue((self.root/scripts['gate']['script']).is_file())
        self.assertFalse((self.root/'gate-started.txt').exists())

    def test_supervised_fixture_scripts_have_no_patch_caps_or_source_execution(self):
        _, scripts = self.check_fixture('qualify_native_supervisor', ['count.txt', 'crash-count.txt'])
        self.assertEqual(set(scripts), {'passed', 'timeout', 'crash'})
        self.assertFalse((self.root/'crash-started.txt').exists())

    def test_ack_gate_uses_pinned_held_source_without_executing_a_script(self):
        # This guards qualifier/controller wiring, not native enforcement.
        module = qualifier('qualify_native_named_tests')
        module.prepare_fixture(self.root)
        held = {'script': 'gate.py', 'script_base64': base64.b64encode(b'# trusted gate bytes\n').decode(),
            'script_sha256': 'a'*64}
        received = []
        class Backend:
            plan = {'executable': '/synthetic/interpreter', 'environment': {'LANG': 'C', 'PATH': '/usr/bin:/bin'}}
            def _script_payload(self, name):
                if name != 'gate': raise AssertionError('unexpected named test')
                return held
            def _command(self, payload):
                received.append(payload)
                return ['synthetic-command-not-executed']
        class Listener:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def bind(self, address): pass
            def listen(self, count): pass
            def getsockname(self): return ('127.0.0.1', 12345)
        header = module.dev_test_backend.HEADER + json.dumps({'ok': True}).encode() + b'\n'
        with patch.object(module.socket, 'socket', return_value=Listener()), \
                patch.object(module.subprocess, 'run', return_value=SimpleNamespace(stdout=header, returncode=79)) as run:
            self.assertTrue(module.measure_ack_gate(Backend(), self.root, self.base))
        self.assertEqual(run.call_count, 1)
        self.assertEqual({key: received[0][key] for key in held}, held)
        self.assertFalse((self.root/'gate-started.txt').exists())
