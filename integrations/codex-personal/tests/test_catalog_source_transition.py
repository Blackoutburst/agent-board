"""Actual Board and Host SQLite transactions; no process/provider/real account."""
from copy import deepcopy
from unittest.mock import patch
import json
import pytest

from blackoutburst_agents.host_config import HostError,digest
from blackoutburst_agents.server_controller import launch_catalog
from blackoutburst_agents.catalog_source_transition import CatalogSourceTransition,LOCAL_METHOD
from test_server_controller import env,current,command


def coordinator(e,**kw):
    return CatalogSourceTransition(e['journal'],e['client'],source_guard=lambda *a:True,
        stopped_guard=lambda:True,**kw)

def plan(e,c=None):
    return (c or coordinator(e)).plan(old_release_sha256='b'*64,new_release_sha256='c'*64,request_key='one-source-change')

def stopped(e):return command(e,'stop','human-stop')

def observed_stopped(e):
    i=current(e)
    return e['board'].report_instance_state(e['actor'],host_id=i['host_id'],instance_id=i['id'],
        expected_observation_revision=i['observation_revision'],generation=i['generation'],
        launchspec_sha256=i['launchspec_sha256'],command_id=i['command_id'],state='stopped',
        cleanup_confirmed=True,reason='stopped',idempotency_key='fixture-stopped-'+str(i['observation_revision']))


def resume(c,identity):
    status=c.status(identity)
    return c.resume(identity,expected_revision=status['revision'],expected_proposal_sha256=status['proposal_sha256'])

def dbdump(e):return '\n'.join(e['journal']._db.iterdump())


def test_real_host2_promotion_and_human_stop_preserve_all_local_data(env):
    e=env;h=e['journal'];stop=stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c);before=h.control();target=h.target('primary');old=h.managed_target('primary')
        done=c.resolve(p)
        assert done['catalog_version']==2
        assert h._db.execute('PRAGMA user_version').fetchone()[0]==3
        assert h._db.execute('SELECT count(*) FROM host_message_abandonments').fetchone()[0]==0
        assert h.control()==before and h.target('primary')==target and h.dispatches()==[]
        new=h.managed_target('primary')
        assert {k:v for k,v in old.items() if k not in ('generation','reason','launchspec_sha256')}=={
            k:v for k,v in new.items() if k not in ('generation','reason','launchspec_sha256')}
        assert new['generation']==stop['generation'] and not new['enabled']
        assert h.managed_settings()['catalog_sha256']==digest(p['new_catalog'])
        assert resume(c,'source-'+digest(p['request_key']))==done
    i=current(e)
    assert i['id']==new['instance_id'] and i['desired_state']=='stopped' and i['observed_state']=='unknown'
    assert i['cleanup_confirmed'] is False
    assert {n for n,a in e['client'].calls}<={'publish_server_catalog','poll_instance_commands','ack_instance_command',
        'report_instance_state','replace_server_catalog'}


def test_no_human_stop_never_acknowledges_start_or_publishes(env):
    e=env;observed_stopped(e);command(e,'start');h=e['journal']
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c)
        with pytest.raises(HostError,match='human_stop_required'):c.resolve(p)
        assert [x for x in e['client'].calls if x[0] in ('ack_instance_command','replace_server_catalog')]==[]
        with pytest.raises(HostError,match='catalog_transition_pending'):e['control'].start_worker()
        assert e['control']._thread is None
        h.queue_managed_command(e['control']._normalized(current(e),current(e)['current_command']))
        with pytest.raises(HostError,match='catalog_transition_pending'):h.apply_managed_command(current(e)['command_id'])
        assert not h.managed_target('primary')['enabled']


def test_lost_ack_replays_exact_stop_and_lost_replace_replays_exact_final_cas(env):
    e=env;h=e['journal'];stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c);identity='source-'+digest(p['request_key'])
        e['client'].lost.add('ack_instance_command')
        with pytest.raises(Exception):c.resolve(p)
        assert h.managed_effect(identity)['state']=='pending'
        assert current(e)['current_command']['status']=='accepted'
        e['client'].lost.add('replace_server_catalog')
        with pytest.raises(Exception):resume(c,identity)
        assert current(e)['launchspec_sha256']==p['new_catalog'][0]['launchspec_sha256']
        assert h.managed_settings()['catalog_sha256']==digest(p['old_catalog'])
        assert resume(c,identity)['catalog_version']==2
        for name in ('ack_instance_command','replace_server_catalog'):
            calls=[a for n,a in e['client'].calls if n==name]
            assert len(calls)==2 and calls[0]==calls[1]
        assert h.managed_target('primary')['remaining']==0


