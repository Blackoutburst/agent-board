"""Real entry/coordinator/Board composition with synthetic installation boundaries."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_message_local_abandonment as fixture
from deploy.isolated_host import owner_entry as entry
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.message_admission_seal import MessageAdmissionSealClient
from blackoutburst_agents.server_control_client import ServerControlClient
from blackoutburst_agents.host_config import digest


class MemoryControl(ServerControlClient):
    def __init__(self, pilot):
        self.pilot = pilot; self.calls = []; self.session_id = 'fixed-server-control'
        self._config = {'mcp_url': 'https://board.test/mcp'}
    def _initialize(self):
        pass
    def close(self):
        pass
    def _tool(self, name, args):
        self.calls.append((name, deepcopy(args)))
        return getattr(self.pilot.board, name)(self.pilot.raw, **args)


class OwnerRecoveryCompositionTests(unittest.TestCase):
    def test_full_closed_message_then_real_human_stop_and_catalogue_entry(self):
        pilot = fixture.MessageLocalAbandonmentTests()
        pilot.setUp(); self.addCleanup(pilot.doCleanups)
        limits = pilot.board.get_runtime(pilot.human)['limits']
        pilot.board.update_runtime_limits(pilot.human, limits['version'], 8, False)
        remote = pilot.board.create_server_host(pilot.human, pilot.raw['device_id'],
            'Synthetic protected owner', 'composition-manager')
        published = pilot.board.publish_server_catalog(pilot.raw, remote['id'], 0,
            pilot.catalog, 'composition-original-catalog')
        instance = published['instances'][0]
        pilot.board.report_instance_state(pilot.raw, host_id=remote['id'], instance_id=instance['id'],
            expected_observation_revision=instance['observation_revision'], generation=instance['generation'],
            launchspec_sha256=instance['launchspec_sha256'], command_id=instance['command_id'],
            state='stopped', cleanup_confirmed=True, reason='stopped', idempotency_key='synthetic-initial-stop')
        pilot.board.command_server_instance(pilot.human, instance['id'], 'start', instance['version'],
            instance['generation'], instance['launchspec_sha256'], 1, 'synthetic-historical-start')
        instance = pilot.board.poll_instance_commands(pilot.raw, remote['id'])['instances'][0]
        # Seed the fixture's historical local identifiers before creating any
        # abandonment proof. Only synthetic setup uses SQL; both recoveries
        # below use their actual guarded product methods.
        with pilot.locked():
            pilot.host._db.execute('UPDATE managed_settings SET host_id=?', (remote['id'],))
            pilot.host._db.execute('UPDATE managed_targets SET instance_id=?', (instance['id'],))
            row = pilot.host._db.execute('SELECT * FROM managed_commands').fetchone()
            payload = json.loads(row['payload']); payload['instance_id'] = instance['id']
            pilot.host._db.execute('UPDATE managed_commands SET payload=?,payload_sha256=? WHERE id=?',
                (json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False), digest(payload), row['id']))
            closed = pilot.admin.resolve(pilot.plan())
        pilot.board.command_server_instance(pilot.human, instance['id'], 'stop', instance['version'],
            instance['generation'], instance['launchspec_sha256'], 0, 'composition-human-stop')
        control = pilot.host.control(); dispatch = pilot.host.dispatches()[0]; host_path = pilot.host.directory
        old_id, new_id = 'b' * 64, 'c' * 64
        args = SimpleNamespace(action='managed-catalog-source', operation='plan',
            owner=entry.OWNER_NAMES[0], release=Path('/synthetic/protected/releases') / new_id,
            old_release_id=old_id, config_sha256=control['hash'], host_instance=control['instance_id'],
            target_id=dispatch['target_id'], batch_id=dispatch['batch_id'], worker_id=dispatch['worker_id'],
            device_id=pilot.raw['device_id'], request_key='explicit-catalogue-composition',
            abandon_request_key='explicit-abandon-one', proposal_sha256=None, expected_revision=None, intent_id=None)
        manifest = {'architecture': 'x86_64', 'python': {'path': '/synthetic/python'},
            'owners': {args.owner: 'owner'}, 'board_endpoint': 'https://board.test/mcp',
            'codex_version': 'fixture-version', 'files': {'tools/codex': {'sha256': 'a'},
                                                        'tools/codex-resources/bwrap': {'sha256': 'b'}}}
        original_closure = [tuple(row) for row in pilot.host._db.execute('SELECT * FROM host_message_abandonments')]
        pilot.messages.close(); pilot.message_runs.close(); pilot.message_runtime.close(); pilot.host.close()
        pilot.seal.close = lambda: None
        client = MemoryControl(pilot)
        with patch.object(entry, 'paths', return_value=(pilot.root, pilot.root / 'unused', host_path)), \
                patch.object(entry, 'verify_release', return_value=manifest), \
                patch.object(entry, 'manifest_id', return_value=new_id), \
                patch.object(entry, 'check_configuration', side_effect=lambda config, *_: config), \
                patch.object(entry, '_recovery_installed_units'), \
                patch.object(entry, '_recovery_services_stopped'), \
                patch('blackoutburst_agents.host_journal.canonical_directory', return_value=pilot.root / 'locks'), \
                patch.object(MessageAdmissionSealClient, 'from_connection_file', return_value=pilot.seal), \
                patch.object(ServerControlClient, 'from_connection_file', return_value=client):
            planned = io.StringIO()
            with redirect_stdout(planned):
                self.assertEqual(entry.execute(args, manifest), 0)
            self.assertEqual(client.calls, [])
            args.operation = 'apply'; args.expected_revision = 0
            reviewed_sha = json.loads(planned.getvalue())['proposal_sha256']
            args.proposal_sha256 = '0' * 64
            rejected = io.StringIO()
            with redirect_stdout(rejected), self.assertRaisesRegex(entry.PackageError, 'recovery_proposal_changed'):
                entry.execute(args, manifest)
            self.assertEqual(rejected.getvalue(), ''); self.assertEqual(client.calls, [])
            args.proposal_sha256 = reviewed_sha
            applied = io.StringIO()
            with redirect_stdout(applied):
                self.assertEqual(entry.execute(args, manifest), 0)
            result = json.loads(applied.getvalue())
            self.assertEqual((result['phase'], result['revision'], result['model_calls']), ('complete', 4, 0))
            args.operation = 'status'; status = io.StringIO()
            with redirect_stdout(status):
                self.assertEqual(entry.execute(args, manifest), 0)
            self.assertEqual(json.loads(status.getvalue()), result)
            prior_calls = len(client.calls)
            args.operation = 'resume'; args.expected_revision = 1
            rejected = io.StringIO()
            with redirect_stdout(rejected), self.assertRaisesRegex(Exception, 'catalog_transition_revision_conflict'):
                entry.execute(args, manifest)
            self.assertEqual(rejected.getvalue(), ''); self.assertEqual(len(client.calls), prior_calls)
        self.assertEqual(pilot.calls, 0)
        self.assertEqual(len(pilot.seal.calls), 1)
        self.assertEqual(pilot.remote_rows(), pilot.old_runtime)
        self.assertEqual(sum(name == 'replace_server_catalog' for name, _ in client.calls), 1)
        self.assertNotIn('publish_server_catalog', [name for name, _ in client.calls])
        self.assertFalse(any(name == 'report_instance_state' and values['state'] != 'stopped'
                             for name, values in client.calls))
        observed = pilot.board.poll_instance_commands(pilot.raw, remote['id'])['instances'][0]
        self.assertEqual((observed['id'], observed['desired_state'], observed['observed_state']),
                         (instance['id'], 'stopped', 'unknown'))
        with HostJournal(host_path) as host:
            self.assertEqual([tuple(row) for row in host._db.execute('SELECT * FROM host_message_abandonments')],
                             original_closure)
            self.assertEqual(host.control(), control)
            self.assertEqual(host.managed_target(dispatch['target_id'])['remaining'], 0)
        for text in (planned.getvalue(), applied.getvalue(), status.getvalue()):
            self.assertNotIn('PRIVATE', text); self.assertNotIn('test-secret', text)

    def test_reviewed_entry_resolves_exact_legacy_attempt_with_one_real_board_seal(self):
        # Unix/source/unit boundaries have separate real boundary tests. Here
        # only those boundaries are synthetic; journal/coordinator/client/Board
        # validation and every durable transition are the production methods.
        pilot = fixture.MessageLocalAbandonmentTests()
        pilot.setUp(); self.addCleanup(pilot.doCleanups)
        host_path = pilot.host.directory
        control = pilot.host.control(); dispatch = pilot.host.dispatches()[0]
        old_id, new_id = 'b' * 64, 'c' * 64
        args = SimpleNamespace(action='managed-message-abandon', operation='plan',
            owner=entry.OWNER_NAMES[0], release=Path('/synthetic/protected/releases') / new_id,
            old_release_id=old_id, config_sha256=control['hash'], host_instance=control['instance_id'],
            target_id=dispatch['target_id'], batch_id=dispatch['batch_id'], worker_id=dispatch['worker_id'],
            device_id=pilot.raw['device_id'], request_key='explicit-entry-composition',
            proposal_sha256=None, expected_revision=None, intent_id=None, retry_seal=False)
        manifest = {'architecture': 'x86_64', 'python': {'path': '/synthetic/python'},
            'owners': {args.owner: 'owner'}, 'board_endpoint': 'https://board.test/mcp',
            'codex_version': 'fixture-version', 'files': {'tools/codex': {'sha256': 'a'},
                                                        'tools/codex-resources/bwrap': {'sha256': 'b'}}}
        pilot.messages.close(); pilot.message_runs.close(); pilot.message_runtime.close(); pilot.host.close()
        pilot.seal.close = lambda: None
        stores = [host_path / name for name in ('host.sqlite3', 'messages/messages.sqlite3',
            'message-runs/runs.sqlite3', 'message-runtime/runtime.sqlite3')]
        before = [path.read_bytes() for path in stores]
        with patch.object(entry, 'paths', return_value=(pilot.root, pilot.root / 'unused', host_path)), \
                patch.object(entry, 'verify_release', return_value=manifest), \
                patch.object(entry, 'manifest_id', return_value=new_id), \
                patch.object(entry, 'check_configuration', side_effect=lambda config, *_: config), \
                patch.object(entry, '_recovery_installed_units'), \
                patch.object(entry, '_recovery_services_stopped'), \
                patch('blackoutburst_agents.host_journal.canonical_directory', return_value=pilot.root / 'locks'), \
                patch.object(MessageAdmissionSealClient, 'from_connection_file', return_value=pilot.seal):
            plan = io.StringIO()
            with redirect_stdout(plan):
                self.assertEqual(entry.execute(args, manifest), 0)
            self.assertEqual([path.read_bytes() for path in stores], before)
            self.assertEqual(pilot.seal.calls, [])
            args.operation = 'apply'; args.expected_revision = 0
            args.proposal_sha256 = json.loads(plan.getvalue())['proposal_sha256']
            applied = io.StringIO()
            with redirect_stdout(applied):
                self.assertEqual(entry.execute(args, manifest), 0)
            result = json.loads(applied.getvalue())
            self.assertEqual((result['phase'], result['model_calls'], result['activation_changed']),
                             ('complete', 0, False))
            args.operation = 'status'
            status = io.StringIO()
            with redirect_stdout(status):
                self.assertEqual(entry.execute(args, manifest), 0)
            self.assertEqual(json.loads(status.getvalue()), result)
        self.assertEqual(len(pilot.seal.calls), 1)
        self.assertEqual(pilot.calls, 0)
        self.assertEqual(pilot.remote_rows(), pilot.old_runtime)
        for text in (plan.getvalue(), applied.getvalue(), status.getvalue()):
            self.assertNotIn('PRIVATE', text); self.assertNotIn('test-secret', text)
        with HostJournal(host_path) as host, MessageJournal(host_path / 'messages') as messages, \
                RuntimeJournal(host_path / 'message-runtime') as runtime:
            runs = RunStore(host_path / 'message-runs', existing_only=True)
            try:
                self.assertEqual(host.control(), control)
                self.assertEqual(host.dispatches()[0]['receipt']['status'], 'abandoned')
                self.assertEqual(messages.operation(dispatch['batch_id'])['state'], 'abandoned')
                self.assertEqual(runtime.list_status()[0]['phase'], 'abandoned_never_started')
                self.assertEqual(runs._db.execute('SELECT status FROM works').fetchone()[0], 'closed')
            finally:
                runs.close()


if __name__ == '__main__':
    unittest.main()
