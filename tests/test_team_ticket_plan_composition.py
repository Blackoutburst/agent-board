"""Normal SDK Lead -> actual ASGI/MCP -> temporary Board, no real network."""
from copy import deepcopy
import http.client
import io
import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import create_app
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents.team_ticket_client import TeamTicketBoardClient
from blackoutburst_agents.team_ticket_plan import TeamTicketPlanController


class AsgiResponse:
    def __init__(self,response):
        self.status=response.status_code;self.headers=response.headers
        self.url=str(response.url);self.body=io.BytesIO(response.content)
    def geturl(self):return self.url
    def read(self,size=-1):return self.body.read(size)
    read1=read
    def __enter__(self):return self
    def __exit__(self,*_):self.body.close()


class AsgiWire:
    """Only replace network IO; SDK RPC, auth, schema and Board stay real."""
    def __init__(self,api):
        self.api=api;self.calls=[];self.delivered=[];self.lose_next_create=False
        self.before_create=lambda _args:None
    def open(self,request,timeout):
        assert request.full_url=='http://localhost/mcp' and timeout>0
        payload=json.loads(request.data);self.calls.append(deepcopy(payload))
        creating=payload.get('method')=='tools/call' and payload['params'].get('name')=='create_ticket'
        if creating:self.before_create(deepcopy(payload['params']['arguments']))
        response=self.api.post('/mcp',content=request.data,headers=dict(request.header_items()))
        if creating:
            assert response.status_code==200,response.text
            self.delivered.append(deepcopy(payload['params']['arguments']))
            if self.lose_next_create:
                self.lose_next_create=False
                raise http.client.RemoteDisconnected('SYNTHETIC lost response after real Board commit')
        return AsgiResponse(response)
    def discard(self):pass
    def close(self):pass


def ordinary_policy(**changes):
    return dict(allow_read=True,allow_messages=True,allow_requests=True,
        actions=['repo.read_file','repo.patch_file','repo.run_test'],resources=['repo:plan','repo:other'],
        autonomy='requested',allow_create_projects=False)|changes


