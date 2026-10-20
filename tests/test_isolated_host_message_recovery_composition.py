"""Real entry/coordinator/Board composition with synthetic installation boundaries."""
from contextlib import redirect_stdout
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


class OwnerRecoveryCompositionTests(unittest.TestCase):
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
