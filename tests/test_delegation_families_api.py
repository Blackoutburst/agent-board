"""Actual MCP and owner REST family boundaries; no models or real MCP endpoint."""
import copy
import json
import time

import pytest

from delegation_families import _result_hash
from test_runtime_api import browser, rpc


ACTIONS=['repo.read_file','repo.patch_file','repo.run_test']
TOOLS={'open_delegation_family','get_delegation_family','lookup_delegation_family',
       'stop_delegation_family','accept_delegation_result','finalize_delegation_family'}


@pytest.fixture
def family_api(browser):
    client,board,human,token,project,ticket,profile=browser
    board.set_policy(human,True,True,True,True,ACTIONS,['fixture:repo'])
    raw=board.authenticate(token)
    for session in ('family-child-one','family-child-two'):
        board.register_agent(raw,session,'Family fixture',[],'codex',False,'fixture')
    current=board.get_ticket(human,ticket['id'])['ticket']
    current=board.update_ticket(human,ticket['id'],current['version'],resource='fixture:repo')
    children=[dict(session_id=session,role='worker',model='fixture-model',title='Child '+str(i),
        description='Bounded synthetic worker',acceptance_criteria='Explicit evidence',files=['part'+str(i)+'.txt'])
        for i,session in enumerate(('family-child-one','family-child-two'))]
    args=dict(parent_ticket_id=ticket['id'],expected_version=current['version'],children=children,
        limits=dict(max_model_turns=4,max_tool_actions=2,max_concurrent=2,deadline=time.time()+600),
        actions=ACTIONS,resource='fixture:repo',manifest_sha256='a'*64,idempotency_key='api-family')
    return browser,args


def opened(fixture):
    browser,args=fixture
    return rpc(browser,'open_delegation_family',args)


def rpc_for(browser,session,name,args,*,error=False):
    return rpc(browser,name,{'session_id':session,**args},error=error)


def test_family_tools_schema_and_open_get_lookup_roundtrip(family_api):
    browser,args=family_api;client,board,human,token,*_=browser
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
    tools={tool['name']:tool for tool in response.json()['result']['tools']}
    assert TOOLS<=set(tools) and len(tools)==55
    schema=tools['open_delegation_family']['inputSchema']
    assert schema['properties']['children']['minItems']==2
    assert schema['properties']['children']['maxItems']==2
    assert schema['properties']['manifest_sha256']['pattern']=='^[0-9a-f]{64}$'
    value=opened(family_api)
    assert value['state']=='running' and len(value['children'])==2
    assert rpc(browser,'open_delegation_family',args)==value
    assert rpc(browser,'get_delegation_family',{'family_id':value['id']})==value
    assert rpc(browser,'lookup_delegation_family',{'parent_ticket_id':args['parent_ticket_id'],
        'idempotency_key':args['idempotency_key']})=={'found':True,'family':value}
    assert client.get('/api/delegation-families/'+value['id']).json()==value
    health=client.get('/health').json()
    assert health['version']=='2.10.0' and health['schema_version']==12
    assert 'delegation_families' in health['features']
    assert board.get_runtime(human)['active_count']==0
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==3


@pytest.mark.parametrize('case',[
    'one_child','three_children','child_bool','child_executable','model_path','file_path','hidden_file',
    'file_backslash','duplicate_file','duplicate_session','limit_bool','limit_float','limit_string',
    'limit_extra','deadline_bool','deadline_string','actions_bool','action_shell','duplicate_action',
    'manifest_bool','manifest_short','resource_bool','version_bool'])
