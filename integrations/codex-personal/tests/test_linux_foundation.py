"""Pure foundation-contract tests; never native/Linux qualification evidence."""
import array
import errno
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location('linux_foundation_probe',
    Path(__file__).with_name('qualify_linux_foundation.py'))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)


def evaluate_filter(architecture, number):
    accumulator = 0
    program = probe.seccomp_program()
    index = 0
    while index < len(program):
        code, yes, no, value = program[index]
        if code == 0x20:
            accumulator = architecture if value == 4 else number
        elif code == 0x15:
            index += yes if accumulator == value else no
        elif code == 0x35:
            index += yes if accumulator >= value else no
        elif code == 0x06:
            return value
        else:
            raise AssertionError('unknown BPF instruction')
        index += 1
    raise AssertionError('no terminal BPF instruction')


class LinuxFoundationTests(unittest.TestCase):
    def test_unknown_abi_killed_and_x32_denied_before_native_syscalls(self):
        self.assertEqual(evaluate_filter(0x40000003, 39), 0x80000000)
        self.assertEqual(evaluate_filter(0xc00000b7, 39), 0x80000000)
        self.assertEqual(evaluate_filter(0xc000003e, 0x40000000 | 39), 0x50000 | errno.EPERM)
        self.assertEqual(evaluate_filter(0xc000003e, 39), 0x7fff0000)

    def test_network_process_mount_and_security_controls_are_denied(self):
        for number in (41, 42, 53, 56, 57, 58, 59, 62, 126, 155, 157, 160, 161,
                       165, 166, 234, 272, 304, 308, 310, 311, 317, 321, 322,
                       424, 425, 426, 427, 428, 434, 435):
            with self.subTest(number=number):
                self.assertEqual(evaluate_filter(0xc000003e, number), 0x50000 | errno.EPERM)

    def test_fixed_command_never_invokes_codex_login_shell_or_sudo(self):
        command = probe.command({'nonce': 'synthetic'}, Path('/tmp/public-fixture.py'))
        self.assertEqual(command[:2], ['/usr/bin/unshare', '--user'])
        for argument in ('--map-root-user', '--mount', '--pid', '--fork', '--kill-child=SIGKILL',
                         '--net', '--ipc', '--uts', '--propagation', 'private', '-I', '-S'):
            self.assertIn(argument, command)
        self.assertFalse(any('codex' in argument or 'sudo' in argument or argument == 'sh' for argument in command))

    def test_unsupported_host_always_unqualified_before_files_or_processes(self):
        with patch.object(probe.platform, 'system', return_value='Darwin'), \
                patch.object(probe, 'read_pin') as read, patch.object(probe.subprocess, 'Popen') as popen:
            report = probe.run()
        self.assertEqual(report['result'], 'linux_unqualified')
        self.assertFalse(report['backend_execution_qualified'])
        self.assertEqual(report['models'], 0)
        read.assert_not_called();popen.assert_not_called()

    def test_file_pin_is_bounded_nofollow_and_rejects_other_writable_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve();source = root / 'canary'
            source.write_bytes(b'synthetic');source.chmod(0o600)
            data, pin = probe.read_pin(source)
            self.assertEqual(data, b'synthetic')
            self.assertEqual(pin['sha256'], probe.digest(data))
            alias = root / 'alias';alias.symlink_to(source)
            with self.assertRaises(probe.ProbeError):probe.read_pin(alias)
            source.chmod(0o622)
            with self.assertRaises(probe.ProbeError):probe.read_pin(source)

    def test_exact_parent_pin_handles_empty_runtime_files_without_adopting_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder).resolve() / 'empty-runtime.py'
            source.touch(mode=0o600)
            _, pin = probe.read_pin(source)
            self.assertEqual(probe.read_pin(source, expected=pin)[1], pin)
            source.write_bytes(b'changed')
            with self.assertRaises(probe.ProbeError):probe.read_pin(source, expected=pin)

    def test_component_source_pins_cover_only_the_explicit_trusted_functions(self):
        pins = probe.component_pins(Path(probe.__file__).read_bytes())
        self.assertEqual(set(pins), {'fixed_canaries', 'inner_setup', 'rootfs', 'lock_down', 'run', 'terminate_owned'})
        self.assertTrue(all(len(value) == 64 for value in pins.values()))

    def test_cleanup_without_init_kernel_identity_never_claims_complete(self):
        child = SimpleNamespace(wait=lambda timeout: 0, poll=lambda: 0)
        with patch.object(probe.signal, 'pidfd_send_signal', create=True) as signal_fd, \
                patch.object(probe.select, 'select', return_value=([], [], [])):
            report = probe.terminate_owned(child, 17, None)
        signal_fd.assert_called_once_with(17, signal.SIGKILL)
        self.assertTrue(report['launcher_reaped'])
        self.assertFalse(report['namespace_init_exited'])
        self.assertFalse(report['cleanup_confirmed'])

    def test_cleanup_uses_only_given_owned_pidfds_not_numeric_namespace_pids(self):
        child = SimpleNamespace(wait=lambda timeout: 0, poll=lambda: 0)
        with patch.object(probe.signal, 'pidfd_send_signal', create=True) as signal_fd, \
                patch.object(probe.select, 'select', return_value=([19], [], [])), \
                patch.object(probe.os, 'kill') as kill, patch.object(probe.os, 'killpg') as killpg:
            report = probe.terminate_owned(child, 17, 19)
        self.assertEqual([call.args for call in signal_fd.call_args_list], [(19, signal.SIGKILL), (17, signal.SIGKILL)])
        self.assertTrue(report['cleanup_confirmed'])
        kill.assert_not_called();killpg.assert_not_called()

    def test_pidfd_signal_error_keeps_cleanup_unconfirmed(self):
        child = SimpleNamespace(wait=lambda timeout: 0, poll=lambda: 0)
        with patch.object(probe.signal, 'pidfd_send_signal', side_effect=PermissionError, create=True), \
                patch.object(probe.select, 'select', return_value=([19], [], [])):
            self.assertFalse(probe.terminate_owned(child, 17, 19)['cleanup_confirmed'])

    def test_cleanup_pidfd_and_launcher_share_one_two_second_deadline(self):
        calls = []
        child = SimpleNamespace(wait=lambda timeout: calls.append(timeout), poll=lambda: 0)
        with patch.object(probe.signal, 'pidfd_send_signal', create=True), \
                patch.object(probe.select, 'select', return_value=([19], [], [])) as ready, \
                patch.object(probe.time, 'monotonic', side_effect=[1, 2, 3]):
            self.assertTrue(probe.terminate_owned(child, 17, 19)['cleanup_confirmed'])
        self.assertEqual(ready.call_args.args[3], 1)
        self.assertEqual(calls, [0])

    def test_bad_control_nonce_closes_received_descriptor(self):
        with tempfile.TemporaryFile() as stream:
            owned = os.dup(stream.fileno())
            channel = SimpleNamespace(recvmsg=lambda *_: (
                b'{"nonce":"wrong"}', [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array('i', [owned]).tobytes())], 0, None))
            with self.assertRaises(probe.ProbeError):probe.receive(channel, 'expected', want_fd=True)
            with self.assertRaises(OSError):os.fstat(owned)

    def test_expired_startup_has_fixed_reason(self):
        with patch.object(probe.time, 'monotonic', return_value=10):
            with self.assertRaisesRegex(probe.ProbeError, '^startup_deadline_exhausted$'):
                probe.remaining(10)

    def test_output_is_bounded_during_reads(self):
        child = SimpleNamespace(stdout=SimpleNamespace(fileno=lambda: 11),
                                stderr=SimpleNamespace(fileno=lambda: 12), poll=lambda: 0)
        reads = []
        def read(_descriptor, size):
            reads.append(size)
            return b'x' * size
        with patch.object(probe.os, 'set_blocking'), patch.object(probe.os, 'read', side_effect=read), \
                patch.object(probe.select, 'select', return_value=([11], [], [])), \
                patch.object(probe.time, 'monotonic', return_value=1):
            with self.assertRaisesRegex(probe.ProbeError, '^output_bound_exceeded$'):
                probe.collect(child, 10)
        self.assertEqual(sum(reads), probe.MAX_OUTPUT + 1)


if __name__ == '__main__':
    unittest.main()
