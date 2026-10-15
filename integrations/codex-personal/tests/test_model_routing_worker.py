"""Pinned routing with real temporary Board/journals; no real inference."""
from copy import deepcopy
import json
import sys
import time
import unittest
from unittest.mock import Mock, patch

import test_board_worker as fixtures
import test_human_worker as human
import test_event_human_drain as events
import test_queue_worker as queues
from blackoutburst_agents.model_policy import connection_identifier, TASK_CLASSES, PROVIDER
from blackoutburst_agents.worker_cli import _close_work
from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.runner import PersonalRunner


def policy_for(scope, registration):
    connection = connection_identifier(scope, registration)
    names = ('focused', 'workhorse', 'deep', 'critic')
    return {'schema_version': 1, 'policy_id': 'routing-fixture', 'version': 1,
        'owner_id': scope['owner_id'], 'connection_id': connection, 'provider': PROVIDER,
        'proof_max_age_seconds': 3600, 'models': {name: {'reasoning_efforts': [],
            'availability': {'status': 'verified', 'observed_at': time.time() - 5,
                'reference': 'fixture-' + name, 'owner_id': scope['owner_id'],
                'connection_id': connection, 'model': name, 'provider': PROVIDER}} for name in names},
        'routes': {task: {'model': name, 'reasoning_effort': None} for task, name in zip(TASK_CLASSES, names)}}


