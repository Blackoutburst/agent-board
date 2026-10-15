from copy import deepcopy
import unittest
from unittest.mock import Mock

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.runtime_client import RuntimeBoardClient


class RuntimeClientTests(unittest.TestCase):
    def setUp(self):
        self.client=RuntimeBoardClient({'mcp_url':'https://board.test/mcp','token':'PRIVATE_TOKEN',
            'client':'codex','identity_key':'binding','name':'Agent'},'original-session',3)
        self.client._initialize=Mock()
        self.client._tool=Mock(return_value={'found':False})

    def test_lookup_only_uses_exact_original_session_without_registration(self):
        result=self.client.call('lookup_execution',{'idempotency_key':'exact-key'})
        self.assertEqual(result,{'found':False})
        self.client._tool.assert_called_once_with('lookup_execution',{'idempotency_key':'exact-key','session_id':'original-session'})
        with self.assertRaises(BoardReadError):self.client.register()

    def test_only_six_runtime_operations_are_allowed(self):
        for name in ['post_message','claim_ticket','register_agent','update_ticket','get_thread']:
            with self.assertRaises(BoardReadError):self.client.call(name,{})
        self.client._tool.assert_not_called()

    def test_invalid_control_arguments_are_rejected_before_any_network(self):
        for args in [{'execution_id':'x','expected_revision':True},{'execution_id':'x','expected_revision':0},
            {'execution_id':'x','expected_revision':1,'session_id':'other'},
            {'execution_id':'/private/path','expected_revision':1}]:
            with self.assertRaises(BoardReadError):self.client.call('start_execution',args)
        self.client._initialize.assert_not_called()

    def test_finish_requires_strict_cleanup_status_and_safe_reason(self):
        base={'execution_id':'id','expected_revision':1,'cleanup_confirmed':True,'status':'completed','reason':'completed'}
        for update in [{'cleanup_confirmed':1},{'status':'done'},{'reason':'PRIVATE diagnostic'},
            {'prompt':'PRIVATE'},{'reason':None}]:
            with self.assertRaises(BoardReadError):self.client.call('finish_execution',{**base,**update})
        self.client.call('finish_execution',deepcopy(base))
        self.assertEqual(self.client._tool.call_count,1)

    def test_reservation_never_accepts_credentials_or_another_session(self):
        base=dict(idempotency_key='key',project_id='project',ticket_id='ticket',work_id='work',turn_id='turn',provider='codex_chatgpt',model='model')
        for update in [{'model':''},{'model':'x\nsecret'},{'token':'PRIVATE'},{'session_id':'other'}]:
            with self.assertRaises(BoardReadError):self.client.call('reserve_execution',{**base,**update})
        self.client._tool.assert_not_called()