@pytest.fixture
def actual_plan(tmp_path):
    application=create_app(tmp_path/'data',base_url='http://localhost',filebrowser_url='',
        allow_local_passwords=True,root_path='')
    b=application.state.board;b.ensure_owner('alice','Alice');b.ensure_owner('bob','Bob')
    human={'kind':'human','owner_id':'alice'};bob={'kind':'human','owner_id':'bob'}
    b.set_policy(human,True,True,True,True,['repo.read_file','repo.patch_file','repo.run_test'],['repo:plan','repo:other'])
    b.set_policy(bob,True,True,True,True,[],[])
    project=b.create_project(human,'Explicit team plan',['alice','bob'],idempotency_key='project')
    hidden=b.create_project(human,'Outside Lead scope',['alice'],idempotency_key='hidden')
    lead=b.create_agent_profile(human,'Lead',project_ids=[project['id']],policy=ordinary_policy(autonomy='queue'))
    lead=b.set_agent_profile_management(human,lead['id'],lead['version'],True)
    device=b.create_device(human,'Synthetic Lead device',[])
    lead=b.bind_agent_profile(human,lead['id'],lead['version'],device['id'],'lead','codex')
    coder=b.create_agent_profile(human,'Ordinary coder',project_ids=[project['id']],policy=ordinary_policy())
    coder_device=b.create_device(human,'Synthetic coder device',[])
    coder=b.bind_agent_profile(human,coder['id'],coder['version'],coder_device['id'],'coder','codex')
    raw_coder=b.authenticate(coder_device['token']);b.register_agent(raw_coder,'coder-session','Coder',[],'codex',False,'coder')
    coder_actor=b.actor_session(raw_coder,'coder-session')
    reviewer=b.create_agent_profile(human,'Distinct reviewer',project_ids=[project['id']],
        policy=ordinary_policy(actions=['repo.read_file']))
    parent=b.create_ticket(human,project['id'],'Lead parent',assignee_owner_id='alice',
        assignee_agent_id=lead['id'],resource='repo:plan')
    config=tmp_path/'lead.json';fd=os.open(config,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(canonical(dict(mcp_url='http://localhost/mcp',token=device['token'],
        client='codex',identity_key='lead',name='Explicit team Lead')))
    plan={'schema':'team-ticket-plan-v1','owner_id':'alice','project_id':project['id'],
        'parent_ticket_id':parent['id'],'project_version':project['version'],'parent_version':parent['version'],
        'steps':[dict(step_id=key,slot_id=slot,title='Step '+key,description='Bounded synthetic work '+key,
            acceptance_criteria='The explicit selected result is verified.',depends_on=deps,milestone=milestone)
            for key,slot,deps,milestone in [('x','coder',['a','b'],'ready'),('a','reviewer',['z'],''),
                ('z','coder',[],''),('b','coder',[],'')]]}
    catalog={'schema':'team-ticket-catalog-v1','owner_id':'alice','slots':[
        dict(slot_id='coder',agent_id=coder['id'],profile_version=coder['version'],resource='repo:plan'),
        dict(slot_id='reviewer',agent_id=reviewer['id'],profile_version=reviewer['version'],resource='repo:plan')]}
    journal=tmp_path/'publication-journal'
    with TestClient(application,base_url='http://localhost') as api:
        wire=AsgiWire(api);client=TeamTicketBoardClient.from_connection_file(config,session_id='lead-session',timeout=10)
        client._opener.close();client._opener=wire
        yield SimpleNamespace(b=b,human=human,bob=bob,project=project,hidden=hidden,parent=parent,
            lead=lead,coder=coder,reviewer=reviewer,coder_actor=coder_actor,client=client,wire=wire,
            plan=plan,catalog=catalog,journal=journal)
        client.close()


def children(f):
    with f.b._connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM tickets WHERE id!=? ORDER BY number',(f.parent['id'],))]


def no_execution(f):
    with f.b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM delegation_families').fetchone()[0]==0
    assert {p['params']['name'] for p in f.wire.calls if p['method']=='tools/call'} <= {
        'register_agent','get_project','get_ticket','get_managed_agent_profile','create_ticket'}


def approval(f,controller=None):
    return (controller or TeamTicketPlanController(f.client)).preview(f.plan,f.catalog)['approval_sha256']


def test_preview_deterministic_on_real_registered_lead_presence_and_no_children(actual_plan):
    f=actual_plan;controller=TeamTicketPlanController(f.client,f.journal)
    first=controller.preview(f.plan,f.catalog)
    assert first['schema']=='team-ticket-preview-v1' and first['order']==['b','z','a','x']
    again=controller.preview(f.plan,f.catalog)
    reordered=deepcopy(f.plan);reordered['steps'].reverse()
    other_catalog=deepcopy(f.catalog);other_catalog['slots'].reverse()
    assert first==again==controller.preview(reordered,other_catalog)
    assert not f.journal.exists() and not children(f)
    assert all(effect['agent_id']!=f.lead['id'] for effect in first['effects'])
    no_execution(f)


def test_actual_mcp_publication_assigns_repeated_slots_and_topological_dependencies(actual_plan):
    f=actual_plan;controller=TeamTicketPlanController(f.client,f.journal);sha=approval(f,controller)
    def durable_before_rpc(_args):
        assert (f.journal/'intent.json').is_file() and (f.journal/'receipts.json').is_file()
        assert json.loads((f.journal/'intent.json').read_bytes())['approval_sha256']==sha
    f.wire.before_create=durable_before_rpc
    result=controller.apply(f.plan,f.catalog,approved_sha256=sha)
    assert result['schema']=='team-ticket-application-v1' and result['status']=='complete'
    receipts={row['step_id']:row for row in result['tickets']}
    assert list(receipts)==['b','z','a','x'] and len({r['ticket_id'] for r in receipts.values()})==4
    for step in f.plan['steps']:
        t=f.b.get_ticket(f.human,receipts[step['step_id']]['ticket_id'])['ticket']
        expected=f.reviewer['id'] if step['slot_id']=='reviewer' else f.coder['id']
        assert t['assignee_agent_id']==expected and t['assignee_owner_id']=='alice'
        assert t['status']=='todo' and t['claimant'] is None and t['resource']=='repo:plan'
        assert set(t['depends_on'])=={receipts[key]['ticket_id'] for key in step['depends_on']}
    last=f.b.get_ticket(f.human,receipts['x']['ticket_id'])['ticket']
    assert last['depends_on']==[receipts['b']['ticket_id'],receipts['a']['ticket_id']]
    assert 'milestone:ready' in last['labels']
    before=children(f);calls=len(f.wire.delivered)
    assert TeamTicketPlanController(f.client,f.journal).apply(f.plan,f.catalog,approved_sha256=sha)==result
    assert children(f)==before and len(f.wire.delivered)==calls
    no_execution(f)


def test_lost_creation_response_replays_exact_key_and_payload_without_duplicate(actual_plan):
    f=actual_plan;controller=TeamTicketPlanController(f.client,f.journal);sha=approval(f,controller)
    f.wire.lose_next_create=True
    with pytest.raises((BoardReadError,DevelopmentError)):
        controller.apply(f.plan,f.catalog,approved_sha256=sha)
    assert len(children(f))==len(f.wire.delivered)==1
    first=deepcopy(f.wire.delivered[0]);child_id=children(f)[0]['id']
    result=TeamTicketPlanController(f.client,f.journal).apply(f.plan,f.catalog,approved_sha256=sha)
    assert result['status']=='complete' and len(children(f))==4
    assert f.wire.delivered[1]==first
    assert result['tickets'][0]['ticket_id']==child_id
    assert len(f.wire.delivered)==5
    no_execution(f)


def test_interruption_after_confirmed_receipt_reopens_without_recreating_first_child(actual_plan):
    class PowerLoss(BaseException):pass
    f=actual_plan;controller=TeamTicketPlanController(f.client,f.journal);sha=approval(f,controller)
    def interrupt_before_second_create(_args):
        if len(f.wire.delivered)==1:raise PowerLoss()
    f.wire.before_create=interrupt_before_second_create
    with pytest.raises(PowerLoss):controller.apply(f.plan,f.catalog,approved_sha256=sha)
    assert len(children(f))==1
    first_id=children(f)[0]['id']
    recorded=json.loads((f.journal/'receipts.json').read_bytes())
    assert recorded['tickets']['b']['ticket_id']==first_id
    f.wire.before_create=lambda _args:None
    result=TeamTicketPlanController(f.client,f.journal).apply(f.plan,f.catalog,approved_sha256=sha)
    assert result['status']=='complete' and result['tickets'][0]['ticket_id']==first_id
    assert len(children(f))==len(f.wire.delivered)==4
    no_execution(f)


@pytest.mark.parametrize('ack_lost',[False,True])
def test_current_child_work_is_preserved_on_confirmed_or_uncertain_replay(actual_plan,ack_lost):
    f=actual_plan;controller=TeamTicketPlanController(f.client,f.journal);sha=approval(f,controller)
    if ack_lost:
        f.wire.lose_next_create=True
        with pytest.raises((BoardReadError,DevelopmentError)):
            controller.apply(f.plan,f.catalog,approved_sha256=sha)
        selected=children(f)[0]
    else:
        result=controller.apply(f.plan,f.catalog,approved_sha256=sha)
        selected=next(row for row in children(f) if row['id']==result['tickets'][0]['ticket_id'])
    claimed=f.b.claim_ticket(f.coder_actor,selected['id'],selected['version'])
    current=f.b.renew_ticket(f.coder_actor,selected['id'],claimed['version'],'Current child work must remain unchanged.')
    f.b.update_ticket(f.human,selected['id'],current['version'],title='Human refined this active task',
        description='Newer accepted work and prose must never be restored from the old plan.')
    before=children(f)
    refused=False
    try:
        resumed=controller.apply(f.plan,f.catalog,approved_sha256=sha)
        assert resumed['status']=='complete'
        if not ack_lost:assert resumed==result
    except (BoardReadError,DevelopmentError):
        assert ack_lost  # Only the unconfirmed response may remain unresolved.
        refused=True
    after=children(f)
    assert next(row for row in after if row['id']==selected['id'])==next(row for row in before if row['id']==selected['id'])
    if ack_lost:
        assert len(after)==(1 if refused else 4)
        assert len({row['id'] for row in after})==len(after)
    else:assert after==before
    no_execution(f)


@pytest.mark.parametrize('fault',['approved_hash','new_payload','target_scope','owner_revoked','parent_changed'])
def test_scope_or_approval_drift_refuses_before_any_child_mutation(actual_plan,fault):
    f=actual_plan;controller=TeamTicketPlanController(f.client,f.journal);sha=approval(f,controller)
    if fault=='approved_hash':sha='a'*64
    elif fault=='new_payload':f.plan['steps'][0]['description']='A different proposal.'
    elif fault=='target_scope':
        f.b.update_agent_profile(f.human,f.coder['id'],f.coder['version'],project_ids=[f.hidden['id']])
    elif fault=='owner_revoked':
        owner=f.b.get_settings(f.human)['policy']
        f.b.set_policy(f.human,**{k:owner[k] for k in ('enabled','allow_read','allow_reply','allow_requests','actions')},
            resources=[r for r in owner['resources'] if r!='agent-profiles:alice'])
    else:f.b.update_ticket(f.human,f.parent['id'],f.parent['version'],title='Changed parent source')
    with pytest.raises((BoardReadError,DevelopmentError)):
        controller.apply(f.plan,f.catalog,approved_sha256=sha)
    assert not children(f) and not f.wire.delivered
    no_execution(f)
