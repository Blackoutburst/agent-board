"""Actual HTTP/MCP and production admin boundary for delegated profile editing."""
import json

from fastapi.testclient import TestClient
import pytest

from profile_management import PROFILE_MANAGEMENT_TOOLS
from test_api import application,login,rpc,call_tool,tool_data
from test_profile_management import policy


@pytest.fixture
def lead_browser(application):
    board=application.state.board
    with TestClient(application) as client:
        human=login(client)
        board.set_policy(human,True,True,True,True,['repo.read_file','repo.patch_file'],['repo:fixture'])
        project=board.create_project(human,'API scope',['blackoutburst'],idempotency_key='scope')
        profile=board.create_agent_profile(human,'Lead',project_ids=[project['id']],policy=policy())
        path='/api/agent-profiles/'+profile['id']+'/management'
        grant=client.put(path,json={'expected_version':profile['version'],'enabled':True})
        assert grant.status_code==200,grant.text
        profile=grant.json()
        device=board.create_device(human,'Synthetic Lead host',[])
        profile=board.bind_agent_profile(human,profile['id'],profile['version'],device['id'],'lead','codex')
        raw=board.authenticate(device['token'])
        board.register_agent(raw,'lead-api','Lead',[],'codex',False,'lead')
        yield client,board,human,device,project,profile


def call(s,name,args=None,error=False):
    result=call_tool(s[0],s[3]['token'],name,{'session_id':'lead-api',**(args or {})})
    if error:
        assert result.get('isError'),result
        return result
    return tool_data(result)


def profile_input(s,**changes):
    return dict(name='MCP coder',idempotency_key='new',project_ids=[s[4]['id']],
        policy=policy(autonomy='requested'),instructions='Exact instructions\nkept intact.')|changes


def test_actual_mcp_tools_roundtrip_exact_retry_and_cas(lead_browser):
    s=lead_browser
    tools=rpc(s[0],s[3]['token'],'tools/list').json()['result']['tools']
    assert PROFILE_MANAGEMENT_TOOLS<={tool['name'] for tool in tools}
    assert all(tool['inputSchema']['additionalProperties'] is False for tool in tools if tool['name'] in PROFILE_MANAGEMENT_TOOLS)
    p=call(s,'create_managed_agent_profile',{'profile':profile_input(s)})
    assert p==call(s,'create_managed_agent_profile',{'profile':profile_input(s)})
    assert p['instructions']=='Exact instructions\nkept intact.' and 'bindings' not in p
    assert call(s,'get_managed_agent_profile',{'profile_id':p['id']})==p
    compact=call(s,'get_managed_agent_profile',{'profile_id':p['id'],'view':'compact'})
    assert compact['full_view']=={'tool':'get_managed_agent_profile','profile_id':p['id'],'view':'full'}
    assert 'instructions' not in compact and 'get_text' not in json.dumps(compact)
    listed=call(s,'list_managed_agent_profiles')
    assert [item['id'] for item in listed['items']]==[p['id']]
    update=dict(profile_id=p['id'],expected_version=p['version'],idempotency_key='edit',changes={'role':'Developer'})
    changed=call(s,'update_managed_agent_profile',update)
    assert changed==call(s,'update_managed_agent_profile',update)
    call(s,'update_managed_agent_profile',update|{'idempotency_key':'stale'},error=True)
    archive=dict(profile_id=p['id'],expected_version=changed['version'],idempotency_key='archive')
    archived=call(s,'archive_managed_agent_profile',archive)
    assert archived==call(s,'archive_managed_agent_profile',archive)
    assert archived['status']=='archived'
    assert call(s,'list_managed_agent_profiles')['items']==[]
    assert call(s,'list_managed_agent_profiles',{'include_archived':True})['items'][0]['id']==p['id']


@pytest.mark.parametrize('change',[
    {'extra':True},{'owner_id':'thescreamer'},{'profile':'{}'},
    {'profile':{'name':'invalid','idempotency_key':'invalid','token':'secret'}},
    {'profile':{'name':'invalid','idempotency_key':'invalid','policy':{'allow_read':'true'}}},
    {'profile':{'name':'invalid','idempotency_key':'invalid','bindings':[]}},
])
def test_mcp_rejects_unknown_coerced_and_connection_fields_before_effects(lead_browser,change):
    s=lead_browser
    call(s,'create_managed_agent_profile',{'profile':profile_input(s)}|change,error=True)
    assert s[1].list_managed_agent_profiles(s[1].actor_session(s[1].authenticate(s[3]['token']),'lead-api'))==[]


@pytest.mark.parametrize('version',[True,1.0,'1',None])
def test_mcp_versions_are_raw_strict(lead_browser,version):
    s=lead_browser;p=call(s,'create_managed_agent_profile',{'profile':profile_input(s)})
    call(s,'update_managed_agent_profile',dict(profile_id=p['id'],expected_version=version,
        idempotency_key='bad-version',changes={'name':'Changed'}),error=True)
    assert call(s,'get_managed_agent_profile',{'profile_id':p['id']})==p


def test_grant_route_requires_real_human_cookie_owner_origin_csrf_and_version(lead_browser):
    s=lead_browser;client,board,human,device,_,profile=s
    path='/api/agent-profiles/'+profile['id']+'/management'
    body={'expected_version':profile['version'],'enabled':False}
    agent_only=TestClient(client.app)
    assert agent_only.put(path,headers={'Authorization':'Bearer '+device['token']},json=body).status_code==401
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.put(path,json=body).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.put(path,json=body,headers={'Origin':'https://attacker.invalid'}).status_code==403
    assert client.put(path,json=body|{'expected_version':1}).status_code==409
    other=TestClient(client.app)
    login(other,'thescreamer')
    assert other.put(path,json=body).status_code==404
    assert board.get_agent_profile(human,profile['id'])['version']==profile['version']
