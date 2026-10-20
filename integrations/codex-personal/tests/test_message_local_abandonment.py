"""Real temporary Board and four journals; no process, provider or model."""
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
from blackoutburst_agents.message_admission_abandonment import MessageAdmissionAbandonment
from blackoutburst_agents.message_admission_seal import MessageAdmissionSealClient
from blackoutburst_agents.message_abandon_protocol import hashed,LEGACY_RESULT


class MemorySeal(MessageAdmissionSealClient):
    def __init__(self,fixture):
        self.fixture=fixture;self._config={'mcp_url':'https://board.test/mcp'};self.session_id=fixture.client.session_id;self.calls=[];self.failure=None
    def _initialize(self):pass
    def _tool(self,name,args):
        self.calls.append((name,deepcopy(args)))
        if self.failure=='before':raise TimeoutError('PRIVATE_FAILURE')
        value=self.fixture.board.seal_message_execution_admission(
            {**self.fixture.raw,'session_id':self.session_id},**{k:v for k,v in args.items() if k!='session_id'})
        if self.failure=='after':raise TimeoutError('PRIVATE_FAILURE')
        return value


class MessageLocalAbandonmentTests(unittest.TestCase):
    resources=fixture.MessageHostTests.resources
    post=fixture.MessageHostTests.post
    tick_host=fixture.MessageHostTests.tick_host
    operation=fixture.MessageHostTests.operation
    def transport(self,*args,**kwargs):
        self.calls+=1
        raise AssertionError('no provider permitted')

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
        # Bind the original now-feed before the human message is posted.
        self.board.set_policy(self.human,True,True,True,True,[],[])
        actor={**self.raw,'session_id':self.client.session_id}
        ticket=self.board.get_ticket(self.human,self.ticket['id'])['ticket']
        self.board.claim_ticket(actor,ticket['id'],ticket['version'])
        old=self.board.reserve_execution(actor,'old-synthetic-slot',self.project['id'],self.ticket['id'],
            str(uuid.uuid4()),str(uuid.uuid4()),'codex_chatgpt','fixture-model')['execution']
        self.board.start_execution(actor,old['id'],old['revision'])
        self.board.set_policy(self.human,True,True,True,False,[],[])
        self.assertEqual(self.tick_host()['status'],'idle')
        self.post();result=self.tick_host()
        self.assertEqual((result['status'],result['reason']),('pending','message_runtime_reconciliation'))
        self.assertEqual(self.calls,0)
        with self.locked():self.host.stop()
        self.old_runtime=self.remote_rows()
        self.seal=MemorySeal(self)
        self.admin=MessageAdmissionAbandonment(self.host,self.messages,self.message_runs,self.message_runtime,seal_client=self.seal)
        self.identity={'owner_id':'owner','agent_id':self.config['targets'][0]['agent_id'],
            'device_id':self.raw['device_id'],'profile_version':self.config['targets'][0]['profile_version'],
            'session_id':self.client.session_id}

    def locked(self):return self.host.owner_lock(lock_directory=self.root/'locks')
    def remote_rows(self):
        with self.board._connection() as db:return [tuple(r) for r in db.execute('SELECT * FROM runtime_executions')]
    def plan(self):
        d=self.host.dispatches()[0]
        return self.admin.propose(request_key='explicit-abandon-one',target_id='primary',batch_id=d['batch_id'],
            worker_id=d['worker_id'],identity=self.identity,expected_config_sha256=self.host.control()['hash'],
            expected_host_instance=self.host.control()['instance_id'])
    def state(self):
        return [[tuple(r) for r in db.execute('SELECT * FROM '+table)] for db,table in (
            (self.host._db,'control'),(self.host._db,'dispatches'),(self.messages.db,'operations'),
            (self.message_runs._db,'works'),(self.message_runs._db,'turns'),(self.message_runtime.db,'executions'))]

    def test_full_explicit_resolution_retains_original_history_and_budget(self):
        with self.locked():
            p=self.plan();before=self.host.control();delivery=deepcopy(p['delivery']);reader=deepcopy(p['reader'])
            result=self.admin.resolve(p)
            self.assertEqual((result['phase'],result['revision']),('complete',6))
            self.assertEqual(self.admin.resume(result['intent_id'],expected_revision=6),result)
            self.assertEqual(self.admin.proposal(p['request_key']),p)
            self.assertEqual(len(self.seal.calls),1)
            for k in ('remaining','activation','revision','enabled'):self.assertEqual(self.host.control()[k],before[k])
            self.assertEqual(dict(self.messages.db.execute('SELECT * FROM readers').fetchone()),reader)
            self.assertEqual(dict(self.messages.db.execute('SELECT * FROM deliveries WHERE id=?',(delivery['id'],)).fetchone()),delivery)
        rt=self.message_runtime.get(p['runtime']['id']);self.assertEqual(rt['phase'],'abandoned_never_started')
        self.assertEqual(rt['start_confirmed'],0);self.assertNotIn('cleanup_confirmed',json.dumps(rt['receipt']))
        turn=self.message_runs._turn(p['turn']['id']);self.assertEqual(json.loads(turn['result_json']),LEGACY_RESULT)
        self.assertEqual(turn['resolution'],'abandon');self.assertEqual(turn['status'],'blocked')
        self.assertEqual(self.operation()['state'],'abandoned');self.assertEqual(self.remote_rows(),self.old_runtime)
        self.assertEqual(self.calls,0)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('token',json.dumps(result))
        self.assertEqual([j.execute('PRAGMA user_version').fetchone()[0] for j in
            (self.host._db,self.messages.db,self.message_runtime.db)],[3,2,4])

    def test_lost_seal_response_keeps_intent_and_requires_explicit_retry(self):
        self.seal.failure='after'
        with self.locked():
            p=self.plan();initial=self.state()
            with self.assertRaisesRegex(HostError,'message_abandon_seal_unconfirmed'):self.admin.resolve(p)
            self.assertEqual(self.state(),initial)
            status=self.admin.status(request_key=p['request_key']);self.assertEqual(status['phase'],'intent')
            with self.assertRaisesRegex(HostError,'message_abandon_retry_required'):
                self.admin.resume(status['intent_id'],expected_revision=1)
            self.assertEqual(len(self.seal.calls),1)
            self.seal.failure=None
            self.assertEqual(self.admin.resume(status['intent_id'],expected_revision=1,retry_seal=True)['phase'],'complete')
        self.assertEqual(len(self.seal.calls),2);self.assertEqual(self.seal.calls[0],self.seal.calls[1]);self.assertEqual(self.calls,0)

    def test_cas_and_proposal_conflicts_before_network(self):
        with self.locked():
            p=self.plan();bad=deepcopy(p);bad['identity']['device_id']='other'
            with self.assertRaises(HostError):self.admin.resolve(bad)
            self.assertEqual(self.seal.calls,[])
            self.seal.failure='before'
            with self.assertRaises(HostError):self.admin.resolve(p)
            st=self.admin.status(request_key=p['request_key'])
            with self.assertRaises(HostError):self.admin.resume(st['intent_id'],expected_revision=2,retry_seal=True)
            self.assertEqual(len(self.seal.calls),1)

    def test_four_crash_boundaries_recover_only_exact_own_successors(self):
        # Each subcase owns a fresh four-journal fixture.
        for phase in ('runtime_abandoned','work_abandoned','message_abandoned','complete'):
            with self.subTest(phase=phase):
                if phase!='runtime_abandoned':
                    self.doCleanups();self.setUp()
                original=self.host._abandon_advance
                def crash(record,next_phase,**kwargs):
                    if next_phase==phase:raise SystemExit('synthetic crash')
                    return original(record,next_phase,**kwargs)
                with self.locked():
                    p=self.plan()
                    with patch.object(self.host,'_abandon_advance',side_effect=crash):
                        with self.assertRaises(SystemExit):self.admin.resolve(p)
                    st=self.admin.status(request_key=p['request_key'])
                    self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=st['revision'])['phase'],'complete')
                    self.assertEqual(len(self.seal.calls),1)
                self.assertEqual(self.calls,0);self.assertEqual(self.remote_rows(),self.old_runtime)

    def test_known_history_retained_by_normal_ack(self):
        with self.locked():p=self.plan();result=self.admin.resolve(p)
        delivery=self.messages.begin_ack(p['delivery']['id'])
        response={'feed_id':p['reader']['feed_id'],'delivery_id':p['delivery']['remote_id'],
            'checkpoint':p['reader']['checkpoint']+1,'acknowledged':True}
        self.messages.finish_ack(delivery['id'],response)
        self.assertEqual(self.operation()['state'],'abandoned')
        with self.locked():self.assertEqual(self.admin.resume(result['intent_id'],expected_revision=6),result)
        self.assertEqual(self.calls,0)

    def test_terminal_host_change_refuses_replay_without_rpc(self):
        with self.locked():
            p=self.plan();r=self.admin.resolve(p);self.host.stop('needs_review');before=self.state()
            with self.assertRaises(HostError):self.admin.resume(r['intent_id'],expected_revision=r['revision'])
            self.assertEqual(self.state(),before);self.assertEqual(len(self.seal.calls),1)

    def test_trigger_refused_before_intent_or_remote_effect(self):
        with self.locked():
            p=self.plan();before=self.state()
            self.message_runtime.db.execute("CREATE TRIGGER forbidden AFTER UPDATE ON executions BEGIN SELECT 1; END")
            with self.assertRaisesRegex(HostError,'message_abandon_schema'):self.admin.resolve(p)
            self.assertEqual(self.state(),before);self.assertEqual(self.seal.calls,[])

    def test_running_or_provider_pid_cannot_be_adopted(self):
        for column,value in (('pid',999999999),('provider_session_id','synthetic-session')):
            with self.subTest(column=column),self.locked():
                self.message_runs._db.execute('UPDATE turns SET '+column+'=?',(value,))
                before=self.state()
                with self.assertRaises(HostError):self.plan()
                self.assertEqual(self.state(),before);self.assertEqual(self.seal.calls,[])
                self.message_runs._db.execute('UPDATE turns SET '+column+'=NULL')

    def test_crash_after_committed_host_phase_keeps_monotone_versions(self):
        for phase in ('sealed','runtime_abandoned','work_abandoned','message_abandoned','complete'):
            with self.subTest(phase=phase):
                if phase!='sealed':self.doCleanups();self.setUp()
                original=self.host._abandon_advance
                def lost(record,next_phase,**kwargs):
                    value=original(record,next_phase,**kwargs)
                    if next_phase==phase:raise SystemExit('lost return after commit')
                    return value
                with self.locked():
                    p=self.plan()
                    with patch.object(self.host,'_abandon_advance',side_effect=lost):
                        with self.assertRaises(SystemExit):self.admin.resolve(p)
                    st=self.admin.status(request_key=p['request_key'])
                    self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=st['revision'])['phase'],'complete')
                    self.assertEqual(len(self.seal.calls),1)
                self.assertEqual(self.calls,0)

    def test_divergent_predecessor_after_intent_refuses_without_seal_retry(self):
        with self.locked():
            p=self.plan();self.seal.failure='before'
            with self.assertRaises(HostError):self.admin.resolve(p)
            st=self.admin.status(request_key=p['request_key'])
            self.message_runs._db.execute('UPDATE turns SET cancel_requested=1 WHERE id=?',(p['turn']['id'],))
            changed=self.state()
            with self.assertRaises(HostError):self.admin.resume(st['intent_id'],expected_revision=1,retry_seal=True)
            self.assertEqual(self.state(),changed);self.assertEqual(len(self.seal.calls),1)

    def test_rollback_runtime_schema_marker_and_target_row_on_write_failure(self):
        original=self.message_runtime.get
        def fail_after_write(identifier):
            value=original(identifier)
            if value['phase']=='abandoned_never_started':raise RuntimeError('PRIVATE_SENTINEL')
            return value
        with self.locked():
            p=self.plan();before=deepcopy(p['runtime'])
            with patch.object(self.message_runtime,'get',side_effect=fail_after_write):
                with self.assertRaisesRegex(HostError,'message_abandon_journal_conflict'):self.admin.resolve(p)
            self.assertEqual(original(p['runtime']['id']),before)
            self.assertEqual(self.message_runtime.db.execute('PRAGMA user_version').fetchone()[0],1)
            st=self.admin.status(request_key=p['request_key']);self.assertEqual(st['phase'],'sealed')
            self.assertEqual(self.admin.resume(st['intent_id'],expected_revision=2)['phase'],'complete')
        self.assertEqual(len(self.seal.calls),1)

    def test_generic_journal_paths_cannot_create_or_overwrite_dispositions(self):
        from blackoutburst_agents.work_journal import JournalError
        with self.locked():p=self.plan();r=self.admin.resolve(p)
        op=self.operation();rt=self.message_runtime.get(p['runtime']['id']);before=self.state()
        with self.assertRaises(HostError):self.messages.update(op['id'],op['revision'],'abandoned',**op['payload'])
        with self.assertRaises(HostError):self.messages.update(op['id'],op['revision'],'complete')
        with self.assertRaises(JournalError):self.message_runtime.update(rt['id'],rt['revision'],'finished',
            {'state':'finished','cleanup_confirmed':True})
        self.assertEqual(self.state(),before)

    def test_start_and_append_refuse_unsettled_intent(self):
        with self.locked():
            p=self.plan();self.seal.failure='before'
            with self.assertRaises(HostError):self.admin.resolve(p)
            with self.assertRaisesRegex(HostError,'message_abandon_unsettled'):
                self.host._require_no_unsettled_abandonment()
            self.assertEqual(self.host.control()['remaining'],0)
            self.assertFalse(self.host.control()['enabled'])

    def test_inspection_running_refuses_without_marking_interruption(self):
        self.message_runs._db.execute("UPDATE turns SET status='running'")
        before=self.state()
        with self.locked(),self.assertRaises(HostError):self.plan()
        self.assertEqual(self.state(),before);self.assertEqual(self.seal.calls,[])

    def test_wrong_current_device_seal_is_not_accepted_as_argument_authority(self):
        with self.locked():
            pinned=self.identity['device_id'];self.identity['device_id']='wrong-public-pin'
            p=self.plan();before=self.state()
            with self.assertRaisesRegex(HostError,'message_abandon_seal_unconfirmed'):self.admin.resolve(p)
            self.assertEqual(self.state(),before)
            st=self.admin.status(request_key=p['request_key']);self.assertEqual(st['phase'],'intent')
            self.identity['device_id']=pinned
        self.assertEqual(self.calls,0)

    def test_reopen_all_journals_retains_exact_terminal_and_refuses_lower_markers(self):
        with self.locked():p=self.plan();result=self.admin.resolve(p)
        self.messages.close();self.message_runtime.close();self.message_runs.close();self.host.close()
        directory=self.root/'managed-host'
        self.host=HostJournal(directory);self.addCleanup(self.host.close)
        self.messages=MessageJournal(directory/'messages');self.addCleanup(self.messages.close)
        self.message_runs=RunStore(directory/'message-runs',existing_only=True);self.addCleanup(self.message_runs.close)
        self.message_runtime=RuntimeJournal(directory/'message-runtime');self.addCleanup(self.message_runtime.close)
        self.admin=MessageAdmissionAbandonment(self.host,self.messages,self.message_runs,self.message_runtime,seal_client=self.seal)
        with self.locked():self.assertEqual(self.admin.resume(result['intent_id'],expected_revision=6),result)
        for obj,name,version,constructor in ((self.host,'host.sqlite3',2,HostJournal),
                (self.messages,'messages.sqlite3',1,MessageJournal),
                (self.message_runtime,'runtime.sqlite3',3,RuntimeJournal)):
            with self.subTest(name=name):
                db=getattr(obj,'db',None) or obj._db;db.execute('PRAGMA user_version='+str(version))
                path=obj.directory/name;obj.close();before=path.read_bytes()
                with self.assertRaises(Exception):constructor(obj.directory)
                self.assertEqual(path.read_bytes(),before)

    def test_seal_revocation_never_changes_other_journals_or_refunds(self):
        with self.locked():
            p=self.plan();before=self.state()
            self.board.revoke_device(self.human,self.raw['device_id'])
            with self.assertRaisesRegex(HostError,'message_abandon_seal_unconfirmed'):self.admin.resolve(p)
            self.assertEqual(self.state(),before);self.assertEqual(self.host.control()['remaining'],0)
            self.assertEqual(self.admin.status(request_key=p['request_key'])['phase'],'intent')
        self.assertEqual(self.remote_rows(),self.old_runtime);self.assertEqual(self.calls,0)

    def test_mutating_saved_successor_after_crash_is_not_adopted(self):
        original=self.host._abandon_advance
        def crash(record,phase,**kwargs):
            if phase=='work_abandoned':raise SystemExit('synthetic crash')
            return original(record,phase,**kwargs)
        with self.locked():
            p=self.plan()
            with patch.object(self.host,'_abandon_advance',side_effect=crash):
                with self.assertRaises(SystemExit):self.admin.resolve(p)
            self.message_runs._db.execute("UPDATE turns SET resolution_reason='different-admin-resolution'")
            current=self.state()
            with self.assertRaises(HostError):self.admin.resume(
                self.host._abandon_record(request_key=p['request_key'])['id'],expected_revision=3)
            self.assertEqual(self.state(),current);self.assertEqual(len(self.seal.calls),1)

    def test_real_proposal_is_read_only_for_all_four_database_bytes(self):
        paths=(self.host.directory/'host.sqlite3',self.messages.directory/'messages.sqlite3',
            self.message_runs.state_dir/'runs.sqlite3',self.message_runtime.directory/'runtime.sqlite3')
        with self.locked():
            before=[p.read_bytes() for p in paths];plan=self.plan()
            self.assertEqual([p.read_bytes() for p in paths],before)
            self.assertNotIn('PRIVATE_HUMAN',json.dumps(plan))
            self.assertNotIn('PRIVATE_INSTRUCTION',json.dumps(plan))
        self.assertEqual(self.seal.calls,[])