@unittest.skipIf(fixtures.Board is None, 'Repository Board not bundled')
class RoutingWorkerTests(unittest.TestCase):
    make_worker = fixtures.BoardWorkerTests.make_worker
    read = fixtures.BoardWorkerTests.read
    change = fixtures.BoardWorkerTests.change

    def setUp(self):
        fixtures.BoardWorkerTests.setUp(self)
        self.policy = policy_for(self.scope, self.client.registration)
        self.models, self.outputs = [], None

    def transport(self, settings, *args, **kwargs):
        self.models.append(settings.model)
        result = fixtures.BoardWorkerTests.transport(self, settings, *args, **kwargs)
        if self.outputs:
            result['text'] = self.outputs[len(self.inferences) - 1]
        result['verification'] = {'cleanup_confirmed': True}
        return result

    def run_worker(self, **overrides):
        return fixtures.BoardWorkerTests.run_worker(self, **{
            'model_policy': self.policy, 'task_class': 'simple', **overrides})

    def test_four_classes_use_chosen_model_with_pinned_context(self):
        for index, (task, model) in enumerate(zip(TASK_CLASSES, ('focused', 'workhorse', 'deep', 'critic'))):
            with self.subTest(task=task):
                if index:
                    self.doCleanups()
                    self.setUp()
                result = self.run_worker(task_class=task)
                self.assertEqual('complete', result['status'], result)
                op = self.journal.get(result['operation_id'])
                self.assertEqual([model], self.models)
                self.assertEqual(model, self.store.get_work(op['payload']['work_id'])['model'])
                self.assertEqual(op['payload']['context']['fingerprint'],
                                 op['payload']['route_decision']['context_hash'])
                self.assertIsNone(op['configuration']['model'])
                self.assertEqual(self.policy, op['configuration']['routing']['policy'])
                self.assertNotIn(model, json.dumps(result))

    def test_manual_override_and_profile_preference(self):
        configured = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, configured['id'], configured['version'],
                                        preferred_model='focused', preferred_provider=PROVIDER)
        result = self.run_worker(model='critic')
        op = self.journal.get(result['operation_id'])
        self.assertEqual(['critic'], self.models)
        self.assertEqual('manual_override', op['payload']['route_decision']['reason'])

    def test_profile_preference_routes_only_when_allowlisted(self):
        configured = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, configured['id'], configured['version'],
                                        preferred_model='focused', preferred_provider=PROVIDER)
        result = self.run_worker()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['focused'], self.models)
        self.assertEqual('profile_preference', self.journal.get(result['operation_id'])['payload']['route_decision']['reason'])

    def test_unknown_profile_preference_stops_without_inference(self):
        configured = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, configured['id'], configured['version'], preferred_model='unknown')
        result = self.run_worker()
        self.assertEqual(('needs_review', 'model_not_allowed'), (result['status'], result['reason']))
        self.assertEqual([], self.models)
        self.assertEqual(0, self.store._db.execute('SELECT count(*) FROM works').fetchone()[0])

    def test_unavailable_candidate_does_not_choose_another(self):
        self.policy['models']['workhorse']['availability']['status'] = 'unavailable'
        result = self.run_worker()
        self.assertEqual('model_unavailable', result['reason'])
        self.assertEqual([], self.models)
        self.assertEqual(0, self.store._db.execute('SELECT count(*) FROM works').fetchone()[0])

    def test_connection_mismatch_precedes_claim(self):
        self.policy['connection_id'] = 'foreign'
        for candidate in self.policy['models'].values():
            candidate['availability']['connection_id'] = 'foreign'
        result = self.run_worker()
        self.assertEqual('policy_scope_mismatch', result['reason'])
        self.assertFalse(any(name == 'claim_ticket' for name, _ in self.client.calls))
        self.assertEqual([], self.models)

    def test_decision_commits_before_work_creation_and_survives_crash(self):
        original = self.store.create_work
        def interrupted(**kwargs):
            row = self.journal._db.execute('SELECT stage,payload_json FROM operations').fetchone()
            self.assertEqual('claimed', row['stage'])
            payload = json.loads(row['payload_json'])
            self.assertEqual(kwargs['model'], payload['route_decision']['model'])
            self.assertEqual(kwargs['model'], payload['selected_model'])
            raise SystemExit('simulated process stop')
        self.store.create_work = interrupted
        with self.assertRaises(SystemExit):
            self.run_worker()
        self.assertEqual([], self.models)
        self.store.create_work = original
        with patch('blackoutburst_agents.board_worker.select_model', side_effect=AssertionError('must not reroute')):
            result = self.run_worker()
            self.assertEqual('complete', result['status'], result)
            self.assertTrue(self.run_worker()['cached'])
        self.assertEqual(['workhorse'], self.models)

    def test_lost_publication_reconciles_without_routing_again(self):
        self.client.lost.add('update_ticket')
        first = self.run_worker()
        self.assertEqual('publishing', first['stage'])
        with patch('blackoutburst_agents.board_worker.select_model', side_effect=AssertionError('must not reroute')), \
                patch('blackoutburst_agents.board_worker.validate_pinned', side_effect=AssertionError('already published')):
            final = self.run_worker()
        self.assertEqual('complete', final['status'], final)
        self.assertEqual(['workhorse'], self.models)

    def test_configuration_change_cannot_replace_route(self):
        first = self.run_worker()
        self.assertEqual('complete', first['status'])
        self.assertEqual('local_operation_conflict', self.run_worker(task_class='review')['reason'])
        self.assertEqual('local_operation_conflict', self.run_worker(model='critic')['reason'])
        changed = deepcopy(self.policy)
        changed['version'] = 2
        self.assertEqual('local_operation_conflict', self.run_worker(model_policy=changed)['reason'])
        self.assertEqual('local_operation_conflict', self.run_worker(model_policy=None, task_class=None)['reason'])
        self.assertEqual(['workhorse'], self.models)

    def test_human_resume_keeps_original_choice_context_and_model(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'}),
                        json.dumps({'kind': 'result', 'text': 'Use the local environment.'})]
        first = self.run_worker(human_owner_id='owner')
        self.assertEqual('waiting_human', first['stage'], first)
        initial = self.journal.get(first['operation_id'])
        request = self.board.list_human_requests(self.human, self.project['id'], True)[0]
        self.board.resolve_human_request(self.human, request['id'], 'Local.')
        with patch('blackoutburst_agents.board_worker.select_model', side_effect=AssertionError('must not reroute')):
            final = self.run_worker(human_owner_id='owner')
        self.assertEqual('complete', final['status'], final)
        updated = self.journal.get(final['operation_id'])
        self.assertEqual(initial['payload']['route_decision'], updated['payload']['route_decision'])
        self.assertNotEqual(updated['payload']['context']['fingerprint'], updated['payload']['route_decision']['context_hash'])
        self.assertEqual(['workhorse', 'workhorse'], self.models)
        self.assertIsNotNone(self.inferences[1]['resume'])

    def test_profile_changed_during_question_blocks_second_model(self):
        self.outputs = [json.dumps({'kind': 'question', 'text': 'Which environment?'})]
        first = self.run_worker(human_owner_id='owner')
        request = self.board.list_human_requests(self.human, self.project['id'], True)[0]
        self.board.resolve_human_request(self.human, request['id'], 'Local.')
        profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        self.board.update_agent_profile(self.human, profile['id'], profile['version'], preferred_model='critic')
        result = self.run_worker(human_owner_id='owner')
        self.assertEqual('human_resume_profile_changed', result['reason'])
        self.assertEqual(['workhorse'], self.models)
        self.assertEqual(first['operation_id'], result['operation_id'])

    def test_abandon_verifies_effective_model_not_nullable_override(self):
        self.after_model = lambda: self.change(description='Changed during model')
        result = self.run_worker()
        operation = self.journal.get(result['operation_id'])
        self.assertEqual('needs_review', operation['stage'])
        operation['payload']['abandonment_intent'] = {'reason': 'Inspected and reconciled.'}
        _close_work(self.store, operation)
        self.assertEqual('closed', self.store.get_work(operation['payload']['work_id'])['status'])

    def test_runtime_opt_in_is_pinned_and_cannot_be_removed(self):
        guard = Mock()
        guard.should_cancel.return_value = False
        self.worker = BoardWorker(self.journal, self.store, self.client,
            runner_factory=lambda state, **options: PersonalRunner(state, self.transport, **options),
            runtime_guard_factory=lambda scope: guard)
        result = self.run_worker(runtime_managed=True)
        self.assertEqual('complete', result['status'], result)
        op = self.journal.get(result['operation_id'])
        self.assertIs(op['configuration']['runtime_managed'], True)
        guard.before.assert_called_once()
        guard.finish.assert_called_once()
        self.worker = self.make_worker()
        self.assertEqual('local_operation_conflict', self.run_worker()['reason'])
        self.assertEqual(['workhorse'], self.models)

    def test_runtime_factory_and_model_required_before_claim(self):
        result = self.run_worker(runtime_managed=True)
        self.assertEqual('runtime_configuration_mismatch', result['reason'])
        self.worker = BoardWorker(self.journal, self.store, self.client, runtime_guard_factory=lambda scope: Mock())
        result = self.run_worker(runtime_managed=True, model_policy=None, task_class=None)
        self.assertEqual('runtime_model_required', result['reason'])
        result = self.run_worker()
        self.assertEqual('runtime_configuration_mismatch', result['reason'])
        self.assertFalse(any(name == 'claim_ticket' for name, _ in self.client.calls))


