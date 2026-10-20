"""Real SQLite boundaries for inert human requests; no manager/provider effect."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import sqlite3
import threading

import pytest

from board import Board, BoardError
from server_provisioning import GRANT_SCHEMA, REQUEST_SCHEMA, CANCEL_SCHEMA, SQL
from test_server_instances import env, report, command, ack


def stop(e, key='stop'):
    c = command(e, 'stop', key); ack(e, c)
    return report(e)['instance']


def profile(e, model=''):
    b, human, device = e[:3]
    p = b.create_agent_profile(human, 'New desired profile', preferred_provider='codex_chatgpt', preferred_model=model)
    return b.bind_agent_profile(human, p['id'], p['version'], device['id'], 'new-' + p['id'], 'codex')


def grant(e, enabled=True, key='provision-grant'):
    b, human, _, _, _, host, *_ = e
    g = b.get_server_provisioning_grant(human, host['id'])
    h = b.get_server_host(human, host['id'])
    values = dict(schema=GRANT_SCHEMA, expected_host_version=h['version'], expected_grant_version=g['version'],
        enabled=enabled, idempotency_key=key)
    return b.set_server_provisioning_grant(human, host['id'], **values), values


def body(e, p, key='provision-request'):
    b, human, _, _, _, host, *_ = e
    context = b.get_server_provisioning_context(human, host['id'], p['id'])
    fields = ('expected_host_version', 'expected_catalog_version', 'expected_instances_sha256', 'grant_version',
        'profile_id', 'profile_version', 'profile_sha256', 'provider_id', 'requested_model')
    return dict(schema=REQUEST_SCHEMA, **{k: context[k] for k in fields}, idempotency_key=key)


def request(e, values):
    return e[0].create_server_provisioning_request(e[1], e[5]['id'], **values)


def cancel(e, result, key='provision-cancel'):
    return e[0].cancel_server_provisioning_request(e[1], e[5]['id'], result['request_id'],
        schema=CANCEL_SCHEMA, expected_revision=result['revision'], idempotency_key=key)


def snapshot(b, *, exclude=()):
    with b._connection() as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: [tuple(r) for r in db.execute('SELECT * FROM ' + n + ' ORDER BY rowid')]
            for n in names if n not in exclude}


@pytest.fixture
def ready(env):
    stop(env); p = profile(env); grant(env)
    return env, p, body(env, p)


def test_inert_request_and_cancel_preserve_every_prior_row_and_legacy_wire(ready):
    from blackoutburst_agents.server_control_protocol import response
    e, p, values = ready; b, human, _, raw, _, host, *_ = e
    allowed = tuple(SQL)[:4] + ('audit',)
    before = snapshot(b, exclude=allowed)
    result = request(e, values)
    assert result['state'] == 'requested' and result['barrier_held'] and result['reserved_slots'] == 1
    assert result['requested_model'] is None and result['apply_implemented'] is False
    assert {result[k] for k in ('connection_status', 'model_catalog_status', 'model_access_status')} == {'not_attested'}
    assert snapshot(b, exclude=allowed) == before
    saved = snapshot(b); assert request(e, values) == result and snapshot(b) == saved
    poll = b.poll_instance_commands(raw, host['id']); response('poll_instance_commands', poll)
    assert not poll['instances'][0]['can_start'] and poll['instances'][0]['control_reason'] == 'authorization_changed'
    final = cancel(e, result)
    assert final['state'] == 'cancelled' and not final['barrier_held'] and not final['can_cancel']
    saved = snapshot(b); assert cancel(e, result) == final and snapshot(b) == saved
    assert b.get_server_provisioning_request(human, host['id'], result['request_id']) == final
    assert b.get_runtime(human)['active_count'] == 0


@pytest.mark.parametrize('who', ['agent', 'other-owner', 'other-device'])
def test_no_implicit_grant_or_human_authority(ready, who):
    e, _, values = ready; b, human, _, raw, _, host, *_ = e
    actor = raw if who == 'agent' else {'kind': 'human', 'owner_id': 'two'}
    if who == 'other-device':
        actor = b.authenticate(b.create_device(human, 'Other', [])['token'])
    saved = snapshot(b)
    with pytest.raises(BoardError): b.create_server_provisioning_request(actor, host['id'], **values)
    with pytest.raises(BoardError): b.get_server_provisioning_context(actor, host['id'], values['profile_id'])
    assert snapshot(b) == saved


def test_grant_is_separate_from_existing_manager_and_cannot_self_promote(env):
    stop(env); p = profile(env); values = body(env, p)
    assert values['grant_version'] == 0
    values['grant_version'] = 1
    saved = snapshot(env[0])
    with pytest.raises(BoardError, match='not_authorized'): request(env, values)
    assert snapshot(env[0]) == saved


@pytest.mark.parametrize('model', ['', 'exact requested model'])
def test_context_pins_real_profile_snapshot_and_null_mapping(env, model):
    p = profile(env, model); before = snapshot(env[0])
    c = env[0].get_server_provisioning_context(env[1], env[5]['id'], p['id'])
    from server_provisioning import _hash
    assert c['profile_sha256'] == _hash(env[0].get_agent_profile_version(env[1], p['id'], p['version']))
    assert c['requested_model'] == (model or None)
    assert snapshot(env[0]) == before


@pytest.mark.parametrize('change', [
    {'expected_host_version': True}, {'expected_catalog_version': 1.0}, {'grant_version': '1'},
    {'requested_model': ''}, {'requested_model': 'x'*201}, {'profile_sha256': 'B'*64},
    {'provider_id': '\ud800'}, {'command': 'PRIVATE_SENTINEL'}, {'schema': 'apply-v1'},
])
def test_closed_input_is_refused_before_any_mutation(ready, change):
    e, _, values = ready; saved = snapshot(e[0])
    with pytest.raises(BoardError): request(e, {**values, **change})
    assert snapshot(e[0]) == saved


@pytest.mark.parametrize('change', ['profile', 'binding', 'grant', 'device', 'host'])
def test_fresh_authority_precedes_historical_request_replay_and_cancel_still_works(ready, change):
    e, p, values = ready; b, human, device, *_ = e
    result = request(e, values)
    if change == 'profile': b.update_agent_profile(human, p['id'], p['version'], status='paused')
    elif change == 'binding':
        with b._tx() as db: db.execute('DELETE FROM agent_profile_bindings WHERE profile_id=?', (p['id'],))
    elif change == 'grant': grant(e, False, 'revoke-grant')
    elif change == 'device': b.revoke_device(human, device['id'])
    else: b.revoke_server_host(human, e[5]['id'], b.get_server_host(human,e[5]['id'])['version'], 'revoke-host')
    saved = snapshot(b)
    with pytest.raises(BoardError): request(e, values)
    assert snapshot(b) == saved
    assert cancel(e, result)['state'] == 'cancelled'


def test_first_body_is_not_changed_by_retry_or_rekey(ready):
    e, _, values = ready; result = request(e, values); saved = snapshot(e[0])
    for altered in ({'requested_model': 'changed'}, {'expected_host_version': values['expected_host_version']+1},
                    {'idempotency_key': 'other-key'}):
        with pytest.raises(BoardError): request(e, {**values, **altered})
        assert snapshot(e[0]) == saved
    assert result['request_sha256'] == request(e, values)['request_sha256']


@pytest.mark.parametrize('cas', ['expected_host_version', 'expected_catalog_version', 'expected_instances_sha256', 'profile_sha256'])
def test_all_public_cas_are_exact(ready, cas):
    e, _, values = ready; saved = snapshot(e[0])
    changed = 'b'*64 if cas.endswith('sha256') else values[cas]+1
    with pytest.raises(BoardError): request(e, {**values, cas: changed})
    assert snapshot(e[0]) == saved


def test_real_complete_human_stop_is_required_not_observed_cleanup(env):
    p = profile(env); grant(env); report(env)
    saved = snapshot(env[0])
    with pytest.raises(BoardError, match='human_stop_required'): request(env, body(env, p))
    assert snapshot(env[0]) == saved
    c = command(env, 'stop'); ack(env, c)
    with pytest.raises(BoardError, match='human_stop_required'): request(env, body(env, p))
    report(env)
    assert request(env, body(env, p))['state'] == 'requested'


def test_barrier_precedes_every_historical_command_report_and_catalog_receipt(ready):
    e, _, values = ready; b, human, _, raw, _, host, entry, *_ = e
    # Create old positive receipts, finish with a new genuine Stop, then admit.
    i = b.poll_instance_commands(raw,host['id'])['instances'][0]
    start_args = dict(action='start',expected_version=i['version'],expected_generation=i['generation'],
        launchspec_sha256=i['launchspec_sha256'],budget=1,idempotency_key='old-start')
    c = b.command_server_instance(human,i['id'],**start_args); ack(e,c)
    i = b.poll_instance_commands(raw,host['id'])['instances'][0]
    ready_args = dict(host_id=host['id'],instance_id=i['id'],expected_observation_revision=i['observation_revision'],
        generation=i['generation'],launchspec_sha256=i['launchspec_sha256'],command_id=i['command_id'],state='ready',
        cleanup_confirmed=False,reason='',idempotency_key='old-ready')
    b.report_instance_state(raw,**ready_args); stop(e,'stop-after-start')
    values = body(e, {'id': values['profile_id']}); result=request(e, values)
    current = b.get_server_host(human,host['id'])['instances'][0]
    saved=snapshot(b)
    attempts = [lambda: b.command_server_instance(human,i['id'],**start_args),
        lambda: b.report_instance_state(raw,**ready_args),
        lambda: b.publish_server_catalog(raw,host['id'],0,[entry],'publish'),
        lambda: b.publish_server_catalog(raw,host['id'],1,[],'empty'),
        lambda: b.create_server_host(human,e[2]['id'],'Server','grant'),
        lambda: ack(e,c), lambda: b.command_server_instance(human,current['id'],'restart',current['version'],
            current['generation'],current['launchspec_sha256'],1,'restart')]
    for attempt in attempts:
        with pytest.raises(BoardError): attempt()
        assert snapshot(b)==saved
    # Stop and independent cleanup must still work while the request is pending.
    stop(e,'safety-stop')
    assert b.get_server_provisioning_request(human,host['id'],result['request_id'])['state']=='requested'
    assert cancel(e,result)['state']=='cancelled'


@pytest.mark.parametrize('first', ['request', 'start'])
def test_deterministic_two_orders_of_start_request_race(ready, monkeypatch, first):
    e, _, values=ready; b=e[0]; entered=threading.Event(); release=threading.Event()
    original=b._provisioning_remember if first=='request' else b._server_remember
    def paused(*args,**kwargs):
        result=original(*args,**kwargs); entered.set(); assert release.wait(3); return result
    monkeypatch.setattr(b,'_provisioning_remember' if first=='request' else '_server_remember',paused)
    def call(which):
        try: return request(e,values) if which=='request' else command(e,'start','race-start')
        except BoardError: return 'refused'
    with ThreadPoolExecutor(2) as pool:
        winner=pool.submit(call,first); assert entered.wait(3)
        loser=pool.submit(call,'start' if first=='request' else 'request')
        release.set(); a=winner.result(); z=loser.result()
    assert isinstance(a,dict) and z=='refused'
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM server_provisioning_barriers').fetchone()[0]==int(first=='request')
        assert db.execute("SELECT COUNT(*) FROM server_instance_commands WHERE action='start'").fetchone()[0]==int(first=='start')


@pytest.mark.parametrize('target', ['barrier', 'request', 'grant', 'instance', 'audit', 'receipt'])
def test_postconditions_rollback_mutations_after_audit(ready, monkeypatch, target):
    e, _, values=ready; b=e[0]; saved=snapshot(b); original=b._audit
    def corrupted(db,actor,event,detail):
        result=original(db,actor,event,detail)
        if event=='server_provisioning_request':
            if target=='barrier': db.execute('DELETE FROM server_provisioning_barriers')
            elif target=='request': db.execute("UPDATE server_provisioning_requests SET request_sha256=?",('f'*64,))
            elif target=='grant': db.execute('UPDATE server_provisioning_grants SET enabled=0')
            elif target=='instance': db.execute('UPDATE server_instances SET generation=generation+1')
            elif target=='audit': db.execute('DELETE FROM audit WHERE id=?',(result,))
            else: db.execute('DELETE FROM server_provisioning_receipts WHERE operation=\'request\'')
        return result
    monkeypatch.setattr(b,'_audit',corrupted)
    with pytest.raises(BoardError):request(e,values)
    assert snapshot(b)==saved


@pytest.mark.parametrize('kind', ['main-trigger', 'temp-trigger', 'temp-view', 'temp-table'])
def test_sensitive_schema_cannot_carry_hidden_effect_or_shadow(ready, kind):
    e,_,values=ready; b=e[0]
    with b._connection() as db:
        if kind.endswith('trigger'):
            db.execute(('CREATE TEMP TRIGGER' if kind=='temp-trigger' else 'CREATE TRIGGER')+
                ' synthetic_provision AFTER INSERT ON server_provisioning_requests BEGIN DELETE FROM server_provisioning_barriers; END')
        else: db.execute('CREATE TEMP '+('VIEW' if kind=='temp-view' else 'TABLE')+
            ' server_provisioning_requests '+('AS SELECT 1' if kind=='temp-view' else '(id TEXT)'))
        with pytest.raises(BoardError,match='schema_invalid'): b._provisioning_schema(db)


def test_cancel_cas_and_pagination_keep_terminal_history(ready):
    e,p,values=ready; b=e[0]; ids=[]
    for number in range(3):
        r=request(e,{**values,'idempotency_key':str(number)}); ids.append(r['request_id']); cancel(e,r,key='cancel-'+str(number))
    page=b.list_server_provisioning_requests(e[1],e[5]['id'],limit=2)
    assert [v['request_id'] for v in page['requests']]==list(reversed(ids))[:2]
    page2=b.list_server_provisioning_requests(e[1],e[5]['id'],before=page['next_before'],limit=2)
    assert [v['request_id'] for v in page2['requests']]==ids[:1] and page2['next_before'] is None
    with pytest.raises(BoardError):b.cancel_server_provisioning_request(e[1],e[5]['id'],ids[-1],schema=CANCEL_SCHEMA,expected_revision=2,idempotency_key='new-key')


def downgrade_empty_provisioning(b):
    with b._tx() as db:
        for name in tuple(SQL)[:4]: assert db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]==0
        for name in ('server_provisioning_barriers','server_provisioning_receipts','server_provisioning_requests','server_provisioning_grants'):
            db.execute('DROP TABLE '+name)
        db.execute('PRAGMA user_version=15')


def test_schema16_migration_is_atomic_and_preserves_old_rows(env,monkeypatch):
    import board as module
    b=env[0]; downgrade_empty_provisioning(b); before=snapshot(b); original=module.migrate_server_provisioning
    def fail(db):
        db.execute(next(iter(SQL.values()))); raise RuntimeError('synthetic migration failure')
    monkeypatch.setattr(module,'migrate_server_provisioning',fail)
    with pytest.raises(RuntimeError,match='synthetic'):Board(b.data_dir)
    with b._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==15
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='server_provisioning_grants'").fetchone() is None
    assert snapshot(b)==before
    monkeypatch.setattr(module,'migrate_server_provisioning',original)
    reopened=Board(b.data_dir)
    assert snapshot(reopened,exclude=tuple(SQL)[:4])==before
    with reopened._connection() as db:assert db.execute('PRAGMA user_version').fetchone()[0]==16


@pytest.mark.parametrize('phase', ['request', 'cancel'])
def test_lost_commit_reply_keeps_exact_first_body_and_terminal(ready, monkeypatch, phase):
    from contextlib import contextmanager
    e,_,values=ready; b=e[0]; original=b._tx
    result=request(e,values) if phase=='cancel' else None
    @contextmanager
    def lost_reply():
        with original() as db:yield db
        raise TimeoutError('synthetic committed response lost')
    monkeypatch.setattr(b,'_tx',lost_reply)
    with pytest.raises(TimeoutError):
        if phase=='request':request(e,values)
        else:cancel(e,result)
    monkeypatch.setattr(b,'_tx',original)
    saved=snapshot(b)
    replay=request(e,values) if phase=='request' else cancel(e,result)
    assert replay['state']==('requested' if phase=='request' else 'cancelled')
    assert snapshot(b)==saved
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM server_provisioning_requests').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM server_provisioning_barriers').fetchone()[0]==int(phase=='request')


def test_history_capacity_never_discards_or_refunds_an_old_request(ready):
    e,_,values=ready; b=e[0]
    for number in range(32):
        r=request(e,{**values,'idempotency_key':'retained-'+str(number)})
        cancel(e,r,'retained-cancel-'+str(number))
    saved=snapshot(b)
    with pytest.raises(BoardError,match='limit'):request(e,{**values,'idempotency_key':'thirty-third'})
    assert snapshot(b)==saved
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM server_provisioning_requests').fetchone()[0]==32
        assert db.execute('SELECT COUNT(*) FROM server_provisioning_barriers').fetchone()[0]==0


@pytest.mark.parametrize('same_key',[False,True])
def test_two_cancel_calls_cannot_create_two_terminal_transitions(ready,monkeypatch,same_key):
    e,_,values=ready; b=e[0];r=request(e,values);entered=threading.Event();release=threading.Event()
    original=b._provisioning_remember
    def paused(*args,**kwargs):
        result=original(*args,**kwargs)
        if args[3]=='cancel':entered.set();assert release.wait(3)
        return result
    monkeypatch.setattr(b,'_provisioning_remember',paused)
    def call(key):
        try:return cancel(e,r,key)
        except BoardError:return 'refused'
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(call,'first');assert entered.wait(3)
        second=pool.submit(call,'first' if same_key else 'second');release.set()
        a,z=first.result(),second.result()
    assert a['state']=='cancelled' and (z==a if same_key else z=='refused')
    with b._connection() as db:
        assert db.execute('SELECT revision FROM server_provisioning_requests').fetchone()[0]==2
        assert db.execute("SELECT COUNT(*) FROM server_provisioning_receipts WHERE operation='cancel'").fetchone()[0]==1


@pytest.mark.parametrize('count',[15,16])
def test_active_catalog_bound_counts_reserved_place(env,count):
    b,human,device,raw,_,host,entry,*_=env
    entries=[entry]
    for number in range(count-1):
        p=b.create_agent_profile(human,'Old '+str(number),policy={'allow_read':True,'allow_messages':True,
            'allow_requests':False,'actions':[],'resources':[],'autonomy':'responses'})
        p=b.bind_agent_profile(human,p['id'],p['version'],device['id'],'old-'+str(number),'codex')
        entries.append(dict(catalog_key='old-'+str(number),profile_id=p['id'],profile_version=p['version'],launchspec_sha256='b'*64,mode='messages'))
    b.publish_server_catalog(raw,host['id'],1,entries,'many')
    for i in b.get_server_host(human,host['id'])['instances']:
        c=b.command_server_instance(human,i['id'],'stop',i['version'],i['generation'],i['launchspec_sha256'],0,'stop-'+i['id'])
        accepted=b.ack_instance_command(raw,host['id'],c['id'],c['revision'],'ack-'+i['id'])['instance']
        b.report_instance_state(raw,host['id'],i['id'],accepted['observation_revision'],i['generation'],i['launchspec_sha256'],c['id'],'stopped',True,'','report-'+i['id'])
    p=profile(env);grant(env);values=body(env,p)
    if count==16:
        saved=snapshot(b)
        with pytest.raises(BoardError,match='limit'):request(env,values)
        assert snapshot(b)==saved
    else:assert request(env,values)['reserved_slots']==1


# A real durable refusal must survive the additive migration, not only empty tables.
from test_runtime_api import browser as runtime_browser


def test_schema16_retains_actual_runtime_and_permanent_admission_refusal(runtime_browser):
    from test_runtime_admission_refusal import occupy, message_arguments, actor
    e=runtime_browser; b=e[1]; occupy(e)
    args={**message_arguments(e),'refusal_mode':'capacity-v1'}
    proof=b.reserve_message_execution(actor(e),**args)
    assert proof['reserved'] is False
    downgrade_empty_provisioning(b)
    before=snapshot(b)
    reopened=Board(b.data_dir)
    assert snapshot(reopened,exclude=tuple(SQL)[:4])==before
    assert reopened.reserve_message_execution(actor(e),**args)==proof
    with reopened._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==1


def test_long_metadata_pages_shrink_before_response_bound(ready):
    from server_provisioning import _json
    e,_,values=ready; b=e[0]
    for n in range(16):
        r=request(e,{**values,'idempotency_key':'large-page-'+str(n)})
        cancel(e,r,'large-cancel-'+str(n))
    page=b.list_server_provisioning_requests(e[1],e[5]['id'])
    assert len(_json(page).encode('ascii'))<=16384
    # The first page may contain fewer than the requested sixteen rows.
    seen=[v['request_id'] for v in page['requests']]
    while page['next_before'] is not None:
        page=b.list_server_provisioning_requests(e[1],e[5]['id'],before=page['next_before'])
        seen.extend(v['request_id'] for v in page['requests'])
    assert len(seen)==len(set(seen))==16
