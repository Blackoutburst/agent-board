"""Independent cleanup regression; fake subprocesses only, no model or network."""
import json
import os
import signal

import blackoutburst_agents.dev_test_backend as native_backend
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_test_backend import HEADER, REQUIRED_CHECKS
import test_dev_test_backend as backend_fixture


class _Listener:
    def bind(self, address):
        pass
    def listen(self, size):
        pass
    def getsockname(self):
        return ('127.0.0.1', 49152)
    def close(self):
        pass


class _ControlInput:
    def __init__(self):
        self.writes = []
        self.closed = False
    def write(self, data):
        self.writes.append(data)
        return len(data)
    def flush(self):
        pass
    def close(self):
        self.closed = True


class _ExitedCLI:
    pid = 999999994
    returncode = 0

    def __init__(self, wrapper_pid, prefix=b''):
        value = {'pid': wrapper_pid, 'pgid': wrapper_pid, 'sid': wrapper_pid,
                 'checks': {key: True for key in REQUIRED_CHECKS}, 'ok': True}
        out_read, out_write = os.pipe()
        err_read, err_write = os.pipe()
        os.write(out_write, prefix + HEADER + json.dumps(value).encode() + b'\n')
        os.close(out_write)
        os.close(err_write)
        self.stdin = _ControlInput()
        self.stdout = os.fdopen(out_read, 'rb', buffering=0)
        self.stderr = os.fdopen(err_read, 'rb', buffering=0)

    def poll(self):
        return self.returncode

    def wait(self, timeout):
        return self.returncode


class NativeBackendCleanupReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = backend_fixture.NativeTestBackendTests('test_controller_policy_has_exact_root_deny_and_no_network')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture

    def test_identified_wrapper_is_killed_even_after_the_cli_parent_exits(self):
        fixture = self.fixture
        wrapper_pid = 999999995
        child = _ExitedCLI(wrapper_pid)
        alive = {wrapper_pid}
        signals = []
        def send(group, value):
            signals.append((group, value))
            alive.discard(group)
        with patch('blackoutburst_agents.dev_test_backend.subprocess.Popen', return_value=child), \
                patch('blackoutburst_agents.dev_test_backend.socket.socket', return_value=_Listener()), \
                patch.object(fixture.backend, '_gone', side_effect=lambda group: group not in alive), \
                patch.object(fixture.backend, '_signal', side_effect=send):
            result, checks, reason = fixture.backend._execute(fixture.request, qualify=False)
        self.assertIn((wrapper_pid, signal.SIGKILL), signals)
        self.assertEqual(reason, 'wrapper_left_running')
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['cleanup_confirmed'])
        self.assertEqual(checks, {key: True for key in REQUIRED_CHECKS})
        self.assertFalse(alive)
        self.assertEqual(child.stdin.writes, [b'G'])
        self.assertTrue(child.stdin.closed)
        self.assertEqual(list(fixture.root.glob('.bb-dev-canary-*')), [])
        self.assertEqual(list(fixture.directory.glob('canary-*')), [])

    def test_cli_stdout_before_guard_never_opens_the_source_gate(self):
        fixture = self.fixture
        wrapper_pid = 999999995
        child = _ExitedCLI(wrapper_pid, prefix=b'unexpected CLI stdout\n')
        signals = []
        with patch('blackoutburst_agents.dev_test_backend.subprocess.Popen', return_value=child), \
                patch('blackoutburst_agents.dev_test_backend.socket.socket', return_value=_Listener()), \
                patch.object(fixture.backend, '_gone', return_value=True), \
                patch.object(fixture.backend, '_signal', side_effect=lambda *args: signals.append(args)):
            result, checks, reason = fixture.backend._execute(fixture.request, qualify=False)
        self.assertEqual(reason, 'guard_protocol_invalid')
        self.assertEqual(child.stdin.writes, [])
        self.assertTrue(child.stdin.closed)
        self.assertFalse(result['cleanup_confirmed'])
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(checks, {})

    def test_revocation_between_guard_and_ack_never_opens_source_gate(self):
        fixture = self.fixture
        wrapper_pid = 999999995
        child = _ExitedCLI(wrapper_pid)
        original_decode = native_backend.decode
        signals = []
        alive = {wrapper_pid}
        def revoke_after_observing_header(data):
            value = original_decode(data)
            if type(value) is dict and value.get('pid') == wrapper_pid:
                fixture.allowed = False
            return value
        def send(group, value):
            signals.append((group, value))
            alive.discard(group)
        with patch('blackoutburst_agents.dev_test_backend.subprocess.Popen', return_value=child), \
                patch('blackoutburst_agents.dev_test_backend.socket.socket', return_value=_Listener()), \
                patch.object(native_backend, 'decode', side_effect=revoke_after_observing_header), \
                patch.object(fixture.backend, '_gone', side_effect=lambda group: group not in alive), \
                patch.object(fixture.backend, '_signal', side_effect=send):
            result, checks, reason = fixture.backend._execute(fixture.request, qualify=False)
        self.assertEqual(child.stdin.writes, [])
        self.assertTrue(child.stdin.closed)
        self.assertIn((wrapper_pid, signal.SIGKILL), signals)
        self.assertNotEqual(result['status'], 'passed')
        self.assertFalse(alive)
