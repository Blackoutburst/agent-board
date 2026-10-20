"""Real Board/SQLite control channel, deterministic workers, no provider calls."""
import copy
import threading
import time

import pytest
from board import Board,BoardError
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_config import HostError,validate,digest
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.server_controller import ServerController,launch_catalog
from blackoutburst_agents.server_control_protocol import arguments,response
from test_host_journal import configuration


class BoardControl:
    def __init__(self,board,actor):self.board,self.actor=board,actor;self.calls=[];self.lost=set();self.offline=False
    def call(self,name,args):
        self.calls.append((name,copy.deepcopy(args)))
        if self.offline:raise BoardReadError('connection_failed')
        try:result=getattr(self.board,name)(self.actor,**args)
        except BoardError:raise BoardReadError('tool_rejected') from None
        if name in self.lost:self.lost.remove(name);raise BoardReadError('timeout')
        return result


@pytest.fixture
def env(tmp_path):
    b=Board(tmp_path/'board');b.ensure_owner('owner','Owner');human={'kind':'human','owner_id':'owner'}
    b.set_policy(human,True,True,True,True,[],[])
    d=b.create_device(human,'Server',[]);actor=b.authenticate(d['token'])
    p=b.create_agent_profile(human,'Worker',policy={'allow_read':True,'allow_messages':True,'allow_requests':True,
        'actions':[],'resources':[],'autonomy':'requested'})
    p=b.bind_agent_profile(human,p['id'],p['version'],d['id'],'worker','codex')
    host=b.create_server_host(human,d['id'],'Owner server','grant')
    cfg=configuration(tmp_path);cfg['dispatch_budget']=3
    cfg['targets'][0].update(agent_id=p['id'],profile_version=p['version'])
    cfg=validate(cfg,bind_connections=True)
    journal=HostJournal(tmp_path/'host');journal.configure(cfg)
    catalog=launch_catalog(cfg,'b'*64)
    with journal.owner_lock(lock_directory=tmp_path/'locks'):journal.prepare_managed(host['id'],catalog)
    client=BoardControl(b,actor)
    class IdleWorker:
        def __init__(self,j):self.journal=j
        def tick(self,**kw):return {'status':'idle','reason':'no_pending_events'}
    control=ServerController(journal,client,supervisor_factory=IdleWorker,lock_directory=tmp_path/'locks')
    published=client.call('publish_server_catalog',{'host_id':host['id'],'expected_catalog_version':0,
        'entries':catalog,'idempotency_key':'managed-catalog-'+digest(sorted(catalog,key=lambda e:e['catalog_key']))})
    journal.bind_managed_instances({i['catalog_key']:i['id'] for i in published['instances']})
    e={'board':b,'human':human,'actor':actor,'profile':p,'device':d,'remote':host,'journal':journal,
       'control':control,'client':client,'catalog':catalog,'root':tmp_path}
    try:yield e
    finally:control.shutdown();journal.close()


def current(e):return e['board'].poll_instance_commands(e['actor'],e['remote']['id'])['instances'][0]
def command(e,action='start',key='start'):
    i=current(e)
    return e['board'].command_server_instance(e['human'],i['id'],action,i['version'],i['generation'],
        i['launchspec_sha256'],0 if action=='stop' else 1,key)
def wait_for(fn):
    deadline=time.monotonic()+3
    while time.monotonic()<deadline:
        if fn():return
        time.sleep(.01)
    assert fn()


def test_catalog_contains_only_opaque_pins_no_private_launch_values(env):
    wire=str(env['catalog'])
    assert 'PRIVATE_INSTRUCTION' not in wire and 'connection' not in wire and str(env['root']) not in wire
    changed=copy.deepcopy(env['journal'].control()['configuration']);changed['targets'][0]['model']='other'
    assert launch_catalog(changed,'b'*64)!=env['catalog']
    assert launch_catalog(env['journal'].control()['configuration'],'c'*64)!=env['catalog']


