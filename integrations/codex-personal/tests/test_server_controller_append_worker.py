"""Actual owner thread and child locks, with finite synthetic control replies."""
from copy import deepcopy
import os
import threading
import time

import pytest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_config import digest
from blackoutburst_agents.message_append_coordinator import MessagesAppendCoordinator
from blackoutburst_agents.message_append_protocol import _ascii_hash, PIN_FIELDS
from blackoutburst_agents.server_control_protocol import catalog_fence
from blackoutburst_agents.server_controller import ServerController
from test_message_append_children import roster
from test_message_append_host5 import candidate


def wait_for(predicate):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.01)
    assert predicate()


class ClosedAppendTransport:
    """No Board service, provider, process, session registration or model."""
    def __init__(self,f):
        self.f=f;self.calls=[];self.worker=None;self.controller=None
        self.phase='requested';self.proof=None;self.catalog_version=1
        self.lost_complete=False;self.lost_prepare=False;self.pause_prepare=None;self.fail_poll=0
        self.new=candidate(f)
        old=f.host.managed_target('primary');entry=f.catalog[0]
        cmd=dict(id='real-old-stop',seq=1,instance_id=old['instance_id'],action='stop',
            generation=old['generation'],launchspec_sha256=entry['launchspec_sha256'],budget=0,
            instance_version=4,revision=3,status='complete',created_at=1.,updated_at=1.,stop_confirmed=False)
        self.instances=[dict(id=old['instance_id'],host_id='fixture-host',**entry,version=4,
            generation=old['generation'],desired_state='stopped',observed_state='stopped',
            observation_revision=1,reason='stopped',last_seen_at=1.,command_id=cmd['id'],
            active=True,cleanup_confirmed=True,can_start=False,control_reason='authorization_changed',current_command=cmd)]
        target=self.new['targets'][-1]
        slot=dict(profile_id=target['agent_id'],profile_version=target['profile_version'],
            device_id='slot-device',session_id=target['board_session_id'],agent_kind='codex',identity_key='slot')
        self.slot=dict(slot_id=_ascii_hash(slot),**slot)
        self.body=dict(schema='server-messages-append-request-v1',expected_host_version=1,
            expected_catalog_version=1,expected_instances_sha256=catalog_fence(self.instances),grant_version=1,
            profile_id=target['agent_id'],profile_version=target['profile_version'],profile_sha256='a'*64,
            provider_id='codex_chatgpt',requested_model=target['model'],slot_id=self.slot['slot_id'],idempotency_key='human-request')
        self.request_hash=_ascii_hash(dict(purpose='messages_append_v1',owner_id=f.config['owner_id'],
            host_id='fixture-host',device_id='manager',body=self.body))

    def record(self):
        revisions={'requested':1,'prepared':2,'precommit':3,'committed':4,'complete':5}
        v=dict(schema='server-messages-append-view-v1',request_id='c'*32,request_sha256=self.request_hash,
            owner_id=self.f.config['owner_id'],host_id='fixture-host',device_id='manager',
            revision=revisions[self.phase],state=self.phase,reason=self.phase,barrier_held=self.phase!='complete',
            profile_id=self.body['profile_id'],profile_version=self.body['profile_version'],
            provider_id=self.body['provider_id'],requested_model=self.body['requested_model'],slot_id=self.slot['slot_id'],
            can_cancel=self.phase=='requested',instance_id=None if self.proof is None else self.proof['instance_id'],
            proof=deepcopy(self.proof),connection_status='not_attested',model_catalog_status='not_attested',
            model_access_status='not_attested',controller_status='not_reported',controller_reason='')
        return dict(request=v,body=deepcopy(self.body),slot=deepcopy(self.slot))

    def stop(self):
        i=self.instances[0];i['version']+=1
        c=dict(i['current_command'],id='new-real-stop',seq=2,instance_version=i['version'],revision=1,status='queued')
        i.update(command_id=c['id'],current_command=c,cleanup_confirmed=False)

    def call(self,name,args):
        self.calls.append((name,deepcopy(args),threading.get_ident()))
        if name=='poll_server_messages_appends':
            if self.fail_poll:
                self.fail_poll-=1;raise BoardReadError('timeout')
            return dict(schema='server-messages-append-poll-v1',host_id='fixture-host',
                requests=[] if self.phase=='complete' else [self.record()])
        if name=='poll_instance_commands':
            return deepcopy(dict(host_id='fixture-host',host_version=1,enabled=True,last_seen_at=2.,
                catalog_version=self.catalog_version,instances=self.instances,
                commands=[i['current_command'] for i in self.instances if i['current_command'] is not None
                    and i['current_command']['status'] in ('queued','accepted')]))
        if name=='report_server_messages_append_status':
            return dict(schema='server-messages-append-controller-status-v1',owner_id=self.f.config['owner_id'],
                host_id='fixture-host',device_id='manager',request_id=args['request_id'],
                request_revision=args['expected_revision'],status=args['status'],reason=args['reason'])
        if name=='ack_instance_command':
            i=next(i for i in self.instances if i['command_id']==args['command_id'])
            i['current_command'].update(status='accepted',revision=2)
            return deepcopy(dict(instance=i,command=i['current_command']))
        if name=='report_instance_state':
            i=next(i for i in self.instances if i['id']==args['instance_id'])
            i.update(observed_state=args['state'],cleanup_confirmed=args['cleanup_confirmed'],reason=args['reason'])
            i['observation_revision']+=1
            if i['current_command'] is not None and args['cleanup_confirmed']:
                i['current_command'].update(status='complete',revision=3)
            return deepcopy(dict(instance=i,command=i['current_command']))
        # The real acquired journal, not a bool or parent thread, must own all
        # append phase RPCs. No controller mutex or SQLite TX may span one.
        assert self.worker._held==(os.getpid(),threading.get_ident())
        assert not self.worker._db.in_transaction
        assert not self.controller._mutex._is_owned()
        phase={'prepare_server_messages_append':'prepared','precommit_server_messages_append':'precommit',
            'commit_server_messages_append':'committed','complete_server_messages_append':'complete'}[name]
        if name=='prepare_server_messages_append':
            self.proof=dict(schema='server-messages-append-proof-v1',operation_id=args['operation_id'],
                request_id='c'*32,request_sha256=self.request_hash,owner_id=self.f.config['owner_id'],host_id='fixture-host',
                device_id='manager',grant_version=1,expected_host_version=1,expected_catalog_version=1,
                expected_instances_sha256=self.body['expected_instances_sha256'],
                **{k:args[k] for k in PIN_FIELDS},instance_id=None,catalog_version=None,instances_sha256=None)
        elif name=='commit_server_messages_append' and self.phase!='committed':
            entry=args['entry']
            self.instances.append(dict(id='added-instance',host_id='fixture-host',**entry,version=1,generation=0,
                desired_state='stopped',observed_state='unknown',observation_revision=0,reason='',last_seen_at=None,
                command_id=None,active=True,cleanup_confirmed=False,can_start=False,control_reason='authorization_changed',current_command=None))
            self.catalog_version=2
            self.proof.update(instance_id='added-instance',catalog_version=2,instances_sha256=catalog_fence(self.instances))
        self.phase=phase;self.proof.update(phase=phase,request_revision={'prepared':2,'precommit':3,'committed':4,'complete':5}[phase])
        if name=='prepare_server_messages_append' and self.pause_prepare is not None:
            started,release=self.pause_prepare;started.set();assert release.wait(5)
        if name=='prepare_server_messages_append' and self.lost_prepare:
            self.lost_prepare=False;raise BoardReadError('timeout')
        if name=='complete_server_messages_append' and self.lost_complete:
            self.lost_complete=False;raise BoardReadError('timeout')
        return deepcopy(self.proof)


