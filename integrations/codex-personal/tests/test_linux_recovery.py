"""Pure receipt/protocol tests. No native namespace, subprocess, model or account."""
from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location('linux_recovery_probe',
    Path(__file__).with_name('qualify_linux_recovery.py'))
probe = importlib.util.module_from_spec(_spec);_spec.loader.exec_module(probe)


class LinuxRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve();self.root.chmod(0o700)
        base, base_hash = probe.foundation()
        self.base = base
        runtime_pin = {'identity': [7, 8], 'size': 10, 'sha256': 'a' * 64}
        plan = {'runtime': {'/usr/bin/python3.10': runtime_pin},
                'tools': {'/usr/bin/python3.10': runtime_pin, '/usr/bin/unshare': runtime_pin},
                'limits': probe.LIMITS, 'seccomp_sha256': 'd' * 64}
        request = {'protocol': probe.PROTOCOL, 'job_uuid': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'nonce': 'f' * 32, 'case': 'normal', 'journal': probe.directory_pin(self.root), 'plan': plan,
            'foundation_sha256': base_hash, 'driver_sha256': 'c' * 64,
            'runtime_sha256': probe.digest(probe.encoded(plan['runtime'])),
            'outside_pin': {'identity': [1, 2], 'size': 9, 'sha256': probe.digest(b'synthetic')}}
        self.job = {'request': request, 'request_sha256': probe.digest(probe.encoded(request)),
                    'plan_sha256': probe.digest(probe.encoded(plan))}
        namespace = {'acquired_by': 'SCM_RIGHTS_from_trusted_init', 'kernel_fdinfo_nspid': [100, 1],
            'host_pid_namespace_identity': [1, 3], 'init_pid_namespace_identity': [1, 4],
            'init_is_exact_launcher_child': True, 'namespace_numbers_used_for_host_signals': False}
        self.admission = {**probe.bindings(self.job), 'phase': 'source_release_intent',
                          'namespace_identity': namespace,
                          'work_input_pin': {'identity': [5, 2], 'size': 9, 'sha256': probe.digest(b'synthetic')}}
        frame = {**probe.bindings(self.job), 'phase': 'source_started',
            'checks': dict.fromkeys(probe.REQUIRED_CHECKS, True),
            'errors': {**dict.fromkeys(probe.ERROR_CHECKS, 1), 'outside_read': 2}}
        final = {**frame, 'phase': 'result'}
        self.observation = {'cleanup': {'cleanup_confirmed': True,
            'kernel_init_identity_received': True, 'namespace_init_exited': True, 'launcher_reaped': True,
            'term_sent_to_init_pidfd': False, 'init_terminal_after_term': None,
            'namespace_pid_numbers_used_for_signals': False}, 'outside_canary_unchanged': True,
            'outcome': 'completed', 'reason': 'completed', 'source_ack_written': True,
            'namespace_identity': namespace, 'admission_sha256': probe.digest(probe.encoded(self.admission)),
            'source_begin_frame': frame, 'source_result_frame': final,
            'source_begin_sha256': probe.digest(probe.encoded(frame)),
            'source_frame_sha256': probe.digest(probe.encoded(final)), 'source_checks': frame['checks'],
            'output_bytes': len(probe.encoded(frame)) + len(probe.encoded(final)) + 2,
            'outside_canary_before': request['outside_pin'], 'outside_canary_after': request['outside_pin'],
            'elapsed_seconds': 0.2}
        result = {**probe.bindings(self.job), 'phase': 'terminal', 'observation': self.observation,
                  'namespace_launches': 1}
        self.receipt = {'result': result, 'result_sha256': probe.digest(probe.encoded(result))}
        self.state = {'phase': 'terminal', 'namespace_launches': 1,
            'receipt_sha256': probe.digest(probe.encoded(self.receipt)),
            'admission_sha256': probe.digest(probe.encoded(self.admission))}

    def tearDown(self):
        self.temp.cleanup()

    def save(self, state=None):
        for name, value in (('job', self.job), ('state', state or self.state),
                            ('receipt', self.receipt), ('admission', self.admission)):
            probe.new_file(self.root / (name + '.json'), probe.encoded(value))

    def test_exact_terminal_receipt_is_consumed_without_effects_or_pid_lookup(self):
        self.save()
        with patch.object(probe.subprocess, 'Popen') as launch, patch.object(probe, 'foundation') as code, \
                patch.object(probe.os, 'kill') as kill:
            first = probe.inspect(self.root);second = probe.controller(self.root)
        self.assertEqual(first, second)
        self.assertFalse(first['backend_execution_qualified'])
        self.assertEqual(first['new_namespace_launches'], 0)
        launch.assert_not_called();code.assert_not_called();kill.assert_not_called()

    def test_open_launch_intent_cannot_start_a_second_helper(self):
        self.save({'phase': 'launch_intent', 'namespace_launches': 0})
        with patch.object(probe.subprocess, 'Popen') as launch:
            with self.assertRaises(probe.RecoveryError):probe.controller(self.root)
        launch.assert_not_called()

    def test_receipt_requires_exact_durable_admission(self):
        for admission in (None, {**self.admission, 'nonce': 'wrong'},
                          {**self.admission, 'phase': 'prepared'}):
            with self.subTest(admission=admission):
                with self.assertRaises(probe.RecoveryError):
                    probe.validate_receipt(self.receipt, self.state, self.job, admission)

    def test_every_identity_component_is_checked_even_with_recomputed_hashes(self):
        for key in probe.bindings(self.job):
            changed = deepcopy(self.receipt);changed['result'][key] = 'other'
            changed['result_sha256'] = probe.digest(probe.encoded(changed['result']))
            state = {**self.state, 'receipt_sha256': probe.digest(probe.encoded(changed))}
            with self.subTest(key=key), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)

    def test_uncertain_cleanup_or_release_never_becomes_clean_non_dispatch(self):
        for key, value in (('outcome', 'release_uncertain'), ('source_ack_written', False),
                           ('cleanup', {'cleanup_confirmed': False})):
            changed = deepcopy(self.receipt);changed['result']['observation'][key] = value
            changed['result_sha256'] = probe.digest(probe.encoded(changed['result']))
            state = {**self.state, 'receipt_sha256': probe.digest(probe.encoded(changed))}
            with self.subTest(key=key), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)

    def test_refusal_before_source_must_have_no_release_admission(self):
        changed = deepcopy(self.receipt);observation = changed['result']['observation']
        observation.update(outcome='not_started', reason='explicit_synthetic_stop',
            source_ack_written=False, admission_sha256=None, source_begin_frame=None, source_result_frame=None,
            source_begin_sha256=None, source_frame_sha256=None, source_checks=None, output_bytes=0)
        changed['result_sha256'] = probe.digest(probe.encoded(changed['result']))
        state = {k: v for k, v in self.state.items() if k != 'admission_sha256'}
        state['receipt_sha256'] = probe.digest(probe.encoded(changed))
        self.assertIs(probe.validate_receipt(changed, state, self.job, None), changed)
        with self.assertRaises(probe.RecoveryError):
            probe.validate_receipt(changed, state, self.job, self.admission)

    def test_source_result_nonce_phase_hashes_and_check_types_are_strict(self):
        frame = {**probe.bindings(self.job), 'phase': 'result',
                 'checks': dict.fromkeys(self.base.REQUIRED_CHECKS, True),
                 'errors': {**dict.fromkeys(probe.ERROR_CHECKS, 1), 'outside_read': 2}}
        self.assertIs(probe.validate_frame(frame, self.job), frame)
        for changed in ({**frame, 'nonce': 'wrong'}, {**frame, 'phase': 'terminal'},
                        {**frame, 'checks': {}}, {**frame, 'extra': 'not allowed'}):
            with self.assertRaises(probe.RecoveryError):probe.validate_frame(changed, self.job)

    def test_duplicate_or_nonfinite_json_is_refused(self):
        for text in (b'{"nonce":"a","nonce":"b"}', b'{"n":NaN}', b'{"n":1e999}'):
            with self.assertRaises(probe.RecoveryError):probe.decoded(text)

    def test_controller_eof_overrides_queued_heartbeats_and_is_sticky(self):
        data = iter([b'GG', b''])
        channel = SimpleNamespace(setblocking=lambda _: None, recv=lambda _: next(data))
        control = probe.Control(channel)
        with self.assertRaisesRegex(probe.RecoveryError, '^controller_eof$'):control.check()

    def test_heartbeat_absence_and_deadline_are_fail_closed(self):
        channel = SimpleNamespace(setblocking=lambda _: None, recv=lambda _: (_ for _ in ()).throw(BlockingIOError()))
        control = probe.Control(channel)
        with self.assertRaises(probe.RecoveryError):control.check()
        control.last = 1
        with patch.object(probe.time, 'monotonic', return_value=2.5):
            with self.assertRaises(probe.RecoveryError):control.check()

    def test_metadata_corruptions_never_launch_or_mutate_the_attempt(self):
        self.save()
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        with patch.object(probe.subprocess, 'Popen') as launch:
            report = probe.corruptions(self.root)
        self.assertTrue(all(report['checks'].values()))
        self.assertEqual(report['new_namespace_launches'], 0)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        launch.assert_not_called()

    def test_changed_journal_inode_is_not_adopted(self):
        self.save()
        changed = deepcopy(self.job);changed['request']['journal']['identity'][1] += 1
        changed['request_sha256'] = probe.digest(probe.encoded(changed['request']))
        (self.root / 'job.json').unlink();probe.new_file(self.root / 'job.json', probe.encoded(changed))
        with self.assertRaises(probe.RecoveryError):probe.load_job(self.root)

    def test_changed_limits_cannot_be_sealed_by_rehashing_the_job(self):
        self.save()
        changed = deepcopy(self.job);changed['request']['plan']['limits']['test_seconds'] = 300
        changed['request_sha256'] = probe.digest(probe.encoded(changed['request']))
        changed['plan_sha256'] = probe.digest(probe.encoded(changed['request']['plan']))
        (self.root / 'job.json').unlink();probe.new_file(self.root / 'job.json', probe.encoded(changed))
        with self.assertRaises(probe.RecoveryError):probe.load_job(self.root)


    def rehash(self, receipt=None, state=None):
        changed = deepcopy(self.receipt if receipt is None else receipt)
        changed['result_sha256'] = probe.digest(probe.encoded(changed['result']))
        changed_state = deepcopy(self.state if state is None else state)
        changed_state['receipt_sha256'] = probe.digest(probe.encoded(changed))
        return changed, changed_state

    def test_namespace_launches_are_exact_integers_despite_equal_values(self):
        for value in (True, False, 1.0, 0.0, '1', -1, 2):
            for target in ('state', 'result'):
                changed = deepcopy(self.receipt);state = deepcopy(self.state)
                if target == 'state':state['namespace_launches'] = value
                else:changed['result']['namespace_launches'] = value
                changed, state = self.rehash(changed, state)
                with self.subTest(value=value, target=target), self.assertRaises(probe.RecoveryError):
                    probe.validate_receipt(changed, state, self.job, self.admission)
        for phase in ('prepared', 'launch_intent', 'helper_started'):
            for value in (False, 0.0):
                with self.subTest(phase=phase, value=value), self.assertRaises(probe.RecoveryError):
                    probe.validate_state({'phase': phase, 'namespace_launches': value})

    def test_completed_cannot_drop_any_observation_or_cleanup_field(self):
        for key in self.observation:
            changed = deepcopy(self.receipt);del changed['result']['observation'][key]
            changed, state = self.rehash(changed)
            with self.subTest(key=key), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)
        for key in self.observation['cleanup']:
            changed = deepcopy(self.receipt);del changed['result']['observation']['cleanup'][key]
            changed, state = self.rehash(changed)
            with self.subTest(cleanup=key), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)
        changed = deepcopy(self.receipt)
        changed['result']['observation']['cleanup'] = {'cleanup_confirmed': True}
        changed, state = self.rehash(changed)
        with self.assertRaises(probe.RecoveryError):
            probe.validate_receipt(changed, state, self.job, self.admission)

    def test_cleanup_claim_requires_both_independent_terminal_observations(self):
        for field in ('kernel_init_identity_received', 'namespace_init_exited', 'launcher_reaped'):
            for value in (False, 1, 'true'):
                changed = deepcopy(self.receipt);changed['result']['observation']['cleanup'][field] = value
                changed, state = self.rehash(changed)
                with self.subTest(field=field, value=value), self.assertRaises(probe.RecoveryError):
                    probe.validate_receipt(changed, state, self.job, self.admission)

    def test_completed_requires_exact_frames_hashes_and_successful_checks(self):
        changes = [('source_begin_frame', None), ('source_result_frame', None),
            ('source_frame_sha256', None), ('source_begin_sha256', 'f' * 64),
            ('source_checks', {}), ('output_bytes', 0), ('reason', 'controller_eof')]
        for key, value in changes:
            changed = deepcopy(self.receipt);changed['result']['observation'][key] = value
            changed, state = self.rehash(changed)
            with self.subTest(key=key), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)
        changed = deepcopy(self.receipt);observation = changed['result']['observation']
        observation['source_result_frame']['checks']['inside_write'] = False
        observation['source_frame_sha256'] = probe.digest(probe.encoded(observation['source_result_frame']))
        changed, state = self.rehash(changed)
        with self.assertRaises(probe.RecoveryError):
            probe.validate_receipt(changed, state, self.job, self.admission)

    def test_cancel_before_first_frame_is_valid_but_not_success_or_non_dispatch(self):
        changed = deepcopy(self.receipt);observation = changed['result']['observation']
        observation.update(outcome='cancelled', reason='controller_eof', source_begin_frame=None,
            source_result_frame=None, source_frame_sha256=None, source_begin_sha256=None,
            source_checks=None, output_bytes=0)
        changed, state = self.rehash(changed)
        self.assertIs(probe.validate_receipt(changed, state, self.job, self.admission), changed)
        for outcome in ('completed', 'not_started', 'release_uncertain'):
            other = deepcopy(changed);other['result']['observation']['outcome'] = outcome
            other, altered = self.rehash(other, state)
            with self.subTest(outcome=outcome), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(other, altered, self.job, self.admission)

    def test_numbers_booleans_and_metadata_shapes_are_not_coerced(self):
        for field, value in (('output_bytes', True), ('output_bytes', 1.0), ('output_bytes', -1),
                ('output_bytes', probe.MAX_OUTPUT + 1), ('elapsed_seconds', True),
                ('elapsed_seconds', -0.1), ('elapsed_seconds', '0.2'),
                ('elapsed_seconds', 10 ** 500), ('outside_canary_unchanged', 1),
                ('source_ack_written', 1), ('reason', [])):
            changed = deepcopy(self.receipt);changed['result']['observation'][field] = value
            changed, state = self.rehash(changed)
            with self.subTest(field=field, value=value), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)
        for field, value in (('namespace_init_exited', 1), ('term_sent_to_init_pidfd', 0),
                ('init_terminal_after_term', False), ('namespace_pid_numbers_used_for_signals', 0)):
            changed = deepcopy(self.receipt);changed['result']['observation']['cleanup'][field] = value
            changed, state = self.rehash(changed)
            with self.subTest(field=field), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)

    def test_admission_requires_typed_namespace_and_synthetic_work_pin(self):
        for field, value in (('namespace_identity', {'synthetic': True}),
                ('work_input_pin', {}), ('work_input_pin', {'identity': [True, 2], 'size': 9,
                    'sha256': probe.digest(b'synthetic')})):
            admission = deepcopy(self.admission);admission[field] = value
            changed = deepcopy(self.receipt);state = deepcopy(self.state)
            changed['result']['observation']['admission_sha256'] = probe.digest(probe.encoded(admission))
            state['admission_sha256'] = probe.digest(probe.encoded(admission))
            changed, state = self.rehash(changed, state)
            with self.subTest(field=field, value=value), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, admission)
        for value in (True, 1.0):
            changed = deepcopy(self.receipt)
            changed['result']['observation']['namespace_identity']['kernel_fdinfo_nspid'][1] = value
            changed, state = self.rehash(changed)
            with self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, self.admission)

    def test_extra_state_admission_and_observation_fields_are_refused(self):
        for target in ('state', 'admission', 'observation'):
            changed = deepcopy(self.receipt);state = deepcopy(self.state);admission = deepcopy(self.admission)
            if target == 'state':state['extra'] = True
            elif target == 'admission':
                admission['extra'] = True
                state['admission_sha256'] = probe.digest(probe.encoded(admission))
                changed['result']['observation']['admission_sha256'] = state['admission_sha256']
            else:changed['result']['observation']['extra'] = True
            changed, state = self.rehash(changed, state)
            with self.subTest(target=target), self.assertRaises(probe.RecoveryError):
                probe.validate_receipt(changed, state, self.job, admission)

    def test_source_frames_require_errno_correspondence_and_typed_errors(self):
        for value in (True, 1.0, -1, 99999, 0):
            frame = deepcopy(self.observation['source_result_frame'])
            frame['errors']['outside_read'] = value
            with self.subTest(value=value), self.assertRaises(probe.RecoveryError):
                probe.validate_frame(frame, self.job)
        self.assertEqual(probe.REQUIRED_CHECKS, self.base.REQUIRED_CHECKS)


    def test_orphan_admission_cannot_be_hidden_by_a_not_started_state(self):
        changed = deepcopy(self.receipt);observation = changed['result']['observation']
        observation.update(outcome='not_started', reason='explicit_synthetic_stop',
            source_ack_written=False, admission_sha256=None, source_begin_frame=None, source_result_frame=None,
            source_begin_sha256=None, source_frame_sha256=None, source_checks=None, output_bytes=0)
        state = {k: v for k, v in self.state.items() if k != 'admission_sha256'}
        self.receipt, self.state = self.rehash(changed, state)
        self.save()
        with patch.object(probe.subprocess, 'Popen') as launch, patch.object(probe, 'foundation') as code:
            with self.assertRaises(probe.RecoveryError):probe.inspect(self.root)
        launch.assert_not_called();code.assert_not_called()
        admission_path = self.root / 'admission.json';admission_path.unlink()
        admission_path.symlink_to(self.root / 'missing-admission')
        with self.assertRaises(probe.RecoveryError):probe.inspect(self.root)

    def test_job_identity_runtime_and_tool_schema_are_checked_before_effects(self):
        changes = [('job_uuid', ''), ('job_uuid', 'AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA'),
            ('nonce', ''), ('nonce', 'G' * 32), ('seccomp_sha256', True),
            ('runtime', []), ('runtime', {}), ('runtime', {'/home/private': {'unsupported': True}}),
            ('tools', {'/usr/bin/python3.10': {'unsupported': True}, '/usr/bin/unshare': {}})]
        for field, value in changes:
            changed = deepcopy(self.job)
            target = changed['request'] if field in ('job_uuid', 'nonce') else changed['request']['plan']
            target[field] = value
            changed['request']['runtime_sha256'] = probe.digest(probe.encoded(changed['request']['plan']['runtime']))
            changed['request_sha256'] = probe.digest(probe.encoded(changed['request']))
            changed['plan_sha256'] = probe.digest(probe.encoded(changed['request']['plan']))
            path = self.root / 'job.json'
            if path.exists():path.unlink()
            probe.new_file(path, probe.encoded(changed))
            with self.subTest(field=field, value=value), self.assertRaises(probe.RecoveryError):
                probe.load_job(self.root)


    def test_concurrent_controller_cannot_launch_second_helper(self):
        self.save({'phase': 'prepared', 'namespace_launches': 0})
        # save() includes receipt/admission fixtures; a terminal fake helper only
        # publishes their exact state. No child process or namespace is created.
        nested = []
        def fake_launch(*args, **kwargs):
            with self.assertRaises(probe.RecoveryError):
                probe.controller(self.root)
            with self.assertRaisesRegex(probe.RecoveryError, 'controller_or_helper_busy'):
                probe.private_lock(self.root, 'controller.lock')
            nested.append('refused')
            probe.replace_state(self.root, self.state)
            os.write(kwargs['pass_fds'][0], b'{"phase":"finished"}\n')
            return SimpleNamespace(wait=lambda **_: 0, poll=lambda: None)
        with patch.object(probe, 'foundation', return_value=(SimpleNamespace(limits=lambda _: None, PYTHON='/not-executed'), 'x')), \
                patch.object(probe, 'verify_sources'), patch.object(probe.subprocess, 'Popen', side_effect=fake_launch) as launch:
            first = probe.controller(self.root)
        self.assertEqual(nested, ['refused']);self.assertEqual(launch.call_count, 1)
        with patch.object(probe.subprocess, 'Popen') as launch:
            self.assertEqual(probe.controller(self.root), first)
        launch.assert_not_called()

    def test_stale_prepared_read_is_rechecked_under_lock(self):
        self.save({'phase': 'prepared', 'namespace_launches': 0})
        original = probe.private_lock
        def completed_before_lock(directory, name):
            probe.replace_state(self.root, self.state)
            return original(directory, name)
        with patch.object(probe, 'private_lock', side_effect=completed_before_lock), \
                patch.object(probe.subprocess, 'Popen') as launch:
            result = probe.controller(self.root)
        self.assertEqual(result['receipt_sha256'], probe.digest(probe.encoded(self.receipt)))
        self.assertEqual(probe.decoded(probe.read_file(self.root / 'state.json')), self.state)
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
