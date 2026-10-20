"""Local Board and actual MCP HTTP boundary only; no model or native execution."""
from collections import Counter
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from legacy_schema_fixtures import remove_server_schema14_tables

from board import Board, BoardError
from development_frames import (get_development_frame, wire_frame, encoded,
    MAX_DATA_BYTES, MAX_WIRE_BYTES)
from test_delegation_families_api import family_api, opened
from test_runtime_api import browser


@pytest.fixture
def frame_fixture(family_api):
    browser,args=family_api
    value=opened(family_api)
    client,board,human,token,*_=browser
    with board._connection() as db:
        member=dict(db.execute('SELECT * FROM delegation_members WHERE family_id=? ORDER BY ticket_id',
                               (value['id'],)).fetchone())
    request=dict(session_id=member['session_id'],ticket_id=member['ticket_id'],family_id=value['id'],terminal=False)
    return browser,value,request


def http_frame(fixture,changes=None,*,error=False):
    browser,_,args=fixture
    arguments={**args,**(changes or {})}
    response=browser[0].post('/mcp',headers={'Authorization':'Bearer '+browser[3],
        'Accept':'application/json, text/event-stream','Content-Type':'application/json'},
        content=json.dumps({'jsonrpc':'2.0','id':42,'method':'tools/call',
            'params':{'name':'get_development_frame','arguments':arguments}},ensure_ascii=True).encode('ascii'))
    assert response.status_code==200,response.text
    result=response.json()['result']
    assert bool(result.get('isError')) is error,result
    return result if error else result['structuredContent']


def local_frame(fixture,**changes):
    browser,_,args=fixture
    board,token=browser[1],browser[3]
    return get_development_frame(board,lambda session:board.actor_session(board.authenticate(token),session),
                                 **{**args,**changes})


def test_actual_mcp_schema_strict_arguments_full_sources_and_one_http(frame_fixture):
    browser,family,args=frame_fixture
    client,board,_,token,*_=browser
    result=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()['result']
    schema=next(tool['inputSchema'] for tool in result['tools'] if tool['name']=='get_development_frame')
    assert schema['additionalProperties'] is False
    assert set(schema['required'])=={'session_id','ticket_id','family_id','terminal'}
    observed=[]
    from contextlib import ExitStack
    with ExitStack() as stack:
        actor_sessions=stack.enter_context(patch.object(board,'actor_session',wraps=board.actor_session))
        for name in ('get_work','get_ticket','get_thread','get_delegation_family'):
            original=getattr(board,name)
            def call(actor,_name=name,_original=original,**arguments):
                observed.append((_name,actor['session_id'],arguments))
                return _original(actor,**arguments)
            stack.enter_context(patch.object(board,name,side_effect=call))
        frame=http_frame(frame_fixture)
        assert actor_sessions.call_count==16  # 12 methods plus two selection brackets, two roles each.
    assert Counter(name for name,_,_ in observed)=={
        'get_work':4,'get_ticket':4,'get_thread':2,'get_delegation_family':2}
    assert frame['selected']['first']['ticket']['id']==args['ticket_id']
    assert frame['family']['first']['plan_sha256']==family['plan_sha256']
    assert frame['selection']['parent_session_id']=='runtime-api'
    assert all(session=='runtime-api' for name,session,_ in observed if name=='get_delegation_family')
    assert 'description' in frame['selected']['first']['ticket']
    assert 'description_preview' not in frame['selected']['first']['ticket']
    # Other tools retain their existing compact behavior.
    other=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'get_ticket',
              'arguments':{'session_id':'runtime-api','ticket_id':args['ticket_id']}}}).json()['result']
    assert not other.get('isError')


