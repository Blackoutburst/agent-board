"""Replay/ACK protocol qualification with temporary boards; no model or service."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from board import Board, BoardError
from message_feed import MAX_READER_FEEDS, MAX_OWNER_FEEDS
from test_conversation_followups import space
from test_api import application, call_tool, tool_data, rpc


def rejects(status, fn, *args, **kwargs):
    with pytest.raises(BoardError) as error:
        fn(*args, **kwargs)
    assert error.value.status == status
    return str(error.value)


def ping(space, key='human', body='Human message'):
    b,h,o,t,reg,_,(actor,_),_=space
    return b.post_message(o,t['id'],body,mentions=['agent:'+reg['identity']['id']],idempotency_key=key)


def feed_row(b, feed):
    with b._connection() as db:
        return dict(db.execute('SELECT * FROM message_feeds WHERE id=?',(feed['feed_id'],)).fetchone())


def ack(b, actor, delivery):
    return b.checkpoint_message_feed(actor,delivery['feed_id'],delivery['delivery_id'],delivery['checkpoint'])


def test_legacy_initial_loss_is_reproduced_but_new_protocol_is_independent(space):
    b,*_=space;actor=space[6][0]
    message=ping(space)
    discarded=b.read_inbox(actor)
    assert [m['id'] for m in discarded['messages']]==[message['id']]
    assert b.read_inbox(actor)['messages']==[]  # Existing compatibility behavior.
    feed=b.open_message_feed(actor,'stable')
    assert [m['id'] for m in b.read_message_feed(actor,feed['feed_id'])['messages']]==[message['id']]


@pytest.mark.parametrize('start_at',['beginning','now'])
def test_lost_open_reply_reuses_original_boundary_after_restart(space,start_at):
    b,*_=space;actor=space[6][0]
    before=ping(space,'before')
    discarded=b.open_message_feed(actor,'lost-open',start_at)
    old=feed_row(b,discarded)
    later=ping(space,'after')
    b=Board(b.data_dir)
    opened=b.open_message_feed(actor,'lost-open',start_at)
    assert opened['feed_id']==discarded['feed_id']
    assert feed_row(b,opened)['initial_seq']==old['initial_seq']
    expected=[before['id'],later['id']] if start_at=='beginning' else [later['id']]
    assert [m['id'] for m in b.read_message_feed(actor,opened['feed_id'])['messages']]==expected
    for kwargs in ({'start_at':'now' if start_at=='beginning' else 'beginning'},{'limit':1}):
        rejects(409,b.open_message_feed,actor,'lost-open',**kwargs)
    assert feed_row(b,opened)['initial_seq']==old['initial_seq']


def test_lost_read_reply_stays_exact_when_new_messages_arrive_and_process_restarts(space):
    b,*_=space;actor=space[6][0]
    message=ping(space)
    feed=b.open_message_feed(actor,'lost-read')
    discarded=b.read_message_feed(actor,feed['feed_id'])
    state=feed_row(b,feed)
    later=ping(space,'later')
    b=Board(b.data_dir)
    replay=b.read_message_feed(actor,feed['feed_id'])
    assert replay['delivery_id']==discarded['delivery_id'] and replay['checkpoint']==0
    assert replay['messages']==discarded['messages']
    after=feed_row(b,feed)
    for key in ('initial_seq','confirmed_seq','pending_end_seq','pending_high_watermark','pending_ids'):
        assert after[key]==state[key]
    assert [m['id'] for m in replay['messages']]==[message['id']]
    ack(b,actor,replay)
    next_delivery=b.read_message_feed(actor,feed['feed_id'])
    assert next_delivery['delivery_id']!=replay['delivery_id']
    assert [m['id'] for m in next_delivery['messages']]==[later['id']]


def test_lost_checkpoint_reply_retries_once_without_skipping_new_pending(space):
    b,*_=space;actor=space[6][0]
    first=ping(space);feed=b.open_message_feed(actor,'ack-loss',limit=1)
    delivered=b.read_message_feed(actor,feed['feed_id'])
    discarded=ack(b,actor,delivered)
    later=ping(space,'later')
    b=Board(b.data_dir)
    assert ack(b,actor,delivered)==discarded
    pending=b.read_message_feed(actor,feed['feed_id'])
    assert [m['id'] for m in pending['messages']]==[later['id']]
    before=feed_row(b,feed)
    assert ack(b,actor,delivered)==discarded  # Old ACK never clears new pending.
    assert feed_row(b,feed)==before
    assert delivered['messages'][0]['id']==first['id']
    rejects(409,b.checkpoint_message_feed,actor,feed['feed_id'],pending['delivery_id'],0)
    rejects(409,b.checkpoint_message_feed,actor,feed['feed_id'],'f'*32,1)
    assert feed_row(b,feed)==before
    assert ack(b,actor,pending)['checkpoint']==2
    rejects(409,b.checkpoint_message_feed,actor,feed['feed_id'],delivered['delivery_id'],0)


def test_concurrent_open_read_and_checkpoint_share_one_exact_delivery(space):
    b,*_=space;actor=space[6][0]
    ping(space)
    with ThreadPoolExecutor(4) as pool:
        feeds=list(pool.map(lambda _:b.open_message_feed(actor,'parallel'),range(4)))
    assert len({f['feed_id'] for f in feeds})==1
    with ThreadPoolExecutor(4) as pool:
        batches=list(pool.map(lambda _:b.read_message_feed(actor,feeds[0]['feed_id']),range(4)))
    assert len({d['delivery_id'] for d in batches})==1
    assert all(d['messages']==batches[0]['messages'] for d in batches)
    with ThreadPoolExecutor(4) as pool:
        checkpoints=list(pool.map(lambda _:ack(b,actor,batches[0]),range(4)))
    assert checkpoints==[checkpoints[0]]*4 and checkpoints[0]['checkpoint']==1
    assert feed_row(b,feeds[0])['checkpoint']==1


def test_empty_batch_keeps_high_watermark_and_excludes_agent_loops(space):
    b,h,o,t,reg,_,(actor,other),_=space
    b.post_message(other,t['id'],'Bot ping',mentions=['agent:'+reg['identity']['id']],idempotency_key='bot')
    b.post_message(o,t['id'],'Not addressed',idempotency_key='noise')
    feed=b.open_message_feed(actor,'empty')
    empty=b.read_message_feed(actor,feed['feed_id'])
    state=feed_row(b,feed)
    assert empty['messages']==[]
    assert state['pending_end_seq']==state['pending_high_watermark']>state['confirmed_seq']==0
    later=ping(space,'later')
    assert b.read_message_feed(actor,feed['feed_id'])['messages']==[]
    assert feed_row(b,feed)['pending_high_watermark']==state['pending_high_watermark']
    ack(b,actor,empty)
    assert [m['id'] for m in b.read_message_feed(actor,feed['feed_id'])['messages']]==[later['id']]
    assert not any(k in empty for k in ('seq','high_watermark','pending_ids'))


def test_pagination_has_no_starvation_from_agent_or_forbidden_messages(space):
    b,h,o,t,reg,_,(actor,other),_=space
    wanted=[]
    for index in range(5):
        for sub in range(10):
            b.post_message(other,t['id'],'Bot',mentions=['agent:'+reg['identity']['id']],idempotency_key=f'bot-{index}-{sub}')
        wanted.append(ping(space,f'human-{index}')['id'])
    feed=b.open_message_feed(actor,'pages',limit=2)
    found=[]
    for _ in range(4):
        delivery=b.read_message_feed(actor,feed['feed_id'])
        found.extend(m['id'] for m in delivery['messages'])
        ack(b,actor,delivery)
    assert found==wanted
    state=feed_row(b,feed)
    with b._connection() as db:
        assert state['confirmed_seq']==db.execute('SELECT MAX(seq) FROM messages').fetchone()[0]


def test_replay_and_checkpoint_refuse_withdrawn_membership_without_partial_content(space):
    b,h,o,t,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'withdrawn')
    delivered=b.read_message_feed(actor,feed['feed_id'])
    with b._tx() as db:db.execute('DELETE FROM thread_members WHERE thread_id=? AND owner_id=?',(t['id'],h['owner_id']))
    before=feed_row(b,feed)
    error=rejects(403,b.read_message_feed,actor,feed['feed_id'])
    assert all(m['id'] not in error and m['body'] not in error for m in delivered['messages'])
    rejects(403,ack,b,actor,delivered)
    assert feed_row(b,feed)==before
    with b._tx() as db:db.execute('INSERT INTO thread_members VALUES(?,?)',(t['id'],h['owner_id']))
    assert b.read_message_feed(actor,feed['feed_id'])['delivery_id']==delivered['delivery_id']


def test_profile_project_scope_change_cannot_replay_or_checkpoint_hidden_content(space):
    b,h,o,t,reg,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'scope')
    delivered=b.read_message_feed(actor,feed['feed_id'])
    profile=b.get_agent_profile(h,reg['identity']['id'])
    b.update_agent_profile(h,profile['id'],profile['version'],project_ids=[])
    rejects(409,b.read_message_feed,actor,feed['feed_id'])
    raw=b.authenticate(space[7]['token'])
    b.register_agent(raw,'codex','Codex',[],'codex',False,'default')
    before=feed_row(b,feed)
    rejects(403,b.read_message_feed,actor,feed['feed_id'])
    rejects(403,ack,b,actor,delivered)
    assert feed_row(b,feed)==before


@pytest.mark.parametrize('change',['owner_read','owner_enabled','profile_pause','profile_read','device_revoke'])
def test_current_authorization_changes_block_read_and_ack_without_effect(space,change):
    b,h,o,t,reg,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'revoked')
    delivered=b.read_message_feed(actor,feed['feed_id'])
    if change.startswith('owner_'):
        b.set_policy(h,change!='owner_enabled',change!='owner_read',True,True,[],[])
    elif change=='device_revoke':
        b.revoke_device(h,space[7]['id'])
    else:
        profile=b.get_agent_profile(h,reg['identity']['id'])
        changes={'status':'paused'} if change=='profile_pause' else {'policy':{**profile['policy'],'allow_read':False}}
        b.update_agent_profile(h,profile['id'],profile['version'],**changes)
    before=feed_row(b,feed)
    for fn,args in ((b.open_message_feed,(actor,'revoked')),(b.read_message_feed,(actor,feed['feed_id'])),(ack,(b,actor,delivered))):
        with pytest.raises(BoardError):fn(*args)
    assert feed_row(b,feed)==before


def test_other_owners_devices_and_sessions_cannot_observe_feed_existence(space):
    b,h,o,t,*_=space;actor=space[6][0]
    feed=b.open_message_feed(actor,'private')
    unknown='f'*32
    raw=b.authenticate(space[7]['token'])
    b.register_agent(raw,'second-session','Second',[],'codex',False,'default')
    second=b.actor_session(raw,'second-session')
    foreign_device=b.create_device(o,'Foreign',[])
    foreign_raw=b.authenticate(foreign_device['token'])
    b.register_agent(foreign_raw,'foreign','Foreign',[],'codex',False,'default')
    foreign=b.actor_session(foreign_raw,'foreign')
    for other in (second,space[6][1],foreign):
        assert rejects(404,b.read_message_feed,other,feed['feed_id'])==rejects(404,b.read_message_feed,other,unknown)
        assert rejects(404,b.checkpoint_message_feed,other,feed['feed_id'],'e'*32,0)==rejects(404,b.checkpoint_message_feed,other,unknown,'e'*32,0)
    rejects(403,b.open_message_feed,o,'not-an-agent')
    assert feed_row(b,feed)['checkpoint']==0


def test_quotas_include_all_owner_sessions_profiles_devices(space):
    b,h,o,t,*_=space;actor=space[6][0]
    for index in range(MAX_READER_FEEDS):b.open_message_feed(actor,f'key-{index}')
    rejects(429,b.open_message_feed,actor,'overflow')
    first=b.open_message_feed(actor,'key-0')
    assert b.open_message_feed(actor,'key-0')['feed_id']==first['feed_id']
    rejects(429,b.open_message_feed,actor,'key-bypass')
    for device_no in range(7):
        device=b.create_device(h,f'Extra {device_no}',[])
        raw=b.authenticate(device['token'])
        session=f'extra-{device_no}'
        b.register_agent(raw,session,'Extra',[],'codex',False,'default')
        new_actor=b.actor_session(raw,session)
        for index in range(MAX_READER_FEEDS):b.open_message_feed(new_actor,f'key-{index}')
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM message_feeds').fetchone()[0]==MAX_OWNER_FEEDS
    device=b.create_device(h,'One more',[]);raw=b.authenticate(device['token'])
    b.register_agent(raw,'overflow','Extra',[],'codex',False,'default')
    rejects(429,b.open_message_feed,b.actor_session(raw,'overflow'),'owner-bypass')
    assert feed_row(b,first)['checkpoint']==0


def test_long_pause_has_no_ttl_and_preserves_initial_boundary_and_pending(space,monkeypatch):
    b,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'long-pause')
    delivered=b.read_message_feed(actor,feed['feed_id'])
    before=feed_row(b,feed)
    current=time.time()
    monkeypatch.setattr(time,'time',lambda:current+365*24*3600)
    b=Board(b.data_dir)
    assert b.open_message_feed(actor,'long-pause')==feed
    assert b.read_message_feed(actor,feed['feed_id'])==delivered
    assert feed_row(b,feed)==before
    assert ack(b,actor,delivered)['checkpoint']==1


def test_session_deletion_cascades_feed_and_opening_key(space):
    b,*_=space;actor=space[6][0]
    b.open_message_feed(actor,'deleted-session')
    with b._tx() as db:db.execute('DELETE FROM sessions WHERE id=?',(actor['session_id'],))
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM message_feeds').fetchone()[0]==0
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_no_work_model_notification_human_read_state_or_legacy_cursor_effects(space,monkeypatch):
    b,*_=space;actor=space[6][0]
    ping(space)
    def forbidden(*args,**kwargs):pytest.fail('Message feed must not enumerate or mutate work')
    for name in ('get_work','_expire','claim_next_ticket','claim_task','_queue_notification'):
        monkeypatch.setattr(b,name,forbidden)
    with b._connection() as db:
        initial={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in ('read_state','reader_state','cursors','notification_events','idempotency')}
    feed=b.open_message_feed(actor,'pure-feed')
    delivered=b.read_message_feed(actor,feed['feed_id'])
    ack(b,actor,delivered)
    with b._connection() as db:
        assert initial=={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in initial}


@pytest.mark.parametrize('kwargs',[{'idempotency_key':''},{'idempotency_key':False},{'idempotency_key':' x'},
    {'idempotency_key':'x\n'},{'idempotency_key':'x'*257},{'idempotency_key':'x','limit':True},
    {'idempotency_key':'x','limit':201},{'idempotency_key':'x','start_at':False},{'idempotency_key':'x','start_at':'future'}])
def test_invalid_open_arguments_create_no_feed(space,kwargs):
    b,*_=space;actor=space[6][0]
    rejects(400,b.open_message_feed,actor,**kwargs)
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM message_feeds').fetchone()[0]==0


@pytest.mark.parametrize('checkpoint',[False,-1,1.0,'0',None])
def test_invalid_checkpoint_never_changes_delivery(space,checkpoint):
    b,*_=space;actor=space[6][0]
    feed=b.open_message_feed(actor,'invalid-ack')
    delivered=b.read_message_feed(actor,feed['feed_id']);before=feed_row(b,feed)
    rejects(400,b.checkpoint_message_feed,actor,feed['feed_id'],delivered['delivery_id'],checkpoint)
    assert feed_row(b,feed)==before


def test_schema_eight_upgrade_preserves_messages_and_legacy_cursors(space):
    b,*_=space;actor=space[6][0]
    message=ping(space);inbox=b.read_inbox(actor)
    with b._tx() as db:
        db.execute('DROP TABLE message_feeds');db.execute('PRAGMA user_version=8')
    upgraded=Board(b.data_dir)
    assert upgraded.read_inbox(actor,cursor=inbox['cursor'])['messages']==[]
    feed=upgraded.open_message_feed(actor,'upgrade')
    assert [m['id'] for m in upgraded.read_message_feed(actor,feed['feed_id'])['messages']]==[message['id']]
    with upgraded._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]== 11
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_mcp_message_feed_tools_preserve_existing_inbox_schema(application):
    b=application.state.board
    h={'kind':'human','owner_id':'blackoutburst'}
    b.set_policy(h,True,True,True,True,[],[])
    device=b.create_device(h,'Feed',[]);raw=b.authenticate(device['token'])
    registered=b.register_agent(raw,'feed-sdk','Feed',[],'codex',False,'default')
    thread=b.create_thread(h,'Feed',['blackoutburst'],'feed')
    message=b.post_message(h,thread['id'],'Hello',mentions=['agent:'+registered['identity']['id']],idempotency_key='hello')
    with TestClient(application) as client:
        def call(name,**args):
            return tool_data(call_tool(client,device['token'],name,{'session_id':'feed-sdk',**args}))
        opened=call('open_message_feed',idempotency_key='sdk')
        assert call('open_message_feed',idempotency_key='sdk')['feed_id']==opened['feed_id']
        delivery=call('read_message_feed',feed_id=opened['feed_id'])
        assert [m['id'] for m in delivery['messages']]==[message['id']]
        assert call('checkpoint_message_feed',feed_id=opened['feed_id'],delivery_id=delivery['delivery_id'],expected_checkpoint=0)['checkpoint']==1
        legacy=call('read_inbox',label='Feed')
        assert set(legacy)=={'messages','cursor','session'}


def test_backup_restore_keeps_opening_and_pending_delivery_exact(space,tmp_path):
    from backup import create_backup, restore_backup
    b,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'backup-feed')
    delivery=b.read_message_feed(actor,feed['feed_id'])
    snapshot=feed_row(b,feed)
    archive=tmp_path/'feed-backup.zip'
    assert create_backup(b.data_dir,archive)['schema']['user_version']== 11
    destination=tmp_path/'restored-feed'
    restore_backup(archive,destination)
    restored=Board(destination)
    assert restored.open_message_feed(actor,'backup-feed')==feed
    assert restored.read_message_feed(actor,feed['feed_id'])==delivery
    assert feed_row(restored,feed)==snapshot
    assert ack(restored,actor,delivery)['checkpoint']==1
    assert restored.read_message_feed(actor,feed['feed_id'])['messages']==[]


def test_schema_nine_migration_failure_is_atomic(space,monkeypatch):
    import board as board_module
    b,*_=space
    ping(space)
    with b._tx() as db:
        db.execute('DROP TABLE message_feeds');db.execute('PRAGMA user_version=8')
    def dump():
        with sqlite3.connect(b.db_path) as db:return tuple(db.iterdump())
    before=dump();original=board_module.migrate_message_feed
    def failed(db):
        original(db)
        raise RuntimeError('synthetic message feed migration failure')
    monkeypatch.setattr(board_module,'migrate_message_feed',failed)
    with pytest.raises(RuntimeError,match='synthetic message feed migration failure'):Board(b.data_dir)
    assert dump()==before


@pytest.mark.parametrize('name,values',[
    ('open_message_feed',{'idempotency_key':'invalid','limit':True}),
    ('open_message_feed',{'idempotency_key':'invalid','limit':1.0}),
    ('open_message_feed',{'idempotency_key':'invalid','limit':'1'}),
    ('open_message_feed',{'idempotency_key':'invalid','start_at':False}),
    ('checkpoint_message_feed',{'expected_checkpoint':False}),
    ('checkpoint_message_feed',{'expected_checkpoint':0.0}),
    ('checkpoint_message_feed',{'expected_checkpoint':'0'}),
])
def test_mcp_strict_arguments_do_not_create_or_ack_delivery(application,name,values):
    b=application.state.board;h={'kind':'human','owner_id':'blackoutburst'}
    b.set_policy(h,True,True,True,True,[],[])
    device=b.create_device(h,'Strict feed',[]);raw=b.authenticate(device['token'])
    b.register_agent(raw,'strict-feed','Strict',[],'codex',False,'default')
    actor=b.actor_session(raw,'strict-feed')
    feed=b.open_message_feed(actor,'existing')
    delivery=b.read_message_feed(actor,feed['feed_id'])
    before=feed_row(b,feed)
    args={'session_id':'strict-feed',**values}
    if name=='checkpoint_message_feed':args.update(feed_id=feed['feed_id'],delivery_id=delivery['delivery_id'])
    with TestClient(application) as client:
        result=call_tool(client,device['token'],name,args)
        assert result.get('isError'),result
    assert feed_row(b,feed)==before
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM message_feeds').fetchone()[0]==1


def test_bounded_scan_checkpoints_hidden_headers_only_after_explicit_ack(space,monkeypatch):
    import message_feed
    b,h,o,t,reg,*_=space;actor=space[6][0]
    for index in range(message_feed.MAX_SCAN_ROWS+3):ping(space,f'hidden-{index}',body='Forbidden historical content')
    project=b.create_project(h,'Permitted',['blackoutburst','thescreamer'],idempotency_key='permitted')
    ticket=b.create_ticket(h,project['id'],'Visible',idempotency_key='visible')
    profile=b.get_agent_profile(h,reg['identity']['id'])
    b.update_agent_profile(h,profile['id'],profile['version'],project_ids=[project['id']])
    b.register_agent(b.authenticate(space[7]['token']),'codex','Codex',[],'codex',False,'default')
    wanted=b.post_message(o,ticket['thread_id'],'Visible final human message',mentions=['agent:'+reg['identity']['id']],idempotency_key='wanted')
    feed=b.open_message_feed(actor,'bounded-scan')
    original=b._profile_thread_allowed;checked=[]
    def check(db,who,thread):
        checked.append(thread)
        return original(db,who,thread)
    monkeypatch.setattr(b,'_profile_thread_allowed',check)
    delivery=b.read_message_feed(actor,feed['feed_id'])
    assert delivery['messages']==[] and len(checked)<=message_feed.MAX_SCAN_ROWS
    pending=feed_row(b,feed)
    assert pending['confirmed_seq']==0 and pending['pending_end_seq']==message_feed.MAX_SCAN_ROWS
    assert pending['pending_high_watermark']>pending['pending_end_seq']
    assert b.read_message_feed(actor,feed['feed_id'])==delivery
    assert feed_row(b,feed)['confirmed_seq']==0
    ack(b,actor,delivery)
    assert feed_row(b,feed)['confirmed_seq']==pending['pending_end_seq']
    checked.clear()
    next_delivery=b.read_message_feed(actor,feed['feed_id'])
    assert [m['id'] for m in next_delivery['messages']]==[wanted['id']]
    assert len(checked)<=message_feed.MAX_SCAN_ROWS+feed['limit']


def test_byte_budget_splits_before_undelivered_candidate_and_replay_is_exact(space,monkeypatch):
    import message_feed
    b,*_=space;actor=space[6][0]
    first=ping(space,'first',body='🧪'*100);second=ping(space,'second',body='🧪'*100)
    messages=b.get_thread(actor,space[3]['id'])['messages']
    single=max(len(json.dumps([m],ensure_ascii=False,separators=(',',':')).encode()) for m in messages)
    monkeypatch.setattr(message_feed,'MAX_MESSAGE_BYTES',single+10)
    feed=b.open_message_feed(actor,'bytes',limit=200)
    delivery=b.read_message_feed(actor,feed['feed_id'])
    assert [m['id'] for m in delivery['messages']]==[first['id']]
    assert len(json.dumps(delivery['messages'],ensure_ascii=False,separators=(',',':')).encode())<=single+10
    state=feed_row(b,feed)
    assert state['pending_end_seq']<state['pending_high_watermark']
    assert b.read_message_feed(actor,feed['feed_id'])==delivery
    ack(b,actor,delivery)
    assert [m['id'] for m in b.read_message_feed(actor,feed['feed_id'])['messages']]==[second['id']]


def test_individually_oversized_message_refuses_without_advancing_or_creating_pending(space,monkeypatch):
    import message_feed
    b,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'oversized');before=feed_row(b,feed)
    monkeypatch.setattr(message_feed,'MAX_MESSAGE_BYTES',10)
    rejects(413,b.read_message_feed,actor,feed['feed_id'])
    assert feed_row(b,feed)==before


def test_pending_current_metadata_growth_is_bounded_without_silent_ack(space,monkeypatch):
    import message_feed
    b,*_=space;actor=space[6][0]
    ping(space)
    feed=b.open_message_feed(actor,'metadata-growth')
    delivery=b.read_message_feed(actor,feed['feed_id']);before=feed_row(b,feed)
    original=b._message
    def expanded(db,row):
        return {**original(db,row),'current_metadata':'x'*message_feed.MAX_MESSAGE_BYTES}
    monkeypatch.setattr(b,'_message',expanded)
    rejects(413,b.read_message_feed,actor,feed['feed_id'])
    rejects(413,ack,b,actor,delivery)
    assert feed_row(b,feed)==before
    monkeypatch.setattr(b,'_message',original)
    assert b.read_message_feed(actor,feed['feed_id'])==delivery


def test_real_oversized_legacy_body_refuses_without_advancing(space):
    b,*_=space;actor=space[6][0]
    ping(space,body='🧪'*100000)  # Board legacy maximum, larger than the feed budget.
    feed=b.open_message_feed(actor,'legacy-oversized');before=feed_row(b,feed)
    rejects(413,b.read_message_feed,actor,feed['feed_id'])
    assert feed_row(b,feed)==before


def test_mcp_serialized_unicode_delivery_stays_under_two_mebibytes(application):
    from message_feed import MAX_MESSAGE_BYTES
    b=application.state.board;h={'kind':'human','owner_id':'blackoutburst'}
    b.set_policy(h,True,True,True,True,[],[])
    device=b.create_device(h,'Byte feed',[]);raw=b.authenticate(device['token'])
    reg=b.register_agent(raw,'byte-feed','Bytes',[],'codex',False,'default')
    actor=b.actor_session(raw,'byte-feed')
    thread=b.create_thread(h,'Bytes',['blackoutburst'],'bytes')
    messages=[b.post_message(h,thread['id'],'🧪'*20000,mentions=['agent:'+reg['identity']['id']],
                             idempotency_key=f'bytes-{i}') for i in range(8)]
    feed=b.open_message_feed(actor,'wire-bytes',limit=200)
    with TestClient(application) as client:
        response=rpc(client,device['token'],'tools/call',{'name':'read_message_feed',
            'arguments':{'session_id':'byte-feed','feed_id':feed['feed_id']}})
        assert response.status_code==200
        assert len(response.content)<2*1024*1024
        result=tool_data(response.json()['result'])
        assert 0<len(result['messages'])<len(messages)
        assert len(json.dumps(result['messages'],ensure_ascii=False,separators=(',',':')).encode())<=MAX_MESSAGE_BYTES
        assert [m['id'] for m in result['messages']]==[m['id'] for m in messages[:len(result['messages'])]]
        replay=rpc(client,device['token'],'tools/call',{'name':'read_message_feed',
            'arguments':{'session_id':'byte-feed','feed_id':feed['feed_id']}})
        assert replay.content==response.content


def test_committed_ack_retry_after_membership_withdrawal_returns_receipt_only(space):
    b,h,o,t,*_=space;actor=space[6][0]
    message=ping(space)
    feed=b.open_message_feed(actor,'committed-ack')
    delivered=b.read_message_feed(actor,feed['feed_id'])
    receipt=ack(b,actor,delivered)
    with b._tx() as db:db.execute('DELETE FROM thread_members WHERE thread_id=? AND owner_id=?',(t['id'],h['owner_id']))
    before=feed_row(b,feed)
    assert ack(b,actor,delivered)==receipt
    assert feed_row(b,feed)==before
    assert set(receipt)=={'feed_id','delivery_id','checkpoint','acknowledged'}
    assert message['id'] not in json.dumps(receipt) and message['body'] not in json.dumps(receipt)


def test_feed_foreign_keys_cascade_on_owning_record_deletion(space):
    b,*_=space
    with b._connection() as db:
        keys={row['table']:row['on_delete'] for row in db.execute('PRAGMA foreign_key_list(message_feeds)')}
    assert keys=={'owners':'CASCADE','devices':'CASCADE','sessions':'CASCADE','agent_profiles':'CASCADE'}


def test_missing_profile_is_a_fixed_authorization_error_not_a_type_error(space,monkeypatch):
    b,*_=space;actor=space[6][0]
    monkeypatch.setattr(b,'_profile_for_actor',lambda *_:None)
    rejects(403,b.open_message_feed,actor,'no-profile')
    with b._connection() as db:assert db.execute('SELECT COUNT(*) FROM message_feeds').fetchone()[0]==0
