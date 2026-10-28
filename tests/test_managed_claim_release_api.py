"""Real HTTP/MCP boundaries for opt-in Lead stale-claim release."""
import pytest
from fastapi.testclient import TestClient

from profile_management import CLAIM_RELEASE_ACTION, PROFILE_MANAGEMENT_TOOLS
from test_api import application, call_tool, rpc, tool_data
from test_profile_management_api import lead_browser
from test_profile_management import policy


@pytest.fixture
def managed(lead_browser):
    client,b,human,device,project,profile=lead_browser
    target=b.create_agent_profile(human,'Ordinary worker',project_ids=[project['id']],policy=policy(autonomy='requested'))
    target_device=b.create_device(human,'Ordinary host',[])
    target=b.bind_agent_profile(human,target['id'],target['version'],target_device['id'],'coder','codex')
    raw=b.authenticate(target_device['token']);b.register_agent(raw,'coder-api','Coder',[],'codex',False,'coder')
    actor=b.actor_session(raw,'coder-api')
    task=b.create_ticket(human,project['id'],'Expired fixture',assignee_agent_id=target['id'])
    task=b.claim_ticket(actor,task['id'],task['version'])
    with b._tx() as db:db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?',(task['id'],))
    return dict(client=client,b=b,human=human,device=device,project=project,profile=profile,target=target,task=task)


def args(s,**changes):
    return dict(session_id='lead-api',ticket_id=s['task']['id'],expected_version=s['task']['version'],
        idempotency_key='release',reason='Worker stopped.',recovery_evidence='Inspected the operation and its pending effects.')|changes


def enable(s):
    profile=s['b'].get_agent_profile(s['human'],s['profile']['id'])
    response=s['client'].put('/api/agent-profiles/'+profile['id']+'/claim-management',json={'expected_version':profile['version'],'enabled':True})
    assert response.status_code==200,response.text
    s['b'].register_agent(s['b'].authenticate(s['device']['token']),'lead-api','Lead',[],'codex',False,'lead')
    s['profile']=response.json()


def call(s,values=None):
    return call_tool(s['client'],s['device']['token'],'release_managed_agent_claim',values or args(s))


def test_human_grant_and_actual_closed_mcp_roundtrip(managed):
    s=managed
    assert call(s)['isError']
    enable(s)
    tools=rpc(s['client'],s['device']['token'],'tools/list').json()['result']['tools']
    spec=next(x for x in tools if x['name']=='release_managed_agent_claim')
    assert spec['inputSchema']['additionalProperties'] is False
    assert set(spec['inputSchema']['required'])=={'session_id','ticket_id','expected_version','idempotency_key','reason','recovery_evidence'}
    result=tool_data(call(s))
    assert result['claimant'] is None and result['status']=='todo'
    assert result['version']==s['task']['version']+1
    assert tool_data(call(s))==result
    runtime=s['b'].get_runtime(s['human'])
    assert runtime['executions']==[]


@pytest.mark.parametrize('field,value',[
    ('expected_version',True),('expected_version',1.0),('expected_version','1'),
    ('expected_version',None),('reason',' '),('reason',True),('recovery_evidence',' '),
    ('recovery_evidence',{}),('idempotency_key',None),('ticket_id',123),('extra',True),
    ('profile_id','arbitrary'),('cleanup_confirmed',True),('force',True),
])
def test_raw_tool_types_unknown_fields_and_claim_assertions_are_rejected(managed,field,value):
    s=managed;enable(s)
    before=s['b'].get_ticket(s['human'],s['task']['id'])['ticket']
    assert call(s,args(s,**{field:value})).get('isError')
    assert s['b'].get_ticket(s['human'],s['task']['id'])['ticket']==before


def test_grant_route_requires_human_owner_origin_csrf_and_cas(managed):
    s=managed;client=s['client'];profile=s['profile'];path='/api/agent-profiles/'+profile['id']+'/claim-management'
    body={'expected_version':profile['version'],'enabled':True}
    agent=TestClient(client.app)
    assert agent.put(path,headers={'Authorization':'Bearer '+s['device']['token']},json=body).status_code==401
    csrf=client.headers.pop('X-CSRF-Token');assert client.put(path,json=body).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.put(path,json=body,headers={'Origin':'https://attacker.invalid'}).status_code==403
    assert client.put(path,json=body|{'expected_version':profile['version']-1}).status_code==409
    assert client.put(path,json=body|{'enabled':'true'}).status_code==422
    assert client.put(path,json=body|{'force':True}).status_code==422
    assert CLAIM_RELEASE_ACTION not in s['b'].get_agent_profile(s['human'],profile['id'])['policy']['actions']