@pytest.mark.parametrize('changes',[
    {'extra':'PRIVATE_TEST_VALUE'}, {'terminal':'true'},{'terminal':1},{'terminal':0},{'terminal':None},
    {'terminal':'false'},{'session_id':True},{'ticket_id':'\ud800'}, {'family_id':'a\x00b'},
    {'session_id':'x'*201}, {'project_id':'arbitrary'}, {'parent_session_id':'arbitrary'},
    {'methods':['post_message']}, {'limit':200},
])
def test_actual_mcp_rejects_raw_extras_coercions_and_invalid_utf8(frame_fixture,changes):
    result=http_frame(frame_fixture,changes,error=True)
    assert 'PRIVATE_TEST_VALUE' not in json.dumps(result)
    assert 'development_frame_invalid_arguments' in json.dumps(result)
    assert 'selection' not in result


@pytest.mark.parametrize('who',['parent','child'])
def test_both_exact_family_roles_can_be_selected(frame_fixture,who):
    browser,family,args=frame_fixture
    changes={} if who=='child' else dict(session_id='runtime-api',ticket_id=family['parent_ticket_id'])
    frame=http_frame(frame_fixture,changes)
    assert frame['selection']['session_id']==changes.get('session_id',args['session_id'])
    assert frame['selected']['first']['ticket']['id']==changes.get('ticket_id',args['ticket_id'])


def test_wrong_member_or_unbound_session_cannot_borrow_parent(frame_fixture):
    browser,_,args=frame_fixture
    board=browser[1];raw=board.authenticate(browser[3])
    board.register_agent(raw,'unbound-same-profile','Fixture',[],'codex',False,'fixture')
    with board._connection() as db:
        other=db.execute('SELECT session_id FROM delegation_members WHERE family_id=? AND ticket_id!=?',
                         (args['family_id'],args['ticket_id'])).fetchone()[0]
    for session in ('runtime-api',other,'unbound-same-profile','unknown'):
        result=http_frame(frame_fixture,dict(session_id=session),error=True)
        assert 'development_frame_unavailable' in json.dumps(result)
        assert 'selection' not in result


@pytest.mark.parametrize('change',['profile','device','membership','owner_read','parent_session'])
def test_changes_during_history_never_return_partial_frame(frame_fixture,change):
    browser,_,args=frame_fixture
    board,human=browser[1],browser[2]
    original=board.get_thread;done=False
    def history(*values,**kw):
        nonlocal done
        result=original(*values,**kw)
        if not done:
            done=True
            if change=='profile':
                profile=board.get_agent_profile(human,browser[-1])
                board.update_agent_profile(human,profile['id'],profile['version'],status='paused')
            elif change=='device':board.revoke_device(human,board.authenticate(browser[3])['device_id'])
            elif change=='owner_read':board.set_policy(human,True,False,True,True,[],[])
            elif change=='membership':
                with board._tx() as db:db.execute('DELETE FROM thread_members WHERE owner_id=?',(human['owner_id'],))
            else:
                with board._tx() as db:db.execute('UPDATE sessions SET profile_version=profile_version+1 WHERE id=?',('runtime-api',))
        return result
    with patch.object(board,'get_thread',side_effect=history):
        result=http_frame(frame_fixture,error=True)
    assert 'development_frame_unavailable' in json.dumps(result)
    assert 'selection' not in result


def test_late_grant_is_fresh_raw_observation_without_claim_mutation(frame_fixture):
    browser,_,args=frame_fixture;board,human=browser[1],browser[2]
    before=board.get_ticket(human,args['ticket_id'])['ticket']
    original=board.get_delegation_family;count=0
    def family(*values,**kw):
        nonlocal count
        result=original(*values,**kw);count+=1
        if count==2:board.set_policy(human,True,True,True,False,[],[])
        return result
    with patch.object(board,'get_delegation_family',side_effect=family):
        frame=http_frame(frame_fixture)
    assert frame['selected']['policy_first']['policy']['allow_requests'] is True
    assert frame['selected']['policy_last']['policy']['allow_requests'] is False
    after=board.get_ticket(human,args['ticket_id'])['ticket']
    assert {k:v for k,v in after.items() if k!='delegation'}=={k:v for k,v in before.items() if k!='delegation'}
    assert board.get_runtime(human)['active_count']==0


