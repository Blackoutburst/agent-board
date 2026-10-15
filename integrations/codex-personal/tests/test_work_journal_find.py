"""Exact read-only work lookup used for queue recovery."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.work_journal import WorkJournal, JournalError


class WorkJournalFindTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory=Path(temporary.name)/'work'
        self.journal=WorkJournal(self.directory)
        self.addCleanup(self.journal.close)
        self.scope={'owner_id':'owner','agent_id':'agent','project_id':'project','ticket_id':'ticket',
                    'board_session_id':'session','board_endpoint':'https://board.test/mcp'}
        self.config={'instruction':'PRIVATE','workspace':'/private/work','executable':'/private/codex','model':'fixture','timeout':120}

    def test_absent_lookup_does_not_create_or_advance_an_operation(self):
        changes=self.journal._db.total_changes
        with patch.object(self.journal,'begin',side_effect=AssertionError('find must not create')):
            self.assertIsNone(self.journal.find(self.scope,'request'))
            with self.journal.lock(self.scope):
                self.assertIsNone(self.journal.find(self.scope,'request'))
        self.assertEqual(self.journal._db.total_changes,changes)
        self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)

    def test_lookup_matches_every_scope_field_and_request(self):
        with self.journal.lock(self.scope):
            operation=self.journal.begin(self.scope,'exact-request',self.config)
        changes=self.journal._db.total_changes
        self.assertEqual(self.journal.find(self.scope,'exact-request'),operation)
        self.assertIsNone(self.journal.find(self.scope,'other-request'))
        for field in self.scope:
            changed={**self.scope,field:'https://other.test/mcp' if field=='board_endpoint' else 'other'}
            self.assertIsNone(self.journal.find(changed,'exact-request'))
        self.assertEqual(self.journal._db.total_changes,changes)
        self.assertEqual(self.journal.get(operation['id']),operation)

    def test_reopen_finds_existing_stage_without_advancing_it(self):
        with self.journal.lock(self.scope):
            operation=self.journal.begin(self.scope,'request',self.config)
            operation=self.journal.update(operation['id'],operation['revision'],stage='needs_review',payload={'reason':'fixture_review'})
        self.journal.close()
        with WorkJournal(self.directory) as reopened:
            with reopened.lock(self.scope):
                self.assertEqual(reopened.find(self.scope,'request'),operation)
            self.assertEqual(reopened._db.total_changes,0)

    def test_invalid_scope_or_key_is_refused_without_writing(self):
        changes=self.journal._db.total_changes
        for scope,key in [({},'request'),({**self.scope,'board_endpoint':'https://user:private@board.test/mcp'},'request'),
                          (self.scope,''),(self.scope,'x'*257),(self.scope,'key\nprivate')]:
            with self.assertRaises(JournalError):self.journal.find(scope,key)
        self.assertEqual(self.journal._db.total_changes,changes)


if __name__=='__main__':unittest.main()
