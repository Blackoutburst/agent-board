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


def test_mcp_release_status_correction_and_self_unassignment(browser):
    client, board = browser
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    project = board.create_project(human, 'Release API', ['blackoutburst'], idempotency_key='release-project')
    task = board.create_ticket(human, project['id'], 'Work', idempotency_key='release-task')
    token = board.create_device(human, 'Release worker', [])['token']
    rpc(client, token, 'register_agent', {'session_id': 'release-api', 'label': 'Release', 'agent_kind': 'codex'})
    base = {'session_id': 'release-api', 'ticket_id': task['id']}
    claimed = rpc(client, token, 'claim_ticket', {**base, 'expected_version': task['version']})
    args = {**base, 'expected_version': claimed['version'], 'idempotency_key': 'block',
            'reason': 'Waiting on environment', 'status': 'blocked'}
    released = rpc(client, token, 'release_ticket', args)
    assert released['status'] == 'blocked' and released['claimant'] is None
    assert rpc(client, token, 'release_ticket', args) == released
    fixed = rpc(client, token, 'release_ticket', {**args, 'expected_version': released['version'],
                'idempotency_key': 'fix', 'reason': 'Environment ready', 'status': 'todo', 'unassign_self': True})
    assert fixed['ready'] and fixed['assignee_agent_id'] is None


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
    assert health['version']=='2.13.1' and health['schema_version']== 14


def test_activity_order_is_available_through_rest_and_mcp(browser):
    client,board=browser
    human={'kind':'human','owner_id':'blackoutburst'}
    project=board.create_project(human,'Activity',['blackoutburst'],idempotency_key='activity')
    task=board.create_ticket(human,project['id'],'Task',idempotency_key='task')
    for number in range(3):
        task=board.update_ticket(human,task['id'],task['version'],description=f'Update {number}')
    params={'project_id':project['id'],'limit':2,'order':'asc'}
    first=client.get('/api/activity',params=params).json()
    second=client.get('/api/activity',params={**params,'after':first[-1]['id']}).json()
    ids=[event['id'] for event in first+second]
    assert len(ids)==4 and ids==sorted(set(ids))
    newest=client.get('/api/activity',params={'project_id':project['id'],'limit':2}).json()
    assert [event['id'] for event in newest]==list(reversed(ids[-2:]))
    assert client.get('/api/activity',params={'order':'unsupported'}).status_code==422

    token=board.create_device(human,'Activity reader',[])['token']
    rpc(client,token,'register_agent',{'session_id':'activity-reader','label':'Reader','agent_kind':'codex'})
    def activity(arguments):
        result=rpc(client,token,'list_activity',{'session_id':'activity-reader','view':'full',**arguments})
        return result['result'] if isinstance(result,dict) else result
    assert activity(params)==first
    assert activity({**params,'after':first[-1]['id']})==second
    assert activity({'project_id':project['id'],'limit':2})==newest
    rejected=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'list_activity',
        'arguments':{'session_id':'activity-reader','order':'unsupported'}}})
    assert rejected.status_code==200 and rejected.json()['result']['isError']


def test_mcp_human_request_pins_identity_after_browser_answer(browser):
    client,board=browser
    human={'kind':'human','owner_id':'blackoutburst'}
    project=board.create_project(human,'Handoff',['blackoutburst'],idempotency_key='handoff')
    task=board.create_ticket(human,project['id'],'Task',idempotency_key='task')
    token=board.create_device(human,'Worker',[])['token']
    registration=rpc(client,token,'register_agent',{'session_id':'original','label':'Original','agent_kind':'codex','identity_key':'worker'})
    rpc(client,token,'claim_ticket',{'session_id':'original','ticket_id':task['id'],'expected_version':task['version']})
    request=rpc(client,token,'request_human_input',{'session_id':'original','ticket_id':task['id'],
        'owner_id':'blackoutburst','question':'Which value?','idempotency_key':'question'})
    pinned=client.get('/api/tickets/'+task['id']).json()['ticket']
    assert pinned['assignee_agent_id']==registration['identity']['id'] and pinned['claimant'] is None
    assert client.post('/api/human-requests/'+request['id']+'/resolve',json={'answer':'Chosen value'}).status_code==200
    rpc(client,token,'register_agent',{'session_id':'restart','label':'Restart','agent_kind':'codex','identity_key':'worker','autonomous':True})
    resumed=rpc(client,token,'claim_next_ticket',{'session_id':'restart','project_id':project['id'],'idempotency_key':'resume'})['ticket']
    assert resumed['claimant']['agent_id']==registration['identity']['id']
    assert resumed['claimant']['session_id']=='restart'