def test_real_cursors_stop_at_forty_unique_without_overfetch(frame_fixture):
    browser,_,args=frame_fixture;board,human=browser[1],browser[2]
    ids=[args['ticket_id'],browser[5]['id']]
    for ticket in ids:
        thread=board.get_ticket(human,ticket)['ticket']['thread_id']
        for i in range(45):board.post_message(human,thread,'message '+str(i),idempotency_key=ticket+str(i))
    with patch.object(board,'get_thread',wraps=board.get_thread) as history:
        frame=local_frame(frame_fixture)
    assert history.call_count==4
    for role in ('selected','parent'):
        pages=frame[role]['pages']
        assert [page['arguments']['limit'] for page in pages]==[20,20]
        assert pages[1]['arguments']['before']==pages[0]['response']['next_before']
        assert len({message['id'] for page in pages for message in page['response']['messages']})==40


def test_overlap_uses_real_remaining_limit_and_at_most_sixteen_reads(frame_fixture):
    browser,_,args=frame_fixture;board,human=browser[1],browser[2]
    for ticket in (args['ticket_id'],browser[5]['id']):
        thread=board.get_ticket(human,ticket)['ticket']['thread_id']
        for i in range(45):board.post_message(human,thread,'message '+str(i),idempotency_key=ticket+str(i))
    original=board.get_thread;seen={}
    def overlap(actor,**kw):
        result=original(actor,**kw)
        state=seen.setdefault(kw['thread_id'],[])
        if len(state)==1:
            result['messages']=result['messages'][1:]+[state[0]['messages'][0]]
            result['next_before']=result['messages'][0]['id']
        state.append(deepcopy(result))
        return result
    from contextlib import ExitStack
    with ExitStack() as stack:
        calls={name:stack.enter_context(patch.object(board,name,wraps=getattr(board,name)))
               for name in ('get_work','get_ticket','get_delegation_family')}
        history=stack.enter_context(patch.object(board,'get_thread',side_effect=overlap))
        frame=local_frame(frame_fixture)
    assert sum(call.call_count for call in calls.values())+history.call_count==16
    for role in ('selected','parent'):
        assert [p['arguments']['limit'] for p in frame[role]['pages']]==[20,20,1]
        assert len({m['id'] for p in frame[role]['pages'] for m in p['response']['messages']})==40


@pytest.mark.parametrize('case',['wrong_family','wrong_ticket','same_owner_other_device','foreign_owner','other_profile'])
def test_foreign_selection_never_exposes_a_parent_session(frame_fixture,case):
    browser,family,args=frame_fixture;board,human=browser[1],browser[2]
    changes={}
    if case=='wrong_family':changes['family_id']='0'*32
    elif case=='wrong_ticket':changes['ticket_id']=family['parent_ticket_id']
    else:
        owner=human
        if case=='foreign_owner':
            board.ensure_owner('thescreamer','Other')
            owner={'kind':'human','owner_id':'thescreamer'}
            board.set_policy(owner,True,True,True,True,[],[])
        if case=='other_profile':
            raw=board.authenticate(browser[3])
            profile=board.create_agent_profile(human,'Other fixture',idempotency_key='other-profile',status='active',
                policy=dict(allow_read=True,allow_messages=False,allow_requests=False,actions=[],resources=[],autonomy='responses'))
            board.bind_agent_profile(human,profile['id'],profile['version'],raw['device_id'],'different-profile','codex')
        else:
            device=board.create_device(owner,'Another installation',[])
            raw=board.authenticate(device['token']);browser=list(browser);browser[3]=device['token']
        board.register_agent(raw,'foreign-selected','Fixture',[],'codex',False,'different-profile')
        changes['session_id']='foreign-selected'
    result=http_frame((browser,family,args),changes,error=True)
    assert 'development_frame_unavailable' in json.dumps(result)
    assert 'parent_session_id' not in json.dumps(result)


