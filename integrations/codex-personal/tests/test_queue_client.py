"""Queue transport opt-in and bounds; no network or model calls."""
from copy import deepcopy
import unittest
from unittest.mock import Mock

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.queue_client import QueueBoardClient, HumanQueueBoardClient


class QueueClientTests(unittest.TestCase):
    def setUp(self):
        self.config={'mcp_url':'https://board.test/mcp','token':'PRIVATE_TOKEN','client':'codex',
                     'identity_key':'installation','name':'Test'}
        self.client=QueueBoardClient(self.config,'queue-session',20)
        self.client._initialized=True
        self.registration={'identity':{'id':'agent','owner_id':'owner','device_id':'device','agent_kind':'codex'},
            'session':{'id':'queue-session','agent_id':'agent','owner_id':'owner','device_id':'device','autonomous':True},
            'policy':{'enabled':True,'allow_read':True,'allow_requests':True,'autonomy':'queue'}}
        self.client._tool=Mock(return_value=deepcopy(self.registration))

    def test_selection_requires_explicit_registration_and_default_registration_revokes_opt_in(self):
        args={'project_id':'project','idempotency_key':'stable'}
        with self.assertRaises(BoardReadError):self.client.call('claim_next_ticket',args)
        self.client._tool.assert_not_called()
        self.client.register_for_selection()
        self.assertTrue(self.client._tool.call_args.args[1]['autonomous'])
        self.client._tool.return_value={'claimed':False,'reason':'no_ready_ticket'}
        self.client.call('claim_next_ticket',args)
        self.client._tool.assert_called_with('claim_next_ticket',{**args,'session_id':'queue-session'})
        self.client._tool.return_value=deepcopy(self.registration)
        self.client.register()
        self.assertFalse(self.client._tool.call_args.args[1]['autonomous'])
        with self.assertRaises(BoardReadError):self.client.call('claim_next_ticket',args)

    def test_lookup_and_preview_never_enable_autonomous_work(self):
        self.client._tool.side_effect=[deepcopy(self.registration),{'found':False},{'ready':[]},{'id':'project'}]
        self.assertEqual(self.client.call('lookup_ticket_selection',{'project_id':'project','idempotency_key':'stable'}),{'found':False})
        self.client.call('get_work',{'project_id':'project','limit':10})
        self.client.call('get_project',{'project_id':'project'})
        self.assertFalse(self.client._tool.call_args_list[0].args[1]['autonomous'])
        self.assertFalse(self.client._selection_enabled)

    def test_bad_arguments_rejected_before_registration(self):
        for name,args in [('get_project',{}),('get_project',{'project_id':''}),
                         ('get_project',{'project_id':'p','session_id':'other'}),
                         ('get_work',{'project_id':'p','limit':True}),('get_work',{'project_id':'p','limit':201}),
                         ('lookup_ticket_selection',{'project_id':'p'}),
                         ('lookup_ticket_selection',{'project_id':'p','idempotency_key':'\ud800'}),
                         ('claim_next_ticket',{'project_id':'p','idempotency_key':'x'*201})]:
            with self.assertRaises(BoardReadError):self.client.call(name,args)
        self.client._tool.assert_not_called()

    def test_unconfirmed_or_restricted_registration_cannot_select(self):
        for field,value in [('autonomous',False),('agent_id','another'),('owner_id','other')]:
            response=deepcopy(self.registration);response['session'][field]=value
            self.client._tool.return_value=response
            with self.assertRaises(BoardReadError):self.client.register_for_selection()
            self.assertFalse(self.client._selection_enabled)
        response=deepcopy(self.registration);response['policy']['autonomy']='requested'
        self.client._tool.return_value=response
        with self.assertRaises(BoardReadError):self.client.register_for_selection()

    def test_human_queue_client_only_adds_bounded_question_tools(self):
        client=HumanQueueBoardClient(self.config,'queue-session',20)
        client._registered=True
        client._tool=Mock(return_value={'found':False})
        question={'ticket_id':'ticket','owner_id':'human','question':'Which environment?',
                  'kind':'question','idempotency_key':'question-key'}
        client.call('lookup_human_request',question)
        client.call('get_project',{'project_id':'project'})
        for name,args in [('read_inbox',{}),('post_message',{}),('delegate_task',{}),
                         ('request_human_input',{**question,'kind':'approval','expected_version':1})]:
            with self.assertRaises(BoardReadError):client.call(name,args)
        with self.assertRaises(BoardReadError):self.client.call('lookup_human_request',question)


if __name__=='__main__':
    unittest.main()
