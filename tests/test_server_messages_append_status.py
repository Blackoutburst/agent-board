"""A manager's bounded inability report never becomes append or cleanup authority."""
from copy import deepcopy
import pytest
from board import BoardError
from server_messages_append import STATUS_KEYS, STATUS_REASONS, validate_messages_append_arguments
from test_server_instances import env, command, ack, report
from test_server_provisioning import snapshot
from test_server_messages_append import ready, create, prepare, later, grant, cancel


def status(e,r,reason='slot_unavailable',key='blocked-report',**changes):
    args=dict(host_id=e[5]['id'],request_id=r['request_id'],expected_revision=r['revision'],status='blocked',reason=reason,idempotency_key=key)
    args.update(changes)
    return e[0].report_server_messages_append_status(e[3],**args),args


def test_report_is_current_revision_metadata_preserving_phase_proof_and_all_prior_work(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v)
    before=snapshot(b,exclude=('server_messages_append_statuses','server_messages_append_receipts','audit'))
    result,args=status(e,r)
    assert set(result)==STATUS_KEYS and result['request_revision']==1 and result['status']=='blocked'
    current=b.get_server_messages_append(h,host['id'],r['request_id'])
    assert current=={**r,'controller_status':'blocked','controller_reason':'slot_unavailable'}
    assert current['state']=='requested'
    assert current['can_cancel'] is True and current['barrier_held'] is True and current['proof'] is None
    assert snapshot(b,exclude=('server_messages_append_statuses','server_messages_append_receipts','audit'))==before
    saved=snapshot(b);assert b.report_server_messages_append_status(raw,**args)==result and snapshot(b)==saved
    assert b.list_server_messages_appends(h,host['id'])['requests']==[current]
    assert b.poll_server_messages_appends(raw,host['id'])['requests'][0]['request']==current
    # A successful phase changes only the phase; the old report stays historical.
    prepared,_=prepare(e,current)
    assert prepared['controller_status']=='not_reported' and prepared['controller_reason']==''
    assert prepared['proof']['phase']=='prepared' and prepared['can_cancel'] is False
    before=snapshot(b)
    with pytest.raises(BoardError,match='changed'):b.report_server_messages_append_status(raw,**args)
    assert snapshot(b)==before
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM server_messages_append_statuses').fetchone()[0]==1


@pytest.mark.parametrize('reason',sorted(STATUS_REASONS))
def test_only_fixed_metadata_codes_are_retained(ready,reason):
    e,p,v=ready;r=create(e,v);result,_=status(e,r,reason)
    assert result['reason']==reason


@pytest.mark.parametrize('change',[
 {'status':'ready'},{'status':'clear'},{'status':True},{'reason':'PRIVATE SENTINEL'},
 {'reason':''},{'reason':{'message':'PRIVATE'}},{'reason':['source_changed']},
 {'expected_revision':True},{'expected_revision':6},{'raw_error':'PRIVATE'},{'reason':'x'*100000},
])
def test_status_wire_never_accepts_raw_text_unknowns_or_positive_authority(ready,change):
    e,p,v=ready;r=create(e,v);args=dict(host_id=e[5]['id'],request_id=r['request_id'],expected_revision=1,status='blocked',reason='source_changed',idempotency_key='bad')
    args.update(change);before=snapshot(e[0])
    with pytest.raises(BoardError):validate_messages_append_arguments('report_server_messages_append_status',args)
    if 'raw_error' not in args:
        with pytest.raises(BoardError):e[0].report_server_messages_append_status(e[3],**args)
    assert snapshot(e[0])==before


@pytest.mark.parametrize('revoke',['grant','owner-work','profile','device'])
def test_independent_reporting_channel_uses_fresh_manager_device_not_launch_grants(ready,revoke):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v);result,args=status(e,r)
    if revoke=='grant':grant(e,enabled=False,key='revoke-grant')
    elif revoke=='owner-work':b.set_policy(h,False,False,False,False,[],[])
    else:
        with b._tx() as db:
            if revoke=='profile':db.execute("UPDATE agent_profiles SET status='paused' WHERE id=?",(p['id'],))
            else:db.execute('UPDATE devices SET revoked_at=1 WHERE id=?',(e[2]['id'],))
    before=snapshot(b)
    if revoke=='device':
        with pytest.raises(BoardError):b.report_server_messages_append_status(raw,**args)
        assert snapshot(b)==before
    else:
        assert b.report_server_messages_append_status(raw,**args)==result
        newer,_=status(e,r,'authorization_changed',key='new-negative-report')
        assert newer['reason']=='authorization_changed'
        current=b.get_server_messages_append(h,host['id'],r['request_id'])
        assert current['controller_status']=='blocked' and current['controller_reason']=='authorization_changed'
        assert current['can_cancel'] is True and current['proof'] is None
        # Reporting does not allow a positive phase under revoked authority.
        with pytest.raises(BoardError):prepare(e,r)


@pytest.mark.parametrize('who',['human','cross-owner','other-own-device'])
def test_report_cannot_promote_an_unrelated_actor(ready,who):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v)
    if who=='human':actor=h
    elif who=='cross-owner':actor=b.authenticate(b.create_device({'kind':'human','owner_id':'two'},'Other',[])['token'])
    else:actor=b.authenticate(b.create_device(h,'Other',[])['token'])
    before=snapshot(b)
    with pytest.raises(BoardError):b.report_server_messages_append_status(actor,host['id'],r['request_id'],1,'blocked','local_state_conflict','other')
    assert snapshot(b)==before


