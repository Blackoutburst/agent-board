"""Actual temporary SQLite/MCP, source-only replacement; no worker or network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from board import BoardError
from server_instances import catalog_fence
from test_server_instances import env,report,command,ack
from test_server_instances_api import browser,rpc,setup,observation


METHOD='replace_server_catalog'


def poll(e):return e[0].poll_instance_commands(e[3],e[5]['id'])


def stop(e,key='human-stop'):
    cmd=command(e,'stop',key);ack(e,cmd);return report(e)['instance']


def arguments(e,*,settle=True):
    if settle:stop(e)
    snapshot=poll(e)
    return dict(host_id=e[5]['id'],expected_catalog_version=snapshot['catalog_version'],
        expected_host_version=snapshot['host_version'],expected_instances_sha256=catalog_fence(snapshot['instances']),
        entries=[{**e[6],'launchspec_sha256':'b'*64}],idempotency_key='source-replacement')


def replace(e,values):return e[0].replace_server_catalog(e[3],**values)


def snapshot(b):
    with b._connection() as db:
        return tuple(tuple(tuple(r) for r in db.execute('SELECT * FROM '+name+' ORDER BY rowid'))
            for name in ('server_hosts','server_instances','server_instance_commands','server_control_receipts','audit','runtime_executions'))


def test_real_replacement_keeps_identity_generation_history_and_no_runtime_or_activation(env):
    values=arguments(env);before=poll(env)['instances'][0]
    with env[0]._connection() as db:commands=[tuple(r) for r in db.execute('SELECT * FROM server_instance_commands')]
    result=replace(env,values);after=result['instances'][0]
    for key in ('id','host_id','catalog_key','profile_id','profile_version','mode','generation','desired_state','command_id'):
        assert after[key]==before[key]
    assert after['launchspec_sha256']=='b'*64 and result['catalog_version']==2
    assert after['observed_state']=='unknown' and after['cleanup_confirmed'] is False
    assert after['version']==before['version']+1 and after['observation_revision']==before['observation_revision']+1
    with env[0]._connection() as db:
        assert [tuple(r) for r in db.execute('SELECT * FROM server_instance_commands')]==commands
        assert not db.execute('SELECT 1 FROM runtime_executions').fetchone()
    saved=snapshot(env[0]);assert replace(env,values)==result and snapshot(env[0])==saved


def test_fence_parity_ignores_presence_but_catches_completed_start_stop_cycle(env):
    from blackoutburst_agents.server_control_protocol import catalog_fence as client_fence
    values=arguments(env);initial=poll(env)['instances']
    assert client_fence(initial)==values['expected_instances_sha256']
    changed=deepcopy(initial);changed[0]['last_seen_at']=0;changed[0]['reason']='stopped'
    changed[0]['control_reason']='quota_unavailable';changed[0]['can_start']=False
    assert client_fence(changed)==values['expected_instances_sha256']
    started=command(env);ack(env,started);report(env,'ready',False);stop(env,'second-stop')
    assert poll(env)['instances'][0]['desired_state']=='stopped'
    assert client_fence(poll(env)['instances'])!=values['expected_instances_sha256']
    before=snapshot(env[0])
    with pytest.raises(BoardError,match='catalog_changed'):replace(env,values)
    assert snapshot(env[0])==before


@pytest.mark.parametrize('kind',['never-started','rejected-start','terminal-start'])
def test_clean_observed_stop_is_not_a_real_complete_human_stop(env,kind):
    report(env)
    if kind!='never-started':
        started=command(env);ack(env,started)
        if kind=='terminal-start':report(env,'ready',False)
        report(env)
    values=arguments(env,settle=False);before=snapshot(env[0])
    with pytest.raises(BoardError,match='human_stop_required'):replace(env,values)
    assert snapshot(env[0])==before


def test_queued_or_accepted_stop_must_first_be_observed_complete(env):
    cmd=command(env,'stop','stop');values=arguments(env,settle=False)
    with pytest.raises(BoardError,match='human_stop_required'):replace(env,values)
    ack(env,cmd)
    with pytest.raises(BoardError,match='human_stop_required'):replace(env,values)
    report(env);values=arguments(env,settle=False)
    assert replace(env,values)['catalog_version']==2


@pytest.mark.parametrize('action',['start','restart'])
def test_concurrent_human_start_and_replace_have_only_one_winner(env,action):
    values=arguments(env);i=poll(env)['instances'][0];barrier=threading.Barrier(2)
    def call(replacement):
        barrier.wait()
        try:
            if replacement:return replace(env,values)
            return env[0].command_server_instance(env[1],i['id'],action,i['version'],i['generation'],
                i['launchspec_sha256'],1,'human-'+action)
        except BoardError:return 'rejected'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(call,[True,False]))
    assert sum(isinstance(v,dict) for v in results)==1 and results.count('rejected')==1


def test_historical_replacement_response_cannot_replace_a_fresh_observation(env):
    values=arguments(env);result=replace(env,values)
    report(env);command(env,'start','later-human-start')
    current=poll(env);saved=snapshot(env[0])
    assert replace(env,values)==result and snapshot(env[0])==saved
    assert result['instances'][0]['desired_state']=='stopped'
    assert current['instances'][0]['desired_state']=='running'
    with pytest.raises(BoardError,match='catalog_changed'):
        replace(env,{**values,'idempotency_key':'fresh-key'})


@pytest.mark.parametrize('change',[{'expected_host_version':2},{'expected_catalog_version':2},
    {'expected_instances_sha256':'c'*64}])
def test_every_first_cas_is_exact_and_rekeying_cannot_reset_it(env,change):
    values=arguments(env);before=snapshot(env[0])
    with pytest.raises(BoardError,match='catalog_changed'):replace(env,{**values,**change})
    assert snapshot(env[0])==before
    replace(env,values);before=snapshot(env[0])
    with pytest.raises(BoardError,match='idempotency_conflict'):replace(env,{**values,**change})
    assert snapshot(env[0])==before


def test_same_owner_other_device_and_foreign_owner_have_no_manager_role(env):
    values=arguments(env);b,human=env[:2]
    other=b.create_device(human,'Unbound fixture',[])
    b.ensure_owner('foreign','Foreign');foreign=b.create_device({'kind':'human','owner_id':'foreign'},'Foreign',[])
    before=snapshot(b)
    for device in (other,foreign):
        with pytest.raises(BoardError):b.replace_server_catalog(b.authenticate(device['token']),**values)
    assert snapshot(b)==before


@pytest.mark.parametrize('change',['policy','profile','device','host','paused'])
def test_current_authority_is_required_even_before_historical_receipt_lookup(env,change):
    values=arguments(env);replace(env,values);b,human,device,_,profile,host,*_=env
    if change=='policy':b.set_policy(human,False,False,False,False,[],[])
    elif change=='profile':b.update_agent_profile(human,profile['id'],profile['version'],status='paused')
    elif change=='device':b.revoke_device(human,device['id'])
    elif change=='host':b.revoke_server_host(human,host['id'],host['version'],'revoke')
    else:
        limits=b.get_runtime(human)['limits'];b.update_runtime_limits(human,limits['version'],limits['max_concurrent'],True)
    before=snapshot(b)
    with pytest.raises(BoardError):replace(env,values)
    assert snapshot(b)==before


@pytest.mark.parametrize('change',[{'catalog_key':'other'},{'profile_id':'other'},{'profile_version':999},
    {'mode':'ticket_text'},{'launchspec_sha256':'a'*64}])
def test_replacement_never_adds_removes_rebinds_or_reuses_source(env,change):
    values=arguments(env);values['entries']=[{**values['entries'][0],**change}];before=snapshot(env[0])
    with pytest.raises(BoardError):replace(env,values)
    assert snapshot(env[0])==before


@pytest.mark.parametrize('where,mutation',[
    ('audit',"UPDATE server_instances SET generation=generation+1;"),
    ('receipt',"UPDATE server_instances SET launchspec_sha256='"+'c'*64+"';"),
    ('receipt','DELETE FROM server_control_receipts WHERE operation LIKE \'catalog-replace:%\';'),
    ('audit',"UPDATE server_instance_commands SET action='start',budget=1,status='queued';"),
])
def test_final_postcondition_rolls_back_corruption_after_audit_or_receipt(env,where,mutation):
    values=arguments(env);b=env[0]
    event=("AFTER INSERT ON audit WHEN NEW.event='server_catalog_replaced'" if where=='audit'
        else "AFTER INSERT ON server_control_receipts WHEN NEW.operation LIKE 'catalog-replace:%'")
    with b._tx() as db:db.execute('CREATE TRIGGER synthetic_catalog_corruption '+event+' BEGIN '+mutation+' END')
    before=snapshot(b)
    with pytest.raises(BoardError):replace(env,values)
    assert snapshot(b)==before


@pytest.mark.parametrize('malformation',['unknown','stop-source'])
def test_corrupted_cached_receipt_cannot_expose_unknown_fields(env,malformation):
    values=arguments(env);result=replace(env,values)
    if malformation=='unknown':result={'secret_unknown':'DO_NOT_RETURN'}
    else:result['instances'][0]['current_command']['launchspec_sha256']='b'*64
    with env[0]._tx() as db:
        db.execute("UPDATE server_control_receipts SET response=? WHERE operation LIKE 'catalog-replace:%'",
            (json.dumps(result,sort_keys=True,separators=(',',':')),))
    with pytest.raises(BoardError,match='invalid_server_receipt'):replace(env,values)


def test_real_mcp_and_client_closed_roundtrip_without_registration(browser):
    from blackoutburst_agents.server_control_client import ServerControlClient
    from blackoutburst_agents.server_control_protocol import catalog_fence as client_fence
    host,i=setup(browser);b,human,device=browser[1:4];raw=b.authenticate(device['token'])
    cmd=b.command_server_instance(human,i['id'],'stop',i['version'],i['generation'],i['launchspec_sha256'],0,'stop')
    b.ack_instance_command(raw,host['id'],cmd['id'],cmd['revision'],'ack')
    i=rpc(browser,'report_instance_state',observation(host,b.poll_instance_commands(raw,host['id'])['instances'][0]))['instance']
    view=b.poll_instance_commands(raw,host['id'])
    values=dict(host_id=host['id'],expected_catalog_version=1,expected_host_version=view['host_version'],
        expected_instances_sha256=client_fence(view['instances']),entries=[{k:i[k] for k in
            ('catalog_key','profile_id','profile_version','mode')}|{'launchspec_sha256':'b'*64}],idempotency_key='replace')
    client=ServerControlClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
        'client':'codex','identity_key':'fixture','name':'Fixture'},'unused-control-session',3)
    client._initialized=True
    def memory_rpc(method,params):
        response=browser[0].post('/mcp',headers={'Authorization':'Bearer '+device['token'],
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':method,'params':params})
        assert response.status_code==200
        return response.json()['result']
    client._rpc=memory_rpc
    result=client.call(METHOD,values)
    assert result['catalog_version']==2 and result['instances'][0]['id']==i['id']
    assert client.call(METHOD,values)==result


@pytest.mark.parametrize('change',[{'unexpected':'DO_NOT_RETURN'},{'expected_host_version':True},
    {'expected_catalog_version':0},{'expected_instances_sha256':'A'*64},{'entries':[]},
    {'entries':'[]'}])
def test_raw_mcp_unknown_or_coerced_fields_do_not_replace(browser,change):
    host,i=setup(browser)
    values=dict(host_id=host['id'],expected_catalog_version=1,expected_host_version=host['version'],
        expected_instances_sha256='a'*64,entries=[{k:i[k] for k in
            ('catalog_key','profile_id','profile_version','mode','launchspec_sha256')}],idempotency_key='replace')
    before=snapshot(browser[1]);result=rpc(browser,METHOD,{**values,**change},error=True)
    assert 'DO_NOT_RETURN' not in json.dumps(result) and snapshot(browser[1])==before


@pytest.mark.parametrize('mutation',['extra','version','profile','cleanup','action','old-launch','command-extra'])
def test_client_rejects_malformed_receipt_without_retry_or_register(env,mutation):
    from blackoutburst_agents.server_control_client import ServerControlClient
    from blackoutburst_agents.board_reader import BoardReadError
    values=arguments(env);result=deepcopy(replace(env,values));i=result['instances'][0]
    if mutation=='extra':result['private_unknown']='DO_NOT_RETURN'
    elif mutation=='version':result['catalog_version']=1
    elif mutation=='profile':i['profile_id']='other'
    elif mutation=='cleanup':i['cleanup_confirmed']=True
    elif mutation=='action':i['current_command'].update(action='start',budget=1)
    elif mutation=='old-launch':i['current_command']['launchspec_sha256']=i['launchspec_sha256']
    else:i['current_command']['private_unknown']='DO_NOT_RETURN'
    client=ServerControlClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
        'client':'codex','identity_key':'fixture','name':'Fixture'},'unused',3)
    calls=[];client._initialize=lambda:calls.append('initialize')
    def tool(name,args):calls.append(name);return result
    client._tool=tool
    with pytest.raises(BoardReadError,match='managed_protocol_invalid'):client.call(METHOD,values)
    assert calls==['initialize',METHOD]
    with pytest.raises(BoardReadError,match='managed_registration_forbidden'):client.register()


def test_client_loss_does_not_retry_and_bad_first_cas_does_not_initialize(env):
    from blackoutburst_agents.server_control_client import ServerControlClient
    from blackoutburst_agents.board_reader import BoardReadError
    values=arguments(env)
    client=ServerControlClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
        'client':'codex','identity_key':'fixture','name':'Fixture'},'unused',3)
    calls=[];client._initialize=lambda:calls.append('initialize')
    def lost(name,args):calls.append(name);raise BoardReadError('timeout')
    client._tool=lost
    with pytest.raises(BoardReadError,match='managed_protocol_invalid'):
        client.call(METHOD,{**values,'expected_host_version':True})
    assert calls==[]
    with pytest.raises(BoardReadError,match='timeout'):client.call(METHOD,values)
    assert calls==['initialize',METHOD]


def test_projection_is_sorted_and_server_client_share_the_exact_closed_fields(env):
    from blackoutburst_agents.server_control_protocol import catalog_fence as client_fence
    arguments(env);one=poll(env)['instances'][0];two=deepcopy(one)
    two.update(id='second-id',catalog_key='second-key',profile_id='second-profile',command_id='second-stop')
    two['current_command'].update(id='second-stop',instance_id='second-id')
    assert catalog_fence([one,two])==catalog_fence([two,one])==client_fence([two,one])
    with pytest.raises(BoardError):catalog_fence([one,one])


def test_cached_response_size_is_checked_before_loading_its_body(env):
    from contextlib import contextmanager
    from unittest.mock import patch
    values=arguments(env);replace(env,values);b=env[0]
    with b._tx() as db:
        db.execute("UPDATE server_control_receipts SET response=? WHERE operation LIKE 'catalog-replace:%'",('x'*65537,))
    original=b._connection;queries=[]
    @contextmanager
    def observed():
        with original() as db:
            db.set_trace_callback(queries.append)
            yield db
    with patch.object(b,'_connection',observed):
        with pytest.raises(BoardError,match='invalid_server_receipt'):replace(env,values)
    assert any('length(CAST(response AS BLOB))' in q for q in queries)
    assert not any(q.lstrip().upper().startswith('SELECT RESPONSE FROM SERVER_CONTROL_RECEIPTS') for q in queries)


def test_raw_nested_unknown_and_entry_or_wire_size_limits_fail_without_mutation(browser):
    host,i=setup(browser);entry={k:i[k] for k in ('catalog_key','profile_id','profile_version','mode','launchspec_sha256')}
    base=dict(host_id=host['id'],expected_catalog_version=1,expected_host_version=host['version'],
        expected_instances_sha256='a'*64,entries=[entry],idempotency_key='replace')
    invalid=[{**base,'entries':[{**entry,'private_unknown':'DO_NOT_RETURN'}]},
        {**base,'entries':[entry]*17},
        {**base,'entries':[{**entry,'catalog_key':str(n)+'é'*199,'profile_id':str(n)+'é'*199} for n in range(16)]}]
    before=snapshot(browser[1])
    for values in invalid:
        result=rpc(browser,METHOD,values,error=True)
        assert 'DO_NOT_RETURN' not in json.dumps(result) and snapshot(browser[1])==before
