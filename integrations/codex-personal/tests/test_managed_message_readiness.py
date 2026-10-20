"""Temporary Board/SQLite, real feed and controller; no transport or inference."""
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
import threading

from blackoutburst_agents.board_reader import BoardReadError

import pytest
from board import Board
from blackoutburst_agents.host_config import validate,digest
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_supervisor import HostSupervisor
from blackoutburst_agents.message_host import PinnedMessageClient
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.server_controller import ServerController,launch_catalog
from test_board_worker import Client
from test_host_journal import configuration
from test_server_controller import BoardControl,wait_for


@pytest.fixture
def messages(tmp_path,request):
    b=Board(tmp_path/'board');b.ensure_owner('owner','Owner');human={'kind':'human','owner_id':'owner'}
    b.set_policy(human,True,True,True,True,[],[])
    device=b.create_device(human,'Fixture',[]);actor=b.authenticate(device['token'])
    project=b.create_project(human,'Fixture project',['owner'],idempotency_key='fixture-project')
    ticket=b.create_ticket(human,project['id'],'Fixture ticket',idempotency_key='fixture-ticket')
    profiles=[]
    for index in range(getattr(request,'param',1)):
        key='primary' if index==0 else 'target-'+str(index)
        profile=b.create_agent_profile(human,'Worker '+str(index),project_ids=[project['id']],policy={
            'allow_read':True,'allow_messages':True,'allow_requests':True,
            'actions':[],'resources':[],'autonomy':'requested'})
        profiles.append(b.bind_agent_profile(human,profile['id'],profile['version'],device['id'],key,'codex'))
    profile=profiles[0]
    remote=b.create_server_host(human,device['id'],'Fixture host','fixture-grant')
    thread=b.get_thread(human,ticket['thread_id'])['thread']
    cfg=configuration(tmp_path);cfg['schema_version']=2
    t=cfg['targets'][0]
    for key in ('project_id','ticket_id','after'):del t[key]
    t.update(agent_id=profile['id'],profile_version=profile['version'],thread_ids=[thread['id']],start_at='now')
    cfg['targets']=[dict(deepcopy(t),id='primary' if i==0 else 'target-'+str(i),
        agent_id=p['id'],profile_version=p['version'],board_session_id='fixture-session-'+str(i))
        for i,p in enumerate(profiles)]
    cfg=validate(cfg,bind_connections=True)
    host=HostJournal(tmp_path/'host');host.configure(cfg);catalog=launch_catalog(cfg,'b'*64)
    with host.owner_lock(lock_directory=tmp_path/'locks'):host.prepare_managed(remote['id'],catalog)
    opening=threading.Event();release=threading.Event();opened=threading.Event();ticks=threading.Event()
    operations=[];calls=[];model_calls=[];fail_open=[];lost_open=[];delivered=[]
    reading=threading.Event();read_release=threading.Event();read_release.set()
    entering=threading.Event();resource_release=threading.Event();resource_release.set()
    stop_after_read=[]
    @contextmanager
    def resources(j,target,config,cancelled):
        entering.set();assert resource_release.wait(3)
        class FeedClient(Client):
            def register(self):
                value=b.register_agent(actor,self.session_id,'Fixture',[],'codex',False,target['id'])
                self.actor=b.actor_session(actor,self.session_id);return value
            def call(self,name,args):
                calls.append(name)
                if name=='open_message_feed':
                    opening.set();assert release.wait(3)
                    if fail_open:raise BoardReadError('tool_rejected')
                if name=='read_message_feed':reading.set();assert read_release.wait(3)
                result=super().call(name,args)
                if name=='open_message_feed':
                    opened.set()
                    if lost_open:
                        lost_open.pop();raise BoardReadError('timeout')
                if name=='read_message_feed':
                    delivered.extend(m['id'] for m in result['messages'])
                    if stop_after_read:raise BoardReadError('timeout')
                return result
        client=PinnedMessageClient(FeedClient(b,actor,target['board_session_id']),j,target,'owner',cancelled)
        with MessageJournal(j.directory/'messages') as journal:
            class NoModel:
                def run(self,*args,**kwargs):
                    model_calls.append(True);raise AssertionError('inference is forbidden')
            try:yield SimpleNamespace(client=client,journal=journal,worker=NoModel())
            finally:
                operations[:]=[dict(r) for r in journal.db.execute('SELECT message_id,state FROM operations')]
                ticks.set()
    def factory(j):
        supervisor=HostSupervisor(j);supervisor.message_resource_factory=resources;return supervisor
    client=BoardControl(b,actor);control=ServerController(host,client,supervisor_factory=factory,lock_directory=tmp_path/'locks')
    published=client.call('publish_server_catalog',{'host_id':remote['id'],'expected_catalog_version':0,
        'entries':catalog,'idempotency_key':'managed-catalog-'+digest(sorted(catalog,key=lambda e:e['catalog_key']))})
    host.bind_managed_instances({i['catalog_key']:i['id'] for i in published['instances']})
    def currents():return b.poll_instance_commands(actor,remote['id'])['instances']
    def current(target='primary'):return next(i for i in currents() if i['catalog_key']==target)
    def start(action='start',key='fixture-start',target='primary'):
        i=current(target);return b.command_server_instance(human,i['id'],action,i['version'],i['generation'],i['launchspec_sha256'],1,key)
    env=SimpleNamespace(board=b,human=human,profile=profile,host=host,control=control,current=current,currents=currents,start=start,
        opening=opening,release=release,opened=opened,ticks=ticks,operations=operations,model_calls=model_calls,
        thread=thread,calls=calls,fail_open=fail_open,lost_open=lost_open,delivered=delivered,
        reading=reading,read_release=read_release,stop_after_read=stop_after_read,entering=entering,resource_release=resource_release)
    try:yield env
    finally:release.set();read_release.set();resource_release.set();control.shutdown();host.close()


