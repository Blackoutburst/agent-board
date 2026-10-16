"""Independent-review reproducers, exercised through the bounded read layer."""
import hashlib
import json
import time

import pytest
from board import BoardError
from mcp_views import BUDGET, MCPViews, compact, size, wire
from test_conversation_followups import space


def ticket(space):
    b,h,*_=space
    p=b.create_project(h,'Review',['blackoutburst'],idempotency_key='review-project')
    return b.create_ticket(h,p['id'],'Ticket',description='😀'*20000,idempotency_key='review-ticket')


def test_ticket_pages_keep_all_human_controls_and_dependencies(space):
    b,h,o,t,reg,_,(actor,_),_=space
    task=ticket(space)
    deps=[b.create_ticket(h,task['project_id'],'Dependency'+str(i),idempotency_key='d'+str(i))['id'] for i in range(15)]
    task=b.update_ticket(h,task['id'],task['version'],depends_on=deps)
    with b._tx() as db:
        for i in range(25):
            db.execute('''INSERT INTO human_requests(id,ticket_id,owner_id,question,kind,requested_by,status,answer,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)''',(f'{i:032x}',task['id'],h['owner_id'],'Q'+str(i),'question',json.dumps(h),'resolved' if i%2 else 'open','answer'+str(i),time.time()+i))
    v=MCPViews(b);cursor=None;requests=[];dependencies=[]
    while True:
        page=v.ticket(actor,task['id'],limit=5,cursor=cursor)
        assert size(page)<=BUDGET
        assert page['ticket']['version']==task['version']
        assert 'human_requests' in page['ticket'] and page['totals']['human_requests']==25
        requests.extend(page['ticket']['human_requests']);dependencies.extend(page['ticket']['depends_on'])
        cursor=page['next_cursor']
        if not cursor:break
    assert len(requests)==25 and len({r['id'] for r in requests})==25
    assert {r['answer'] for r in requests}=={'answer'+str(i) for i in range(25)}
    assert set(dependencies)==set(deps)


def test_authoritative_priority_recency_and_total_limit(space):
    b,h,o,t,reg,_,(actor,_),_=space
    task=ticket(space)
    for i in range(8):b.create_ticket(h,task['project_id'],'Low'+str(i),priority='low',idempotency_key='low'+str(i))
    urgent=b.create_ticket(h,task['project_id'],'Urgent',priority='urgent',idempotency_key='urgent')
    v=MCPViews(b);source=b.get_work(actor,task['project_id'],3,_mcp_all=True)
    first=v.page(actor,'get_work',source,limit=3)
    assert first['ready'][0]['id']==urgent['id']
    assert sum(len(first[k]) for k in ('ready','assigned','waiting','recovery','human_requests'))<=3
    rows=b.list_tickets(actor,task['project_id'])
    assert [x['id'] for x in v.page(actor,'list_tickets',rows,limit=3)['items']]==[x['id'] for x in rows[:3]]
    # A changed ordering is a visible restart, never an unnoticed skipped row.
    with pytest.raises(BoardError) as error:v.page(actor,'get_work',{**source,'ready':list(reversed(source['ready']))},limit=3,cursor=first['next_cursor'])
    assert error.value.status==409


def test_large_metadata_cannot_poison_inbox_thread_or_confirmed_feed(space):
    b,h,o,t,reg,_,(actor,_),_=space
    attachments=[b.add_attachment(h,t['id'],str(i)+'😀'*190+'.txt','text/plain',b'fixture','a'+str(i))['id'] for i in range(25)]
    message=b.post_message(o,t['id'],'Body😀'*1000,mentions=['agent:'+reg['identity']['id']],attachment_ids=attachments,idempotency_key='metadata')
    v=MCPViews(b)
    inbox=b.read_inbox(actor,mcp_compact=True)
    assert inbox['messages'][0]['id']==message['id'] and size(inbox)<=BUDGET
    item=inbox['messages'][0]
    assert 'attachments' not in item and item['attachments_count']==25
    assert v.thread(actor,t['id'])['messages'][0]['id']==message['id']
    feed=b.open_message_feed(actor,'metadata-feed');page=b.read_message_feed(actor,feed['feed_id'],mcp_compact=True)
    assert page['messages'][0]['id']==message['id']
    assert b.checkpoint_message_feed(actor,feed['feed_id'],page['delivery_id'],0)['checkpoint']==1
    offset=0;parts=[];digest=''
    while True:
        chunk=v.get_text(actor,'message',message['id'],'/attachments',offset,512,digest)
        digest=chunk['sha256'];parts.append(chunk['text']);offset=chunk['next_offset']
        if offset is None:break
    assert json.loads(''.join(parts))==message['attachments']
    assert b.read_inbox(actor,cursor=inbox['cursor'],mcp_compact=True)['messages']==[]


