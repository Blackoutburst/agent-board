"""Fresh authorization windows with fake clocks/pipes/helpers; no process launch."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import Mock, patch

from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents.dev_test_supervisor import (_ControlPipe, CONTROL_TTL,
    _fresh_control_window, _send_control_window, supervise_packet)
import test_dev_test_supervisor as supervisor_fixture


NONCE = 'a' * 32


class Clock:
    def __init__(self, value=100.0):
        self.value = value
    def __call__(self):
        return self.value
    def advance(self, seconds):
        self.value += seconds


def window(sequence=1, start=100.0, validated=100.1):
    return {'schema': 1, 'nonce': NONCE, 'sequence': sequence,
        'started_at': start, 'validated_at': validated, 'deadline': start + 1.5}


class FreshControlWindowTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.read, self.write = os.pipe()
        self.addCleanup(os.close, self.read)
        self.addCleanup(lambda: os.close(self.write) if self.write is not None else None)
        self.initial = window()
        self.clock.value = 100.1
        self.control = _ControlPipe(self.read, nonce=NONCE, initial=self.initial, clock=self.clock)

    def send(self, value):
        os.write(self.write, canonical(value) + b'\n')

    def test_ttl_remains_exactly_one_point_five(self):
        self.assertEqual(CONTROL_TTL, 1.5)

    def test_no_frame_is_not_authority_and_first_frame_is_bound_to_packet(self):
        self.assertFalse(self.control.authorized())
        self.send(self.initial)
        self.assertTrue(self.control.authorized())
        self.assertTrue(self.control.authorized())
        self.clock.value = self.initial['deadline']
        self.assertFalse(self.control.authorized())

    def test_initial_wait_is_bounded_and_receives_without_other_work(self):
        observed = []
        def ready(*args):
            observed.append(True)
            self.clock.advance(0.1)
            self.send(self.initial)
            return ([self.read], [], [])
        with patch('blackoutburst_agents.dev_test_supervisor.select.select', side_effect=ready):
            self.assertTrue(self.control.wait_initial())
        self.assertEqual(observed, [True])

    def test_queued_old_frame_never_extends_its_original_deadline(self):
        self.clock.value = 102.0
        self.send(self.initial)
        self.assertFalse(self.control.authorized())
        self.assertTrue(self.control.ended)

    def test_queued_continuous_frames_use_their_timestamps_not_read_time(self):
        second = window(2, 100.8, 101.0)
        self.send(self.initial); self.assertTrue(self.control.authorized())
        self.send(second)
        self.clock.value = 101.7
        self.assertTrue(self.control.authorized())
        self.clock.value = second['deadline']
        self.assertFalse(self.control.authorized())

    def test_queued_later_frames_cannot_extend_an_unobserved_initial_window(self):
        self.send(self.initial); self.send(window(2, 100.8, 101.0))
        self.clock.value = 101.7
        self.assertFalse(self.control.authorized())
        self.assertTrue(self.control.ended)

    def test_observed_expiry_never_resurrects(self):
        self.send(self.initial); self.assertTrue(self.control.authorized())
        self.clock.value = 101.6
        self.assertFalse(self.control.authorized())
        self.send(window(2, 101.0, 101.2))
        self.assertFalse(self.control.authorized())

    def test_partial_frame_never_renews_and_is_bounded(self):
        raw = canonical(self.initial) + b'\n'
        os.write(self.write, raw[:30])
        self.assertFalse(self.control.authorized())
        os.write(self.write, raw[30:])
        self.assertTrue(self.control.authorized())
        os.write(self.write, b'x' * 513)
        self.assertFalse(self.control.authorized())
        self.assertTrue(self.control.ended)

    def test_eof_is_permanent_even_with_a_queued_frame(self):
        self.send(self.initial)
        os.close(self.write); self.write = None
        self.assertFalse(self.control.authorized())
        self.assertTrue(self.control.ended)

    def test_replayed_sequence_is_refused(self):
        self.send(self.initial); self.assertTrue(self.control.authorized())
        self.send(self.initial)
        self.assertFalse(self.control.authorized())
        self.assertTrue(self.control.ended)

    def test_gap_between_validations_is_not_hidden_by_new_deadline(self):
        self.send(self.initial); self.assertTrue(self.control.authorized())
        self.clock.value = 101.7
        self.send(window(2, 101.3, 101.6))
        self.assertFalse(self.control.authorized())

    def test_gapped_sequence_and_wrong_nonce_after_established_lease_refuse(self):
        for value in (window(3, 100.2, 100.3), {**window(2, 100.2, 100.3), 'nonce': 'b' * 32}):
            read, write = os.pipe()
            try:
                control = _ControlPipe(read, nonce=NONCE, initial=self.initial, clock=self.clock)
                os.write(write, canonical(self.initial) + b'\n')
                self.assertTrue(control.authorized())
                self.clock.value = 100.3
                os.write(write, canonical(value) + b'\n')
                self.assertFalse(control.authorized())
                self.assertTrue(control.ended)
            finally:
                os.close(read); os.close(write)

    def test_corrupt_types_future_times_nonce_and_keys_refuse(self):
        mutations = [lambda v: v.update(schema=True), lambda v: v.update(sequence=1.0),
            lambda v: v.update(nonce='b' * 32), lambda v: v.update(extra=True),
            lambda v: v.update(started_at=True), lambda v: v.update(validated_at=100.2),
            lambda v: v.update(deadline=103.0), lambda v: v.update(sequence=2),
            lambda v: v.update(started_at=10 ** 400)]
        for mutate in mutations:
            read, write = os.pipe()
            try:
                control = _ControlPipe(read, nonce=NONCE, initial=self.initial, clock=self.clock)
                value = deepcopy(self.initial); mutate(value)
                os.write(write, canonical(value) + b'\n')
                with self.subTest(mutate=mutate):
                    self.assertFalse(control.authorized())
            finally:
                os.close(read); os.close(write)

    def test_duplicate_keys_nonfinite_or_excessive_frames_fail_closed(self):
        excessive = b''.join(canonical({**self.initial, 'sequence': sequence}) + b'\n'
                             for sequence in range(1, 66))
        for raw in (b'{"schema":1,"schema":1}\n', b'{"started_at":NaN}\n',
                    b'{"deadline":Infinity}\n', b'G', excessive):
            read, write = os.pipe()
            try:
                control = _ControlPipe(read, nonce=NONCE, initial=self.initial, clock=self.clock)
                os.write(write, raw)
                with self.subTest(raw=raw):
                    self.assertFalse(control.authorized())
            finally:
                os.close(read); os.close(write)

    def test_clock_regression_permanently_refuses(self):
        self.send(self.initial); self.assertTrue(self.control.authorized())
        self.clock.value = 100.0
        self.assertFalse(self.control.authorized())

    def test_every_fresh_window_calls_authorization_and_retains_start_age(self):
        self.clock.value = 100.0
        authorization = Mock(side_effect=lambda: self.clock.advance(0.1))
        first = _fresh_control_window(authorization, nonce=NONCE, sequence=1, clock=self.clock)
        self.assertEqual(first, self.initial)
        self.clock.advance(0.1)
        second = _fresh_control_window(authorization, nonce=NONCE, sequence=2, previous=first, clock=self.clock)
        self.assertEqual(authorization.call_count, 2)
        self.assertEqual(second['deadline'], second['started_at'] + 1.5)

    def test_slow_or_denied_collection_never_produces_a_window(self):
        self.clock.value = 100.0
        with self.assertRaises(DevelopmentError):
            _fresh_control_window(lambda: self.clock.advance(1.5), nonce=NONCE, sequence=1, clock=self.clock)
        with self.assertRaises(DevelopmentError):
            _fresh_control_window(lambda: False, nonce=NONCE, sequence=1, clock=self.clock)
        self.clock.value = 100.0
        with self.assertRaises(DevelopmentError):
            _fresh_control_window(lambda: self.clock.advance(0.6), nonce=NONCE, sequence=2,
                previous=window(start=99.0, validated=99.1), clock=self.clock)

    def test_send_rechecks_deadline_and_never_retries_a_partial_write(self):
        with patch('blackoutburst_agents.dev_test_supervisor.os.write', return_value=1) as write:
            with self.assertRaises(DevelopmentError):
                _send_control_window(self.write, self.initial, clock=self.clock)
            self.assertEqual(write.call_count, 1)
        self.clock.value = 101.5
        with patch('blackoutburst_agents.dev_test_supervisor.os.write') as write, self.assertRaises(DevelopmentError):
            _send_control_window(self.write, self.initial, clock=self.clock)
        write.assert_not_called()

    def test_bad_sequence_or_nonce_never_calls_authorization(self):
        for sequence, nonce in ((True, NONCE), (0, NONCE), (1, 'wrong')):
            callback = Mock()
            with self.subTest(sequence=sequence, nonce=nonce), self.assertRaises(DevelopmentError):
                _fresh_control_window(callback, nonce=nonce, sequence=sequence, clock=self.clock)
            callback.assert_not_called()

    def test_send_must_precede_the_previous_deadline_even_when_new_window_is_fresh(self):
        second = window(2, 100.8, 101.0)
        self.clock.value = self.initial['deadline']
        with patch('blackoutburst_agents.dev_test_supervisor.os.write') as write, self.assertRaises(DevelopmentError):
            _send_control_window(self.write, second, previous=self.initial, clock=self.clock)
        write.assert_not_called()
        self.clock.value = self.initial['deadline'] - 0.01
        def delayed_write(descriptor, raw):
            self.clock.advance(0.02)
            return len(raw)
        with patch('blackoutburst_agents.dev_test_supervisor.os.write', side_effect=delayed_write), self.assertRaises(DevelopmentError):
            _send_control_window(self.write, second, previous=self.initial, clock=self.clock)

    def test_sender_rejects_clock_regression_during_write(self):
        def write(descriptor, raw):
            self.clock.value -= 0.05
            return len(raw)
        with patch('blackoutburst_agents.dev_test_supervisor.os.write', side_effect=write), self.assertRaises(DevelopmentError):
            _send_control_window(self.write, self.initial, clock=self.clock)


class SupervisorFreshStartupTests(unittest.TestCase):
    def setUp(self):
        supervisor_fixture.SupervisorTests.setUp(self)
        self.clock = Clock()
        guard = patch('subprocess.Popen', side_effect=AssertionError('no real process'))
        guard.start(); self.addCleanup(guard.stop)

    qualify = supervisor_fixture.SupervisorTests.qualify
    complete = supervisor_fixture.SupervisorTests.complete

    def test_first_send_consumes_prelaunch_validation_without_another_callback(self):
        self.qualify()
        authorizations = []
        helper = supervisor_fixture._Helper()
        self.addCleanup(helper.stdin.close)
        def launch(packet):
            self.assertEqual(len(authorizations), 3)
            self.complete(packet)
            return helper
        with patch('blackoutburst_agents.dev_test_supervisor.time.monotonic', self.clock), \
                patch('blackoutburst_agents.dev_test_supervisor._control_clock', self.clock), \
                patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_authorize', side_effect=lambda: authorizations.append(True)), \
                patch.object(self.backend, '_launch', side_effect=launch):
            result = self.backend.run(self.request)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(len(authorizations), 3)

    def test_slow_prelaunch_validation_starts_no_helper(self):
        self.qualify(); calls = []
        def authorize():
            calls.append(True)
            if len(calls) == 3:
                self.clock.advance(1.5)
        with patch('blackoutburst_agents.dev_test_supervisor.time.monotonic', self.clock), \
                patch('blackoutburst_agents.dev_test_supervisor._control_clock', self.clock), \
                patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_authorize', side_effect=authorize), \
                patch.object(self.backend, '_launch') as launch, self.assertRaises(DevelopmentError):
            self.backend.run(self.request)
        launch.assert_not_called()
        self.assertEqual(self.backend.lookup_not_started(self.request)['reason'], 'supervisor_authorization_expired')

    def prepared_packet(self, *, mark_entered):
        class Prepared(BaseException):
            pass
        observed = []
        def pause(packet):
            if mark_entered:
                self.backend._mark_launch()
                observed.append(packet)
                raise Prepared()
            observed.append(packet)
            raise DevelopmentError('permission_revoked')
        expected = Prepared if mark_entered else DevelopmentError
        with patch.object(self.backend, '_launch', side_effect=pause), self.assertRaises(expected):
            self.backend.run(self.request)
        return self.fixture.directory / observed[0]

    def test_initial_wait_precedes_every_canary_and_source(self):
        self.qualify()
        packet = self.prepared_packet(mark_entered=True)
        control = Mock()
        control.wait_initial.return_value = False
        with patch('blackoutburst_agents.dev_test_supervisor._ControlPipe', return_value=control), \
                patch('blackoutburst_agents.dev_test_supervisor._SignalCanary') as canary, \
                patch('blackoutburst_agents.dev_test_supervisor.os.getpgrp', side_effect=os.getpid), \
                patch('blackoutburst_agents.dev_test_supervisor.SupervisedNativeNamedPythonBackend._execute') as execute:
            self.assertEqual(supervise_packet(packet), 0)
        control.wait_initial.assert_called_once()
        canary.assert_not_called(); execute.assert_not_called()
        value = json.loads((self.fixture.directory / (self.request.operation_id + '.json')).read_text())
        self.assertEqual(value['result']['status'], 'cancelled')
        self.assertIs(value['result']['cleanup_confirmed'], True)
        self.assertIsNone(self.backend.lookup_not_started(self.request))  # Helper entry was already crossed.

    def test_stale_prepared_packet_cannot_revive_terminal_nonlaunch(self):
        self.qualify()
        packet = self.prepared_packet(mark_entered=False)
        self.assertIsNotNone(self.backend.lookup_not_started(self.request))
        with patch('blackoutburst_agents.dev_test_supervisor._ControlPipe') as control, \
                patch('blackoutburst_agents.dev_test_supervisor._SignalCanary') as canary:
            self.assertEqual(supervise_packet(packet), 78)
        control.return_value.wait_initial.assert_not_called()
        canary.assert_not_called()

    def exercise_parent(self, *, slow_refresh=False, slow_launch=False):
        from blackoutburst_agents.dev_test_backend import _receipt
        self.qualify()
        events, packets = [], []
        original_write = os.write
        helper = supervisor_fixture._Helper(callback=lambda: self.complete(packets[0],
            _receipt('cancelled', cleanup=True), {}))
        self.addCleanup(helper.stdin.close)
        sent = []
        def launch(packet):
            packets.append(packet)
            if slow_launch:
                self.clock.advance(1.5)
            return helper
        def authorize():
            events.append(True)
            self.clock.advance(1.5 if slow_refresh and len(events) == 4 else 0.1)
            if not slow_refresh and not slow_launch and len(events) == 5:
                self.complete(packets[0])
        def write(descriptor, raw):
            if descriptor == helper.stdin.write:
                sent.append((json.loads(raw), len(events)))
            return original_write(descriptor, raw)
        with patch('blackoutburst_agents.dev_test_supervisor.time.monotonic', self.clock), \
                patch('blackoutburst_agents.dev_test_supervisor._control_clock', self.clock), \
                patch('blackoutburst_agents.dev_test_supervisor.time.sleep', side_effect=self.clock.advance), \
                patch('blackoutburst_agents.dev_test_supervisor.os.write', side_effect=write), \
                patch.object(self.backend, 'qualified_for', return_value=True), \
                patch.object(self.backend, '_authorize', side_effect=authorize), \
                patch.object(self.backend, '_launch', side_effect=launch):
            result = self.backend.run(self.request)
        return result, sent, events

    def test_every_parent_refresh_has_its_own_complete_collection(self):
        result, sent, events = self.exercise_parent()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual([(frame['sequence'], count) for frame, count in sent], [(1, 3), (2, 4), (3, 5)])
        self.assertEqual(len(events), 5)
        self.assertTrue(all(frame['deadline'] == frame['started_at'] + 1.5 for frame, _ in sent))

    def test_slow_refresh_closes_pipe_without_sending_an_unvalidated_lease(self):
        result, sent, events = self.exercise_parent(slow_refresh=True)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIs(result['cleanup_confirmed'], True)
        self.assertEqual([frame['sequence'] for frame, _ in sent], [1])
        self.assertEqual(len(events), 4)
        self.assertTrue(self.backend.lookup_not_started(self.request) is None)

    def test_popen_delay_consuming_initial_window_sends_nothing(self):
        result, sent, events = self.exercise_parent(slow_launch=True)
        self.assertEqual(result['status'], 'cancelled')
        self.assertIs(result['cleanup_confirmed'], True)
        self.assertEqual(sent, [])
        self.assertEqual(len(events), 3)


if __name__ == '__main__':
    unittest.main()
