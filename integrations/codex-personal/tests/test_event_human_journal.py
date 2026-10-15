"""Proof-bound response absorption, metadata privacy and journal migration."""
from copy import deepcopy
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import test_event_journal as fixtures
from blackoutburst_agents.event_journal import EventJournal, EventJournalError


class EventHumanJournalTests(unittest.TestCase):
    setUp = fixtures.EventJournalTests.setUp
    error = fixtures.EventJournalTests.error
    event = fixtures.EventJournalTests.event
    subscription = fixtures.EventJournalTests.subscription

    def prepared(self, count=1):
        self.configuration['human_owner_id'] = 'recipient'
        sub = self.subscription()
        self.journal.append_page(sub['id'], 0, [self.event(n) for n in range(1, count + 1)])
        batch = self.journal.prepare_batch(sub['id'])
        return sub, self.journal.start_batch(batch['id'])

    def proof(self, batch, *, resolved=False, stage=None):
        identifier = str(uuid.uuid4())
        return {'id': identifier, 'scope': self.scope, 'configuration': self.configuration,
                'request_id': batch['request_id'], 'revision': 10,
                'stage': stage or ('complete' if resolved else 'waiting_human'),
                'payload': {'human_used': True,
                    'human_intent': {'owner_id': 'recipient', 'question': 'PRIVATE QUESTION\nSecond line',
                                     'idempotency_key': 'board-human-' + identifier, 'expected_version': 2},
                    'human_request': {'id': 'question-id', 'owner_id': 'recipient', 'ticket_id': 'ticket',
                        'question': 'PRIVATE QUESTION\nSecond line', 'status': 'resolved' if resolved else 'open',
                        'answer': 'PRIVATE ANSWER' if resolved else '', 'resolved_at': 123.0 if resolved else None,
                        'requested_by': {'kind': 'agent', 'owner_id': 'owner', 'agent_id': 'agent',
                                         'device_id': 'device', 'session_id': 'session'}}}}

    def receipt(self, operation):
        status = 'complete' if operation['stage'] == 'complete' else 'pending'
        return {'status': status, 'stage': operation['stage'], 'reason': 'test_result',
                'cached': False, 'operation_id': operation['id']}

    def response(self, number, owner='recipient', request='question-id'):
        return {**self.event(number, event='human_input_resolved'), 'request_id': request,
                'actor': {'kind': 'human', 'owner_id': owner}}

    def test_open_question_link_absorbs_exact_response_but_does_not_complete_batch(self):
        with self.journal.lock(self.scope):
            sub, batch = self.prepared()
            proof = self.proof(batch)
            self.journal.finish_batch(batch['id'], self.receipt(proof), operation=proof)
            page = [self.response(2), self.response(3, owner='different'), self.response(4, request='unrelated'), self.event(5)]
            state = self.journal.append_page(sub['id'], 1, page)
            self.assertEqual(state['pending_count'], 4)  # original + 3 independent events
            self.assertEqual(self.journal.get_batch(batch['id'])['status'], 'pending')
            self.assertEqual(self.journal.prepare_batch(sub['id'])['id'], batch['id'])
            self.assertEqual(self.journal.append_page(sub['id'], 1, page), state)
            self.assertEqual(self.journal._db.execute('SELECT event_id FROM consumed_response_events').fetchall()[0][0], 2)
        raw = (self.directory / 'event-journal.sqlite3').read_bytes()
        self.assertNotIn(b'PRIVATE QUESTION', raw)
        self.assertNotIn(b'PRIVATE ANSWER', raw)

    def test_proof_other_scope_request_configuration_or_author_rolls_back_everything(self):
        with self.journal.lock(self.scope):
            sub, batch = self.prepared()
            proof = self.proof(batch)
            changes = [lambda op: op['scope'].update(ticket_id='another'),
                       lambda op: op.update(request_id='manual-operation'),
                       lambda op: op['configuration'].update(human_owner_id='other'),
                       lambda op: op['payload']['human_request']['requested_by'].update(session_id='other'),
                       lambda op: op['payload']['human_request'].update(question='not the intent'),
                       lambda op: op['payload']['human_intent'].update(idempotency_key='other-key'),
                       lambda op: op.update(id=str(uuid.uuid4()))]
            for change in changes:
                candidate = deepcopy(proof)
                change(candidate)
                self.error('operation_proof_mismatch', lambda: self.journal.finish_batch(
                    batch['id'], self.receipt(proof), operation=candidate))
                self.assertEqual(self.journal.get_batch(batch['id']), batch)
                self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM human_links').fetchone()[0], 0)
            self.error('operation_proof_required', lambda: self.journal.finish_batch(batch['id'], self.receipt(proof)))
            self.assertEqual(self.journal.get_subscription(sub['id'])['pending_count'], 1)

    def test_link_consumption_and_completion_are_one_transaction(self):
        with self.journal.lock(self.scope):
            sub, batch = self.prepared()
            proof = self.proof(batch, resolved=True)
            self.journal.append_page(sub['id'], 1, [self.response(2)])
            original = self.journal._consume_responses
            def fail(*args):
                original(*args)
                raise EventJournalError('journal_unavailable')
            with patch.object(self.journal, '_consume_responses', fail):
                self.error('journal_unavailable', lambda: self.journal.finish_batch(
                    batch['id'], self.receipt(proof), operation=proof))
            self.assertEqual(self.journal.get_batch(batch['id']), batch)
            self.assertEqual(self.journal.get_subscription(sub['id'])['pending_count'], 2)
            self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM human_links').fetchone()[0], 0)
            self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM consumed_response_events').fetchone()[0], 0)
            final = self.journal.finish_batch(batch['id'], self.receipt(proof), operation=proof)
            self.assertEqual(final['status'], 'complete')
            self.assertEqual(self.journal.get_subscription(sub['id'])['pending_count'], 0)
            self.assertEqual(self.journal.finish_batch(batch['id'], self.receipt(proof), operation=proof), final)

    def test_response_does_not_expand_full_batch_or_exhaust_pending_capacity(self):
        with self.journal.lock(self.scope):
            sub, batch = self.prepared(count=200)
            proof = self.proof(batch)
            self.journal.finish_batch(batch['id'], self.receipt(proof), operation=proof)
            with patch('blackoutburst_agents.event_journal.MAX_PENDING_EVENTS', 200):
                state = self.journal.append_page(sub['id'], 200, [self.response(201)])
            self.assertEqual(state['after'], 201)
            self.assertEqual(state['pending_count'], 200)
            self.assertEqual(len(self.journal.get_batch(batch['id'])['event_ids']), 200)

    def test_closed_abandoned_question_keeps_response_link_without_new_batch(self):
        with self.journal.lock(self.scope):
            sub, batch = self.prepared()
            proof = self.proof(batch)
            self.journal.finish_batch(batch['id'], self.receipt(proof), operation=proof)
            proof['stage'] = 'abandoned'
            proof['revision'] += 1
            self.journal.resolve_batch(batch['id'], proof)
            state = self.journal.append_page(sub['id'], 1, [self.response(2)])
            self.assertEqual(state['pending_count'], 0)
            self.assertIsNone(self.journal.prepare_batch(sub['id']))

    def test_ambiguous_question_requires_positive_exact_proof_before_closing(self):
        with self.journal.lock(self.scope):
            sub, batch = self.prepared()
            original = self.proof(batch)
            question = original['payload']['human_request']
            ambiguous = deepcopy(original)
            ambiguous['stage'] = 'abandoned'
            del ambiguous['payload']['human_request']
            before = deepcopy(ambiguous)
            self.error('human_request_unresolved', lambda: self.journal.resolve_batch(batch['id'], ambiguous))
            self.error('human_request_unresolved', lambda: self.journal.resolve_batch(
                batch['id'], ambiguous, verified_human_request={'found': False}))
            self.assertEqual(self.journal.get_batch(batch['id']), batch)
            self.assertEqual(self.journal._db.execute('SELECT COUNT(*) FROM human_links').fetchone()[0], 0)
            for change in ({'owner_id': 'other'}, {'ticket_id': 'other'}, {'question': 'other'},
                           {'requested_by': {**question['requested_by'], 'session_id': 'another'}}):
                foreign = {**question, **change}
                self.error('operation_proof_mismatch', lambda: self.journal.resolve_batch(
                    batch['id'], ambiguous, verified_human_request=foreign))
                self.assertEqual(self.journal.get_batch(batch['id']), batch)
            final = self.journal.resolve_batch(batch['id'], ambiguous, verified_human_request=question)
            self.assertEqual(final['status'], 'complete')
            self.assertEqual(ambiguous, before)  # Never rewrite the private WorkJournal proof.
            state = self.journal.append_page(sub['id'], 1, [self.response(2)])
            self.assertEqual(state['pending_count'], 0)
            self.assertIsNone(self.journal.prepare_batch(sub['id']))
            self.assertEqual(self.journal.resolve_batch(batch['id'], ambiguous,
                                                       verified_human_request=question), final)
            self.error('operation_proof_mismatch', lambda: self.journal.resolve_batch(
                batch['id'], ambiguous, verified_human_request={**question, 'id': 'other-question'}))

    def test_known_question_rejects_foreign_optional_lookup_proof(self):
        with self.journal.lock(self.scope):
            _, batch = self.prepared()
            operation = self.proof(batch, stage='abandoned')
            foreign = {**operation['payload']['human_request'], 'id': 'unrelated-question'}
            self.error('resolution_mismatch', lambda: self.journal.resolve_batch(
                batch['id'], operation, verified_human_request=foreign))
            self.assertEqual(self.journal.get_batch(batch['id']), batch)

    def test_v1_migration_preserves_existing_cursor_configuration_and_batch(self):
        with self.journal.lock(self.scope):
            sub = self.subscription()
            self.journal.append_page(sub['id'], 0, [self.event(5)])
            batch = self.journal.prepare_batch(sub['id'])
        self.journal.close()
        database = self.directory / 'event-journal.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('DROP TABLE consumed_response_events')
            db.execute('DROP TABLE human_links')
            db.execute('PRAGMA user_version=1')
        with EventJournal(self.directory) as migrated:
            self.assertEqual(migrated.get_batch(batch['id']), batch)
            self.assertEqual(migrated.get_subscription(sub['id'])['after'], 5)
            self.assertEqual(migrated.get_subscription(sub['id'])['configuration'], self.configuration)
            self.assertEqual(migrated._db.execute('PRAGMA user_version').fetchone()[0], 2)
            with migrated.lock(self.scope):
                with self.assertRaises(EventJournalError) as caught:
                    migrated.open_subscription(self.scope, {**self.configuration, 'human_owner_id': 'recipient'})
                self.assertEqual(caught.exception.code, 'subscription_configuration_conflict')

    def test_migration_failure_rolls_back_schema_and_preserves_v1_cursor(self):
        with self.journal.lock(self.scope):
            sub = self.subscription()
            self.journal.append_page(sub['id'], 0, [self.event(7)])
        self.journal.close()
        database = self.directory / 'event-journal.sqlite3'
        with sqlite3.connect(database) as db:
            db.execute('DROP TABLE consumed_response_events')
            db.execute('DROP TABLE human_links')
            db.execute('CREATE TABLE consumed_response_events(unexpected TEXT)')
            db.execute('PRAGMA user_version=1')
        self.error('journal_unavailable', lambda: EventJournal(self.directory))
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT after_id FROM subscriptions').fetchone()[0], 7)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='human_links'").fetchone())


if __name__ == '__main__':
    unittest.main()
