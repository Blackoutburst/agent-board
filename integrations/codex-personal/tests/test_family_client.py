"""No HTTP, model or server: fixed tool and argument boundary only."""
import unittest
from unittest.mock import patch

from blackoutburst_agents.family_client import FamilyBoardClient,_arguments
from blackoutburst_agents.board_reader import BoardReadError


class FamilyClientTests(unittest.TestCase):
    def setUp(self):
        self.client=FamilyBoardClient({'mcp_url':'https://board.test/mcp','token':'synthetic',
            'client':'codex','identity_key':'family-fixture','name':'Fixture'},'root',3)
        self.client.adopt_registration()

    def test_fixed_scope_injected_without_registration_or_extra_tools(self):
        with patch.object(self.client,'_initialize'),patch.object(self.client,'_tool',return_value={'state':'running'}) as tool:
            self.client.call('get_delegation_family',{'family_id':'family'})
            self.assertEqual(tool.call_args.args,('get_delegation_family',{'family_id':'family','session_id':'root'}))
            with self.assertRaises(BoardReadError):self.client.call('get_delegation_family',{'family_id':'family','session_id':'other'})
            with self.assertRaises(BoardReadError):self.client.call('read_inbox',{})
            with self.assertRaises(BoardReadError):self.client.register()
            self.assertEqual(tool.call_count,1)

    def test_versions_digests_and_boundaries_are_strict(self):
        valid=dict(family_id='family',expected_revision=1,child_ticket_id='child',expected_child_version=2,
            result_sha256='a'*64,verification_evidence='controller audit',idempotency_key='fixed')
        self.assertEqual(_arguments('accept_delegation_result',valid),valid)
        for key,value in [('expected_revision',True),('expected_child_version',0),('result_sha256','invalid'),
                          ('verification_evidence','x'*20001),('idempotency_key','')]:
            with self.subTest(key=key),self.assertRaises(BoardReadError):
                _arguments('accept_delegation_result',{**valid,key:value})


if __name__=='__main__':unittest.main()
