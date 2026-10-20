"""Exact temporary Board/four-journal fixture; synthetic transport, no real model."""
from copy import deepcopy
import json
import sqlite3
import unittest
import uuid
from unittest.mock import patch

import test_message_host as fixture
from blackoutburst_agents.host_config import HostError,validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.message_host import MessageHostSupervisor
from blackoutburst_agents.server_controller import launch_catalog
from blackoutburst_agents.message_executed_disposition import MessageExecutedDisposition,PHASES,TABLE,hashed,proposal,encoded


class MemoryObserver:
    endpoint='https://board.test/mcp'
    def __init__(self,fixture):self.fixture=fixture;self.session_id=fixture.client.session_id;self.calls=[];self.failure=None;self.mutate=None
    def call(self,name,args):
        if name not in ('lookup_execution','get_execution_control'):raise AssertionError('only read-only Runtime calls')
        f=self.fixture
        if not f.host._held or not f.message_runs._locks or any(db.in_transaction for db in (f.host._db,f.messages.db,f.message_runs._db,f.message_runtime.db)):
            raise AssertionError('held locks and no SQL transaction')
        record=f.host._db.execute('SELECT phase FROM '+TABLE).fetchone()
        if record is None or record[0]!='intent':raise AssertionError('durable intent before every observation')
        self.calls.append((name,deepcopy(args)))
        if self.failure==('before',name):raise TimeoutError('PRIVATE_OBSERVER_FAILURE')
        value=getattr(f.board,name)({**f.raw,'session_id':self.session_id},**args)
        if self.failure==('after',name):raise TimeoutError('PRIVATE_OBSERVER_FAILURE')
        return self.mutate(name,value) if self.mutate else value