def test_mcp_lookup_human_request_does_not_create_and_reads_resolved_answer(browser):
    client,board=browser
    human={'kind':'human','owner_id':'blackoutburst'}
    project=board.create_project(human,'Lookup',['blackoutburst'],idempotency_key='lookup')
    task=board.create_ticket(human,project['id'],'Task',idempotency_key='task')
    token=board.create_device(human,'Worker',[])['token']
    rpc(client,token,'register_agent',{'session_id':'lookup','label':'Lookup','agent_kind':'codex'})
    arguments={'session_id':'lookup','view':'full','ticket_id':task['id'],'owner_id':'blackoutburst',
               'question':'Which value?','idempotency_key':'question'}
    assert rpc(client,token,'lookup_human_request',arguments)=={'found':False}
    assert board.get_ticket(human,task['id'])['ticket']==task
    assert board.list_human_requests(human)==[]
    assert board.get_thread(human,task['thread_id'])['messages']==[]
    rpc(client,token,'claim_ticket',{'session_id':'lookup','ticket_id':task['id'],'expected_version':task['version']})
    claimed=board.get_ticket(human,task['id'])['ticket']
    for invalid in (str(claimed['version']), float(claimed['version']), True, False, 0):
        response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},json={
            'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'request_human_input',
            'arguments':{**arguments,'expected_version':invalid}}})
        assert response.status_code==200 and response.json()['result']['isError']
        assert board.get_ticket(human,task['id'])['ticket']==claimed
        assert board.list_human_requests(human)==[]
    changed=board.update_ticket(human,task['id'],claimed['version'],description='Updated requirements')
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'request_human_input',
        'arguments':{**arguments,'expected_version':claimed['version']}}})
    assert response.status_code==200 and response.json()['result']['isError']
    assert board.get_ticket(human,task['id'])['ticket']==changed
    assert board.list_human_requests(human)==[]
    request=rpc(client,token,'request_human_input',{**arguments,'expected_version':changed['version']})
    assert rpc(client,token,'lookup_human_request',arguments)==request
    answered=client.post('/api/human-requests/'+request['id']+'/resolve',json={'answer':'Chosen value'}).json()
    before=board.get_ticket(human,task['id'])
    assert rpc(client,token,'lookup_human_request',arguments)==answered
    assert board.get_ticket(human,task['id'])==before
    assert len(board.get_thread(human,task['thread_id'])['messages'])==2


def test_unsupported_mcp_lookup_fails_without_calling_creation(browser,monkeypatch):
    client,board=browser
    human={'kind':'human','owner_id':'blackoutburst'}
    token=board.create_device(human,'Worker',[])['token']
    # Simulate a server predating the additive tool, while retaining its
    # creation handler. An optional flag on that old handler would be ignored.
    client.app.state.mcp.remove_tool('lookup_human_request')
    creation_calls=[]
    def unexpected_creation(*args,**kwargs):
        creation_calls.append((args,kwargs))
        return {'unexpected':'creation'}
    monkeypatch.setattr(board,'request_human_input',unexpected_creation)
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},json={
        'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'lookup_human_request',
        'arguments':{'session_id':'lookup','ticket_id':'task','owner_id':'blackoutburst',
                     'question':'Which value?','idempotency_key':'question'}}})
    assert response.status_code==200 and response.json()['result']['isError']
    assert creation_calls==[]


def test_mcp_ticket_selection_lookup_never_creates_and_replays_empty_result(browser):
    from test_queue_selection import snapshot
    client, board = browser
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    project = board.create_project(human, 'Queue lookup', ['blackoutburst'], idempotency_key='queue-project')
    device = board.create_device(human, 'Queue fixture', [])
    token = device['token']
    rpc(client, token, 'register_agent', {'session_id': 'queue', 'label': 'Queue', 'agent_kind': 'codex', 'autonomous': True})
    args = {'session_id': 'queue', 'idempotency_key': 'one-selection', 'project_id': project['id']}
    before = snapshot(board)
    assert rpc(client, token, 'lookup_ticket_selection', args) == {'found': False}
    assert snapshot(board) == before
    empty = rpc(client, token, 'claim_next_ticket', args)
    assert empty == {'claimed': False, 'reason': 'no_ready_ticket', 'retried': False}
    ticket = board.create_ticket(human, project['id'], 'New task', idempotency_key='new-task')
    rpc(client, token, 'register_agent', {'session_id': 'queue', 'label': 'Queue reader', 'agent_kind': 'codex', 'autonomous': False})
    board.set_policy(human, True, True, True, False, [], [])
    before = snapshot(board)
    assert rpc(client, token, 'lookup_ticket_selection', args) == {
        'found': True, 'claimed': False, 'reason': 'no_ready_ticket', 'retried': True}
    assert snapshot(board) == before
    assert board.get_ticket(human, ticket['id'])['ticket']['claimant'] is None


def test_task_attachment_upload_retry_and_rest_mcp_links(browser):
    client, board = browser
    human = {'kind': 'human', 'owner_id': 'blackoutburst'}
    p = board.create_project(human, 'Files', ['blackoutburst'], idempotency_key='files')
    task = board.create_ticket(human, p['id'], 'Task', idempotency_key='task-files')
    url = '/api/threads/' + task['thread_id'] + '/attachments'
    def upload(content=b'file'):
        return client.post(url, files={'file': ('file.txt', content, 'text/plain')}, data={'idempotency_key': 'stable-file'})
    first = upload()
    assert first.status_code == 200
    assert upload().json() == first.json()
    assert upload(b'different').status_code == 409
    response = client.patch('/api/tickets/' + task['id'], json={'expected_version': task['version'], 'attachment_ids': [first.json()['id']]})
    assert response.status_code == 200
    assert response.json()['attachments'] == [first.json()]
    assert client.get('/api/threads/' + task['thread_id']).json()['messages'] == []
    assert client.get('/api/attachments/' + first.json()['id']).content == b'file'
    assert client.patch('/api/tickets/' + task['id'], json={'expected_version': response.json()['version'], 'attachment_ids': None}).status_code == 422
    token = board.create_device(human, 'Files worker', [])['token']
    rpc(client, token, 'register_agent', {'session_id': 'file-api', 'label': 'File API', 'agent_kind': 'codex'})
    base = {'session_id': 'file-api', 'ticket_id': task['id']}
    claimed = rpc(client, token, 'claim_ticket', {**base, 'expected_version': response.json()['version']})
    updated = rpc(client, token, 'update_ticket', {**base, 'expected_version': claimed['version'], 'changes': {'attachment_ids': []}})
    assert updated['attachments'] == []
