"""Owner lifetimes with local doubles only: no transport or model dispatch."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from blackoutburst_agents import context_cli, decision_cli, runtime_cli, usage_cli
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.context import ContextScope
from blackoutburst_agents.decision_memory import DecisionMemoryError
from blackoutburst_agents import message_runtime


ENDPOINT = 'https://board.test/mcp'


class CountingClient:
    def __init__(self, events, *, endpoint=ENDPOINT, registration_error=None, report_error=None):
        self.events = events
        self.endpoint = endpoint
        self.registration_error = registration_error
        self.report_error = report_error
        self.close_count = 0
        self.registration = {'identity': {'id': 'agent', 'owner_id': 'owner'}}
        self.report_arguments = None

    def register(self):
        assert self.close_count == 0
        self.events.append('register')
        if self.registration_error is not None:
            raise self.registration_error
        return self.registration

    def call(self, name, arguments):
        raise AssertionError('unexpected board call')

    def report(self, owner, agent, observation):
        assert self.close_count == 0
        self.events.append('report')
        self.report_arguments = (owner, agent, observation)
        if self.report_error is not None:
            raise self.report_error
        return {'status': 'reported'}

    def close(self):
        self.close_count += 1
        self.events.append('client_close')


def invoke(run, args):
    output = io.StringIO()
    with redirect_stdout(output):
        code = run(args)
    return code, json.loads(output.getvalue())


class ContextClientLifetimeTests(unittest.TestCase):
    scope = ContextScope('owner', 'agent', 'project', 'ticket')

    def test_fetch_success_closes_after_builder_and_preserves_explicit_memory(self):
        events = []
        client = CountingClient(events)
        memory, descriptor = object(), {'revision': 1}

        def build(call, **arguments):
            self.assertEqual(client.close_count, 0)
            events.append('build')
            self.assertEqual(call, client.call)
            self.assertEqual(arguments, {'scope': self.scope, 'registration': client.registration,
                'decision_memory': memory, 'decision_descriptor': descriptor, 'board_endpoint': ENDPOINT})
            return {'fingerprint': 'snapshot'}

        with patch.object(context_cli.BoardReader, 'from_connection_file', return_value=client) as connect, \
                patch.object(context_cli, 'build_context', side_effect=build):
            result = context_cli.fetch_context('connection', 'session', self.scope,
                decision_memory=memory, decision_descriptor=descriptor)
        self.assertEqual(result, {'fingerprint': 'snapshot'})
        connect.assert_called_once_with('connection', session_id='session')
        self.assertEqual((client.close_count, events), (1, ['register', 'build', 'client_close']))

    def test_registration_failure_closes_without_entering_builder(self):
        client = CountingClient([], registration_error=BoardReadError('access_denied'))
        with patch.object(context_cli.BoardReader, 'from_connection_file', return_value=client), \
                patch.object(context_cli, 'build_context') as build:
            with self.assertRaises(BoardReadError):
                context_cli.fetch_context('connection', 'session', self.scope)
        build.assert_not_called()
        self.assertEqual(client.close_count, 1)

    def test_builder_failure_closes(self):
        client = CountingClient([])
        with patch.object(context_cli.BoardReader, 'from_connection_file', return_value=client), \
                patch.object(context_cli, 'build_context', side_effect=ValueError('invalid snapshot')):
            with self.assertRaises(ValueError):
                context_cli.fetch_context('connection', 'session', self.scope)
        self.assertEqual(client.close_count, 1)

    def test_existing_fake_without_close_still_fetches(self):
        reader = SimpleNamespace(register=lambda: {}, call=lambda *_: {})
        with patch.object(context_cli.BoardReader, 'from_connection_file', return_value=reader), \
                patch.object(context_cli, 'build_context', return_value={'fingerprint': 'snapshot'}):
            self.assertEqual(context_cli.fetch_context('connection', 'session', self.scope),
                             {'fingerprint': 'snapshot'})


class UsageClientLifetimeTests(unittest.TestCase):
    def test_report_success_and_failure_close_exactly_once(self):
        observation = {'observed_at': 1, 'snapshot': {}}
        args = SimpleNamespace(usage_command='report', observation_file='observation',
            connection_file='connection', session='session', owner='owner', agent='agent')
        for error, expected in ((None, (0, {'status': 'reported'})),
                                (BoardReadError('access_denied'),
                                 (2, {'status': 'unavailable', 'reason': 'access_denied'}))):
            with self.subTest(error=error):
                events = []
                client = CountingClient(events, report_error=error)
                with patch.object(usage_cli, 'read_observation', return_value=observation), \
                        patch.object(usage_cli.UsageBoardClient, 'from_connection_file', return_value=client):
                    self.assertEqual(invoke(usage_cli.run, args), expected)
                self.assertEqual(client.report_arguments, ('owner', 'agent', observation))
                self.assertEqual((client.close_count, events), (1, ['report', 'client_close']))


class DecisionClientLifetimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.summary = self.root / 'summary'
        self.summary.write_text('Selected decision')

    def pin(self, *, endpoint=ENDPOINT, summary=None, registration_error=None, pin_error=None):
        events = []
        client = CountingClient(events, endpoint=endpoint, registration_error=registration_error)
        memory = Mock()

        @contextmanager
        def open_memory(*args, **kwargs):
            events.append('memory_enter')
            try:
                yield memory
            finally:
                events.append('memory_exit')

        def pin(*args, **kwargs):
            self.assertEqual(client.close_count, 0)
            events.append('pin')
            if pin_error is not None:
                raise pin_error
            return {'revision': 1}

        memory.pin.side_effect = pin
        args = SimpleNamespace(endpoint=ENDPOINT, owner='owner', agent='agent', project='project',
            ticket='ticket', state=self.root / 'memory', memory_command='pin',
            connection_file='connection', session='session', revision=0,
            summary_file=self.summary if summary is None else summary, thread='thread',
            message='message', decision=None)
        with patch.object(decision_cli, 'DecisionMemory', side_effect=open_memory), \
                patch.object(decision_cli.BoardReader, 'from_connection_file', return_value=client):
            result = invoke(decision_cli.run, args)
        self.assertEqual(client.close_count, 1)
        self.assertLess(events.index('client_close'), events.index('memory_exit'))
        return result, events, memory

    def test_success_closes_after_pin_before_memory(self):
        result, events, memory = self.pin()
        self.assertEqual(result, (0, {'status': 'pinned', 'revision': 1}))
        self.assertEqual(events, ['memory_enter', 'register', 'pin', 'client_close', 'memory_exit'])
        self.assertEqual(memory.pin.call_args.kwargs['summary'], 'Selected decision')

    def test_endpoint_failure_closes_before_registration_or_pin(self):
        result, events, memory = self.pin(endpoint='https://other.test/mcp')
        self.assertEqual(result, (2, {'status': 'blocked', 'reason': 'decision_scope_invalid'}))
        self.assertNotIn('register', events)
        memory.pin.assert_not_called()

    def test_summary_failure_closes_before_registration_or_pin(self):
        result, events, memory = self.pin(summary=self.root / 'missing')
        self.assertEqual(result, (2, {'status': 'blocked', 'reason': 'decision_summary_invalid'}))
        self.assertNotIn('register', events)
        memory.pin.assert_not_called()

    def test_registration_failure_closes_before_pin(self):
        result, events, memory = self.pin(registration_error=BoardReadError('access_denied'))
        self.assertEqual(result, (2, {'status': 'blocked', 'reason': 'access_denied'}))
        self.assertIn('register', events)
        memory.pin.assert_not_called()

    def test_pin_failure_closes_before_memory(self):
        result, _, _ = self.pin(pin_error=DecisionMemoryError('decision_revision_changed'))
        self.assertEqual(result, (2, {'status': 'blocked', 'reason': 'decision_revision_changed'}))


class RuntimeClientLifetimeTests(unittest.TestCase):
    def reconcile(self, *, kind='ticket', store_error=None, guard_error=None):
        events = []
        client = CountingClient(events)
        scope = {'board_session_id': 'session', 'scope_kind': kind}
        operation = {'id': 'operation', 'configuration':
            {'scope': scope, 'work_id': 'work', 'turn_id': 'turn'}}
        journal = Mock()
        journal.get.return_value = operation
        journal.status.return_value = {'status': 'completed', 'operation': 'operation'}
        store = Mock()
        store.close.side_effect = lambda: events.append('store_close')
        store.status.return_value = {'turns': [{'id': 'turn', 'status': 'completed'}], 'work': {'id': 'work'}}
        store.get_context_guard.return_value = None
        store.turn_result.return_value = {'status': 'completed'}
        guard = Mock()

        @contextmanager
        def open_journal(*args):
            events.append('journal_enter')
            try:
                yield journal
            finally:
                events.append('journal_exit')

        @contextmanager
        def work_lock(*args):
            yield

        def acquire(*args, **kwargs):
            events.append('acquire')
            return client

        def open_store(*args):
            events.append('store_open')
            if store_error is not None:
                raise store_error
            return store

        def confirm(*args):
            self.assertEqual(client.close_count, 0)
            events.append('confirm')
            if guard_error is not None:
                raise guard_error

        store.work_lock.side_effect = work_lock
        guard.confirm_cached.side_effect = confirm
        args = SimpleNamespace(journal='journal', runtime_command='reconcile', operation='operation',
            state='state', connection_file='connection', development_operation=None)
        with ExitStack() as patches:
            patches.enter_context(patch.object(runtime_cli, 'RuntimeJournal', side_effect=open_journal))
            patches.enter_context(patch.object(runtime_cli, 'RunStore', side_effect=open_store))
            legacy = patches.enter_context(patch.object(runtime_cli.RuntimeBoardClient,
                'from_connection_file', side_effect=acquire))
            message = patches.enter_context(patch.object(message_runtime.MessageRuntimeClient,
                'from_connection_file', side_effect=acquire))
            legacy_guard = patches.enter_context(patch.object(runtime_cli, 'RuntimeGuard', return_value=guard))
            message_guard = patches.enter_context(patch.object(message_runtime, 'MessageRuntimeGuard', return_value=guard))
            development_guard = patches.enter_context(patch.object(runtime_cli, 'DevelopmentRuntimeGuard'))
            result = invoke(runtime_cli.run, args)
        selected, unused = (message, legacy) if kind == 'message' else (legacy, message)
        selected.assert_called_once_with('connection', session_id='session', timeout=3)
        unused.assert_not_called()
        development_guard.assert_not_called()
        selected_guard, unused_guard = ((message_guard, legacy_guard) if kind == 'message'
                                       else (legacy_guard, message_guard))
        unused_guard.assert_not_called()
        if store_error is None:
            selected_guard.assert_called_once_with(journal, client, scope)
            self.assertLess(events.index('store_close'), events.index('client_close'))
        else:
            selected_guard.assert_not_called()
            store.close.assert_not_called()
        self.assertEqual(client.close_count, 1)
        self.assertLess(events.index('client_close'), events.index('journal_exit'))
        return result, events, guard

    def test_success_closes_legacy_and_message_clients_before_journal(self):
        for kind in ('ticket', 'message'):
            with self.subTest(kind=kind):
                result, events, guard = self.reconcile(kind=kind)
                self.assertEqual(result, (0, {'status': 'completed', 'operation': 'operation'}))
                self.assertEqual(events, ['journal_enter', 'acquire', 'store_open', 'confirm',
                                          'store_close', 'client_close', 'journal_exit'])
                guard.confirm_cached.assert_called_once_with({'id': 'work'}, 'turn', {'status': 'completed'})

    def test_store_construction_failure_still_closes_acquired_client(self):
        result, events, _ = self.reconcile(store_error=OSError('unavailable state'))
        self.assertEqual(result, (2, {'status': 'needs_review', 'reason': 'runtime_state_unavailable'}))
        self.assertEqual(events, ['journal_enter', 'acquire', 'store_open', 'client_close', 'journal_exit'])

    def test_guard_failure_closes_store_client_and_journal(self):
        result, _, _ = self.reconcile(guard_error=runtime_cli.RuntimeControlError('access_denied'))
        self.assertEqual(result, (2, {'status': 'needs_review', 'reason': 'access_denied'}))


if __name__ == '__main__':
    unittest.main()
