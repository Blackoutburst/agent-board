"""Temporary-family contract with real SQLite and no model or host process.

The server integrates the module. A few isolated budget tests additionally call
the hook in the same write transaction used for their synthetic Runtime receipt;
separate integration tests exercise the actual Runtime and app wiring.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import secrets
import threading
import time

import pytest

from board import Board, BoardError
from delegation_families import DelegationFamiliesMixin, migrate_delegation_families, _result_hash


class FamilyBoard(Board):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        with self._tx() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delegation_families'").fetchone():
                migrate_delegation_families(db)


@pytest.fixture
def family(tmp_path):
    b=FamilyBoard(tmp_path/'board')
    h={'kind':'human','owner_id':'alice'}
    other={'kind':'human','owner_id':'bob'}
    actions=['repo.read_file','repo.patch_file','repo.run_test']
    for person in (h,other):
        b.ensure_owner(person['owner_id'],person['owner_id'].title())
        b.set_policy(person,True,True,True,True,actions,['fixture:repo','fixture:other'])
    project=b.create_project(h,'Shared',['alice','bob'],'project')
    device=b.create_device(h,'Fixture',[])
    raw=b.authenticate(device['token'])
    actors=[]
    for session in ('coordinator','child-one','child-two'):
        b.register_agent(raw,session,'Fixture',[],'codex',False,'same-profile')
        actors.append(b.actor_session(raw,session))
    parent=b.create_ticket(h,project['id'],'Parent',resource='fixture:repo',idempotency_key='parent')
    parent=b.claim_ticket(actors[0],parent['id'],parent['version'])
    children=[dict(session_id=actor['session_id'],role='worker',model='fixture-model',title='Child '+str(i),
        description='Bounded child '+str(i),acceptance_criteria='Evidence required',files=['part'+str(i)+'.txt'])
        for i,actor in enumerate(actors[1:])]
    return dict(b=b,h=h,other=other,project=project,device=device,actors=actors,parent=parent,
        args=dict(parent_ticket_id=parent['id'],expected_version=parent['version'],children=children,
            limits=dict(max_model_turns=2,max_tool_actions=1,max_concurrent=1,deadline=time.time()+600),
            actions=actions,resource='fixture:repo',manifest_sha256='a'*64,idempotency_key='open'))


def reject(fn,*args,**kwargs):
    with pytest.raises(BoardError) as error:fn(*args,**kwargs)
    return str(error.value)


def open_family(f,**changes):
    return f['b'].open_delegation_family(f['actors'][0],**{**f['args'],**changes})


def member(f,opened,index=0):
    session=f['actors'][index+1]['session_id']
    with f['b']._connection() as db:
        return dict(db.execute('SELECT * FROM delegation_members WHERE family_id=? AND session_id=?',
            (opened['id'],session)).fetchone())


def hook(f,actor,ticket_id,provider='codex_chatgpt',model='fixture-model'):
    with f['b']._tx() as db:
        f['b']._runtime_authorize(db,actor,f['project']['id'],ticket_id)
        f['b']._delegation_runtime_check(db,actor,ticket_id,provider,model)


def receipt(f,actor,ticket_id,key,provider='codex_chatgpt',model='fixture-model'):
    """Exercise the proposed admission hook atomically with its retained row."""
    b=f['b']
    with b._tx() as db:
        actor,profile=b._runtime_authorize(db,actor,f['project']['id'],ticket_id)
        b._delegation_runtime_check(db,actor,ticket_id,provider,model)
        identifier=secrets.token_hex(16);now=time.time()
        db.execute('''INSERT INTO runtime_executions(
            id,owner_id,agent_id,profile_version,device_id,session_id,project_id,ticket_id,
            work_id,turn_id,provider,model,idempotency_key,payload,state,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)''',
            (identifier,actor['owner_id'],profile['id'],profile['version'],actor['device_id'],actor['session_id'],
             f['project']['id'],ticket_id,'work-'+key,'turn-'+key,provider,model,key,'{}',now,now))
        return identifier


def review_child(f,opened,index=0):
    b=f['b'];m=member(f,opened,index);actor=f['actors'][index+1]
    child=b.get_ticket(actor,m['ticket_id'])['ticket']
    child=b.claim_ticket(actor,child['id'],child['version'])
    child=b.update_ticket(actor,child['id'],child['version'],status='review',release_claim=True,
        outcome='Completed bounded fixture',evidence='Synthetic verification evidence')
    with b._connection() as db:
        digest=_result_hash(db.execute('SELECT * FROM tickets WHERE id=?',(child['id'],)).fetchone())
    return child,digest


def accept(f,opened,index=0,key=None):
    child,digest=review_child(f,opened,index)
    return f['b'].accept_delegation_result(f['actors'][0],opened['id'],opened['revision'],
        child['id'],child['version'],digest,'Coordinator inspected fixture',key or 'accept-'+str(index))


def test_open_is_atomic_retry_keeps_parent_claim_profile_and_exact_children(family):
    f=family;b=f['b']
    with b._connection() as db:profiles=db.execute('SELECT COUNT(*) FROM agent_profiles').fetchone()[0]
    opened=open_family(f)
    parent=b.get_ticket(f['actors'][0],f['parent']['id'])['ticket']
    assert parent['claimant']['session_id']=='coordinator' and parent['status']=='in_progress'
    assert set(parent['depends_on'])=={m['ticket_id'] for m in opened['children']}
    assert len(opened['children'])==2 and opened['state']=='running'
    with b._connection() as db:
        before=tuple(db.iterdump())
        assert db.execute('SELECT COUNT(*) FROM agent_profiles').fetchone()[0]==profiles
        sessions=[tuple(row) for row in db.execute('SELECT session_id FROM delegation_members ORDER BY session_id')]
        assert sessions==[('child-one',),('child-two',)]
    assert open_family(f)==opened
    assert b.lookup_delegation_family(f['actors'][0],parent['id'],'open')['family']==opened
    with b._connection() as db:assert tuple(db.iterdump())==before
    reject(open_family,f,idempotency_key='another')
    reject(open_family,f,manifest_sha256='b'*64)


@pytest.mark.parametrize('change',['device','profile','session'])
def test_open_refuses_foreign_or_coordinator_worker_identity_without_partial_children(family,change):
    f=family;b=f['b'];children=copy.deepcopy(f['args']['children'])
    if change=='session':children[0]['session_id']='coordinator'
    else:
        raw=b.authenticate(f['device']['token'] if change=='profile' else b.create_device(f['h'],'Other',[])['token'])
        if change=='profile':
            # Same device cannot silently acquire another default binding; use
            # an explicit active profile then bind it to this new identity.
            profile=b.create_agent_profile(f['h'],'Another',policy=dict(allow_read=True,
                allow_messages=True,allow_requests=False,actions=[],resources=[],autonomy='responses'))
            b.bind_agent_profile(f['h'],profile['id'],profile['version'],raw['device_id'],'other','codex')
        b.register_agent(raw,'foreign','Other',[],'codex',False,'other')
        children[0]['session_id']='foreign'
    reject(open_family,f,children=children)
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM delegation_families').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==1


@pytest.mark.parametrize('path',['PART0.TXT','part0.txt','../outside','.hidden','a/../b','a\\b'])
def test_file_bounds_reject_alias_overlap_and_traversal(family,path):
    children=copy.deepcopy(family['args']['children']);children[1]['files']=[path]
    reject(open_family,family,children=children)


@pytest.mark.parametrize('actor_index',[0,1])
@pytest.mark.parametrize('cancelled',[False,True])
def test_bound_session_cannot_escape_family_to_ordinary_ticket(family,actor_index,cancelled):
    f=family;b=f['b'];opened=open_family(f)
    if cancelled:b.stop_delegation_family(f['actors'][0],opened['id'],opened['revision'])
    child=f['actors'][actor_index]
    outside=b.create_ticket(f['h'],f['project']['id'],'Outside bounds',idempotency_key='outside')
    reject(b.claim_ticket,child,outside['id'],outside['version'])
    assert b.get_ticket(f['h'],outside['id'])['ticket']['claimant'] is None


@pytest.mark.parametrize('actor_index',[0,1])
@pytest.mark.parametrize('active',['claim','runtime'])
def test_creation_refuses_session_with_existing_work(family,active,actor_index):
    f=family;b=f['b'];child=f['actors'][actor_index]
    outside=b.create_ticket(f['h'],f['project']['id'],'Prior work',idempotency_key='prior')
    outside=b.claim_ticket(child,outside['id'],outside['version'])
    if active=='runtime':
        b.reserve_execution(child,'prior-runtime',f['project']['id'],outside['id'],'prior-work','prior-turn','codex_chatgpt','fixture-model')
        b.release_ticket(child,outside['id'],outside['version'],'prior-release','Still cleaning',status='review')
    reject(open_family,f)


@pytest.mark.parametrize('actor_index',[0,1])
def test_bound_session_cannot_use_general_message_scope(family,actor_index):
    f=family;b=f['b'];open_family(f)
    with b._tx() as db:
        reject(b._delegation_runtime_check,db,f['actors'][actor_index],None,'codex_chatgpt','fixture-model')


def test_parent_resource_change_stops_family_without_silent_lock_migration(family):
    f=family;b=f['b'];opened=open_family(f);opened=accept(f,accept(f,opened),1)
    parent=b.get_ticket(f['h'],f['parent']['id'])['ticket']
    changed=b.update_ticket(f['h'],parent['id'],parent['version'],resource='fixture:other')
    assert changed['claimant']['session_id']=='coordinator'
    current=b.get_delegation_family(f['actors'][0],opened['id'])
    assert current['should_stop'] and current['control_reason']=='delegation_resource_changed'


def test_parent_lease_expiry_stops_all_children_but_exact_stop_can_be_retried(family):
    f=family;b=f['b'];opened=open_family(f)
    with b._tx() as db:db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',(time.time()-1,f['parent']['id']))
    current=b.get_delegation_family(f['actors'][0],opened['id'])
    assert current['should_stop'] and current['control_reason']=='delegation_parent_authorization_changed'
    stopped=b.stop_delegation_family(f['actors'][0],opened['id'],opened['revision'])
    assert stopped['state']=='cancelled'
    assert b.stop_delegation_family(f['actors'][0],opened['id'],opened['revision'])==stopped


def test_child_cannot_create_second_level_family(family):
    f=family;b=f['b'];opened=open_family(f);child_id=member(f,opened)['ticket_id']
    child=b.get_ticket(f['actors'][1],child_id)['ticket']
    child=b.claim_ticket(f['actors'][1],child_id,child['version'])
    nested={**f['args'],'parent_ticket_id':child_id,'expected_version':child['version'],'idempotency_key':'nested'}
    reject(b.open_delegation_family,f['actors'][1],**nested)
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==3


def test_child_model_action_and_deadline_bounds_are_enforced_at_admission(family):
    f=family;b=f['b'];opened=open_family(f);child_id=member(f,opened)['ticket_id'];actor=f['actors'][1]
    child=b.get_ticket(actor,child_id)['ticket'];b.claim_ticket(actor,child_id,child['version'])
    reject(hook,f,actor,child_id,model='unplanned-model')
    reject(hook,f,actor,child_id,provider='another-account')
    one=receipt(f,actor,child_id,'tool-one',provider='local_broker',model='read_file')
    b.finish_execution(actor,one,1,'cancelled',True)
    assert reject(receipt,f,actor,child_id,'tool-two',provider='local_broker',model='patch_file')=='Delegation budget exhausted'
    with b._tx() as db:
        row=b._family_row(db,opened['id']);plan=json.loads(row['plan']);plan['limits']['deadline']=time.time()-1
        db.execute('UPDATE delegation_families SET plan=? WHERE id=?',(json.dumps(plan),opened['id']))
    assert reject(hook,f,actor,child_id)=='delegation_deadline'


def test_current_grant_withdrawal_stops_family_and_refuses_admission(family):
    f=family;b=f['b'];opened=open_family(f);child_id=member(f,opened)['ticket_id'];actor=f['actors'][1]
    child=b.get_ticket(actor,child_id)['ticket'];b.claim_ticket(actor,child_id,child['version'])
    b.set_policy(f['h'],True,True,True,True,['repo.read_file'],['fixture:repo'])
    current=b.get_delegation_family(f['actors'][0],opened['id'])
    assert current['control_reason']=='delegation_grants_changed'
    reject(hook,f,actor,child_id)


def test_family_budget_is_serialized_and_failed_cleanup_retains_its_capacity(family):
    f=family;b=f['b'];opened=open_family(f)
    tickets=[]
    for i in range(2):
        actor=f['actors'][i+1];child=b.get_ticket(actor,member(f,opened,i)['ticket_id'])['ticket']
        tickets.append(b.claim_ticket(actor,child['id'],child['version'])['id'])
    barrier=threading.Barrier(2)
    def contend(i):
        barrier.wait()
        try:return receipt(f,f['actors'][i+1],tickets[i],'race-'+str(i))
        except BoardError as error:return str(error)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(contend,range(2)))
    assert results.count('Delegation capacity exhausted')==1
    winner=next(value for value in results if value!='Delegation capacity exhausted')
    winner_actor=f['actors'][1 if results[0]==winner else 2]
    loser=1 if results[0]==winner else 0
    b.finish_execution(winner_actor,winner,1,'cancelled',False,'cleanup_unconfirmed')
    reject(receipt,f,f['actors'][loser+1],tickets[loser],'blocked-by-cleanup')
    current=b.get_execution_control(winner_actor,winner)
    b.finish_execution(winner_actor,winner,current['revision'],'cancelled',True,'cleanup_unconfirmed')
    second=receipt(f,f['actors'][loser+1],tickets[loser],'second')
    b.finish_execution(f['actors'][loser+1],second,1,'cancelled',True)
    assert reject(receipt,f,f['actors'][loser+1],tickets[loser],'third')=='Delegation budget exhausted'


def test_result_acceptance_requires_cleanup_and_exact_released_evidence(family):
    f=family;b=f['b'];opened=open_family(f);m=member(f,opened);actor=f['actors'][1]
    child=b.get_ticket(actor,m['ticket_id'])['ticket'];child=b.claim_ticket(actor,child['id'],child['version'])
    execution=receipt(f,actor,child['id'],'child')
    child=b.update_ticket(actor,child['id'],child['version'],status='review',release_claim=True,outcome='Done',evidence='Fixture')
    with b._connection() as db:digest=_result_hash(db.execute('SELECT * FROM tickets WHERE id=?',(child['id'],)).fetchone())
    args=(f['actors'][0],opened['id'],opened['revision'],child['id'],child['version'],digest,'Inspected','accept')
    assert reject(b.accept_delegation_result,*args)=='Delegated cleanup is not confirmed'
    b.finish_execution(actor,execution,1,'cancelled',True)
    accepted=b.accept_delegation_result(*args)
    assert accepted['children'][next(i for i,x in enumerate(accepted['children']) if x['ticket_id']==child['id'])]['state']=='accepted'
    assert b.accept_delegation_result(*args)==accepted
    reject(b.accept_delegation_result,*args[:-1],idempotency_key='different-key')


def test_accepted_result_edit_stops_family_and_prevents_finalization(family):
    f=family;b=f['b'];opened=open_family(f);one=accept(f,opened);both=accept(f,one,1)
    assert both['state']=='integrating'
    child=b.get_ticket(f['h'],member(f,both)['ticket_id'])['ticket']
    b.update_ticket(f['h'],child['id'],child['version'],evidence='Later human change')
    current=b.get_delegation_family(f['actors'][0],both['id'])
    assert current['control_reason']=='delegation_result_changed'
    reject(b.finalize_delegation_family,f['actors'][0],both['id'],both['revision'],'b'*64,'Integrated fixture','finish')


def test_finalization_is_a_retryable_receipt_without_finishing_parent(family):
    f=family;b=f['b'];opened=open_family(f);both=accept(f,accept(f,opened),1)
    args=(f['actors'][0],both['id'],both['revision'],'b'*64,'Integrated fixture','finish')
    done=b.finalize_delegation_family(*args)
    assert done['state']=='review_ready' and done['final_snapshot_sha256']=='b'*64
    assert b.finalize_delegation_family(*args)==done
    parent=b.get_ticket(f['h'],f['parent']['id'])['ticket']
    assert parent['status']=='in_progress' and parent['claimant']['session_id']=='coordinator'
    reject(b.stop_delegation_family,f['actors'][0],done['id'],done['revision'])


def test_finalization_waits_for_coordinator_cleanup_after_children_accepted(family):
    f=family;b=f['b'];opened=open_family(f);both=accept(f,accept(f,opened),1)
    execution=receipt(f,f['actors'][0],f['parent']['id'],'integration')
    b.finish_execution(f['actors'][0],execution,1,'cancelled',False,'cleanup_unconfirmed')
    args=(f['actors'][0],both['id'],both['revision'],'b'*64,'Integrated fixture','finish')
    assert reject(b.finalize_delegation_family,*args)=='Family cleanup is not confirmed'
    current=b.get_execution_control(f['actors'][0],execution)
    b.finish_execution(f['actors'][0],execution,current['revision'],'cancelled',True,'cleanup_unconfirmed')
    assert b.finalize_delegation_family(*args)['state']=='review_ready'


def test_other_owner_gets_no_private_plan_and_cannot_manage_family(family):
    f=family;b=f['b'];opened=open_family(f)
    public=b.get_delegation_family(f['other'],opened['id'])
    assert 'plan' not in public and 'child-one' not in json.dumps(public) and 'part0.txt' not in json.dumps(public)
    reject(b.stop_delegation_family,f['other'],opened['id'],opened['revision'])
    assert b.lookup_delegation_family(f['actors'][1],f['parent']['id'],'open')=={'found':False}