def test_empty_controller_does_not_activate_a_target_or_open_a_feed(env):
    control=env['control'];control.start_worker()
    for _ in range(4):assert control.step()['model_calls_by_control_loop']==0
    assert not env['journal'].target_selected('primary')
    assert env['journal'].dispatches()==[] and current(env)['observed_state']=='stopped'
    assert set(n for n,_ in env['client'].calls)<={'publish_server_catalog','poll_instance_commands','report_instance_state'}


def test_real_commands_start_stop_and_restart_wait_for_local_observation(env):
    c=env['control'];c.start_worker();c.step();command(env)
    c.step();wait_for(lambda:env['journal'].managed_target('primary')['enabled'])
    c.step();assert current(env)['observed_state']=='ready'
    command(env,'restart','restart');c.step()
    wait_for(lambda:env['journal'].managed_target('primary')['generation']==2 and env['journal'].managed_target('primary')['enabled'])
    c.step();assert current(env)['observed_state']=='ready'
    command(env,'stop','stop');c.step();wait_for(lambda:not env['journal'].managed_target('primary')['enabled'])
    c.step();assert current(env)['observed_state']=='stopped' and current(env)['cleanup_confirmed']


def test_lost_ack_uses_original_cas_and_key_without_new_budget(env):
    c=env['control'];c.start_worker();c.step();cmd=command(env)
    env['client'].lost.add('ack_instance_command')
    with pytest.raises(BoardReadError):c.step()
    assert not env['journal'].managed_target('primary')['enabled']
    c.step();wait_for(lambda:env['journal'].managed_target('primary')['enabled'])
    a=[a for n,a in env['client'].calls if n=='ack_instance_command']
    assert len(a)==2 and a[0]==a[1] and a[0]['expected_revision']==1
    assert env['journal'].managed_target('primary')['remaining']==cmd['budget']


def test_stop_supplants_start_even_if_ack_response_was_lost(env):
    c=env['control'];c.start_worker();c.step();command(env);env['client'].lost.add('ack_instance_command')
    with pytest.raises(BoardReadError):c.step()
    command(env,'stop','stop');c.step()
    assert not env['journal'].managed_target('primary')['enabled']
    assert current(env)['observed_state']=='stopped'


def test_lost_report_retries_original_observation_revision(env):
    c=env['control'];c.start_worker();env['client'].lost.add('report_instance_state')
    with pytest.raises(BoardReadError):c.step()
    c.step()
    reports=[a for n,a in env['client'].calls if n=='report_instance_state']
    assert reports[0]==reports[1] and reports[0]['expected_observation_revision']==0
    assert current(env)['observed_state']=='stopped'


def test_network_loss_disables_activated_targets_and_old_command_does_not_reactivate(env):
    c=env['control'];c.start_worker();c.step();command(env);c.step()
    wait_for(lambda:env['journal'].managed_target('primary')['enabled']);c.step()
    env['client'].offline=True
    with pytest.raises(BoardReadError):c.step()
    assert not env['journal'].managed_target('primary')['enabled']
    env['client'].offline=False;c.step();assert current(env)['observed_state']=='stopped'
    for _ in range(3):c.step()
    assert not env['journal'].managed_target('primary')['enabled']


def test_stop_reaches_busy_worker_without_waiting_for_tick_completion(env):
    c=env['control'];began=threading.Event();cancelled=threading.Event()
    class Busy:
        def __init__(self,j):self.journal=j
        def tick(self,*,should_cancel):
            began.set()
            while not should_cancel() and self.journal.managed_target('primary')['enabled']:time.sleep(.01)
            cancelled.set();return {'status':'disabled','reason':'stopped'}
    c.factory=Busy;c.start_worker();c.step();command(env);c.step();assert began.wait(2)
    command(env,'stop','stop');c.step();assert cancelled.wait(2)
    c.step();assert current(env)['observed_state']=='stopped'


