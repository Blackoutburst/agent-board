"""Ordinary probe-contract checks; these are not Linux sandbox evidence."""
import importlib.util
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_test_backend import pin_test_plan

_spec = importlib.util.spec_from_file_location('linux_feasibility_probe',
    Path(__file__).with_name('qualify_linux_primitives.py'))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


class LinuxFeasibilityTests(unittest.TestCase):
    def test_backend_still_refuses_linux_without_binary_or_process_access(self):
        with patch('blackoutburst_agents.dev_test_backend._host', return_value={
                'system': 'Linux', 'release': 'synthetic', 'machine': 'synthetic'}), patch(
                'blackoutburst_agents.dev_test_backend._binary') as binary, patch(
                'blackoutburst_agents.dev_test_backend.subprocess.Popen') as launch:
            with self.assertRaises(DevelopmentError) as error:
                pin_test_plan(root='/unopened', codex='/unopened/codex', cli_version=probe.VERSION,
                    scripts={'synthetic': {'script': 'test.py', 'timeout_seconds': 1, 'max_output_bytes': 0}})
        self.assertEqual(error.exception.code, 'native_test_platform_unqualified')
        binary.assert_not_called();launch.assert_not_called()

    def test_only_sandbox_commands_and_explicit_fixture_tool_scope_are_constructed(self):
        command = probe.command(Path('/private/tools/codex'), Path('/private/fixture'), probe.CANARY, {})
        self.assertEqual(command[:2], ['/private/tools/codex', 'sandbox'])
        policy = command[command.index('-c') + 1]
        self.assertIn('":root"="deny"', policy)
        self.assertIn('"/private/fixture"="write"', policy)
        self.assertIn('"/private/tools"="read"', policy)
        self.assertIn('network={enabled=false}', policy)
        self.assertNotIn('exec', command);self.assertNotIn('login', command)
        self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', command)

    def test_binary_pin_rejects_symlink_and_shared_writable_tool(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve();binary = root / 'synthetic'
            binary.write_bytes(b'not executable test code');binary.chmod(0o700)
            self.assertEqual(len(probe.binary_hash(binary)), 64)
            alias = root / 'alias';alias.symlink_to(binary)
            with self.assertRaises(ValueError):probe.binary_hash(alias)
            binary.chmod(0o722)
            with self.assertRaises(ValueError):probe.binary_hash(binary)

    def test_cleanup_signals_exact_owned_process_never_namespace_pid(self):
        stream = io.BytesIO(b'READY\n')
        process = SimpleNamespace(pid=91701, stdout=stream, killed=0, reaped=0)
        def kill():process.killed += 1
        def wait(timeout):process.reaped += 1
        process.kill = kill;process.wait = wait;process.poll = lambda: 0 if process.killed else None
        with patch.object(probe.subprocess, 'Popen', return_value=process), patch.object(
                probe.select, 'select', return_value=([stream], [], [])), patch.object(
                probe, 'owned_descendants', return_value={}), patch.object(
                probe, 'process_identity', return_value=('synthetic-birth', 'S')), patch.object(
                probe.os, 'kill') as host_signal, patch.object(probe.os, 'killpg') as host_group_signal:
            receipt = probe.observe_launcher_cleanup(Path('/private/tools/codex'), Path('/private/fixture'), {})
        self.assertEqual(process.killed, 1)
        self.assertGreaterEqual(process.reaped, 1)
        self.assertTrue(receipt['launcher_reaped'])
        self.assertFalse(receipt['descendants_disappeared'])
        host_signal.assert_not_called();host_group_signal.assert_not_called()


if __name__ == '__main__':
    unittest.main()
