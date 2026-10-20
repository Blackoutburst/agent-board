"""Consumed closure guard with real journals and no client/provider use."""
from copy import deepcopy
import json
import unittest
import uuid
from unittest.mock import patch

import test_message_local_abandonment as fixture
from blackoutburst_agents.host_config import HostError,digest
from blackoutburst_agents.message_abandoned_children import verify_completed_message_children


class CompletedMessageChildrenTests(unittest.TestCase):
    resources=fixture.MessageLocalAbandonmentTests.resources
    post=fixture.MessageLocalAbandonmentTests.post
    tick_host=fixture.MessageLocalAbandonmentTests.tick_host
    operation=fixture.MessageLocalAbandonmentTests.operation
    transport=fixture.MessageLocalAbandonmentTests.transport
    locked=fixture.MessageLocalAbandonmentTests.locked
    remote_rows=fixture.MessageLocalAbandonmentTests.remote_rows
    plan=fixture.MessageLocalAbandonmentTests.plan
    state=fixture.MessageLocalAbandonmentTests.state

    def setUp(self):
        fixture.MessageLocalAbandonmentTests.setUp(self)
        with self.locked():
            self.proposal=self.plan();self.resolved=self.admin.resolve(self.proposal)
            self.initial=self.verify()
        self.initial_calls=deepcopy(self.seal.calls)

    def verify(self):
        return verify_completed_message_children(self.host,self.messages,self.message_runs,self.message_runtime,
            intent_id=self.resolved['intent_id'])

    def test_same_proof_after_pending_catalogue_intent_stop_and_catalogue_changes(self):
        with self.locked():
            args={'schema':'synthetic-transition-only'}
            self.host._db.execute("INSERT INTO managed_effects VALUES(?,?,?,?,\'pending\',\'{}\')",
                ('synthetic-local-intent','catalog_source_transition',json.dumps(args),digest(args)))
            with self.assertRaises(HostError):self.admin.status(intent_id=self.resolved['intent_id'])
            self.assertEqual(self.verify(),self.initial)
            self.host.queue_managed_command({'id':'genuine-stop-fixture','target_id':'primary',
                'instance_id':'fixture-instance','generation':2,'action':'stop',
                'launchspec_sha256':self.catalog[0]['launchspec_sha256'],'budget':0})
            self.host.apply_managed_command('genuine-stop-fixture')
            self.assertEqual(self.verify(),self.initial)
            self.host._db.execute("UPDATE managed_settings SET catalog_sha256=?",('c'*64,))
            self.host._db.execute("UPDATE managed_targets SET launchspec_sha256=?",('d'*64,))
            self.assertEqual(self.verify(),self.initial)
        self.assertEqual(self.seal.calls,self.initial_calls);self.assertEqual(self.calls,0)
        self.assertEqual(self.remote_rows(),self.old_runtime)

    def test_output_closed_and_four_databases_unchanged(self):
        paths=(self.host.directory/'host.sqlite3',self.messages.directory/'messages.sqlite3',
            self.message_runs.state_dir/'runs.sqlite3',self.message_runtime.directory/'runtime.sqlite3')
        with self.locked():
            before=[p.read_bytes() for p in paths];value=self.verify()
            self.assertEqual([p.read_bytes() for p in paths],before)
        self.assertEqual(set(value),{'schema','intent_id','proof_sha256','children_sha256'})
        text=json.dumps(value)
        for sentinel in ('PRIVATE','token','runtime-turn-',self.proposal['work']['id'],self.proposal['turn']['id']):
            self.assertNotIn(sentinel,text)
        self.assertEqual(self.seal.calls,self.initial_calls)

    def test_normal_ack_retains_same_terminal_child_proof(self):
        p=self.proposal
        delivery=self.messages.begin_ack(p['delivery']['id'])
        self.messages.finish_ack(delivery['id'],{'feed_id':p['reader']['feed_id'],
            'delivery_id':p['delivery']['remote_id'],'checkpoint':p['reader']['checkpoint']+1,'acknowledged':True})
        with self.locked():self.assertEqual(self.verify(),self.initial)
        self.assertEqual(self.operation()['state'],'abandoned')

    def test_any_other_work_turn_runtime_or_operation_refused(self):
        for kind in ('work','turn','runtime','operation'):
            with self.subTest(kind=kind):
                if kind!='work':self.doCleanups();self.setUp()
                if kind=='work':
                    self.message_runs._db.execute("INSERT INTO works SELECT ?,owner_id,agent_id,project_id,ticket_id,workspace,executable,model,NULL,'closed',created_at,updated_at FROM works",(str(uuid.uuid4()),))
                elif kind=='turn':
                    self.message_runs._db.execute("INSERT INTO turns SELECT ?,work_id,?,prompt_sha256,status,pid,provider_session_id,cancel_requested,requires_review,result_json,resolution,resolution_reason,created_at,updated_at FROM turns",(str(uuid.uuid4()),'another-request'))
                elif kind=='runtime':
                    self.message_runtime.db.execute("INSERT INTO executions SELECT ?,?,configuration,phase,receipt,revision,start_confirmed,created_at,updated_at FROM executions",(str(uuid.uuid4()),'other-runtime-key'))
                else:
                    self.messages.db.execute("INSERT INTO operations SELECT ?,delivery_id,?,thread_id,message_hash,worker_id,work_id,state,revision,payload_json FROM operations",(str(uuid.uuid4()),'other-message'))
                before=self.state()
                with self.locked(),self.assertRaises(HostError):self.verify()
                self.assertEqual(self.state(),before);self.assertEqual(self.seal.calls,self.initial_calls)

    def test_pid_provider_payload_and_checkpoint_mutations_refuse_without_signals(self):
        for kind in ('pid','provider','payload','checkpoint'):
            with self.subTest(kind=kind):
                if kind!='pid':self.doCleanups();self.setUp()
                if kind=='pid':self.message_runs._db.execute("UPDATE turns SET pid=999999999")
                elif kind=='provider':self.message_runs._db.execute("UPDATE works SET provider_session_id='synthetic-session'")
                elif kind=='payload':self.messages.db.execute("UPDATE operations SET payload_json=json_set(payload_json,'$.prompt','PRIVATE_CHANGED')")
                else:self.host._db.execute("UPDATE host_message_abandonments SET checkpoints_json=json_set(checkpoints_json,'$.runtime',?)",('f'*64,))
                before=self.state()
                with self.locked(),patch('os.kill',side_effect=AssertionError('no signals')),patch('os.killpg',side_effect=AssertionError('no signals')):
                    with self.assertRaises(HostError):self.verify()
                self.assertEqual(self.state(),before);self.assertEqual(self.seal.calls,self.initial_calls)

    def test_owner_lock_and_all_targets_stopped_required(self):
        with self.assertRaises(HostError):self.verify()
        for table in ('control','managed_targets'):
            with self.locked():
                self.host._db.execute('UPDATE '+table+' SET enabled=1')
                try:
                    with self.assertRaises(HostError):self.verify()
                finally:self.host._db.execute('UPDATE '+table+' SET enabled=0')

    def test_changed_host_instance_configuration_or_identity_is_refused(self):
        for column,value in (('instance_id',str(uuid.uuid4())),('hash','e'*64)):
            with self.locked():
                old=self.host._db.execute('SELECT '+column+' FROM control').fetchone()[0]
                self.host._db.execute('UPDATE control SET '+column+'=?',(value,))
                try:
                    with self.assertRaises(HostError):self.verify()
                finally:self.host._db.execute('UPDATE control SET '+column+'=?',(old,))

    def test_unsettled_or_tampered_terminal_proof_is_not_adopted(self):
        for column,sql in (('phase',"UPDATE host_message_abandonments SET phase='message_abandoned'"),
                ('proof_json',"UPDATE host_message_abandonments SET proof_json=json_set(proof_json,'$.owner_id','foreign')")):
            with self.locked():
                old=self.host._db.execute('SELECT '+column+' FROM host_message_abandonments').fetchone()[0]
                self.host._db.execute(sql)
                try:
                    with self.assertRaises(HostError):self.verify()
                finally:self.host._db.execute('UPDATE host_message_abandonments SET '+column+'=?',(old,))
        self.assertEqual(self.seal.calls,self.initial_calls)

    def test_existing_caller_transaction_is_not_rolled_back_or_adopted(self):
        for db in (self.host._db,self.messages.db,self.message_runs._db,self.message_runtime.db):
            with self.subTest(database=id(db)),self.locked():
                db.execute('BEGIN IMMEDIATE')
                try:
                    with self.assertRaisesRegex(HostError,'message_abandon_children_transaction_active'):self.verify()
                    self.assertTrue(db.in_transaction)
                finally:db.execute('ROLLBACK')
