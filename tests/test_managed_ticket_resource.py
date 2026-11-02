"""Real transaction/authorization tests for delegated metadata-only routing."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier

import pytest

from board import BoardError
from profile_management import (TICKET_RESOURCE_ACTION, PROFILE_MANAGEMENT_TOOLS,
    validate_profile_management_arguments)
from test_profile_management import lead, policy
from test_managed_claim_release import coder
from test_profile_claim_reconciliation_security import snapshot

OLD='repo:fixture'
NEW='repo:next'
REASON='Route this unclaimed task to its explicitly granted workspace.'


@pytest.fixture
def routing(lead):
    s=lead;b=s['b'];p=s['profile']
    owner=b.get_settings(s['human'])['policy']
    b.set_policy(s['human'],**{k:owner[k] for k in ('enabled','allow_read','allow_reply','allow_requests','actions')},
        resources=[*owner['resources'],NEW])
    p=b.update_agent_profile(s['human'],p['id'],p['version'],policy=p['policy']|{'resources':[*p['policy']['resources'],NEW]})
    p=b.set_agent_profile_ticket_resource_management(s['human'],p['id'],p['version'],True)
    b.register_agent(s['raw'],'route-session','Router',[],'codex',False,'lead')
    s['actor']=b.actor_session(s['raw'],'route-session');s['profile']=p
    return s


def ticket(s,**changes):
    return s['b'].create_ticket(s['human'],s['project']['id'],'Route task',
        **(dict(assignee_owner_id='alice',resource=OLD)|changes))


def move(s,t,**changes):
    args=dict(actor=s['actor'],ticket_id=t['id'],expected_version=t['version'],
        expected_resource=t['resource'],new_resource=NEW,idempotency_key='route-'+t['id'],reason=REASON)
    return s['b'].retarget_managed_ticket_resource(**(args|changes))


def denied(s,t,**changes):
    before=snapshot(s['b'])
    with pytest.raises(BoardError) as error:move(s,t,**changes)
    assert error.value.status in (400,401,403,404,409)
    assert snapshot(s['b'])==before
    return str(error.value)


def raw_ticket(s,t):
    with s['b']._connection() as db:return dict(db.execute('SELECT * FROM tickets WHERE id=?',(t['id'],)).fetchone())


def test_explicit_grant_is_separate_human_only_and_revocable_without_prerequisites(lead):
    s=lead;b=s['b'];p=s['profile']
    assert TICKET_RESOURCE_ACTION not in p['policy']['actions']
    t=ticket(s)
    assert 'Human-delegated' in denied(s,t,new_resource='repo:fixture-other')
    for actor in (s['actor'],s['other']):
        with pytest.raises(BoardError):b.set_agent_profile_ticket_resource_management(actor,p['id'],p['version'],True)
    with pytest.raises(BoardError):b.set_agent_profile_ticket_resource_management(s['human'],p['id'],True,True)
    p=b.set_agent_profile_ticket_resource_management(s['human'],p['id'],p['version'],True)
    assert TICKET_RESOURCE_ACTION in p['policy']['actions']
    p=b.update_agent_profile(s['human'],p['id'],p['version'],policy=p['policy']|{'allow_read':False,'resources':[]})
    p=b.set_agent_profile_ticket_resource_management(s['human'],p['id'],p['version'],False)
    assert TICKET_RESOURCE_ACTION not in p['policy']['actions']
    with pytest.raises(BoardError):b.set_agent_profile_ticket_resource_management(s['human'],p['id'],p['version'],True)


def test_retarget_and_exact_ack_retry_preserve_all_other_ticket_fields(routing):
    s=routing;b=s['b'];dep=ticket(s);t=ticket(s,depends_on=[dep['id']],labels=['kept'],required_skills=['python'])
    before=raw_ticket(s,t);result=move(s,t);after=raw_ticket(s,t)
    assert result['resource']==NEW and result['version']==t['version']+1
    assert {k:v for k,v in before.items() if k not in ('resource','version','updated_at')}=={
        k:v for k,v in after.items() if k not in ('resource','version','updated_at')}
    assert result['depends_on']==[dep['id']] and result['status']=='todo' and result['claimant'] is None
    state=snapshot(b);again=move(s,t)
    assert again['version']==result['version'] and snapshot(b)==state
    with b._connection() as db:
        events=db.execute("SELECT detail FROM audit WHERE event='managed_ticket_resource_retargeted'").fetchall()
    assert len(events)==1 and json.loads(events[0][0])['reason']==REASON


@pytest.mark.parametrize('assignee',['none','self','ordinary'])
def test_assignment_scope_is_checked_without_changing_assignment(routing,assignee):
    s=routing;target=None
    if assignee=='self':target=s['profile']['id']
    elif assignee=='ordinary':target=coder(s,grants=policy(resources=[OLD,NEW],autonomy='requested'))[0]['id']
    t=ticket(s,assignee_agent_id=target)
    assert move(s,t)['assignee_agent_id']==target


@pytest.mark.parametrize('fault',['foreign_owner','no_owner','hidden_project','target_foreign','target_management','target_higher','target_missing_resource'])
def test_cross_owner_project_and_unmanaged_assignees_are_denied(routing,fault):
    s=routing;b=s['b'];t=ticket(s)
    if fault=='foreign_owner':t=ticket(s,assignee_owner_id='bob')
    elif fault=='no_owner':t=ticket(s,assignee_owner_id=None)
    elif fault=='hidden_project':t=b.create_ticket(s['human'],s['hidden']['id'],'Hidden',assignee_owner_id='alice',resource=OLD)
    else:
        grants=policy(resources=[OLD,NEW],autonomy='requested');human=s['human']
        if fault=='target_foreign':human=s['other']
        if fault=='target_higher':grants['actions']=['server.deploy']
        if fault=='target_missing_resource':grants['resources']=[OLD]
        p=b.create_agent_profile(human,'Target',project_ids=[s['project']['id']],policy=grants)
        if fault=='target_management':p=b.set_agent_profile_management(human,p['id'],p['version'],True)
        # A mismatched owner assignment is not accepted by the ordinary editor;
        # model the stored foreign target directly to exercise the new guard.
        with b._tx() as db:db.execute('UPDATE tickets SET assignee_agent_id=? WHERE id=?',(p['id'],t['id']))
    denied(s,t)


@pytest.mark.parametrize('field,value',[('claimant','null'),('claimant','{}'),('claim_started_version',0),('lease_expires_at',0),('lease_expires_at',-1)])
def test_any_claim_or_lease_field_is_a_refusal_even_if_expired(routing,field,value):
    s=routing;t=ticket(s)
    with s['b']._tx() as db:db.execute('UPDATE tickets SET '+field+'=? WHERE id=?',(value,t['id']))
    assert 'claim_present' in denied(s,t)


@pytest.mark.parametrize('fault',['done','archived','human_request','family_parent','family_child'])
def test_closed_or_human_pending_or_ever_family_targets_are_denied(routing,fault):
    s=routing;b=s['b'];t=ticket(s)
    if fault=='done':
        with b._tx() as db:db.execute("UPDATE tickets SET status='done',outcome='Delivered',evidence='Verified' WHERE id=?",(t['id'],))
    elif fault=='archived':b.update_project(s['human'],s['project']['id'],s['project']['version'],archived=True)
    elif fault=='human_request':
        b.request_human_input(s['human'],t['id'],'alice','Inspect this first.',idempotency_key='question')
    else:
        parent=t if fault=='family_parent' else ticket(s)
        with b._tx() as db:
            db.execute('''INSERT INTO delegation_families(id,owner_id,agent_id,profile_version,device_id,session_id,
                project_id,parent_ticket_id,plan,plan_sha256,state,created_at,updated_at)
                VALUES('family','alice',?,?,?,?,?,?,'{}',?,'cancelled',0,0)''',
                (s['profile']['id'],s['profile']['version'],s['device']['id'],s['actor']['session_id'],s['project']['id'],parent['id'],'a'*64))
            if fault=='family_child':db.execute("INSERT INTO delegation_members(family_id,ticket_id,session_id,role,model,files) VALUES('family',?,?,'coder','synthetic','[]')",(t['id'],s['actor']['session_id']))
    denied(s,t)


def runtime_target(s):
    b=s['b'];p,actor,_=coder(s,grants=policy(resources=[OLD,NEW],autonomy='requested'))
    t=ticket(s,assignee_agent_id=p['id']);t=b.claim_ticket(actor,t['id'],t['version'])
    execution=b.reserve_execution(actor,'runtime',s['project']['id'],t['id'],'work','turn','codex_chatgpt','synthetic')['execution']
    with b._tx() as db:db.execute('UPDATE tickets SET claimant=NULL,claim_started_version=NULL,lease_expires_at=NULL WHERE id=?',(t['id'],))
    return b.get_ticket(s['human'],t['id'])['ticket'],execution


@pytest.mark.parametrize('state,cleanup',[('reserved',0),('running',0),('needs_review',0),('finished',0),('running',1),('finished',1)])
def test_only_finished_positive_cleanup_allows_metadata_move(routing,state,cleanup):
    s=routing;b=s['b'];t,e=runtime_target(s)
    with b._tx() as db:db.execute('UPDATE runtime_executions SET state=?,cleanup_confirmed=? WHERE id=?',(state,cleanup,e['id']))
    before=b.get_runtime(s['human'])
    if (state,cleanup)==('finished',1):assert move(s,t)['resource']==NEW
    else:assert 'runtime_unresolved' in denied(s,t)
    assert b.get_runtime(s['human'])==before


@pytest.mark.parametrize('old',['',OLD])
def test_empty_old_resource_and_busy_other_resources_do_not_move_the_other_claim(routing,old):
    s=routing;b=s['b'];p,actor,_=coder(s,grants=policy(resources=[OLD,NEW],autonomy='requested'))
    busy=ticket(s,assignee_agent_id=p['id']);busy=b.claim_ticket(actor,busy['id'],busy['version'])
    dest=ticket(s,resource=NEW,assignee_agent_id=p['id'])
    # Preserve a stored different-resource reservation too; metadata movement
    # must not consult, release or circumvent either claim/family fence.
    with b._tx() as db:db.execute('UPDATE tickets SET claimant=?,claim_started_version=version,lease_expires_at=? WHERE id=?',
        (json.dumps(busy['claimant']),busy['lease_expires_at'],dest['id']))
    t=ticket(s,resource=old);before={x['id']:raw_ticket(s,x) for x in (busy,dest)}
    assert move(s,t)['resource']==NEW
    assert {x['id']:raw_ticket(s,x) for x in (busy,dest)}==before
    with pytest.raises(BoardError):b.claim_ticket(actor,t['id'],t['version']+1)


@pytest.mark.parametrize('fault',['owner_action','owner_resource','profile_action','profile_resource','profile_read','paused','project_scope'])
def test_fresh_restrictions_block_even_exact_completed_retries(routing,fault):
    s=routing;b=s['b'];t=ticket(s);move(s,t)
    if fault.startswith('owner'):
        owner=b.get_settings(s['human'])['policy']
        owner['actions']=[x for x in owner['actions'] if x!=TICKET_RESOURCE_ACTION] if fault=='owner_action' else owner['actions']
        owner['resources']=[x for x in owner['resources'] if x!=NEW] if fault=='owner_resource' else owner['resources']
        b.set_policy(s['human'],**{k:owner[k] for k in ('enabled','allow_read','allow_reply','allow_requests','actions','resources')})
    else:
        p=b.get_agent_profile(s['human'],s['profile']['id']);changes={};pol=p['policy']
        if fault=='profile_action':pol['actions'].remove(TICKET_RESOURCE_ACTION)
        elif fault=='profile_resource':pol['resources'].remove(NEW)
        elif fault=='profile_read':pol['allow_read']=False
        elif fault=='paused':changes['status']='paused'
        elif fault=='project_scope':changes['project_ids']=[]
        b.update_agent_profile(s['human'],p['id'],p['version'],**(changes or {'policy':pol}))
    denied(s,t)


def test_later_ticket_context_or_retry_payload_cannot_move_again(routing):
    s=routing;b=s['b'];t=ticket(s);result=move(s,t)
    denied(s,t,reason='Changed reason')
    b.update_ticket(s['human'],t['id'],result['version'],title='New human context')
    assert 'retry_conflict' in denied(s,t)
    assert b.get_ticket(s['human'],t['id'])['ticket']['resource']==NEW


def test_retry_refuses_a_later_claim_or_silent_source_context_change(routing):
    s=routing;b=s['b'];t=ticket(s);result=move(s,t)
    with b._tx() as db:db.execute("UPDATE tickets SET description='Different source context' WHERE id=?",(t['id'],))
    assert 'retry_conflict' in denied(s,t)
    other=ticket(s);moved=move(s,other)
    b.claim_ticket(s['actor'],other['id'],moved['version'])
    assert 'claim_present' in denied(s,other)


@pytest.mark.parametrize('kind',['role','legacy'])
def test_role_names_and_owner_wildcards_never_delegate_routing(routing,kind):
    s=routing;b=s['b'];device=b.create_device(s['human'],'Unprivileged host',[])
    if kind=='role':
        p=b.create_agent_profile(s['human'],'Management / Lead',role='Lead',project_ids=[s['project']['id']],
            policy=policy(actions=['*'],resources=['*']))
        b.bind_agent_profile(s['human'],p['id'],p['version'],device['id'],'unprivileged','codex')
    raw=b.authenticate(device['token']);b.register_agent(raw,'unprivileged','Lead',[],'codex',False,'unprivileged')
    actor=b.actor_session(raw,'unprivileged')
    denied(s,ticket(s),actor=actor)


def test_version_and_source_cas_and_concurrent_claim_are_serialized(routing):
    s=routing;b=s['b'];t=ticket(s)
    denied(s,t,expected_version=t['version']+1);denied(s,t,expected_resource='')
    gate=Barrier(2)
    def retarget():
        gate.wait()
        try:return move(s,t)['resource']
        except BoardError:return 'refused'
    def claim():
        gate.wait()
        try:return b.claim_ticket(s['actor'],t['id'],t['version'])['claimant']
        except BoardError:return 'refused'
    with ThreadPoolExecutor(2) as pool:
        a=pool.submit(retarget);c=pool.submit(claim);results=[a.result(),c.result()]
    assert results.count('refused')==1
    after=b.get_ticket(s['human'],t['id'])['ticket']
    assert after['version']==t['version']+1
    assert (after['resource']==NEW)==(after['claimant'] is None)


@pytest.mark.parametrize('field,value',[('expected_version',None),('expected_version',True),('expected_resource',None),
    ('new_resource',None),('new_resource',''),('new_resource','*'),('new_resource','repo:*'),
    ('new_resource','agent-profiles:alice'),('new_resource','runtime:all'),('new_resource',' repo:next'),('new_resource','x'*201),
    ('idempotency_key',''),('reason','  ')])
def test_null_wildcard_reserved_and_inexact_fields_never_write(routing,field,value):
    denied(routing,ticket(routing),**{field:value})


def test_grant_cannot_be_delegated_or_laundered_and_base_revoke_removes_it(routing):
    s=routing;b=s['b']
    with pytest.raises(BoardError):b.create_managed_agent_profile(s['actor'],'Escalation','new-ticket-grant',
        project_ids=[s['project']['id']],policy=policy(actions=[TICKET_RESOURCE_ACTION]))
    p=b.create_agent_profile(s['human'],'Ticket manager',project_ids=[s['project']['id']],policy=policy(actions=[TICKET_RESOURCE_ACTION]))
    with pytest.raises(BoardError):b.get_managed_agent_profile(s['actor'],p['id'])
    p=b.set_agent_profile_management(s['human'],s['profile']['id'],s['profile']['version'],False)
    assert not any(x.startswith(('agent.profile.','agent.ticket.')) for x in p['policy']['actions'])


def test_closed_tool_argument_validator():
    name='retarget_managed_ticket_resource'
    assert name in PROFILE_MANAGEMENT_TOOLS
    args=dict(session_id='session',ticket_id='ticket',expected_version=1,expected_resource=OLD,
        new_resource=NEW,idempotency_key='intent',reason=REASON)
    validate_profile_management_arguments(name,args)
    for changed in (args|{'execute':True},args|{'new_resource':None},args|{'expected_resource':None},args|{'expected_version':True}):
        with pytest.raises(BoardError):validate_profile_management_arguments(name,changed)
    with pytest.raises(BoardError):validate_profile_management_arguments(name,{k:v for k,v in args.items() if k!='expected_resource'})