def controller(f,client):
    ticks=[]
    class NeverTick:
        def __init__(self,worker):pass
        def tick(self,**kwargs):ticks.append(True);raise AssertionError('B must never tick or open a feed')
    def factory(worker):
        worker._require_lock();client.worker=worker
        return MessagesAppendCoordinator(worker,client,source_release_sha256='b'*64,
            candidate_factory=lambda record,old:deepcopy(client.new),source_guard=lambda:None)
    c=ServerController(f.host,client,append_factory=factory,supervisor_factory=NeverTick,lock_directory=f.root/'locks')
    client.controller=c
    return c,ticks


def drive(c,predicate):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        c.step()
        if predicate():return
        time.sleep(.02)
    assert predicate(), (c._append_failure,c._append_epoch,c._append_attempted,[n for n,a,t in c.client.calls][:20])


def test_real_worker_advances_four_phases_without_feed_or_model(roster):
    f=roster;client=ClosedAppendTransport(f);c,ticks=controller(f,client)
    before=f.host.managed_target('primary');main=threading.get_ident()
    try:
        c.start_worker();drive(c,lambda:client.phase=='complete' and not c._append_hold)
        assert f.host.managed_target('primary')==before
        added=f.host.managed_target(client.new['targets'][-1]['id'])
        assert added['instance_id']=='added-instance' and not added['enabled']
        calls=[(n,t) for n,a,t in client.calls if n in ('prepare_server_messages_append',
            'precommit_server_messages_append','commit_server_messages_append','complete_server_messages_append')]
        assert len(calls)==4 and all(t!=main for n,t in calls)
        assert not ticks and not f.host.control()['enabled']
    finally:assert c.shutdown()


