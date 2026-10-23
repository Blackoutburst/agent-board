"""Real Board/Git + SDK frames; only transport latency/native/model are synthetic."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest import TestCase
import uuid

import pytest

from board import BoardError
from development_ticket_frames import get_development_ticket_frame
from test_linux_pool_preparation import Fixture
from blackoutburst_agents import linux_pool_preparation as preparation
from blackoutburst_agents.board_reader import BoardReadError,PROTOCOL_VERSION
from blackoutburst_agents.context import ContextScope,build_context
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from blackoutburst_agents.dev_test_supervisor import _fresh_control_window
from blackoutburst_agents.family_client import FamilyBoardClient
from blackoutburst_agents.linux_test_backend import _QualificationContext

TOOL={'name':'get_development_ticket_frame','inputSchema':{'type':'object',
    'properties':{'session_id':{'type':'string'},'ticket_id':{'type':'string'}},
    'required':['session_id','ticket_id'],'additionalProperties':False}}


class Transport:
    def __init__(self,fixture):
        self.f=fixture;self.clock=100.;self.cost=.1;self.calls=[];self.frame_calls=[]
        self.before_frame=lambda:None;self.after_frame=lambda value:value;self.catalogue=[TOOL]

    def client(self,path,*,session_id,timeout):
        f=self.f
        value=DevelopmentBoardClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_NOT_LIVE',
            'client':'codex','identity_key':'worker','name':'Worker'},session_id,timeout)
        value.connection_identity=file_document(path)[1]['identity']
        def rpc(method,params):
            self.calls.append((method,deepcopy(params)))
            if method=='initialize':result={'protocolVersion':PROTOCOL_VERSION}
            elif method=='tools/list':result={'tools':deepcopy(self.catalogue)}
            else:
                assert method=='tools/call'
                name=params['name'];args=deepcopy(params['arguments']);args.pop('view',None)
                try:
                    if name=='register_agent':payload=f.board.register_agent(f.raw,**args)
                    elif name=='get_development_ticket_frame':
                        self.before_frame();self.frame_calls.append(deepcopy(args))
                        payload=get_development_ticket_frame(f.board,lambda sid:f.board.actor_session(f.raw,sid),**args)
                        payload=self.after_frame(payload)
                    else:
                        actor=f.board.actor_session(f.raw,args.pop('session_id'))
                        if name=='update_ticket':args.update(args.pop('changes'))
                        payload=getattr(f.board,name)(actor,**args)
                    result={'structuredContent':payload}
                except BoardError:result={'isError':True,'content':[{'type':'text','text':'FIXED synthetic refusal'}]}
            self.clock+=self.cost
            return result
        value._rpc=rpc
        f.clients.append(value)
        return value


@pytest.fixture
def framed(tmp_path,monkeypatch):
    f=Fixture(tmp_path,monkeypatch);transport=Transport(f)
    monkeypatch.setattr(preparation.DevelopmentBoardClient,'from_connection_file',transport.client)
    yield f,transport
    f.pool.close()


def test_preclaim_qualification_source_frames_keep_ttl_with_slow_stable_rpc(framed,monkeypatch):
    f,t=framed;job,ticket,args=f.selected();windows=[]
    def qualify(backend):
        producer=SimpleNamespace(_authorize=lambda:backend.authorize(backend.manifest['scope'],
            'repo.run_test',backend.manifest['resource']))
        context=_QualificationContext(producer,'normal');t.clock=100.;t.frame_calls.clear()
        first=_fresh_control_window(context.authorize,nonce='a'*32,sequence=1,clock=lambda:t.clock)
        t.clock+=.4  # Same synthetic setup/READY cost as the failing legacy repro.
        source=_fresh_control_window(context.before_source,nonce='a'*32,sequence=2,previous=first,clock=lambda:t.clock)
        windows.extend((first,source))
        with PrivateDirectory(backend.directory) as private:private.replace('qualification.json',canonical({'synthetic':True}))
    monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',qualify)
    result=f.prepare(job,args)
    assert result['status']=='bound'
    assert windows[0]=={'schema':1,'nonce':'a'*32,'sequence':1,'started_at':100.,'validated_at':pytest.approx(100.1),'deadline':101.5}
    assert windows[1]['validated_at']==pytest.approx(100.6) and windows[1]['deadline']==pytest.approx(102.)
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None
    assert all(params['name']=='get_development_ticket_frame' for method,params in t.calls
        if method=='tools/call' and params['name']!='register_agent')


def authority(f,t,job,ticket,args):
    client=t.client(f.connection,session_id=job['session_id'],timeout=3)
    registration=client.register_initial()
    scope={**f.driver.selections.fixed,'project_id':ticket['project_id'],'ticket_id':ticket['id'],
        'board_session_id':job['session_id']}
    manifest={'scope':{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')},
        'actions':args['actions'],'resource':args['resource']}
    manifest['scope'].update(session_id=job['session_id'],device_id=registration['identity']['device_id'],
        profile_version=registration['profile_version'])
    return DevelopmentAuthorization(client,scope=scope,manifest=manifest,registration=registration)


def test_single_frame_has_the_exact_legacy_context_sources(framed):
    f,t=framed;job,ticket,args=f.selected();a=authority(f,t,job,ticket,args)
    current=a.snapshot()
    legacy=build_context(a.client.call,scope=ContextScope(**{key:a.scope[key] for key in
        ('owner_id','agent_id','project_id','ticket_id')}),registration=a.registration)
    assert current['context']==legacy
    assert current['selected_ticket']['id']==ticket['id']


@pytest.mark.parametrize('change',['policy','profile','device','ticket','context','stop'])
def test_source_frame_refuses_revocation_or_drift_without_fallback(framed,change):
    f,t=framed;job,ticket,args=f.selected();a=authority(f,t,job,ticket,args)
    f.board.claim_ticket(f.board.actor_session(f.raw,job['session_id']),ticket['id'],ticket['version'])
    a.seal(a.snapshot(require_claim=True));before=len(t.calls)
    if change=='policy':f.board.set_policy(f.human,True,True,True,False,f.actions,['repo:fixture'])
    elif change=='profile':f.board.update_agent_profile(f.human,f.agent,f.profile_version,instructions='changed')
    elif change=='device':f.board.revoke_device(f.human,f.device)
    elif change=='ticket':f.update(ticket,description='changed')
    elif change=='context':
        p=f.board.get_project(f.human,f.project['id']);f.board.update_project(f.human,p['id'],p['version'],context='changed')
    else:a.stop_requested=lambda:True
    with pytest.raises((BoardReadError,DevelopmentError)):a.snapshot(require_claim=True)
    assert all(params['name']=='get_development_ticket_frame' for method,params in t.calls[before:] if method=='tools/call')


@pytest.mark.parametrize('change',['missing','wrong_session','wrong_version','oversized','bad_pages'])
def test_malformed_partial_frame_never_falls_back(framed,change):
    f,t=framed;job,ticket,args=f.selected();a=authority(f,t,job,ticket,args)
    def malformed(value):
        if change=='missing':value['selected'].pop('last')
        elif change=='wrong_session':value['selection']['session_id']='other'
        elif change=='wrong_version':value['selection']['profile_version']=True
        elif change=='oversized':value['selected']['first']['private']='x'*(384*1024)
        else:value['selected']['pages']=[]
        return value
    t.after_frame=malformed;before=len(t.calls)
    with pytest.raises(BoardReadError):a.snapshot()
    assert [params['name'] for method,params in t.calls[before:] if method=='tools/call']==['get_development_ticket_frame']


def test_local_stop_during_rpc_refuses_the_returned_frame(framed):
    f,t=framed;job,ticket,args=f.selected();a=authority(f,t,job,ticket,args)
    def stop(value):a.stop_requested=lambda:True;return value
    t.after_frame=stop;before=len(t.calls)
    with pytest.raises(DevelopmentError,match='development_stop_requested'):a.snapshot()
    assert [params['name'] for method,params in t.calls[before:] if method=='tools/call']==['get_development_ticket_frame']


def test_lost_frame_response_never_reconstructs_a_sequential_snapshot(framed):
    f,t=framed;job,ticket,args=f.selected();a=authority(f,t,job,ticket,args)
    def timeout(_):raise TimeoutError('synthetic lost response')
    t.after_frame=timeout;before=len(t.calls)
    with pytest.raises(TimeoutError):a.snapshot()
    assert [params['name'] for method,params in t.calls[before:] if method=='tools/call']==['get_development_ticket_frame']


@pytest.mark.parametrize('catalogue',[[],[{**TOOL,'inputSchema':{'properties':{},'required':[]}}],[TOOL,TOOL]])
def test_support_probe_refuses_before_qualification_binding_or_model(framed,catalogue):
    f,t=framed;job,ticket,args=f.selected();t.catalogue=catalogue
    with pytest.raises(BoardReadError,match='development_ticket_frame_unavailable'):f.prepare(job,args)
    assert not t.frame_calls and 'backend' not in f.events and 'policy' not in f.events
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None


def test_family_client_does_not_select_or_probe_standalone_frame():
    client=FamilyBoardClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_NOT_LIVE',
        'client':'codex','identity_key':'worker','name':'Worker'},'fixed-session',3)
    client._rpc=Mock(side_effect=AssertionError('Family protocol must remain separately selected'))
    client.adopt_registration()
    assert client.development_ticket_frame is False
    client._rpc.assert_not_called()


def test_real_named_test_source_and_continuation_use_fresh_standalone_frames(framed):
    """Real planner/broker/lease/five captures; native/model transport are doubles."""
    from linux_lock_fixture import LockObservationFixture
    from test_linux_connected_execution import ConnectedExecutionTests
    from test_dev_planner import FakePolicy,FakeControl
    from blackoutburst_agents.dev_planner import DevelopmentPlanner
    from blackoutburst_agents.run_store import RunStore
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    f,t=framed;job,ticket,args=f.selected();a=authority(f,t,job,ticket,args)
    f.board.claim_ticket(f.board.actor_session(f.raw,job['session_id']),ticket['id'],ticket['version'])
    a.seal(a.snapshot(require_claim=True))
    cleanup=TestCase();lock_fixture=LockObservationFixture(cleanup)
    connected=ConnectedExecutionTests();connected.scope_override=a.manifest['scope'];connected.lock_fixture=lock_fixture
    try:
        connected.setUp()
        connected.broker.authorize_scope=a.allowed;connected.backend.authorize_scope=a.allowed
        store=RunStore(connected.base/'runs');cleanup.addCleanup(store.close)
        runtime=RuntimeJournal(connected.base/'runtime');cleanup.addCleanup(runtime.close)
        scope={key:connected.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
        scope.update(board_endpoint=a.scope['board_endpoint'],board_session_id=connected.scope['session_id'])
        control=FakeControl(scope,connected.scope);control.session_id=connected.scope['session_id']
        workspace=connected.base/'planner-empty';workspace.mkdir(mode=0o700)
        actions=[{'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'Verified.'}]
        def transport(settings,prompt,workspace,**callbacks):
            callbacks['on_started'](999999999)
            if 'before_prompt' in callbacks:callbacks['before_prompt']()
            session=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](session)
            return {'status':'completed','reason':'verified','session_id':session,
                'text':canonical(actions.pop(0)).decode(),'verification':dict(cleanup_confirmed=True,
                    tool_free=True,session_verified=True,turn_completed=True,exit_zero=True,authentication='chatgpt')}
        observations=[]
        def run(attempt):
            t.clock=100.;t.frame_calls.clear()
            first=_fresh_control_window(attempt.authorize,nonce='a'*32,sequence=1,clock=lambda:t.clock)
            t.clock+=.4
            source=_fresh_control_window(attempt.before_source,nonce='a'*32,sequence=2,previous=first,clock=lambda:t.clock)
            continuation=_fresh_control_window(attempt.authorize,nonce='a'*32,sequence=3,previous=source,clock=lambda:t.clock)
            observations.append((len(t.frame_calls),first,source,continuation))
            return connected.native(attempt.request)
        connected.backend.run.side_effect=run
        planner=DevelopmentPlanner(broker=connected.broker,store=store,runtime_journal=runtime,
            control_client=control,board_scope=scope,planner_policy=FakePolicy(workspace),authorization=a,
            claim_id=a.claim_id,instruction='Run the approved named test.',model='fixture-model',enabled=True,
            _test_anchor=connected.base/'planner-anchor',_transport=transport,_allow_fixture_backend=True,_clock=lambda:100.)
        cleanup.addCleanup(planner.close)
        result=planner.run()
        assert result['status']=='review_ready',result
        assert len(observations)==1 and observations[0][0]==3
        _,first,source,continuation=observations[0]
        assert first['deadline']==101.5 and first['validated_at']==pytest.approx(100.1)
        assert source['validated_at']==pytest.approx(100.6) and continuation['validated_at']==pytest.approx(100.7)
        state=connected.state()
        assert next(iter(state['linux_execution'].values()))['phase']=='broker_admitted'
        assert state['used']['tests']==1 and state['used']['read_bytes']>=5*65537
    finally:
        # Consumers close before their backing fixture and the lock observer.
        cleanup.doCleanups();connected.doCleanups()