class ExecutedDispositionTests(unittest.TestCase):
    resources=fixture.MessageHostTests.resources
    post=fixture.MessageHostTests.post
    tick_host=fixture.MessageHostTests.tick_host
    operation=fixture.MessageHostTests.operation

    def transport(self,*args,**callbacks):
        self.calls+=1
        callbacks['on_started'](999999999)
        session=str(uuid.uuid4());callbacks['on_session'](session)
        return {'status':'blocked','reason':'protocol_error','session_id':session,'text':None,
            'verification':{'cleanup_confirmed':True,'exit_zero':False,'session_verified':True,
                'tool_free':True,'turn_completed':False}}

    def setUp(self):
        fixture.MessageHostTests.setUp(self)
        self.messages.close();self.message_runs.close();self.message_runtime.close();self.host.close()
        self.config=deepcopy(self.config);self.config['targets'][0]['start_at']='now';self.config=validate(self.config)
        self.host=HostJournal(self.root/'managed-host');self.addCleanup(self.host.close);self.host.configure(self.config)
        self.messages=MessageJournal(self.host.directory/'messages');self.addCleanup(self.messages.close)
        self.message_runs=RunStore(self.host.directory/'message-runs');self.addCleanup(self.message_runs.close)
        self.message_runtime=RuntimeJournal(self.host.directory/'message-runtime');self.addCleanup(self.message_runtime.close)
        self.supervisor=MessageHostSupervisor(self.host,resource_factory=self.resources)
        self.catalog=launch_catalog(self.config,'b'*64)
        with self.locked():
            self.host.prepare_managed('fixture-host',self.catalog)
            self.host.bind_managed_instances({'primary':'fixture-instance'})
            self.host.queue_managed_command({'id':'fixture-start','target_id':'primary','instance_id':'fixture-instance',
                'generation':1,'action':'start','launchspec_sha256':self.catalog[0]['launchspec_sha256'],'budget':1})
            self.host.apply_managed_command('fixture-start')
        self.assertEqual(self.tick_host()['status'],'idle')
        self.post();self.failed=self.tick_host()
        self.assertEqual((self.failed['status'],self.failed['reason']),('needs_review','message_result_requires_review'))
        self.assertEqual(self.calls,1)
        with self.locked():self.host.stop()
        self.identity={'owner_id':'owner','agent_id':self.config['targets'][0]['agent_id'],
            'device_id':self.raw['device_id'],'profile_version':self.config['targets'][0]['profile_version'],
            'session_id':self.client.session_id}
        self.observer=MemoryObserver(self)
        self.admin=MessageExecutedDisposition(self.host,self.messages,self.message_runs,self.message_runtime,observer=self.observer)

    def locked(self):return self.host.owner_lock(lock_directory=self.root/'locks')
    def remote_rows(self):
        with self.board._connection() as db:return [tuple(r) for r in db.execute('SELECT * FROM runtime_executions')]

    def plan(self):
        d=self.host.dispatches()[0];op=self.operation()
        turn=self.message_runs._db.execute('SELECT id FROM turns WHERE work_id=?',(op['work_id'],)).fetchone()[0]
        return self.admin.propose(request_key='explicit-executed-disposition',target_id='primary',batch_id=d['batch_id'],
            worker_id=d['worker_id'],work_id=op['work_id'],turn_id=turn,identity=self.identity,
            expected_config_sha256=self.host.control()['hash'],expected_host_instance=self.host.control()['instance_id'])
    def state(self):
        return [[tuple(r) for r in db.execute('SELECT * FROM '+table)] for db,table in (
            (self.host._db,'control'),(self.host._db,'dispatches'),(self.host._db,'targets'),
            (self.messages.db,'operations'),(self.messages.db,'readers'),(self.messages.db,'deliveries'),
            (self.message_runs._db,'works'),(self.message_runs._db,'turns'),(self.message_runtime.db,'executions'))]
    def test_proposal_is_read_only_and_closed_before_observation(self):
        before=self.state()
        with self.locked():p=self.plan()
        self.assertEqual(before,self.state());self.assertEqual(self.observer.calls,[]);self.assertEqual(self.calls,1)
        self.assertEqual(proposal(p),p);self.assertEqual(p['turn']['requires_review'],1)
    def test_five_phases_finish_without_replay_publication_ack_or_runtime_write(self):
        remote=self.remote_rows()
        with self.locked():
            p=self.plan();before=deepcopy(p);control=self.host.control();result=self.admin.apply(p)
            self.assertEqual((result['phase'],result['revision']),('complete',5))
            self.assertEqual(self.admin.resume(result['intent_id'],expected_revision=5),result)
            record=self.host._executed_record(intent_id=result['intent_id'])
            self.assertEqual(set(record['checkpoints']),{'runtime','work','message','host'})
            self.assertEqual(set(result),{'intent_id','revision','phase','reason'})
            self.assertEqual(self.admin.proposal(p['request_key']),p)
            self.assertEqual(self.host.control(),control)
        work=self.message_runs.get_work(p['work']['id']);turn=self.message_runs._turn(p['turn']['id'])
        self.assertEqual(work['status'],'closed');self.assertEqual(turn['resolution'],'abandon')
        for k in ('result_json','pid','provider_session_id','requires_review','status','prompt_sha256','cancel_requested'):
            self.assertEqual(turn[k],p['turn'][k])
        self.assertEqual(work['provider_session_id'],p['work']['provider_session_id'])
        self.assertEqual(self.message_runtime.get(p['runtime']['id']),p['runtime']);self.assertEqual(self.remote_rows(),remote)
        operation=self.operation();self.assertEqual(operation['state'],'executed_disposed')
        payload=deepcopy(operation['payload']);marker=payload.pop('disposition');self.assertEqual(hashed(payload),p['message']['payload_sha256'])
        self.assertEqual(marker['proof_sha256'],hashed(record['proof']));self.assertEqual(payload['reason'],'message_result_requires_review')
        self.assertEqual(dict(self.messages.db.execute('SELECT * FROM readers').fetchone()),p['reader'])
        self.assertEqual(dict(self.messages.db.execute('SELECT * FROM deliveries WHERE id=?',(p['delivery']['id'],)).fetchone()),p['delivery'])
        self.assertEqual([name for name,args in self.observer.calls],['lookup_execution','get_execution_control']);self.assertEqual(self.calls,1)

    def test_observation_losses_keep_intent_no_child_change_and_require_explicit_retry(self):
        for number,failure in enumerate((('before','lookup_execution'),('after','lookup_execution'),('before','get_execution_control'),('after','get_execution_control'))):
            with self.subTest(failure=failure):
                if number:self.doCleanups();self.setUp()
                self.observer.failure=failure;before=self.state();remote=self.remote_rows()
                with self.locked():
                    p=self.plan()
                    with self.assertRaisesRegex(HostError,'message_executed_observation_unconfirmed'):self.admin.apply(p)
                    self.assertEqual(self.state(),before);self.assertEqual(self.remote_rows(),remote)
                    st=self.admin.status(request_key=p['request_key']);self.assertEqual((st['phase'],st['revision']),('intent',1))
                    calls=deepcopy(self.observer.calls)
                    with self.assertRaisesRegex(HostError,'message_executed_retry_required'):self.admin.resume(st['intent_id'],expected_revision=1)
                    self.assertEqual(self.observer.calls,calls);self.observer.failure=None
                    self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=1,retry_observation=True)['phase'],'complete')
                self.assertEqual(self.calls,1)

    def reopen(self):
        directory=self.host.directory
        self.messages.close();self.message_runs.close();self.message_runtime.close();self.host.close()
        self.host=HostJournal(directory);self.addCleanup(self.host.close)
        self.messages=MessageJournal(directory/'messages');self.addCleanup(self.messages.close)
        self.message_runs=RunStore(directory/'message-runs',existing_only=True);self.addCleanup(self.message_runs.close)
        self.message_runtime=RuntimeJournal(directory/'message-runtime');self.addCleanup(self.message_runtime.close)
        self.admin=MessageExecutedDisposition(self.host,self.messages,self.message_runs,self.message_runtime,observer=self.observer)

    def test_cold_resume_after_each_child_commit_recognizes_only_own_successor(self):
        for number,phase in enumerate(('work_disposed','message_disposed','complete')):
            with self.subTest(phase=phase):
                if number:self.doCleanups();self.setUp()
                original=self.host._executed_advance
                def crash(record,next_phase,**kwargs):
                    if next_phase==phase:raise SystemExit('synthetic crash before ledger checkpoint')
                    return original(record,next_phase,**kwargs)
                with self.locked():
                    p=self.plan()
                    with patch.object(self.host,'_executed_advance',side_effect=crash):
                        with self.assertRaises(SystemExit):self.admin.apply(p)
                self.reopen()
                with self.locked():
                    st=self.admin.status(request_key=p['request_key'])
                    self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=st['revision'])['phase'],'complete')
                self.assertEqual(len(self.observer.calls),2);self.assertEqual(self.calls,1)

    def test_cold_resume_after_each_ledger_commit_keeps_exact_phases(self):
        for number,phase in enumerate(PHASES[1:]):
            with self.subTest(phase=phase):
                if number:self.doCleanups();self.setUp()
                original=self.host._executed_advance
                def lost(record,next_phase,**kwargs):
                    value=original(record,next_phase,**kwargs)
                    if next_phase==phase:raise SystemExit('lost return after actual commit')
                    return value
                with self.locked():
                    p=self.plan()
                    with patch.object(self.host,'_executed_advance',side_effect=lost):
                        with self.assertRaises(SystemExit):self.admin.apply(p)
                self.reopen()
                with self.locked():
                    st=self.admin.status(request_key=p['request_key'])
                    self.assertEqual(st['phase'],phase)
                    self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=st['revision'])['phase'],'complete')
                self.assertEqual(len(self.observer.calls),2);self.assertEqual(self.calls,1)

    def test_lost_observation_commit_requires_new_explicit_read_not_cached_receipt(self):
        original=self.host._executed_advance
        with self.locked():
            p=self.plan()
            def lost(record,phase,**kwargs):
                if phase=='runtime_verified':raise SystemExit('not committed')
                return original(record,phase,**kwargs)
            with patch.object(self.host,'_executed_advance',side_effect=lost):
                with self.assertRaises(SystemExit):self.admin.apply(p)
            st=self.admin.status(request_key=p['request_key']);self.assertEqual(st['phase'],'intent')
            with self.assertRaisesRegex(HostError,'message_executed_retry_required'):self.admin.resume(st['intent_id'],expected_revision=1)
            self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=1,retry_observation=True)['phase'],'complete')
        self.assertEqual(len(self.observer.calls),4)

    def test_cas_body_identity_and_boolean_type_changes_refuse_before_network(self):
        with self.locked():
            p=self.plan()
            for key,value in (('profile_version',True),('device_id','foreign'),('owner_id','foreign'),('session_id','foreign')):
                bad=deepcopy(p);bad['identity'][key]=value;bad['proposal_sha256']=hashed({k:v for k,v in bad.items() if k!='proposal_sha256'})
                before=self.state()
                with self.assertRaises(HostError):self.admin.apply(bad)
                self.assertEqual(before,self.state());self.assertEqual(self.observer.calls,[])
            self.observer.failure=('before','lookup_execution')
            with self.assertRaises(HostError):self.admin.apply(p)
            for revision in (True,0,2):
                with self.assertRaises(HostError):self.admin.resume(self.admin.status(request_key=p['request_key'])['intent_id'],expected_revision=revision,retry_observation=True)
            self.assertEqual(len(self.observer.calls),1)

    def test_pending_intent_blocks_start_and_divergent_predecessor_never_retries(self):
        with self.locked():
            p=self.plan();self.observer.failure=('before','lookup_execution')
            with self.assertRaises(HostError):self.admin.apply(p)
            with self.assertRaisesRegex(HostError,'message_executed_unsettled'):self.host._require_no_unsettled_executed_disposition()
            self.message_runs._db.execute('UPDATE turns SET pid=pid+1 WHERE id=?',(p['turn']['id'],));before=self.state()
            with self.assertRaises(HostError):self.admin.resume(self.host._executed_record(request_key=p['request_key'])['id'],expected_revision=1,retry_observation=True)
            self.assertEqual(before,self.state());self.assertEqual(len(self.observer.calls),1)

    def test_wrong_or_absent_runtime_evidence_never_closes_work(self):
        mutations=(lambda name,value:{'found':False} if name=='lookup_execution' else value,
            lambda name,value:{**value,'execution':{**value['execution'],'owner_id':'foreign'}} if name=='lookup_execution' else value,
            lambda name,value:{**value,'revision':True} if name=='get_execution_control' else value,
            lambda name,value:{**value,'cleanup_confirmed':1} if name=='get_execution_control' else value,
            lambda name,value:{**value,'state':'running'} if name=='get_execution_control' else value)
        for number,mutate in enumerate(mutations):
            with self.subTest(number=number):
                if number:self.doCleanups();self.setUp()
                self.observer.mutate=mutate;before=self.state()
                with self.locked():
                    p=self.plan()
                    with self.assertRaisesRegex(HostError,'message_executed_observation_unconfirmed'):self.admin.apply(p)
                    self.assertEqual(self.admin.status(request_key=p['request_key'])['phase'],'intent')
                self.assertEqual(before,self.state());self.assertEqual(self.calls,1)

    def test_read_status_and_proposal_are_allowed_in_host_transaction_but_effects_are_not(self):
        with self.locked():
            p=self.plan();result=self.admin.apply(p);calls=deepcopy(self.observer.calls)
            self.host._db.execute('BEGIN IMMEDIATE')
            try:
                self.assertEqual(self.admin.status(intent_id=result['intent_id']),result);self.assertEqual(self.admin.proposal(p['request_key']),p)
                with self.assertRaisesRegex(HostError,'message_executed_transaction_active'):self.admin.resume(result['intent_id'],expected_revision=5)
                with self.assertRaisesRegex(HostError,'message_executed_transaction_active'):self.admin.apply(p,expected_revision=5)
                self.assertTrue(self.host._db.in_transaction)
            finally:self.host._db.execute('ROLLBACK')
            self.assertEqual(self.observer.calls,calls)

    def test_positive_cleanup_does_not_probe_signal_publish_or_ack(self):
        with self.locked():
            p=self.plan();before=deepcopy(p)
            with patch('os.kill',side_effect=AssertionError('never probe')),patch('os.killpg',side_effect=AssertionError('never probe')),patch.object(self.client,'call',side_effect=AssertionError('no feed/publication')),patch.object(self.messages,'begin_ack',side_effect=AssertionError('no ACK')):
                self.assertEqual(self.admin.apply(p)['phase'],'complete')
        self.assertEqual(self.calls,1)

    def test_ack_normal_retains_disposition_and_does_not_reexecute(self):
        with self.locked():p=self.plan();result=self.admin.apply(p)
        delivery=self.messages.begin_ack(p['delivery']['id'])
        self.messages.finish_ack(delivery['id'],{'feed_id':p['reader']['feed_id'],'delivery_id':p['delivery']['remote_id'],'checkpoint':p['reader']['checkpoint']+1,'acknowledged':True})
        self.assertEqual(self.operation()['state'],'executed_disposed')
        with self.locked():self.assertEqual(self.admin.resume(result['intent_id'],expected_revision=5),result)
        self.assertEqual(self.calls,1);self.assertEqual(len(self.observer.calls),2)

    def test_schema_trigger_or_shadow_refuse_before_intent_and_do_not_rollback_caller_transaction(self):
        with self.locked():
            p=self.plan();before=self.state()
            self.message_runs._db.execute("CREATE TEMP TRIGGER forbidden AFTER UPDATE ON turns BEGIN SELECT 1; END")
            with self.assertRaises(HostError):self.admin.apply(p)
            self.assertEqual(before,self.state());self.assertEqual(self.observer.calls,[])
            self.message_runs._db.execute('DROP TRIGGER forbidden')
            for db in (self.messages.db,self.message_runs._db,self.message_runtime.db):
                db.execute('BEGIN IMMEDIATE')
                try:
                    with self.assertRaisesRegex(HostError,'message_executed_transaction_active'):self.admin.apply(p)
                    self.assertTrue(db.in_transaction)
                finally:db.execute('ROLLBACK')

    def test_non_boolean_cleanup_or_duplicate_result_keys_refuse_before_intent(self):
        raw=self.message_runs._db.execute('SELECT result_json FROM turns').fetchone()[0]
        invalids=[]
        for key in ('cleanup_confirmed','exit_zero','session_verified','tool_free','turn_completed'):
            value=json.loads(raw);value['verification'][key]=int(value['verification'][key]);invalids.append(json.dumps(value))
        invalids.append(raw[:-1]+',"reason":"protocol_error"}')
        with self.locked():
            for value in invalids:
                self.message_runs._db.execute('UPDATE turns SET result_json=?',(value,));before=self.state()
                with self.assertRaises(HostError):self.plan()
                self.assertEqual(before,self.state());self.assertEqual(self.observer.calls,[])
        self.message_runs._db.execute('UPDATE turns SET result_json=?',(raw,))

    def test_intent_transaction_rolls_back_markers_and_foreign_writes_after_record_read(self):
        for number,change in enumerate(('PRAGMA application_id=9','PRAGMA user_version=3',"UPDATE managed_commands SET state='superseded' WHERE id='fixture-start'")):
            with self.subTest(change=change):
                if number:self.doCleanups();self.setUp()
                original=self.host._executed_record;done=[False]
                def injected(**kwargs):
                    value=original(**kwargs)
                    if value is not None and self.host._db.in_transaction and not done[0]:
                        done[0]=True;self.host._db.execute(change)
                    return value
                with self.locked():
                    p=self.plan();before=self.state();markers=tuple(self.host._db.execute('PRAGMA '+name).fetchone()[0] for name in ('user_version','application_id'))
                    oldcommands=[tuple(r) for r in self.host._db.execute('SELECT * FROM managed_commands')]
                    with patch.object(self.host,'_executed_record',side_effect=injected):
                        with self.assertRaises(HostError):self.admin.apply(p)
                    self.assertEqual(before,self.state());self.assertEqual(tuple(self.host._db.execute('PRAGMA '+name).fetchone()[0] for name in ('user_version','application_id')),markers)
                    self.assertEqual([tuple(r) for r in self.host._db.execute('SELECT * FROM managed_commands')],oldcommands)
                    self.assertIsNone(self.host._db.execute('SELECT name FROM sqlite_master WHERE name=?',(TABLE,)).fetchone())
                self.assertEqual(self.observer.calls,[])

    def test_observation_commit_rollback_rejects_schema_drift_without_child_or_remote_changes(self):
        original=self.host._executed_record;done=[False]
        def injected(**kwargs):
            value=original(**kwargs)
            if value is not None and value['phase']=='runtime_verified' and self.host._db.in_transaction and not done[0]:
                done[0]=True;self.host._db.execute('PRAGMA schema_version=999')
            return value
        with self.locked():
            p=self.plan();before=self.state();remote=self.remote_rows()
            with patch.object(self.host,'_executed_record',side_effect=injected):
                with self.assertRaises(HostError):self.admin.apply(p)
            self.assertEqual(self.state(),before);self.assertEqual(self.remote_rows(),remote)
            self.assertEqual(self.admin.status(request_key=p['request_key'])['phase'],'intent');self.assertEqual(len(self.observer.calls),2)
            self.assertEqual(self.admin.resume(self.host._executed_record(request_key=p['request_key'])['id'],expected_revision=1,retry_observation=True)['phase'],'complete')

    def test_terminal_scope_checkpoint_or_proof_tamper_is_never_self_adopted(self):
        with self.locked():
            p=self.plan();result=self.admin.apply(p);record=self.host._executed_record(intent_id=result['intent_id']);calls=deepcopy(self.observer.calls)
            for column,value in (('proof_json',encoded({**record['proof'],'identity':{**record['proof']['identity'],'owner_id':'foreign'}})),
                    ('checkpoints_json',encoded({**record['checkpoints'],'work':'f'*64}))):
                original=self.host._db.execute('SELECT '+column+' FROM '+TABLE).fetchone()[0]
                self.host._db.execute('UPDATE '+TABLE+' SET '+column+'=?',(value,))
                try:
                    with self.assertRaises(HostError):self.admin.status(intent_id=result['intent_id'])
                finally:self.host._db.execute('UPDATE '+TABLE+' SET '+column+'=?',(original,))
            self.assertEqual(self.observer.calls,calls)
            self.host.stop('stopped');before=self.state()
            with self.assertRaises(HostError):self.admin.resume(result['intent_id'],expected_revision=5)
            self.assertEqual(before,self.state());self.assertEqual(self.observer.calls,calls)

    def test_fixture_executed_failure_has_positive_runtime_cleanup_and_preserved_session_pid(self):
        op=self.operation();work=self.message_runs.get_work(op['work_id'])
        turns=[dict(r) for r in self.message_runs._db.execute('SELECT * FROM turns WHERE work_id=?',(work['id'],))]
        self.assertEqual(len(turns),1);turn=turns[0];result=json.loads(turn['result_json'])
        self.assertEqual((work['status'],turn['status'],turn['requires_review']),('needs_review','blocked',1))
        self.assertIsNotNone(turn['pid']);self.assertEqual(work['provider_session_id'],turn['provider_session_id'])
        self.assertTrue(result['verification']['cleanup_confirmed']);self.assertNotIn('turn_id',op['payload'])
        runtime=self.message_runtime.find(self.message_runtime.configuration(op['payload']['scope'],work['id'],turn['id'],work['model']))
        self.assertEqual((runtime['phase'],runtime['start_confirmed'],runtime['receipt']['state'],runtime['receipt']['cleanup_confirmed']),('finished',1,'finished',True))
        self.assertEqual(self.host.dispatches()[0]['state'],'needs_review');self.assertEqual(self.host.control()['remaining'],0)
