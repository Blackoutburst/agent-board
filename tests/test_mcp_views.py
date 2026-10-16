"""Wire-size, loss/replay, text integrity and assignment delivery contracts."""
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from board import BoardError
from mcp_views import BUDGET, MCPViews, compact, size
from test_conversation_followups import space
from test_api import application, call_tool, tool_data


def project(space):
    b,h,*_=space
    return b.create_project(h,'Unicode project',['blackoutburst'],context='😀'*30000,idempotency_key='p')


def test_compact_inbox_replays_lost_initial_page_and_drains_without_gaps(space):
    b,h,o,t,reg,_,(actor,_),_=space
    identifiers=[]
    for i in range(27):
        item=b.post_message(o,t['id'],('😀\x00"\\'*25000)[:100000],
            mentions=['agent:'+reg['identity']['id']],idempotency_key='huge'+str(i))
        identifiers.append(item['id'])
    first=b.read_inbox(actor,limit=50,mcp_compact=True)
    assert set(first)=={'messages','cursor','session'}
    assert first['messages'] and len(first['messages'])<27
    assert 'body' not in first['messages'][0]
    retry=b.read_inbox(actor,limit=50,mcp_compact=True)
    assert [m['id'] for m in first['messages']]==[m['id'] for m in retry['messages']]
    received=[];page=first
    while page['messages']:
        assert size(page)<=BUDGET
        received.extend(m['id'] for m in page['messages'])
        page=b.read_inbox(actor,cursor=page['cursor'],limit=50,mcp_compact=True)
    assert received==identifiers
    assert b.read_inbox(actor,mcp_compact=True)['messages']==[]


def test_unicode_text_chunks_hash_edit_conflicts_and_scope(space):
    b,h,o,t,reg,_,(actor,_),_=space
    original=('😀👨‍👩‍👧‍👦é\u202e\x00"\\'*200)
    message=b.post_message(h,t['id'],original,idempotency_key='text')
    v=MCPViews(b);offset=0;parts=[];digest=hashlib.sha256(original.encode()).hexdigest()
    while True:
        chunk=v.get_text(actor,'message',message['id'],'body',offset,64,digest)
        assert chunk['sha256']==digest and size(chunk)<BUDGET
        assert len(json.dumps(chunk['text'],ensure_ascii=True,separators=(',',':')).encode())<=64
        parts.append(chunk['text'])
        if chunk['next_offset'] is None: break
        assert chunk['next_offset']>offset
        offset=chunk['next_offset']
    assert ''.join(parts)==original
    with pytest.raises(BoardError) as err:
        v.get_text(actor,'message',message['id'],'body',expected_sha256='0'*64)
    assert err.value.status==409
    private=b.create_project(h,'Private',['blackoutburst'],idempotency_key='private')
    ticket=b.create_ticket(h,private['id'],'Secret',description='private',idempotency_key='secret')
    with pytest.raises(BoardError) as err:
        v.get_text(o,'ticket',ticket['id'],'description')
    assert err.value.status==404
    with pytest.raises(BoardError):
        b.post_message(h,t['id'],'\ud800',idempotency_key='invalid-unicode')


def test_thread_pages_use_sequence_not_mutable_metadata(space):
    b,h,o,t,reg,_,(actor,_),_=space
    expected=[b.post_message(h,t['id'],'😀'*100000,idempotency_key=str(i))['id'] for i in range(12)]
    v=MCPViews(b);before=None;seen=[]
    while True:
        page=v.thread(actor,t['id'],before,50)
        assert size(page)<=BUDGET
        seen=[m['id'] for m in page['messages']]+seen
        if before is None:
            b.post_message(h,t['id'],'later',idempotency_key='later')
        before=page['next_before']
        if before is None: break
    assert seen==expected