def test_changed_source_or_children_fail_before_intent_or_rpc(env):
    h=env['journal'];c=coordinator(env)
    with h.owner_lock(lock_directory=env['root']/'locks'):
        p=plan(env,c);before=dbdump(env);calls=len(env['client'].calls)
        c.source_guard=lambda *a:False
        with pytest.raises(HostError,match='sources_unconfirmed'):c.resolve(p)
        assert dbdump(env)==before and len(env['client'].calls)==calls
        c.source_guard=lambda *a:True;c.stopped_guard=lambda:False
        with pytest.raises(HostError,match='children_unconfirmed'):c.resolve(p)
        assert dbdump(env)==before and len(env['client'].calls)==calls


def test_crash_after_local_intent_gates_start_and_reopens_only_exact_body(env):
    e=env;h=e['journal'];stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c)
        row=h.begin_catalog_source_transition(p,consume_closures=lambda:[])
        with pytest.raises(HostError,match='pending'):h.require_no_catalog_source_transition()
        bad=deepcopy(p);bad['new_release_sha256']='d'*64
        bad['new_catalog']=sorted(launch_catalog(h.control()['configuration'],'d'*64),key=lambda r:r['catalog_key'])
        bad['proposal_sha256']=digest({k:v for k,v in bad.items() if k!='proposal_sha256'})
        with pytest.raises(HostError):c.resolve(bad)
        with pytest.raises(HostError):h.supersede_managed_effect(row['id'])
        assert resume(c,row['id'])['catalog_version']==2


def test_fresh_start_after_remote_receipt_keeps_local_intent_pending(env):
    e=env;h=e['journal'];stopped(e);real=e['client'].call
    def call(name,args):
        value=real(name,args)
        if name=='replace_server_catalog':
            observed_stopped(e);command(e,'start','human-race')
        return value
    e['client'].call=call
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c)
        with pytest.raises(HostError,match='remote_conflict'):c.resolve(p)
        assert h.managed_settings()['catalog_sha256']==digest(p['old_catalog'])
        assert h.managed_effect('source-'+digest(p['request_key']))['state']=='pending'
        assert h.managed_target('primary')['remaining']==0


def test_old_catalog_bootstrap_receipt_not_used_as_current_binding(env):
    e=env;h=e['journal'];e['client'].calls.clear()
    with h.owner_lock(lock_directory=e['root']/'locks'):
        # No background thread: only the real owner may perform this call.
        e['control']._owns_worker=True
        try:r=e['control'].publish(e['catalog'])
        finally:e['control']._owns_worker=False
    assert r['catalog_version']==1
    assert [n for n,a in e['client'].calls]==['poll_instance_commands']
    assert h.pending_managed_effects()==[]


def test_schema_promotion_rolls_back_if_insert_postcondition_fails(env):
    e=env;h=e['journal'];c=coordinator(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        p=plan(e,c);before=dbdump(e);real=h.managed_effect
        def corrupt(key):
            value=real(key)
            if value is not None and value['method']==LOCAL_METHOD:return {**value,'state':'superseded'}
            return value
        with patch.object(h,'managed_effect',side_effect=corrupt):
            with pytest.raises(HostError):h.begin_catalog_source_transition(p,consume_closures=lambda:[])
        assert dbdump(e)==before and h._db.execute('PRAGMA user_version').fetchone()[0]==2


def test_unknown_table_or_trigger_refuses_before_promotion(env):
    e=env;h=e['journal']
    h._db.execute('CREATE TABLE future_unknown(id)')
    with h.owner_lock(lock_directory=e['root']/'locks'):
        with pytest.raises(HostError,match='schema'):plan(e)
    assert h._db.execute('PRAGMA user_version').fetchone()[0]==2


def test_pending_non_stop_effect_never_replayed_by_transition(env):
    e=env;h=e['journal'];stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c);r=h.begin_catalog_source_transition(p,consume_closures=lambda:[])
        i=current(e)
        h.begin_managed_effect('historical-ready','report_instance_state',{
            'host_id':i['host_id'],'instance_id':i['id'],'expected_observation_revision':i['observation_revision'],
            'generation':i['generation'],'launchspec_sha256':i['launchspec_sha256'],'command_id':i['command_id'],
            'state':'ready','cleanup_confirmed':False,'reason':'','idempotency_key':'historical-ready'})
        with pytest.raises(HostError,match='unsettled'):resume(c,r['id'])
        assert not any(n=='report_instance_state' and a['state']=='ready' for n,a in e['client'].calls)