def test_storage_exception_is_a_fixed_error_not_a_diagnostic_channel(frame_fixture):
    with patch.object(frame_fixture[0][1],'get_work',side_effect=RuntimeError('PRIVATE_STORAGE_DETAIL')):
        result=http_frame(frame_fixture,error=True)
    assert 'development_frame_unavailable' in json.dumps(result)
    assert 'PRIVATE_STORAGE_DETAIL' not in json.dumps(result)


def test_complete_read_has_no_new_presence_claim_runtime_or_message_effect(frame_fixture):
    board=frame_fixture[0][1]
    tables=('tickets','tasks','runtime_executions','sessions','messages','human_requests',
            'reader_state','cursors','mcp_page_cursors','audit')
    def state():
        with board._connection() as db:
            return {table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                    for table in tables}
    before=state()
    local_frame(frame_fixture)
    assert state()==before


@pytest.mark.parametrize('change',['mark_read','title','duplicate_body'])
def test_pagination_compares_source_projection_and_keeps_raw_observations(frame_fixture,change):
    browser,_,args=frame_fixture;board,human=browser[1],browser[2]
    thread=board.get_ticket(human,args['ticket_id'])['ticket']['thread_id']
    for i in range(25):board.post_message(human,thread,'Source '+str(i),idempotency_key='projection-'+str(i))
    original=board.get_thread;pages=[]
    def history(actor,**arguments):
        result=original(actor,**arguments)
        if arguments['thread_id']==thread:
            if not pages:
                if change=='mark_read':board.mark_read(human,thread)
                elif change=='title':
                    with board._tx() as db:db.execute('UPDATE threads SET title=? WHERE id=?',('Changed source title',thread))
            elif change=='duplicate_body':
                duplicate=deepcopy(pages[0]['messages'][0]);duplicate['body']='Changed duplicate body'
                result['messages'].append(duplicate)
            pages.append(deepcopy(result))
        return result
    with patch.object(board,'get_thread',side_effect=history):
        if change=='mark_read':
            frame=local_frame(frame_fixture)
            observed=frame['selected']['pages']
            assert [p['response']['thread']['unread_count'] for p in observed]==[25,0]
            assert len({m['id'] for p in observed for m in p['response']['messages']})==25
        else:
            with pytest.raises(BoardError,match='development_frame_invalid_pagination'):
                local_frame(frame_fixture)


@pytest.mark.parametrize('value',['😀' * 40000, '\\\"\n'*80000, '\ud800'],ids=['nonbmp','escapes','surrogate'])
def test_aggregate_overflow_or_invalid_unicode_has_no_partial_response(frame_fixture,value):
    board=frame_fixture[0][1];original=board.get_work
    def work(*args,**kw):return {**original(*args,**kw),'synthetic':value}
    with patch.object(board,'get_work',side_effect=work):
        result=http_frame(frame_fixture,error=True)
    assert 'development_frame_' in json.dumps(result)
    assert 'selection' not in result


def test_real_call_tool_wire_bound_counts_both_escaped_copies(frame_fixture):
    frame=local_frame(frame_fixture)
    frame['synthetic']='😀\\\"\n'*2000
    assert len(encoded(frame))<MAX_DATA_BYTES
    result=wire_frame(frame)
    actual=len(encoded(result.model_dump(mode='json',exclude_none=True)))+1024
    assert actual<=MAX_WIRE_BYTES<2*1024*1024
    with patch('development_frames.MAX_WIRE_BYTES',actual-1):
        with pytest.raises(BoardError,match='development_frame_too_large'):wire_frame(frame)


