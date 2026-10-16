"""Local feed replay/refusal/limits plus narrow-client preflight; no networking."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_config import HostError, validate
from blackoutburst_agents.message_client import MessageBoardClient
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.run_store import RunStore, StoreError
from test_host_journal import configuration


class MessageStateTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve();self.journal=MessageJournal(self.root/'messages');self.addCleanup(self.journal.close)
        self.reader=self.journal.reader('primary','a'*64,'now',50)
        self.journal.bind_feed(self.reader['id'],{'feed_id':'b'*32,'start_at':'now','limit':50,'human_only':True,'checkpoint':0})

    def delivery(self,ids=('one',)):
        response={'feed_id':'b'*32,'delivery_id':'c'*32,'checkpoint':0,'messages':[{} for _ in ids],'human_only':True}
        messages=[{'message_id':value,'thread_id':'thread','message_hash':'d'*64} for value in ids]
        return self.journal.ingest(self.reader['id'],response,messages)

    def test_opening_key_and_pending_operation_ids_survive_restart(self):
        delivery=self.delivery();before=delivery['operations'][0]
        self.journal.close();self.journal=MessageJournal(self.root/'messages');self.addCleanup(self.journal.close)
        self.assertEqual(self.journal.reader('primary','a'*64,'now',50)['opening_key'],self.reader['opening_key'])
        again=self.delivery();self.assertEqual(again['id'],delivery['id'])
        self.assertEqual(again['operations'][0],before)

    def test_changed_opening_bounds_or_scope_do_not_reopen(self):
        for hashed,start,limit in [('f'*64,'now',50),('a'*64,'beginning',50),('a'*64,'now',51)]:
            with self.assertRaises(HostError):self.journal.reader('primary',hashed,start,limit)
        self.assertEqual(self.journal.reader('primary','a'*64,'now',50)['checkpoint'],0)

    def test_changed_delivery_or_second_pending_never_overwrites(self):
        original=self.delivery()
        changed={'feed_id':'b'*32,'delivery_id':'c'*32,'checkpoint':0,'messages':[{}],'human_only':True}
        with self.assertRaises(HostError):self.journal.ingest(self.reader['id'],changed,[{'message_id':'other','thread_id':'thread','message_hash':'d'*64}])
        changed['delivery_id']='e'*32
        with self.assertRaises(HostError):self.journal.ingest(self.reader['id'],changed,[])
        self.assertEqual(self.journal.pending(self.reader['id']),original)

    def test_ack_cannot_skip_unfinished_message_or_accept_wrong_receipt(self):
        delivery=self.delivery();op=delivery['operations'][0]
        with self.assertRaises(HostError):self.journal.begin_ack(delivery['id'])
        self.journal.update(op['id'],op['revision'],'ignored',reason='thread_not_selected')
        self.journal.begin_ack(delivery['id'])
        for ack in [{'feed_id':'other','delivery_id':'c'*32,'checkpoint':1,'acknowledged':True},
                    {'feed_id':'b'*32,'delivery_id':'c'*32,'checkpoint':True,'acknowledged':True}]:
            with self.assertRaises(HostError):self.journal.finish_ack(delivery['id'],ack)
        self.assertEqual(self.journal.pending(self.reader['id'])['state'],'acking')
        ack={'feed_id':'b'*32,'delivery_id':'c'*32,'checkpoint':1,'acknowledged':True}
        self.journal.finish_ack(delivery['id'],ack)
        self.assertEqual(self.journal.finish_ack(delivery['id'],ack)['state'],'acked')

    def test_thousand_confirmed_empty_pages_keep_one_receipt(self):
        for checkpoint in range(1000):
            remote_id=format(checkpoint,'032x')
            response={'feed_id':'b'*32,'delivery_id':remote_id,'checkpoint':checkpoint,'messages':[],'human_only':True}
            delivery=self.journal.ingest(self.reader['id'],response,[])
            self.journal.begin_ack(delivery['id'])
            self.journal.finish_ack(delivery['id'],{'feed_id':'b'*32,'delivery_id':remote_id,
                'checkpoint':checkpoint+1,'acknowledged':True})
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0],1)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM operations').fetchone()[0],0)
        self.assertEqual(self.journal.db.execute('SELECT checkpoint FROM readers').fetchone()[0],1000)

    def test_worker_binding_cannot_adopt_another_uuid(self):
        op=self.delivery()['operations'][0];first=str(uuid.uuid4())
        self.journal.bind_worker(op['id'],first)
        with self.assertRaises(HostError):self.journal.bind_worker(op['id'],str(uuid.uuid4()))
        self.assertEqual(self.journal.operation(op['id'])['worker_id'],first)

    def test_capacity_refusal_rolls_back_complete_page(self):
        with patch('blackoutburst_agents.message_journal.MAX_OPERATIONS',0):
            with self.assertRaises(HostError):self.delivery()
        self.assertIsNone(self.journal.pending(self.reader['id']))
        self.assertEqual(self.journal.db.execute('SELECT checkpoint FROM readers').fetchone()[0],0)

    def test_closed_preallocated_work_cannot_be_recreated(self):
        with self._store() as runs:
            import sys
            args=dict(owner_id='owner',agent_id='board-message-namespace',project_id='thread',ticket_id='message',
                workspace=self.root,executable=sys.executable,model='fixture',work_id=str(uuid.uuid4()))
            work=runs.create_work(**args)
            runs._db.execute("UPDATE works SET status='closed' WHERE id=?",(work['id'],))
            with self.assertRaises(StoreError):runs.create_work(**args)

    def _store(self):
        from contextlib import contextmanager
        @contextmanager
        def store():
            value=RunStore(self.root/'runs')
            try:yield value
            finally:value.close()
        return store()


class MessageClientTests(unittest.TestCase):
    def setUp(self):
        self.client=MessageBoardClient({'mcp_url':'https://board.test/mcp','token':'synthetic',
            'client':'codex','identity_key':'synthetic','name':'Synthetic'},'session',3)

    def test_unapproved_tools_and_injected_scope_refused_before_registration(self):
        with patch.object(self.client,'register') as register:
            for name,args in [('claim_ticket',{'ticket_id':'x'}),('post_message',{'thread_id':'t','body':'answer','reply_to':'m','idempotency_key':'k','session_id':'foreign'}),
                              ('checkpoint_message_feed',{'feed_id':'a'*32,'delivery_id':'b'*32,'expected_checkpoint':True})]:
                with self.assertRaises(BoardReadError):self.client.call(name,args)
            register.assert_not_called()

    def test_unicode_reply_wire_bound_is_checked_before_network(self):
        with patch.object(self.client,'register') as register:
            with self.assertRaises(BoardReadError):self.client.call('post_message',{
                'thread_id':'t','body':'😀'*2001,'reply_to':'m','idempotency_key':'k'})
            register.assert_not_called()


if __name__=='__main__':unittest.main()
