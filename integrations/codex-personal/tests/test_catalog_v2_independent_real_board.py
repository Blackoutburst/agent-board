"""Independent Host4 -> real Board/SQLite source replacement; no real model/process.

Real remote IDs/commands are created BEFORE the failure/proposal/closure. Historical
proofs are never rewritten to substitute a manager/instance or change a fence.
"""
from copy import deepcopy
import json
import unittest

import pytest
import test_message_host as message_fixture
import test_message_executed_disposition as executed_fixture
from test_server_controller import BoardControl
from blackoutburst_agents.host_config import validate,digest
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.message_host import MessageHostSupervisor
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.server_controller import launch_catalog
from blackoutburst_agents.message_executed_disposition import MessageExecutedDisposition,hashed,disposition_receipt
from blackoutburst_agents.catalog_source_transition import CatalogSourceTransition,EXECUTED_SCHEMA


class Fixture(unittest.TestCase):
    resources=message_fixture.MessageHostTests.resources
    post=message_fixture.MessageHostTests.post
    tick_host=message_fixture.MessageHostTests.tick_host
    operation=message_fixture.MessageHostTests.operation
    transport=executed_fixture.ExecutedDispositionTests.transport
    locked=executed_fixture.ExecutedDispositionTests.locked
    plan=executed_fixture.ExecutedDispositionTests.plan
    remote_rows=executed_fixture.ExecutedDispositionTests.remote_rows

    def instance(self):
        return self.board.poll_instance_commands(self.raw,self.remote['id'])['instances'][0]

    def report(self,state,cleanup,key):
        i=self.instance()
        return self.server.call('report_instance_state',{'host_id':self.remote['id'],'instance_id':i['id'],
            'expected_observation_revision':i['observation_revision'],'generation':i['generation'],
            'launchspec_sha256':i['launchspec_sha256'],'command_id':i['command_id'],
            'state':state,'cleanup_confirmed':cleanup,'reason':'stopped' if cleanup else '', 'idempotency_key':key})

    def human_command(self,action,key):
        i=self.instance()
        return self.board.command_server_instance(self.human,i['id'],action,i['version'],i['generation'],
            i['launchspec_sha256'],0 if action=='stop' else 1,key)

    def setUp(self):
        message_fixture.MessageHostTests.setUp(self)
        self.messages.close();self.message_runs.close();self.message_runtime.close();self.host.close()
        self.config=deepcopy(self.config);self.config['targets'][0]['start_at']='now';self.config=validate(self.config)
        self.host=HostJournal(self.root/'real-managed');self.addCleanup(self.host.close);self.host.configure(self.config)
        self.messages=MessageJournal(self.host.directory/'messages');self.addCleanup(self.messages.close)
        self.message_runs=RunStore(self.host.directory/'message-runs');self.addCleanup(self.message_runs.close)
        self.message_runtime=RuntimeJournal(self.host.directory/'message-runtime');self.addCleanup(self.message_runtime.close)
        self.supervisor=MessageHostSupervisor(self.host,resource_factory=self.resources)
        self.catalog=launch_catalog(self.config,'b'*64)
        self.remote=self.board.create_server_host(self.human,self.raw['device_id'],'Synthetic owner server','explicit-human-grant')
        self.server=BoardControl(self.board,self.raw)
        published=self.server.call('publish_server_catalog',{'host_id':self.remote['id'],'expected_catalog_version':0,
            'entries':self.catalog,'idempotency_key':'initial-publication'})
        instance=published['instances'][0]
        with self.locked():
            self.host.prepare_managed(self.remote['id'],self.catalog)
            self.host.bind_managed_instances({'primary':instance['id']})
        # Empty synthetic owner state genuinely precedes any child or message.
        self.report('stopped',True,'initial-clean-owner-state')
        command=self.human_command('start','first-human-start')
        self.server.call('ack_instance_command',{'host_id':self.remote['id'],'command_id':command['id'],
            'expected_revision':command['revision'],'idempotency_key':'initial-start-ack'})
        with self.locked():
            self.host.queue_managed_command({'id':command['id'],'target_id':'primary','instance_id':instance['id'],
                **{k:command[k] for k in ('generation','action','launchspec_sha256','budget')}})
            self.host.apply_managed_command(command['id'])
        self.report('ready',False,'initial-ready-state')
        assert self.tick_host()['status']=='idle'
        self.post();failed=self.tick_host()
        assert (failed['status'],failed['reason'])==('needs_review','message_result_requires_review')
        assert self.calls==1
        with self.locked():self.host.stop()
        self.identity={'owner_id':'owner','agent_id':self.config['targets'][0]['agent_id'],
            'device_id':self.raw['device_id'],'profile_version':self.config['targets'][0]['profile_version'],
            'session_id':self.client.session_id}
        self.observer=executed_fixture.MemoryObserver(self)
        self.admin=MessageExecutedDisposition(self.host,self.messages,self.message_runs,self.message_runtime,observer=self.observer)
        with self.locked():
            self.disposition_proposal=self.plan();self.closed=self.admin.apply(self.disposition_proposal)
            self.disposition_record=self.host._executed_record(intent_id=self.closed['intent_id'])
        self.observer_history=deepcopy(self.observer.calls)
        self.server.calls.clear()

    def stopped_children(self):
        # Deliberately a single-executed-child test guard. It checks the same real
        # terminal storage predicates, not Fresh.status's historical Host fence.
        assert not any(db.in_transaction for db in (self.host._db,self.messages.db,self.message_runs._db,self.message_runtime.db))
        record=self.host._executed_record(intent_id=self.closed['intent_id']);p=record['proposal']
        assert record==self.disposition_record and record['phase']=='complete'
        runtime=self.message_runtime.get(p['runtime']['id'])
        work={'work':self.message_runs._work(p['work']['id']),'turn':self.message_runs._turn(p['turn']['id'])}
        operation=self.messages.operation(p['message']['row']['id'])
        assert runtime==p['runtime'] and self.admin._work_after(p,record['id'],work)
        assert self.admin._message_after(p,record['id'],record['proof'],operation)
        assert all(hashed(value)==record['checkpoints'][key] for key,value in (('runtime',runtime),('work',work),('message',operation)))
        assert self.host.dispatches()[0]['receipt']==disposition_receipt(p,record['id'],record['proof'])
        return True


