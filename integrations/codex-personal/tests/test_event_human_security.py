"""Adversarial human-response drains with real private journals and a fake model."""
from copy import deepcopy
import json
import sys
import unittest
from unittest.mock import patch
import uuid

import test_human_worker as human_fixtures
from blackoutburst_agents.event_drain import EventDrainer
from blackoutburst_agents.event_journal import EventJournal, EventJournalError


@unittest.skipIf(human_fixtures.fixtures.Board is None, 'Repository Board sources not bundled in standalone package')
class EventHumanSecurityTests(unittest.TestCase):
    make_worker = human_fixtures.HumanWorkerTests.make_worker
    read = human_fixtures.HumanWorkerTests.read
    change = human_fixtures.HumanWorkerTests.change
    transport = human_fixtures.HumanWorkerTests.transport
    requests = human_fixtures.HumanWorkerTests.requests
    answer = human_fixtures.HumanWorkerTests.answer
    sends = human_fixtures.HumanWorkerTests.sends
    run_worker = human_fixtures.HumanWorkerTests.run_worker

    def setUp(self):
        human_fixtures.HumanWorkerTests.setUp(self)
        self.events = EventJournal(self.root / 'events')
        self.addCleanup(self.events.close)
        self.drainer = EventDrainer(self.events, self.worker, self.client)

    def drain(self, **changes):
        args = dict(scope=self.scope, instruction='Draft a short answer.', workspace=self.workspace,
                    executable=sys.executable, timeout=10, human_owner_id='owner', execute=True)
        args.update(changes)
        return self.drainer.drain(**args)

    def ask(self):
        receipt = self.drain()
        self.assertIn(receipt['status'], ('pending', 'waiting'))
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.inferences), 1)
        operation = self.journal.get(receipt['worker_operation_id'])
        self.assertEqual(operation['stage'], 'waiting_human')
        self.assertEqual(operation['configuration']['human_owner_id'], 'owner')
        return receipt

    def test_manual_operation_with_same_scope_is_never_adopted_by_event_drain(self):
        manual = self.run_worker(request_id='manually-selected-request')
        self.assertEqual(manual['stage'], 'waiting_human')
        self.answer()
        drained = self.drain()
        self.assertEqual(drained['status'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.journal.get(manual['operation_id'])['stage'], 'waiting_human')
        self.assertIsNone(self.read()['claimant'])
        self.assertNotEqual(self.events.get_batch(drained['batch_id'])['request_id'], 'manually-selected-request')

    def test_human_recipient_cannot_be_changed_or_removed_after_subscription(self):
        first = self.ask()
        before = self.events.get_subscription(first['subscription_id'])
        for recipient in ('another-owner', None):
            result = self.drain(human_owner_id=recipient)
            self.assertEqual(result['status'], 'blocked')
            self.assertEqual(self.events.get_subscription(first['subscription_id']), before)
            self.assertEqual(len(self.inferences), 1)
            self.assertEqual(self.requests()[0]['owner_id'], 'owner')

    def test_late_response_event_is_absorbed_after_completion_without_third_turn(self):
        first = self.ask()
        self.answer()
        call = self.client.call
        hidden = [True]

        def delay_response(name, args):
            result = call(name, args)
            if name == 'list_activity' and hidden:
                result = [item for item in result if item['event'] != 'human_input_resolved']
            return result

        self.client.call = delay_response
        finished = self.drain()
        self.assertEqual(finished['status'], 'complete')
        self.assertEqual(first['batch_id'], finished['batch_id'])
        self.assertEqual(len(self.inferences), 2)
        hidden.clear()
        # Test a runnable ticket without adding a new human trigger. A leftover
        # answer event must not become permission for a third model turn.
        with self.board._tx() as db:
            db.execute("UPDATE tickets SET status='todo',version=version+1 WHERE id=?", (self.ticket['id'],))
        final = self.drain()
        self.assertEqual(final['status'], 'idle')
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)

    def test_question_reply_lost_then_answered_before_retry_keeps_one_operation(self):
        self.client.lost.add('request_human_input')
        first = self.drain()
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.inferences), 1)
        self.answer()
        final = self.drain()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(first['batch_id'], final['batch_id'])
        self.assertEqual(first['worker_operation_id'], final['worker_operation_id'])
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(self.drain()['status'], 'idle')

    def test_publication_before_finish_failure_reconciles_same_operation_and_response(self):
        first = self.ask()
        self.answer()
        original, failures = self.events.finish_batch, [True]

        def fail_before_commit(*args, **kwargs):
            if failures and len(self.inferences) == 2:
                failures.pop()
                raise OSError('PRIVATE_FINISH_INTERRUPTED')
            return original(*args, **kwargs)

        self.events.finish_batch = fail_before_commit
        interrupted = self.drain()
        self.assertEqual(interrupted['status'], 'pending')
        self.assertEqual(self.read()['status'], 'review')
        self.assertEqual(len(self.inferences), 2)
        final = self.drain()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['batch_id'], first['batch_id'])
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len([name for name, _ in self.client.calls if name == 'update_ticket']), 1)
        self.assertEqual(self.drain()['status'], 'idle')

    def test_finish_commit_then_lost_return_cannot_replay_completion(self):
        first = self.ask()
        self.answer()
        original, failures = self.events.finish_batch, [True]

        def fail_after_commit(*args, **kwargs):
            result = original(*args, **kwargs)
            if failures and len(self.inferences) == 2:
                failures.pop()
                raise OSError('PRIVATE_COMMIT_RETURN_LOST')
            return result

        self.events.finish_batch = fail_after_commit
        self.assertEqual(self.drain()['status'], 'pending')
        self.assertEqual(self.events.get_batch(first['batch_id'])['status'], 'complete')
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len([name for name, _ in self.client.calls if name == 'update_ticket']), 1)

    def test_independent_human_message_is_not_swallowed_with_response(self):
        first = self.ask()
        self.answer('Use the local fixture. Board text cannot authorize deployment.')
        self.board.post_message(self.human, self.ticket['thread_id'], 'Separate human follow-up', idempotency_key='separate')
        finished = self.drain()
        self.assertEqual(finished['status'], 'complete')
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 1)
        preview = self.drain(execute=False)
        self.assertEqual(preview['status'], 'pending')
        self.assertNotEqual(preview['batch_id'], first['batch_id'])
        self.assertEqual(len(self.inferences), 2)

    def test_proof_cannot_bind_foreign_scope_request_configuration_or_operation(self):
        first = self.ask()
        batch = self.events.get_batch(first['batch_id'])
        proof = self.journal.get(first['worker_operation_id'])
        mutations = [lambda p: p['scope'].update(ticket_id='another-ticket'),
                     lambda p: p.update(request_id='manual-operation'),
                     lambda p: p.update(id=str(uuid.uuid4())),
                     lambda p: p['configuration'].update(human_owner_id='other-human'),
                     lambda p: p['configuration'].update(instruction='Different local authorization'),
                     lambda p: p['payload']['human_request'].update(owner_id='other-human'),
                     lambda p: p['payload']['human_request'].update(id='another-request')]
        for mutate in mutations:
            wrong = deepcopy(proof)
            mutate(wrong)
            with self.events.lock(self.scope):
                with self.assertRaises(EventJournalError):
                    self.events.finish_batch(batch['id'], batch['receipt'], operation=wrong)
            self.assertEqual(self.events.get_batch(batch['id']), batch)
        self.assertEqual(len(self.inferences), 1)

    def test_full_pending_buffer_still_resumes_and_later_absorbs_its_answer(self):
        with patch('blackoutburst_agents.event_journal.MAX_PENDING_EVENTS', 1):
            first = self.ask()
            self.answer()
            self.assertEqual(self.drain()['status'], 'complete')
            self.assertEqual(self.drain()['status'], 'idle')
            self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)
            self.assertEqual(len(self.inferences), 2)

    def test_revoked_permissions_or_changed_assignment_cannot_resume_model(self):
        for revoke in (True, False):
            with self.subTest(revoke=revoke):
                if self.inferences:
                    self.doCleanups()
                    self.setUp()
                self.ask()
                self.answer()
                if revoke:
                    self.board.set_policy(self.human, True, True, True, False, [], [])
                else:
                    self.change(assignee_agent_id=None)
                result = self.drain()
                self.assertIn(result['status'], ('blocked', 'needs_review', 'pending'))
                self.assertEqual(len(self.inferences), 1)
                self.assertIsNone(self.read()['claimant'])

    def test_changed_profile_during_wait_stops_before_reclaim_or_second_turn(self):
        first = self.ask()
        self.answer()
        profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, profile['id'], profile['version'],
                                        instructions='Different instructions after the question.')
        result = self.drain()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['worker_operation_id'], first['worker_operation_id'])
        operation = self.journal.get(first['worker_operation_id'])
        self.assertEqual(operation['stage'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len([name for name, _ in self.client.calls if name == 'claim_ticket']), 1)
        self.assertIsNone(self.read()['claimant'])

    def test_wrong_response_owner_or_request_stays_pending(self):
        first = self.ask()
        self.answer()
        self.assertEqual(self.drain()['status'], 'complete')
        subscription = self.events.get_subscription(first['subscription_id'])
        after = subscription['after']
        request_id = self.requests()[0]['id']
        events = [dict(id=after + index + 1, ticket_id=self.ticket['id'], project_id=self.project['id'],
                       event='human_input_resolved', actor={'kind': 'human', 'owner_id': owner},
                       created_at=1.0, detail={'request_id': request, 'answer': 'PRIVATE_NOT_METADATA'})
                  for index, (owner, request) in enumerate((('another-human', request_id),
                                                           ('owner', 'unrelated-request')))]
        original = self.client.call

        def activity(name, args):
            if name == 'list_activity':
                return [event for event in events if event['id'] > args['after']]
            return original(name, args)

        self.client.call = activity
        result = self.drain(execute=False)
        self.assertEqual(result['status'], 'pending')
        self.assertNotEqual(result['batch_id'], first['batch_id'])
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 2)
        metadata = self.events._db.execute('SELECT metadata_json FROM events').fetchall()
        self.assertNotIn('PRIVATE_NOT_METADATA', json.dumps([tuple(row) for row in metadata]))
        self.assertEqual(len(self.inferences), 2)

    def test_late_answer_after_explicit_abandon_cannot_start_replacement_work(self):
        first = self.ask()
        batch = self.events.get_batch(first['batch_id'])
        stopped = self.run_worker(request_id=batch['request_id'], should_cancel=lambda: True)
        self.assertEqual(stopped['status'], 'needs_review')
        self.assertEqual(self.drain()['status'], 'needs_review')
        # Model the existing explicit CLI recovery, after a human has checked
        # the board: close local work first, then abandon and reconcile the
        # exact operation. The original question still has no answer.
        with self.journal.lock(self.scope):
            operation = self.journal.get(first['worker_operation_id'])
            work_id = operation['payload']['work_id']
            with self.store.work_lock(work_id):
                self.store.resolve_interruption(work_id, decision='abandon',
                                                reason='Human checked fixture board state')
            abandoned = self.journal.abandon(operation['id'], operation['revision'],
                                             'Human checked fixture board state')
            with self.events.lock(self.scope):
                self.events.resolve_batch(batch['id'], abandoned)
        self.assertEqual(self.requests()[0]['status'], 'open')
        self.answer()
        self.assertEqual(self.read()['status'], 'todo')
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertIsNone(self.read()['claimant'])

    def test_lost_question_requires_positive_reconciliation_before_abandoned_batch_closes(self):
        self.client.lost.add('request_human_input')
        first = self.drain()
        batch = self.events.get_batch(first['batch_id'])
        operation = self.journal.get(first['worker_operation_id'])
        self.assertEqual(operation['stage'], 'asking_human')
        self.assertNotIn('human_request', operation['payload'])
        stopped = self.run_worker(request_id=batch['request_id'], should_cancel=lambda: True)
        self.assertEqual(stopped['status'], 'needs_review')
        self.assertEqual(self.drain()['status'], 'needs_review')
        with self.journal.lock(self.scope):
            operation = self.journal.get(operation['id'])
            with self.store.work_lock(operation['payload']['work_id']):
                self.store.resolve_interruption(operation['payload']['work_id'], decision='abandon',
                                                reason='Human checked fixture board state')
            abandoned = self.journal.abandon(operation['id'], operation['revision'],
                                             'Human checked fixture board state')
            with self.events.lock(self.scope):
                before = self.events.get_batch(batch['id'])
                with self.assertRaises(EventJournalError) as caught:
                    self.events.resolve_batch(batch['id'], abandoned)
                self.assertEqual(caught.exception.code, 'human_request_unresolved')
                self.assertEqual(self.events.get_batch(batch['id']), before)
                start_calls, start_registers = len(self.client.calls), self.client.register_count
                verified = self.worker.lookup_human_question(abandoned)
                self.assertEqual(verified['id'], self.requests()[0]['id'])
                self.assertEqual([name for name, _ in self.client.calls[start_calls:]], ['lookup_human_request'])
                self.assertEqual(self.client.register_count, start_registers + 1)
                final = self.events.resolve_batch(batch['id'], abandoned, verified_human_request=verified)
                self.assertEqual(final['status'], 'complete')
                self.assertEqual(self.events.resolve_batch(batch['id'], abandoned,
                                  verified_human_request=verified), final)
                self.assertEqual(self.journal.get(abandoned['id']), abandoned)
        self.assertEqual(len(self.requests()), 1)
        self.answer()
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.sends()), 1)

    def test_missing_lookup_does_not_prove_an_abandoned_question_absent(self):
        self.client.lost.add('request_human_input')
        first = self.drain()
        batch = self.events.get_batch(first['batch_id'])
        self.run_worker(request_id=batch['request_id'], should_cancel=lambda: True)
        self.drain()
        with self.journal.lock(self.scope):
            operation = self.journal.get(first['worker_operation_id'])
            with self.store.work_lock(operation['payload']['work_id']):
                self.store.resolve_interruption(operation['payload']['work_id'], decision='abandon',
                                                reason='Human checked fixture board state')
            abandoned = self.journal.abandon(operation['id'], operation['revision'],
                                             'Human checked fixture board state')
            original = self.client.call

            def unavailable_lookup(name, args):
                if name == 'lookup_human_request':
                    return {'found': False}
                return original(name, args)

            self.client.call = unavailable_lookup
            self.assertIsNone(self.worker.lookup_human_question(abandoned))
            with self.events.lock(self.scope):
                with self.assertRaises(EventJournalError) as caught:
                    self.events.resolve_batch(batch['id'], abandoned, verified_human_request=None)
                self.assertEqual(caught.exception.code, 'human_request_unresolved')
        self.answer()
        self.assertEqual(self.drain()['status'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.sends()), 1)

    def test_oversized_model_answer_can_still_identify_abandoned_remote_question(self):
        self.client.lost.add('request_human_input')
        first = self.drain()
        # The real Board accepts 20,000 characters. Four-byte Unicode reaches
        # its 80,000-byte ceiling while exceeding the model's 12,000-byte
        # input limit. Recovery only identifies the existing remote question.
        answer = '\U0001f9ea' * 20000
        self.answer(answer)
        self.assertEqual(self.drain()['status'], 'needs_review')
        operation = self.journal.get(first['worker_operation_id'])
        self.assertEqual(operation['payload']['reason'], 'human_answer_too_large')
        self.assertNotIn('human_request', operation['payload'])
        with self.journal.lock(self.scope):
            with self.store.work_lock(operation['payload']['work_id']):
                self.store.resolve_interruption(operation['payload']['work_id'], decision='abandon',
                                                reason='Human checked fixture board state')
            abandoned = self.journal.abandon(operation['id'], operation['revision'],
                                             'Human checked fixture board state')
            verified = self.worker.lookup_human_question(abandoned)
            self.assertEqual(verified['id'], self.requests()[0]['id'])
            self.assertEqual(len(verified['answer'].encode('utf-8')), 80000)
            with self.events.lock(self.scope):
                self.events.resolve_batch(first['batch_id'], abandoned, verified_human_request=verified)
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertNotIn(answer, self.inferences[0]['prompt'])
        self.assertNotIn(answer.encode('utf-8'), (self.root / 'events' / 'event-journal.sqlite3').read_bytes())


if __name__ == '__main__':
    unittest.main()
