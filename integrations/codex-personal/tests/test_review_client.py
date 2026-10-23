"""Reviewer registration is explicit; the dispatch channel has no mutations."""
from copy import deepcopy
import unittest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.review_client import DeliveryReviewBoardClient


class ReviewClientTests(unittest.TestCase):
    def client(self):
        value=DeliveryReviewBoardClient(dict(mcp_url='https://board.test/mcp',token='SYNTHETIC_TOKEN',
            client='codex',identity_key='reviewer',name='Reviewer'),'review-session',3)
        value._initialize=lambda:None;value.calls=[]
        def tool(name,args):
            value.calls.append((name,deepcopy(args)))
            return {'session':{'id':value.session_id}} if name=='register_agent' else {'ticket':{'id':'ticket'}}
        value._tool=tool
        return value

    def test_registration_is_explicit_with_review_skills_and_non_autonomous(self):
        value=self.client()
        with self.assertRaises(BoardReadError):value.register()
        with self.assertRaises(BoardReadError):value.call('get_ticket',{'ticket_id':'ticket'})
        self.assertEqual(value.calls,[])
        registration=value.register_initial(skills=['review','python'])
        args=value.calls[0][1]
        self.assertEqual(args['skills'],['review','python']);self.assertIs(args['autonomous'],False)
        self.assertEqual(value.register(),registration);self.assertEqual(len(value.calls),1)
        value.register()['session']['id']='another'
        self.assertEqual(value.register(),registration)
        with self.assertRaises(BoardReadError):value.register_initial(skills=['review'])

    def test_pinned_dispatch_never_registers_and_rejects_mutations(self):
        value=self.client();value.pin_registration({'session':{'id':value.session_id}})
        self.assertEqual(value.call('get_ticket',{'ticket_id':'ticket'}),{'ticket':{'id':'ticket'}})
        for name in ('claim_ticket','claim_next_ticket','renew_ticket','update_ticket','release_ticket','post_message','register_agent','create_ticket'):
            with self.subTest(name=name),self.assertRaises(BoardReadError):value.call(name,{})
        self.assertEqual([name for name,_ in value.calls],['get_ticket'])

    def test_invalid_skills_refuse_without_transport(self):
        for skills in ([],['python'],['review','review'],['review',{}],'review'):
            with self.subTest(skills=skills):
                value=self.client()
                with self.assertRaises(BoardReadError):value.register_initial(skills=skills)
                self.assertEqual(value.calls,[])
