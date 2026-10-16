"""Exact addressed-message admission, shared capacity and schema10 durability."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from board import Board, BoardError
from backup import create_backup, restore_backup
from test_conversation_followups import space
from test_runtime import runtime, reserve as ticket_reserve


def reject(code,fn,*args,**kwargs):
    with pytest.raises(BoardError) as error:fn(*args,**kwargs)
    assert error.value.status == code
    return str(error.value)


def ping(space,key='source',actor=None,mentions=None):
    b,h,o,t,reg,_,actors,_=space
    return b.post_message(actor or o,t['id'],'Human source 🧪',
        mentions=mentions if mentions is not None else ['agent:'+reg['identity']['id']],idempotency_key=key)


def reserve(space,message,key='message-turn',actor=None,**changes):
    b,*_=space;actor=actor or space[6][0]
    args=dict(idempotency_key=key,thread_id=message['thread_id'],message_id=message['id'],
        source_sha256=b.get_message(actor,message['thread_id'],message['id'])['source_sha256'],
        work_id='work-'+key,turn_id='turn-'+key,provider='codex_chatgpt',model='fixture-model')
    args.update(changes)
    return b.reserve_message_execution(actor,**args)


def test_general_message_roundtrip_needs_reply_not_request_permission_and_creates_no_work(space):
    b,h,_,_,_,_,(actor,_),_=space
    b.set_policy(h,True,True,True,False,[],[])
    message=ping(space)
    with b._connection() as db:
        before={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('projects','tickets','tasks')}
    first=reserve(space,message)
    row=first['execution']
    assert not first['retried'] and row['scope_kind']=='message'
    assert row['project_id'] is None and row['ticket_id'] is None
    assert (row['thread_id'],row['message_id'])==(message['thread_id'],message['id'])
    assert reserve(space,message)['execution']==row and reserve(space,message)['retried']
    live=b.start_execution(actor,row['id'],1)
    assert b.start_execution(actor,row['id'],1)==live
    heartbeat=b.heartbeat_execution(actor,row['id'],live['revision'])
    done=b.finish_execution(actor,row['id'],heartbeat['revision'],'completed',True,'completed')
    assert done['state']=='finished' and b.get_runtime(h)['active_count']==0
    assert b.finish_execution(actor,row['id'],heartbeat['revision'],'completed',True,'completed')==done
    with b._connection() as db:
        assert before=={table:db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in before}
    assert not any(name in json.dumps(done) for name in ('Human source','source_body','payload','idempotency_key'))


def test_hash_uses_raw_canonical_source_and_excludes_dynamic_display_metadata(space):
    b,h,_,t,reg,_,(actor,_),_=space
    message=ping(space)
    with b._connection() as db:
        row=db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone()
        source={key:row[key] for key in ('id','thread_id','body','created_at','reply_to')}
        source.update(author=json.loads(row['author']),mentions=json.loads(row['mentions']),attachment_ids=json.loads(row['attachments']))
    expected=hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    before=b.get_message(actor,t['id'],message['id'])
    assert before['source_sha256']==expected
    with b._tx() as db:db.execute('UPDATE owners SET display_name=? WHERE id=?',('Renamed display',h['owner_id']))
    assert b.get_message(actor,t['id'],message['id'])['source_sha256']==expected
    with b._tx() as db:db.execute('UPDATE messages SET attachments=? WHERE id=?',(json.dumps(['synthetic-attachment-id']),message['id']))
    assert b.get_message(actor,t['id'],message['id'])['source_sha256']!=expected


@pytest.mark.parametrize('change',['body','author','reply_to','mentions','attachments','created_at'])
def test_source_change_blocks_start_and_requests_stop_but_cleanup_still_works(space,change):
    b,_,_,_,_,_,(actor,_),_=space
    message=ping(space);execution=reserve(space,message)['execution']
    values={'body':'Edited source','author':json.dumps({'kind':'human','owner_id':'blackoutburst','display_name':'changed'}),
        'reply_to':'different-message','mentions':json.dumps([]),'attachments':json.dumps(['different-attachment']),
        'created_at':message['created_at']+1}
    with b._tx() as db:db.execute('UPDATE messages SET '+change+'=? WHERE id=?',(values[change],message['id']))
    reject(409 if change!='mentions' else 403,b.start_execution,actor,execution['id'],1)
    control=b.get_execution_control(actor,execution['id'])
    assert control['should_stop'] and control['control_reason']=='authorization_changed'
    assert b.finish_execution(actor,execution['id'],1,'cancelled',True,'policy_changed')['state']=='finished'


@pytest.mark.parametrize('which',['agent','unaddressed','wrong_agent','owner_only'])
def test_only_human_messages_addressed_by_actual_inbox_targets_are_admitted(space,which):
    b,h,_,_,_,other_reg,(actor,other_actor),_=space
    message=ping(space,actor=other_actor if which=='agent' else None,
        mentions=[] if which=='unaddressed' else ['agent:'+other_reg['identity']['id']] if which=='wrong_agent'
        else [h['owner_id']] if which=='owner_only' else None)
    reject(403,reserve,space,message)
    assert b.get_runtime(h)['active_count']==0


def test_message_source_in_ticket_conversation_needs_no_claim_and_grants_no_ticket_work(space):
    b,h,_,_,reg,_,(actor,_),_=space
    project=b.create_project(h,'Related project',[h['owner_id']],'related-project')
    ticket=b.create_ticket(h,project['id'],'Human question',idempotency_key='question-ticket')
    message=b.post_message(h,ticket['thread_id'],'Question only',mentions=['agent:'+reg['identity']['id']],idempotency_key='question')
    b.set_policy(h,True,True,True,False,[],[])
    result=reserve(space,message)['execution']
    assert result['project_id']==project['id'] and result['ticket_id']==ticket['id']
    assert b.get_ticket(h,ticket['id'])['ticket']['claimant'] is None
    reject(403,b.claim_ticket,actor,ticket['id'],ticket['version'])
    assert b.start_execution(actor,result['id'],1)['state']=='running'


def test_member_and_scope_withdrawal_stops_live_message_but_retains_exact_cleanup(space):
    b,h,_,t,_,_,(actor,other_actor),_=space
    message=ping(space);execution=reserve(space,message)['execution']
    live=b.start_execution(actor,execution['id'],1)
    with b._tx() as db:db.execute('DELETE FROM thread_members WHERE thread_id=? AND owner_id=?',(t['id'],h['owner_id']))
    assert b.heartbeat_execution(actor,execution['id'],live['revision'])['should_stop']
    reject(404,b.get_execution_control,other_actor,execution['id'])
    control=b.get_execution_control(actor,execution['id'])
    b.set_policy(h,False,False,False,False,[],[])
    assert b.lookup_execution(actor,'message-turn')['found']
    done=b.finish_execution(actor,execution['id'],control['revision'],'cancelled',True,'policy_changed')
    assert done['state']=='finished'


def test_revoked_device_cannot_even_cleanup_and_uncertain_capacity_is_retained(space):
    b,h,*_=space;actor=space[6][0];message=ping(space)
    execution=reserve(space,message)['execution']
    b.revoke_device(h,space[7]['id'])
    reject(403,b.lookup_execution,actor,'message-turn')
    reject(403,b.finish_execution,actor,execution['id'],1,'cancelled',True)
    assert b.get_runtime(h)['active_count']==1


@pytest.mark.parametrize('field',['allow_read','allow_reply'])
def test_current_owner_permissions_gate_message_admission(space,field):
    b,h,*_=space;message=ping(space)
    values={'enabled':True,'allow_read':True,'allow_reply':True,'allow_requests':False,'actions':[],'resources':[]}
    values[field]=False;b.set_policy(h,**values)
    reject(403,reserve,space,message)
    assert b.get_runtime(h)['active_count']==0


def test_profile_version_is_pinned_at_admission_but_exact_cleanup_survives_change(space):
    b,h,_,_,reg,_,(actor,_),device=space
    message=ping(space);execution=reserve(space,message)['execution']
    profile=b.get_agent_profile(h,reg['identity']['id'])
    b.update_agent_profile(h,profile['id'],profile['version'],description='Changed profile')
    reject(409,b.start_execution,actor,execution['id'],1)
    raw=b.authenticate(device['token'])
    b.register_agent(raw,actor['session_id'],'Codex',[],'codex',False,'default')
    reject(409,b.start_execution,actor,execution['id'],1)
    assert b.get_execution_control(actor,execution['id'])['should_stop']
    assert b.finish_execution(actor,execution['id'],1,'cancelled',True,'policy_changed')['state']=='finished'


def test_current_profile_message_permission_is_required_even_after_reregistration(space):
    b,h,_,_,reg,_,(actor,_),device=space
    message=ping(space)
    profile=b.get_agent_profile(h,reg['identity']['id'])
    policy={**profile['policy'],'allow_messages':False}
    b.update_agent_profile(h,profile['id'],profile['version'],policy=policy)
    b.register_agent(b.authenticate(device['token']),actor['session_id'],'Codex',[],'codex',False,'default')
    reject(403,reserve,space,message)
    assert b.get_runtime(h)['active_count']==0


def test_generic_agent_preserves_existing_owner_addressing(space):
    b,h,_,t,*_=space
    raw=b.authenticate(b.create_device(h,'Generic fixture',[])['token'])
    b.register_agent(raw,'generic','Generic fixture',[],'agent',False,'default')
    actor=b.actor_session(raw,'generic')
    message=b.post_message(space[2],t['id'],'Owner-addressed question',mentions=[h['owner_id']],idempotency_key='generic-question')
    execution=reserve(space,message,actor=actor)['execution']
    assert execution['scope_kind']=='message' and not execution['should_stop']


def test_private_project_link_is_not_revealed_and_old_context_requests_stop(space):
    b,h,_,_,reg,_,(actor,_),_=space
    project=b.create_project(h,'Private context',[h['owner_id']],'private-project')
    ticket=b.create_ticket(h,project['id'],'Question',idempotency_key='private-ticket')
    message=b.post_message(h,ticket['thread_id'],'Question',mentions=['agent:'+reg['identity']['id']],idempotency_key='private-question')
    execution=reserve(space,message)['execution']
    assert execution['project_id']==project['id']
    with b._tx() as db:db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',(project['id'],h['owner_id']))
    assert b.get_execution_control(actor,execution['id'])['should_stop']
    b.finish_execution(actor,execution['id'],1,'cancelled',True,'policy_changed')
    # Explicit thread membership survives, but no private project reference may
    # be disclosed through a message-only reservation.
    replacement=reserve(space,message,key='thread-only')['execution']
    assert replacement['project_id'] is None and replacement['ticket_id'] is None


@pytest.mark.parametrize('bad',[True,1,None,'A'*64,'0'*63,'0'*65,'/private/path'])
def test_source_hash_is_strict_and_invalid_admission_leaves_no_slot(space,bad):
    message=ping(space)
    reject(400,reserve,space,message,source_sha256=bad)
    assert space[0].get_runtime(space[1])['active_count']==0


def test_message_and_ticket_admissions_share_one_owner_limit_and_cross_scope_keys(runtime):
    b,h,_,_,agent=runtime
    actor,ticket,profile,*_=agent()
    thread=b.create_thread(h,'General',[h['owner_id']],'general')
    message=b.post_message(h,thread['id'],'Please reply',mentions=['agent:'+profile],idempotency_key='ping')
    digest_value=b.get_message(actor,thread['id'],message['id'])['source_sha256']
    args=dict(idempotency_key='message',thread_id=thread['id'],message_id=message['id'],source_sha256=digest_value,
              work_id='message-work',turn_id='message-turn',provider='codex_chatgpt',model='fixture-model')
    barrier=threading.Barrier(2)
    def compete(kind):
        barrier.wait()
        try:return b.reserve_message_execution(actor,**args)['execution'] if kind=='message' else ticket_reserve(runtime,actor,ticket)
        except BoardError as error:return str(error)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(compete,['message','ticket']))
    assert sum(isinstance(item,dict) for item in results)==1 and results.count('capacity_exhausted')==1
    row=next(item for item in results if isinstance(item,dict))
    b.finish_execution(actor,row['id'],1,'cancelled',True)
    legacy=ticket_reserve(runtime,actor,ticket,key='shared-key')
    assert not {'scope_kind','thread_id','message_id','source_sha256'}.intersection(legacy)
    reject(409,b.reserve_message_execution,actor,**{**args,'idempotency_key':'shared-key'})
    reject(409,b.reserve_message_execution,actor,**{**args,'work_id':legacy['work_id'],'turn_id':legacy['turn_id']})


def test_schema_nine_migration_preserves_legacy_rows_indexes_foreign_keys_and_rolls_back(tmp_path,monkeypatch):
    import board as module
    with monkeypatch.context() as patch:
        patch.setattr(module,'migrate_message_runtime',lambda db:None)
        b=Board(tmp_path/'legacy')
    h={'kind':'human','owner_id':'owner'};b.ensure_owner('owner','Owner');b.set_policy(h,True,True,True,True,[],[])
    project=b.create_project(h,'Project',['owner'],'project');ticket=b.create_ticket(h,project['id'],'Task',idempotency_key='ticket')
    device=b.create_device(h,'Fixture',[]);raw=b.authenticate(device['token'])
    b.register_agent(raw,'legacy-session','Legacy',[],'codex',False,'legacy');actor={**raw,'session_id':'legacy-session'}
    b.claim_ticket(actor,ticket['id'],ticket['version'])
    original_view=b._execution_view
    b._execution_view=lambda db,row:original_view(db,{**dict(row),'scope_kind':'ticket'})
    b.reserve_execution(actor,'old',project['id'],ticket['id'],'old-work','old-turn','codex_chatgpt','fixture')
    with b._tx() as db:
        db.execute('DROP TABLE mcp_page_cursors')
        db.execute('ALTER TABLE message_feeds DROP COLUMN pending_view')
        db.execute('ALTER TABLE message_feeds DROP COLUMN pending_compact_json')
        db.execute('PRAGMA user_version=9')
        db.execute('CREATE INDEX runtime_fixture_provider ON runtime_executions(provider)')
        fields=[row['name'] for row in db.execute('PRAGMA table_info(runtime_executions)')]
        before=[tuple(row) for row in db.execute('SELECT * FROM runtime_executions')]
        indexes=[tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='runtime_executions' ORDER BY name")]
        fks=[tuple(row)[2:] for row in db.execute('PRAGMA foreign_key_list(runtime_executions)')]
    original=module.migrate_message_runtime
    def fail(db):original(db);raise RuntimeError('synthetic migration failure')
    with monkeypatch.context() as patch:
        patch.setattr(module,'migrate_message_runtime',fail)
        with pytest.raises(RuntimeError,match='synthetic migration failure'):Board(b.data_dir)
    with b._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==9
        assert [tuple(row) for row in db.execute('SELECT * FROM runtime_executions')]==before
    upgraded=Board(b.data_dir)
    with upgraded._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT '+','.join(fields)+' FROM runtime_executions')]==before
        assert [tuple(row) for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='runtime_executions' ORDER BY name")]==indexes
        current_fks=[tuple(row)[2:] for row in db.execute('PRAGMA foreign_key_list(runtime_executions)')]
        assert all(fk in current_fks for fk in fks)
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    assert upgraded.lookup_execution(actor,'old')['execution']['project_id']==project['id']


def test_schema_ten_backup_restores_message_capacity_and_exact_source(space,tmp_path):
    b,h,*_=space;actor=space[6][0];message=ping(space);execution=reserve(space,message)['execution']
    archive=tmp_path/'message-runtime.zip';manifest=create_backup(b.data_dir,archive)
    assert manifest['schema']['user_version']==11
    destination=tmp_path/'restored';restore_backup(archive,destination);restored=Board(destination)
    assert restored.lookup_execution(actor,'message-turn')['execution']==execution
    assert restored.get_message(actor,message['thread_id'],message['id'])==b.get_message(actor,message['thread_id'],message['id'])
    assert restored.get_runtime(h)['active_count']==1
