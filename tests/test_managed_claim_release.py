"""Delegated stale-claim recovery never invents Runtime cleanup or authority."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier
import time

import pytest

from board import BoardError
from profile_management import CLAIM_RELEASE_ACTION, MANAGEMENT_ACTIONS
from test_profile_management import lead, policy
from test_profile_claim_reconciliation_security import snapshot

REASON='The stopped worker no longer owns this task.'
EVIDENCE='Inspected its recorded effects and cleanup; no pending write remains.'


def enable(s):
    b=s['b'];p=b.get_agent_profile(s['human'],s['profile']['id'])
    p=b.set_agent_profile_claim_management(s['human'],p['id'],p['version'],True)
    b.register_agent(s['raw'],'lead-release','Lead release',[],'codex',False,'lead')
    s['actor']=b.actor_session(s['raw'],'lead-release');s['profile']=p


def coder(s, *, owner=None, projects=None, grants=None, key='coder'):
    b=s['b'];human=owner or s['human']
    p=b.create_agent_profile(human,'Coder',project_ids=projects or [s['project']['id']],policy=grants or policy(autonomy='requested'))
    device=b.create_device(human,'Coder device',[])
    p=b.bind_agent_profile(human,p['id'],p['version'],device['id'],key,'codex')
    raw=b.authenticate(device['token']);b.register_agent(raw,key+'-session','Coder',[],'codex',False,key)
    return p,b.actor_session(raw,key+'-session'),raw


def claim(s, *, target=None, actor=None, project=None):
    b=s['b']
    if target is None:target,actor,_=coder(s)
    task=b.create_ticket(s['human'],(project or s['project'])['id'],'Stale work',
        assignee_owner_id=s['human']['owner_id'],assignee_agent_id=target['id'],resource='repo:fixture')
    task=b.claim_ticket(actor,task['id'],task['version'])
    task=b.renew_ticket(actor,task['id'],task['version'],'Implementation and verification retained')
    execution=b.reserve_execution(actor,'model-'+task['id'],task['project_id'],task['id'],'work','turn','codex_chatgpt','synthetic')['execution']
    execution=b.finish_execution(actor,execution['id'],execution['revision'],'cancelled',True,'cancelled')
    with b._tx() as db:db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?',(task['id'],))
    return b.get_ticket(s['human'],task['id'])['ticket'],target,actor,execution


def release(s, task, *, actor=None, key='release', version=None, reason=REASON, evidence=EVIDENCE):
    return s['b'].release_managed_agent_claim(actor or s['actor'],task['id'],
        task['version'] if version is None else version,key,reason,evidence)


def reject(s,task,**kwargs):
    before=snapshot(s['b'])
    with pytest.raises(BoardError) as e:release(s,task,**kwargs)
    assert e.value.status in (400,401,403,404,409)
    assert snapshot(s['b'])==before
    return str(e.value)


def test_existing_management_does_not_gain_release_and_human_grant_is_separate(lead):
    s=lead;task,*_=claim(s)
    assert CLAIM_RELEASE_ACTION not in s['profile']['policy']['actions']
    assert 'Human-delegated' in reject(s,task)
    enable(s)
    assert all(a in s['profile']['policy']['actions'] for a in (*MANAGEMENT_ACTIONS,CLAIM_RELEASE_ACTION))
    assert release(s,task)['claimant'] is None


def test_release_preserves_work_assignment_and_all_runtime_records(lead):
    s=lead;enable(s);b=s['b'];task,target,_,_=claim(s)
    runtime=b.get_runtime(s['human']);profile=b.get_agent_profile(s['human'],target['id'])
    released=release(s,task)
    assert released['version']==task['version']+1 and released['status']=='todo'
    assert released['claimant'] is None and released['lease_expires_at'] is None
    for k in ('assignee_owner_id','assignee_agent_id','resource','progress','outcome','evidence','depends_on','human_requests'):
        assert released[k]==task[k]
    assert b.get_runtime(s['human'])==runtime
    current=b.get_agent_profile(s['human'],target['id'])
    for k in ('version','policy','project_ids','bindings'):assert current[k]==profile[k]
    with b._connection() as db:
        events=db.execute("SELECT detail FROM ticket_events WHERE ticket_id=? AND event='managed_agent_claim_released'",(task['id'],)).fetchall()
    assert len(events)==1
    event=json.loads(events[0][0]);assert event['recovery_evidence']==EVIDENCE and event['reason']==REASON
    assert event['target_profile_id']==target['id'] and event['manager_profile_id']==s['profile']['id']


def test_missing_runtime_is_not_a_cleanup_receipt_and_requires_explicit_evidence(lead):
    s=lead;enable(s);task,*_=claim(s)
    with s['b']._tx() as db:db.execute('DELETE FROM runtime_executions')
    reject(s,task,evidence='  ')
    assert release(s,task)['claimant'] is None
    assert s['b'].get_runtime(s['human'])['executions']==[]


@pytest.mark.parametrize('state,cleanup',[('reserved',0),('running',0),('needs_review',0),('finished',0),('running',1)])
def test_unresolved_runtime_blocks_without_altering_capacity(lead,state,cleanup):
    s=lead;enable(s);task,_,_,execution=claim(s)
    with s['b']._tx() as db:db.execute('UPDATE runtime_executions SET state=?,cleanup_confirmed=? WHERE id=?',(state,cleanup,execution['id']))
    assert 'runtime_unresolved' in reject(s,task)


@pytest.mark.parametrize('expiry',[time.time()+3600,None,float('inf')])
def test_live_or_unknown_lease_requires_human_intervention(lead,expiry):
    s=lead;enable(s);task,*_=claim(s)
    with s['b']._tx() as db:db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',(expiry,task['id']))
    assert 'lease_not_expired' in reject(s,task)


def test_retry_after_lost_response_never_releases_a_later_human_claim(lead):
    s=lead;enable(s);b=s['b'];task,*_=claim(s)
    released=release(s,task)
    later=b.claim_ticket(s['human'],task['id'],released['version'])
    before=snapshot(b)
    assert release(s,task)==later
    assert snapshot(b)==before
    reject(s,later,key='different')
    reject(s,task,key='release',reason='Changed reason')


@pytest.mark.parametrize('version',[True,1.0,'2',None,0,-1])
def test_strict_versions_and_nonempty_evidence(lead,version):
    s=lead;enable(s);task,*_=claim(s)
    if version is None:
        with pytest.raises(BoardError):s['b'].release_managed_agent_claim(s['actor'],task['id'],None,'bad',REASON,EVIDENCE)
    else:reject(s,task,version=version)


@pytest.mark.parametrize('field,value',[('reason',' '),('reason',True),('evidence',' '),('evidence','\ud800'),('evidence',123)])
def test_invalid_reason_or_evidence_never_mutates(lead,field,value):
    s=lead;enable(s);task,*_=claim(s);reject(s,task,**{field:value})


@pytest.mark.parametrize('restriction',['owner','device','profile','scope','release_grant','target_scope','target_grant'])
def test_current_authority_is_rechecked_on_first_call_and_retries(lead,restriction):
    s=lead;enable(s);b=s['b'];task,target,_,_=claim(s)
    if restriction=='owner':
        current=b.get_settings(s['human'])['policy'];b.set_policy(s['human'],False,True,True,True,current['actions'],current['resources'])
    elif restriction=='device':b.revoke_device(s['human'],s['device']['id'])
    elif restriction=='profile':b.update_agent_profile(s['human'],s['profile']['id'],s['profile']['version'],status='paused')
    elif restriction in ('scope','release_grant'):
        current=b.get_agent_profile(s['human'],s['profile']['id'])
        changes={'project_ids':[]} if restriction=='scope' else {'policy':current['policy']|{'actions':list(MANAGEMENT_ACTIONS)}}
        b.update_agent_profile(s['human'],current['id'],current['version'],**changes)
        b.register_agent(s['raw'],'changed-lead','Changed',[],'codex',False,'lead');s['actor']=b.actor_session(s['raw'],'changed-lead')
    elif restriction=='target_scope':b.update_agent_profile(s['human'],target['id'],target['version'],project_ids=[s['hidden']['id']])
    else:b.update_agent_profile(s['human'],target['id'],target['version'],policy=target['policy']|{'actions':['server.deploy']})
    reject(s,task)


def test_human_management_foreign_owner_and_malformed_claims_are_never_targets(lead):
    s=lead;enable(s);b=s['b'];task,*_=claim(s)
    original=b.get_ticket(s['human'],task['id'])['ticket']['claimant']
    for claimant in ({'kind':'human','owner_id':'alice'},original|{'owner_id':'bob'},original|{'agent_id':s['profile']['id']},original|{'device_id':'forged'}):
        with b._tx() as db:db.execute('UPDATE tickets SET claimant=? WHERE id=?',(json.dumps(claimant),task['id']))
        reject(s,task,key='bad-'+str(len(json.dumps(claimant))))
    # A real manager source is refused even when the forged metadata is otherwise consistent.
    with b._tx() as db:
        db.execute('UPDATE tickets SET claimant=?,assignee_agent_id=? WHERE id=?',(json.dumps(b._author(db,s['actor'])),s['profile']['id'],task['id']))
    reject(s,task,key='manager')


def test_grant_is_human_only_cas_and_disable_base_removes_new_permission(lead):
    s=lead;b=s['b'];p=s['profile']
    with pytest.raises(BoardError):b.set_agent_profile_claim_management(s['actor'],p['id'],p['version'],True)
    with pytest.raises(BoardError):b.set_agent_profile_claim_management(s['other'],p['id'],p['version'],True)
    with pytest.raises(BoardError):b.set_agent_profile_claim_management(s['human'],p['id'],p['version']-1,True)
    enable(s);p=s['profile'];disabled=b.set_agent_profile_management(s['human'],p['id'],p['version'],False)
    assert CLAIM_RELEASE_ACTION not in disabled['policy']['actions']
    with pytest.raises(BoardError):b.set_agent_profile_claim_management(s['human'],p['id'],disabled['version'],True)


def test_concurrent_cas_releases_once(lead):
    s=lead;enable(s);b=s['b'];task,*_=claim(s);barrier=Barrier(2)
    def attempt(key):
        barrier.wait()
        try:return release(s,task,key=key)
        except BoardError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:rows=list(pool.map(attempt,['a','b']))
    assert sum(row is not None for row in rows)==1
    with b._connection() as db:assert db.execute("SELECT COUNT(*) FROM ticket_events WHERE event='managed_agent_claim_released'").fetchone()[0]==1


@pytest.mark.parametrize('column',['cleanup_confirmed','state'])
def test_nullable_historical_runtime_is_unknown_not_cleanup(lead,column):
    s=lead;enable(s);task,*_=claim(s)
    # Simulate a historical/corrupt nullable record without asking Runtime to
    # accept a new invalid execution. The release must retain responsibility.
    with s['b']._tx() as db:
        db.execute('CREATE TABLE nullable_runtime AS SELECT * FROM runtime_executions')
        db.execute('UPDATE nullable_runtime SET '+column+'=NULL')
        db.execute('ALTER TABLE runtime_executions RENAME TO retained_runtime_original')
        db.execute('ALTER TABLE nullable_runtime RENAME TO runtime_executions')
    assert 'runtime_unresolved' in reject(s,task)


@pytest.mark.parametrize('identity',['other_profile','other_owner'])
def test_unresolved_runtime_of_any_ticket_participant_blocks(lead,identity):
    s=lead;enable(s);task,_,_,execution=claim(s)
    owner=s['other'] if identity=='other_owner' else s['human']
    target,_,_=coder(s,owner=owner,key='second')
    with s['b']._tx() as db:
        db.execute("UPDATE runtime_executions SET owner_id=?,agent_id=?,state='needs_review',cleanup_confirmed=0 WHERE id=?",
            (owner['owner_id'],target['id'],execution['id']))
    assert 'runtime_unresolved' in reject(s,task)


def test_ticket_resource_and_project_remain_in_current_and_original_scope(lead):
    s=lead;enable(s);b=s['b'];task,target,_,_=claim(s)
    changed=b.update_ticket(s['human'],task['id'],task['version'],resource='repo:outside')
    assert 'ticket_outside_scope' in reject(s,changed)
    changed=b.update_ticket(s['human'],task['id'],changed['version'],resource='repo:fixture')
    b.update_agent_profile(s['human'],target['id'],target['version'],project_ids=[])
    assert 'ticket_outside_scope' in reject(s,changed)


def test_narrowed_target_and_lead_do_not_acquire_historical_resource(lead):
    s=lead;enable(s);b=s['b'];task,target,_,_=claim(s)
    owner=b.get_settings(s['human'])['policy']
    b.set_policy(s['human'],True,True,True,True,owner['actions'],owner['resources']+['repo:new'])
    current=b.get_agent_profile(s['human'],s['profile']['id'])
    new_resources=[x for x in current['policy']['resources'] if x!='repo:fixture']+['repo:new']
    p=b.update_agent_profile(s['human'],current['id'],current['version'],policy=current['policy']|{'resources':new_resources})
    b.register_agent(s['raw'],'narrowed','Lead',[],'codex',False,'lead');s['actor']=b.actor_session(s['raw'],'narrowed')
    b.update_agent_profile(s['human'],target['id'],target['version'],policy=target['policy']|{'resources':['repo:new']})
    assert 'ticket_outside_scope' in reject(s,task)
    changed=b.update_ticket(s['human'],task['id'],task['version'],resource='repo:new')
    assert 'management ceiling' in reject(s,changed)


def test_claim_issued_under_management_cannot_be_laundered_into_ordinary(lead):
    s=lead;enable(s);b=s['b']
    target=b.create_agent_profile(s['human'],'Former manager',project_ids=[s['project']['id']],policy=policy(autonomy='requested'))
    target=b.set_agent_profile_management(s['human'],target['id'],target['version'],True)
    device=b.create_device(s['human'],'Former manager device',[])
    target=b.bind_agent_profile(s['human'],target['id'],target['version'],device['id'],'former','codex')
    raw=b.authenticate(device['token']);b.register_agent(raw,'former','Former',[],'codex',False,'former')
    actor=b.actor_session(raw,'former')
    task,_,_,_=claim(s,target=target,actor=actor)
    b.set_agent_profile_management(s['human'],target['id'],target['version'],False)
    assert 'source_profile_outside_scope' in reject(s,task)


@pytest.mark.parametrize('mutation',['missing','invalid','incomplete'])
def test_missing_or_invalid_historical_snapshot_refuses_instead_of_guessing(lead,mutation):
    s=lead;enable(s);task,target,_,_=claim(s)
    with s['b']._tx() as db:
        if mutation=='missing':db.execute('DELETE FROM agent_profile_versions WHERE profile_id=? AND version=?',(target['id'],target['version']))
        else:db.execute('UPDATE agent_profile_versions SET snapshot=? WHERE profile_id=? AND version=?',
            ('{' if mutation=='invalid' else '{}',target['id'],target['version']))
    assert 'source_profile_outside_scope' in reject(s,task)


def test_invalid_claim_json_is_a_controlled_refusal(lead):
    s=lead;enable(s);task,*_=claim(s)
    with s['b']._tx() as db:db.execute("UPDATE tickets SET claimant='{' WHERE id=?",(task['id'],))
    assert 'source_mismatch' in reject(s,task)


def test_release_grant_cannot_be_delegated_by_profile_management(lead):
    s=lead;enable(s);b=s['b']
    for action in ('create','update'):
        with pytest.raises(BoardError):
            if action=='create':
                b.create_managed_agent_profile(s['actor'],'Escalation','escalate',project_ids=[s['project']['id']],
                    policy=policy(autonomy='requested',actions=['repo.read_file',CLAIM_RELEASE_ACTION]))
            else:
                target,_,_=coder(s)
                b.update_managed_agent_profile(s['actor'],target['id'],target['version'],'escalate-edit',
                    policy=target['policy']|{'actions':target['policy']['actions']+[CLAIM_RELEASE_ACTION]})