def test_open_rejects_unbounded_or_coerced_plan_without_partial_effect(family_api,case):
    browser,original=family_api;args=copy.deepcopy(original)
    if case=='one_child':args['children']=args['children'][:1]
    elif case=='three_children':args['children'].append(copy.deepcopy(args['children'][0]))
    elif case=='child_bool':args['children'][0]['session_id']=True
    elif case=='child_executable':args['children'][0]['executable']='/bin/sh'
    elif case=='model_path':args['children'][0]['model']='/bin/sh'
    elif case=='file_path':args['children'][0]['files']=['../private']
    elif case=='hidden_file':args['children'][0]['files']=['.env']
    elif case=='file_backslash':args['children'][0]['files']=['dir\\file']
    elif case=='duplicate_file':args['children'][1]['files']=['PART0.TXT']
    elif case=='duplicate_session':args['children'][1]['session_id']=args['children'][0]['session_id']
    elif case=='limit_bool':args['limits']['max_model_turns']=True
    elif case=='limit_float':args['limits']['max_model_turns']=4.0
    elif case=='limit_string':args['limits']['max_model_turns']='4'
    elif case=='limit_extra':args['limits']['unlimited']=True
    elif case=='deadline_bool':args['limits']['deadline']=True
    elif case=='deadline_string':args['limits']['deadline']=str(args['limits']['deadline'])
    elif case=='actions_bool':args['actions']=[True]
    elif case=='action_shell':args['actions']=['shell.execute']
    elif case=='duplicate_action':args['actions']=[ACTIONS[0],ACTIONS[0]]
    elif case=='manifest_bool':args['manifest_sha256']=True
    elif case=='manifest_short':args['manifest_sha256']='a'*63
    elif case=='resource_bool':args['resource']=True
    elif case=='version_bool':args['expected_version']=True
    rpc(browser,'open_delegation_family',args,error=True)
    with browser[1]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM delegation_families').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0


def test_mcp_stop_survives_profile_policy_withdrawal_without_releasing_runtime(family_api):
    browser,_=family_api;_,board,human,_,project,_,profile=browser;value=opened(family_api)
    member=value['children'][0]
    with board._connection() as db:
        session=db.execute('SELECT session_id FROM delegation_members WHERE ticket_id=?',(member['ticket_id'],)).fetchone()[0]
    raw=board.authenticate(browser[3]);actor=board.actor_session(raw,session)
    child=board.get_ticket(actor,member['ticket_id'])['ticket']
    board.claim_ticket(actor,child['id'],child['version'])
    execution=board.reserve_execution(actor,'family-stop-model',project['id'],child['id'],
        'family-stop-work','family-stop-turn','codex_chatgpt','fixture-model')['execution']
    current=board.get_agent_profile(human,profile)
    board.update_agent_profile(human,profile,current['version'],status='paused')
    board.set_policy(human,False,False,False,False,[],[])
    args={'family_id':value['id'],'expected_revision':value['revision']}
    stopped=rpc(browser,'stop_delegation_family',args)
    assert stopped['state']=='cancelled' and rpc(browser,'stop_delegation_family',args)==stopped
    assert board.get_runtime(human)['active_count']==1
    control=board.get_execution_control(actor,execution['id'])
    assert control['should_stop'] and control['cleanup_confirmed'] is False
    rpc(browser,'get_delegation_family',{'family_id':value['id']},error=True)


@pytest.mark.parametrize('revision',[True,0,-1,1.0,'1',None])
def test_human_stop_revision_is_strict_and_has_no_effect(family_api,revision):
    browser,_=family_api;client=browser[0];value=opened(family_api)
    response=client.post('/api/delegation-families/'+value['id']+'/stop',json={'expected_revision':revision})
    assert response.status_code==422
    assert rpc(browser,'get_delegation_family',{'family_id':value['id']})['state']=='running'


def test_human_stop_is_owner_scoped_csrf_protected_and_does_not_cleanup(family_api):
    browser,_=family_api;client,board,human,*_=browser;value=opened(family_api)
    route='/api/delegation-families/'+value['id']
    rpc_for(browser,'family-child-one','stop_delegation_family',
        {'family_id':value['id'],'expected_revision':value['revision']},error=True)
    assert rpc(browser,'get_delegation_family',{'family_id':value['id']})['state']=='running'
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post(route+'/stop',json={'expected_revision':value['revision']}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    stopped=client.post(route+'/stop',json={'expected_revision':value['revision']})
    assert stopped.status_code==200 and stopped.json()['state']=='cancelled'
    assert client.post(route+'/stop',json={'expected_revision':value['revision']}).json()==stopped.json()
    board.ensure_owner('thescreamer','TheScreamer','other-fixture-password')
    login=client.post('/api/login',json={'username':'thescreamer','password':'other-fixture-password'})
    client.headers['X-CSRF-Token']=login.json()['csrf_token']
    missing=client.get('/api/delegation-families/'+'0'*32)
    assert client.get(route).status_code==404
    assert client.get(route).json()==missing.json()
    assert client.post(route+'/stop',json={'expected_revision':stopped.json()['revision']}).status_code==404


def test_revoked_mcp_device_cannot_stop_but_human_owner_can(family_api):
    browser,_=family_api;client,board,human,token,*_=browser;value=opened(family_api)
    board.revoke_device(human,board.authenticate(token)['device_id'])
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'stop_delegation_family',
            'arguments':{'session_id':'runtime-api','family_id':value['id'],'expected_revision':value['revision']}}})
    assert response.status_code==401
    route='/api/delegation-families/'+value['id']
    assert client.get(route).json()['state']=='running'
    stopped=client.post(route+'/stop',json={'expected_revision':value['revision']})
    assert stopped.status_code==200 and stopped.json()['state']=='cancelled'
    assert board.get_runtime(human)['active_count']==0


