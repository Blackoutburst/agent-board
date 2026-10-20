"""Human-only ASGI routes, exact JSON and legacy device wire, zero providers."""
import json

import pytest

from server_provisioning import GRANT_SCHEMA, REQUEST_SCHEMA, CANCEL_SCHEMA
from test_server_instances_api import browser, setup, rpc, observation


def prepare(browser):
    client,b,human,device,_=browser; host,i=setup(browser)
    cmd=client.post('/api/server-instances/'+i['id']+'/commands',json=dict(action='stop',expected_version=i['version'],
        expected_generation=i['generation'],launchspec_sha256=i['launchspec_sha256'],budget=0,idempotency_key='stop')).json()
    accepted=rpc(browser,'ack_instance_command',dict(host_id=host['id'],command_id=cmd['id'],expected_revision=cmd['revision'],idempotency_key='ack'))
    rpc(browser,'report_instance_state',observation(host,accepted['instance']))
    p=b.create_agent_profile(human,'Desired',preferred_provider='codex_chatgpt')
    p=b.bind_agent_profile(human,p['id'],p['version'],device['id'],'desired','codex')
    path='/api/server-hosts/'+host['id']
    grant=dict(schema=GRANT_SCHEMA,expected_host_version=host['version'],expected_grant_version=0,enabled=True,idempotency_key='enable')
    return path,p,grant


def request_body(client,path,p):
    context=client.get(path+'/provisioning-context',params={'profile_id':p['id']})
    assert context.status_code==200,context.text
    value=context.json()
    keys=('expected_host_version','expected_catalog_version','expected_instances_sha256','grant_version','profile_id',
        'profile_version','profile_sha256','provider_id','requested_model')
    return dict(schema=REQUEST_SCHEMA,**{k:value[k] for k in keys},idempotency_key='request')


def test_real_human_request_cancel_and_closed_legacy_mcp_roundtrip(browser):
    from blackoutburst_agents.server_control_protocol import response
    client,b,human,*_=browser; path,p,g=prepare(browser)
    assert client.get(path+'/provisioning-grant').json()['version']==0
    assert client.put(path+'/provisioning-grant',json=g).json()['enabled'] is True
    before=b.get_runtime(human)
    values=request_body(client,path,p)
    posted=client.post(path+'/provisioning-requests',json=values)
    assert posted.status_code==200,posted.text
    r=posted.json();assert r['requested_model'] is None
    assert posted.headers['cache-control']=='no-store'
    assert client.get(path+'/provisioning-requests').json()['requests']==[r]
    assert client.get(path+'/provisioning-requests/'+r['request_id']).json()==r
    poll=rpc(browser,'poll_instance_commands',{'host_id':r['host_id']}); response('poll_instance_commands',poll)
    assert poll['instances'][0]['control_reason']=='authorization_changed'
    c=client.post(path+'/provisioning-requests/'+r['request_id']+'/cancel',json=dict(schema=CANCEL_SCHEMA,expected_revision=1,idempotency_key='cancel'))
    assert c.status_code==200,c.text
    assert c.json()['state']=='cancelled' and not c.json()['can_cancel']
    assert b.get_runtime(human)==before


def test_human_cookie_csrf_origin_required_and_not_new_mcp_tools(browser):
    client,b,human,device,*_=browser; path,p,g=prepare(browser)
    csrf=client.headers.pop('X-CSRF-Token')
    assert client.put(path+'/provisioning-grant',json=g).status_code==403
    client.headers['X-CSRF-Token']=csrf
    assert client.put(path+'/provisioning-grant',json=g,headers={'Origin':'https://foreign.invalid'}).status_code==403
    for name in ('create_server_provisioning_request','set_server_provisioning_grant','cancel_server_provisioning_request'):
        rpc(browser,name,{'host_id':path.split('/')[3]},error=True)
    client.cookies.clear()
    assert client.get(path+'/provisioning-grant').status_code==401
    assert client.get(path+'/provisioning-context',params={'profile_id':p['id']}).status_code==401


@pytest.mark.parametrize('raw', [
    b'{"schema":"x","schema":"y"}', b'{"enabled":NaN}', b'{"enabled":true,"extra":"SYNTHETIC_PRIVATE"}',
    b'\xff', b'[]', b'{"enabled":1}', b'{"schema":"SYNTHETIC_PRIVATE"}',
])
def test_raw_json_errors_are_bounded_closed_and_never_echo_secrets(browser,raw):
    client,b,*_=browser;path,_,_=prepare(browser)
    result=client.put(path+'/provisioning-grant',content=raw,headers={'Content-Type':'application/json'})
    assert result.status_code in (400,422) and 'SYNTHETIC_PRIVATE' not in result.text
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM server_provisioning_grants').fetchone()[0]==0


def test_route_limit_is_eight_kib_only_for_new_provisioning_routes(browser):
    client,*_=browser;path,p,g=prepare(browser)
    raw=json.dumps(g).encode();raw+=b' '*(8192-len(raw))
    assert client.put(path+'/provisioning-grant',content=raw,headers={'Content-Type':'application/json'}).status_code==200
    assert client.put(path+'/provisioning-grant',content=raw+b' ',headers={'Content-Type':'application/json'}).status_code==413
    assert client.post('/api/server-hosts',content=b' '*4097,headers={'Content-Type':'application/json'}).status_code==413


@pytest.mark.parametrize('query', ['profile_id=x&profile_id=y','profile_id=x&secret=SYNTHETIC_PRIVATE','', 'profile_id='])
def test_context_query_is_closed(browser,query):
    client,*_=browser;path,_,_=prepare(browser)
    result=client.get(path+'/provisioning-context?'+query)
    assert result.status_code in (400,409,422) and 'SYNTHETIC_PRIVATE' not in result.text


@pytest.mark.parametrize('query',['limit=true','limit=17','limit=1.0','limit=01','limit=1&limit=2','extra=SYNTHETIC_PRIVATE'])
def test_request_list_does_not_coerce_unknown_query(browser,query):
    client,*_=browser;path,_,_=prepare(browser)
    result=client.get(path+'/provisioning-requests?'+query)
    assert result.status_code==422 and 'SYNTHETIC_PRIVATE' not in result.text
