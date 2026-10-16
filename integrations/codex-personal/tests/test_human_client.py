"""Question-only transport validation without a live connection."""
import json
import unittest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.human_client import HumanWorkerClient
from blackoutburst_agents.worker_client import WorkerBoardClient
from test_board_reader import Opener, Response, TOKEN, URL


class HumanClientTests(unittest.TestCase):
    def setUp(self):
        self.client = HumanWorkerClient({'mcp_url': URL, 'token': TOKEN, 'client': 'codex',
            'identity_key': 'installation', 'name': 'Test Codex'}, 'explicit-worker', 20)
        self.client._opener = Opener()
        self.arguments = {'ticket_id': 'chosen', 'owner_id': 'named-human',
            'question': 'Quelle langue faut-il utiliser ?', 'idempotency_key': 'stable-question',
            'kind': 'question'}

    def test_lookup_and_creation_share_payload_and_pinned_session(self):
        for name in ('lookup_human_request', 'request_human_input'):
            arguments = {**self.arguments, **({'expected_version': 7} if name == 'request_human_input' else {})}
            before = json.dumps(arguments, sort_keys=True)
            self.assertEqual(self.client.call(name, arguments), {'read': name})
            self.assertEqual(json.dumps(arguments, sort_keys=True), before)
        requests = [json.loads(request.data) for request, _ in self.client._opener.requests]
        self.assertEqual(len(requests), 4)
        self.assertFalse(requests[1]['params']['arguments']['autonomous'])
        for request, name in zip(requests[2:], ('lookup_human_request', 'request_human_input')):
            self.assertEqual(request['params'], {'name': name, 'arguments': {**self.arguments, 'session_id': 'explicit-worker', 'view':'full',
                **({'expected_version': 7} if name == 'request_human_input' else {})}})

    def test_invalid_or_broader_requests_never_register_or_reach_network(self):
        variants = [None, [], {}, {**self.arguments, 'kind': 'approval'},
            {**self.arguments, 'kind': 'action'}, {**self.arguments, 'owner_id': ' '},
            {**self.arguments, 'owner_id': '\ud800'}, {**self.arguments, 'ticket_id': 't\nx'},
            {**self.arguments, 'question': ''}, {**self.arguments, 'question': '\ud800'},
            {**self.arguments, 'question': '\x00'}, {**self.arguments, 'question': 'a' * 6001},
            {**self.arguments, 'question': '🙂' * 1501}, {**self.arguments, 'question': []},
            {**self.arguments, 'idempotency_key': 'k' * 201},
            {**self.arguments, 'lookup_only': 1}, {**self.arguments, 'lookup_only': None},
            {**self.arguments, 'session_id': 'other'}, {**self.arguments, 'action': 'run'},
            {key: value for key, value in self.arguments.items() if key != 'kind'}]
        for args in variants:
            with self.subTest(arguments=repr(args)[:80]):
                with self.assertRaises(BoardReadError) as error:
                    self.client.call('lookup_human_request', args)
                self.assertEqual(error.exception.code, 'invalid_arguments')
        for version in (None, True, 0, -1, 2 ** 63, '7', 1.5):
            with self.assertRaises(BoardReadError) as error:
                self.client.call('request_human_input', {**self.arguments, 'expected_version': version})
            self.assertEqual(error.exception.code, 'invalid_arguments')
        with self.assertRaises(BoardReadError):
            self.client.call('request_human_input', self.arguments)
        with self.assertRaises(BoardReadError):
            self.client.call('lookup_human_request', {**self.arguments, 'expected_version': 7})
        for name in ('post_message', 'claim_next_ticket', 'respond_human_input', 'list_human_requests',
                     'delegate_task', 'read_inbox', 'list_activity'):
            with self.assertRaises(BoardReadError) as error:
                self.client.call(name, {})
            self.assertEqual(error.exception.code, 'tool_not_allowed')
        self.assertEqual(self.client._opener.requests, [])

    def test_unicode_question_at_limit_is_accepted_without_expanding_plain_worker(self):
        self.client.call('request_human_input', {**self.arguments, 'expected_version': 7, 'question': '🙂' * 1500})
        worker = WorkerBoardClient(self.client._config, 'normal-worker', 20)
        worker._opener = Opener()
        with self.assertRaises(BoardReadError) as error:
            worker.call('request_human_input', self.arguments)
        self.assertEqual(error.exception.code, 'tool_not_allowed')
        self.assertEqual(worker._opener.requests, [])

    def test_remote_failure_does_not_disclose_question_or_credentials(self):
        self.client._initialized = self.client._registered = True
        self.client._opener = Opener([Response(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
            'isError': True, 'content': [{'type': 'text', 'text': TOKEN + ' private question'}]}}).encode())])
        with self.assertRaises(BoardReadError) as error:
            self.client.call('request_human_input', {**self.arguments, 'expected_version': 7})
        self.assertEqual(str(error.exception), 'tool_rejected')


if __name__ == '__main__':
    unittest.main()
