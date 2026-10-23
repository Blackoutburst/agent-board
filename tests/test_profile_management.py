"""Delegated profile management uses real authorization, versions and audits."""
from copy import deepcopy
import json

import pytest

from board import Board,BoardError
from profile_management import MANAGEMENT_ACTIONS,management_resource


def policy(**changes):
    return dict(allow_read=True,allow_messages=True,allow_requests=True,actions=['repo.read_file','repo.patch_file'],
                resources=['repo:fixture'],autonomy='queue',allow_create_projects=False)|changes


@pytest.fixture
def lead(tmp_path):
    b=Board(tmp_path/'board');b.ensure_owner('alice','Alice');b.ensure_owner('bob','Bob')
    human={'kind':'human','owner_id':'alice'};other={'kind':'human','owner_id':'bob'}
    b.set_policy(human,True,True,True,True,['repo.read_file','repo.patch_file','runtime.manage','server.deploy'],['repo:fixture','runtime:all','server:all'])
    b.set_policy(other,True,True,True,True,[],[])
    project=b.create_project(human,'Allowed',['alice','bob'],idempotency_key='project')
    hidden=b.create_project(human,'Hidden',['alice'],idempotency_key='hidden')
    profile=b.create_agent_profile(human,'Management / Lead',role='Management',project_ids=[project['id']],policy=policy())
    profile=b.set_agent_profile_management(human,profile['id'],profile['version'],True)
    device=b.create_device(human,'Lead device',[])
    profile=b.bind_agent_profile(human,profile['id'],profile['version'],device['id'],'lead','codex')
    raw=b.authenticate(device['token']);b.register_agent(raw,'lead-session','Lead',[], 'codex',False,'lead')
    actor=b.actor_session(raw,'lead-session')
    return dict(b=b,human=human,other=other,project=project,hidden=hidden,profile=profile,device=device,raw=raw,actor=actor)


def create(s,key='coder',**fields):
    return s['b'].create_managed_agent_profile(s['actor'],'Coder',key,project_ids=[s['project']['id']],
        policy=policy(autonomy='requested'),**fields)


def denied(fn,*args,**kwargs):
    with pytest.raises(BoardError) as error:fn(*args,**kwargs)
    assert error.value.status in (400,401,403,404,409)


def test_create_edit_archive_restore_stay_bounded_unbound_and_audited(lead):
    s=lead;b=s['b'];actor=s['actor'];p=create(s)
    human_view=b.get_agent_profile(s['human'],p['id'])
    assert human_view['bindings']==[] and not human_view['legacy']
    assert 'bindings' not in p and 'active_work' not in p and 'token' not in p
    assert b.create_managed_agent_profile(actor,'Coder','coder',project_ids=[s['project']['id']],policy=policy(autonomy='requested'))==p
    changed=b.update_managed_agent_profile(actor,p['id'],p['version'],'edit',instructions='Inspect before changing code.',role='Coder')
    assert changed['version']==p['version']+1
    assert b.update_managed_agent_profile(actor,p['id'],p['version'],'edit',instructions='Inspect before changing code.',role='Coder')==changed
    archived=b.archive_managed_agent_profile(actor,p['id'],changed['version'],'archive')
    assert archived['status']=='archived'
    assert b.archive_managed_agent_profile(actor,p['id'],changed['version'],'archive')==archived
    assert b.list_managed_agent_profiles(actor)==[]
    assert [row['id'] for row in b.list_managed_agent_profiles(actor,True)]==[p['id']]
    restored=b.update_managed_agent_profile(actor,p['id'],archived['version'],'restore',status='paused')
    assert restored['status']=='paused'
    with b._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='managed_agent_profile_created'").fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='managed_agent_profile_archived'").fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM agent_profile_versions WHERE profile_id=?',(p['id'],)).fetchone()[0]==4


def test_versions_and_retry_keys_never_overwrite_newer_human_edits(lead):
    s=lead;b=s['b'];p=create(s)
    saved=b.update_managed_agent_profile(s['actor'],p['id'],p['version'],'edit',name='First')
    newer=b.update_agent_profile(s['human'],p['id'],saved['version'],name='Human newer')
    assert b.update_managed_agent_profile(s['actor'],p['id'],p['version'],'edit',name='First')==saved
    assert b.get_agent_profile(s['human'],p['id'])['name']=='Human newer'
    denied(b.update_managed_agent_profile,s['actor'],p['id'],p['version'],'fresh-stale',name='Overwrite')
    denied(b.update_managed_agent_profile,s['actor'],p['id'],p['version'],'edit',name='Changed retry payload')
    assert b.get_agent_profile(s['human'],p['id'])==newer


