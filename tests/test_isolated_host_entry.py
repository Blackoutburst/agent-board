"""Owner boundary and persisted lifecycle tests; all model/network calls mocked."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy.isolated_host import owner_entry as entry, package as pkg

ADAPTER = Path(__file__).resolve().parents[1] / 'integrations/codex-personal'
sys.path.insert(0, str(ADAPTER))
from blackoutburst_agents.host_journal import HostJournal


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='owner-entry-')
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / 'owner'; self.home.mkdir(mode=0o700)
        self.username = pkg.OWNER_NAMES[0]
        self.uid, self.gid = os.geteuid(), os.getegid()
        self.release = self.root / 'release'; (self.release / 'tools').mkdir(parents=True)
        self.codex = self.release / 'tools/codex'; self.codex.write_text('# fake, never executed'); self.codex.chmod(0o755)
        self.manifest = {'architecture': 'x86_64', 'python': {'path': sys.executable},
                         'owners': dict(zip(pkg.OWNER_NAMES, ('owner-a', 'owner-b'))),
                         'board_endpoint': 'https://board.example/mcp'}
        self.connection = self.home / '.config/blackoutburst-board/agent/other.json'
        self.workspace = self.home / 'workspaces/response'
        self.config_path = self.home / '.config/blackoutburst-agent-host/host.json'
        self.journal = self.home / '.local/state/blackoutburst-agents/host'
        for path in (self.connection.parent, self.workspace, self.config_path.parent, self.journal.parent):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in self.home.rglob('*'):
            if path.is_dir():
                path.chmod(0o700)
        self.connection.write_text(json.dumps({'mcp_url': 'https://board.example/mcp', 'client': 'other',
                                               'identity_key': 'fixture-identity', 'name': 'Fixture agent',
                                               'token': 'PRIVATE-CREDENTIAL-MUST-NOT-BE-PRINTED'}))
        self.connection.chmod(0o600)
        self.config = {'schema_version': 1, 'owner_id': 'owner-a', 'board_endpoint': 'https://board.example/mcp',
                       'interval_seconds': 30, 'max_pages': 1, 'page_limit': 50, 'dispatch_budget': 1,
                       'targets': [{'id': 'one', 'agent_id': 'profile-a', 'project_id': 'project-a',
                                    'ticket_id': 'ticket-a', 'board_session_id': 'session-a', 'profile_version': 1,
                                    'connection_file': str(self.connection), 'instruction': 'PRIVATE-INSTRUCTION',
                                    'workspace': str(self.workspace), 'executable': str(self.codex),
                                    'model': 'fixture-model', 'timeout': 60, 'after': 0}]}
        self.config_path.write_text(json.dumps(self.config)); self.config_path.chmod(0o600)
        self.home_patch = patch.object(entry, 'home', return_value=self.home); self.home_patch.start()
        self.fence_patch = patch('blackoutburst_agents.host_journal.canonical_directory',
                                 return_value=self.home / '.local/state/blackoutburst-agents/hosts')
        self.fence_patch.start()
        self.owner = SimpleNamespace(pw_uid=self.uid, pw_gid=self.gid, pw_dir=str(self.home))

    def tearDown(self):
        self.fence_patch.stop(); self.home_patch.stop(); self.temp.cleanup()

    def args(self, action, **kwargs):
        return SimpleNamespace(action=action, owner=self.username, release=self.release, **kwargs)

    def assertCode(self, code, call):
        with self.assertRaises(pkg.PackageError) as error:
            call()
        self.assertEqual(error.exception.code, code)

    def test_owner_checks_real_uid_home_groups_and_environment_without_rewriting_them(self):
        with patch.object(entry.pwd, 'getpwnam', return_value=self.owner), \
                patch.object(entry.os, 'getgroups', return_value=[self.gid]), \
                patch.object(entry.platform, 'system', return_value='Linux'), \
                patch.object(entry.platform, 'machine', return_value='x86_64'), \
                patch.object(entry.os, 'environ', {'HOME': str(self.home)}):
            self.assertEqual(entry.owner_context(self.manifest, self.username), self.owner)
            self.assertEqual(os.environ['HOME'], str(self.home))
            with patch.dict(os.environ, {'CODEX_HOME': '/some/other/account'}):
                self.assertCode('owner_home_environment_mismatch', lambda: entry.owner_context(self.manifest, self.username))
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'PRIVATE-SENTINEL'}):
                self.assertCode('unexpected_provider_or_ssh_environment', lambda: entry.owner_context(self.manifest, self.username))
            with patch.object(entry.os, 'getgroups', return_value=[self.gid, 0]):
                self.assertCode('unix_owner_mismatch', lambda: entry.owner_context(self.manifest, self.username))
            self.owner.pw_uid += 1
            self.assertCode('unix_owner_mismatch', lambda: entry.owner_context(self.manifest, self.username))

    def test_configuration_rejects_other_owner_and_unpinned_binary(self):
        self.assertIs(entry.check_configuration(self.config, self.manifest, self.release, self.username), self.config)
        self.config['owner_id'] = 'owner-b'
        self.assertCode('host_owner_or_endpoint_mismatch', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))
        self.config['owner_id'] = 'owner-a'; self.config['targets'][0]['executable'] = '/usr/local/bin/codex'
        self.assertCode('unpinned_host_executable', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))

    def test_configuration_rejects_outside_connection_workspace_symlink_and_weak_permissions(self):
        target = self.config['targets'][0]
        target['connection_file'] = str(self.root / 'other-owner.json')
        self.assertCode('outside_owner_connection_directory', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))
        target['connection_file'] = str(self.connection); target['workspace'] = str(self.home)
        self.assertCode('dedicated_workspace_required', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))
        target['workspace'] = str(self.workspace); self.connection.chmod(0o644)
        self.assertCode('insecure_owner_path', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))
        self.connection.chmod(0o600)
        alias = self.connection.parent / 'alias.json'; alias.symlink_to(self.connection)
        target['connection_file'] = str(alias)
        self.assertCode('symlink_path', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))

    def test_other_endpoint_and_implicit_model_or_routing_refuse(self):
        value = json.loads(self.connection.read_text()); value['mcp_url'] = 'https://elsewhere.example/mcp'
        self.connection.write_text(json.dumps(value))
        self.assertCode('connection_endpoint_mismatch', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))
        self.config['targets'][0]['model'] = None
        self.assertCode('explicit_model_required', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))
        self.config['targets'][0]['model'] = 'fixture-model'; self.config['targets'][0]['routing'] = {}
        self.assertCode('explicit_model_required', lambda: entry.check_configuration(self.config, self.manifest, self.release, self.username))

    def test_configure_creates_disabled_real_journal_without_network_or_model(self):
        output = io.StringIO()
        with patch('subprocess.Popen') as popen, patch('urllib.request.urlopen') as urlopen, redirect_stdout(output):
            self.assertEqual(entry.execute(self.args('configure'), self.manifest), 0)
        popen.assert_not_called(); urlopen.assert_not_called()
        with HostJournal(self.journal) as journal:
            self.assertFalse(journal.control()['enabled'])
            self.assertEqual(journal.control()['remaining'], 0)
        for value in ('PRIVATE-CREDENTIAL', 'PRIVATE-INSTRUCTION'):
            self.assertNotIn(value, output.getvalue())

    def test_run_without_existing_journal_fails_without_creating_authority(self):
        with patch('blackoutburst_agents.cli.main') as adapter:
            self.assertCode('private_path_unavailable', lambda: entry.execute(self.args('run'), self.manifest))
        adapter.assert_not_called(); self.assertFalse(self.journal.exists())

    def test_service_run_uses_durable_config_and_does_not_reenable_after_stop(self):
        output = io.StringIO()
        with redirect_stdout(output):
            entry.execute(self.args('configure'), self.manifest)
        # Editing the initial JSON cannot change the saved owner's instruction.
        self.config['owner_id'] = 'owner-b'; self.config_path.write_text(json.dumps(self.config))
        with redirect_stdout(output), patch('subprocess.Popen') as popen:
            self.assertEqual(entry.execute(self.args('run'), self.manifest), 0)
        popen.assert_not_called()
        with HostJournal(self.journal) as journal:
            self.assertFalse(journal.control()['enabled'])
            self.assertEqual(journal.control()['configuration']['owner_id'], 'owner-a')
        self.assertNotIn('PRIVATE-INSTRUCTION', output.getvalue())

    def test_stop_remains_available_when_configuration_or_connection_is_invalid(self):
        with redirect_stdout(io.StringIO()):
            entry.execute(self.args('configure'), self.manifest)
            self.connection.unlink()
            self.assertEqual(entry.execute(self.args('stop'), self.manifest), 0)
        with HostJournal(self.journal) as journal:
            self.assertFalse(journal.control()['enabled'])
            self.assertEqual(journal.control()['reason'], 'stopped')

    def test_diagnostic_does_not_claim_provider_or_board_authentication(self):
        with redirect_stdout(io.StringIO()):
            entry.execute(self.args('configure'), self.manifest)
        output = io.StringIO()
        with patch('subprocess.Popen') as popen, redirect_stdout(output):
            self.assertEqual(entry.execute(self.args('check'), self.manifest), 0)
        popen.assert_not_called()
        receipt = json.loads(output.getvalue())
        self.assertFalse(receipt['provider_login_checked'])
        self.assertFalse(receipt['board_authorization_checked'])
        self.assertFalse(receipt['linux_development_qualified'])
        self.assertFalse(receipt['host_enabled'])
        self.assertFalse(receipt['activation_changed'])
        self.assertNotIn(str(self.home), output.getvalue())

    def test_diagnostic_reports_actual_enabled_state_without_renewing_budget(self):
        with redirect_stdout(io.StringIO()):
            entry.execute(self.args('configure'), self.manifest)
            entry.execute(self.args('enable', revision=1), self.manifest)
        with HostJournal(self.journal) as journal:
            before = journal.status()
        output = io.StringIO()
        with redirect_stdout(output):
            entry.execute(self.args('check'), self.manifest)
        self.assertTrue(json.loads(output.getvalue())['host_enabled'])
        with HostJournal(self.journal) as journal:
            self.assertEqual(before, journal.status())

    def test_probe_is_separate_explicit_bounded_and_never_has_a_model_fallback(self):
        with patch('blackoutburst_agents.cli.main', return_value=0) as adapter:
            self.assertEqual(entry.execute(self.args('probe', model='fixture-model', timeout=40), self.manifest), 0)
            self.assertEqual(adapter.call_args.args[0], ['probe', '--codex', str(self.codex), '--model', 'fixture-model', '--timeout', '40'])
            adapter.reset_mock()
            self.assertCode('invalid_probe_request', lambda: entry.execute(self.args('probe', model='', timeout=40), self.manifest))
            adapter.assert_not_called()

    def test_unsupported_actions_and_untrusted_release_never_load_the_adapter(self):
        for command in ('login', 'pair', 'queue', 'development', 'family'):
            with redirect_stdout(io.StringIO()), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit), patch.object(entry, 'execute') as execute:
                entry.main(['--release', str(self.release), '--owner', self.username, command])
            execute.assert_not_called()
        output = io.StringIO()
        with patch.object(entry, 'verify_release', side_effect=pkg.PackageError('untrusted_installation')), \
                patch.object(entry, 'execute') as execute, redirect_stdout(output):
            self.assertEqual(entry.main(['--release', str(self.release), '--owner', self.username, 'run']), 2)
        execute.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['reason'], 'untrusted_installation')


if __name__ == '__main__':
    unittest.main()
