import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import create_app
from board import Board,BoardError


@pytest.fixture
def space(tmp_path):
    b=Board(tmp_path/'data')
    humans=[{'kind':'human','owner_id':owner} for owner in ('blackoutburst','thescreamer')]
    for h in humans:
        b.ensure_owner(h['owner_id'],h['owner_id'].title())
        b.set_policy(h,True,True,True,True,[],[])
    human,other=humans
    device=b.create_device(human,'Mac',[])
    raw=b.authenticate(device['token'])
    codex=b.register_agent(raw,'codex','Codex',[],'codex',False,'default')
    claude_device=b.create_device(human,'Claude Mac',[])
    claude_raw=b.authenticate(claude_device['token'])
    claude=b.register_agent(claude_raw,'claude','Claude',[],'claude-code',False,'default')
    actors=[b.actor_session(raw,'codex'),b.actor_session(claude_raw,'claude')]
    thread=b.create_thread(human,'Shared',['blackoutburst','thescreamer'],'shared')
    return b,human,other,thread,codex,claude,actors,device


def test_agent_mentions_are_addressed_and_reply_notifies_original_agent(space):
    b,h,o,t,codex,claude,(ca,cl),_=space
    b.post_message(o,t['id'],'Unrelated announcement',idempotency_key='noise')
    target='agent:'+claude['identity']['id']
    message=b.post_message(ca,t['id'],'@'+claude['identity']['name']+' please acknowledge',mentions=[target],idempotency_key='ping')
    assert message['mentions']==[target] and message['mention_targets'][0]['name']==claude['identity']['name']
    received=b.read_inbox(cl)
    assert [m['id'] for m in received['messages']]==[message['id']]
    assert b.read_inbox(ca)['messages']==[]
    assert b.read_inbox(cl,cursor=received['cursor'])['messages']==[]
    reply=b.post_message(cl,t['id'],'Acknowledged',reply_to=message['id'],idempotency_key='reply')
    assert reply['mentions']==['agent:'+codex['identity']['id']]
    assert b.read_inbox(ca)['messages'][0]['id']==reply['id']
    assert b.inbox_snapshot(h)['messages']==[]
    assert b.post_message(ca,t['id'],message['body'],mentions=[target],idempotency_key='ping')['id']==message['id']


@pytest.mark.parametrize('autonomous', [False, True])
def test_message_only_inbox_skips_work_enumeration_and_keeps_delivery(space, monkeypatch, autonomous):
    b,h,o,t,codex,claude,(ca,cl),_=space
    p=b.create_project(h,'Large queue',['blackoutburst'],idempotency_key='large-queue')
    for index in range(25):
        b.create_ticket(h,p['id'],f'Ready {index}',description='Work context. '*200,idempotency_key=f'ready-{index}')
    raw=b.authenticate(b.create_device(h,'Monitor',[])['token'])
    monitor=b.register_agent(raw,'monitor','Monitor',[],'codex',autonomous,'default')
    actor=b.actor_session(raw,'monitor')
    target='agent:'+monitor['identity']['id']
    ping=b.post_message(ca,t['id'],'Please acknowledge',mentions=[target],idempotency_key='monitor-ping')
    def unexpected_work(*args, **kwargs):
        pytest.fail('Message-only polling must not enumerate or expire work')
    for method in ('get_work','_expire','_task'):
        monkeypatch.setattr(b,method,unexpected_work)
    inbox=b.read_inbox(actor)
    assert set(inbox)=={'messages','cursor','session'}
    assert [m['id'] for m in inbox['messages']]==[ping['id']]
    assert inbox['session']['online']
    assert len(json.dumps(inbox).encode())<4000
    empty=b.read_inbox(actor,cursor=inbox['cursor'])
    assert empty['messages']==[] and set(empty)==set(inbox)
    later=b.post_message(ca,t['id'],'One more',mentions=[target],idempotency_key='monitor-ping-2')
    assert b.read_inbox(actor,cursor=empty['cursor'])['messages'][0]['id']==later['id']


def test_human_inbox_filters_unread_mentions_and_assigned_work(space):
    b,h,o,t,codex,claude,(ca,cl),_=space
    p=b.create_project(h,'Project',['blackoutburst','thescreamer'],idempotency_key='p')
    b.create_ticket(h,p['id'],'Unassigned',idempotency_key='unassigned')
    b.create_ticket(h,p['id'],'Other owner',assignee_owner_id=o['owner_id'],idempotency_key='other')
    own=b.create_ticket(h,p['id'],'My task',assignee_owner_id=h['owner_id'],idempotency_key='mine')
    agent=b.create_ticket(h,p['id'],'My agent task',assignee_agent_id=codex['identity']['id'],idempotency_key='agent')
    b.post_message(o,t['id'],'Ordinary message')
    ping=b.post_message(o,t['id'],'@Blackoutburst can you check?')
    snapshot=b.inbox_snapshot(h)
    assert {ticket['id'] for ticket in snapshot['tickets']}=={own['id'],agent['id']}
    assert [m['id'] for m in snapshot['messages']]==[ping['id']]
    assert snapshot['count']==3
    b.mark_read(h,t['id'])
    assert b.inbox_snapshot(h)['messages']==[]
    assert b.inbox_snapshot(h)['count']==2


