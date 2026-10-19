"""One-use observed context sources keep the existing composition semantics."""
from copy import deepcopy
import unittest

from test_context import BoardFixture,registration
from blackoutburst_agents.context import (ContextError,ContextLimits,ContextScope,
    build_context,build_context_from_reads,read_context_pages)


class ContextReadFrameTests(unittest.TestCase):
    def setUp(self):
        self.scope=ContextScope('alice','agent-a','project-a','ticket-a')
        self.board=BoardFixture(count=45)
        self.registration=registration()
        self.limits=ContextLimits(max_messages=60,page_size=20,max_pages=3)

    def collect(self):
        first=self.board('get_ticket',{'ticket_id':self.scope.ticket_id})
        pages=read_context_pages(self.board,scope=self.scope,first_response=first,limits=self.limits)
        last=self.board('get_ticket',{'ticket_id':self.scope.ticket_id})
        return first,pages,last

    def compose(self,first,pages,last):
        return build_context_from_reads(scope=self.scope,registration=self.registration,
            first_response=first,pages=pages,final_response=last,limits=self.limits)

    def test_three_actual_pages_produce_the_identical_context_without_new_reads(self):
        first,pages,last=self.collect();before=len(self.board.calls)
        actual=self.compose(first,pages,last)
        self.assertEqual(len(self.board.calls),before)
        self.assertEqual(len(pages),3)
        expected=build_context(self.board,scope=self.scope,registration=self.registration,limits=self.limits)
        self.assertEqual(actual,expected)
        self.assertEqual(actual['sources']['history']['selected_count'],45)

    def test_page_keys_order_cursor_extra_and_missing_are_refused(self):
        first,pages,last=self.collect()
        alternatives=[pages[::-1],pages[:-1],pages+[deepcopy(pages[-1])],
                      [{**pages[0],'unexpected':True},*pages[1:]],
                      [{**pages[0],'arguments':{**pages[0]['arguments'],'limit':True}},*pages[1:]],
                      [pages[0],{**pages[1],'arguments':{**pages[1]['arguments'],'before':'wrong'}},pages[2]]]
        for changed in alternatives:
            with self.subTest(changed=changed),self.assertRaises(ContextError):self.compose(first,changed,last)

    def test_ticket_project_versions_and_scope_differences_refused(self):
        first,pages,last=self.collect()
        for target,key,value in [('ticket','version',3),('project','version',2),
                                 ('ticket','thread_id','different'),('project','participants',['bob'])]:
            changed=deepcopy(last);changed[target][key]=value
            with self.subTest(target=target,key=key),self.assertRaises(ContextError):self.compose(first,pages,changed)

    def test_real_pagination_loop_and_oversized_page_are_refused_during_read(self):
        first=self.board('get_ticket',{'ticket_id':self.scope.ticket_id})
        def bad(response,args):
            response['messages']=response['messages']*2
            return response
        self.board.transform_page=bad
        with self.assertRaises(ContextError):
            read_context_pages(self.board,scope=self.scope,first_response=first,limits=self.limits)
        self.assertEqual(len(self.board.calls),2)

    def test_returned_context_is_detached_from_observed_mutable_inputs(self):
        first,pages,last=self.collect();actual=self.compose(first,pages,last);saved=deepcopy(actual)
        first['ticket']['title']='after capture'
        pages[0]['response']['messages'].clear()
        last['project']['description']='after capture'
        self.assertEqual(actual,saved)