def test_message_target_stays_starting_until_feed_open_is_confirmed(messages):
    e=messages;c=e.control;c.start_worker();c.step();e.start();c.step();assert e.opening.wait(2)
    # Exact real worker is stopped immediately before the first open RPC.
    assert e.host.managed_target('primary')['enabled'];assert not e.opened.is_set()
    c.step();assert e.current()['observed_state']=='starting'
    message=e.board.post_message(e.human,e.thread['id'],'synthetic marker',
        mentions=['agent:'+e.profile['id']],idempotency_key='fixture-source')
    e.release.set();assert e.ticks.wait(2)
    assert e.opened.is_set() and e.operations==[] and e.model_calls==[]
    with e.board._connection() as db:
        feed=db.execute('SELECT initial_seq FROM message_feeds').fetchone()
        source=db.execute('SELECT seq FROM messages WHERE id=?',(message['id'],)).fetchone()
        assert feed['initial_seq']>=source['seq']
    assert e.host.managed_target('primary')['remaining']==1
    c.step();assert e.current()['observed_state']=='ready'


def test_pre_dispatch_needs_review_stops_managed_target_and_reports_review(messages):
    e=messages;e.fail_open.append(True);e.release.set()
    c=e.control;c.start_worker();c.step();e.start();c.step();assert e.ticks.wait(2)
    wait_for(lambda:not c._busy)
    assert c._last_tick['status']=='needs_review'
    assert not e.host.control()['enabled']
    target=e.host.managed_target('primary')
    assert not target['enabled'] and target['reason']=='needs_review' and target['remaining']==1
    assert e.host.dispatches()==[] and e.model_calls==[]
    c.step();assert e.current()['observed_state']=='needs_review'


def test_ready_after_feed_binding_precedes_read_and_preserves_a_new_message(messages):
    e=messages;e.release.set();e.read_release.clear();e.stop_after_read.append(True)
    c=e.control;c.start_worker();c.step();e.start();c.step();assert e.reading.wait(2)
    assert e.opened.is_set() and e.host.dispatches()==[]
    c.step();assert e.current()['observed_state']=='ready'
    # A confirmed open is sufficient; even an in-flight empty scan is not busy.
    source=e.board.post_message(e.human,e.thread['id'],'synthetic after-ready marker',
        mentions=['agent:'+e.profile['id']],idempotency_key='fixture-after-ready-source')
    e.read_release.set();assert e.ticks.wait(2)
    assert e.delivered==[source['id']] and e.host.dispatches()==[] and e.model_calls==[]
    assert e.host.managed_target('primary')['remaining']==1


def test_lost_open_reply_is_not_ready_until_exact_retry_confirms_it(messages):
    e=messages;e.release.set();e.lost_open.append(True)
    c=e.control;clock=[100.];c.clock=lambda:clock[0];c.start_worker();c.step();e.start();c.step()
    assert e.ticks.wait(2);wait_for(lambda:not c._busy)
    assert c._last_tick['reason']=='board_unavailable'
    c.step();assert e.current()['observed_state']=='starting'
    assert c._message_feeds=={} and e.model_calls==[]
    e.ticks.clear();clock[0]+=6.;c.step();assert e.ticks.wait(2)
    c.step();assert e.current()['observed_state']=='ready'
    assert e.calls.count('open_message_feed')==2
    with e.board._connection() as db:assert db.execute('SELECT COUNT(*) FROM message_feeds').fetchone()[0]==1
    assert e.host.managed_target('primary')['remaining']==1


def test_restart_cannot_reuse_previous_activation_readiness(messages):
    e=messages;e.release.set();c=e.control;c.start_worker();c.step();e.start();c.step()
    assert e.ticks.wait(2);wait_for(lambda:not c._busy);c.step()
    assert e.current()['observed_state']=='ready'
    old=c._message_feeds['primary'];e.entering.clear();e.resource_release.clear();e.ticks.clear()
    e.start('restart','fixture-restart');c.step();assert e.entering.wait(2)
    assert e.host.target_token('primary')!=old
    c.step();assert e.current()['observed_state']=='starting'
    # Stale or foreign callbacks cannot attest the new activation.
    c._message_feed_ready('primary',old);assert c._message_feeds['primary']==old
    e.resource_release.set();assert e.ticks.wait(2)
    c.step();assert e.current()['observed_state']=='ready'
    assert e.calls.count('open_message_feed')==1 and e.model_calls==[]


@pytest.mark.parametrize('messages',[16],indirect=True)
def test_sixteen_targets_each_require_their_own_confirmed_feed(messages):
    e=messages;c=e.control;c.start_worker();c.step()
    for i in e.currents():e.start(key='start-'+i['catalog_key'],target=i['catalog_key'])
    c.step();assert e.opening.wait(2)
    c.step();assert {i['observed_state'] for i in e.currents()}=={'starting'}
    e.release.set();assert e.ticks.wait(2)
    wait_for(lambda:len(c._message_feeds)==16)
    c.step();assert {i['observed_state'] for i in e.currents()}=={'ready'}
    assert len(c._message_feeds)==16 and e.calls.count('open_message_feed')==16
    assert e.host.dispatches()==[] and e.model_calls==[]
    assert all(e.host.managed_target(i['catalog_key'])['remaining']==1 for i in e.currents())
