from fastapi.testclient import TestClient
import pytest

from app import create_app


@pytest.fixture
def browser(tmp_path):
    app=create_app(tmp_path/'data',base_url='http://testserver',filebrowser_url='',allow_local_passwords=True)
    board=app.state.board
    board.ensure_owner('blackoutburst','Blackoutburst','fixture-password-only')
    board.set_policy({'kind':'human','owner_id':'blackoutburst'},True,True,True,True,[],[])
    with TestClient(app) as client:
        login=client.post('/api/login',json={'username':'blackoutburst','password':'fixture-password-only'})
        client.headers['X-CSRF-Token']=login.json()['csrf_token']
        yield client,board


def rpc(client,token,name,arguments):
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':arguments}})
    assert response.status_code==200
    result=response.json()['result']
    assert not result.get('isError'),result
    import json
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def test_pairing_and_mcp_project_workflow(browser):
    client,board=browser
    p=client.post('/api/projects',json={'name':'Team','participants':['blackoutburst'],'idempotency_key':'p'}).json()
    code=client.post('/api/settings/pairings',json={'label':'Work Mac','capabilities':[]})
    assert code.status_code==200
    exchange=client.post('/mcp/connect/exchange',json={'code':code.json()['code']})
    assert exchange.status_code==200 and exchange.headers['cache-control']=='no-store'
    token=exchange.json()['token']
    assert client.post('/mcp/connect/exchange',json={'code':code.json()['code']}).status_code==400
    registration=rpc(client,token,'register_agent',{'session_id':'api-worker','label':'API worker','agent_kind':'codex','identity_key':'work-mac','skills':['macos'],'autonomous':True})
    assert registration['identity']['name']=='Codex · Work Mac'
    task=rpc(client,token,'create_ticket',{'session_id':'api-worker','project_id':p['id'],'title':'Parent','idempotency_key':'parent'})
    claim=rpc(client,token,'claim_next_ticket',{'session_id':'api-worker','project_id':p['id'],'idempotency_key':'claim'})
    assert claim['ticket']['id']==task['id']
    delegation=rpc(client,token,'delegate_task',{'session_id':'api-worker','parent_ticket_id':task['id'],'expected_version':claim['ticket']['version'],'title':'Linux subtask','description':'Run the Linux-only tool','required_skills':['linux'],'idempotency_key':'delegate'})
    assert delegation['parent']['status']=='blocked'
    assert delegation['child']['required_skills']==['linux']
    get=client.get('/api/tickets/'+task['id']).json()['ticket']
    assert get['blocked_by'][0]['id']==delegation['child']['id']
    agents=client.get('/api/agents').json()
    assert agents[0]['name']=='Codex · Work Mac'


def test_human_requests_csrf_and_pairing_throttling(browser):
    client,_=browser
    p=client.post('/api/projects',json={'name':'Team','participants':['blackoutburst'],'idempotency_key':'p'}).json()
    t=client.post('/api/projects/'+p['id']+'/tickets',json={'title':'Task','idempotency_key':'t'}).json()
    request=client.post('/api/tickets/'+t['id']+'/input',json={'owner_id':'blackoutburst','question':'Which value?','idempotency_key':'input'}).json()
    assert client.get('/api/human-requests').json()[0]['id']==request['id']
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.post('/api/human-requests/'+request['id']+'/resolve',json={'answer':'Answer'}).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.post('/api/human-requests/'+request['id']+'/resolve',json={'answer':'Team value'}).status_code==200
    assert client.get('/api/tickets/'+t['id']).json()['ticket']['ready']
    for _ in range(10):
        assert client.post('/mcp/connect/exchange',json={'code':'AAAA-AAAA-AAAA-AAAA'}).status_code==400
    assert client.post('/mcp/connect/exchange',json={'code':'AAAA-AAAA-AAAA-AAAA'}).status_code==429


def test_no_anonymous_private_metadata_and_static_assets(browser):
    client,_=browser
    client.cookies.clear()
    for route in ['/api/agents','/api/work','/api/human-requests','/api/activity']:
        assert client.get(route).status_code==401
    assert client.get('/workspace.js').status_code==200
    assert client.get('/connect.py').status_code==200
    health=client.get('/health').json()
    assert health['version']=='2.0.0' and health['schema_version']==3