def test_worker_authority_expires_when_control_loop_is_stalled(env):
    c=env['control'];now=[10.];c.clock=lambda:now[0];c.start_worker();c.step();command(env);c.step()
    wait_for(lambda:env['journal'].managed_target('primary')['enabled'])
    now[0]=26.;wait_for(lambda:not env['journal'].managed_target('primary')['enabled'])


def test_slow_report_does_not_extend_authority_from_an_old_poll(env):
    c=env['control'];now=[10.];c.clock=lambda:now[0];c.start_worker();c.step();command(env)
    original=env['client'].call
    def delayed(name,args):
        result=original(name,args)
        if name=='report_instance_state' and args['state']=='starting':now[0]+=20.
        return result
    env['client'].call=delayed
    with pytest.raises(HostError,match='managed_authority_expired'):c.step()
    assert now[0]==30. and c._cancelled() and not c._commands
    assert not env['journal'].target_selected('primary')


def test_worker_activation_transition_cannot_be_reported_clean_stopped(env,monkeypatch):
    c=env['control'];c.start_worker();c.step();cmd=command(env)
    applying=threading.Event();release=threading.Event()
    original=HostJournal.apply_managed_command
    def paused(j,identity):
        if identity==cmd['id']:
            applying.set();assert release.wait(3)
        return original(j,identity)
    monkeypatch.setattr(HostJournal,'apply_managed_command',paused)
    try:
        c.step();assert applying.wait(2)
        assert c._busy and not env['journal'].managed_target('primary')['enabled']
        c.step();i=current(env)
        assert not i['cleanup_confirmed'] and i['observed_state']=='starting'
    finally:release.set()
    wait_for(lambda:env['journal'].managed_target('primary')['enabled'])


def test_pending_child_is_never_reported_clean_or_restarted(env):
    c=env['control'];c.start_worker();c.step();command(env);c.step()
    wait_for(lambda:env['journal'].managed_target('primary')['enabled'])
    # Simulated crash after durable reservation, before a child could finish.
    # The controller must preserve this uncertainty even with no busy thread.
    with HostJournal(env['journal'].directory) as j:
        j._db.execute("INSERT INTO dispatches VALUES('batch','primary','request','sub','11111111-1111-4111-8111-111111111111','pending','{}',?)",(j.control()['activation'],))
    c.step();i=current(env)
    assert i['observed_state']=='needs_review' and not i['cleanup_confirmed']
    assert env['journal'].dispatches()[0]['state']=='pending'


def test_profile_pause_stops_work_but_retains_control_channel(env):
    c=env['control'];c.start_worker();c.step();command(env);c.step()
    wait_for(lambda:env['journal'].managed_target('primary')['enabled']);c.step()
    p=env['board'].get_agent_profile(env['human'],env['profile']['id'])
    env['board'].update_agent_profile(env['human'],p['id'],p['version'],status='paused')
    c.step();assert not env['journal'].managed_target('primary')['enabled']
    assert current(env)['observed_state']=='stopped' and not current(env)['can_start']


@pytest.mark.parametrize('lost_ack',[False,True])
def test_permission_withdrawal_settles_unstarted_command_even_with_lost_ack(env,lost_ack):
    c=env['control'];c.start_worker();c.step();command(env)
    if lost_ack:
        env['client'].lost.add('ack_instance_command')
        with pytest.raises(BoardReadError):c.step()
    env['board'].set_policy(env['human'],False,False,False,False,[],[])
    c.step();i=current(env)
    assert i['observed_state']=='stopped' and i['cleanup_confirmed']
    assert i['current_command']['status']=='rejected'
    assert not env['journal'].managed_target('primary')['enabled']