def test_work_and_lists_page_every_group_and_bind_cursors_to_actor_query(space):
    b,h,o,t,reg,_,(actor,other),_=space
    p=project(space);v=MCPViews(b)
    expected=[]
    for i in range(31):
        expected.append(b.create_ticket(h,p['id'],'Work '+str(i),description='文'*20000,
            acceptance_criteria='😀'*20000,idempotency_key=str(i))['id'])
    cursor=None;seen=[]
    while True:
        source=b.get_work(actor,p['id'],5,_mcp_all=True)
        page=v.page(actor,'get_work',source,limit=5,cursor=cursor,filters={'project_id':p['id']})
        assert size(page)<=BUDGET and len(page['ready'])<=5
        seen.extend(x['id'] for x in page['ready'])
        cursor=page['next_cursor']
        if not cursor: break
        with pytest.raises(BoardError):
            v.page(other,'get_work',source,limit=5,cursor=cursor,filters={'project_id':p['id']})
        with pytest.raises(BoardError):
            v.page(actor,'get_work',source,limit=5,cursor=cursor,filters={'project_id':'other'})
    assert sorted(seen)==sorted(expected) and len(seen)==len(set(seen))
    rows=b.list_tickets(actor,p['id'])
    first=v.page(actor,'list_tickets',rows,limit=2)
    changed=next(x for x in rows if x['id']==first['items'][0]['id'])
    b.update_ticket(h,changed['id'],changed['version'],title='Updated without moving its ID')
    rest=[];cursor=first['next_cursor']
    while cursor:
        page=v.page(actor,'list_tickets',b.list_tickets(actor,p['id']),limit=2,cursor=cursor)
        rest.extend(x['id'] for x in page['items']);cursor=page['next_cursor']
    assert len(set(rest+ [x['id'] for x in first['items']]))==31


def test_assignment_notices_are_transactional_deduplicated_and_addressed(space):
    b,h,o,t,reg,reg2,(actor,other),_=space
    p=project(space)
    args=dict(assignee_agent_id=reg['identity']['id'],idempotency_key='assigned')
    ticket=b.create_ticket(h,p['id'],'Assignment',**args)
    assert b.create_ticket(h,p['id'],'Assignment',**args)['id']==ticket['id']
    one=b.read_inbox(actor,mcp_compact=True)
    assert len(one['messages'])==1 and one['messages'][0]['thread_id']==ticket['thread_id']
    changed=b.update_ticket(h,ticket['id'],ticket['version'],assignee_agent_id=reg2['identity']['id'])
    with pytest.raises(BoardError):
        b.update_ticket(h,ticket['id'],ticket['version'],assignee_agent_id=reg2['identity']['id'])
    back=b.update_ticket(h,ticket['id'],changed['version'],assignee_agent_id=reg['identity']['id'])
    with b._connection() as db:
        notices=db.execute('SELECT * FROM messages WHERE thread_id=?',(ticket['thread_id'],)).fetchall()
        assert len(notices)==5
        assert db.execute("SELECT COUNT(*) FROM ticket_events WHERE ticket_id=? AND event='ticket_message_posted'",(ticket['id'],)).fetchone()[0]==0
    inbox=b.read_inbox(actor,cursor=one['cursor'],mcp_compact=True)
    assert len(inbox['messages'])==2
    b.update_ticket(h,ticket['id'],back['version'],assignee_owner_id=h['owner_id'])
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM messages WHERE thread_id=?',(ticket['thread_id'],)).fetchone()[0]==5


def test_compact_feed_replays_and_checks_large_original_without_losing_ack(space):
    b,h,o,t,reg,_,(actor,_),_=space
    target='agent:'+reg['identity']['id']
    ids=[b.post_message(o,t['id'],'😀'*100000,mentions=[target],idempotency_key='f'+str(i))['id'] for i in range(18)]
    f=b.open_message_feed(actor,'compact-feed')
    seen=[]
    while True:
        d=b.read_message_feed(actor,f['feed_id'],mcp_compact=True)
        assert size(d)<=BUDGET
        assert b.read_message_feed(actor,f['feed_id'],mcp_compact=True)==d
        seen.extend(m['id'] for m in d['messages'])
        b.checkpoint_message_feed(actor,f['feed_id'],d['delivery_id'],d['checkpoint'])
        if not d['messages']: break
    assert seen==ids


