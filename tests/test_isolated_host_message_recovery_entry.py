"""Finite owner entry boundaries using real private journals, no remote/model calls."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import subprocess
import sqlite3
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_isolated_host_entry as fixture
from deploy.isolated_host import owner_entry as entry, package as pkg
from blackoutburst_agents.host_config import validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.server_controller import launch_catalog


class RecoveryEntryTests(unittest.TestCase):
    setUp = fixture.EntryTests.setUp
    tearDown = fixture.EntryTests.tearDown
    args = fixture.EntryTests.args
    assertCode = fixture.EntryTests.assertCode
    old_id = 'a' * 64
    new_id = 'b' * 64

    def prepare(self):
        self.config['schema_version'] = 2
        target = self.config['targets'][0]
        for key in ('project_id', 'ticket_id', 'after'):
            del target[key]
        target.update(thread_ids=['thread-a'], start_at='now')
        self.config = validate(self.config, bind_connections=True)
        with HostJournal(self.journal) as host:
            host.configure(self.config)
            with host.owner_lock():
                host.prepare_managed('host-a', launch_catalog(self.config, self.old_id))
            self.control = host.control()
        with MessageJournal(self.journal / 'messages'):
            pass
        runs = RunStore(self.journal / 'message-runs'); runs.close()
        with RuntimeJournal(self.journal / 'message-runtime'):
            pass
        self.manifest['files'] = {'tools/codex': {'sha256': 'x', 'size': 1},
                                 'tools/codex-resources/bwrap': {'sha256': 'y', 'size': 1}}
        self.old_manifest = deepcopy(self.manifest)
        return self.args('managed-message-abandon', operation='plan', old_release_id=self.old_id,
            config_sha256=self.control['hash'], host_instance=self.control['instance_id'],
            target_id='one', batch_id='batch-a', worker_id='worker-a', device_id='device-a',
            request_key='explicit-abandon-a', proposal_sha256=None, expected_revision=None,
            intent_id=None, retry_seal=False)

    def stopped(self, argv, **kwargs):
        self.assertEqual(argv[0], '/usr/bin/systemctl')
        self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin'})
        return subprocess.CompletedProcess(argv, 0, stdout=b'LoadState=loaded\nActiveState=inactive\n'
            b'SubState=dead\nUnitFileState=disabled\nMainPID=0\nControlPID=0\nDropInPaths=\n'
            + ('FragmentPath=' + str(entry.UNIT_DIR / argv[-1]) + '\nNames=' + argv[-1] + '\n').encode())

    def invoke(self, args, *, coordinator=None, service=None):
        factory = MagicMock(return_value=coordinator or MagicMock())
        module = SimpleNamespace(MessageAdmissionAbandonment=factory)
        output = io.StringIO()
        with patch.dict(sys.modules, {'blackoutburst_agents.message_admission_abandonment': module}), \
                patch.object(entry, 'verify_release', return_value=self.old_manifest), \
                patch.object(entry, 'manifest_id', return_value=self.new_id), \
                patch.object(entry, '_recovery_installed_units'), \
                patch.object(entry.subprocess, 'run', side_effect=service or self.stopped), \
                patch('urllib.request.urlopen') as network, patch('subprocess.Popen') as model, \
                redirect_stdout(output):
            result = entry.execute(args, self.manifest)
        network.assert_not_called(); model.assert_not_called()
        return result, factory, output.getvalue()

    def test_missing_database_refuses_before_any_constructor_can_create_authority(self):
        args = self.prepare()
        path = self.journal / 'message-runtime/runtime.sqlite3'; path.unlink()
        self.assertCode('private_path_unavailable', lambda: self.invoke(args))
        self.assertFalse(path.exists())

    def test_unexpected_runstore_version_is_not_implicitly_migrated(self):
        args = self.prepare()
        path = self.journal / 'message-runs/runs.sqlite3'
        with sqlite3.connect(path) as database:
            database.execute('PRAGMA user_version=1')
        before = path.read_bytes()
        self.assertCode('recovery_journal_version_requires_review', lambda: self.invoke(args))
        self.assertEqual(path.read_bytes(), before)

    def test_sidecar_symlink_and_weak_private_path_refuse(self):
        args = self.prepare()
        path = self.journal / 'messages/messages.sqlite3-wal'
        path.symlink_to(self.root / 'missing')
        self.assertCode('recovery_journal_sidecar_requires_review', lambda: self.invoke(args))
        path.unlink(); (self.journal / 'messages').chmod(0o755)
        self.assertCode('insecure_owner_path', lambda: self.invoke(args))

    def test_active_or_malformed_service_refuses_without_attempting_stop(self):
        args = self.prepare()
        for output in (b'ActiveState=active\n', b'ActiveState=inactive\nActiveState=inactive\n', b'garbage'):
            def service(argv, **kwargs):
                self.assertEqual(argv[1], 'show')
                return subprocess.CompletedProcess(argv, 0, stdout=output)
            self.assertCode('recovery_requires_stopped_disabled_services',
                            lambda: self.invoke(args, service=service))

    def test_changed_config_instance_catalog_or_connection_refuses(self):
        args = self.prepare()
        args.config_sha256 = 'c' * 64
        self.assertCode('recovery_host_or_catalog_changed', lambda: self.invoke(args))
        args.config_sha256 = self.control['hash']; args.host_instance = 'different'
        self.assertCode('recovery_host_or_catalog_changed', lambda: self.invoke(args))
        args.host_instance = self.control['instance_id']
        value = json.loads(self.connection.read_text()); value['name'] = 'Changed'
        self.connection.write_text(json.dumps(value))
        self.assertCode('recovery_connection_changed', lambda: self.invoke(args))

    def test_changed_old_tool_pin_refuses_source_chain(self):
        args = self.prepare()
        self.old_manifest['files']['tools/codex']['sha256'] = 'changed'
        self.assertCode('recovery_source_chain_changed', lambda: self.invoke(args))

    def test_plan_opens_only_existing_journals_without_rpc_or_outputting_private_context(self):
        args = self.prepare()
        coordinator = MagicMock()
        coordinator.propose.return_value = {'proposal_sha256': 'd' * 64}
        result, factory, output = self.invoke(args, coordinator=coordinator)
        self.assertEqual(result, 0); factory.assert_called_once()
        coordinator.propose.assert_called_once()
        coordinator.resolve.assert_not_called(); coordinator.resume.assert_not_called()
        self.assertEqual(json.loads(output), {'status': 'planned', 'proposal_sha256': 'd' * 64,
            'activation_changed': False, 'model_calls': 0})
        self.assertNotIn('PRIVATE-', output)
        with HostJournal(self.journal) as host:
            self.assertEqual(host.control(), self.control)

    def test_apply_requires_reviewed_hash_and_revision_and_does_not_accept_retry_flag(self):
        args = self.prepare(); args.operation = 'apply'
        self.assertCode('recovery_expected_proposal_and_revision_required', lambda: self.invoke(args))
        args.proposal_sha256 = 'd' * 64; args.expected_revision = 0; args.retry_seal = True
        self.assertCode('recovery_retry_requires_explicit_resume', lambda: self.invoke(args))

    def test_apply_passes_only_reviewed_proposal_and_reports_closed_status(self):
        args = self.prepare(); args.operation = 'apply'
        args.proposal_sha256 = 'd' * 64; args.expected_revision = 0
        coordinator = MagicMock()
        proposal = {'proposal_sha256': args.proposal_sha256}
        coordinator.propose.return_value = proposal
        coordinator.status.return_value = {'intent_id': '17449ca4-bab3-4676-9fdb-f2d9f13993b1', 'revision': 7,
                                            'phase': 'complete', 'reason': 'message_abandon_complete'}
        _, _, output = self.invoke(args, coordinator=coordinator)
        coordinator.resolve.assert_called_once_with(proposal, expected_revision=0)
        coordinator.resume.assert_not_called()
        self.assertEqual(json.loads(output)['phase'], 'complete')
        coordinator.reset_mock(); args.proposal_sha256 = 'e' * 64
        self.assertCode('recovery_proposal_changed', lambda: self.invoke(args, coordinator=coordinator))
        coordinator.resolve.assert_not_called()

    def test_resume_binds_stored_proposal_to_all_explicit_pins_before_any_effect(self):
        args = self.prepare(); args.operation = 'resume'; args.intent_id = '17449ca4-bab3-4676-9fdb-f2d9f13993b1'
        args.proposal_sha256 = 'd' * 64; args.expected_revision = 2
        coordinator = MagicMock()
        proposal = {'proposal_sha256': args.proposal_sha256, 'request_key': args.request_key,
            'host': {'target_id': args.target_id, 'configuration_sha256': args.config_sha256,
                     'instance_id': args.host_instance},
            'dispatch': {'target_id': args.target_id, 'batch_id': args.batch_id, 'worker_id': args.worker_id},
            'identity': {'owner_id': 'owner-a', 'agent_id': 'profile-a', 'device_id': args.device_id,
                         'profile_version': 1, 'session_id': 'session-a'}}
        coordinator.proposal.return_value = proposal
        coordinator.status.return_value = {'intent_id': '17449ca4-bab3-4676-9fdb-f2d9f13993b1', 'revision': 2,
                                            'phase': 'intent', 'reason': 'message_abandon_intent'}
        self.invoke(args, coordinator=coordinator)
        coordinator.resume.assert_called_once_with('17449ca4-bab3-4676-9fdb-f2d9f13993b1', expected_revision=2, retry_seal=False)
        for section, field in (('host', 'target_id'), ('host', 'configuration_sha256'),
                ('host', 'instance_id'), ('dispatch', 'target_id'), ('dispatch', 'batch_id'),
                ('dispatch', 'worker_id'), ('identity', 'device_id'), ('identity', 'session_id')):
            with self.subTest(section=section, field=field):
                changed = deepcopy(proposal); changed[section][field] = 'foreign'
                coordinator.proposal.return_value = changed; coordinator.reset_mock()
                self.assertCode('recovery_proposal_changed', lambda: self.invoke(args, coordinator=coordinator))
                coordinator.resume.assert_not_called()

    def test_current_installed_unit_bytes_are_required(self):
        self.prepare()
        expected = 'ExecStart=protected-current-release\n'
        with patch.object(entry, 'read_regular', side_effect=[b'template', b'ExecStart=old-release\n']), \
                patch.object(entry, 'protected'), patch.object(entry, 'render_unit', return_value=expected):
            self.assertCode('recovery_installed_source_changed',
                lambda: entry._recovery_installed_units(self.manifest, self.release))

    def test_closed_grammar_is_manual_only(self):
        with patch.object(entry, 'verify_release') as verify, patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                entry.main(['--release', str(self.release), '--owner', self.username,
                            'managed-message-abandon', 'auto'])
        verify.assert_not_called()

    def test_entry_cannot_mix_a_different_executed_wrapper_and_release(self):
        args = self.prepare()
        argv = ['--release', str(self.release), '--owner', self.username,
                'managed-message-abandon', 'plan']
        for key in ('old_release_id', 'config_sha256', 'host_instance', 'target_id',
                    'batch_id', 'worker_id', 'device_id', 'request_key'):
            argv += ['--' + key.replace('_', '-'), getattr(args, key)]
        output = io.StringIO()
        with patch.object(entry, 'verify_release') as verify, redirect_stdout(output):
            self.assertEqual(entry.main(argv), 2)
        verify.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['reason'], 'recovery_entry_release_mismatch')


if __name__ == '__main__':
    unittest.main()
