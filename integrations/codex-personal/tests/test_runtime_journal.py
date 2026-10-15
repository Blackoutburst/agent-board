"""Private runtime journal invariants; no Board, network or model needed."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
import uuid

from blackoutburst_agents.runtime_journal import APPLICATION_ID, RuntimeJournal
from blackoutburst_agents.work_journal import JournalError


class RuntimeJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.directory = self.root / 'runtime'
        self.journal = RuntimeJournal(self.directory)
        self.addCleanup(self.journal.close)
        self.scope = {'owner_id': 'owner', 'agent_id': 'agent', 'project_id': 'project',
            'ticket_id': 'ticket', 'board_session_id': 'session', 'board_endpoint': 'https://fixture.test/mcp'}
        self.config = RuntimeJournal.configuration(self.scope, str(uuid.uuid4()), str(uuid.uuid4()), 'fixture-model')

    def begin(self, configuration=None):
        return self.journal.begin(self.config if configuration is None else configuration)[0]

    def assert_error(self, action, code=None):
        with self.assertRaises(JournalError) as caught:
            action()
        if code is not None:
            self.assertEqual(caught.exception.code, code)
        return caught.exception

    def advance(self, target):
        operation = self.begin()
        for phase in ('reserved', 'starting', 'active', 'finished'):
            operation = self.journal.update(operation['id'], operation['revision'], phase,
                {'id': 'remote-id', 'state': 'finished' if phase == 'finished' else 'running' if phase == 'active' else 'reserved',
                 'cleanup_confirmed': phase == 'finished'},
                start_confirmed=phase == 'active')
            if phase == target:
                return operation
        self.fail('unknown phase')

    def test_files_are_private_and_reopen_preserves_metadata(self):
        operation = self.advance('active')
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.journal.close()
        with RuntimeJournal(self.directory) as reopened:
            self.assertEqual(reopened.get(operation['id']), operation)
            self.assertEqual(reopened.db.execute('PRAGMA application_id').fetchone()[0], APPLICATION_ID)
            self.assertEqual(reopened.db.execute('PRAGMA synchronous').fetchone()[0], 2)

    def test_idempotence_find_and_status_do_not_create_records(self):
        before = self.journal.db.total_changes
        self.assertIsNone(self.journal.find(self.config))
        self.assertEqual(self.journal.db.total_changes, before)
        one, created = self.journal.begin(self.config)
        self.assertTrue(created)
        before = self.journal.db.total_changes
        reordered = dict(reversed(list(self.config.items())))
        again, created = self.journal.begin(reordered)
        self.assertFalse(created)
        self.assertEqual(again, one)
        self.assertEqual(self.journal.find(reordered), one)
        self.assertEqual(self.journal.db.total_changes, before)
        self.assertEqual(len(self.journal.list_status()), 1)

    def test_every_scope_and_turn_field_is_part_of_opaque_identity(self):
        first = self.begin()
        variants = []
        for key in self.scope:
            scope = {**self.scope, key: 'https://other.test/mcp' if key == 'board_endpoint' else 'different'}
            variants.append(RuntimeJournal.configuration(scope, self.config['work_id'], self.config['turn_id'], self.config['model']))
        for field in ('work_id', 'turn_id', 'model'):
            changed = {**self.config, field: 'other-model' if field == 'model' else str(uuid.uuid4())}
            variants.append(changed)
        for value in variants:
            with self.subTest(value=value):
                other = self.begin(value)
                self.assertNotEqual(other['id'], first['id'])
                self.assertNotEqual(other['key'], first['key'])

    def test_invalid_config_or_credential_fields_leave_no_record(self):
        variants = [{**self.config, 'provider': 'other'}, {**self.config, 'token': 'PRIVATE_CREDENTIAL'},
            {**self.config, 'unexpected': 'PRIVATE_TEXT'}, {**self.config, 'model': ''},
            {**self.config, 'model': 'x\nPRIVATE'}, {**self.config, 'model': 'x' * 201},
            {**self.config, 'scope': {**self.scope, 'token': 'PRIVATE_CREDENTIAL'}}]
        for value in variants:
            with self.subTest(fields=list(value)):
                self.assert_error(lambda: self.journal.begin(value))
        self.assertEqual(self.journal.list_status(), [])
        self.assertNotIn(b'PRIVATE', (self.directory / 'runtime.sqlite3').read_bytes())

    def test_uuid_identity_must_be_canonical(self):
        for value in [None, 1, True, '', 'not-a-uuid', self.config['work_id'].upper()]:
            with self.subTest(value=value):
                # configuration rejects before persistence; malformed UUID
                # parsing may be ValueError, while canonicalization is JournalError.
                with self.assertRaises((JournalError, ValueError, TypeError, AttributeError)):
                    RuntimeJournal.configuration(self.scope, value, self.config['turn_id'], self.config['model'])
        self.assertEqual(self.journal.list_status(), [])

    def test_update_requires_exact_strict_revision(self):
        operation = self.begin()
        for revision in (True, 1.0, '1', None, 0, -1, 2):
            with self.subTest(revision=revision):
                self.assert_error(lambda: self.journal.update(operation['id'], revision, 'reserved'))
                self.assertEqual(self.journal.get(operation['id']), operation)
        updated = self.journal.update(operation['id'], 1, 'reserved')
        self.assertEqual(updated['revision'], 2)
        self.assert_error(lambda: self.journal.update(operation['id'], 1, 'starting'), 'revision_conflict')

    def test_transitions_reject_skipping_start_and_restarting_terminal(self):
        operation = self.begin()
        for phase in ('active', 'finished', 'starting'):
            self.assert_error(lambda: self.journal.update(operation['id'], 1, phase), 'invalid_runtime_phase')
        finished = self.advance('finished')
        for phase in ('reserving', 'reserved', 'starting', 'active', 'needs_review', 'finished'):
            self.assert_error(lambda: self.journal.update(finished['id'], finished['revision'], phase), 'runtime_already_finished')

    def test_review_cannot_become_active_and_start_confirmation_cannot_be_cleared(self):
        active = self.advance('active')
        self.assertTrue(active['start_confirmed'])
        review = self.journal.update(active['id'], active['revision'], 'needs_review')
        self.assertTrue(review['start_confirmed'])
        self.assert_error(lambda: self.journal.update(review['id'], review['revision'], 'active'), 'invalid_runtime_phase')
        finished = self.journal.update(review['id'], review['revision'], 'finished',
            {'id': 'remote-id', 'state': 'finished', 'cleanup_confirmed': True})
        self.assertTrue(finished['start_confirmed'])

    def test_active_and_finished_require_local_start_and_remote_cleanup_proofs(self):
        starting = self.advance('starting')
        self.assert_error(lambda: self.journal.update(starting['id'], starting['revision'], 'active'), 'runtime_start_unconfirmed')
        active = self.journal.update(starting['id'], starting['revision'], 'active', start_confirmed=True)
        for receipt in (None, {'state': 'running', 'cleanup_confirmed': True},
                        {'state': 'finished', 'cleanup_confirmed': False}, {'state': 'finished', 'cleanup_confirmed': 1}):
            self.assert_error(lambda: self.journal.update(active['id'], active['revision'], 'finished', receipt),
                              'runtime_cleanup_unconfirmed')
        self.assertEqual(self.journal.get(active['id']), active)

    def test_start_confirmation_requires_activation_and_strict_boolean(self):
        operation = self.begin()
        for value in (True, 1, 'true'):
            self.assert_error(lambda: self.journal.update(operation['id'], 1, 'reserved', start_confirmed=value))
        self.assertEqual(self.journal.get(operation['id']), operation)

    def test_receipts_are_bounded_and_reject_nested_credential_fields(self):
        operation = self.begin()
        for receipt in ({'details': {'token': 'PRIVATE_TOKEN'}}, {'raw': 'x' * 16385},
                        {'nan': float('nan')}, {'text': '\ud800'}):
            self.assert_error(lambda: self.journal.update(operation['id'], 1, 'reserved', receipt))
            self.assertEqual(self.journal.get(operation['id']), operation)
        safe = {'id': 'opaque', 'state': 'reserved', 'revision': 1}
        self.assertEqual(self.journal.update(operation['id'], 1, 'reserved', safe)['receipt'], safe)

    def test_status_and_list_expose_only_metadata_not_config_or_receipts(self):
        operation = self.advance('active')
        expected = {'id', 'phase', 'revision', 'created_at', 'updated_at'}
        self.assertEqual(set(self.journal.status(operation['id'])), expected)
        self.assertEqual(set(self.journal.list_status()[0]), expected)
        raw = json.dumps(self.journal.list_status())
        for value in ('fixture-model', self.config['work_id'], self.config['turn_id'],
                      'https://fixture.test/mcp', 'remote-id', 'configuration', 'receipt'):
            self.assertNotIn(value, raw)

    def test_transaction_exception_rolls_back_all_mutations(self):
        operation = self.begin()
        with self.assertRaises(RuntimeError):
            with self.journal.transaction():
                self.journal.db.execute("UPDATE executions SET phase='needs_review' WHERE id=?", (operation['id'],))
                raise RuntimeError('synthetic interruption')
        self.assertEqual(self.journal.get(operation['id']), operation)

    def test_future_version_wrong_application_and_columns_refused_unchanged(self):
        self.journal.close()
        path = self.directory / 'runtime.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA user_version=99')
        before = path.read_bytes()
        self.assert_error(lambda: RuntimeJournal(self.directory), 'unsupported_runtime_journal')
        self.assertEqual(path.read_bytes(), before)
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA user_version=1')
            db.execute('PRAGMA application_id=123')
        self.assert_error(lambda: RuntimeJournal(self.directory), 'runtime_journal_corrupt')
        with sqlite3.connect(path) as db:
            db.execute(f'PRAGMA application_id={APPLICATION_ID}')
            db.execute('ALTER TABLE executions ADD COLUMN unexpected TEXT')
        self.assert_error(lambda: RuntimeJournal(self.directory), 'runtime_journal_corrupt')

    def test_existing_unversioned_database_is_not_adopted(self):
        self.journal.close()
        path = self.directory / 'runtime.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA user_version=0')
        self.assert_error(lambda: RuntimeJournal(self.directory), 'runtime_journal_corrupt')

    def test_invalid_persisted_identity_or_time_or_receipt_is_rejected(self):
        operation = self.begin()
        original = dict(self.journal.db.execute('SELECT * FROM executions').fetchone())
        variants = [('key', 'different'), ('phase', 'invalid'), ('revision', 0),
                    ('start_confirmed', 2), ('created_at', float('inf')), ('receipt', '{bad json')]
        for column, value in variants:
            with self.subTest(column=column):
                self.journal.db.execute('UPDATE executions SET ' + column + '=? WHERE id=?', (value, operation['id']))
                self.assert_error(lambda: self.journal.get(operation['id']))
                self.journal.db.execute('UPDATE executions SET ' + column + '=? WHERE id=?', (original[column], operation['id']))
        altered = copy.deepcopy(self.config)
        altered['scope']['owner_id'] = 'different'
        self.journal.db.execute('UPDATE executions SET configuration=? WHERE id=?', (json.dumps(altered), operation['id']))
        self.assert_error(lambda: self.journal.get(operation['id']), 'runtime_journal_corrupt')

    def test_public_directory_or_database_is_rejected(self):
        self.journal.close()
        self.directory.chmod(0o755)
        self.assert_error(lambda: RuntimeJournal(self.directory), 'insecure_runtime_directory')
        self.directory.chmod(0o700)
        (self.directory / 'runtime.sqlite3').chmod(0o644)
        self.assert_error(lambda: RuntimeJournal(self.directory), 'insecure_journal_file')

    def test_symlink_directory_or_database_is_rejected(self):
        self.journal.close()
        link = self.root / 'directory-link'
        link.symlink_to(self.directory, target_is_directory=True)
        self.assert_error(lambda: RuntimeJournal(link), 'insecure_runtime_directory')
        path = self.directory / 'runtime.sqlite3'
        original = self.directory / 'original.sqlite3'
        path.rename(original)
        path.symlink_to(original)
        self.assert_error(lambda: RuntimeJournal(self.directory), 'invalid_journal_file')

    def test_hardlinked_database_and_symlink_sidecar_are_rejected(self):
        self.journal.close()
        path = self.directory / 'runtime.sqlite3'
        hardlink = self.root / 'hardlink'
        os.link(path, hardlink)
        self.assert_error(lambda: RuntimeJournal(self.directory), 'insecure_journal_file')
        hardlink.unlink()
        sidecar = Path(str(path) + '-wal')
        sidecar.symlink_to(path)
        self.assert_error(lambda: RuntimeJournal(self.directory), 'invalid_journal_file')


if __name__ == '__main__':
    unittest.main()