def test_real_mcp_wire_budget_and_full_detail_opt_in(application):
    b=application.state.board;h={'kind':'human','owner_id':'blackoutburst'}
    b.set_policy(h,True,True,True,True,[],[])
    dev=b.create_device(h,'Wire',[]);raw=b.authenticate(dev['token'])
    reg=b.register_agent(raw,'wire','Wire',[],'codex',True)
    p=b.create_project(h,'Wire',['blackoutburst'],context='😀'*30000,idempotency_key='p')
    tickets=[b.create_ticket(h,p['id'],'Ticket '+str(i),description='文'*20000,
            acceptance_criteria='😀'*20000,idempotency_key=str(i)) for i in range(22)]
    t=tickets[0]
    question=b.request_human_input(h,t['id'],h['owner_id'],'Confirm source','wire-question')
    b.resolve_human_request(h,question['id'],'Confirmed')
    b.post_message(h,t['thread_id'],'😀'*100000,mentions=['agent:'+reg['identity']['id']],idempotency_key='wire')
    with TestClient(application) as client:
        wire_sizes={}
        def call(name,**args):
            result=call_tool(client,dev['token'],name,{'session_id':'wire',**args})
            assert not result.get('isError'),result
            measured=len(json.dumps(result,ensure_ascii=True,separators=(',',':')).encode())
            wire_sizes[name]=max(wire_sizes.get(name,0),measured)
            assert measured<20000,(name,measured)
            return tool_data(result)
        inbox=call('read_inbox',label='Wire')
        assert set(inbox)=={'messages','cursor','session'} and 'body' not in inbox['messages'][0]
        detail=call('get_ticket',ticket_id=t['id'])['ticket']
        assert 'description' not in detail
        assert detail['human_requests'][0]['status']=='resolved'
        assert detail['human_requests'][0]['answer']=='Confirmed'
        full=tool_data(call_tool(client,dev['token'],'get_ticket',{'session_id':'wire','ticket_id':t['id'],'view':'full'}))
        assert full['ticket']['description']==t['description']
        calls={'get_project':{'project_id':p['id']},'list_projects':{},'list_tickets':{'project_id':p['id']},
               'get_thread':{'thread_id':t['thread_id']},'get_work':{'limit':200},'list_activity':{'project_id':p['id']},
               'list_conversations':{},'list_agents':{},'list_labels':{'project_id':p['id']},
               'list_mention_targets':{'thread_id':t['thread_id']},'list_human_requests':{}}
        for name,args in calls.items(): call(name,**args)
        call('get_message',thread_id=t['thread_id'],message_id=inbox['messages'][0]['id'])
        chunk=call('get_text',kind='message',identifier=inbox['messages'][0]['id'],field='body')
        assert chunk['next_offset']>0
        claimed=call('claim_next_ticket',project_id=p['id'],idempotency_key='claim')
        task=claimed['ticket']
        updated=call('renew_ticket',ticket_id=task['id'],expected_version=task['version'],progress='😀'*20000)
        assert updated['version']==task['version']+1
        attachment=b.add_attachment(h,t['thread_id'],'large.bin','application/octet-stream',b'x'*9000,'blob')
        call('download_attachment',attachment_id=attachment['id'])
        if os.environ.get('BOARD_MCP_METRICS'):
            Path(os.environ['BOARD_MCP_METRICS']).write_text(json.dumps({
                'fixture':'isolated synthetic Unicode/long-body data','budget_bytes':20000,
                'maximum_result_bytes':max(wire_sizes.values()),'tool_result_bytes':wire_sizes},indent=2)+'\n')


def test_context_adapter_seals_full_source_through_real_mcp(application):
    sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/codex-personal'))
    from blackoutburst_agents.board_reader import BoardReader
    from blackoutburst_agents.context import ContextScope, build_context, ContextError
    from blackoutburst_agents.event_client import EventBoardClient
    b=application.state.board;h={'kind':'human','owner_id':'blackoutburst'}
    b.set_policy(h,True,True,True,True,[],[])
    dev=b.create_device(h,'Context',[]);raw=b.authenticate(dev['token'])
    reg=b.register_agent(raw,'context','Context',[],'codex',False)
    p=b.create_project(h,'Context',['blackoutburst'],context='Source context',idempotency_key='p')
    t=b.create_ticket(h,p['id'],'Source',description='Original description',idempotency_key='t')
    m=b.post_message(h,t['thread_id'],'Original body',idempotency_key='m')
    scope=ContextScope(h['owner_id'],reg['identity']['id'],p['id'],t['id'])
    with TestClient(application) as client:
        def compact_call(name,args):
            return tool_data(call_tool(client,dev['token'],name,{'session_id':'context',**args}))
        with pytest.raises(ContextError):
            build_context(compact_call,scope=scope,registration=reg)
        reader=object.__new__(BoardReader)
        reader._rpc=lambda method,params:call_tool(client,dev['token'],params['name'],params['arguments'])
        artifact=build_context(lambda name,args:reader._tool(name,{'session_id':'context',**args}),scope=scope,registration=reg)
        events=object.__new__(EventBoardClient)
        events._registered=True;events.session_id='context';events._rpc=reader._rpc
        activity=events.call('list_activity',{'project_id':p['id'],'ticket_id':t['id'],'after':0,'limit':50,'order':'asc'})
        assert activity==b.list_activity(h,p['id'],t['id'],0,50,'asc')
    assert artifact['data']['ticket']['description']=='Original description'
    assert artifact['data']['messages'][0]['id']==m['id']
    assert artifact['data']['messages'][0]['body']=='Original body'