def test_self_and_other_management_profiles_are_never_targets(lead):
    s=lead;b=s['b'];second=b.create_agent_profile(s['human'],'Another lead',project_ids=[s['project']['id']],policy=policy())
    second=b.set_agent_profile_management(s['human'],second['id'],second['version'],True)
    for p in (s['profile'],second):
        denied(b.get_managed_agent_profile,s['actor'],p['id'])
        denied(b.update_managed_agent_profile,s['actor'],p['id'],p['version'],'self-'+p['id'],name='Owned')
        denied(b.archive_managed_agent_profile,s['actor'],p['id'],p['version'],'delete-'+p['id'])
    assert not b.list_managed_agent_profiles(s['actor'])


@pytest.mark.parametrize('changes',[
    {'actions':['agent.profile.create']},{'resources':['agent-profiles:alice']},{'actions':['*']},{'resources':['*']},
    {'actions':['runtime.manage']},{'resources':['runtime:all']},{'actions':['server.deploy']},{'resources':['server:all']},
    {'actions':['repo.run_test']},{'resources':['repo:outside']},{'allow_create_projects':True},
])
def test_delegation_cannot_grant_management_runtime_or_any_higher_ceiling(lead,changes):
    s=lead
    denied(s['b'].create_managed_agent_profile,s['actor'],'Escape','escape',project_ids=[s['project']['id']],policy=policy(**changes))
    assert not s['b'].list_managed_agent_profiles(s['actor'])


def test_project_owner_and_existing_higher_grants_cannot_be_read_or_modified(lead):
    s=lead;b=s['b']
    outside=b.create_agent_profile(s['human'],'Outside marker',project_ids=[s['hidden']['id']],policy=policy())
    foreign=b.create_agent_profile(s['other'],'Other owner marker',project_ids=[s['project']['id']],policy=policy())
    stronger=b.create_agent_profile(s['human'],'Higher grant marker',project_ids=[s['project']['id']],policy=policy(actions=['server.deploy']))
    for p in (outside,foreign,stronger):
        denied(b.get_managed_agent_profile,s['actor'],p['id'])
        denied(b.update_managed_agent_profile,s['actor'],p['id'],p['version'],'edit-'+p['id'],name='Steal')
    denied(b.create_managed_agent_profile,s['actor'],'Outside','outside',project_ids=[s['hidden']['id']],policy=policy())
    assert not b.list_managed_agent_profiles(s['actor'])


def test_role_name_and_legacy_owner_wildcard_are_not_management_grants(lead):
    s=lead;b=s['b'];plain=b.create_agent_profile(s['human'],'Looks like a lead',role='Management / Lead',project_ids=[s['project']['id']],policy=policy())
    device=b.create_device(s['human'],'Plain',[]);b.bind_agent_profile(s['human'],plain['id'],plain['version'],device['id'],'plain','codex')
    raw=b.authenticate(device['token']);b.register_agent(raw,'plain','Plain',[], 'codex',False,'plain');actor=b.actor_session(raw,'plain')
    denied(b.create_managed_agent_profile,actor,'Unauthorized','no')
    legacy_device=b.create_device(s['human'],'Legacy',[]);legacy_raw=b.authenticate(legacy_device['token'])
    b.register_agent(legacy_raw,'legacy','Legacy',[], 'codex',False,'legacy');legacy=b.actor_session(legacy_raw,'legacy')
    denied(b.list_managed_agent_profiles,legacy)
    # The label is still ordinary editable data when no management grant exists.
    assert b.get_managed_agent_profile(s['actor'],plain['id'])['role']=='Management / Lead'


def test_live_owner_ceiling_and_lead_profile_revocation_apply_before_retries(lead):
    s=lead;b=s['b'];p=create(s)
    owner=b.get_settings(s['human'])['policy'];owner['actions'].remove('agent.profile.create')
    b.set_policy(s['human'],**{key:owner[key] for key in ('enabled','allow_read','allow_reply','allow_requests','actions','resources')})
    denied(b.create_managed_agent_profile,s['actor'],'Coder','coder',project_ids=[s['project']['id']],policy=policy(autonomy='requested'))
    b.set_agent_profile_management(s['human'],s['profile']['id'],s['profile']['version'],False)
    denied(b.get_managed_agent_profile,s['actor'],p['id'])


