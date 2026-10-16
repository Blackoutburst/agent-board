"""Independent restart/recovery and migration-failure checks; no real model."""
from contextlib import redirect_stdout
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from blackoutburst_agents.cli import main
from blackoutburst_agents.run_store import RunStore, StoreError
from blackoutburst_agents.runner import PersonalRunner
import test_decision_guards as guard_fixtures
import test_runner as runner_fixtures


class MemoryRuntimeRecoveryReview(unittest.TestCase):
    transport = guard_fixtures.ManagedDecisionTests.transport
    run_worker = guard_fixtures.ManagedDecisionTests.run_worker
    execution = guard_fixtures.ManagedDecisionTests.execution

    def setUp(self):
        guard_fixtures.ManagedDecisionTests.setUp(self)

    def test_restart_reconciles_lost_managed_finish_without_memory_path_or_model(self):
        self.control.lost.add('finish_execution')
        self.assertEqual(self.run_worker()['status'], 'needs_review')
        operation = self.runtime.list_status()[0]
        self.assertEqual(operation['phase'], 'needs_review')
        self.assertEqual(self.calls, 1)
        self.store.close()
        self.memory.close()
        output = io.StringIO()
        with redirect_stdout(output), patch(
                'blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',
                return_value=self.control):
            code = main(['runtime', '--journal', str(self.root / 'runtime'), 'reconcile',
                         '--operation', operation['id'], '--state', str(self.root / 'runs'),
                         '--connection-file', str(self.root / 'connection.json')])
        receipt = json.loads(output.getvalue())
        self.assertEqual((code, receipt.get('phase')), (0, 'finished'), receipt)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.control.calls.count('reserve_execution'), 1)
        self.assertEqual(self.control.calls.count('start_execution'), 1)
        self.assertEqual(self.control.calls.count('finish_execution'), 1)
        self.assertEqual(self.board.get_runtime(self.human)['active_count'], 0)


class MemoryMigrationAtomicityReview(unittest.TestCase):
    create_work = runner_fixtures.RunnerTests.create_work
    transport = runner_fixtures.RunnerTests.transport

    def setUp(self):
        runner_fixtures.RunnerTests.setUp(self)
        self.addCleanup(self.store.close)

    def test_failed_format_upgrade_rolls_back_schema_version_and_preserves_history(self):
        first = PersonalRunner(self.store, self.transport).run(self.work['id'], 'before', 'legacy')
        self.store.close()
        database = self.state / 'runs.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('DROP TABLE context_guards')
            db.execute('PRAGMA user_version=1')
            works = list(db.execute('SELECT * FROM works'))
            turns = list(db.execute('SELECT * FROM turns'))
        real_connect = sqlite3.connect

        class MigrationWriteFailure(sqlite3.Connection):
            def execute(self, statement, *args, **kwargs):
                value = super().execute(statement, *args, **kwargs)
                if statement.strip() == 'PRAGMA user_version=2':
                    raise sqlite3.OperationalError('synthetic migration write failure')
                return value

        def broken_connect(*args, **kwargs):
            return real_connect(*args, factory=MigrationWriteFailure, **kwargs)

        with patch('blackoutburst_agents.run_store.sqlite3.connect', side_effect=broken_connect):
            with self.assertRaises(StoreError) as error:
                RunStore(self.state)
        self.assertEqual(error.exception.code, 'state_unavailable')
        with real_connect(database) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='context_guards'").fetchone())
            self.assertEqual(list(db.execute('SELECT * FROM works')), works)
            self.assertEqual(list(db.execute('SELECT * FROM turns')), turns)
        self.store = RunStore(self.state)
        self.addCleanup(self.store.close)
        replay = PersonalRunner(self.store, self.transport).run(self.work['id'], 'before', 'legacy')
        self.assertTrue(replay['cached'])
        self.assertEqual(replay['result']['session_id'], first['result']['session_id'])
        self.assertEqual(len(self.calls), 1)
