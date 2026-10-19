"""Cross-runtime clock choice and fail-closed behavior, without subprocesses."""
import os
from unittest import TestCase
from unittest.mock import patch

from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents import dev_test_supervisor as supervisor


class ControlClockTests(TestCase):
    def test_all_default_shared_timestamps_use_os_clock_never_runtime_epoch(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        with patch.object(supervisor.time, 'clock_gettime', return_value=10000.0) as clock, \
                patch.object(supervisor.time, 'monotonic', side_effect=AssertionError('runtime epoch forbidden')):
            first = supervisor._fresh_control_window(lambda: True, nonce='a' * 32, sequence=1)
            self.assertEqual(first['deadline'], 10001.5)
            control = supervisor._ControlPipe(read, nonce='a' * 32, initial=first)
            self.assertFalse(control.authorized())
            supervisor._send_control_window(write, first)
            self.assertTrue(control.authorized())
            clock.assert_called_with(supervisor.time.CLOCK_MONOTONIC)
            clock.return_value = 10001.5
            self.assertFalse(control.authorized())
            self.assertTrue(control.ended)

    def test_unavailable_system_clock_never_falls_back(self):
        with patch.object(supervisor.time, 'clock_gettime', side_effect=OSError('synthetic clock failure')), \
                patch.object(supervisor.time, 'monotonic', side_effect=AssertionError('fallback forbidden')):
            with self.assertRaises(DevelopmentError) as error:
                supervisor._fresh_control_window(lambda: True, nonce='a' * 32, sequence=1)
            self.assertEqual(error.exception.code, 'supervisor_clock_unavailable')

    def test_missing_system_clock_never_falls_back(self):
        with patch.object(supervisor.time, 'CLOCK_MONOTONIC'):
            del supervisor.time.CLOCK_MONOTONIC
            with self.assertRaises(DevelopmentError) as error:
                supervisor._control_clock()
            self.assertEqual(error.exception.code, 'supervisor_clock_unavailable')

    def test_invalid_system_clock_values_are_rejected(self):
        for value in (-1, True, float('nan'), float('inf')):
            with self.subTest(value=value), patch.object(supervisor.time, 'clock_gettime', return_value=value), \
                    self.assertRaises(DevelopmentError) as error:
                supervisor._fresh_control_window(lambda: True, nonce='a' * 32, sequence=1)
            self.assertEqual(error.exception.code, 'supervisor_clock_changed')
