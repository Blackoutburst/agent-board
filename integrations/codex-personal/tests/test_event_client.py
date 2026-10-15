"""Feed transport bounds; no network, credentials or live board."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.event_client import EventBoardClient, HumanEventBoardClient


class EventClientTests(unittest.TestCase):
    def setUp(self):
        self.client = EventBoardClient({'mcp_url': 'https://board.test/mcp', 'token': 'PRIVATE_TOKEN',
            'client': 'codex', 'identity_key': 'test', 'name': 'Test'}, 'session', 20)
        self.client._registered = True
        self.client._rpc = Mock()
        self.args = {'project_id': 'project', 'ticket_id': 'ticket', 'after': 42, 'limit': 50, 'order': 'asc'}

    def error(self, code, action):
        with self.assertRaises(BoardReadError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn('PRIVATE', str(caught.exception))

    def test_structured_and_text_list_responses_keep_pinned_session(self):
        events = [{'id': 43, 'event': 'ticket_updated'}]
        for response in ({'structuredContent': {'result': events}},
                         {'content': [{'type': 'text', 'text': json.dumps(events)}]},
                         {'content': [{'type': 'text', 'text': json.dumps({'result': events})}]}):
            self.client._rpc.return_value = response
            before = deepcopy(self.args)
            self.assertEqual(self.client.call('list_activity', self.args), events)
            self.assertEqual(self.args, before)
            self.client._rpc.assert_called_with('tools/call', {'name': 'list_activity',
                'arguments': {**self.args, 'session_id': 'session'}})

    def test_only_complete_scoped_ascending_requests_pass(self):
        bad = [None, {}, {**self.args, 'session_id': 'other'}, {**self.args, 'order': 'desc'},
               {**self.args, 'limit': 201}, {**self.args, 'limit': True}, {**self.args, 'after': -1},
               {**self.args, 'after': True}, {**self.args, 'after': 2 ** 63},
               {**self.args, 'ticket_id': ''}, {**self.args, 'project_id': '\ud800'}]
        for values in bad:
            self.error('invalid_arguments', lambda: self.client.call('list_activity', values))
        self.client._rpc.assert_not_called()

    def test_invalid_response_and_remote_error_never_expose_body(self):
        for response in (None, {'isError': 'yes'}, {'structuredContent': {'wrong': []}},
                         {'structuredContent': {'result': [1]}},
                         {'structuredContent': {'result': [{}] * 51}},
                         {'content': [{'type': 'text', 'text': 'PRIVATE DIAGNOSTIC'}]}):
            self.client._rpc.return_value = response
            self.error('invalid_response', lambda: self.client.call('list_activity', self.args))
        self.client._rpc.return_value = {'isError': True, 'content': [{'type': 'text', 'text': 'PRIVATE_TOKEN'}]}
        self.error('tool_rejected', lambda: self.client.call('list_activity', self.args))

    def test_queue_and_inbox_tools_still_refused(self):
        for name in ('read_inbox', 'get_work', 'claim_next_ticket', 'post_message'):
            self.error('tool_not_allowed', lambda: self.client.call(name, {}))
        self.client._rpc.assert_not_called()

    def test_registration_remains_non_autonomous_before_first_feed(self):
        self.client._registered = False
        self.client._initialized = True
        self.client._rpc.side_effect = [
            {'structuredContent': {'session': {'id': 'session'}}},
            {'structuredContent': {'result': []}},
        ]
        self.assertEqual(self.client.call('list_activity', self.args), [])
        registration = self.client._rpc.call_args_list[0].args[1]
        self.assertEqual(registration['name'], 'register_agent')
        self.assertFalse(registration['arguments']['autonomous'])

    def test_human_event_client_composes_both_narrow_contracts_without_queue_access(self):
        client=HumanEventBoardClient(self.client._config, 'human-event-session', 20)
        client._registered=True
        client._rpc=Mock(return_value={'structuredContent': {'result': []}})
        self.assertEqual(client.call('list_activity', self.args), [])
        question={'ticket_id':'ticket','owner_id':'named-human','question':'Which test environment?',
            'kind':'question','idempotency_key':'event-question'}
        client._tool=Mock(return_value={'found':False})
        self.assertEqual(client.call('lookup_human_request',question),{'found':False})
        client._tool.assert_called_with('lookup_human_request',{**question,'session_id':'human-event-session'})
        client.call('request_human_input',{**question,'expected_version':7})
        self.assertEqual(client._tool.call_args.args[1]['expected_version'],7)
        for name,args in [('request_human_input',{**question,'kind':'approval','expected_version':7}),
                          ('list_activity',{**self.args,'order':'desc'}),('claim_next_ticket',{}),
                          ('read_inbox',{}),('post_message',{}),('resolve_human_request',{})]:
            with self.assertRaises(BoardReadError):client.call(name,args)
        with self.assertRaises(BoardReadError):self.client.call('lookup_human_request',question)


if __name__ == '__main__':
    unittest.main()
