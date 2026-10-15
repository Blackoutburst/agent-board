"""Event durability, exact scope and recovery; no server, model or credentials."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.event_journal import EventJournal, EventJournalError
from blackoutburst_agents.work_journal import WorkJournal, STAGES


class EventJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='event-journal-test-')
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'private'
        self.journal = EventJournal(self.directory)
        self.addCleanup(self.journal.close)
        self.scope = {'owner_id': 'owner', 'agent_id': 'agent', 'project_id': 'project',
                      'ticket_id': 'ticket', 'board_session_id': 'session',
                      'board_endpoint': 'https://board.test/mcp'}
        self.configuration = {'instruction': 'PRIVATE instruction', 'workspace': '/private/workspace',
                              'executable': '/private/codex', 'model': None, 'timeout': 120}

    def error(self, code, action):
        with self.assertRaises(EventJournalError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def event(self, number, *, kind='human', event='ticket_updated', scope=None):
        scope = scope or self.scope
        return {'id': number, 'ticket_id': scope['ticket_id'], 'project_id': scope['project_id'],
                'actor': {'kind': kind, 'owner_id': 'another-participant'},
                'event': event, 'created_at': 123.0}

    def receipt(self, status='complete', **extra):
        return {'status': status, 'stage': {'complete': 'complete', 'pending': 'publishing',
                'needs_review': 'needs_review', 'abandoned': 'abandoned'}[status],
                'reason': 'test_reason', 'cached': False, **extra}

    def subscription(self, *, after=0):
        return self.journal.open_subscription(self.scope, self.configuration, after=after)

    def prepared(self):
        subscription = self.subscription()
        self.journal.append_page(subscription['id'], 0, [self.event(1)])
        return self.journal.prepare_batch(subscription['id'])

    def test_private_storage_reopen_and_metadata(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription(after=40)
            subscription = self.journal.append_page(subscription['id'], 40, [self.event(44)])
        self.journal.close()
        with EventJournal(self.directory) as reopened:
            self.assertEqual(reopened.get_subscription(subscription['id']), subscription)
            status = reopened.status(subscription['id'])
            self.assertEqual(status['after'], 44)
            self.assertEqual(status['pending_count'], 1)
            encoded = json.dumps(status)
            for excluded in ('PRIVATE', '/private', 'configuration', 'instruction', 'scope', 'board.test'):
                self.assertNotIn(excluded, encoded)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_configuration_and_initial_cursor_are_immutable(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription(after=5)
            self.assertEqual(self.subscription(after=5), subscription)
            self.journal.append_page(subscription['id'], 5, [self.event(7)])
            self.assertEqual(self.subscription(after=5)['after'], 7)
            self.error('subscription_configuration_conflict', self.subscription)
            self.error('subscription_configuration_conflict', lambda: self.subscription(after=7))
            for field, different in (('instruction', 'different'), ('workspace', '/other'),
                    ('executable', '/another'), ('model', 'other'), ('timeout', 30)):
                self.error('subscription_configuration_conflict', lambda: self.journal.open_subscription(
                    self.scope, {**self.configuration, field: different}, after=5))

    def test_configuration_and_scopes_reject_credentials_invalid_shapes_and_bounds(self):
        with self.journal.lock(self.scope):
            for change in ({'token': 'PRIVATE KEY'}, {'instruction': ''}, {'instruction': '\ud800'},
                           {'instruction': 'é' * 6001}, {'timeout': True}, {'timeout': 0},
                           {'timeout': float('nan')}, {'timeout': 10 ** 400}, {'workspace': 'relative'},
                           {'model': {'authorization': 'PRIVATE'}}):
                self.error('invalid_configuration', lambda: self.journal.open_subscription(
                    self.scope, {**self.configuration, **change}))
            for cursor in (-1, True, 2 ** 63, 1.0):
                self.error('invalid_cursor', lambda: self.subscription(after=cursor))
        for scope in ({}, {**self.scope, 'other': 'private'},
                      {**self.scope, 'board_endpoint': 'https://user:password@board.test/mcp'},
                      {**self.scope, 'board_endpoint': 'https://board.test/mcp?token=private'}):
            with self.assertRaises(EventJournalError) as caught:
                with self.journal.lock(scope):
                    self.fail('invalid scope accepted')
            self.assertEqual(caught.exception.code, 'invalid_scope')

    def test_every_scope_field_is_part_of_subscription_identity(self):
        with self.journal.lock(self.scope):
            original = self.subscription()
            for field in self.scope:
                other_scope = {**self.scope, field: 'https://different.test/mcp' if field == 'board_endpoint' else 'other'}
                with self.journal.lock(other_scope):
                    other = self.journal.open_subscription(other_scope, self.configuration)
                self.assertNotEqual(other['id'], original['id'])
                self.error('scope_lock_required', lambda: self.journal.append_page(other['id'], 0, []))

    def test_lock_excludes_connections_and_processes_and_releases_on_exception(self):
        other = EventJournal(self.directory)
        self.addCleanup(other.close)
        self.error('scope_lock_required', self.subscription)
        code = '''
import json, sys
from blackoutburst_agents.event_journal import EventJournal, EventJournalError
with EventJournal(sys.argv[1]) as journal:
    try:
        with journal.lock(json.loads(sys.argv[2])):
            print('acquired')
    except EventJournalError as error:
        print(error.code)
'''
        args = [sys.executable, '-c', code, str(self.directory), json.dumps(self.scope)]
        with self.assertRaisesRegex(RuntimeError, 'interruption'):
            with self.journal.lock(self.scope):
                subscription = self.subscription()
                for journal in (self.journal, other):
                    with self.assertRaises(EventJournalError) as caught:
                        with journal.lock(self.scope):
                            self.fail('concurrent lock acquired')
                    self.assertEqual(caught.exception.code, 'scope_busy')
                self.error('scope_lock_required', lambda: other.append_page(subscription['id'], 0, []))
                self.error('journal_locked', self.journal.close)
                output = subprocess.run(args, text=True, capture_output=True, check=True, timeout=10)
                self.assertEqual(output.stdout.strip(), 'scope_busy')
                raise RuntimeError('interruption')
        output = subprocess.run(args, text=True, capture_output=True, check=True, timeout=10)
        self.assertEqual(output.stdout.strip(), 'acquired')

    def test_replayed_page_is_idempotent_and_conflicting_duplicate_rolls_back(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription()
            page = [self.event(3), self.event(8)]
            appended = self.journal.append_page(subscription['id'], 0, page)
            self.assertEqual(self.journal.append_page(subscription['id'], 0, page), appended)
            self.assertEqual(self.journal.append_page(subscription['id'], 8, []), appended)
            self.error('event_conflict', lambda: self.journal.append_page(subscription['id'], 0,
                [self.event(3), self.event(8, kind='agent')]))
            self.error('cursor_conflict', lambda: self.journal.append_page(subscription['id'], 0,
                [self.event(3), self.event(8), self.event(10)]))
            self.error('cursor_conflict', lambda: self.journal.append_page(subscription['id'], 9, [self.event(10)]))
            self.error('cursor_conflict', lambda: self.journal.append_page(subscription['id'], 0, []))
            self.assertEqual(self.journal.get_subscription(subscription['id']), appended)
            self.assertEqual(self.journal.prepare_batch(subscription['id'])['event_ids'], [3, 8])

    def test_invalid_page_cannot_advance_or_partially_insert(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription()
            invalid = ([self.event(2), self.event(1)], [self.event(1), self.event(1)],
                       [self.event(0)], [self.event(True)], [self.event(1.0)], [self.event(2 ** 63)],
                       [self.event(1), {**self.event(2), 'detail': {'body': 'PRIVATE MESSAGE'}}],
                       [self.event(1), {**self.event(2), 'title': 'PRIVATE TITLE'}],
                       [self.event(1), {**self.event(2), 'created_at': float('inf')}],
                       [self.event(1), {**self.event(2), 'actor': {'kind': 'human', 'owner_id': 'x', 'token': 'key'}}],
                       [self.event(1), {**self.event(2), 'actor': {'kind': 'system', 'owner_id': 'x'}}],
                       [self.event(number) for number in range(1, 202)], {}, None)
            for page in invalid:
                with self.assertRaises(EventJournalError):
                    self.journal.append_page(subscription['id'], 0, page)
                self.assertEqual(self.journal.get_subscription(subscription['id']), subscription)
                self.assertIsNone(self.journal.prepare_batch(subscription['id']))
            self.error('event_scope_mismatch', lambda: self.journal.append_page(subscription['id'], 0,
                [self.event(1), {**self.event(2), 'ticket_id': 'different-ticket'}]))
            self.assertEqual(self.journal.get_subscription(subscription['id'])['after'], 0)
        raw = (self.directory / 'event-journal.sqlite3').read_bytes()
        self.assertNotIn(b'PRIVATE MESSAGE', raw)
        self.assertNotIn(b'PRIVATE TITLE', raw)

    def test_all_nonhuman_and_unselected_types_advance_without_triggering(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription()
            page = [self.event(1, kind='agent'), self.event(2, kind='agent', event='ticket_message_posted'),
                    self.event(3, event='ticket_claimed'), self.event(4, event='unknown_future_event')]
            appended = self.journal.append_page(subscription['id'], 0, page)
            self.assertEqual(appended['after'], 4)
            self.assertEqual(appended['pending_count'], 0)
            self.assertIsNone(self.journal.prepare_batch(subscription['id']))
            self.assertEqual(self.journal.append_page(subscription['id'], 0, page), appended)
            types = ('ticket_created', 'ticket_updated', 'ticket_message_posted', 'human_input_resolved')
            self.journal.append_page(subscription['id'], 4, [self.event(index + 5, event=event) for index, event in enumerate(types)])
            self.assertEqual(self.journal.prepare_batch(subscription['id'])['event_ids'], [5, 6, 7, 8])

    def test_backlog_over_page_size_no_loss_and_insertions_between_batches(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription()
            # Nonconsecutive global IDs represent events hidden by board access rules.
            for offset, count in ((0, 200), (200, 200), (400, 13)):
                subscription = self.journal.append_page(subscription['id'], subscription['after'],
                    [self.event(3 * number) for number in range(offset + 1, offset + count + 1)])
            self.assertEqual(subscription['pending_count'], 413)
            first = self.journal.prepare_batch(subscription['id'])
            self.assertEqual(first['event_ids'], list(range(3, 601, 3)))
            self.journal.append_page(subscription['id'], subscription['after'], [self.event(2000)])
            self.assertEqual(self.journal.prepare_batch(subscription['id']), first)
            seen = []
            while (batch := self.journal.prepare_batch(subscription['id'])) is not None:
                seen.extend(batch['event_ids'])
                self.journal.start_batch(batch['id'])
                self.journal.finish_batch(batch['id'], self.receipt())
            self.assertEqual(seen, list(range(3, 1240, 3)) + [2000])
            self.assertEqual(self.journal.status(subscription['id'])['pending_count'], 0)

    def test_append_sql_failure_rolls_back_insert_and_cursor_together(self):
        with self.journal.lock(self.scope):
            subscription = self.subscription()
            self.journal._db.execute("CREATE TRIGGER fail_cursor BEFORE UPDATE ON subscriptions BEGIN SELECT RAISE(ABORT,'private error'); END")
            self.error('journal_unavailable', lambda: self.journal.append_page(subscription['id'], 0, [self.event(1)]))
            self.assertEqual(self.journal.get_subscription(subscription['id']), subscription)
            self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 0)

    def test_pending_count_limit_is_atomic_and_counts_open_batch_events(self):
        with self.journal.lock(self.scope), patch('blackoutburst_agents.event_journal.MAX_PENDING_EVENTS', 2):
            subscription = self.subscription()
            self.journal.append_page(subscription['id'], 0, [self.event(1), self.event(2)])
            batch = self.journal.prepare_batch(subscription['id'])
            self.error('pending_event_limit', lambda: self.journal.append_page(subscription['id'], 2,
                [self.event(3, kind='agent'), self.event(4)]))
            self.assertEqual(self.journal.status(subscription['id'])['after'], 2)
            self.journal.append_page(subscription['id'], 2, [self.event(3, kind='agent')])
            self.journal.start_batch(batch['id'])
            self.journal.finish_batch(batch['id'], self.receipt())
            appended = self.journal.append_page(subscription['id'], 3, [self.event(4)])
            self.assertEqual(appended['pending_count'], 1)

    def test_pending_batch_reopens_with_same_key_and_complete_allows_next(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.assertFalse(batch['dispatched'])
            started = self.journal.start_batch(batch['id'])
            self.assertTrue(started['dispatched'])
            self.assertEqual(self.journal.start_batch(batch['id']), started)
            pending = self.journal.finish_batch(batch['id'], self.receipt('pending'))
            self.journal.append_page(batch['subscription_id'], 1, [self.event(2)])
        self.journal.close()
        with EventJournal(self.directory) as reopened, reopened.lock(self.scope):
            self.assertEqual(reopened.prepare_batch(batch['subscription_id']), pending)
            self.assertEqual(reopened.start_batch(batch['id']), pending)
            completed = reopened.finish_batch(batch['id'], self.receipt())
            self.assertEqual(reopened.finish_batch(batch['id'], self.receipt()), completed)
            next_batch = reopened.prepare_batch(batch['subscription_id'])
            self.assertEqual(next_batch['event_ids'], [2])
            self.assertNotEqual(next_batch['request_id'], batch['request_id'])
            self.error('batch_receipt_conflict', lambda: reopened.finish_batch(batch['id'], self.receipt('pending')))

    def test_dispatch_marker_survives_crash_without_receipt(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.error('batch_not_started', lambda: self.journal.finish_batch(batch['id'], self.receipt()))
            started = self.journal.start_batch(batch['id'])
        self.journal.close()
        with EventJournal(self.directory) as reopened, reopened.lock(self.scope):
            resumed = reopened.prepare_batch(batch['subscription_id'])
            self.assertEqual(resumed, started)
            self.assertTrue(resumed['dispatched'])
            self.assertEqual(resumed['status'], 'prepared')

    def test_needs_review_stops_automatic_batches_even_with_new_events(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.journal.start_batch(batch['id'])
            review = self.journal.finish_batch(batch['id'], self.receipt('needs_review'))
            self.journal.append_page(batch['subscription_id'], 1, [self.event(2)])
            self.assertEqual(self.journal.prepare_batch(batch['subscription_id']), review)
            self.assertEqual(self.journal.finish_batch(batch['id'], self.receipt('needs_review')), review)
            self.error('batch_needs_review', lambda: self.journal.start_batch(batch['id']))
            self.error('batch_receipt_conflict', lambda: self.journal.finish_batch(batch['id'], self.receipt()))
            self.assertEqual(self.journal.status(batch['subscription_id'])['pending_count'], 2)

    def test_abandoned_worker_result_requires_local_review_instead_of_new_batch(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.journal.start_batch(batch['id'])
            review = self.journal.finish_batch(batch['id'], self.receipt('abandoned'))
            self.assertEqual(review['status'], 'needs_review')
            self.assertEqual(self.journal.prepare_batch(batch['subscription_id']), review)

    def test_receipts_are_bounded_metadata_and_all_writes_need_exact_scope_lock(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.journal.start_batch(batch['id'])
            for receipt in ({}, [], {**self.receipt(), 'result': 'PRIVATE MODEL TEXT'},
                            {**self.receipt(), 'reason': 'PRIVATE reason with spaces'},
                            {**self.receipt(), 'operation_id': 'not-a-uuid'},
                            {**self.receipt(), 'published_status': 'done'},
                            {**self.receipt(), 'stage': 'running'},
                            {**self.receipt('pending'), 'stage': 'complete'}):
                self.error('invalid_receipt', lambda: self.journal.finish_batch(batch['id'], receipt))
            self.assertEqual(self.journal.get_batch(batch['id'])['status'], 'prepared')
        self.error('scope_lock_required', lambda: self.journal.prepare_batch(batch['subscription_id']))
        self.error('scope_lock_required', lambda: self.journal.start_batch(batch['id']))
        self.error('scope_lock_required', lambda: self.journal.finish_batch(batch['id'], self.receipt()))

    def test_pending_receipt_cannot_switch_operation_and_repeated_receipt_is_stable(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.journal.start_batch(batch['id'])
            operation_id = str(uuid.uuid4())
            receipt = self.receipt('pending', operation_id=operation_id)
            pending = self.journal.finish_batch(batch['id'], receipt)
            self.assertEqual(self.journal.finish_batch(batch['id'], receipt), pending)
            for extra in ({}, {'operation_id': str(uuid.uuid4())}):
                self.error('batch_receipt_conflict', lambda: self.journal.finish_batch(batch['id'], self.receipt(**extra)))
            complete = self.journal.finish_batch(batch['id'], self.receipt(operation_id=operation_id))
            self.assertEqual(complete['status'], 'complete')

    def test_explicit_resolution_after_work_abandon_preserves_receipt_and_unblocks_next(self):
        work_directory = self.directory.parent / 'work'
        with self.journal.lock(self.scope), WorkJournal(work_directory) as work, work.lock(self.scope):
            batch = self.prepared()
            self.journal.start_batch(batch['id'])
            operation = work.begin(self.scope, batch['request_id'], self.configuration)
            operation = work.update(operation['id'], operation['revision'], stage='needs_review', payload={'reason': 'private_outcome'})
            original_receipt = self.receipt('needs_review', operation_id=operation['id'])
            self.journal.finish_batch(batch['id'], original_receipt)
            self.journal.append_page(batch['subscription_id'], 1, [self.event(2)])
            self.error('resolution_mismatch', lambda: self.journal.resolve_batch(batch['id'], operation))
            operation = work.abandon(operation['id'], operation['revision'], 'Private human reconciliation')
            resolved = self.journal.resolve_batch(batch['id'], operation)
            self.assertEqual(resolved['status'], 'complete')
            self.assertEqual(resolved['receipt'], original_receipt)
            self.assertEqual(resolved['resolution'], {'decision': 'abandoned', 'operation_id': operation['id'],
                                                     'revision': operation['revision']})
            self.assertNotIn('Private human reconciliation', json.dumps(resolved))
            self.assertEqual(self.journal.resolve_batch(batch['id'], operation), resolved)
            self.assertEqual(self.journal.prepare_batch(batch['subscription_id'])['event_ids'], [2])
            self.error('resolution_mismatch', lambda: self.journal.resolve_batch(batch['id'],
                {**operation, 'revision': operation['revision'] + 1}))
        self.journal.close()
        with EventJournal(self.directory) as reopened, reopened.lock(self.scope):
            self.assertEqual(reopened.get_batch(batch['id']), resolved)
            self.assertEqual(reopened.resolve_batch(batch['id'], operation), resolved)

    def test_explicit_resolution_requires_exact_scope_request_configuration_and_recorded_id(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            operation = {'id': str(uuid.uuid4()), 'scope': self.scope, 'request_id': batch['request_id'],
                         'configuration': self.configuration, 'stage': 'complete', 'revision': 3}
            self.error('batch_not_started', lambda: self.journal.resolve_batch(batch['id'], operation))
            self.journal.start_batch(batch['id'])
            self.journal.finish_batch(batch['id'], self.receipt('needs_review', operation_id=operation['id']))
            invalid = [None, {}, {**operation, 'scope': {**self.scope, 'ticket_id': 'other'}},
                       {**operation, 'request_id': 'other'}, {**operation, 'stage': 'running'},
                       {**operation, 'configuration': {**self.configuration, 'model': 'other'}},
                       {**operation, 'id': str(uuid.uuid4())}, {**operation, 'revision': True}]
            for value in invalid:
                self.error('resolution_mismatch', lambda: self.journal.resolve_batch(batch['id'], value))
                self.assertEqual(self.journal.get_batch(batch['id'])['status'], 'needs_review')
        self.error('scope_lock_required', lambda: self.journal.resolve_batch(batch['id'], operation))

    def test_explicit_complete_proof_resolves_lost_receipt_after_dispatch(self):
        with self.journal.lock(self.scope), WorkJournal(self.directory.parent / 'work') as work, work.lock(self.scope):
            batch = self.prepared()
            self.journal.start_batch(batch['id'])
            operation = work.begin(self.scope, batch['request_id'], self.configuration)
            for stage in STAGES[1:STAGES.index('complete') + 1]:
                operation = work.update(operation['id'], operation['revision'], stage=stage, payload={})
            resolved = self.journal.resolve_batch(batch['id'], operation)
            self.assertIsNone(resolved['receipt'])
            self.assertEqual(resolved['resolution']['decision'], 'complete')
            self.assertEqual(self.journal.status(batch['subscription_id'])['pending_count'], 0)
            self.assertIsNone(self.journal.prepare_batch(batch['subscription_id']))

    def test_corrupt_rows_do_not_become_work(self):
        with self.journal.lock(self.scope):
            batch = self.prepared()
            self.journal._db.execute('UPDATE events SET metadata_json=?', ('[]',))
            self.error('journal_corrupt', lambda: self.journal.get_batch(batch['id']))
            self.error('journal_corrupt', lambda: self.journal.prepare_batch(batch['subscription_id']))

    def test_future_version_and_unrelated_database_are_not_modified(self):
        self.journal.close()
        database = self.directory / 'event-journal.sqlite3'
        with sqlite3.connect(database) as connection:
            connection.execute('PRAGMA user_version=99')
        before = database.read_bytes()
        self.error('unsupported_journal_version', lambda: EventJournal(self.directory))
        self.assertEqual(database.read_bytes(), before)
        with sqlite3.connect(database) as connection:
            connection.execute('PRAGMA user_version=0')
        self.error('journal_corrupt', lambda: EventJournal(self.directory))

    def test_insecure_modes_aliases_and_hardlinks_are_refused(self):
        self.journal.close()
        database = self.directory / 'event-journal.sqlite3'
        database.chmod(0o644)
        self.error('insecure_journal_file', lambda: EventJournal(self.directory))
        database.chmod(0o600)
        other = self.directory / 'copy'
        os.link(database, other)
        self.error('insecure_journal_file', lambda: EventJournal(self.directory))
        other.unlink()
        alias = self.directory.parent / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.error('insecure_journal_directory', lambda: EventJournal(alias))
        sidecar = self.directory / 'event-journal.sqlite3-journal'
        sidecar.symlink_to(database)
        self.error('invalid_journal_file', lambda: EventJournal(self.directory))
        sidecar.unlink()
        self.directory.chmod(0o755)
        self.error('insecure_journal_directory', lambda: EventJournal(self.directory))


if __name__ == '__main__':
    unittest.main()
