"""Exact opt-in queue admission against the real Board transaction and MCP."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest

from board import BoardError
from test_coordination import space, new
from test_coordination_api import browser, rpc
from test_delegation_families import family, open_family
from test_queue_selection import snapshot


def select(board, actor, ticket, key='selected'):
    return board.claim_selected_queue_ticket(actor, ticket['id'], ticket['version'], key)


def independent(board, human, name='separate'):
    device=board.create_device(human,name,[])
    raw=board.authenticate(device['token'])
    board.register_agent(raw,name,name,['python'],'codex',True,name)
    return board.actor_session(raw,name)


def test_exact_selection_never_chooses_higher_priority_work_or_launches_runtime(space):
    board,human,_,_,first,*_=space
    urgent=new(space,'Urgent other',priority='urgent',resource='other')
    ticket=new(space,'Explicit work',resource='selected')
    result=select(board,first,ticket)
    assert result['claimed'] and not result['retried']
    assert result['ticket']['id']==ticket['id'] and result['ticket']['version']==ticket['version']+1
    assert result['ticket']['claimant']['session_id']==first['session_id']
    assert board.get_ticket(human,urgent['id'])['ticket']['claimant'] is None
    assert board.get_runtime(human)['executions']==[]
    with board._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==18
        assert db.execute("SELECT COUNT(*) FROM ticket_events WHERE event='ticket_claimed'").fetchone()[0]==1


@pytest.mark.parametrize('same_ticket',[False,True])
def test_two_sessions_same_profile_cannot_admit_two_claims_atomically(space,same_ticket):
    board,human,_,_,first,second,*_=space
    one=new(space,'One',resource='one');two=one if same_ticket else new(space,'Two',resource='two')
    barrier=threading.Barrier(2)
    def run(args):
        actor,ticket,key=args;barrier.wait()
        try:return select(board,actor,ticket,key)
        except BoardError as error:return error.status
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(run,[(first,one,'one'),(second,two,'two')]))
    assert sum(isinstance(r,dict) and r['claimed'] for r in results)==1
    if same_ticket:assert results.count(409)==1
    else:assert next(r for r in results if not r['claimed'])['reason']=='unfinished_work'
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM tickets WHERE claimant IS NOT NULL').fetchone()[0]==1
    assert board.get_runtime(human)['executions']==[]


def test_distinct_profiles_can_admit_distinct_resources_concurrently(space):
    board,human,_,_,first,*_=space
    other=independent(board,human)
    tickets=[new(space,'One',resource='one'),new(space,'Two',resource='two')]
    barrier=threading.Barrier(2)
    def run(args):
        actor,ticket=args;barrier.wait();return select(board,actor,ticket)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(run,zip((first,other),tickets)))
    assert all(r['claimed'] for r in results)
    assert len({r['ticket']['claimant']['agent_id'] for r in results})==2


def test_same_key_lost_response_replays_before_own_claim_barrier_without_mutation(space):
    board,human,_,_,first,*_=space
    ticket=new(space)
    claimed=select(board,first,ticket)
    before=snapshot(board)
    assert select(board,first,ticket)=={**claimed,'retried':True}
    assert snapshot(board)==before
    changed=board.update_ticket(human,ticket['id'],claimed['ticket']['version'],
        title='Human retained change',release_claim=True)
    before=snapshot(board)
    replay=select(board,first,ticket)
    assert replay['claimed'] and replay['retried'] and replay['ticket']==changed
    assert replay['ticket']['claimant'] is None  # historical ACK is never fresh authority
    assert snapshot(board)==before
    with pytest.raises(BoardError) as error:select(board,first,changed)
    assert error.value.status==409


def test_same_key_is_bound_to_exact_ticket_version_and_actor_session(space):
    board,_,_,_,first,second,*_=space
    one=new(space);two=new(space,'Other')
    select(board,first,one)
    before=snapshot(board)
    for ticket in (two,{**one,'version':one['version']+1}):
        with pytest.raises(BoardError) as error:select(board,first,ticket)
        assert error.value.status==409
    assert snapshot(board)==before
    assert select(board,second,two)['reason']=='unfinished_work'
    # The ordinary claim_next namespace cannot consume a selected-queue ACK.
    result=board.claim_next_ticket(first,'selected')
    assert not result['claimed'] and result['reason']=='unfinished_work'


def test_negative_unfinished_result_remains_negative_after_release(space):
    board,human,_,_,first,second,*_=space
    original=select(board,first,new(space,'Original'))['ticket']
    ticket=new(space,'Next')
    denied=select(board,second,ticket)
    assert denied=={'claimed':False,'reason':'unfinished_work','retried':False}
    board.update_ticket(human,original['id'],original['version'],release_claim=True)
    before=snapshot(board)
    assert select(board,second,ticket)=={**denied,'retried':True}
    assert snapshot(board)==before
    assert select(board,second,ticket,'new-intent')['ticket']['id']==ticket['id']


def test_no_ready_negative_is_durable_after_automation_enabled(space):
    board,human,_,project,first,*_=space
    ticket=new(space)
    project=board.update_project(human,project['id'],project['version'],automation_enabled=False)
    denied=select(board,first,ticket)
    assert denied=={'claimed':False,'reason':'no_ready_ticket','retried':False}
    board.update_project(human,project['id'],project['version'],automation_enabled=True)
    assert select(board,first,ticket)=={**denied,'retried':True}
    assert select(board,first,ticket,'after-enable')['claimed']


def test_expired_legacy_ticket_without_agent_id_blocks_across_projects_and_scope(space):
    board,human,_,_,first,second,*_=space
    previous=new(space,'Prior')
    board.claim_ticket(first,previous['id'],previous['version'])
    with board._tx() as db:
        db.execute("UPDATE tickets SET lease_expires_at=0,claimant=json_remove(claimant,'$.agent_id') WHERE id=?",(previous['id'],))
    project=board.create_project(human,'Other project',[human['owner_id']],idempotency_key='other-project')
    ticket=board.create_ticket(human,project['id'],'Next',idempotency_key='next')
    # The old claim remains a profile fence even after access to that project is removed.
    with board._tx() as db:
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',(previous['project_id'],human['owner_id']))
    assert select(board,second,ticket)['reason']=='unfinished_work'


def test_expired_legacy_task_without_agent_id_blocks_selected_ticket(space):
    board,human,_,_,first,second,*_=space
    board.set_policy(human,True,True,True,True,['fixture_action'],['fixture_resource'])
    thread=board.create_thread(human,'Legacy',[human['owner_id']],'legacy')
    old=board.create_task(human,thread['id'],'Legacy work','Fixture','','group',human['owner_id'],
        action='fixture_action',resource='fixture_resource')
    assert board.claim_task(first,old['id'])['claimed']
    with board._tx() as db:
        db.execute("UPDATE tasks SET lease_expires_at=0,claimant=json_remove(claimant,'$.agent_id') WHERE id=?",(old['id'],))
    assert select(board,second,new(space))['reason']=='unfinished_work'


def test_same_profile_on_another_bound_device_cannot_bypass_barrier(space):
    board,human,_,_,first,_,original=space
    profile=board.list_agent_profiles(human)[0]
    device=board.create_device(human,'Remote',[])
    board.bind_agent_profile(human,profile['id'],profile['version'],device['id'],'remote','codex')
    board.register_agent(board.authenticate(original['token']),first['session_id'],'First',['python'],'codex',True,'installation')
    raw=board.authenticate(device['token']);board.register_agent(raw,'remote','Remote',['python'],'codex',True,'remote')
    select(board,first,new(space,'Current'))
    assert select(board,board.actor_session(raw,'remote'),new(space,'Next'))['reason']=='unfinished_work'


@pytest.mark.parametrize('change',['session','owner_requests','profile_requests','profile_autonomy','paused','scope','revoked'])
def test_replay_rechecks_fresh_authorization_before_returning_ack(space,change):
    board,human,_,_,first,_,device=space
    ticket=new(space);select(board,first,ticket)
    if change=='session':
        board.register_agent(board.authenticate(device['token']),first['session_id'],'Read',['python'],'codex',False,'installation')
    elif change=='owner_requests':board.set_policy(human,True,True,True,False,[],[])
    elif change=='revoked':board.revoke_device(human,device['id'])
    else:
        profile=board.list_agent_profiles(human)[0]
        changes={'policy':{**profile['policy'],'allow_requests':False}} if change=='profile_requests' else (
            {'policy':{**profile['policy'],'autonomy':'responses'}} if change=='profile_autonomy' else (
            {'status':'paused'} if change=='paused' else {'project_ids':[]}))
        board.update_agent_profile(human,profile['id'],profile['version'],**changes)
    before=snapshot(board)
    with pytest.raises(BoardError):select(board,first,ticket)
    assert snapshot(board)==before


def test_human_and_stale_versions_fail_without_remembering_or_claiming(space):
    board,human,_,_,first,*_=space
    ticket=new(space)
    for actor,version in ((human,ticket['version']),(first,ticket['version']+1)):
        before=snapshot(board)
        with pytest.raises(BoardError) as error:
            board.claim_selected_queue_ticket(actor,ticket['id'],version,'invalid')
        assert error.value.status==(403 if actor==human else 409)
        assert snapshot(board)==before


@pytest.mark.parametrize('stored',['fresh','positive','negative'])
@pytest.mark.parametrize('companion_for',['future-owner-session',''])
def test_future_companion_actor_is_refused_before_key_lookup_or_any_write(space,monkeypatch,stored,companion_for):
    board,human,_,project,first,*_=space
    ticket=new(space)
    if stored=='positive':select(board,first,ticket)
    elif stored=='negative':
        board.update_project(human,project['id'],project['version'],automation_enabled=False)
        assert select(board,first,ticket)['reason']=='no_ready_ticket'
    original=board._actor
    def future_actor(db,actor,*args,**kwargs):
        value=original(db,actor,*args,**kwargs)
        return {**value,'companion_for':companion_for} if value['kind']=='agent' else value
    monkeypatch.setattr(board,'_actor',future_actor)
    def no_lookup(*args,**kwargs):raise AssertionError('Companion reached idempotency lookup')
    monkeypatch.setattr(board,'_retry',no_lookup)
    before=snapshot(board)
    with pytest.raises(BoardError) as error:select(board,first,ticket)
    assert error.value.status==403
    assert snapshot(board)==before


def test_claim_transaction_also_refuses_future_companion_marker_without_changing_manual_claim(space):
    board,_,_,_,first,*_=space
    ticket=new(space)
    before=snapshot(board)
    with pytest.raises(BoardError) as error:
        with board._tx() as db:
            actor={**board._actor(db,first,'allow_requests'),'companion_for':'future-owner-session'}
            row=board._ticket_member(db,actor,ticket['id'])
            board._claim_ticket_tx(db,actor,row,ticket['version'])
    assert error.value.status==403 and snapshot(board)==before
    assert board.claim_ticket(first,ticket['id'],ticket['version'])['claimant']['session_id']==first['session_id']


@pytest.mark.parametrize('condition',['review','in_progress','blocked','done','skills','owner','agent','dependency','question',
    'resource','expired_claim','residual_lease','residual_version','runtime','archived'])
def test_ineligible_exact_ticket_never_falls_back_or_changes_fences(space,condition):
    board,human,other,project,first,*_=space
    ticket=new(space,'Target',resource='shared')
    fallback=new(space,'Fallback',resource='different')
    if condition in ('review','in_progress','blocked','done'):
        fields={'status':condition}
        if condition=='done':fields.update(outcome='Fixture',evidence='Fixture')
        ticket=board.update_ticket(human,ticket['id'],ticket['version'],**fields)
    elif condition=='skills':ticket=board.update_ticket(human,ticket['id'],ticket['version'],required_skills=['android'])
    elif condition=='owner':ticket=board.update_ticket(human,ticket['id'],ticket['version'],assignee_owner_id=other['owner_id'])
    elif condition=='agent':
        other_actor=independent(board,human)
        with board._connection() as db:agent_id=db.execute('SELECT agent_id FROM sessions WHERE id=?',(other_actor['session_id'],)).fetchone()[0]
        ticket=board.update_ticket(human,ticket['id'],ticket['version'],assignee_agent_id=agent_id)
    elif condition=='dependency':ticket=board.update_ticket(human,ticket['id'],ticket['version'],depends_on=[fallback['id']])
    elif condition=='question':
        board.request_human_input(human,ticket['id'],human['owner_id'],'Which value?','question')
        ticket=board.get_ticket(human,ticket['id'])['ticket']
    elif condition in ('resource','expired_claim'):
        other_actor=independent(board,human)
        busy=ticket if condition=='expired_claim' else new(space,'Busy',resource='shared')
        board.claim_ticket(other_actor,busy['id'],busy['version'])
        with board._tx() as db:db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?',(busy['id'],))
        ticket=board.get_ticket(human,ticket['id'])['ticket']
    elif condition in ('residual_lease','residual_version'):
        column='lease_expires_at' if condition=='residual_lease' else 'claim_started_version'
        with board._tx() as db:db.execute(f'UPDATE tickets SET {column}=0 WHERE id=?',(ticket['id'],))
    elif condition=='runtime':
        actor=independent(board,human)
        busy=board.claim_ticket(actor,ticket['id'],ticket['version'])
        board.reserve_execution(actor,'reserved',project['id'],ticket['id'],'work','turn','codex_chatgpt','fixture-model')
        ticket=board.update_ticket(human,ticket['id'],busy['version'],release_claim=True,status='todo')
    else:board.update_project(human,project['id'],project['version'],archived=True)
    with board._connection() as db:
        prior_fences=[tuple(r) for r in db.execute('SELECT id,claimant,lease_expires_at FROM tickets ORDER BY id')]
        prior_runtime=[tuple(r) for r in db.execute('SELECT * FROM runtime_executions')]
    denied=select(board,first,ticket)
    assert denied=={'claimed':False,'reason':'no_ready_ticket','retried':False}
    assert board.get_ticket(human,fallback['id'])['ticket']['claimant'] is None
    with board._connection() as db:
        assert [tuple(r) for r in db.execute('SELECT id,claimant,lease_expires_at FROM tickets ORDER BY id')]==prior_fences
        assert [tuple(r) for r in db.execute('SELECT * FROM runtime_executions')]==prior_runtime


@pytest.mark.parametrize('cancelled',[False,True])
def test_all_delegation_family_tickets_are_excluded_even_when_unclaimed_and_todo(family,cancelled):
    f=family;board=f['b'];opened=open_family(f)
    if cancelled:board.stop_delegation_family(f['actors'][0],opened['id'],opened['revision'])
    actor=independent(board,f['h'])
    ids=[f['parent']['id']]+[child['ticket_id'] for child in opened['children']]
    # Make ordinary queue predicates true; membership itself must still exclude every historical family row.
    with board._tx() as db:
        db.execute('DELETE FROM ticket_dependencies')
        db.execute("UPDATE tickets SET status='todo',claimant=NULL,claim_started_version=NULL,lease_expires_at=NULL")
    for index,identifier in enumerate(ids):
        ticket=board.get_ticket(f['h'],identifier)['ticket']
        assert select(board,actor,ticket,'family-'+str(index))['reason']=='no_ready_ticket'
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM tickets WHERE claimant IS NOT NULL').fetchone()[0]==0


def api_space(browser):
    client,board=browser;human={'kind':'human','owner_id':'blackoutburst'}
    project=board.create_project(human,'Selected API',[human['owner_id']],idempotency_key='selected-api')
    device=board.create_device(human,'Queue',[])
    for sid in ('selected-one','selected-two'):
        rpc(client,device['token'],'register_agent',dict(session_id=sid,label=sid,agent_kind='codex',
            identity_key='queue',autonomous=True,skills=['python']))
    return client,board,human,project,device['token']


def raw_call(client,token,arguments):
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':7,'method':'tools/call',
              'params':{'name':'claim_selected_queue_ticket','arguments':arguments}})
    assert response.status_code==200
    return response.json()['result']


def test_mcp_real_transaction_and_current_state_retry(browser):
    client,board,human,project,token=api_space(browser)
    ticket=board.create_ticket(human,project['id'],'Selected',idempotency_key='selected')
    args=dict(session_id='selected-one',ticket_id=ticket['id'],expected_version=ticket['version'],idempotency_key='ack',view='full')
    selected=rpc(client,token,'claim_selected_queue_ticket',args)
    assert selected['claimed'] and selected['ticket']['claimant']['session_id']=='selected-one'
    assert rpc(client,token,'claim_selected_queue_ticket',args)=={**selected,'retried':True}
    other=board.create_ticket(human,project['id'],'Other',idempotency_key='other')
    denied=rpc(client,token,'claim_selected_queue_ticket',{**args,'session_id':'selected-two','ticket_id':other['id'],
        'expected_version':other['version'],'idempotency_key':'other'})
    assert denied=={'claimed':False,'reason':'unfinished_work','retried':False}
    with board._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM ticket_events WHERE event='ticket_claimed'").fetchone()[0]==1


@pytest.mark.parametrize('alteration',[{'expected_version':True},{'expected_version':1.0},{'expected_version':'1'},
    {'expected_version':None},{'idempotency_key':False},{'session_id':None},{'ticket_id':[]},{'force':True},
    {'recovery_evidence':'claimed clean'},{'view':'other'}])
def test_mcp_closed_arguments_refuse_coercion_or_recovery_before_effects(browser,alteration):
    client,board,human,project,token=api_space(browser)
    ticket=board.create_ticket(human,project['id'],'Selected',idempotency_key='selected')
    args=dict(session_id='selected-one',ticket_id=ticket['id'],expected_version=ticket['version'],idempotency_key='invalid')
    result=raw_call(client,token,{**args,**alteration})
    assert result['isError']
    assert board.get_ticket(human,ticket['id'])['ticket']['claimant'] is None
    with board._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM idempotency WHERE operation='claim_selected_queue_ticket'").fetchone()[0]==0


def test_mcp_catalogue_is_closed_and_concurrent_sessions_use_one_atomic_barrier(browser):
    client,board,human,project,token=api_space(browser)
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':'tools/list','params':{}})
    schema=next(t for t in response.json()['result']['tools'] if t['name']=='claim_selected_queue_ticket')['inputSchema']
    assert schema['additionalProperties'] is False
    assert set(schema['required'])=={'session_id','ticket_id','expected_version','idempotency_key'}
    tickets=[board.create_ticket(human,project['id'],str(i),resource=str(i),idempotency_key=str(i)) for i in range(2)]
    barrier=threading.Barrier(2)
    def run(args):
        sid,ticket=args;barrier.wait()
        return rpc(client,token,'claim_selected_queue_ticket',dict(session_id=sid,ticket_id=ticket['id'],
            expected_version=ticket['version'],idempotency_key=sid,view='full'))
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(run,zip(('selected-one','selected-two'),tickets)))
    assert sum(r['claimed'] for r in results)==1
    assert next(r for r in results if not r['claimed'])['reason']=='unfinished_work'
