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
        plan = {'runtime': {}, 'tools': {'/usr/bin/python3.10': {}, '/usr/bin/unshare': {}},
                'limits': probe.LIMITS, 'seccomp_sha256': 'd' * 64}
        request = {'protocol': probe.PROTOCOL, 'job_uuid': 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
            'nonce': 'f' * 32, 'case': 'normal', 'journal': probe.directory_pin(self.root), 'plan': plan,
            'foundation_sha256': base_hash, 'driver_sha256': 'c' * 64,
            'runtime_sha256': probe.digest(probe.encoded(plan['runtime'])), 'outside_pin': {}}
        self.job = {'request': request, 'request_sha256': probe.digest(probe.encoded(request)),
                    'plan_sha256': probe.digest(probe.encoded(plan))}
        self.admission = {**probe.bindings(self.job), 'phase': 'source_release_intent',
                          'namespace_identity': {'synthetic': True}, 'work_input_pin': {}}
        self.observation = {'cleanup': {'cleanup_confirmed': True}, 'outside_canary_unchanged': True,
            'outcome': 'completed', 'source_ack_written': True,
            'namespace_identity': self.admission['namespace_identity'],
            'admission_sha256': probe.digest(probe.encoded(self.admission))}
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
        observation.update(outcome='not_started', source_ack_written=False, admission_sha256=None)
        changed['result_sha256'] = probe.digest(probe.encoded(changed['result']))
        state = {**self.state, 'receipt_sha256': probe.digest(probe.encoded(changed))}
        self.assertIs(probe.validate_receipt(changed, state, self.job, None), changed)
        with self.assertRaises(probe.RecoveryError):
            probe.validate_receipt(changed, state, self.job, self.admission)

    def test_source_result_nonce_phase_hashes_and_check_types_are_strict(self):
        frame = {**probe.bindings(self.job), 'phase': 'result',
                 'checks': dict.fromkeys(self.base.REQUIRED_CHECKS, True), 'errors': {}}
        self.assertIs(probe.validate_frame(frame, self.job), frame)
        for changed in ({**frame, 'nonce': 'wrong'}, {**frame, 'phase': 'terminal'},
                        {**frame, 'checks': {}}, {**frame, 'extra': 'not allowed'}):
            with self.assertRaises(probe.RecoveryError):probe.validate_frame(changed, self.job)

    def test_duplicate_or_nonfinite_json_is_refused(self):
        for text in (b'{"nonce":"a","nonce":"b"}', b'{"n":NaN}'):
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


if __name__ == '__main__':
    unittest.main()
