"""Real SQLite B boundaries; no private source, credential or executor exists."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading
import uuid

import pytest
from board import Board, BoardError
from server_messages_append import (GRANT_SCHEMA, REQUEST_SCHEMA, CANCEL_SCHEMA, HASH_KEYS, SQL,
    _hash, validate_messages_append_arguments)
from server_instances import catalog_fence
from test_server_instances import env, command, ack, report
from test_server_provisioning import stop, snapshot, profile as profile_a, grant as grant_a, body as body_a, request as request_a


def desired(e, *, identity='append-slot', model='gpt-6.1-sol', provider='codex_chatgpt', device=None):
    b,h,manager,raw=e[:4]
    p=b.create_agent_profile(h,'New responder',preferred_provider=provider,preferred_model=model,
        policy={'allow_read':True,'allow_messages':True,'allow_requests':False,'actions':[],'resources':[],'autonomy':'responses'})
    p=b.bind_agent_profile(h,p['id'],p['version'],manager['id'],identity,'codex')
    slot_device=device or manager
    if device is not None:
        p=b.bind_agent_profile(h,p['id'],p['version'],device['id'],identity,'codex')
    actor=b.authenticate(slot_device['token'])
    b.register_agent(actor,'session-'+p['id'],'Slot',agent_kind='codex',identity_key=identity)
    return p


def grant(e, *, enabled=True, key='b-grant'):
    b,h,_,_,_,host,*_=e
    g=b.get_server_messages_append_grant(h,host['id'])
    args=dict(schema=GRANT_SCHEMA,expected_host_version=b.get_server_host(h,host['id'])['version'],
        expected_grant_version=g['version'],enabled=enabled,idempotency_key=key)
    return b.set_server_messages_append_grant(h,host['id'],**args),args


def body(e,p,key='b-request',slot_index=0):
    c=e[0].get_server_messages_append_context(e[1],e[5]['id'],p['id'])
    keys='expected_host_version expected_catalog_version expected_instances_sha256 grant_version profile_id profile_version profile_sha256 provider_id requested_model'.split()
    return dict(schema=REQUEST_SCHEMA,**{k:c[k] for k in keys},slot_id=c['slots'][slot_index]['slot_id'],idempotency_key=key)


def create(e,v): return e[0].create_server_messages_append(e[1],e[5]['id'],**v)


def prepare(e,r,key='b-prepare',**changes):
    args=dict(host_id=e[5]['id'],request_id=r['request_id'],expected_revision=1,operation_id=str(uuid.uuid4()),
        **{k:format(i+1,"x")*64 for i,k in enumerate(HASH_KEYS)},idempotency_key=key)
    args.update(changes)
    proof=e[0].prepare_server_messages_append(e[3],**args)
    from blackoutburst_agents.message_append_protocol import response
    assert response('prepare_server_messages_append',proof,args)==proof
    row=e[0].get_server_messages_append(e[1],e[5]['id'],r['request_id'])
    assert row['proof']==proof
    return row,args


def later(e,r,phase,key=None,**changes):
    args=dict(host_id=e[5]['id'],request_id=r['request_id'],expected_revision=r['revision'],proof=r['proof'],idempotency_key=key or 'b-'+phase)
    if phase=='commit': args['entry']=dict(catalog_key='target-b',profile_id=r['profile_id'],profile_version=r['profile_version'],mode='messages',launchspec_sha256='b'*64)
    args.update(changes)
    proof=getattr(e[0],phase+'_server_messages_append')(e[3],**args)
    from blackoutburst_agents.message_append_protocol import response
    assert response(phase+'_server_messages_append',proof,args)==proof
    row=e[0].get_server_messages_append(e[1],e[5]['id'],r['request_id'])
    assert row['proof']==proof
    return row,args


def cancel(e,r,key='b-cancel'):
    return e[0].cancel_server_messages_append(e[1],e[5]['id'],r['request_id'],schema=CANCEL_SCHEMA,expected_revision=r['revision'],idempotency_key=key)


@pytest.fixture
def ready(env):
    stop(env);p=desired(env);grant(env)
    return env,p,body(env,p)


def test_real_phases_exact_replay_and_inactive_membership(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e
    preserved=snapshot(b,exclude=tuple(SQL)[:5]+('audit',))
    r=create(e,v)
    assert r['state']=='requested' and r['can_cancel'] and r['proof'] is None and r['instance_id'] is None
    assert snapshot(b,exclude=tuple(SQL)[:5]+('audit',))==preserved
    assert create(e,v)==r
    polled=b.poll_server_messages_appends(raw,host['id'])
    assert polled['requests'][0]['body']==v and polled['requests'][0]['slot']['slot_id']==v['slot_id']
    old=b.get_server_host(h,host['id'])['instances'][0]
    with b._connection() as db:
        old_row=dict(db.execute('SELECT * FROM server_instances WHERE id=?',(old['id'],)).fetchone())
        old_commands=[dict(x) for x in db.execute('SELECT * FROM server_instance_commands')]
    prepared,pa=prepare(e,r); assert prepared['revision']==2 and not prepared['can_cancel']
    assert b.prepare_server_messages_append(raw,**pa)==prepared['proof']
    with pytest.raises(BoardError): cancel(e,prepared)
    pre,pr=later(e,prepared,'precommit');assert pre['revision']==3
    committed,co=later(e,pre,'commit');assert committed['revision']==4 and committed['barrier_held']
    assert b.commit_server_messages_append(raw,**co)==committed['proof']
    assert b.prepare_server_messages_append(raw,**pa)==prepared['proof']  # historic delivery only
    with b._connection() as db:
        assert dict(db.execute('SELECT * FROM server_instances WHERE id=?',(old['id'],)).fetchone())==old_row
        assert [dict(x) for x in db.execute('SELECT * FROM server_instance_commands')]==old_commands
        new=dict(db.execute('SELECT * FROM server_instances WHERE id=?',(committed['instance_id'],)).fetchone())
        assert new['active']==1 and new['desired_state']=='stopped' and new['observed_state']=='unknown'
        assert new['cleanup_confirmed']==0 and new['command_id'] is None and new['generation']==0
    complete,ca=later(e,committed,'complete');assert complete['revision']==5 and not complete['barrier_held']
    assert complete['connection_status']==complete['model_catalog_status']==complete['model_access_status']=='not_attested'
    assert b.complete_server_messages_append(raw,**ca)==complete['proof']
    assert b.get_runtime(h)['active_count']==0
    assert b.list_server_messages_appends(h,host['id'])['requests']==[complete]
    assert b.poll_server_messages_appends(raw,host['id'])['requests']==[]
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM messages').fetchone()[0]==0


def test_cancel_requested_only_preserves_slot_and_no_work(ready):
    e,p,v=ready; r=create(e,v);before=snapshot(e[0],exclude=tuple(SQL)[:5]+('audit',))
    final=cancel(e,r)
    assert final['state']=='cancelled' and final['reason']=='cancelled_by_owner' and not final['barrier_held']
    assert not final['can_cancel'] and final['proof'] is None
    assert cancel(e,r)==final and snapshot(e[0],exclude=tuple(SQL)[:5]+('audit',))==before


@pytest.mark.parametrize('which',['agent','other-owner','other-device'])
def test_human_and_manager_owner_device_isolation(ready,which):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v)
    other=b.create_device(h,'Other',[])
    actor=raw if which=='agent' else {'kind':'human','owner_id':'two'} if which=='other-owner' else b.authenticate(other['token'])
    saved=snapshot(b)
    with pytest.raises(BoardError): b.create_server_messages_append(actor,host['id'],**v)
    if which!='agent':
        with pytest.raises(BoardError): b.poll_server_messages_appends(actor,host['id'])
    assert snapshot(b)==saved


@pytest.mark.parametrize('field,value',[
 ('expected_host_version',True),('expected_catalog_version',1.0),('grant_version','1'),('profile_version',False),
 ('slot_id','A'*64),('profile_sha256','z'*64),('requested_model',None),('requested_model',''),
 ('provider_id','claude_code'),('schema','apply-now'),('token','SENTINEL'),('idempotency_key','\ud800'),
])
def test_closed_human_wire_rejects_unknown_and_coercions(ready,field,value):
    e,p,v=ready;v={**v,field:value};before=snapshot(e[0])
    with pytest.raises(BoardError):create(e,v)
    assert snapshot(e[0])==before


@pytest.mark.parametrize('change',['grant','binding','session-version','slot-device','owner-policy','profile-version','profile-preference'])
def test_fresh_authority_before_cached_request_and_prepare(ready,change):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v);prepared,args=prepare(e,r)
    if change=='grant':grant(e,enabled=False,key='revoke')
    else:
        with b._tx() as db:
            if change=='binding':db.execute('DELETE FROM agent_profile_bindings WHERE profile_id=?',(p['id'],))
            elif change=='session-version':db.execute('UPDATE sessions SET profile_version=profile_version+1 WHERE agent_id=?',(p['id'],))
            elif change=='slot-device':db.execute('UPDATE devices SET revoked_at=1 WHERE id=?',(e[2]['id'],))
            elif change=='owner-policy':
                policy=json.loads(db.execute('SELECT policy FROM owners WHERE id=?',(h['owner_id'],)).fetchone()[0]);policy['allow_reply']=False
                db.execute('UPDATE owners SET policy=? WHERE id=?',(json.dumps(policy),h['owner_id']))
            elif change=='profile-version':db.execute('UPDATE agent_profiles SET version=version+1 WHERE id=?',(p['id'],))
            else:
                fields=json.loads(db.execute('SELECT fields FROM agent_profiles WHERE id=?',(p['id'],)).fetchone()[0]);fields['preferred_model']='another'
                db.execute('UPDATE agent_profiles SET fields=? WHERE id=?',(json.dumps(fields),p['id']))
    before=snapshot(b)
    with pytest.raises(BoardError):create(e,v)
    with pytest.raises(BoardError):b.prepare_server_messages_append(raw,**args)
    assert snapshot(b)==before


def test_manager_and_slot_bindings_both_required_and_explicit(env):
    stop(env);b,h,manager,raw,_,host,*_=env
    other=b.create_device(h,'Own slot',[]);p=desired(env,device=other)
    c=b.get_server_messages_append_context(h,host['id'],p['id'])
    assert len(c['slots'])==1 and c['slots'][0]['device_id']==other['id']
    slot=c['slots'][0];assert slot['slot_id']==_hash({k:v for k,v in slot.items() if k!='slot_id'})
    with b._tx() as db:db.execute('DELETE FROM agent_profile_bindings WHERE profile_id=? AND device_id=?',(p['id'],manager['id']))
    with pytest.raises(BoardError):b.get_server_messages_append_context(h,host['id'],p['id'])


@pytest.mark.parametrize('model,provider',[('', 'codex_chatgpt'),('model','claude_code')])
def test_context_never_admits_an_unsupported_template(env,model,provider):
    p=desired(env,model=model,provider=provider)
    with pytest.raises(BoardError):env[0].get_server_messages_append_context(env[1],env[5]['id'],p['id'])


def test_symmetric_A_B_barriers_before_cached_delivery(ready):
    e,p,v=ready;b,h,_,raw,_,host,entry,*_=e
    a=profile_a(e);grant_a(e);av=body_a(e,a);ar=request_a(e,av)
    before=snapshot(b)
    with pytest.raises(BoardError,match='provisioning_pending'):create(e,v)
    assert snapshot(b)==before
    b.cancel_server_provisioning_request(h,host['id'],ar['request_id'],schema='server-provision-draft-cancel-v1',expected_revision=1,idempotency_key='cancel-a')
    # The B fence is unchanged by inert A metadata/cancellation.
    r=create(e,v);i=b.get_server_host(h,host['id'])['instances'][0];before=snapshot(b)
    with pytest.raises(BoardError,match='messages_append_pending'):request_a(e,av)
    with pytest.raises(BoardError,match='messages_append_pending'):b.publish_server_catalog(raw,host['id'],0,[entry],'publish')
    with pytest.raises(BoardError,match='messages_append_pending'):command(e,'start',instance=i)
    with pytest.raises(BoardError,match='messages_append_pending'):command(e,'restart',instance=i)
    with pytest.raises(BoardError,match='messages_append_pending'):report(e,'ready',False,key='positive',instance=i)
    with pytest.raises(BoardError,match='messages_append_pending'):b.create_server_host(h,e[2]['id'],'Server','grant')
    assert snapshot(b)==before
    # Stop/cleanup retains its existing channel after owner work revocation.
    b.set_policy(h,False,False,False,False,[],[])
    stop_command=command(e,'stop','stop-again');ack(e,stop_command);report(e)
    assert b.poll_instance_commands(raw,host['id'])['instances'][0]['desired_state']=='stopped'


@pytest.mark.parametrize('change',['fence','proof-extra','proof-pin','proof-bool','entry-mode','entry-profile','entry-key'])
def test_transition_requires_exact_previous_proof_entry_and_fence(ready,change):
    e,p,v=ready;b=e[0];r=create(e,v);r,_=prepare(e,r);r,_=later(e,r,'precommit')
    overrides={}
    if change=='fence':report(e,key='new-observation')
    elif change.startswith('proof-'):
        proof=deepcopy(r['proof'])
        if change=='proof-extra':proof['private']='SENTINEL'
        elif change=='proof-pin':proof['children_roster_sha256']='0'*64
        else:proof['expected_host_version']=True
        overrides['proof']=proof
    else:
        entry=dict(catalog_key='target-b',profile_id=p['id'],profile_version=p['version'],launchspec_sha256='b'*64,mode='messages')
        entry[{'entry-mode':'mode','entry-profile':'profile_id','entry-key':'catalog_key'}[change]]={'entry-mode':'ticket_text','entry-profile':e[4]['id'],'entry-key':'target-a'}[change]
        overrides['entry']=entry
    before=snapshot(b)
    with pytest.raises(BoardError):later(e,r,'commit',**overrides)
    assert snapshot(b)==before


def test_completed_B_closes_ordinary_publish_identity_bypass(ready):
    e,p,v=ready;b,h,_,raw,_,host,old,*_=e;r=create(e,v);r,_=prepare(e,r);r,_=later(e,r,'precommit');r,args=later(e,r,'commit');r,_=later(e,r,'complete')
    before=snapshot(b)
    with pytest.raises(BoardError,match='identity_required'):b.publish_server_catalog(raw,host['id'],0,[old],'publish')
    with pytest.raises(BoardError,match='identity_required'):b.publish_server_catalog(raw,host['id'],2,[old],'remove-b')
    assert snapshot(b)==before


@pytest.mark.parametrize('kind',['insert','audit','temp-trigger','temp-shadow','view','extra-index','unknown-table'])
def test_sensitive_schema_rejects_effects_and_shadows_before_mutation(ready,kind):
    e,p,v=ready;b=e[0]
    with b._connection() as db:
        if kind=='insert':db.execute('CREATE TRIGGER x AFTER INSERT ON server_messages_append_requests BEGIN UPDATE owners SET display_name="changed";END')
        elif kind=='audit':db.execute('CREATE TRIGGER x AFTER INSERT ON audit BEGIN DELETE FROM server_messages_append_barriers;END')
        elif kind=='temp-trigger':db.execute('CREATE TEMP TRIGGER x AFTER INSERT ON main.server_messages_append_receipts BEGIN SELECT 1;END')
        elif kind=='temp-shadow':db.execute('CREATE TEMP TABLE server_messages_append_grants(x)')
        elif kind=='view':
            db.execute('DROP TABLE server_messages_append_grants');db.execute('CREATE VIEW server_messages_append_grants AS SELECT 1')
        elif kind=='extra-index':db.execute('CREATE INDEX x ON server_messages_append_requests(host_id)')
        else:db.execute('CREATE TABLE server_messages_append_surprise(x)')
        before=db.total_changes
        with pytest.raises(BoardError,match='schema_invalid'):b._messages_append_schema(db)
        assert db.total_changes==before


@pytest.mark.parametrize('phase',['request','prepared','precommit','committed','complete','cancel'])
def test_postcondition_detects_extra_write_after_receipt_and_rolls_back(ready,phase,monkeypatch):
    e,p,v=ready;b=e[0];r=None
    if phase!='request':r=create(e,v)
    if phase in ('precommit','committed','complete'):r,_=prepare(e,r)
    if phase in ('committed','complete'):r,_=later(e,r,'precommit')
    if phase=='complete':r,_=later(e,r,'commit')
    original=b._audit
    def late(db,actor,event,detail):
        result=original(db,actor,event,detail)
        if event=='server_messages_append_'+phase:db.execute("UPDATE owners SET display_name='changed' WHERE id='two'")
        return result
    monkeypatch.setattr(b,'_audit',late);before=snapshot(b)
    with pytest.raises(BoardError,match='postcondition'):
        if phase=='request':create(e,v)
        elif phase=='cancel':cancel(e,r)
        elif phase=='prepared':prepare(e,r)
        else:later(e,r,{'precommit':'precommit','committed':'commit','complete':'complete'}[phase])
    assert snapshot(b)==before


def test_concurrent_create_exact_idempotency_and_different_key(ready):
    e,p,v=ready;b=e[0];barrier=threading.Barrier(2)
    def call(value):
        barrier.wait()
        try:return create(e,value)
        except BoardError as ex:return ex
    with ThreadPoolExecutor(2) as pool:values=list(pool.map(call,[v,v]))
    assert values[0]==values[1] and values[0]['state']=='requested'
    with pytest.raises(BoardError):create(e,{**v,'idempotency_key':'other'})
    assert len(b.list_server_messages_appends(e[1],e[5]['id'])['requests'])==1


def test_migration_16_to_17_is_atomic_and_preserves_every_existing_row(ready,monkeypatch):
    import board as module
    e,p,v=ready;b=e[0]
    with b._tx() as db:
        for name in reversed(tuple(SQL)):
            db.execute(('DROP INDEX ' if name.endswith('one_active') else 'DROP TABLE ')+name)
        db.execute('PRAGMA user_version=16')
    before=snapshot(b);original=module.migrate_server_messages_append
    def fail(db):
        db.execute(next(iter(SQL.values())));raise RuntimeError('synthetic migration')
    monkeypatch.setattr(module,'migrate_server_messages_append',fail)
    with pytest.raises(RuntimeError,match='synthetic'):Board(b.data_dir)
    assert snapshot(b)==before
    with b._connection() as db:assert db.execute('PRAGMA user_version').fetchone()[0]==16
    monkeypatch.setattr(module,'migrate_server_messages_append',original);reopened=Board(b.data_dir)
    assert snapshot(reopened,exclude=tuple(SQL)[:5])==before
    with reopened._connection() as db:assert db.execute('PRAGMA user_version').fetchone()[0]==17


@pytest.mark.parametrize('name,args',[
 ('poll_server_messages_appends',{'host_id':'host','limit':True}),
 ('poll_server_messages_appends',{'host_id':'host','limit':17}),
 ('poll_server_messages_appends',{'host_id':'host','limit':1,'unknown':None}),
 ('prepare_server_messages_append',{}),
 ('run_server_messages_append',{}),
])
def test_raw_tool_arguments_are_closed_without_coercion(name,args):
    with pytest.raises(BoardError):validate_messages_append_arguments(name,args)


def test_cold_replay_each_phase_without_repeating_audit_or_membership(ready):
    e,p,v=ready;r=create(e,v);r,pa=prepare(e,r);r,pr=later(e,r,'precommit');r,co=later(e,r,'commit');r,ca=later(e,r,'complete')
    b=Board(e[0].data_dir);before=snapshot(b)
    assert b.prepare_server_messages_append(e[3],**pa)['phase']=='prepared'
    assert b.precommit_server_messages_append(e[3],**pr)['phase']=='precommit'
    assert b.commit_server_messages_append(e[3],**co)['phase']=='committed'
    assert b.complete_server_messages_append(e[3],**ca)==r['proof']
    assert snapshot(b)==before


def test_race_start_vs_request_is_fenced_in_both_orders(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e
    i=b.get_server_host(h,host['id'])['instances'][0];barrier=threading.Barrier(2)
    def call(which):
        barrier.wait()
        try:return create(e,v) if which=='request' else command(e,'start',instance=i)
        except BoardError as ex:return ex
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(call,['request','start']))
    assert sum(isinstance(x,BoardError) for x in results)==1
    rows=b.list_server_messages_appends(h,host['id'])['requests']
    current=b.get_server_host(h,host['id'])['instances'][0]
    assert (len(rows)==1 and current['desired_state']=='stopped') or (not rows and current['desired_state']=='running')


@pytest.mark.parametrize('phase',['precommit','commit','complete'])
def test_later_phase_replay_requires_fresh_grant_even_after_success(ready,phase):
    e,p,v=ready;r=create(e,v);r,_=prepare(e,r)
    if phase in ('commit','complete'):r,_=later(e,r,'precommit')
    if phase=='complete':r,_=later(e,r,'commit')
    r,args=later(e,r,phase)
    grant(e,enabled=False,key='revoked-after-'+phase);before=snapshot(e[0])
    with pytest.raises(BoardError):getattr(e[0],phase+'_server_messages_append')(e[3],**args)
    assert snapshot(e[0])==before


@pytest.mark.parametrize('value',[[],{},None,True,1])
def test_malformed_proof_phase_is_rejected_without_python_type_error(ready,value):
    e,p,v=ready;r=create(e,v);r,_=prepare(e,r);proof={**r['proof'],'phase':value};before=snapshot(e[0])
    with pytest.raises(BoardError):later(e,r,'precommit',proof=proof)
    assert snapshot(e[0])==before


def test_list_cursor_is_owner_scoped_and_metadata_bound(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e
    r=create(e,v);cancel(e,r)
    second=create(e,{**v,'idempotency_key':'second'})
    page=b.list_server_messages_appends(h,host['id'],limit=1)
    assert page['requests']==[second] and page['next_before']==second['request_id']
    page2=b.list_server_messages_appends(h,host['id'],before=page['next_before'],limit=1)
    assert page2['requests'][0]['request_id']==r['request_id'] and page2['next_before'] is None
    with pytest.raises(BoardError):b.list_server_messages_appends({'kind':'human','owner_id':'two'},host['id'],before=page['next_before'])


def test_postcommit_does_not_turn_receipt_into_fresh_catalogue_authority(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v);r,_=prepare(e,r);r,_=later(e,r,'precommit');r,args=later(e,r,'commit')
    # A real Stop remains legal, but changes the append's pinned fence.
    command(e,'stop','human-stop-new',instance=b.get_server_host(h,host['id'])['instances'][0])
    assert b.commit_server_messages_append(raw,**args)==r['proof']
    before=snapshot(b)
    with pytest.raises(BoardError,match='catalog_changed'):later(e,r,'complete')
    assert snapshot(b)==before


def test_prepare_replay_is_exact_same_key_and_body_not_adoption(ready):
    e,p,v=ready;b=e[0];r=create(e,v);prepared,args=prepare(e,r)
    before=snapshot(b)
    assert b.prepare_server_messages_append(e[3],**args)==prepared['proof']
    with pytest.raises(BoardError,match='changed'):
        b.prepare_server_messages_append(e[3],**{**args,'idempotency_key':'another-key'})
    with pytest.raises(BoardError,match='idempotency_conflict'):
        b.prepare_server_messages_append(e[3],**{**args,'source_release_sha256':'f'*64})
    assert snapshot(b)==before


def test_historical_start_and_positive_report_do_not_bypass_new_B_barrier(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e
    c=command(e,'start','old-start');ack(e,c)
    i=b.get_server_host(h,host['id'])['instances'][0]
    report_args=dict(host_id=host['id'],instance_id=i['id'],expected_observation_revision=i['observation_revision'],
        generation=i['generation'],launchspec_sha256=i['launchspec_sha256'],command_id=i['command_id'],
        state='ready',cleanup_confirmed=False,reason='',idempotency_key='old-positive')
    b.report_instance_state(raw,**report_args);stop(e,'new-stop')
    v=body(e,p);r=create(e,v);i=b.get_server_host(h,host['id'])['instances'][0];before=snapshot(b)
    with pytest.raises(BoardError,match='messages_append_pending'):b.report_instance_state(raw,**report_args)
    with pytest.raises(BoardError,match='messages_append_pending'):
        b.command_server_instance(h,i['id'],'start',c['instance_version']-1,c['generation']-1,c['launchspec_sha256'],1,'old-start')
    assert snapshot(b)==before


def test_receipt_pin_corruption_is_not_an_adopted_historical_proof(ready):
    e,p,v=ready;b=e[0];r=create(e,v);r,args=prepare(e,r);r,_=later(e,r,'precommit')
    with b._tx() as db:
        old=json.loads(db.execute("SELECT response FROM server_messages_append_receipts WHERE operation='prepared'").fetchone()[0])
        old['children_roster_sha256']='f'*64
        from server_instances import _json
        db.execute("UPDATE server_messages_append_receipts SET response=? WHERE operation='prepared'",(_json(old),))
    before=snapshot(b)
    with pytest.raises(BoardError,match='corrupt'):b.prepare_server_messages_append(e[3],**args)
    assert snapshot(b)==before


def test_slot_list_has_a_closed_sixteen_slot_boundary(ready):
    e,p,v=ready;b,h,_,raw,_,host,*_=e
    for n in range(15):b.register_agent(raw,'extra-'+str(n),'Slot',agent_kind='codex',identity_key='append-slot')
    assert len(b.get_server_messages_append_context(h,host['id'],p['id'])['slots'])==16
    b.register_agent(raw,'one-too-many','Slot',agent_kind='codex',identity_key='append-slot')
    before=snapshot(b)
    with pytest.raises(BoardError,match='slot_limit'):b.get_server_messages_append_context(h,host['id'],p['id'])
    with pytest.raises(BoardError,match='slot_limit'):create(e,v)
    assert snapshot(b)==before


def test_request_history_limit_preserves_all_terminal_records(ready):
    e,p,v=ready;b=e[0]
    for n in range(32):
        row=create(e,{**v,'idempotency_key':'request-'+str(n)})
        cancel(e,row,key='cancel-'+str(n))
    before=snapshot(b)
    with pytest.raises(BoardError,match='limit'):create(e,{**v,'idempotency_key':'overflow'})
    assert snapshot(b)==before
    rows=b.list_server_messages_appends(e[1],e[5]['id'],limit=16)
    assert len(rows['requests'])==16 and rows['next_before'] is not None


def test_grant_final_publication_also_refuses_an_unexpected_write(ready,monkeypatch):
    e,p,v=ready;b=e[0];original=b._audit
    def injected(db,actor,event,detail):
        result=original(db,actor,event,detail)
        if event=='server_messages_append_grant':db.execute("UPDATE owners SET display_name='changed' WHERE id='two'")
        return result
    monkeypatch.setattr(b,'_audit',injected);before=snapshot(b)
    with pytest.raises(BoardError,match='postcondition'):grant(e,enabled=False,key='revoke-with-fault')
    assert snapshot(b)==before


def test_real_Board_to_ServerControlClient_composition_returns_proof23(ready):
    from blackoutburst_agents.server_control_client import ServerControlClient
    from blackoutburst_agents.message_append_protocol import PROOF_FIELDS
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v)
    client=object.__new__(ServerControlClient)
    calls=[]
    client._initialize=lambda:None
    def dispatch(name,args):
        calls.append(name)
        return getattr(b,name)(raw,**args)
    client._tool=dispatch
    polled=client.call('poll_server_messages_appends',dict(host_id=host['id'],limit=1))
    assert polled['requests'][0]['request']==r
    args=dict(host_id=host['id'],request_id=r['request_id'],expected_revision=1,operation_id=str(uuid.uuid4()),
        **{k:format(n+1,'x')*64 for n,k in enumerate(HASH_KEYS)},idempotency_key='client-prepare')
    proof=client.call('prepare_server_messages_append',args)
    assert set(proof)==set(PROOF_FIELDS.split()) and proof['phase']=='prepared'
    for name in ('precommit','commit','complete'):
        args=dict(host_id=host['id'],request_id=r['request_id'],expected_revision=proof['request_revision'],proof=proof,idempotency_key='client-'+name)
        if name=='commit':args['entry']=dict(catalog_key='target-b',profile_id=p['id'],profile_version=p['version'],mode='messages',launchspec_sha256='b'*64)
        proof=client.call(name+'_server_messages_append',args)
        assert set(proof)==set(PROOF_FIELDS.split())
    view=b.get_server_messages_append(h,host['id'],r['request_id'])
    assert view['proof']==proof and view['state']=='complete' and view['barrier_held'] is False
    assert calls==['poll_server_messages_appends','prepare_server_messages_append','precommit_server_messages_append','commit_server_messages_append','complete_server_messages_append']


@pytest.mark.parametrize('which',['configuration','catalog'])
def test_noop_pins_refused_by_Board_and_SDK_before_preparation(ready,which):
    from blackoutburst_agents.message_append_protocol import arguments
    from blackoutburst_agents.host_config import HostError
    e,p,v=ready;r=create(e,v);args=dict(host_id=e[5]['id'],request_id=r['request_id'],expected_revision=1,operation_id=str(uuid.uuid4()),
        **{k:format(n+1,'x')*64 for n,k in enumerate(HASH_KEYS)},idempotency_key='noop')
    args['new_'+which+'_sha256']=args['old_'+which+'_sha256']
    before=snapshot(e[0])
    with pytest.raises(HostError):arguments('prepare_server_messages_append',args)
    with pytest.raises(BoardError):e[0].prepare_server_messages_append(e[3],**args)
    assert snapshot(e[0])==before


def test_non_codex_binding_is_not_offered_as_a_supported_slot(ready):
    e,p,v=ready;b,h,_,_,_,host,*_=e
    with b._tx() as db:
        db.execute("UPDATE agent_profile_bindings SET agent_kind='other' WHERE profile_id=?",(p['id'],))
        db.execute("UPDATE sessions SET agent_kind='other' WHERE agent_id=?",(p['id'],))
    assert b.get_server_messages_append_context(h,host['id'],p['id'])['slots']==[]
    before=snapshot(b)
    with pytest.raises(BoardError,match='slot_changed'):create(e,v)
    assert snapshot(b)==before


@pytest.mark.parametrize('corruption',['request-sha','schema'])
def test_invalid_B_denies_launch_but_preserves_independent_Stop_channel(ready,corruption):
    e,p,v=ready;b,h,_,raw,_,host,*_=e;r=create(e,v)
    with b._tx() as db:
        if corruption=='request-sha':db.execute("UPDATE server_messages_append_requests SET request_sha256=? WHERE id=?",('f'*64,r['request_id']))
        else:db.execute('CREATE INDEX unexpected_b_index ON server_messages_append_requests(state)')
    with b._connection() as db:
        corrupt=[tuple(x) for x in db.execute('SELECT * FROM server_messages_append_requests')]
    instance=b.poll_instance_commands(raw,host['id'])['instances'][0]
    assert instance['can_start'] is False and instance['control_reason']=='authorization_changed'
    c=command(e,'stop','stop-invalid-b',instance=instance)
    queued=b.poll_instance_commands(raw,host['id'])
    assert queued['instances'][0]['current_command']['id']==c['id']
    assert queued['commands'][0]['id']==c['id']
    accepted=ack(e,c)
    assert accepted['command']['status']=='accepted'
    cleaned=report(e,instance=accepted['instance'],key='cleanup-invalid-b')
    assert cleaned['instance']['observed_state']=='stopped' and cleaned['instance']['cleanup_confirmed'] is True
    assert cleaned['command']['status']=='complete'
    before=snapshot(b)
    with pytest.raises(BoardError):command(e,'start','no-start-invalid-b',instance=cleaned['instance'])
    with pytest.raises(BoardError):command(e,'restart','no-restart-invalid-b',instance=cleaned['instance'])
    with pytest.raises(BoardError):report(e,'ready',False,instance=cleaned['instance'],key='no-positive-invalid-b')
    with pytest.raises(BoardError):b.get_server_messages_append(h,host['id'],r['request_id'])
    with pytest.raises(BoardError):b.poll_server_messages_appends(raw,host['id'])
    assert snapshot(b)==before
    with b._connection() as db:assert [tuple(x) for x in db.execute('SELECT * FROM server_messages_append_requests')]==corrupt
    assert b.get_runtime(h)['active_count']==0