def test_child_result_acceptance_and_finalization_use_exact_real_receipts(family_api):
    browser,args=family_api;_,board,human,token,project,*_=browser;value=opened(family_api)
    raw=board.authenticate(token)
    for index,member in enumerate(list(value['children'])):
        with board._connection() as db:
            session=db.execute('SELECT session_id FROM delegation_members WHERE ticket_id=?',(member['ticket_id'],)).fetchone()[0]
        actor=board.actor_session(raw,session)
        child=board.get_ticket(actor,member['ticket_id'])['ticket']
        child=board.claim_ticket(actor,child['id'],child['version'])
        execution=board.reserve_execution(actor,'api-child-'+str(index),project['id'],child['id'],
            'api-work-'+str(index),'api-turn-'+str(index),'codex_chatgpt','fixture-model')['execution']
        execution=board.start_execution(actor,execution['id'],execution['revision'])
        board.finish_execution(actor,execution['id'],execution['revision'],'completed',True,'completed')
        child=board.update_ticket(actor,child['id'],child['version'],status='review',release_claim=True,
            outcome='Completed bounded synthetic task',evidence='Synthetic independent check')
        with board._connection() as db:
            digest=_result_hash(db.execute('SELECT * FROM tickets WHERE id=?',(child['id'],)).fetchone())
        acceptance=dict(family_id=value['id'],expected_revision=value['revision'],child_ticket_id=child['id'],
            expected_child_version=child['version'],result_sha256=digest,verification_evidence='Coordinator inspected fixture',
            idempotency_key='api-accept-'+str(index))
        value=rpc(browser,'accept_delegation_result',acceptance)
        assert rpc(browser,'accept_delegation_result',acceptance)==value
    assert value['state']=='integrating'
    finish=dict(family_id=value['id'],expected_revision=value['revision'],snapshot_sha256='b'*64,
        verification_evidence='Coordinator integrated and inspected fixture',idempotency_key='api-finalize')
    done=rpc(browser,'finalize_delegation_family',finish)
    assert done['state']=='review_ready' and rpc(browser,'finalize_delegation_family',finish)==done
    parent=board.get_ticket(human,args['parent_ticket_id'])['ticket']
    assert parent['status']=='in_progress' and parent['claimant']['session_id']=='runtime-api'
    assert board.get_runtime(human)['active_count']==0


def test_compact_family_budget_and_trusted_full_source_client(family_api):
    import sys
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/codex-personal'))
    from blackoutburst_agents.family_client import FamilyBoardClient
    from blackoutburst_agents.board_reader import BoardReadError
    from test_api import call_tool,tool_data
    browser,args=family_api;client,board,human,token,*_=browser
    args=copy.deepcopy(args);args['limits']['max_concurrent']=1
    args['children'][1]['role']='reviewer'
    args['children'][0]['description']='Exact family source '+'😀'*1000
    channel=FamilyBoardClient({'mcp_url':'https://board.test/mcp','token':'synthetic','client':'codex','identity_key':'fixture','name':'Fixture'},'runtime-api',3)
    channel.adopt_registration();channel._initialize=lambda:None
    calls=[]
    def rpc(method,params):
        calls.append(params)
        return call_tool(client,token,params['name'],params['arguments'])
    channel._rpc=rpc
    full=channel.call('open_delegation_family',args)
    assert full['plan']['children'][0]['description']==args['children'][0]['description']
    assert channel.call('get_delegation_family',{'family_id':full['id']})==full
    assert all(x['arguments']['view']=='full' for x in calls)
    count=len(calls)
    with pytest.raises(BoardReadError):channel.call('get_delegation_family',{'family_id':full['id'],'view':'full'})
    assert len(calls)==count
    compact=call_tool(client,token,'get_delegation_family',{'session_id':'runtime-api','family_id':full['id']})
    assert not compact.get('isError') and len(json.dumps(compact,ensure_ascii=True).encode())<20000
    assert 'description' not in tool_data(compact)['plan']['children'][0]