def test_complete_can_explain_pending_local_reconciliation_without_new_barrier_or_cleanup_claim(ready):
    e,p,v=ready;r=create(e,v);r,_=prepare(e,r);r,_=later(e,r,'precommit');r,_=later(e,r,'commit');r,_=later(e,r,'complete')
    before=snapshot(e[0],exclude=('server_messages_append_statuses','server_messages_append_receipts','audit'))
    result,_=status(e,r,'requires_reconciliation')
    current=e[0].get_server_messages_append(e[1],e[5]['id'],r['request_id'])
    assert current['state']=='complete' and current['revision']==5 and current['proof']==r['proof']
    assert current['barrier_held'] is False and current['can_cancel'] is False
    assert current['controller_status']=='blocked' and current['controller_reason']=='requires_reconciliation'
    assert snapshot(e[0],exclude=('server_messages_append_statuses','server_messages_append_receipts','audit'))==before


def test_cancelled_request_never_takes_a_new_controller_signal(ready):
    e,p,v=ready;r=create(e,v);status(e,r);cancelled=cancel(e,r);before=snapshot(e[0])
    assert cancelled['controller_status']=='not_reported' and cancelled['controller_reason']==''
    with pytest.raises(BoardError):status(e,cancelled)
    assert snapshot(e[0])==before


def test_idempotency_does_not_rewrite_or_erase_another_fixed_code(ready):
    e,p,v=ready;r=create(e,v);first,args=status(e,r);second,_=status(e,r,'connection_lost',key='second')
    before=snapshot(e[0]);assert e[0].report_server_messages_append_status(e[3],**args)==first
    assert e[0].get_server_messages_append(e[1],e[5]['id'],r['request_id'])['controller_reason']=='connection_lost'
    with pytest.raises(BoardError,match='idempotency_conflict'):e[0].report_server_messages_append_status(e[3],**{**args,'reason':'source_changed'})
    assert snapshot(e[0])==before


@pytest.mark.parametrize('fault',['record','receipt','extra'])
def test_final_postcondition_after_status_audit_rolls_back_whole_mutation(ready,monkeypatch,fault):
    e,p,v=ready;b=e[0];r=create(e,v);original=b._audit
    def inject(db,actor,event,detail):
        result=original(db,actor,event,detail)
        if event=='server_messages_append_controller_status':
            if fault=='record':db.execute("UPDATE server_messages_append_requests SET revision=2,state='cancelled' WHERE id=?",(r['request_id'],))
            elif fault=='receipt':db.execute("DELETE FROM server_messages_append_receipts WHERE operation='controller-status'")
            else:db.execute("UPDATE owners SET display_name='changed' WHERE id='two'")
        return result
    monkeypatch.setattr(b,'_audit',inject);before=snapshot(b)
    with pytest.raises(BoardError):status(e,r)
    assert snapshot(b)==before


def test_status_uses_real_ServerControlClient_wire_without_enabling_any_phase(ready):
    from blackoutburst_agents.server_control_client import ServerControlClient
    e,p,v=ready;b=e[0];r=create(e,v);client=object.__new__(ServerControlClient);calls=[]
    client._initialize=lambda:None
    def dispatch(name,args):
        calls.append(name);return getattr(b,name)(e[3],**args)
    client._tool=dispatch
    args=dict(host_id=e[5]['id'],request_id=r['request_id'],expected_revision=1,status='blocked',reason='slot_unavailable',idempotency_key='client-status')
    result=client.call('report_server_messages_append_status',args)
    assert set(result)==STATUS_KEYS and result['reason']=='slot_unavailable'
    polled=client.call('poll_server_messages_appends',dict(host_id=e[5]['id'],limit=1))
    assert polled['requests'][0]['request']['controller_status']=='blocked'
    assert calls==['report_server_messages_append_status','poll_server_messages_appends']


def test_status_cannot_hide_a_cross_linked_host_record(ready):
    e,p,v=ready;b=e[0];r=create(e,v);status(e,r)
    with b._connection() as db:
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute("UPDATE server_messages_append_statuses SET host_id='different-host'")
    before=snapshot(b)
    with pytest.raises(BoardError):b.get_server_messages_append(e[1],e[5]['id'],r['request_id'])
    with pytest.raises(BoardError):status(e,r,key='new')
    assert snapshot(b)==before


def test_real16_migration_keeps_existing_A_projection_and_adds_empty_optional_status_table(env):
    from board import Board
    from server_messages_append import SQL
    from test_server_provisioning import stop,profile as profile_a,grant as grant_a,body as body_a,request as request_a
    e=env;b,h,_,_,_,host,*_=e;stop(e);p=profile_a(e);grant_a(e);r=request_a(e,body_a(e,p))
    with b._tx() as db:
        for name in reversed(tuple(SQL)):
            db.execute(('DROP INDEX ' if name.endswith('one_active') else 'DROP TABLE ')+name)
        db.execute('PRAGMA user_version=16')
    before=snapshot(b);reopened=Board(b.data_dir)
    assert snapshot(reopened,exclude=tuple(SQL)[:5])==before
    assert reopened.get_server_provisioning_request(h,host['id'],r['request_id'])==r
    with reopened._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==17
        assert db.execute('SELECT COUNT(*) FROM server_messages_append_statuses').fetchone()[0]==0
