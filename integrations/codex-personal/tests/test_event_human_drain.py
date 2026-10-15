"""Explicit activity drain → one question → same durable operation, real Board."""
from copy import deepcopy
import json
import sys
import unittest

import test_board_worker as fixtures
import test_human_worker as human_fixtures
from blackoutburst_agents.event_drain import EventDrainer
from blackoutburst_agents.event_journal import EventJournal
from blackoutburst_agents.work_journal import WorkJournal


@unittest.skipIf(fixtures.Board is None, 'Repository Board sources not bundled in standalone package')
class EventHumanDrainTests(unittest.TestCase):
    make_worker = fixtures.BoardWorkerTests.make_worker
    read = fixtures.BoardWorkerTests.read
    change = fixtures.BoardWorkerTests.change
    transport = human_fixtures.HumanWorkerTests.transport
    requests = human_fixtures.HumanWorkerTests.requests
    answer = human_fixtures.HumanWorkerTests.answer
    sends = human_fixtures.HumanWorkerTests.sends

    def setUp(self):
        human_fixtures.HumanWorkerTests.setUp(self)
        self.events = EventJournal(self.root / 'events')
        self.addCleanup(self.events.close)
        self.drainer = EventDrainer(self.events, self.worker, self.client)

    def drain(self, **overrides):
        args = {'scope': self.scope, 'instruction': 'Draft a short answer.',
                'workspace': self.workspace, 'executable': sys.executable, 'timeout': 10,
                'human_owner_id': 'owner', 'execute': True}
        args.update(overrides)
        return self.drainer.drain(**args)

    def operation(self, receipt):
        return self.journal.get(receipt['worker_operation_id'])

    def restart(self):
        self.events.close()
        self.journal.close()
        self.events = EventJournal(self.root / 'events')
        self.journal = WorkJournal(self.root / 'journal')
        self.addCleanup(self.events.close)
        self.addCleanup(self.journal.close)
        self.worker = self.make_worker()
        self.drainer = EventDrainer(self.events, self.worker, self.client)

    def test_question_wait_answer_same_batch_operation_session_and_no_third_turn(self):
        preview = self.drain(execute=False)
        self.assertEqual(self.inferences, [])
        first = self.drain()
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(self.operation(first)['stage'], 'waiting_human')
        self.assertEqual(first['batch_id'], preview['batch_id'])
        self.assertEqual(self.read()['status'], 'blocked')
        self.assertIsNone(self.read()['claimant'])
        self.assertEqual(self.drain()['batch_id'], first['batch_id'])
        self.assertEqual(len(self.inferences), 1)
        self.answer()
        self.assertEqual(self.drain(execute=False)['status'], 'pending')
        self.assertEqual(len(self.inferences), 1)
        final = self.drain()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['batch_id'], first['batch_id'])
        self.assertEqual(final['worker_operation_id'], first['worker_operation_id'])
        self.assertEqual(len(self.inferences), 2)
        self.assertIsNotNone(self.inferences[1]['resume'])
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)
        body = (self.root / 'events' / 'event-journal.sqlite3').read_bytes()
        for private in (b'Which environment', b'Use the local test environment.', b'The plan describes'):
            self.assertNotIn(private, body)
        self.assertNotIn('Which environment', json.dumps(final))

    def test_answer_event_arriving_after_completion_is_absorbed_even_when_ticket_runnable(self):
        original, hide = self.client.call, [True]
        def delayed(name, args):
            result = original(name, args)
            if name == 'list_activity' and hide[0]:
                # Simulate an activity read preceding a just-committed answer;
                # do not move the cursor past the hidden event.
                result = [item for item in result if item['event'] != 'human_input_resolved']
            return result
        self.client.call = delayed
        first = self.drain()
        self.answer()
        self.assertEqual(self.drain()['status'], 'complete')
        # No extra independent human event: specifically test that the delayed
        # answer itself cannot dispatch a new batch when readiness permits it.
        with self.board._tx() as db:
            db.execute("UPDATE tickets SET status='todo',version=version+1 WHERE id=?", (self.ticket['id'],))
        hide[0] = False
        self.restart()
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(self.events.status(first['subscription_id'])['pending_count'], 0)
        self.assertEqual(len(self.inferences), 2)

    def test_lost_question_reply_then_answer_reuses_original_operation(self):
        self.client.lost.add('request_human_input')
        first = self.drain()
        self.assertEqual(self.operation(first)['stage'], 'asking_human')
        self.answer()
        self.restart()
        final = self.drain()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['batch_id'], first['batch_id'])
        self.assertEqual(final['worker_operation_id'], first['worker_operation_id'])
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.drain()['status'], 'idle')

    def test_publication_reply_lost_is_reconciled_without_model_or_second_publication(self):
        first = self.drain()
        self.answer()
        self.client.lost.add('update_ticket')
        pending = self.drain()
        self.assertEqual(self.operation(pending)['stage'], 'publishing')
        self.restart()
        final = self.drain()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['worker_operation_id'], first['worker_operation_id'])
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len([1 for name, _ in self.client.calls if name == 'update_ticket']), 1)
        self.assertEqual(self.drain()['status'], 'idle')

    def test_final_journal_failure_before_commit_keeps_batch_until_cached_confirmation(self):
        first = self.drain()
        self.answer()
        original, fail = self.events.finish_batch, [True]
        def finish(*args, **kwargs):
            if args[1]['status'] == 'complete' and fail:
                fail.pop()
                raise OSError('PRIVATE_STORAGE_FAILURE')
            return original(*args, **kwargs)
        self.events.finish_batch = finish
        self.assertEqual(self.drain()['status'], 'pending')
        self.assertEqual(self.read()['status'], 'review')
        self.assertEqual(self.events.get_batch(first['batch_id'])['status'], 'pending')
        self.restart()
        self.assertEqual(self.drain()['status'], 'complete')
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len([1 for name, _ in self.client.calls if name == 'update_ticket']), 1)

    def test_commit_then_lost_receipt_preserves_absorption_on_restart(self):
        self.drain()
        self.answer()
        original, fail = self.events.finish_batch, [True]
        def finish(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[1]['status'] == 'complete' and fail:
                fail.pop()
                raise OSError('PRIVATE_STORAGE_FAILURE')
            return result
        self.events.finish_batch = finish
        self.assertEqual(self.drain()['status'], 'pending')
        self.restart()
        self.assertEqual(self.drain()['status'], 'idle')
        self.assertEqual(len(self.inferences), 2)

    def test_reassignment_during_wait_prevents_resume(self):
        self.drain()
        self.answer()
        self.change(assignee_agent_id=None)
        final = self.drain()
        self.assertEqual(final['status'], 'needs_review')
        self.assertEqual(self.operation(final)['payload']['reason'], 'human_resume_reassigned')
        self.assertEqual(len(self.inferences), 1)
        self.assertIsNone(self.read()['claimant'])

    def test_revocation_prevents_second_turn_and_keeps_pending_batch(self):
        first = self.drain()
        self.answer()
        self.board.set_policy(self.human, True, True, True, False, [], [])
        final = self.drain()
        self.assertEqual((final['status'], final['reason']), ('blocked', 'work_permission_denied'))
        self.assertEqual(self.events.get_batch(first['batch_id'])['status'], 'pending')
        self.assertEqual(len(self.inferences), 1)

    def test_changed_profile_requires_review_without_adopting_new_instructions(self):
        self.drain()
        profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, profile['id'], profile['version'],
                                        instructions='Different private instructions.')
        self.answer()
        final = self.drain()
        self.assertEqual(final['status'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.assertIsNone(self.read()['claimant'])

    def test_recipient_cannot_be_added_removed_or_changed_after_opening_subscription(self):
        first = self.drain()
        for value in (None, 'someone-else', '', 123, 'x\ninvalid'):
            with self.subTest(value=value):
                final = self.drain(human_owner_id=value)
                self.assertEqual((final['status'], final['reason']), ('blocked', 'invalid_drain_request'))
                self.assertEqual(self.events.get_batch(first['batch_id'])['status'], 'pending')
        self.assertEqual(len(self.inferences), 1)

    def test_independent_message_during_question_survives_answer_absorption(self):
        first = self.drain()
        self.board.post_message(self.human, self.ticket['thread_id'], 'A separate comment.', idempotency_key='independent-message')
        self.answer()
        self.assertEqual(self.drain()['status'], 'complete')
        next_result = self.drain()
        self.assertEqual(next_result['status'], 'waiting')
        self.assertNotEqual(next_result['batch_id'], first['batch_id'])
        batch = self.events.get_batch(next_result['batch_id'])
        events = self.board.list_activity(self.human, ticket_id=self.ticket['id'], order='asc')
        names = {event['id']: event['event'] for event in events}
        self.assertEqual([names[item] for item in batch['event_ids']], ['ticket_message_posted'])
        self.assertEqual(len(self.inferences), 2)


if __name__ == '__main__':
    unittest.main()
