"""Actual cookie/HTTP and raw MCP boundaries for delegated ticket routing."""
import pytest
from fastapi.testclient import TestClient

from profile_management import TICKET_RESOURCE_ACTION
from test_api import application, call_tool, login, rpc, tool_data
from test_profile_management_api import lead_browser
from test_profile_management import policy


@pytest.fixture
def managed_resource(lead_browser):
    client,b,human,device,project,profile=lead_browser
    owner=b.get_settings(human)['policy']
    b.set_policy(human,**{key:owner[key] for key in ('enabled','allow_read','allow_reply','allow_requests','actions')},
                 resources=[*owner['resources'],'repo:next'])
    profile=b.update_agent_profile(human,profile['id'],profile['version'],
        policy=profile['policy']|{'resources':[*profile['policy']['resources'],'repo:next']})
    target=b.create_agent_profile(human,'Ordinary worker',project_ids=[project['id']],
        policy=policy(autonomy='requested',resources=['repo:fixture','repo:next']))
    task=b.create_ticket(human,project['id'],'Inactive routing fixture',resource='repo:fixture',
        assignee_owner_id=human['owner_id'],assignee_agent_id=target['id'])
    b.register_agent(b.authenticate(device['token']),'lead-api','Lead',[],'codex',False,'lead')
    return dict(client=client,b=b,human=human,device=device,project=project,profile=profile,target=target,task=task)


def args(s,**changes):
    return dict(session_id='lead-api',ticket_id=s['task']['id'],expected_version=s['task']['version'],
        expected_resource='repo:fixture',new_resource='repo:next',idempotency_key='retarget',
        reason='Use the explicitly authorized new workspace.')|changes


def call(s,values=None):
    return call_tool(s['client'],s['device']['token'],'retarget_managed_ticket_resource',values or args(s))


def enable(s):
    profile=s['b'].get_agent_profile(s['human'],s['profile']['id'])
    response=s['client'].put('/api/agent-profiles/'+profile['id']+'/ticket-resource-management',
        json={'expected_version':profile['version'],'enabled':True})
    assert response.status_code==200,response.text
    s['profile']=response.json()
    s['b'].register_agent(s['b'].authenticate(s['device']['token']),'lead-api','Lead',[],'codex',False,'lead')


def test_separate_human_grant_raw_mcp_closed_schema_and_exact_retry(managed_resource):
    s=managed_resource
    assert call(s).get('isError')
    enable(s)
    assert TICKET_RESOURCE_ACTION in s['profile']['policy']['actions']
    tools=rpc(s['client'],s['device']['token'],'tools/list').json()['result']['tools']
    spec=next(x for x in tools if x['name']=='retarget_managed_ticket_resource')['inputSchema']
    assert spec['additionalProperties'] is False
    assert set(spec['required'])=={'session_id','ticket_id','expected_version','expected_resource','new_resource','idempotency_key','reason'}
    result=tool_data(call(s))
    assert result['resource']=='repo:next' and result['version']==s['task']['version']+1
    assert result['claimant'] is None and result['assignee_agent_id']==s['target']['id']
    assert tool_data(call(s))==result
    assert s['b'].get_runtime(s['human'])['executions']==[]


@pytest.mark.parametrize('field,value',[
    ('expected_version',True),('expected_version',1.0),('expected_version','1'),('expected_version',None),
    ('expected_resource',None),('expected_resource',True),('new_resource',[]),('new_resource',''),
    ('new_resource','*'),('new_resource','server:all'),('new_resource','repo:outside'),
    ('reason',' '),('reason',True),('idempotency_key',None),('ticket_id',123),
    ('extra',True),('force',True),('cleanup_confirmed',True),
])
def test_raw_types_unknown_fields_and_outside_scopes_have_no_effect(managed_resource,field,value):
    s=managed_resource;enable(s)
    before=s['b'].get_ticket(s['human'],s['task']['id'])['ticket']
    assert call(s,args(s,**{field:value})).get('isError')
    assert s['b'].get_ticket(s['human'],s['task']['id'])['ticket']==before


def test_empty_expected_resource_is_supported_but_stale_resource_is_not(managed_resource):
    s=managed_resource;enable(s)
    empty=s['b'].create_ticket(s['human'],s['project']['id'],'Unrouted fixture',resource='',
        assignee_owner_id=s['human']['owner_id'],assignee_agent_id=s['target']['id'])
    result=tool_data(call(s,args(s,ticket_id=empty['id'],expected_version=empty['version'],
        expected_resource='',idempotency_key='empty')))
    assert result['resource']=='repo:next'
    assert call(s,args(s,expected_resource='repo:next',idempotency_key='wrong-source')).get('isError')


def test_human_grant_requires_cookie_owner_origin_csrf_and_strict_cas(managed_resource):
    s=managed_resource;client=s['client'];profile=s['profile']
    path='/api/agent-profiles/'+profile['id']+'/ticket-resource-management'
    body={'expected_version':profile['version'],'enabled':True}
    agent=TestClient(client.app)
    assert agent.put(path,headers={'Authorization':'Bearer '+s['device']['token']},json=body).status_code==401
    csrf=client.headers.pop('X-CSRF-Token');assert client.put(path,json=body).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.put(path,json=body,headers={'Origin':'https://attacker.invalid'}).status_code==403
    assert client.put(path,json=body|{'expected_version':profile['version']-1}).status_code==409
    assert client.put(path,json=body|{'enabled':'true'}).status_code==422
    assert client.put(path,json=body|{'force':True}).status_code==422
    other=TestClient(client.app);login(other,'thescreamer')
    assert other.put(path,json=body).status_code==404
    assert TICKET_RESOURCE_ACTION not in s['b'].get_agent_profile(s['human'],profile['id'])['policy']['actions']


def test_disabled_management_revokes_new_permission_and_blocks_old_session(managed_resource):
    s=managed_resource;enable(s);p=s['profile']
    response=s['client'].put('/api/agent-profiles/'+p['id']+'/management',
        json={'expected_version':p['version'],'enabled':False})
    assert response.status_code==200,response.text
    assert TICKET_RESOURCE_ACTION not in response.json()['policy']['actions']
    assert call(s).get('isError')
    # Revocation stays available even after the prerequisite grant was removed.
    response=s['client'].put('/api/agent-profiles/'+p['id']+'/ticket-resource-management',
        json={'expected_version':response.json()['version'],'enabled':False})
    assert response.status_code==200,response.text