def test_archive_retains_real_claim_and_runtime_without_fabricating_cleanup(lead):
    s=lead;b=s['b'];p=create(s)
    device=b.create_device(s['human'],'Coder',[]);p=b.bind_agent_profile(s['human'],p['id'],p['version'],device['id'],'coder','codex')
    raw=b.authenticate(device['token']);b.register_agent(raw,'coder-session','Coder',[], 'codex',False,'coder');actor=b.actor_session(raw,'coder-session')
    ticket=b.create_ticket(s['human'],s['project']['id'],'Work');claimed=b.claim_ticket(actor,ticket['id'],ticket['version'])
    reserved=b.reserve_execution(actor,'active-runtime',s['project']['id'],ticket['id'],'work','turn','codex_chatgpt','synthetic-model')['execution']
    running=b.start_execution(actor,reserved['id'],reserved['revision'])
    b.archive_managed_agent_profile(s['actor'],p['id'],p['version'],'archive-running')
    current=b.get_ticket(s['human'],ticket['id'])['ticket'];runtime=b.get_runtime(s['human'])['executions'][0]
    assert current['claimant']==claimed['claimant'] and current['lease_expires_at']==claimed['lease_expires_at']
    assert runtime['id']==running['id'] and runtime['state']=='running' and not runtime['cleanup_confirmed']
    assert b.get_execution_control(actor,running['id'])['should_stop'] is True


def test_manager_never_uses_human_grant_or_binding_apis(lead):
    s=lead;b=s['b'];p=create(s)
    denied(b.set_agent_profile_management,s['actor'],p['id'],p['version'],True)
    denied(b.bind_agent_profile,s['actor'],p['id'],p['version'],s['device']['id'],'escape','codex')
    denied(b.create_device,s['actor'],'Forged identity',[])


def test_effective_message_and_autonomy_caps_apply_to_create_and_update(lead):
    s=lead;b=s['b'];p=create(s)
    # A live owner restriction immediately narrows the lead's delegation ceiling.
    settings=b.get_settings(s['human'])['policy']
    b.set_policy(s['human'],True,True,False,True,settings['actions'],settings['resources'])
    denied(b.create_managed_agent_profile,s['actor'],'Too talkative','message-cap',
        project_ids=[s['project']['id']],policy=policy())
    # Existing higher permission profiles are outside management scope as well.
    denied(b.update_managed_agent_profile,s['actor'],p['id'],p['version'],'message-edit',name='Hidden')
    profile=b.get_agent_profile(s['human'],s['profile']['id'])
    narrow=b.update_agent_profile(s['human'],profile['id'],profile['version'],policy=profile['policy']|{'autonomy':'requested'})
    # An explicit new session adopts the changed lead configuration.
    b.register_agent(s['raw'],'narrow-lead','Lead',[],'codex',False,'lead')
    actor=b.actor_session(s['raw'],'narrow-lead')
    denied(b.create_managed_agent_profile,actor,'Queue escalation','autonomy-cap',
        project_ids=[s['project']['id']],policy=policy(allow_messages=False,autonomy='queue'))
    ordinary=b.create_managed_agent_profile(actor,'Narrow','narrow',project_ids=[s['project']['id']],
        policy=policy(allow_messages=False,autonomy='requested'))
    denied(b.update_managed_agent_profile,actor,ordinary['id'],ordinary['version'],'edit-cap',
        policy=policy(allow_messages=False,autonomy='queue'))
    assert b.get_agent_profile(s['human'],ordinary['id'])['version']==ordinary['version']


def test_update_cannot_add_project_management_or_archive_without_archive_grant(lead):
    s=lead;b=s['b'];p=create(s)
    for change in ({'project_ids':[s['hidden']['id']]},{'policy':policy(actions=['agent.profile.create'])},
                   {'status':'archived'},{'device_id':s['device']['id']},{'owner_id':'bob'}):
        denied(b.update_managed_agent_profile,s['actor'],p['id'],p['version'],'escape-'+str(len(str(change))),**change)
    settings=b.get_settings(s['human'])['policy']
    b.set_policy(s['human'],True,True,True,True,
        [a for a in settings['actions'] if a!='agent.profile.archive'],settings['resources'])
    denied(b.archive_managed_agent_profile,s['actor'],p['id'],p['version'],'missing-grant')
    assert b.get_agent_profile(s['human'],p['id'])['version']==p['version']
