"""Actual MCP/REST transport for additive message reservations."""
import pytest

from test_runtime_api import browser, rpc


def request(browser,**changes):
    _,board,human,_,_,_,profile=browser
    thread=board.create_thread(human,'Message only',[human['owner_id']],'message-api')
    message=board.post_message(human,thread['id'],'Human question',mentions=['agent:'+profile],idempotency_key='message-api')
    source=rpc(browser,'get_message',{'thread_id':thread['id'],'message_id':message['id']})
    values=dict(idempotency_key='message-api',thread_id=thread['id'],message_id=message['id'],
        source_sha256=source['source_sha256'],work_id='message-api-work',turn_id='message-api-turn',
        provider='codex_chatgpt',model='fixture-model')
    values.update(changes)
    return rpc(browser,'reserve_message_execution',values,error=bool(changes))


def test_message_mcp_roundtrip_and_owner_view_share_existing_control_tools(browser):
    client,board,human,*_=browser
    board.set_policy(human,True,True,True,False,[],[])
    first=request(browser);execution=first['execution']
    assert execution['scope_kind']=='message' and execution['ticket_id'] is None
    assert request(browser)['execution']==execution
    assert client.get('/api/runtime').json()['active_count']==1
    active=rpc(browser,'start_execution',{'execution_id':execution['id'],'expected_revision':1})
    done=rpc(browser,'finish_execution',{'execution_id':execution['id'],'expected_revision':active['revision'],
        'status':'completed','cleanup_confirmed':True,'reason':'completed'})
    assert done['state']=='finished' and client.get('/api/runtime').json()['active_count']==0
    assert rpc(browser,'lookup_execution',{'idempotency_key':'message-api'})['execution']==done
    assert client.get('/health').json()['schema_version']== 11


@pytest.mark.parametrize('field,value',[('source_sha256',True),('source_sha256','0'*63),
    ('thread_id',False),('message_id',''),('work_id','/private/path'),('provider',1)])
def test_message_rpc_strict_values_refuse_without_slot(browser,field,value):
    request(browser,**{field:value})
    assert browser[0].get('/api/runtime').json()['active_count']==0
