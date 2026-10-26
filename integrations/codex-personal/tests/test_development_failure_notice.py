"""Real Board and client, synthetic transports/native/models; no live account."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
from unittest.mock import patch

import pytest

from board import Board, BoardError
from blackoutburst_agents import development_pool as pool_module
from blackoutburst_agents.board_reader import BoardReadError, PROTOCOL_VERSION
from blackoutburst_agents.development_client import DevelopmentBoardClient, failure_notice
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
import test_development_pool as pool_fixture
from test_development_ticket_frame_client import TOOL, Transport


@pytest.fixture
def case(tmp_path):
    board=Board(tmp_path/'board');board.ensure_owner('owner','Owner')
    human={'kind':'human','owner_id':'owner'}
    board.set_policy(human,True,True,True,True,['repo.read_file'],['repo:fixture'])
    project=board.create_project(human,'Project',['owner'],idempotency_key='project')
    ticket=board.create_ticket(human,project['id'],'Task',resource='repo:fixture',idempotency_key='ticket')
    device=board.create_device(human,'Synthetic',[]);raw=board.authenticate(device['token'])
    path=tmp_path/'connection.json'
    path.write_bytes(canonical({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_NOT_LIVE',
        'client':'codex','identity_key':'worker','name':'Worker'}));path.chmod(0o600)
    client=DevelopmentBoardClient.from_connection_file(path,session_id=str(uuid.uuid4()))
    calls=[];lose=[];alter=[lambda value:value]
    def rpc(method,params):
        calls.append((method,deepcopy(params)))
        if method=='initialize':return {'protocolVersion':PROTOCOL_VERSION}
        if method=='tools/list':return {'tools':[deepcopy(TOOL)]}
        assert method=='tools/call'
        name=params['name'];args=deepcopy(params['arguments']);args.pop('view',None)
        try:
            if name=='register_agent':value=board.register_agent(raw,**args)
            else:
                actor=board.actor_session(raw,args.pop('session_id'))
                value=getattr(board,name)(actor,**args)
        except BoardError:return {'isError':True,'content':[{'type':'text','text':'PRIVATE_SERVER_ERROR'}]}
        if name in lose:lose.remove(name);raise TimeoutError('PRIVATE_LOST_ACK')
        return {'structuredContent':alter[0](value) if name=='post_message' else value}
    client._rpc=rpc;registration=client.register_initial()
    actor=board.actor_session(raw,client.session_id)
    board.claim_ticket(actor,ticket['id'],ticket['version'])
    key='development-failure-notice-'+str(uuid.uuid4())
    yield SimpleNamespace(board=board,human=human,client=client,calls=calls,lose=lose,alter=alter,
        ticket=ticket,thread=ticket['thread_id'],key=key,registration=registration,raw=raw)
    client.close()


def publish(s,**kwargs):
    return s.client.post_failure_notice(thread_id=s.thread,idempotency_key=s.key,
        notice=failure_notice('development_context_changed'),**kwargs)


def messages(s):
    return s.board.get_thread(s.human,s.thread)['messages']


def test_post_is_closed_idempotent_and_cannot_change_claim_runtime_or_ticket(case):
    s=case;ticket=s.board.get_ticket(s.human,s.ticket['id']);runtime=s.board.get_runtime(s.human)
    before=len(s.calls);first=publish(s);second=publish(s)
    assert first==second and len(messages(s))==1
    assert s.board.get_ticket(s.human,s.ticket['id'])==ticket
    assert s.board.get_runtime(s.human)==runtime
    posts=s.calls[before:];assert len(posts)==2
    assert all(method=='tools/call' and params['name']=='post_message' for method,params in posts)
    body=messages(s)[0]['body'];data=json.loads(body.split('\n',1)[1])
    assert data=={'schema':1,'phase':'worker_run','code':'development_context_changed','cleanup_state':'unknown'}
    assert set(first)=={'message_id','thread_id','body_sha256'}
    with pytest.raises(BoardReadError):s.client.call('post_message',{})


def test_lost_ack_reuses_exact_key_and_payload_without_register_or_duplicate(case):
    s=case;s.lose.append('post_message');before=len(s.calls)
    with pytest.raises(TimeoutError):publish(s)
    assert len(messages(s))==1
    receipt=publish(s);assert receipt['message_id']==messages(s)[0]['id']
    posts=s.calls[before:];assert len(posts)==2 and posts[0]==posts[1]


@pytest.mark.parametrize('change',['messages_denied','profile_changed','profile_paused'])
def test_existing_permission_or_identity_refusal_has_no_fallback(case,change):
    s=case
    if change=='messages_denied':s.board.set_policy(s.human,True,True,False,True,['repo.read_file'],['repo:fixture'])
    else:
        p=s.board.get_agent_profile(s.human,s.registration['identity']['id'])
        fields={'instructions':'changed'} if change=='profile_changed' else {'status':'paused'}
        s.board.update_agent_profile(s.human,p['id'],p['version'],**fields)
    before=len(s.calls)
    with pytest.raises(BoardReadError):publish(s)
    assert not messages(s)
    assert [p['name'] for m,p in s.calls[before:]]==['post_message']


@pytest.mark.parametrize('change',['unregistered','uninitialized','closed','extra','raw_code','bool_schema','phase','cleanup','key'])
def test_malformed_or_unready_notice_is_refused_before_transport(case,change):
    s=case;notice=failure_notice('development_context_changed');args=dict(thread_id=s.thread,idempotency_key=s.key,notice=notice)
    if change=='unregistered':s.client._registered=False
    elif change=='uninitialized':s.client._initialized=False
    elif change=='closed':s.client.close()
    elif change=='extra':notice['raw']='PRIVATE_SECRET'
    elif change=='raw_code':notice['code']='PRIVATE_SECRET'
    elif change=='bool_schema':notice['schema']=True
    elif change=='phase':notice['phase']='PRIVATE_SECRET'
    elif change=='cleanup':notice['cleanup_state']=True
    else:args['idempotency_key']='new-arbitrary-key'
    before=len(s.calls)
    with pytest.raises(BoardReadError):s.client.post_failure_notice(**args)
    assert len(s.calls)==before and not messages(s)


@pytest.mark.parametrize('bad',[{}, {'id':'fake','thread_id':'wrong','body':'PRIVATE_SECRET','author':{}}])
def test_malformed_ack_remains_uncertain_and_same_key_recovers(case,bad):
    s=case;s.alter[0]=lambda _:bad
    with pytest.raises(BoardReadError):publish(s)
    assert len(messages(s))==1
    s.alter[0]=lambda value:value
    assert publish(s)['message_id']==messages(s)[0]['id']
    assert len(messages(s))==1


def test_arbitrary_reason_object_or_exception_is_never_converted_to_text():
    class Secret:
        def __str__(self):raise AssertionError('PRIVATE_SECRET must not be read')
    for value in (Secret(),RuntimeError('PRIVATE_SECRET'),{'private':'PRIVATE_SECRET'},'PRIVATE_SECRET'):
        notice=failure_notice(value,1)
        assert notice=={'schema':1,'phase':'worker_run','code':'development_run_requires_review','cleanup_state':'unknown'}
        assert b'PRIVATE' not in canonical(notice)


@pytest.fixture
def pool_case(monkeypatch):
    f=pool_fixture.PoolDevelopmentTests('runTest');f.setUp()
    transport=Transport(f);snapshots=[];lost=[False];clients=[];posts=[]
    def client(path,**kwargs):
        value=transport.client(path,**kwargs);clients.append(value);rpc=value._rpc
        def observed(method,params):
            if method=='tools/call' and params['name']=='post_message':
                thread=params['arguments']['thread_id']
                ticket_id=next(d['ticket']['id'] for d in f.data.values() if d['ticket']['thread_id']==thread)
                before=(f.board.get_ticket(f.human,ticket_id),f.board.get_runtime(f.human))
                result=rpc(method,params)
                after=(f.board.get_ticket(f.human,ticket_id),f.board.get_runtime(f.human))
                snapshots.append((before,after));posts.append(deepcopy(params))
                if lost[0]:lost[0]=False;raise TimeoutError('PRIVATE_LOST_ACK')
                return result
            return rpc(method,params)
        value._rpc=observed;return value
    patcher=patch.object(pool_module.DevelopmentBoardClient,'from_connection_file',side_effect=client);patcher.start()
    try:yield SimpleNamespace(f=f,transport=transport,clients=clients,snapshots=snapshots,lost=lost,posts=posts)
    finally:patcher.stop();f.doCleanups()


def test_real_pool_failed_run_posts_once_without_cleanup_or_claim_mutation(pool_case):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False
    f.pool.dispatch_pending();jobs=f.wait()
    assert jobs[0]['state']=='retained' and jobs[0]['outcome']=='needs_review'
    assert jobs[0]['cleanup_confirmed'] is False and f.pool.status()['capacity_held']==1
    assert len(s.posts)==1 and all(before==after for before,after in s.snapshots)
    body=s.posts[0]['arguments']['body'];assert json.loads(body.split('\n',1)[1])['cleanup_state']=='unknown'
    thread=f.board.get_thread(f.human,data['ticket']['thread_id'])
    assert len(thread['messages'])==1
    queue=f.base/'pool/workers'/job['worker_id']/'queue'
    intent=decode((queue/'development-failure-notice-intent.json').read_bytes())
    assert intent['idempotency_key']=='development-failure-notice-'+job['id']
    assert (queue/'development-failure-notice-receipt.json').is_file()
    assert f.board.get_ticket(f.human,data['ticket']['id'])['ticket']['claimant'] is not None


def test_pool_lost_ack_keeps_intent_and_explicit_notice_only_repeat_is_idempotent(pool_case,monkeypatch):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False;s.lost[0]=True
    original=pool_module._DevelopmentRun._failure_notice;states=[]
    def twice(run,code,cleaned=False):
        original(run,code,cleaned)
        queue=run.context.journals['queue'];states.append((queue/'development-failure-notice-receipt.json').exists())
        original(run,code,cleaned) # Explicit notification-only retry; no worker run/reconcile.
    monkeypatch.setattr(pool_module._DevelopmentRun,'_failure_notice',twice)
    f.pool.dispatch_pending();jobs=f.wait()
    assert states==[False] and len(s.posts)==2 and s.posts[0]==s.posts[1]
    assert len(f.board.get_thread(f.human,data['ticket']['thread_id'])['messages'])==1
    assert all(before==after for before,after in s.snapshots)
    assert jobs[0]['state']=='retained' and jobs[0]['cleanup_confirmed'] is False
    assert len(f.calls)==1 # Original failed synthetic turn was never repeated.


def test_notice_storage_error_cannot_change_run_error_instance(pool_case,monkeypatch):
    s=pool_case;f=s.f;job,data=f.selected();error=DevelopmentError('development_context_changed')
    from blackoutburst_agents.development_worker import DevelopmentBoardWorker
    original=DevelopmentBoardWorker.run
    def failed(worker,operation):
        original(worker,operation) # Real claim/journals/Runtime, synthetic model only.
        raise error
    monkeypatch.setattr(DevelopmentBoardWorker,'run',failed)
    create=pool_module.PrivateDirectory.create
    def broken(private,name,raw):
        if name=='development-failure-notice-intent.json':raise SystemExit('PRIVATE_NOTICE_FAILURE')
        return create(private,name,raw)
    monkeypatch.setattr(pool_module.PrivateDirectory,'create',broken)
    original_run=pool_module._DevelopmentRun.run;seen=[]
    def observed(run):
        try:return original_run(run)
        except BaseException as caught:seen.append(caught);raise
    monkeypatch.setattr(pool_module._DevelopmentRun,'run',observed)
    f.pool.dispatch_pending();jobs=f.wait()
    assert seen==[error] and seen[0] is error
    assert not s.posts and jobs[0]['state']=='retained'


def test_pool_message_denied_retains_exact_failure_without_auth_retry(pool_case,monkeypatch):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False
    original=pool_module.DevelopmentBoardClient.post_failure_notice;boundaries=[]
    def denied(client,**kwargs):
        boundaries.append(len(s.transport.calls))
        f.board.set_policy(f.human,True,True,False,True,
            ['repo.read_file','repo.patch_file','repo.run_test'],f.resources)
        return original(client,**kwargs)
    monkeypatch.setattr(pool_module.DevelopmentBoardClient,'post_failure_notice',denied)
    f.pool.dispatch_pending();jobs=f.wait()
    assert jobs[0]['state']=='retained' and jobs[0]['cleanup_confirmed'] is False
    assert not f.board.get_thread(f.human,data['ticket']['thread_id'])['messages']
    queue=f.base/'pool/workers'/job['worker_id']/'queue'
    assert (queue/'development-failure-notice-intent.json').is_file()
    assert not (queue/'development-failure-notice-receipt.json').exists()
    assert len(boundaries)==1
    assert not any(p['name']=='register_agent' for m,p in s.transport.calls[boundaries[0]:] if m=='tools/call')


@pytest.mark.parametrize('reason',['development_context_changed','PRIVATE_UNKNOWN_REASON'])
def test_known_run_exception_notified_with_closed_projection_and_same_original(pool_case,monkeypatch,reason):
    s=pool_case;f=s.f;job,data=f.selected();error=DevelopmentError(reason)
    from blackoutburst_agents.development_worker import DevelopmentBoardWorker
    original=DevelopmentBoardWorker.run;seen=[]
    def failed(worker,operation):original(worker,operation);raise error
    monkeypatch.setattr(DevelopmentBoardWorker,'run',failed)
    run=pool_module._DevelopmentRun.run
    def observed(value):
        try:return run(value)
        except BaseException as caught:seen.append(caught);raise
    monkeypatch.setattr(pool_module._DevelopmentRun,'run',observed)
    f.pool.dispatch_pending();jobs=f.wait()
    assert seen==[error] and seen[0] is error
    assert jobs[0]['state']=='retained' and jobs[0]['cleanup_confirmed'] is None
    assert len(s.posts)==1 and all(before==after for before,after in s.snapshots)
    body=s.posts[0]['arguments']['body'];notice=json.loads(body.split('\n',1)[1])
    assert notice['code']==(reason if reason=='development_context_changed' else 'development_run_requires_review')
    assert notice['cleanup_state']=='unknown' and 'PRIVATE' not in body


def test_conflicting_notice_intent_never_uses_a_new_key_or_mutates_old_evidence(pool_case,monkeypatch):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False
    original=pool_module._DevelopmentRun._failure_notice
    conflict=canonical({'private':'PRIVATE_EXISTING_INTENT'})
    def conflicting(run,code,cleaned=False):
        with pool_module.PrivateDirectory(run.context.journals['queue']) as private:
            private.create('development-failure-notice-intent.json',conflict)
        original(run,code,cleaned)
    monkeypatch.setattr(pool_module._DevelopmentRun,'_failure_notice',conflicting)
    f.pool.dispatch_pending();jobs=f.wait()
    assert jobs[0]['state']=='retained' and jobs[0]['cleanup_confirmed'] is False
    assert not s.posts
    queue=f.base/'pool/workers'/job['worker_id']/'queue'
    assert (queue/'development-failure-notice-intent.json').read_bytes()==conflict


def test_notice_uses_one_closed_snapshot_when_caller_mutates_before_serialization(case,monkeypatch):
    # Regression for the independent review's concurrent-mutation finding.
    from blackoutburst_agents import development_client as client_module
    s=case;notice=failure_notice('development_context_changed');original=client_module.canonical
    def mutate_before_encoding(value):
        notice['raw']='PRIVATE_CONCURRENT_CALLER_DATA'
        notice['code']='PRIVATE_CHANGED_REASON'
        return original(value)
    monkeypatch.setattr(client_module,'canonical',mutate_before_encoding)
    receipt=s.client.post_failure_notice(thread_id=s.thread,idempotency_key=s.key,notice=notice)
    assert receipt['message_id']==messages(s)[0]['id']
    body=messages(s)[0]['body']
    assert 'PRIVATE' not in body
    assert json.loads(body.split('\n',1)[1])==failure_notice('development_context_changed')
    assert s.calls[-1][1]['arguments']['view']=='full'


def test_review_ready_with_unconfirmed_cleanup_posts_unknown_without_changing_pool_outcome(pool_case,monkeypatch):
    s=pool_case;f=s.f;job,data=f.selected()
    def unavailable(*args,**kwargs):raise DevelopmentError('development_pool_cleanup_unconfirmed')
    monkeypatch.setattr(f.driver,'_cleanup',unavailable)
    f.pool.dispatch_pending();jobs=f.wait()
    assert jobs[0]['state']=='retained' and jobs[0]['outcome']=='complete'
    assert jobs[0]['cleanup_confirmed'] is False and f.pool.status()['capacity_held']==1
    assert len(s.posts)==1 and all(before==after for before,after in s.snapshots)
    notice=json.loads(s.posts[0]['arguments']['body'].split('\n',1)[1])
    assert notice['code']=='development_cleanup_requires_review' and notice['cleanup_state']=='unknown'
    assert len(f.calls)==2 # Real successful synthetic turns are never repeated.


@pytest.mark.parametrize('code',['planner_turns_budget_exhausted','planner_actions_budget_exhausted',
    'planner_bytes_budget_exhausted'])
def test_real_planner_budget_refusal_codes_survive_the_closed_wire_projection(case,code):
    s=case;before=s.board.get_ticket(s.human,s.ticket['id']);runtime=s.board.get_runtime(s.human)
    s.client.post_failure_notice(thread_id=s.thread,idempotency_key=s.key,notice=failure_notice(code))
    data=json.loads(messages(s)[0]['body'].split('\n',1)[1])
    assert data=={'schema':1,'phase':'worker_run','code':code,'cleanup_state':'unknown'}
    assert s.board.get_ticket(s.human,s.ticket['id'])==before
    assert s.board.get_runtime(s.human)==runtime