def test_nested_text_locator_and_unicode_thread_title_are_retrievable(space):
    b,h,o,t,reg,_,(actor,_),_=space
    # Direct DB fixture exceeds ordinary UI limits to exercise legacy metadata.
    with b._tx() as db:db.execute('UPDATE threads SET title=? WHERE id=?',('😀'*200,t['id']))
    v=MCPViews(b);item=v.thread(actor,t['id'])['thread'];locator=item['text_fields']['title']['get_text']
    parts=[];offset=0;digest=item['text_fields']['title']['sha256']
    while True:
        chunk=v.get_text(actor,**locator,offset=offset,limit=256,expected_sha256=digest)
        parts.append(chunk['text']);offset=chunk['next_offset']
        if offset is None:break
    assert ''.join(parts)=='😀'*200
    message=b.post_message(h,t['id'],'text',idempotency_key='nested')
    with b._tx() as db:
        source=json.loads(db.execute('SELECT author FROM messages WHERE id=?',(message['id'],)).fetchone()[0]);source['session_label']='😀'*200
        db.execute('UPDATE messages SET author=? WHERE id=?',(json.dumps(source),message['id']))
    item=v.thread(actor,t['id'])['messages'][0]
    info=item['author']['text_fields']['session_label']
    assert info['get_text']['field']=='/author/session_label'
    chunk=v.get_text(actor,**info['get_text'],limit=4096,expected_sha256=info['sha256'])
    assert chunk['text']=='😀'*200
    with pytest.raises(BoardError):v.get_text(o,'thread',ticket(space)['thread_id'],'title')


def test_include_work_is_explicit_and_compact_ack_mode_and_mixed_polling(space):
    b,h,o,t,reg,_,(actor,_),_=space
    msg=b.post_message(o,t['id'],'Please read',mentions=['agent:'+reg['identity']['id']],idempotency_key='cursor')
    first=b.read_inbox(actor,mcp_compact=True,include_work=True)
    assert 'tasks' not in first and first['work']['embedded'] is False
    assert b.read_inbox(actor,mcp_compact=True)['messages'][0]['id']==msg['id']
    assert b.read_inbox(actor,cursor=first['cursor'],mcp_compact=True)['messages']==[]
    assert b.read_inbox(actor)['messages']==[]


def test_page_cursor_expiry_and_retention_are_bounded(space,monkeypatch):
    import mcp_views
    b,h,o,t,reg,_,(actor,_),_=space
    v=MCPViews(b);values=[{'id':str(i),'name':str(i)} for i in range(3)]
    first=v.page(actor,'test',values,limit=1)
    for _ in range(140):v.page(actor,'test',values,limit=1)
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM mcp_page_cursors').fetchone()[0]==128
    fresh=v.page(actor,'test',values,limit=1)
    now=time.time();monkeypatch.setattr(mcp_views.time,'time',lambda:now+3601)
    with pytest.raises(BoardError):v.page(actor,'test',values,limit=1,cursor=fresh['next_cursor'])
    # Failed reads roll back their transaction; the next successful read
    # performs opportunistic TTL cleanup without creating another cursor.
    v.page(actor,'test',values,limit=3)
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM mcp_page_cursors').fetchone()[0]==0


def test_assignment_with_title_change_uses_current_title(space):
    b,h,o,t,reg,_,(actor,_),_=space
    task=ticket(space)
    b.update_ticket(h,task['id'],task['version'],title='Current title',assignee_agent_id=reg['identity']['id'])
    assert 'Current title' in b.read_inbox(actor)['messages'][0]['body']


def test_pending_compact_feed_replay_survives_display_rename(space):
    b,h,o,t,reg,_,(actor,_),_=space
    for i in range(8):
        b.post_message(o,t['id'],'Read'+str(i),mentions=['agent:'+reg['identity']['id'],'blackoutburst'],idempotency_key='rename'+str(i))
    feed=b.open_message_feed(actor,'rename-feed')
    first=b.read_message_feed(actor,feed['feed_id'],mcp_compact=True)
    b.ensure_owner('blackoutburst','😀'*120)
    replay=b.read_message_feed(actor,feed['feed_id'],mcp_compact=True)
    assert replay==first
    assert b.checkpoint_message_feed(actor,feed['feed_id'],first['delivery_id'],0)['checkpoint']==1
