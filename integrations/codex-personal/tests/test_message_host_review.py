"""Independent message-host regressions with temporary Board and fake inference."""
import unittest
from unittest.mock import patch

import test_message_host as fixture
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_config import HostError
from blackoutburst_agents.message_host import MessageHostSupervisor


class MessageHostReviewTests(unittest.TestCase):
    # Reuse ordinary setup/helpers without collecting the author's test methods.
    setUp=fixture.MessageHostTests.setUp
    resources=fixture.MessageHostTests.resources
    transport=fixture.MessageHostTests.transport
    post=fixture.MessageHostTests.post
    enable=fixture.MessageHostTests.enable
    tick_host=fixture.MessageHostTests.tick_host
    replies=fixture.MessageHostTests.replies
    operation=fixture.MessageHostTests.operation

    def ticket_target(self):
        self.board.update_project(self.human,self.project['id'],self.project['version'],context='PROJECT_SECRET_REVOKED')
        self.host.close()
        self.config['targets'][0]['thread_ids']=[self.ticket['thread_id']]
        self.host=HostJournal(self.root/'ticket-host');self.addCleanup(self.host.close);self.host.configure(self.config)
        self.supervisor=MessageHostSupervisor(self.host,resource_factory=self.resources)
        self.post({'id':self.ticket['thread_id']})

    def test_prepared_context_cannot_be_sent_after_linked_project_access_withdrawal(self):
        self.ticket_target();self.enable()
        with patch.object(self.message_runs,'create_work',side_effect=SystemExit('ordinary interruption')):
            with self.assertRaises(SystemExit):self.tick_host()
        self.assertEqual(self.operation()['state'],'prepared')
        self.assertIn('PROJECT_SECRET_REVOKED',self.operation()['payload']['prompt'])
        with self.board._tx() as db:
            db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',(self.project['id'],'owner'))
        result=self.tick_host()
        self.assertEqual(self.calls,0,result)
        self.assertEqual(result['status'],'needs_review',result)
        self.assertFalse(any(name=='post_message' for name,_ in self.client.calls))
        self.assertFalse(any(name=='checkpoint_message_feed' for name,_ in self.client.calls))

    def test_context_acl_is_checked_after_runtime_admission_before_transport(self):
        self.ticket_target();self.enable();original=self.control.call
        def admit_then_withdraw(name,args):
            response=original(name,args)
            if name=='start_execution':
                with self.board._tx() as db:
                    db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',(self.project['id'],'owner'))
            return response
        with patch.object(self.control,'call',side_effect=admit_then_withdraw):result=self.tick_host()
        self.assertEqual(self.calls,0,result)
        self.assertFalse(any(name=='post_message' for name,_ in self.client.calls))

    def test_historical_context_edit_after_inference_prevents_stale_reply_publication(self):
        history=self.board.post_message(self.human,self.thread['id'],'Historical private context',idempotency_key='historical')
        self.post();self.enable()
        def change_history(callbacks):
            with self.board._tx() as db:db.execute('UPDATE messages SET body=? WHERE id=?',('Historical context changed',history['id']))
        self.during=change_history
        result=self.tick_host()
        self.assertEqual(self.calls,1)
        self.assertEqual(result['status'],'needs_review',result)
        self.assertEqual(self.replies(),[])
        self.assertFalse(any(name=='checkpoint_message_feed' for name,_ in self.client.calls))

    def test_foreign_message_runtime_receipt_never_starts_inference_or_acknowledges(self):
        self.post();self.enable();original=self.control.call
        def wrong_scope(name,args):
            response=original(name,args)
            if name=='reserve_message_execution':
                response={**response,'execution':{**response['execution'],'source_sha256':'0'*64}}
            return response
        with patch.object(self.control,'call',side_effect=wrong_scope):result=self.tick_host()
        self.assertEqual(self.calls,0,result)
        self.assertFalse(any(name=='post_message' for name,_ in self.client.calls))
        self.assertFalse(any(name=='checkpoint_message_feed' for name,_ in self.client.calls))
        self.assertEqual(self.board.get_runtime(self.human)['active_count'],1)

    def test_feed_receipts_refuse_boolean_limit_and_integer_acknowledgement(self):
        reader=self.messages.reader('strict-receipts','a'*64,'now',1)
        opened=dict(feed_id='b'*32,start_at='now',limit=1,human_only=True,checkpoint=0)
        with self.assertRaises(HostError):self.messages.bind_feed(reader['id'],{**opened,'limit':True})
        self.messages.bind_feed(reader['id'],opened)
        delivery=self.messages.ingest(reader['id'],dict(feed_id='b'*32,delivery_id='c'*32,
            checkpoint=0,messages=[],human_only=True),[])
        self.messages.begin_ack(delivery['id'])
        ack=dict(feed_id='b'*32,delivery_id='c'*32,checkpoint=1,acknowledged=True)
        with self.assertRaises(HostError):self.messages.finish_ack(delivery['id'],{**ack,'acknowledged':1})
        self.assertEqual(self.messages.pending(reader['id'])['state'],'acking')
        self.assertEqual(self.messages.finish_ack(delivery['id'],ack)['state'],'acked')

    def test_live_context_acl_withdrawal_triggers_periodic_transport_cancellation(self):
        self.ticket_target();self.enable();clock=[0.0]
        def withdraw(callbacks):
            with self.board._tx() as db:
                db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',(self.project['id'],'owner'))
            clock[0]=6.0
            self.assertTrue(callbacks['should_cancel']())
        self.during=withdraw
        with patch('blackoutburst_agents.message_worker.time.monotonic',side_effect=lambda:clock[0]):
            result=self.tick_host()
        self.assertEqual(self.calls,1,result)
        self.assertEqual(result['status'],'needs_review',result)
        self.assertFalse(any(name=='post_message' for name,_ in self.client.calls))


if __name__=='__main__':unittest.main()
