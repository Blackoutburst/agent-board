"""Routing flags stay explicit and private across worker, events and queue."""
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

import test_worker_cli as workers
import test_event_cli as events
import test_queue_cli as queues
from test_model_routing_worker import policy_for


def install_policy(test):
    scope = {'owner_id': 'owner', 'agent_id': 'agent', 'board_endpoint': 'https://board.test/mcp'}
    registration = {'identity': {'owner_id': 'owner', 'id': 'agent', 'device_id': 'device'}}
    test.policy = policy_for(scope, registration)
    test.policy_path = test.root / 'model-policy.json'
    test.policy_path.write_text(json.dumps(test.policy))
    test.policy_path.chmod(0o600)
    return ['--model-policy', str(test.policy_path), '--task-class', 'simple']


class RoutingWorkerCliTests(unittest.TestCase):
    args = workers.WorkerCliTests.args
    invoke = workers.WorkerCliTests.invoke

    def setUp(self):
        workers.WorkerCliTests.setUp(self)
        self.routing_args = install_policy(self)

    def test_policy_snapshot_and_manual_override_are_passed_without_printing_policy(self):
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            worker.return_value.run.return_value = self.result
            code, output, raw = self.invoke(self.args(*self.routing_args, '--model', 'critic'))
            self.assertEqual(0, code)
            self.assertEqual(self.result, output)
            kwargs = worker.return_value.run.call_args.kwargs
            self.assertEqual(self.policy, kwargs['model_policy'])
            self.assertEqual('simple', kwargs['task_class'])
            self.assertEqual('critic', kwargs['model'])
            self.assertNotIn('routing-fixture', raw)
            self.assertNotIn('critic', raw)

    def test_partial_options_bad_owner_or_public_file_fail_before_connection(self):
        options = [self.routing_args[:-2], self.routing_args[-2:]]
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file') as client:
            for args in options:
                self.assertEqual(2, self.invoke(self.args(*args))[0])
            self.policy['owner_id'] = 'foreign'
            self.policy_path.write_text(json.dumps(self.policy))
            self.assertEqual(2, self.invoke(self.args(*self.routing_args))[0])
            self.policy_path.chmod(0o644)
            self.assertEqual(2, self.invoke(self.args(*self.routing_args))[0])
            client.assert_not_called()

    def test_runtime_flag_installs_factory_and_pins_opt_in(self):
        with patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.worker_cli.RuntimeBoardClient.from_worker') as runtime_client, \
                patch('blackoutburst_agents.worker_cli.RuntimeGuard') as guard, \
                patch('blackoutburst_agents.worker_cli.BoardWorker') as worker:
            worker.return_value.run.return_value = self.result
            code, _, _ = self.invoke(self.args(*self.routing_args, '--runtime-journal', str(self.root / 'runtime')))
            self.assertEqual(0, code)
            factory = worker.call_args.kwargs['runtime_guard_factory']
            scope = worker.return_value.run.call_args.kwargs['scope']
            factory(scope)
            self.assertEqual(scope, guard.call_args.args[2])
            runtime_client.assert_called_once_with(self.client)
            self.assertTrue(worker.return_value.run.call_args.kwargs['runtime_managed'])
            self.assertIsNone(guard.call_args.args[0].db)


class RoutingEventsCliTests(unittest.TestCase):
    invoke = events.EventCliTests.invoke

    def setUp(self):
        events.EventCliTests.setUp(self)
        self.routing_args = install_policy(self)

    def test_preview_pins_policy_without_starting_model(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.event_cli.EventJournal'), \
                patch('blackoutburst_agents.event_cli.EventDrainer') as drain, \
                patch('blackoutburst_agents.event_cli.BoardWorker') as worker:
            drain.return_value.drain.return_value = self.receipt
            self.assertEqual(0, self.invoke(self.args + self.routing_args)[0])
            worker.assert_not_called()
            self.assertEqual(self.policy, drain.return_value.drain.call_args.kwargs['model_policy'])

    def test_runtime_preview_records_intent_without_opening_runtime_journal(self):
        with patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file', return_value=self.client), \
                patch('blackoutburst_agents.event_cli.EventJournal'), \
                patch('blackoutburst_agents.event_cli.EventDrainer') as drain, \
                patch('blackoutburst_agents.worker_cli.RuntimeJournal') as runtime:
            drain.return_value.drain.return_value = self.receipt
            args = self.args + self.routing_args + ['--runtime-journal', str(self.root / 'runtime')]
            self.assertEqual(0, self.invoke(args)[0])
            self.assertTrue(drain.return_value.drain.call_args.kwargs['runtime_managed'])
            runtime.assert_not_called()


class RoutingQueueCliTests(unittest.TestCase):
    invoke = queues.QueueCliTests.invoke
    execution_doubles = queues.QueueCliTests.execution_doubles
    executing = queues.QueueCliTests.executing

    def setUp(self):
        queues.QueueCliTests.setUp(self)
        self.routing_args = install_policy(self)
        self.args = self.args[:-2]

    def test_policy_replaces_required_manual_model_for_explicit_execution(self):
        with ExitStack() as stack:
            stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file', return_value=self.client))
            doubles = self.execution_doubles(stack)
            self.assertEqual(0, self.invoke(self.executing(*self.routing_args))[0])
            kwargs = doubles['QueueWorker'].return_value.run.call_args.kwargs
            self.assertIsNone(kwargs['model'])
            self.assertEqual(self.policy, kwargs['model_policy'])
            self.assertEqual('simple', kwargs['task_class'])

    def test_policy_preview_checks_binding_without_selection(self):
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file', return_value=self.client):
            result = self.invoke(self.args + self.routing_args)
            self.assertEqual((0, 'preview'), (result[0], result[1]['status']))
            self.assertFalse((self.root / 'queue').exists())
            self.assertEqual(['get_project', 'get_work'], [call.args[0] for call in self.client.call.call_args_list])

    def test_runtime_queue_factory_defers_ticket_scope_until_selection(self):
        with ExitStack() as stack:
            stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file', return_value=self.client))
            stack.enter_context(patch('blackoutburst_agents.worker_cli.RuntimeBoardClient.from_worker'))
            guard = stack.enter_context(patch('blackoutburst_agents.worker_cli.RuntimeGuard'))
            doubles = self.execution_doubles(stack)
            args = self.executing(*self.routing_args, '--runtime-journal', str(self.root / 'runtime'))
            self.assertEqual(0, self.invoke(args)[0])
            self.assertTrue(doubles['QueueWorker'].return_value.run.call_args.kwargs['runtime_managed'])
            guard.assert_not_called()
            factory = doubles['BoardWorker'].call_args.kwargs['runtime_guard_factory']
            final_scope = {'ticket_id': 'selected-later'}
            factory(final_scope)
            self.assertEqual(final_scope, guard.call_args.args[2])


if __name__ == '__main__':
    unittest.main()