def test_activity_pages_honor_direction_and_do_not_move_on_ticket_edits(space):
    b,h,_,_,_,_,(actor,_),_=space
    p=project(space);t=b.create_ticket(h,p['id'],'Activity',idempotency_key='task')
    for i in range(12):
        t=b.update_ticket(h,t['id'],t['version'],description=str(i))
    v=MCPViews(b)
    for order in ['asc','desc']:
        cursor=None;seen=[]
        while True:
            rows=b.list_activity(actor,p['id'],limit=2,order=order,_mcp_all=True)
            page=v.page(actor,'list_activity',rows,limit=2,cursor=cursor,filters={'order':order})
            seen.extend(x['id'] for x in page['items']);cursor=page['next_cursor']
            if not cursor:break
        assert seen==sorted(set(seen),reverse=order=='desc') and len(seen)==13


def test_new_message_and_development_adapters_keep_full_source_after_validation(application):
    sys.path.insert(0,str(Path(__file__).parents[1]/'integrations/codex-personal'))
    from blackoutburst_agents.message_client import MessageBoardClient
    from blackoutburst_agents.development_client import DevelopmentBoardClient
    from blackoutburst_agents.board_reader import BoardReadError
    b=application.state.board;h={'kind':'human','owner_id':'blackoutburst'}
    b.set_policy(h,True,True,True,True,[],[])
    device=b.create_device(h,'Source bridge',[]);raw=b.authenticate(device['token'])
    reg=b.register_agent(raw,'source-bridge','Source bridge',[],'codex',False)
    p=b.create_project(h,'Source bridge',['blackoutburst'],idempotency_key='bridge-p')
    t=b.create_ticket(h,p['id'],'Source',description='Exact development description',idempotency_key='bridge-t')
    message=b.post_message(h,t['thread_id'],'Exact human source😀'*100,
        mentions=['agent:'+reg['identity']['id']],idempotency_key='bridge-m')
    with TestClient(application) as client:
        calls=[]
        def rpc(method,params):
            calls.append(params)
            return call_tool(client,device['token'],params['name'],params['arguments'])
        channel=object.__new__(MessageBoardClient);channel._registered=True;channel.session_id='source-bridge';channel._rpc=rpc
        source=channel.call('get_message',{'thread_id':t['thread_id'],'message_id':message['id']})
        assert source==b.get_message(b.actor_session(raw,'source-bridge'),t['thread_id'],message['id'])
        feed=channel.call('open_message_feed',{'idempotency_key':'bridge-feed','start_at':'beginning','limit':50})
        page=channel.call('read_message_feed',{'feed_id':feed['feed_id']})
        assert page['messages'][0]['body']==message['body']
        development=object.__new__(DevelopmentBoardClient);development._registered=True;development.session_id='source-bridge';development._rpc=rpc;development._initialize=lambda:None
        assert development.call('get_ticket',{'ticket_id':t['id']})['ticket']['description']=='Exact development description'
        assert development.call('get_work',{'project_id':p['id'],'limit':1})['ready'][0]['description']=='Exact development description'
        before=len(calls)
        with pytest.raises(BoardReadError):channel.call('get_message',{'thread_id':t['thread_id'],'message_id':message['id'],'view':'full'})
        assert len(calls)==before
        assert all(c['arguments']['view']=='full' for c in calls)
