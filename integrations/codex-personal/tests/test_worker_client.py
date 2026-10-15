"""Mutation scope validation using fake MCP responses, never a live board."""
import json
from pathlib import Path
import tempfile
import unittest

from blackoutburst_agents.board_reader import BoardReader, BoardReadError
from blackoutburst_agents.worker_client import WorkerBoardClient
from test_board_reader import Opener, Response, TOKEN, URL


class WorkerClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'connection.json'
        self.config = {'mcp_url': URL, 'token': TOKEN, 'client': 'codex',
                       'identity_key': 'installation', 'name': 'Test Codex'}
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)
        self.client = WorkerBoardClient.from_connection_file(self.path, session_id='explicit-worker')
        self.client._opener = Opener()

    def error(self, code, action):
        with self.assertRaises(BoardReadError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(TOKEN, repr(caught.exception))

    def test_five_operations_inject_session_and_preserve_arguments(self):
        calls = [
            ('get_ticket', {'ticket_id': 'chosen'}),
            ('get_thread', {'thread_id': 'discussion', 'before': 'older', 'limit': 50}),
            ('claim_ticket', {'ticket_id': 'chosen', 'expected_version': 1}),
            ('renew_ticket', {'ticket_id': 'chosen', 'expected_version': 2, 'progress': 'Text turn'}),
            ('update_ticket', {'ticket_id': 'chosen', 'expected_version': 3,
                'changes': {'status': 'review', 'outcome': 'Review text', 'evidence': 'Text-only', 'release_claim': True}}),
        ]
        for name, args in calls:
            before = json.dumps(args, sort_keys=True)
            self.assertEqual(self.client.call(name, args), {'read': name})
            self.assertEqual(json.dumps(args, sort_keys=True), before)
        requests = [json.loads(request.data) for request, _ in self.client._opener.requests]
        self.assertEqual(len(requests), 7)
        registration = requests[1]['params']['arguments']
        self.assertFalse(registration['autonomous'])
        self.assertEqual(registration['skills'], [])
        for index, (name, args) in enumerate(calls, 2):
            self.assertEqual(requests[index]['params'], {'name': name,
                'arguments': {**args, 'session_id': 'explicit-worker'}})

    def test_explicit_registration_rechecks_policy_without_enabling_queue(self):
        self.client.register()
        self.client.register()
        requests = [json.loads(request.data) for request, _ in self.client._opener.requests]
        self.assertEqual([request['params']['name'] for request in requests[1:]], ['register_agent'] * 2)
        self.assertTrue(all(request['params']['arguments']['autonomous'] is False for request in requests[1:]))

    def test_unrelated_tools_and_session_overrides_never_reach_transport(self):
        for name in ('post_message', 'claim_next_ticket', 'read_inbox', 'get_work', 'create_ticket',
                     'register_agent', 'list_activity', 'delegate_task', 'request_human_input', None):
            self.error('tool_not_allowed', lambda: self.client.call(name, {}))
        for name, args in (
            ('get_ticket', {'ticket_id': 't', 'session_id': 'another'}),
            ('claim_ticket', {'ticket_id': 't', 'expected_version': 1, 'session_id': 'another'}),
        ):
            self.error('invalid_arguments', lambda: self.client.call(name, args))
        self.assertEqual(self.client._opener.requests, [])

    def test_mutators_reject_unknown_assignment_completion_and_recovery_fields(self):
        forbidden = ['assignee_agent_id', 'assignee_owner_id', 'depends_on', 'title', 'description',
                     'priority', 'labels', 'required_skills', 'resource', 'progress', 'token']
        for field in forbidden:
            self.error('invalid_arguments', lambda: self.client.call('update_ticket', {
                'ticket_id': 't', 'expected_version': 1, 'changes': {field: 'forbidden'}}))
        for status in ('done', 'todo', 'in_progress', None, [], True):
            self.error('invalid_arguments', lambda: self.client.call('update_ticket', {
                'ticket_id': 't', 'expected_version': 1, 'changes': {'status': status}}))
        self.error('invalid_arguments', lambda: self.client.call('claim_ticket', {
            'ticket_id': 't', 'expected_version': 1, 'recovery_evidence': 'Unreviewed recovery'}))
        self.assertEqual(self.client._opener.requests, [])

    def test_required_fields_types_versions_and_bounds_are_checked_before_network(self):
        invalid = [
            ('get_ticket', []), ('get_ticket', {}), ('get_ticket', {'ticket_id': ' '}),
            ('get_ticket', {'ticket_id': 'x\ny'}), ('get_ticket', {'ticket_id': 't', 'project_id': 'p'}),
            ('get_thread', {'thread_id': 't', 'limit': True}),
            ('get_thread', {'thread_id': 't', 'limit': 201}),
            ('get_thread', {'thread_id': 't', 'before': 1}),
            ('get_thread', {'thread_id': 't', 'unknown': None}),
            ('claim_ticket', {'ticket_id': 't'}),
            ('renew_ticket', {'ticket_id': 't', 'expected_version': 1, 'progress': None}),
            ('renew_ticket', {'ticket_id': 't', 'expected_version': 1, 'progress': '\x00'}),
            ('update_ticket', {'ticket_id': 't', 'expected_version': 1, 'changes': {}}),
            ('update_ticket', {'ticket_id': 't', 'expected_version': 1, 'changes': []}),
            ('update_ticket', {'ticket_id': 't', 'expected_version': 1, 'changes': {'release_claim': 1}}),
            ('update_ticket', {'ticket_id': 't', 'expected_version': 1, 'changes': {'outcome': 'x' * 20001}}),
            ('update_ticket', {'ticket_id': 't', 'expected_version': 1, 'changes': {'evidence': '\ud800'}}),
            ('update_ticket', {'ticket_id': 't', 'expected_version': 1, 'changes': {'outcome': '🙂' * 20000}}),
        ]
        for revision in (None, True, 0, -1, 2 ** 63, 1.5, float('nan')):
            invalid.append(('claim_ticket', {'ticket_id': 't', 'expected_version': revision}))
        for name, args in invalid:
            with self.subTest(name=name, args=str(args)[:70]):
                self.error('invalid_arguments', lambda: self.client.call(name, args))
        self.assertEqual(self.client._opener.requests, [])

    def test_endpoint_and_connection_are_pinned_without_modifying_reader(self):
        client = WorkerBoardClient(self.config, 'worker', 20)
        self.config['mcp_url'] = 'https://different.test/mcp'
        self.config['token'] = 'DIFFERENT'
        self.assertEqual(client.endpoint, URL)
        self.assertEqual(client._config['mcp_url'], URL)
        self.assertEqual(client._config['token'], TOKEN)
        with self.assertRaises(AttributeError):
            client.endpoint = 'https://different.test/mcp'
        reader = BoardReader.from_connection_file(self.path, session_id='reader')
        self.error('tool_not_allowed', lambda: reader.call('claim_ticket', {'ticket_id': 't', 'expected_version': 1}))

    def test_remote_error_response_is_fixed_and_private(self):
        self.client._initialized = self.client._registered = True
        self.client._opener = Opener([Response(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
            'isError': True, 'content': [{'type': 'text', 'text': TOKEN}]}}).encode())])
        self.error('tool_rejected', lambda: self.client.call('update_ticket', {
            'ticket_id': 't', 'expected_version': 1, 'changes': {'status': 'blocked'}}))


if __name__ == '__main__':
    unittest.main()
