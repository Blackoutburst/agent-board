"""Real CLI/host ownership boundaries; no network, MCP server or model."""
from contextlib import ExitStack, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackoutburst_agents.cli import main
from blackoutburst_agents.family_journal import SLOTS
from blackoutburst_agents.host_supervisor import default_resources
from blackoutburst_agents.message_host import default_message_resources


class Client:
    endpoint = 'https://board.test/mcp'
    session_id = 'session'

    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1

    def adopt_registration(self):
        self.assert_open()

    def assert_open(self):
        if self.closed:
            raise AssertionError('client closed before owner finished')


class OwnerClientLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.instruction = self.root / 'instruction.txt'
        self.instruction.write_text('Fixture instruction')

    def invoke(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(args)
        return code, json.loads(output.getvalue())

    def remote_args(self, kind):
        args = [kind, '--journal', str(self.root / kind)]
        if kind == 'worker':
            args += ['--state', str(self.root / 'runs')]
        args += ['drain' if kind == 'events' else 'run', '--connection-file', 'unused',
                 '--session', 'session', '--owner', 'owner', '--agent', 'agent', '--project', 'project']
        if kind != 'queue':
            args += ['--ticket', 'ticket']
        if kind != 'events':
            args += ['--request', 'request']
        return args + ['--instruction-file', str(self.instruction), '--workspace', str(self.root),
                       '--codex', sys.executable, '--model', 'fixture-model']

    def test_worker_closes_worker_and_runtime_on_success_and_failure(self):
        for failure in (None, 'worker', 'journal'):
            with self.subTest(failure=failure), ExitStack() as stack:
                raw, control = Client(), Client()
                stack.enter_context(patch('blackoutburst_agents.worker_cli.WorkerBoardClient.from_connection_file', return_value=raw))
                factory = stack.enter_context(patch('blackoutburst_agents.worker_cli.RuntimeBoardClient.from_worker', return_value=control))
                worker = stack.enter_context(patch('blackoutburst_agents.worker_cli.BoardWorker'))
                def execute(**kwargs):
                    raw.assert_open(); control.assert_open()
                    if failure == 'worker':
                        raise OSError('fixture')
                    return {'status': 'complete', 'stage': 'complete', 'reason': 'publication_confirmed', 'cached': False}
                worker.return_value.run.side_effect = execute
                if failure == 'journal':
                    stack.enter_context(patch('blackoutburst_agents.worker_cli.WorkJournal', side_effect=OSError('fixture')))
                code, _ = self.invoke(self.remote_args('worker') + ['--runtime-journal', str(self.root / 'runtime')])
                self.assertEqual(code, 0 if failure is None else 2)
                self.assertEqual(raw.closed, 1)
                self.assertEqual(control.closed, 0 if failure == 'journal' else 1)
                if failure != 'journal':
                    factory.assert_called_once_with(raw)

    def test_queue_preview_and_reconcile_selection_close_their_clients(self):
        registration = {'identity': {'id': 'agent', 'owner_id': 'owner', 'device_id': 'device', 'agent_kind': 'codex', 'name': 'Fixture'},
            'session': {'id': 'session', 'agent_id': 'agent', 'owner_id': 'owner', 'device_id': 'device', 'agent_kind': 'codex', 'skills': []},
            'policy': {'enabled': True, 'allow_read': True, 'allow_requests': False}}
        for failure in (False, True):
            with self.subTest(failure=failure), ExitStack() as stack:
                raw = Client()
                def register():
                    raw.assert_open()
                    if failure:
                        raise OSError('fixture')
                    return registration
                raw.register = register
                raw.call = lambda name, args: ({'id': 'project', 'participants': ['owner'], 'archived': False}
                    if name == 'get_project' else {'ready': [], 'recovery': [], 'waiting': []})
                stack.enter_context(patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file', return_value=raw))
                code, _ = self.invoke(self.remote_args('queue'))
                self.assertEqual(code, 2 if failure else 0)
                self.assertEqual(raw.closed, 1)
        raw = Client()
        with patch('blackoutburst_agents.queue_cli.QueueBoardClient.from_connection_file', return_value=raw), \
                patch('blackoutburst_agents.queue_cli.QueueJournal') as journals, \
                patch('blackoutburst_agents.queue_cli.QueueWorker') as workers:
            journals.return_value.__enter__.return_value.get.return_value = {'id': 'operation', 'scope': {'board_session_id': 'session'}}
            workers.return_value.reconcile_selection.return_value = {'status': 'complete', 'stage': 'complete',
                'reason': 'selection_confirmed', 'cached': False, 'operation_id': 'operation'}
            self.assertEqual(self.invoke(['queue', '--journal', str(self.root / 'queue'), 'reconcile-selection',
                '--operation', 'operation', '--connection-file', 'unused'])[0], 0)
        self.assertEqual(raw.closed, 1)

    def test_event_drain_closes_client_before_or_after_journal_failure(self):
        receipt = {'status': 'idle', 'reason': 'no_pending_events', 'after': 0, 'ingested_count': 0, 'pages': 1}
        for failure in (None, 'journal', 'drain'):
            with self.subTest(failure=failure), ExitStack() as stack:
                raw = Client()
                stack.enter_context(patch('blackoutburst_agents.event_cli.EventBoardClient.from_connection_file', return_value=raw))
                drainer = stack.enter_context(patch('blackoutburst_agents.event_cli.EventDrainer'))
                def drain(**kwargs):
                    raw.assert_open()
                    if failure == 'drain':
                        raise OSError('fixture')
                    return receipt
                drainer.return_value.drain.side_effect = drain
                if failure == 'journal':
                    stack.enter_context(patch('blackoutburst_agents.event_cli.EventJournal', side_effect=OSError('fixture')))
                self.assertEqual(self.invoke(self.remote_args('events'))[0], 0 if failure is None else 2)
                self.assertEqual(raw.closed, 1)

    def test_event_reconcile_closes_human_lookup_client_even_on_scope_mismatch(self):
        scope = {'board_session_id': 'session', 'board_endpoint': Client.endpoint}
        operation = {'id': 'operation', 'scope': scope, 'request_id': 'request', 'configuration': {}, 'stage': 'complete'}
        batch = {'id': 'batch', 'subscription_id': 'subscription', 'request_id': 'request'}
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch), ExitStack() as stack:
                raw = Client()
                if mismatch:
                    raw.endpoint = 'https://other.test/mcp'
                stack.enter_context(patch('blackoutburst_agents.event_cli.HumanEventBoardClient.from_connection_file', return_value=raw))
                journals = stack.enter_context(patch('blackoutburst_agents.event_cli.EventJournal'))
                journal = journals.return_value.__enter__.return_value
                journal.get_batch.return_value = batch
                journal.get_subscription.return_value = {'scope': scope, 'configuration': {}}
                journal.resolve_batch.return_value = batch
                work = stack.enter_context(patch('blackoutburst_agents.event_cli.WorkJournal'))
                work.return_value.__enter__.return_value.get.return_value = operation
                worker = stack.enter_context(patch('blackoutburst_agents.event_cli.BoardWorker'))
                worker.return_value.lookup_human_question.return_value = {'id': 'question'}
                self.assertEqual(self.invoke(['events', '--journal', str(self.root / 'events'),
                    '--work-journal', str(self.root / 'work'), 'reconcile', '--batch', 'batch',
                    '--operation', 'operation', '--connection-file', 'unused'])[0], 2 if mismatch else 0)
                self.assertEqual(raw.closed, 1)

    def test_queue_and_events_execution_close_separate_runtime_channels(self):
        for kind, module, client_kind, operation in (
                ('queue', 'queue_cli', 'QueueBoardClient', 'QueueWorker'),
                ('events', 'event_cli', 'EventBoardClient', 'EventDrainer')):
            with self.subTest(kind=kind), ExitStack() as stack:
                raw, control = Client(), Client()
                base = 'blackoutburst_agents.' + module + '.'
                stack.enter_context(patch(base + client_kind + '.from_connection_file', return_value=raw))
                stack.enter_context(patch('blackoutburst_agents.worker_cli.RuntimeBoardClient.from_worker', return_value=control))
                stack.enter_context(patch(base + 'BoardWorker'))
                worker = stack.enter_context(patch(base + operation))
                if kind == 'queue':
                    worker.return_value.run.return_value = {'status': 'complete', 'stage': 'complete',
                        'reason': 'publication_confirmed', 'cached': False, 'operation_id': 'operation'}
                else:
                    worker.return_value.drain.return_value = {'status': 'complete', 'reason': 'worker_complete',
                        'after': 0, 'ingested_count': 0, 'pages': 1}
                args = self.remote_args(kind)
                args = args[:3] + ['--work-journal', str(self.root / (kind + '-work')),
                    '--state', str(self.root / (kind + '-runs'))] + args[3:] + [
                    '--execute', '--runtime-journal', str(self.root / (kind + '-runtime'))]
                self.assertEqual(self.invoke(args)[0], 0)
                self.assertEqual((raw.closed, control.closed), (1, 1))

    def test_development_resume_closes_clients_on_success_scope_or_worker_failure(self):
        operation = {'scope': {'board_session_id': 'session', 'board_endpoint': Client.endpoint},
            'configuration': {'pins': {kind: {'path': str(self.root / kind)} for kind in ('runs', 'runtime')}}}
        for failure in (None, 'scope', 'worker'):
            with self.subTest(failure=failure), ExitStack() as stack:
                raw, control = Client(), Client()
                if failure == 'scope':
                    raw.endpoint = 'https://other.test/mcp'
                base = 'blackoutburst_agents.development_cli.'
                stack.enter_context(patch(base + 'DevelopmentBoardClient.from_connection_file', return_value=raw))
                stack.enter_context(patch(base + 'RuntimeBoardClient.from_worker', return_value=control))
                journals = stack.enter_context(patch(base + 'WorkJournal'))
                journals.return_value.__enter__.return_value.get.return_value = operation
                stack.enter_context(patch(base + 'RunStore'))
                stack.enter_context(patch(base + 'RuntimeJournal'))
                worker = stack.enter_context(patch(base + 'DevelopmentBoardWorker'))
                def execute(*args, **kwargs):
                    raw.assert_open(); control.assert_open()
                    if failure == 'worker':
                        raise OSError('fixture')
                    return {'status': 'review_ready'}
                worker.return_value.run.side_effect = execute
                self.assertEqual(self.invoke(['development', '--journal', str(self.root / 'development'),
                    'run', '--operation', 'operation', '--connection-file', 'unused'])[0], 0 if failure is None else 2)
                self.assertEqual(raw.closed, 1)
                self.assertEqual(control.closed, 0 if failure == 'scope' else 1)

    def test_family_closes_every_slot_and_partial_construction(self):
        state = {'configuration': {'scope': {'board_endpoint': Client.endpoint}, 'registrations': {},
            'provisional_scopes': {slot: {'board_session_id': slot} for slot in SLOTS},
            'pins': {slot: {'journal': {'path': str(self.root / slot / 'work')}} for slot in SLOTS},
            'child_stores': {slot: {kind: {'path': str(self.root / slot / kind)} for kind in ('runs', 'runtime')} for slot in SLOTS},
            'roots': {}}}
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch), ExitStack() as stack:
                clients, controls = [Client() for _ in SLOTS], [Client() for _ in SLOTS]
                if mismatch:
                    clients[1].endpoint = 'https://other.test/mcp'
                stack.enter_context(patch('blackoutburst_agents.family_cli._local_state', return_value=(state, False)))
                stack.enter_context(patch('blackoutburst_agents.family_cli._resume_preflight'))
                stack.enter_context(patch('blackoutburst_agents.family_cli._outside'))
                journals = stack.enter_context(patch('blackoutburst_agents.family_cli.FamilyJournal'))
                journals.return_value.__enter__.return_value.read.return_value = state
                for name in ('WorkJournal', 'RunStore', 'RuntimeJournal', 'DevelopmentBoardWorker'):
                    stack.enter_context(patch('blackoutburst_agents.family_cli.' + name))
                stack.enter_context(patch('blackoutburst_agents.family_cli.FamilyBoardClient.from_connection_file', side_effect=clients))
                stack.enter_context(patch('blackoutburst_agents.family_cli.RuntimeBoardClient.from_worker', side_effect=controls))
                worker = stack.enter_context(patch('blackoutburst_agents.family_cli.DevelopmentFamilyWorker'))
                worker.return_value.run.return_value = {'status': 'review_ready'}
                code, _ = self.invoke(['family', '--journal', str(self.root / 'family'), 'run', '--connection-file', 'unused'])
                self.assertEqual(code, 2 if mismatch else 0)
                self.assertEqual([client.closed for client in clients], [1, 1, 0] if mismatch else [1, 1, 1])
                self.assertEqual([control.closed for control in controls], [1, 0, 0] if mismatch else [1, 1, 1])

    def test_host_resource_wrappers_close_raw_and_separate_control(self):
        for message in (False, True):
            for failure in (None, 'wrapper', 'body'):
                with self.subTest(message=message, failure=failure), ExitStack() as stack:
                    raw, control = Client(), Client()
                    module = 'blackoutburst_agents.message_host' if message else 'blackoutburst_agents.host_supervisor'
                    kind = 'MessageBoardClient' if message else 'EventBoardClient'
                    runtime = 'MessageRuntimeClient' if message else 'RuntimeBoardClient'
                    wrapper = 'PinnedMessageClient' if message else 'PinnedClient'
                    stack.enter_context(patch(module + '.' + kind + '.from_connection_file', return_value=raw))
                    factory = stack.enter_context(patch(module + '.' + runtime + '.from_worker', return_value=control))
                    if failure == 'wrapper':
                        stack.enter_context(patch(module + '.' + wrapper, side_effect=ValueError('fixture')))
                    host = SimpleNamespace(directory=self.root / ('message-host' if message else 'host'))
                    host.directory.mkdir(mode=0o700, exist_ok=True)
                    target = {'connection_file': 'unused', 'board_session_id': 'session'}
                    scope = {'board_session_id': 'session'}
                    resources = (default_message_resources(host, target, {'owner_id': 'owner'}, lambda: False) if message
                        else default_resources(host, target, scope, lambda: False))
                    try:
                        with resources as instance:
                            self.assertIs(instance.client.client, raw)
                            raw.assert_open(); control.assert_open()
                            if failure == 'body':
                                raise ValueError('fixture')
                    except ValueError:
                        self.assertIsNotNone(failure)
                    else:
                        self.assertIsNone(failure)
                    self.assertEqual(raw.closed, 1)
                    self.assertEqual(control.closed, 0 if failure == 'wrapper' else 1)
                    if failure != 'wrapper':
                        factory.assert_called_once_with(raw)


if __name__ == '__main__':
    unittest.main()
