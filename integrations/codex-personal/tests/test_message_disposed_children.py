"""Real two-child journals/Board fixture, no provider or account operations."""
from copy import deepcopy
import json
import unittest
import uuid
from unittest.mock import patch

import test_message_local_abandonment as legacy
from blackoutburst_agents.host_config import HostError
from blackoutburst_agents.message_disposed_children import verify_disposed_message_children
from blackoutburst_agents.message_executed_disposition import MessageExecutedDisposition


class TwoDisposedChildrenTests(unittest.TestCase):
    resources=legacy.MessageLocalAbandonmentTests.resources
    post=legacy.MessageLocalAbandonmentTests.post
    tick_host=legacy.MessageLocalAbandonmentTests.tick_host
    locked=legacy.MessageLocalAbandonmentTests.locked
    remote_rows=legacy.MessageLocalAbandonmentTests.remote_rows

    def transport(self,*args,**callbacks):
        self.calls+=1
        callbacks['on_started'](999999999)
        session=str(uuid.uuid4());callbacks['on_session'](session)
        return {'status':'blocked','reason':'protocol_error','session_id':session,'text':None,
            'verification':{'cleanup_confirmed':True,'exit_zero':False,'session_verified':True,
                'tool_free':True,'turn_completed':False}}

    def setUp(self):
        legacy.MessageLocalAbandonmentTests.setUp(self)
        with self.locked():
            self.legacy_plan=legacy.MessageLocalAbandonmentTests.plan(self)
            self.legacy_result=self.admin.resolve(self.legacy_plan)
        limits=self.board.get_runtime(self.human)['limits']
        # Preserve the separate old global Runtime; merely give the synthetic
        # next message a second slot. This never writes a real account.
        self.board.update_runtime_limits(self.human,limits['version'],2,False)
        with self.locked():
            self.host.queue_managed_command({'id':'fixture-next-start','target_id':'primary',
                'instance_id':'fixture-instance','generation':2,'action':'start',
                'launchspec_sha256':self.catalog[0]['launchspec_sha256'],'budget':1})
            self.host.apply_managed_command('fixture-next-start')
        self.assertEqual(self.tick_host()['status'],'idle')  # Genuine ACK of the first delivery.
        for _ in range(2):self.assertEqual(self.tick_host()['status'],'idle')  # Normal empty feed ACKs.
        self.post();failed=self.tick_host()
        self.assertEqual(failed['status'],'needs_review',failed)
        self.assertEqual(self.calls,1)
        with self.locked():self.host.stop()
        old_identity=self.identity
        self.identity=dict(old_identity)
        fixture=self
        class Observer:
            endpoint='https://board.test/mcp'
            session_id=fixture.client.session_id
            def __init__(self):self.calls=[]
            def call(self,name,args):
                if name not in ('lookup_execution','get_execution_control'):raise AssertionError('read only')
                self.calls.append((name,deepcopy(args)))
                return getattr(fixture.board,name)({**fixture.raw,'session_id':self.session_id},**args)
        self.observer=Observer()
        self.disposition=MessageExecutedDisposition(self.host,self.messages,self.message_runs,
            self.message_runtime,observer=self.observer)
        new=self.messages.db.execute('SELECT id FROM operations WHERE message_id=?',(self.source['id'],)).fetchone()
        op=self.messages.operation(new['id'])
        turn=self.message_runs._db.execute('SELECT id FROM turns WHERE work_id=?',(op['work_id'],)).fetchone()[0]
        dispatch=next(d for d in self.host.dispatches() if d['batch_id']==op['id'])
        with self.locked():
            self.executed_plan=self.disposition.propose(request_key='dispose-executed-after-sealed',target_id='primary',
                batch_id=op['id'],worker_id=dispatch['worker_id'],work_id=op['work_id'],turn_id=turn,
                identity=self.identity,expected_config_sha256=self.host.control()['hash'],
                expected_host_instance=self.host.control()['instance_id'])
            self.executed_result=self.disposition.apply(self.executed_plan)
            self.initial=self.verify()
        self.assertGreater(self.executed_plan['reader']['checkpoint'],self.legacy_plan['reader']['checkpoint']+1)
        self.before_calls=(self.calls,deepcopy(self.seal.calls),deepcopy(self.observer.calls))

    def verify(self):
        return verify_disposed_message_children(self.host,self.messages,self.message_runs,self.message_runtime,
            abandonment_intent_id=self.legacy_result['intent_id'],executed_intent_id=self.executed_result['intent_id'])

    def snapshot(self):
        return [tuple(db.iterdump()) for db in (self.host._db,self.messages.db,self.message_runs._db,self.message_runtime.db)]

    def test_real_roster_has_two_distinct_proofs_and_stays_stable_after_normal_ack(self):
        with self.locked():
            before=self.snapshot();self.assertEqual(self.verify(),self.initial);self.assertEqual(self.snapshot(),before)
        self.assertEqual(set(self.initial),{'schema','roster_sha256'})
        self.assertEqual(self.initial['schema'],'message-disposed-children-v1')
        self.assertEqual(len(self.initial['roster_sha256']),64)
        p=self.executed_plan;delivery=self.messages.delivery(p['delivery']['id'])
        self.supervisor._ack(self.messages,self.client,delivery)
        with self.locked():self.assertEqual(self.verify(),self.initial)
        self.assertEqual(self.messages.operation(self.legacy_plan['message']['row']['id'])['state'],'abandoned')
        self.assertEqual(self.messages.operation(p['message']['row']['id'])['state'],'executed_disposed')
        self.assertEqual(self.before_calls,(self.calls,self.seal.calls,self.observer.calls))
        rows=self.remote_rows()
        self.assertEqual(rows[0],self.old_runtime[0])

    def test_legitimate_host_catalogue_revision_outbox_changes_are_not_child_authority(self):
        with self.locked():
            self.host._db.execute("INSERT INTO managed_effects VALUES(?,?,?,?, 'pending','{}')",
                ('synthetic-transition','catalog_source_transition','{}','0'*64))
            self.host._db.execute('UPDATE managed_settings SET catalog_sha256=?',('d'*64,))
            self.host._db.execute('UPDATE managed_targets SET launchspec_sha256=?',('e'*64,))
            self.host.stop()
            self.assertEqual(self.verify(),self.initial)
        self.assertEqual(self.before_calls,(self.calls,self.seal.calls,self.observer.calls))

    def test_any_extra_child_or_dispatch_or_reader_is_refused(self):
        cases=[(self.message_runs._db,'works','id',"INSERT INTO works SELECT ?,owner_id,agent_id,project_id,ticket_id,workspace,executable,model,NULL,'closed',created_at,updated_at FROM works LIMIT 1"),
            (self.message_runs._db,'turns','id',"INSERT INTO turns SELECT ?,work_id,'third-request',prompt_sha256,status,pid,provider_session_id,cancel_requested,requires_review,result_json,resolution,resolution_reason,created_at,updated_at FROM turns LIMIT 1"),
            (self.message_runtime.db,'executions','id',"INSERT INTO executions SELECT ?,'third-key',configuration,phase,receipt,revision,start_confirmed,created_at,updated_at FROM executions LIMIT 1"),
            (self.messages.db,'operations','id',"INSERT INTO operations SELECT ?,delivery_id,'third-message',thread_id,message_hash,worker_id,work_id,state,revision,payload_json FROM operations LIMIT 1"),
            (self.host._db,'dispatches','batch_id',"INSERT INTO dispatches SELECT ?,target_id,request_id,subscription_id,worker_id,state,receipt,activation FROM dispatches LIMIT 1"),
            (self.messages.db,'readers','id',"INSERT INTO readers SELECT ?,'third-target',configuration_hash,'third-opening',start_at,page_limit,'',0 FROM readers LIMIT 1")]
        for db,table,column,sql in cases:
            with self.subTest(table=table),self.locked():
                key=str(uuid.uuid4());db.execute(sql,(key,));before=self.snapshot()
                try:
                    with self.assertRaises(HostError):self.verify()
                    self.assertEqual(self.snapshot(),before)
                finally:db.execute('DELETE FROM '+table+' WHERE '+column+'=?',(key,))

    def test_mutating_either_child_or_legacy_ack_is_not_adopted_without_signals(self):
        message_id=self.executed_plan['message']['row']['id']
        payload=deepcopy(self.messages.operation(message_id)['payload'])
        payload['disposition']['proof_sha256']='0'*64
        for db,table,column,key,value in (
                (self.message_runs._db,'turns','result_json',self.executed_plan['turn']['id'],'{}'),
                (self.message_runs._db,'turns','requires_review',self.executed_plan['turn']['id'],0),
                (self.message_runs._db,'turns','pid',self.legacy_plan['turn']['id'],123456),
                (self.messages.db,'deliveries','ack_json',self.legacy_plan['delivery']['id'],'{}'),
                (self.messages.db,'operations','payload_json',message_id,json.dumps(payload))):
            with self.subTest(column=column),self.locked():
                saved=db.execute('SELECT '+column+' FROM '+table+' WHERE id=?',(key,)).fetchone()[0]
                db.execute('UPDATE '+table+' SET '+column+'=? WHERE id=?',(value,key));before=self.snapshot()
                try:
                    with patch('os.kill',side_effect=AssertionError('no PID probes')),patch('os.killpg',side_effect=AssertionError('no signals')):
                        with self.assertRaises(HostError):self.verify()
                    self.assertEqual(self.snapshot(),before)
                finally:db.execute('UPDATE '+table+' SET '+column+'=? WHERE id=?',(saved,key))

    def test_owner_lock_stopped_targets_and_no_caller_transaction_are_required(self):
        with self.assertRaises(HostError):self.verify()
        with self.locked():
            self.host._db.execute('UPDATE managed_targets SET enabled=1')
            try:
                with self.assertRaises(HostError):self.verify()
            finally:self.host._db.execute('UPDATE managed_targets SET enabled=0')
            for db in (self.host._db,self.messages.db,self.message_runs._db,self.message_runtime.db):
                db.execute('BEGIN IMMEDIATE')
                try:
                    with self.assertRaisesRegex(HostError,'message_disposed_children_transaction_active'):self.verify()
                    self.assertTrue(db.in_transaction)
                finally:db.execute('ROLLBACK')


if __name__=='__main__':unittest.main()
