"""Actual authenticated MCP over local ASGI; no live service or model."""
import json
import pytest
from fastapi.testclient import TestClient
from app import create_app
from board import BoardError
from hierarchy_mcp import HIERARCHY_TOOLS, validate_hierarchy_arguments
from test_hierarchy_store import setup, enabled
from test_hierarchy_policy import child


@pytest.fixture
def channel(setup):
    s = setup; root = enabled(s); b = s['b']
    device = b.create_device(s['human'], 'MCP parent', [])
    p = b.get_agent_profile(s['human'], s['parent']['id'])
    b.bind_agent_profile(s['human'], p['id'], p['version'], device['id'], 'mcp-parent', 'codex')
    b.register_agent(b.authenticate(device['token']), 'hierarchy-mcp', 'Parent', [], 'codex', True, 'mcp-parent')
    app = create_app(b.data_dir, base_url='http://testserver', filebrowser_url='', allow_local_passwords=True)
    with TestClient(app) as client:
        def rpc(name, args, error=False):
            response = client.post('/mcp', headers={'Authorization': 'Bearer '+device['token'],
                'Accept': 'application/json, text/event-stream'}, json={'jsonrpc':'2.0','id':1,
                'method':'tools/call','params':{'name':name,'arguments':{'session_id':'hierarchy-mcp',**args}}})
            assert response.status_code == 200
            value = response.json()['result']
            assert bool(value.get('isError')) == error, value
            return value if error else value.get('structuredContent') or json.loads(value['content'][0]['text'])
        yield s, root, rpc, client, device


def test_mcp_schema_and_scoped_team_roundtrip(channel):
    s, root, rpc, client, device = channel
    response = client.post('/mcp', headers={'Authorization':'Bearer '+device['token'],
        'Accept':'application/json, text/event-stream'}, json={'jsonrpc':'2.0','id':1,'method':'tools/list'})
    tools = {t['name']:t for t in response.json()['result']['tools']}
    assert HIERARCHY_TOOLS <= tools.keys()
    assert not any('hierarchy' in n and ('root' in n or 'configure' in n) for n in tools)
    assert all(tools[n]['inputSchema']['additionalProperties'] is False for n in HIERARCHY_TOOLS)
    assert rpc('inspect_hierarchy', {'delegation_id':root['id']})['effective']['project_ids'] == s['ceiling']()['project_ids']
    args = dict(parent_id=root['id'], name='Reviewer', role='reviewer', policy=child(
        project_ids=s['ceiling']()['project_ids'], operations=[], max_depth=0), expected_version=1,idempotency_key='team')
    leaf = rpc('create_hierarchy_team_profile', args)
    assert rpc('create_hierarchy_team_profile', args) == leaf
    updated = rpc('update_hierarchy_team_profile', dict(parent_id=root['id'],profile_id=leaf['profile_id'],
        changes={'description':'Independent reviewer'},expected_version=1,idempotency_key='edit'))
    assert updated['version'] == 2
    archived = rpc('archive_hierarchy_team_profile', dict(parent_id=root['id'],profile_id=leaf['profile_id'],
        expected_version=2,idempotency_key='archive'))
    assert archived['version'] == 3


def test_mcp_refuses_forged_fields_wrong_beneficiary_and_revoked_grant(channel):
    s, root, rpc, _, _ = channel
    args = dict(parent_id=root['id'], beneficiary_id=s['target']['id'],policy=s['subordinate'](),
                expected_version=1,idempotency_key='delegate')
    leaf = rpc('delegate_hierarchy_project', args)
    rpc('inspect_hierarchy', {'delegation_id':leaf['id']}, error=True)
    for changes in ({'actor':s['human']},{'owner_id':'bob'},{'expected_version':True},
                    {'expected_version':'1'},{'policy':json.dumps(args['policy'])}):
        rpc('delegate_hierarchy_project', args | changes, error=True)
    s['store'].change(s['human'],root['id'],1,'revoke',revoke=True)
    rpc('delegate_hierarchy_project',args,error=True)
    rpc('inspect_hierarchy',{'delegation_id':root['id']},error=True)


@pytest.mark.parametrize('name', sorted(HIERARCHY_TOOLS))
def test_validator_refuses_unknown_and_missing_fields(name):
    with pytest.raises(BoardError): validate_hierarchy_arguments(name, {'session_id':'session','actor':{}})


def test_mcp_project_constructor_binds_singleton_without_connection(channel):
    s, root, rpc, _, _ = channel
    args = dict(parent_id=root['id'],name='Created project',lead_name='Project Lead',
                policy=child(project_ids=[]),expected_version=1,idempotency_key='new-project')
    created = rpc('create_hierarchy_project_lead',args)
    assert rpc('create_hierarchy_project_lead',args) == created
    profile = s['b'].get_agent_profile(s['human'],created['profile_id'])
    assert profile['project_ids'] == [created['project_id']]
    assert profile['bindings'] == []
    rpc('inspect_hierarchy', {'delegation_id':root['id'],'session_id':'unknown-session'},error=True)