def test_lost_complete_cold_worker_replays_exact_key_before_publication(roster):
    f=roster;client=ClosedAppendTransport(f);client.lost_complete=True;c,ticks=controller(f,client)
    try:
        c.start_worker();drive(c,lambda:client.phase=='complete' and c._append_failure=='connection_lost')
        saved=f.host.control();old=next(a for n,a,t in client.calls if n=='complete_server_messages_append')
    finally:assert c.shutdown()
    assert f.host.control()==saved
    resumed,more=controller(f,client)
    try:
        resumed.start_worker()
        assert resumed.publish(f.catalog)['status']=='messages_append_pending'
        drive(resumed,lambda:not resumed._append_hold)
        calls=[a for n,a,t in client.calls if n=='complete_server_messages_append']
        assert len(calls)==2 and calls[0]==calls[1]==old
        assert not ticks and not more and not f.host.control()['enabled']
    finally:assert resumed.shutdown()


def test_lost_prepare_Host4_outbox_does_not_rewrite_disabled_startup_snapshot(roster):
    f=roster;client=ClosedAppendTransport(f);client.lost_prepare=True;c,ticks=controller(f,client)
    saved=f.host.control();targets=f.host.managed_target('primary')
    try:
        c.start_worker();drive(c,lambda:client.phase=='prepared' and c._append_failure=='connection_lost')
        assert f.host._messages_append_version()==4
    finally:assert c.shutdown()
    assert f.host.control()==saved and f.host.managed_target('primary')==targets
    resumed,more=controller(f,client)
    try:
        resumed.start_worker()
        assert f.host.control()==saved and f.host.managed_target('primary')==targets
        resumed.publish(f.catalog)
        drive(resumed,lambda:client.phase=='complete' and not resumed._append_hold)
        calls=[a for n,a,t in client.calls if n=='prepare_server_messages_append']
        assert len(calls)==2 and calls[0]==calls[1]
        assert not ticks and not more
    finally:assert resumed.shutdown()


def test_real_Stop_during_prepared_RPC_settles_and_invalidates_B(roster):
    f=roster;client=ClosedAppendTransport(f);started=threading.Event();release=threading.Event()
    client.pause_prepare=(started,release);c,ticks=controller(f,client)
    try:
        c.start_worker();c.step();assert started.wait(3)
        client.stop();c.step();release.set()
        wait_for(lambda:(f.host.managed_command('new-real-stop') or {}).get('state')=='applied')
        drive(c,lambda:client.instances[0]['cleanup_confirmed'])
        assert client.instances[0]['current_command']['status']=='complete'
        assert c._append_hold and c._append_invalidated
        assert not any(n in ('precommit_server_messages_append','commit_server_messages_append',
            'complete_server_messages_append') for n,a,t in client.calls)
        count=len([1 for n,a,t in client.calls if n=='report_server_messages_append_status'])
        for _ in range(3):c.step()
        assert len([1 for n,a,t in client.calls if n=='report_server_messages_append_status'])==count
        assert not ticks and not f.host.control()['enabled']
    finally:release.set();assert c.shutdown()


def test_empty_queue_recovers_after_metadata_loss_without_old_activation_replay(roster):
    f=roster;client=ClosedAppendTransport(f);client.phase='complete'
    c,_=controller(f,client)
    class Idle:
        def __init__(self,worker):pass
        def tick(self,**kwargs):return {'status':'idle'}
    c.factory=Idle
    try:
        c.start_worker();c.step()
        i=client.instances[0];i['version']+=1;i['generation']+=1
        cmd=dict(i['current_command'],id='new-human-start',seq=2,action='start',budget=1,
            instance_version=i['version'],generation=i['generation'],revision=1,status='queued')
        i.update(command_id=cmd['id'],current_command=cmd,desired_state='running',can_start=True,control_reason='')
        c.step();wait_for(lambda:f.host.managed_target('primary')['enabled'])
        client.fail_poll=1;c.step()
        assert c._append_hold and not f.host.managed_target('primary')['enabled']
        for _ in range(3):c.step()
        assert not c._append_hold and not f.host.managed_target('primary')['enabled']
        assert not any(n.endswith('_server_messages_append') for n,a,t in client.calls)
    finally:assert c.shutdown()