def test_long_agent_name_does_not_also_ping_a_human_prefix(space):
    b,h,o,t,codex,claude,(ca,cl),_=space
    device=b.create_device(h,'Helper',[])
    helper=b.register_agent(b.authenticate(device['token']),'helper','Helper',[],'agent',False,'default','Blackoutburst helper')
    posted=b.post_message(ca,t['id'],'@Blackoutburst helper please check')
    assert posted['mentions']==['agent:'+helper['identity']['id']]
    assert b.inbox_snapshot(h)['unread_count']==0


def test_mention_targets_do_not_leak_private_or_scoped_profiles(space):
    b,h,o,t,codex,claude,(ca,cl),_=space
    p=b.create_agent_profile(o,'Private profile',project_ids=[],idempotency_key='private')
    assert 'agent:'+p['id'] not in {item['id'] for item in b.list_mention_targets(ca,t['id'])}
    with pytest.raises(BoardError):b.post_message(ca,t['id'],'No access',mentions=['agent:'+p['id']])
    private=b.create_thread(h,'Private',['blackoutburst'],'private')
    with pytest.raises(BoardError):b.list_mention_targets(o,private['id'])


def test_reusable_labels_are_concurrent_and_survive_restart(space):
    b,h,o,t,*_=space
    p=b.create_project(h,'Tags',['blackoutburst','thescreamer'],idempotency_key='tags')
    assert {label['name'] for label in b.list_labels(h,p['id'])}=={'feature','bugfix'}
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:b.create_label(h,p['id'],'regression','#123456'),range(2)))
    assert results[0]==results[1]
    b=Board(b.data_dir)
    assert len([label for label in b.list_labels(o,p['id']) if label['name']=='regression'])==1


def test_mcp_file_transfer_and_safe_inline_previews(tmp_path):
    app=create_app(tmp_path/'web',base_url='http://testserver',filebrowser_url='',allow_local_passwords=True)
    b=app.state.board;h={'kind':'human','owner_id':'blackoutburst'}
    b.ensure_owner('blackoutburst','Blackoutburst','fixture-password')
    b.set_policy(h,True,True,True,True,[],[])
    device=b.create_device(h,'Mac',[])
    b.register_agent(b.authenticate(device['token']),'files','Files',[],'codex',False,'default')
    with TestClient(app) as client:
        headers={'Authorization':'Bearer '+device['token'],'Accept':'application/json, text/event-stream','X-Board-Session-Id':'files'}
        def call(name,**arguments):
            response=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{'session_id':'files',**arguments}}})
            assert response.status_code==200
            result=response.json()['result'];assert not result.get('isError'),result
            return result.get('structuredContent') or json.loads(result['content'][0]['text'])
        t=call('create_conversation',title='Files',participants=['blackoutburst'],idempotency_key='t')
        content=b'\x89PNG\r\n\x1a\nsynthetic'
        a=call('upload_attachment',thread_id=t['id'],filename='sample.png',content_type='image/png',content_base64=base64.b64encode(content).decode(),idempotency_key='image')
        assert call('upload_attachment',thread_id=t['id'],filename='sample.png',content_type='image/png',content_base64=base64.b64encode(content).decode(),idempotency_key='image')['id']==a['id']
        data=call('download_attachment',attachment_id=a['id'],limit=5)
        assert base64.b64decode(data['content_base64'])==content[:5] and not data['eof']
        assert client.get('/mcp/attachments/'+a['id'],headers=headers).content==content
        preview=client.get('/mcp/attachments/'+a['id']+'?inline=true',headers=headers)
        assert preview.headers['content-type']=='image/png' and preview.headers['content-disposition'].startswith('inline')
        unsafe=b.add_attachment(b.actor_session(b.authenticate(device['token']),'files'),t['id'],'unsafe.svg','image/svg+xml',b'<svg onload="alert(1)"/>')
        response=client.get('/mcp/attachments/'+unsafe['id']+'?inline=true',headers=headers)
        assert response.headers['content-type']=='application/octet-stream' and response.headers['content-disposition'].startswith('attachment')
        assert client.get('/mcp/attachments/'+a['id']).status_code==401