@pytest.fixture
def real_abandonment():
    from test_message_local_abandonment import MessageLocalAbandonmentTests
    t=MessageLocalAbandonmentTests('test_full_explicit_resolution_retains_original_history_and_budget')
    t.setUp()
    try:yield t
    finally:t.doCleanups()


def test_real_host3_closure_consumed_inside_tx_before_local_intent(real_abandonment):
    t=real_abandonment
    with t.locked():
        abandoned=t.admin.resolve(t.plan())
        c=CatalogSourceTransition(t.host,None,source_guard=lambda *a:True,stopped_guard=lambda:True,abandonment=t.admin)
        p=c.plan(old_release_sha256='b'*64,new_release_sha256='c'*64,request_key='after-real-abandonment')
        assert p['closures'][0]['intent_id']==abandoned['intent_id']
        before=[tuple(r) for r in t.host._db.execute('SELECT * FROM host_message_abandonments')]
        original=t.admin.status;called=[]
        def checked(**kw):
            assert t.host._db.in_transaction
            assert t.host.pending_managed_effects()==[]
            called.append(kw);return original(**kw)
        with patch.object(t.admin,'status',side_effect=checked):
            result=t.host.begin_catalog_source_transition(p,consume_closures=c._closure_rows)
        assert called==[{'intent_id':abandoned['intent_id']}]
        assert result['state']=='pending' and result['method']==LOCAL_METHOD
        assert [tuple(r) for r in t.host._db.execute('SELECT * FROM host_message_abandonments')]==before
        # Original full Host checkpoint is history now; never rewrite it to
        # pretend the new effect had existed at the abandonment boundary.
        with pytest.raises(HostError):t.admin.status(intent_id=abandoned['intent_id'])
        assert t.calls==0


def test_closure_changed_between_plan_and_begin_rolls_back_host_intent(real_abandonment):
    t=real_abandonment
    with t.locked():
        t.admin.resolve(t.plan())
        c=CatalogSourceTransition(t.host,None,source_guard=lambda *a:True,stopped_guard=lambda:True,abandonment=t.admin)
        p=c.plan(old_release_sha256='b'*64,new_release_sha256='c'*64,request_key='changed-child')
        t.message_runtime.db.execute("UPDATE executions SET start_confirmed=1")
        before='\n'.join(t.host._db.iterdump())
        with pytest.raises(HostError):t.host.begin_catalog_source_transition(p,consume_closures=c._closure_rows)
        assert '\n'.join(t.host._db.iterdump())==before and t.host.pending_managed_effects()==[]
        assert t.calls==0


def test_stop_boundary_without_local_intent_has_no_effect(env):
    e=env;h=e['journal'];stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c);before=dbdump(e);calls=len(e['client'].calls)
        with pytest.raises(HostError,match='intent_required'):c._finite_stop(p)
        assert dbdump(e)==before and len(e['client'].calls)==calls


def test_resume_cas_revision_and_proposal_checked_before_rpc(env):
    e=env;h=e['journal'];stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c);r=h.begin_catalog_source_transition(p,consume_closures=lambda:[])
        assert c.status(r['id'])=={'intent_id':r['id'],'revision':1,'phase':'stop_pending','proposal_sha256':p['proposal_sha256']}
        before=dbdump(e);calls=len(e['client'].calls)
        for rev,sha in ((True,p['proposal_sha256']),(2,p['proposal_sha256']),(1,'0'*64)):
            with pytest.raises(HostError,match='revision_conflict'):
                c.resume(r['id'],expected_revision=rev,expected_proposal_sha256=sha)
        assert dbdump(e)==before and len(e['client'].calls)==calls
        assert resume(c,r['id'])['catalog_version']==2
        assert c.status(r['id'])['revision']==4


