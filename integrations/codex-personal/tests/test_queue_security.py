"""Independent one-shot queue invariants with a real Board and fake inference."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
import sys
import unittest
from unittest.mock import patch

import test_board_worker as fixtures
import test_human_worker as human_fixtures
from blackoutburst_agents.queue_journal import QueueJournal, QueueJournalError
from blackoutburst_agents.queue_worker import QueueWorker
from blackoutburst_agents.cli import main


class AutonomousFixtureClient(fixtures.Client):
    def register(self):
        return self._register(autonomous=False)

    def register_for_selection(self):
        return self._register(autonomous=True)

    def _register(self, *, autonomous):
        self.register_count += 1
        if self.on_register:
            self.on_register(self.register_count)
        value = self.board.register_agent(self.raw, self.session_id, 'Queue fixture', [], 'codex', autonomous, 'worker')
        self.actor = self.board.actor_session(self.raw, self.session_id)
        self.registration = value
        return value


@unittest.skipIf(fixtures.Board is None, 'Repository Board sources not bundled in standalone package')
class QueueSecurityTests(unittest.TestCase):
    make_worker = fixtures.BoardWorkerTests.make_worker
    read = fixtures.BoardWorkerTests.read
    change = fixtures.BoardWorkerTests.change
    transport = human_fixtures.HumanWorkerTests.transport
    requests = human_fixtures.HumanWorkerTests.requests
    answer = human_fixtures.HumanWorkerTests.answer
    sends = human_fixtures.HumanWorkerTests.sends
    run_worker = human_fixtures.HumanWorkerTests.run_worker

    def setUp(self):
        human_fixtures.HumanWorkerTests.setUp(self)
        self.outputs = ['Ready for review.'] * 4
        self.client = AutonomousFixtureClient(self.board, self.raw)
        self.worker = self.make_worker(client=self.client)
        self.queue_journal = QueueJournal(self.root / 'queue')
        self.addCleanup(self.queue_journal.close)
        self.queue = QueueWorker(self.queue_journal, self.worker, self.client)
        self.queue_scope = {key: self.scope[key] for key in
                            ('board_endpoint', 'owner_id', 'agent_id', 'board_session_id')}

    def run_queue(self, **changes):
        arguments = dict(scope=self.queue_scope, request_id='explicit-queue-request',
                         project_id=self.project['id'], instruction='Draft a short answer.',
                         workspace=self.workspace, executable=sys.executable,
                         model='fixture-model', timeout=10)
        arguments.update(changes)
        return self.queue.run(**arguments)

    def another_ticket(self, title='Another task', **changes):
        return self.board.create_ticket(self.human, self.project['id'], title,
                                        idempotency_key=title, **changes)

    def selection_calls(self):
        return [arguments for name, arguments in self.client.calls if name == 'claim_next_ticket']

    def lose_selection_and_reconciliation(self):
        original, selected = self.client.call, []

        def interrupted(name, arguments):
            if name == 'lookup_ticket_selection' and selected:
                raise TimeoutError('Fixture lookup unavailable after lost selection')
            value = original(name, arguments)
            if name == 'claim_next_ticket':
                selected.append(True)
                raise TimeoutError('Fixture selection response lost')
            return value

        self.client.call = interrupted
        return lambda: setattr(self.client, 'call', original)

    def test_lost_selection_reply_never_picks_new_higher_priority_ticket(self):
        restore = self.lose_selection_and_reconciliation()
        first = self.run_queue()
        self.assertEqual(first['status'], 'pending')
        restore()
        urgent = self.another_ticket('More urgent after the lost reply', priority='urgent')
        final = self.run_queue()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['operation_id'], first['operation_id'])
        self.assertEqual(final['ticket_id'], self.ticket['id'])
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertEqual(len(self.inferences), 1)
        self.assertIsNone(self.board.get_ticket(self.human, urgent['id'])['ticket']['claimant'])
        self.assertEqual(self.run_queue()['operation_id'], first['operation_id'])
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertEqual(len(self.inferences), 1)

    def test_negative_selection_is_stable_when_a_new_ticket_arrives(self):
        self.change(status='done', outcome='Fixture closed', evidence='Fixture evidence')
        first = self.run_queue()
        self.assertEqual(first['status'], 'idle')
        new = self.another_ticket('Arrived after an empty selection', priority='urgent')
        final = self.run_queue()
        self.assertEqual(final['status'], 'idle')
        self.assertEqual(final['operation_id'], first['operation_id'])
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertEqual(self.inferences, [])
        self.assertIsNone(self.board.get_ticket(self.human, new['id'])['ticket']['claimant'])

    def test_missing_read_only_lookup_cannot_fall_back_to_mutating_selection(self):
        original = self.client.call

        def old_server(name, arguments):
            if name == 'lookup_ticket_selection':
                raise RuntimeError('Fixture unknown tool')
            return original(name, arguments)

        self.client.call = old_server
        first = self.run_queue()
        self.assertIn(first['status'], ('pending', 'needs_review'))
        self.assertEqual(self.selection_calls(), [])
        self.assertEqual(self.inferences, [])
        self.assertIsNone(self.read()['claimant'])

    def test_waiting_question_holds_original_selection_until_same_operation_resumes(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'}),
                        json.dumps({'kind': 'result', 'text': 'Use the local fixture.'})]
        first = self.run_queue(human_owner_id='owner')
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(len(self.requests()), 1)
        urgent = self.another_ticket('Urgent while waiting', priority='urgent')
        pending = self.run_queue(human_owner_id='owner')
        self.assertEqual(pending['operation_id'], first['operation_id'])
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.selection_calls()), 1)
        self.answer()
        final = self.run_queue(human_owner_id='owner')
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['operation_id'], first['operation_id'])
        self.assertEqual(final['ticket_id'], self.ticket['id'])
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertEqual(len(self.inferences), 2)
        self.assertIsNone(self.board.get_ticket(self.human, urgent['id'])['ticket']['claimant'])

    def test_instruction_model_project_and_recipient_remain_immutable_after_selection(self):
        self.lose_selection_and_reconciliation()
        first = self.run_queue()
        original = self.queue_journal.get(first['operation_id'])
        changes = ({'instruction': 'A different instruction'}, {'model': 'another-model'},
                   {'project_id': 'another-project'}, {'human_owner_id': 'owner'})
        for change in changes:
            with self.subTest(change=change):
                result = self.run_queue(**change)
                self.assertIn(result['status'], ('blocked', 'needs_review', 'pending'))
                self.assertEqual(self.queue_journal.get(first['operation_id']), original)
                self.assertEqual(len(self.selection_calls()), 1)
                self.assertEqual(self.inferences, [])

    def test_profile_edit_during_wait_does_not_resume_or_select_second_ticket(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'})]
        first = self.run_queue(human_owner_id='owner')
        self.assertEqual(first['status'], 'pending')
        urgent = self.another_ticket('Urgent after profile change', priority='urgent')
        profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, profile['id'], profile['version'],
                                        instructions='Different instructions after the question')
        self.answer()
        final = self.run_queue(human_owner_id='owner')
        self.assertEqual(final['status'], 'needs_review')
        self.assertEqual(final['operation_id'], first['operation_id'])
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertIsNone(self.board.get_ticket(self.human, urgent['id'])['ticket']['claimant'])

    def test_new_session_cannot_bypass_unfinished_local_selection_or_lock(self):
        self.lose_selection_and_reconciliation()
        first = self.run_queue()
        operation = self.queue_journal.get(first['operation_id'])
        replacement_scope = {**self.queue_scope, 'board_session_id': 'replacement-session'}
        with QueueJournal(self.root / 'queue') as replacement:
            with self.queue_journal.lock(self.queue_scope):
                with self.assertRaises(QueueJournalError) as caught:
                    with replacement.lock(replacement_scope):
                        self.fail('A replacement session acquired the same identity lock')
                self.assertEqual(caught.exception.code, 'scope_busy')
            with replacement.lock(replacement_scope):
                with self.assertRaises(QueueJournalError) as caught:
                    replacement.begin(replacement_scope, 'new-request', operation['configuration'])
                self.assertEqual(caught.exception.code, 'scope_has_unfinished_operation')
        self.assertEqual(self.queue_journal.get(first['operation_id']), operation)
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertEqual(self.inferences, [])

    def test_prior_expired_claim_blocks_another_session_of_same_profile(self):
        self.client.register()
        self.board.claim_ticket(self.client.actor, self.ticket['id'], self.ticket['version'])
        with self.board._tx() as db:
            db.execute('UPDATE tickets SET lease_expires_at=1 WHERE id=?', (self.ticket['id'],))
        second = self.another_ticket('Available during old expired work', priority='urgent')
        self.client = AutonomousFixtureClient(self.board, self.raw, session='replacement-session')
        self.worker = self.make_worker(client=self.client)
        self.queue = QueueWorker(self.queue_journal, self.worker, self.client)
        self.queue_scope['board_session_id'] = 'replacement-session'
        result = self.run_queue()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(self.inferences, [])
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertIsNone(self.board.get_ticket(self.human, second['id'])['ticket']['claimant'])
        self.assertEqual(self.read()['claimant']['session_id'], 'worker-session')
        self.assertTrue(self.read()['claim_expired'])

    def test_released_human_wait_preserves_question_while_other_device_handles_unrelated_ticket(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'})]
        waiting = self.run_worker()
        self.assertEqual(waiting['stage'], 'waiting_human')
        self.outputs.append('Unrelated ticket response.')
        other = self.another_ticket('Ready while another device waits', priority='urgent')
        device = self.board.create_device(self.human, 'Replacement local device', [])
        profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.bind_agent_profile(self.human, profile['id'], profile['version'],
                                      device['id'], 'worker', 'codex')
        self.client = AutonomousFixtureClient(self.board, self.board.authenticate(device['token']),
                                              session='another-device-session')
        self.worker = self.make_worker(client=self.client)
        self.queue = QueueWorker(self.queue_journal, self.worker, self.client)
        self.queue_scope['board_session_id'] = 'another-device-session'
        result = self.run_queue()
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['ticket_id'], other['id'])
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(len(self.selection_calls()), 1)
        original = self.read()
        self.assertTrue(original['waiting_on_human'])
        self.assertEqual(original['status'], 'blocked')
        self.assertIsNone(original['claimant'])
        unrelated = self.board.get_ticket(self.human, other['id'])['ticket']
        self.assertEqual(unrelated['status'], 'review')
        self.assertIsNone(unrelated['claimant'])
        self.assertEqual(self.run_queue()['operation_id'], result['operation_id'])
        self.assertEqual(len(self.inferences), 2)
        self.assertEqual(len(self.selection_calls()), 1)

    def test_cancellation_after_selection_keeps_claim_and_blocks_another_request(self):
        original, cancelled = self.client.call, []

        def cancel_after_claim(name, arguments):
            value = original(name, arguments)
            if name == 'claim_next_ticket':
                cancelled.append(True)
            return value

        self.client.call = cancel_after_claim
        result = self.run_queue(should_cancel=lambda: bool(cancelled))
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(result['reason'], 'cancel_requested')
        other = self.another_ticket('Ready after cancellation', priority='urgent')
        self.assertEqual(self.run_queue()['operation_id'], result['operation_id'])
        self.assertEqual(self.run_queue(request_id='different-request')['status'], 'needs_review')
        self.assertEqual(self.inferences, [])
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertIsNotNone(self.read()['claimant'])
        self.assertIsNone(self.board.get_ticket(self.human, other['id'])['ticket']['claimant'])

    def test_quota_block_does_not_select_next_ticket_even_after_claim_release(self):
        original = self.worker.runner_factory

        def blocked_transport(settings, prompt, workspace, **callbacks):
            self.inferences.append({'prompt': prompt, 'resume': callbacks['resume_session_id']})
            return {'status': 'blocked', 'reason': 'quota_exhausted', 'text': '',
                    'verification': {'cleanup_confirmed': True}}

        self.worker.runner_factory = lambda store: fixtures.PersonalRunner(store, blocked_transport)
        result = self.run_queue()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(self.read()['status'], 'blocked')
        self.assertIsNone(self.read()['claimant'])
        other = self.another_ticket('Ready after quota refusal', priority='urgent')
        self.worker.runner_factory = original
        self.assertEqual(self.run_queue()['operation_id'], result['operation_id'])
        self.assertEqual(self.run_queue(request_id='new-request-after-quota')['status'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertIsNone(self.board.get_ticket(self.human, other['id'])['ticket']['claimant'])

    def test_worker_publication_before_queue_save_failure_reconciles_without_another_ticket(self):
        original, fail = self.queue_journal.finish_worker, [True]

        def interrupted(*args, **kwargs):
            if fail:
                fail.pop()
                raise OSError('Fixture queue receipt write unavailable')
            return original(*args, **kwargs)

        self.queue_journal.finish_worker = interrupted
        first = self.run_queue()
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(self.read()['status'], 'review')
        other = self.another_ticket('Ready after publication', priority='urgent')
        final = self.run_queue()
        self.assertEqual(final['status'], 'complete')
        self.assertEqual(final['operation_id'], first['operation_id'])
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.selection_calls()), 1)
        self.assertEqual(len([name for name, _ in self.client.calls if name == 'update_ticket']), 1)
        self.assertIsNone(self.board.get_ticket(self.human, other['id'])['ticket']['claimant'])

    def test_wrong_child_scope_request_or_model_cannot_reconcile_queue(self):
        complete = self.run_queue()
        operation = self.queue_journal.get(complete['operation_id'])
        proof = self.journal.get(complete['worker_operation_id'])
        changes = (lambda item: item['scope'].update(ticket_id='other-ticket'),
                   lambda item: item.update(request_id='manual-work'),
                   lambda item: item['configuration'].update(model='other-model'),
                   lambda item: item['configuration'].update(instruction='Unrelated instruction'))
        for change in changes:
            wrong = deepcopy(proof)
            change(wrong)
            with self.queue_journal.lock(self.queue_scope):
                with self.assertRaises(QueueJournalError):
                    self.queue_journal.resolve(operation['id'], wrong)
            self.assertEqual(self.queue_journal.get(operation['id']), operation)
        self.assertEqual(len(self.inferences), 1)

    def test_unconfirmed_delayed_selection_cannot_be_abandoned_to_release_scope(self):
        original, delayed = self.client.call, []

        def delayed_claim(name, arguments):
            if name == 'claim_next_ticket':
                delayed.append(deepcopy(arguments))
                raise TimeoutError('Fixture request still in flight')
            return original(name, arguments)

        self.client.call = delayed_claim
        first = self.run_queue()
        self.assertEqual((first['status'], first['reason']), ('needs_review', 'selection_unconfirmed'))
        self.assertEqual(len(delayed), 1)
        operation = self.queue_journal.get(first['operation_id'])
        self.assertIs(operation['payload'].get('selection_attempted'), True)
        with self.queue_journal.lock(self.queue_scope):
            with self.assertRaises(QueueJournalError) as caught:
                self.queue_journal.abandon(operation['id'], operation['revision'],
                                           'Human saw no current claim in fixture')
            self.assertEqual(caught.exception.code, 'selection_unconfirmed')
            with self.assertRaises(QueueJournalError):
                self.queue_journal.update(operation['id'], operation['revision'], stage='needs_review',
                                          payload={**operation['payload'], 'selection_attempted': False})
        self.assertEqual(self.queue_journal.get(operation['id']), operation)
        self.client.call = original
        absent = self.queue.reconcile_selection(operation['id'])
        self.assertEqual((absent['status'], absent['reason']), ('needs_review', 'selection_unconfirmed'))
        self.assertIsNot(self.queue_journal.get(operation['id'])['payload'].get('selection_confirmed'), True)
        self.assertEqual(self.run_queue(request_id='next-request')['status'], 'needs_review')
        self.assertEqual(self.inferences, [])
        self.assertIsNone(self.read()['claimant'])
        # The delayed original call can still commit later. It keeps its own
        # stable key; no subsequent request was allowed to select around it.
        self.client.register_for_selection()
        late = self.board.claim_next_ticket(self.client.actor, **delayed[0])
        self.assertTrue(late['claimed'])
        self.assertEqual(late['ticket']['id'], self.ticket['id'])
        self.assertEqual(self.inferences, [])
        before_calls = len(self.client.calls)
        confirmed = self.queue.reconcile_selection(operation['id'])
        self.assertEqual((confirmed['status'], confirmed['reason']), ('needs_review', 'selection_reconciled'))
        self.assertEqual([name for name, _ in self.client.calls[before_calls:]], ['lookup_ticket_selection'])
        self.assertEqual(self.inferences, [])
        self.change(release_claim=True)
        operation = self.queue_journal.get(operation['id'])
        with self.queue_journal.lock(self.queue_scope):
            abandoned = self.queue_journal.abandon(operation['id'], operation['revision'],
                                                   'Human released the confirmed fixture claim')
        self.assertEqual(abandoned['stage'], 'abandoned')
        self.assertEqual(self.run_queue()['status'], 'abandoned')
        self.assertEqual(self.inferences, [])

    def test_child_result_lost_before_queue_receipt_still_requires_terminal_child_proof(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'})]

        def fail_receipt(*args, **kwargs):
            raise OSError('Fixture child receipt was not saved')

        self.queue_journal.finish_worker = fail_receipt
        first = self.run_queue(human_owner_id='owner')
        self.assertEqual(first['status'], 'pending')
        selected = self.queue_journal.get(first['operation_id'])
        self.assertIsNotNone(selected['payload'].get('worker_operation_id'))
        child = self.journal.get(selected['payload']['worker_operation_id'])
        self.assertEqual(child['stage'], 'waiting_human')
        cancelled = self.run_queue(human_owner_id='owner', should_cancel=lambda: True)
        self.assertEqual(cancelled['status'], 'needs_review')
        operation = self.queue_journal.get(first['operation_id'])
        with self.queue_journal.lock(self.queue_scope):
            for proof in (None, child):
                with self.assertRaises(QueueJournalError):
                    self.queue_journal.abandon(operation['id'], operation['revision'],
                                               'Human checked fixture state', proof=proof)
        self.assertEqual(self.queue_journal.get(operation['id']), operation)
        self.answer()
        self.assertEqual(self.run_queue(request_id='new-request')['status'], 'needs_review')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.selection_calls()), 1)

    def test_child_preallocation_write_failure_has_a_local_recovery_path(self):
        original, fail = self.queue_journal.update, [True]

        def lose_child_id(*args, **kwargs):
            if kwargs.get('stage') == 'dispatched' and fail:
                fail.pop()
                raise OSError('Fixture queue child ID write interrupted')
            return original(*args, **kwargs)

        self.queue_journal.update = lose_child_id
        first = self.run_queue()
        self.assertEqual(first['status'], 'pending')
        selected = self.queue_journal.get(first['operation_id'])
        self.assertIs(selected['payload'].get('worker_preparing'), True)
        self.assertNotIn('worker_operation_id', selected['payload'])
        self.assertEqual(self.run_queue(should_cancel=lambda: True)['status'], 'needs_review')
        operation = self.queue_journal.get(first['operation_id'])
        with self.queue_journal.lock(self.queue_scope):
            with self.journal.lock(self.scope):
                child = self.journal.find(self.scope, operation['payload']['worker_request_id'])
                self.assertEqual(child['stage'], 'new')
                with self.assertRaises(QueueJournalError):
                    self.queue_journal.abandon(operation['id'], operation['revision'],
                                               'Human inspected fixture', proof=child)
        # Existing worker recovery must support this known preallocated state,
        # without reconnecting or executing a turn merely to mark it stopped.
        before_calls, before_registration = len(self.client.calls), self.client.register_count
        self.change(release_claim=True)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(['worker', '--journal', str(self.root / 'journal'), '--state', str(self.root / 'runs'),
                         'abandon', '--operation', child['id'], '--revision', str(child['revision']),
                         '--reason', 'Human released fixture claim', '--acknowledge-board-reconciled'])
        self.assertEqual(code, 0, stdout.getvalue())
        abandoned_child = self.journal.get(child['id'])
        self.assertEqual(abandoned_child['stage'], 'abandoned')
        self.assertEqual(len(self.client.calls), before_calls)
        self.assertEqual(self.client.register_count, before_registration)
        with self.queue_journal.lock(self.queue_scope):
            final = self.queue_journal.abandon(operation['id'], operation['revision'],
                                              'Human released fixture claim', proof=abandoned_child)
        self.assertEqual(final['stage'], 'abandoned')
        self.assertEqual(self.inferences, [])

    def test_failed_cli_abandon_does_not_poison_later_selection_reconciliation(self):
        original, delayed = self.client.call, []

        def delayed_claim(name, arguments):
            if name == 'claim_next_ticket':
                delayed.append(deepcopy(arguments))
                raise TimeoutError('Fixture original selection still in flight')
            return original(name, arguments)

        self.client.call = delayed_claim
        first = self.run_queue()
        self.assertEqual(first['reason'], 'selection_unconfirmed')
        operation = self.queue_journal.get(first['operation_id'])

        def abandon_from_cli(revision):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(['queue', '--journal', str(self.root / 'queue'), 'abandon',
                             '--operation', operation['id'], '--revision', str(revision),
                             '--reason', 'Human inspected and reconciled the fixture',
                             '--acknowledge-board-reconciled'])
            return code, json.loads(output.getvalue())

        code, rejected = abandon_from_cli(operation['revision'])
        self.assertEqual((code, rejected['reason']), (2, 'selection_unconfirmed'))
        self.assertEqual(self.queue_journal.get(operation['id']), operation)
        self.client.call = original
        self.client.register_for_selection()
        self.board.claim_next_ticket(self.client.actor, **delayed[0])
        reconciled = self.queue.reconcile_selection(operation['id'])
        self.assertEqual(reconciled['reason'], 'selection_reconciled')
        self.change(release_claim=True)
        operation = self.queue_journal.get(operation['id'])
        code, result = abandon_from_cli(operation['revision'])
        self.assertEqual(code, 0, result)
        self.assertEqual(self.queue_journal.get(operation['id'])['stage'], 'abandoned')
        self.assertEqual(self.inferences, [])

    def test_cli_cannot_treat_another_empty_child_journal_as_absence_after_dispatch(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Private environment question?'})]
        first = self.run_queue(human_owner_id='owner')
        self.assertEqual(first['status'], 'pending')
        self.run_queue(human_owner_id='owner', should_cancel=lambda: True)
        operation = self.queue_journal.get(first['operation_id'])
        before_calls, before_registrations = len(self.client.calls), self.client.register_count
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['queue', '--journal', str(self.root / 'queue'),
                         '--work-journal', str(self.root / 'wrong-empty-child-journal'),
                         'abandon', '--operation', operation['id'], '--revision', str(operation['revision']),
                         '--reason', 'Private reason only in journal', '--acknowledge-board-reconciled'])
        self.assertEqual(code, 2, output.getvalue())
        self.assertNotIn('Private', output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())
        self.assertEqual(self.queue_journal.get(operation['id']), operation)
        self.assertEqual(self.journal.get(operation['payload']['worker_operation_id'])['stage'], 'waiting_human')
        self.assertEqual(len(self.client.calls), before_calls)
        self.assertEqual(self.client.register_count, before_registrations)
        self.assertEqual(len(self.inferences), 1)

    def test_cli_child_proof_is_read_under_child_lock_before_queue_release(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'})]
        first = self.run_queue(human_owner_id='owner')
        self.run_queue(human_owner_id='owner', should_cancel=lambda: True)
        operation = self.queue_journal.get(first['operation_id'])
        output = io.StringIO()
        with self.journal.lock(self.scope):
            with redirect_stdout(output):
                code = main(['queue', '--journal', str(self.root / 'queue'),
                             '--work-journal', str(self.root / 'journal'), 'abandon',
                             '--operation', operation['id'], '--revision', str(operation['revision']),
                             '--reason', 'Human reviewed fixture', '--acknowledge-board-reconciled'])
        self.assertEqual((code, json.loads(output.getvalue())['reason']), (2, 'scope_busy'))
        self.assertEqual(self.queue_journal.get(operation['id']), operation)
        self.assertEqual(self.journal.get(operation['payload']['worker_operation_id'])['stage'], 'waiting_human')
        self.assertEqual(len(self.inferences), 1)
        self.assertEqual(len(self.selection_calls()), 1)

    def exercise_interrupted_cli_abandon(self, *, after_commit):
        original_call, selected = self.client.call, []

        def cancel_after_selection(name, arguments):
            value = original_call(name, arguments)
            if name == 'claim_next_ticket':
                selected.append(True)
            return value

        self.client.call = cancel_after_selection
        first = self.run_queue(should_cancel=lambda: bool(selected))
        self.assertEqual(first['reason'], 'cancel_requested')
        operation = self.queue_journal.get(first['operation_id'])
        self.change(release_claim=True)
        args = ['queue', '--journal', str(self.root / 'queue'), 'abandon',
                '--operation', operation['id'], '--revision', str(operation['revision']),
                '--reason', 'Human reconciled the private fixture', '--acknowledge-board-reconciled']
        original_abandon, interrupted = QueueJournal.abandon, []

        def lose_result(journal, *arguments, **kwargs):
            if interrupted:
                return original_abandon(journal, *arguments, **kwargs)
            interrupted.append(True)
            if after_commit:
                original_abandon(journal, *arguments, **kwargs)
            raise OSError('Fixture interruption after durable abandonment intent')

        before_calls, before_registrations = len(self.client.calls), self.client.register_count
        output = io.StringIO()
        with patch.object(QueueJournal, 'abandon', lose_result):
            with redirect_stdout(output):
                code = main(args)
        self.assertEqual(code, 2, output.getvalue())
        interrupted_operation = self.queue_journal.get(operation['id'])
        self.assertEqual(interrupted_operation['stage'], 'abandoned' if after_commit else 'needs_review')
        self.assertEqual(interrupted_operation['revision'], operation['revision'] + (2 if after_commit else 1))
        self.assertNotIn('private fixture', output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            resumed = main(args)
        self.assertEqual(resumed, 0, output.getvalue())
        final = self.queue_journal.get(operation['id'])
        self.assertEqual(final['stage'], 'abandoned')
        self.assertEqual(final['revision'], operation['revision'] + 2)
        output = io.StringIO()
        with redirect_stdout(output):
            repeated = main(args)
        self.assertEqual(repeated, 0, output.getvalue())
        self.assertEqual(self.queue_journal.get(operation['id']), final)
        self.assertEqual(len(self.client.calls), before_calls)
        self.assertEqual(self.client.register_count, before_registrations)
        self.assertEqual(self.inferences, [])

    def test_cli_abandon_recovers_after_intent_before_terminal_commit(self):
        self.exercise_interrupted_cli_abandon(after_commit=False)

    def test_cli_abandon_repeats_after_terminal_commit_response_was_lost(self):
        self.exercise_interrupted_cli_abandon(after_commit=True)


if __name__ == '__main__':
    unittest.main()