@pytest.fixture
def executed():
    f=Fixture();f.setUp()
    try:yield f
    finally:f.doCleanups()


@pytest.mark.parametrize('lost',[None,'ack_instance_command','replace_server_catalog'])
def test_actual_v2_full_remote_stop_source_cas_and_explicit_resume(executed,lost):
    f=executed;h=f.host
    stop=f.human_command('stop','genuine-new-human-stop')
    initial_runtime=f.remote_rows()
    initial_ledger=[tuple(r) for r in h._db.execute('SELECT * FROM host_message_dispositions')]
    initial_children={name:[tuple(r) for r in db.execute('SELECT * FROM '+name)] for db,name in (
        (f.message_runs._db,'works'),(f.message_runs._db,'turns'),(f.message_runtime.db,'executions'),
        (f.messages.db,'operations'),(f.messages.db,'readers'),(f.messages.db,'deliveries'))}
    source_checks=[]
    def sources(old,new,configuration):
        assert (old,new,configuration)==('b'*64,'c'*64,h.control()['hash'])
        source_checks.append('tx' if h._db.in_transaction else 'outside')
        return True
    c=CatalogSourceTransition(h,f.server,source_guard=sources,stopped_guard=f.stopped_children,
        executed_disposition=f.admin)
    with f.locked():
        p=c.plan(old_release_sha256='b'*64,new_release_sha256='c'*64,request_key='real-executed-source-transition')
        assert p['schema']==EXECUTED_SCHEMA and p['closures']==[]
        assert p['executed_closures']==[{'intent_id':f.closed['intent_id'],'request_key':f.disposition_proposal['request_key'],
            'revision':5,'proposal_sha256':f.disposition_proposal['proposal_sha256']}]
        identity='source-'+digest(p['request_key'])
        if lost:
            f.server.lost.add(lost)
            with pytest.raises(BoardReadError,match='timeout'):c.resolve(p)
            status=c.status(identity)
            assert status['phase'] in ('stop_pending','replace_pending')
            assert h.managed_settings()['catalog_sha256']==digest(p['old_catalog'])
            result=c.resume(identity,expected_revision=status['revision'],expected_proposal_sha256=status['proposal_sha256'])
            calls=[args for name,args in f.server.calls if name==lost]
            assert len(calls)==2 and calls[0]==calls[1]
        else:result=c.resolve(p)
        assert result['catalog_version']==2
        assert c.status(identity)['phase']=='complete'
        assert c.resume(identity,expected_revision=4,expected_proposal_sha256=p['proposal_sha256'])==result
        assert h.managed_settings()['catalog_sha256']==digest(p['new_catalog'])
        assert h.control()['remaining']==0 and not h.control()['enabled']
        assert h.managed_target('primary')['generation']==stop['generation']
        assert h.managed_target('primary')['remaining']==0 and not h.managed_target('primary')['enabled']
        assert [tuple(r) for r in h._db.execute('SELECT * FROM host_message_dispositions')]==initial_ledger
    assert initial_children=={name:[tuple(r) for r in db.execute('SELECT * FROM '+name)] for db,name in (
        (f.message_runs._db,'works'),(f.message_runs._db,'turns'),(f.message_runtime.db,'executions'),
        (f.messages.db,'operations'),(f.messages.db,'readers'),(f.messages.db,'deliveries'))}
    assert f.observer.calls==f.observer_history and f.remote_rows()==initial_runtime and f.calls==1
    instance=f.instance();assert instance['desired_state']=='stopped' and instance['observed_state']=='unknown'
    assert instance['cleanup_confirmed'] is False and instance['current_command']['id']==stop['id']
    assert instance['launchspec_sha256']==p['new_catalog'][0]['launchspec_sha256']
    assert 'tx' in source_checks
    assert {name for name,args in f.server.calls}<={'poll_instance_commands','ack_instance_command','report_instance_state','replace_server_catalog'}
    assert not any(name in ('publish_server_catalog','post_message','checkpoint_message_feed') for name,args in f.server.calls)