@unittest.skipIf(fixtures.Board is None, 'Repository Board not bundled')
class RoutingQueueTests(unittest.TestCase):
    make_worker = fixtures.BoardWorkerTests.make_worker
    transport = fixtures.BoardWorkerTests.transport
    read = fixtures.BoardWorkerTests.read
    change = fixtures.BoardWorkerTests.change
    run_queue = queues.QueueWorkerTests.run_queue

    def setUp(self):
        queues.QueueWorkerTests.setUp(self)
        self.policy = policy_for(self.scope, self.client.registration)

    def test_queue_without_model_keeps_exact_child_configuration(self):
        result = self.run_queue(model=None, model_policy=self.policy, task_class='difficult')
        self.assertEqual('complete', result['status'], result)
        op = self.queue_journal.get(result['operation_id'])
        child = self.journal.get(result['worker_operation_id'])
        self.assertEqual(child['configuration'], {key: value for key, value in op['configuration'].items() if key != 'project_id'})
        self.assertEqual('deep', self.store.get_work(child['payload']['work_id'])['model'])
        with patch('blackoutburst_agents.board_worker.select_model', side_effect=AssertionError('must not reroute')):
            self.assertTrue(self.run_queue(model=None, model_policy=self.policy, task_class='difficult')['cached'])
        self.assertEqual(1, len(self.inferences))

    def test_wrong_binding_never_selects_a_ticket(self):
        self.policy['connection_id'] = 'foreign'
        for item in self.policy['models'].values():
            item['availability']['connection_id'] = 'foreign'
        result = self.run_queue(model=None, model_policy=self.policy, task_class='simple')
        self.assertEqual('policy_scope_mismatch', result['reason'], result)
        self.assertFalse(any(name == 'claim_next_ticket' for name, _ in self.client.calls))


@unittest.skipIf(fixtures.Board is None, 'Repository Board not bundled')
class RoutingEventsTests(unittest.TestCase):
    make_worker = fixtures.BoardWorkerTests.make_worker
    read = fixtures.BoardWorkerTests.read
    change = fixtures.BoardWorkerTests.change
    transport = human.HumanWorkerTests.transport
    requests = human.HumanWorkerTests.requests
    answer = human.HumanWorkerTests.answer
    drain = events.EventHumanDrainTests.drain

    def setUp(self):
        events.EventHumanDrainTests.setUp(self)
        self.policy = policy_for(self.scope, self.client.registration)

    def test_events_human_answer_preserves_routing_configuration_and_deduplicates(self):
        args = {'model_policy': self.policy, 'task_class': 'review'}
        first = self.drain(**args)
        self.assertEqual('pending', first['status'], first)
        self.answer()
        with patch('blackoutburst_agents.board_worker.select_model', side_effect=AssertionError('must not reroute')):
            final = self.drain(**args)
        self.assertEqual('complete', final['status'], final)
        op = self.journal.get(final['worker_operation_id'])
        subscription = self.events.get_subscription(first['subscription_id'])
        self.assertEqual(op['configuration'], subscription['configuration'])
        self.assertEqual('critic', self.store.get_work(op['payload']['work_id'])['model'])
        self.assertEqual('idle', self.drain(**args)['status'])
        self.assertEqual(2, len(self.inferences))
        self.assertEqual('blocked', self.drain(model_policy=self.policy, task_class='simple')['status'])


if __name__ == '__main__':
    unittest.main()