def test_nonbmp_and_escaped_source_survives_the_actual_mcp_wire(frame_fixture):
    browser,_,args=frame_fixture;client,board,human,token,*_=browser
    body='😀\\\"\n'*1000
    thread=board.get_ticket(human,args['ticket_id'])['ticket']['thread_id']
    board.post_message(human,thread,body,idempotency_key='wire-escaped-body')
    response=client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
        json={'jsonrpc':'2.0','id':42,'method':'tools/call',
              'params':{'name':'get_development_frame','arguments':args}})
    assert response.status_code==200
    assert len(response.content)<MAX_WIRE_BYTES<2*1024*1024
    result=response.json()['result']
    assert not result.get('isError')
    assert result['structuredContent']['selected']['pages'][0]['response']['messages'][0]['body']==body


def test_frame_policy_reads_do_not_sweep_blocks_but_ordinary_get_work_does(frame_fixture):
    browser, _, args = frame_fixture
    board, human, project = browser[1], browser[2], browser[4]
    ghost = board.create_ticket(human, project['id'], 'Legacy block fixture',
                                idempotency_key='frame-legacy-block')
    with board._connection() as db:
        db.execute("UPDATE tickets SET status='blocked',blocked_reason='',recheck_at=NULL WHERE id=?", (ghost['id'],))
        db.commit()
    before = board.get_ticket(human, ghost['id'])['ticket']
    events = board.list_activity(human, ticket_id=ghost['id'])
    history = board.get_thread(human, ghost['thread_id'])['messages']
    with patch.object(board, 'sweep_blocked', wraps=board.sweep_blocked) as sweep:
        frame = http_frame(frame_fixture)
        sweep.assert_not_called()
    assert frame['selection']['ticket_id'] == args['ticket_id']
    after = board.get_ticket(human, ghost['id'])['ticket']
    assert (after['status'], after['version']) == ('blocked', before['version'])
    assert board.list_activity(human, ticket_id=ghost['id']) == events
    assert board.get_thread(human, ghost['thread_id'])['messages'] == history
    # Exercise the ordinary public MCP tool, with its usual permission checks.
    from test_runtime_api import rpc
    rpc(browser, 'get_work', {'project_id': project['id']})
    after = board.get_ticket(human, ghost['id'])['ticket']
    assert after['status'] == 'todo' and after['version'] == before['version'] + 1
    assert len([event for event in board.list_activity(human, ticket_id=ghost['id'])
                if event['event'] == 'ticket_unblocked']) == 1


def test_observation_only_policy_reads_keep_read_permission_guards(frame_fixture):
    browser, _, args = frame_fixture
    board, human, token = browser[1], browser[2], browser[3]
    actor = board.actor_session(board.authenticate(token), args['session_id'])
    board.set_policy(human, False, True, True, True, [], [])
    with pytest.raises(BoardError) as rejected:
        board.get_work(actor, project_id=browser[4]['id'], _sweep=False)
    assert rejected.value.status == 403
    assert 'development_frame_unavailable' in json.dumps(http_frame(frame_fixture, error=True))


def test_schema12_migration_preserves_live_family_and_frame_identities(frame_fixture):
    browser, family, args = frame_fixture
    board = browser[1]
    before = http_frame(frame_fixture)
    # Model a pre-schema13 store with real family/session/claim records. Reopen
    # through the production migration path, never by disabling access guards.
    with board._connection() as db:
        remove_server_schema14_tables(db)
        for column in ('blocked_reason', 'unblock_owner', 'recheck_at'):
            db.execute('ALTER TABLE tickets DROP COLUMN ' + column)
        db.execute('PRAGMA user_version=12')
        db.commit()
    migrated = Board(board.data_dir)
    with migrated._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 15
        assert {'blocked_reason', 'unblock_owner', 'recheck_at'} <= {
            row['name'] for row in db.execute('PRAGMA table_info(tickets)')}
    after = http_frame(frame_fixture)
    assert after['selection'] == before['selection']
    assert after['family']['first']['id'] == family['id']
    for role in ('selected', 'parent'):
        assert after[role]['first']['ticket'] == before[role]['first']['ticket']
    assert after['selected']['first']['ticket']['id'] == args['ticket_id']