@pytest.mark.parametrize('state',['stopped','stopping'])
def test_completed_activation_cannot_revive_after_stop_without_cleanup(env,state):
    c=env['control'];c.start_worker();c.step();command(env);c.step()
    wait_for(lambda:env['journal'].managed_target('primary')['enabled']);c.step();i=current(env)
    args=dict(host_id=env['remote']['id'],instance_id=i['id'],generation=i['generation'],launchspec_sha256=i['launchspec_sha256'],
        command_id=i['command_id'],expected_observation_revision=i['observation_revision'],state=state,
        cleanup_confirmed=False,reason='cleanup_unconfirmed',idempotency_key='stop-uncertain')
    r=env['board'].report_instance_state(env['actor'],**args)['instance']
    args.update(expected_observation_revision=r['observation_revision'],state='ready',reason='',idempotency_key='revive')
    with pytest.raises(BoardError,match='not_authorized'):env['board'].report_instance_state(env['actor'],**args)


def test_closed_metadata_refuses_shell_prompt_and_session(env):
    with pytest.raises(HostError):arguments('poll_instance_commands',{'host_id':'h','limit':20,'session_id':'s'})
    with pytest.raises(HostError):arguments('exec',{'command':'anything'})
    with pytest.raises(HostError):response('poll_instance_commands',{'instructions':'do anything'})


def test_manager_device_revocation_is_absolute(env):
    c=env['control'];c.start_worker();c.step()
    env['board'].revoke_device(env['human'],env['device']['id'])
    with pytest.raises(BoardReadError):c.step()
    assert not env['journal'].control()['enabled']


def test_shutdown_of_rejected_second_controller_never_stops_the_live_owner(env):
    first=env['control'];first.start_worker();first.step();command(env);first.step()
    wait_for(lambda:env['journal'].target_selected('primary'));first.step()
    with HostJournal(env['journal'].directory) as journal:
        second=ServerController(journal,env['client'],lock_directory=env['root']/'locks')
        with pytest.raises(HostError,match='managed_worker_unavailable'):second.start_worker()
        assert env['journal'].target_selected('primary')
        second.shutdown();assert env['journal'].target_selected('primary')
        with pytest.raises(HostError,match='managed_worker_not_owned'):second.step()


def test_late_shutdown_of_old_object_never_stops_its_successor(env):
    first=env['control'];first.start_worker();first.step();command(env);first.step()
    wait_for(lambda:env['journal'].target_selected('primary'));first.step();assert first.shutdown()
    with HostJournal(env['journal'].directory) as journal:
        second=ServerController(journal,env['client'],supervisor_factory=first.factory,lock_directory=env['root']/'locks')
        try:
            second.start_worker();second.step();command(env,key='explicit-new-generation');second.step()
            wait_for(lambda:journal.target_selected('primary'))
            first.shutdown();assert journal.target_selected('primary')
        finally:second.shutdown()


@pytest.mark.parametrize('boundary',['publish_server_catalog','poll_instance_commands','ack_instance_command','report_instance_state'])
def test_old_rpc_completion_cannot_mutate_the_successor_after_handoff(env,boundary):
    old=env['control'];old.start_worker();old.step();command(env)
    original=env['client'].call;successors=[];captured=[]
    def handoff(name,args):
        result=original(name,args)
        if name==boundary and not captured:
            captured.append(result);assert old.shutdown()
            state=HostJournal(env['journal'].directory)
            new=ServerController(state,BoardControl(env['board'],env['actor']),
                supervisor_factory=old.factory,lock_directory=env['root']/'locks')
            successors.append((new,state));new.start_worker();new.publish(env['catalog'])
            command(env,'stop','successor-stop');new.step()
            command(env,'start','successor-new-start');new.step()
            wait_for(lambda:state.target_selected('primary'));new.step()
            assert state.managed_target('primary')['generation']==2
        return result
    env['client'].call=handoff
    try:
        with pytest.raises(HostError,match='managed_worker_not_owned'):
            if boundary=='publish_server_catalog':old.publish(env['catalog'])
            else:old.step()
        assert captured and env['journal'].target_selected('primary')
        assert env['journal'].managed_target('primary')['generation']==2
    finally:
        env['client'].call=original
        for new,state in successors:new.shutdown();state.close()