def test_authorization_withdrawn_after_replacement_does_not_commit_local(env):
    e=env;h=e['journal'];stopped(e);real=e['client'].call
    def call(name,args):
        value=real(name,args)
        if name=='replace_server_catalog':e['board'].set_policy(e['human'],True,False,True,True,[],[])
        return value
    e['client'].call=call
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c)
        with pytest.raises(HostError,match='remote_conflict'):c.resolve(p)
        assert h.managed_settings()['catalog_sha256']==digest(p['old_catalog'])
        assert c.status('source-'+digest(p['request_key']))['phase']=='commit_pending'
        assert h.managed_target('primary')['remaining']==0


def test_guard_failure_inside_final_local_tx_rolls_back_hash_and_intent_completion(env):
    e=env;h=e['journal'];stopped(e);c=coordinator(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        p=plan(e,c);real=h.commit_catalog_source_transition
        def commit(*args,**kw):
            check=kw['recheck'];count=[0]
            def fail_second():
                count[0]+=1;check()
                if count[0]==2:raise HostError('catalog_transition_children_unconfirmed')
            return real(*args,recheck=fail_second)
        with patch.object(h,'commit_catalog_source_transition',side_effect=commit):
            with pytest.raises(HostError,match='children_unconfirmed'):c.resolve(p)
        assert h.managed_settings()['catalog_sha256']==digest(p['old_catalog'])
        assert c.status('source-'+digest(p['request_key']))['phase']=='commit_pending'
        assert resume(c,'source-'+digest(p['request_key']))['catalog_version']==2


def test_new_controller_binds_existing_catalog_without_bootstrap_then_only_new_start(env):
    from test_server_controller import wait_for
    e=env;h=e['journal'];stop=stopped(e)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c=coordinator(e);p=plan(e,c);c.resolve(p)
    i=current(e)
    assert i['current_command']['launchspec_sha256']==p['old_catalog'][0]['launchspec_sha256']
    assert i['current_command']['instance_version']==i['version']-1
    e['client'].calls.clear();control=e['control'];control.start_worker()
    try:
        result=control.publish(p['new_catalog'])
        assert result['catalog_version']==2
        assert [n for n,a in e['client'].calls]==['poll_instance_commands']
        control.step()
        assert not h.managed_target('primary')['enabled'] and current(e)['cleanup_confirmed'] is True
        assert current(e)['observed_state']=='stopped' and current(e)['command_id']==stop['id']
        new=command(e,'start','new-human-start');control.step()
        wait_for(lambda:h.managed_target('primary')['enabled'])
        assert new['launchspec_sha256']==p['new_catalog'][0]['launchspec_sha256']
        assert new['generation']>stop['generation'] and h.managed_target('primary')['remaining']==1
        assert not any(n=='publish_server_catalog' for n,a in e['client'].calls)
    finally:control.shutdown()


def test_full_child_guard_never_called_under_open_sqlite_transaction(env):
    e=env;h=e['journal'];stopped(e);calls=[]
    def children():
        assert not h._db.in_transaction
        calls.append('children');return True
    def sources(*args):
        calls.append('source-in-tx' if h._db.in_transaction else 'source');return True
    c=CatalogSourceTransition(h,e['client'],source_guard=sources,stopped_guard=children)
    with h.owner_lock(lock_directory=e['root']/'locks'):
        c.resolve(plan(e,c))
    assert 'source-in-tx' in calls and 'children' in calls


def test_oversized_effect_refused_before_body_select(env):
    h=env['journal']
    h._db.execute("INSERT INTO managed_effects VALUES('large','catalog_source_transition',?,'hash','pending','{}')",('x'*32769,))
    db=h._db;queries=[]
    class Trace:
        def execute(self,sql,*a):
            queries.append(sql)
            if sql.startswith('SELECT * FROM managed_effects'):raise AssertionError('body must not be loaded')
            return db.execute(sql,*a)
    h._db=Trace()
    try:
        with pytest.raises(HostError,match='host_corrupt'):h.managed_effect('large')
        assert len(queries)==1 and 'length(CAST(arguments AS BLOB))' in queries[0]
    finally:h._db=db
