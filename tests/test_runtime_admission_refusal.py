"""Actual MCP endpoint/SDK, immutable Board refusals and serialized races."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest

from board import Board,BoardError
from legacy_schema_fixtures import remove_provisioning_schema16_tables
from test_runtime_api import browser,rpc,reservation


def message_arguments(browser):
    _,board,human,token,*_=browser
    profile=browser[6]
    thread=board.create_thread(human,'Admission fixture',[human['owner_id']],'admission-fixture')
    message=board.post_message(human,thread['id'],'SYNTHETIC_PRIVATE_HUMAN',
        mentions=['agent:'+profile],idempotency_key='admission-fixture')
    source=rpc(browser,'get_message',{'thread_id':thread['id'],'message_id':message['id']})
    return dict(idempotency_key='admission-fixture',thread_id=thread['id'],message_id=message['id'],
        source_sha256=source['source_sha256'],work_id='admission-work',turn_id='admission-turn',
        provider='codex_chatgpt',model='fixture-model')


def actor(browser):
    return {**browser[1].authenticate(browser[3]),'session_id':'runtime-api'}


def occupy(browser):
    row=reservation(browser)['execution']
    rpc(browser,'start_execution',{'execution_id':row['id'],'expected_revision':row['revision']})
    with browser[1]._connection() as db:
        return tuple(db.execute('SELECT * FROM runtime_executions WHERE id=?',(row['id'],)).fetchone())


def increase(browser):
    board,human=browser[1:3];limits=board.get_runtime(human)['limits']
    board.update_runtime_limits(human,limits['version'],2,False)


def test_true_mcp_sdk_confirms_closed_bound_denial_and_no_execution(browser):
    from blackoutburst_agents.message_runtime import MessageRuntimeClient
    from blackoutburst_agents.runtime_admission import validate_refusal
    old=occupy(browser);args=message_arguments(browser)
    client=MessageRuntimeClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_MEMORY_ONLY',
        'client':'codex','identity_key':'fixture','name':'Fixture'},'runtime-api',3)
    client._initialized=True
    def memory_rpc(method,params):
        response=browser[0].post('/mcp',headers={'Authorization':'Bearer '+browser[3],
            'Accept':'application/json, text/event-stream'},json={
                'jsonrpc':'2.0','id':7,'method':method,'params':params})
        assert response.status_code==200
        return response.json()['result']
    client._rpc=memory_rpc
    result=client.call('reserve_message_execution',args)
    raw=browser[1].authenticate(browser[3])
    proof=validate_refusal(result,{**args,'session_id':'runtime-api','refusal_mode':'capacity-v1'},
        owner_id=browser[2]['owner_id'],agent_id=browser[6],device_id=raw['device_id'],profile_version=1)
    assert result['reserved'] is False and proof['reason']=='capacity_exhausted'
    assert 'execution' not in result and 'cleanup' not in json.dumps(result)
    assert not any(word in json.dumps(result) for word in ('SYNTHETIC_PRIVATE_HUMAN','Bearer','token'))
    with browser[1]._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==16
        denied=db.execute('SELECT * FROM runtime_admission_refusals').fetchall()
        assert len(denied)==1 and json.loads(denied[0]['response'])==result
        assert tuple(db.execute('SELECT * FROM runtime_executions').fetchone())==old
    assert rpc(browser,'lookup_execution',{'idempotency_key':args['idempotency_key']})=={'found':False}
    increase(browser)
    assert client.call('reserve_message_execution',args)==result
    rpc(browser,'reserve_message_execution',args,error=True)  # legacy cannot create a late slot
    assert browser[1].get_runtime(browser[2])['active_count']==1


@pytest.mark.parametrize('mode',[True,False,1,0,'other'])
def test_mcp_refusal_mode_strict_and_legacy_failure_stays_legacy(browser,mode):
    occupy(browser);args=message_arguments(browser)
    rpc(browser,'reserve_message_execution',{**args,'refusal_mode':mode},error=True)
    with browser[1]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
    rpc(browser,'reserve_message_execution',args,error=True)


def test_existing_positive_precedes_capacity_and_never_becomes_a_denial(browser):
    args=message_arguments(browser)
    positive=rpc(browser,'reserve_message_execution',args)
    replay=rpc(browser,'reserve_message_execution',{**args,'refusal_mode':'capacity-v1'})
    assert replay['reserved'] is True and replay['retried'] is True
    assert replay['execution']==positive['execution']
    with browser[1]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0


def test_key_turn_ticket_and_session_collisions_cannot_bypass_durable_refusal(browser):
    occupy(browser);args={**message_arguments(browser),'refusal_mode':'capacity-v1'}
    denied=rpc(browser,'reserve_message_execution',args);increase(browser)
    assert rpc(browser,'reserve_message_execution',args)==denied
    for change in ({'idempotency_key':'changed'},{'turn_id':'changed'},{'model':'changed'}):
        rpc(browser,'reserve_message_execution',{**args,**change},error=True)
    board=browser[1];current=actor(browser)
    other={**current,'session_id':'other-session'}
    raw=board.authenticate(browser[3])
    board.register_agent(raw,'other-session','Fixture',[],'codex',False,'fixture')
    with pytest.raises(BoardError):board.reserve_message_execution(other,**args)
    with pytest.raises(BoardError):
        board.reserve_execution(current,args['idempotency_key'],browser[4]['id'],browser[5]['id'],
            args['work_id'],args['turn_id'],'codex_chatgpt','fixture-model')
    with pytest.raises(BoardError):
        board.reserve_execution(current,'different-key',browser[4]['id'],browser[5]['id'],
            args['work_id'],args['turn_id'],'codex_chatgpt','fixture-model')
    assert board.get_runtime(browser[2])['active_count']==1


@pytest.mark.parametrize('change',['policy','source','profile','device'])
def test_replay_requires_current_authorization_source_and_binding(browser,change):
    occupy(browser);args={**message_arguments(browser),'refusal_mode':'capacity-v1'}
    rpc(browser,'reserve_message_execution',args);increase(browser)
    board,human=browser[1:3]
    if change=='policy':board.set_policy(human,False,False,False,False,[],[])
    elif change=='source':
        with board._tx() as db:db.execute('UPDATE messages SET body=? WHERE id=?',('changed',args['message_id']))
    elif change=='profile':
        profile=board.get_agent_profile(human,browser[6])
        board.update_agent_profile(human,browser[6],profile['version'],status='paused')
    else:
        raw=board.authenticate(browser[3]);board.revoke_device(human,raw['device_id'])
    with pytest.raises(BoardError):board.reserve_message_execution(actor(browser) if change!='device' else {
        'kind':'agent','owner_id':human['owner_id'],'device_id':raw['device_id'],'session_id':'runtime-api'},**args)
    with board._connection() as db:assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==1


def test_denial_commit_is_atomic_and_failure_does_not_return_a_proof(browser):
    occupy(browser);args={**message_arguments(browser),'refusal_mode':'capacity-v1'};board=browser[1]
    with board._tx() as db:
        db.execute('''CREATE TRIGGER synthetic_refusal_abort BEFORE INSERT ON runtime_admission_refusals
            BEGIN SELECT RAISE(ABORT,'synthetic failure'); END''')
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):board.reserve_message_execution(actor(browser),**args)
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==1


def test_denial_and_positive_cross_table_corruption_is_refused(browser):
    occupy(browser);args={**message_arguments(browser),'refusal_mode':'capacity-v1'};board=browser[1]
    rpc(browser,'reserve_message_execution',args);increase(browser)
    # Synthetic corruption only: retain proof, temporarily hide it to create a
    # contradictory positive, restore it. Product APIs cannot do this.
    with board._tx() as db:
        row=tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())
        db.execute('DELETE FROM runtime_admission_refusals')
    board.reserve_message_execution(actor(browser),**args)
    with board._tx() as db:db.execute('INSERT INTO runtime_admission_refusals VALUES(?,?,?,?,?,?,?,?,?,?)',row)
    with pytest.raises(BoardError,match='execution_admission_conflict'):
        board.reserve_message_execution(actor(browser),**args)


def test_two_namespaces_remain_closed_under_concurrent_legacy_retries(browser):
    occupy(browser);args={**message_arguments(browser),'refusal_mode':'capacity-v1'};board=browser[1]
    denied=board.reserve_message_execution(actor(browser),**args);increase(browser)
    barrier=threading.Barrier(3)
    def attempt(kind):
        barrier.wait()
        try:
            if kind=='message-legacy':
                legacy={key:value for key,value in args.items() if key!='refusal_mode'}
                return board.reserve_message_execution(actor(browser),**legacy)
            if kind=='ticket-other-key':
                return board.reserve_execution(actor(browser),'new-key',browser[4]['id'],browser[5]['id'],
                    args['work_id'],args['turn_id'],'codex_chatgpt','fixture-model')
            return board.reserve_message_execution(actor(browser),**args)
        except BoardError:return 'rejected'
    with ThreadPoolExecutor(3) as pool:
        results=list(pool.map(attempt,['message-legacy','ticket-other-key','message-new']))
    assert results==['rejected','rejected',denied]
    assert board.get_runtime(browser[2])['active_count']==1


@pytest.mark.parametrize('same_key',[False,True])
def test_first_admission_serializes_legacy_and_opt_in_without_false_negative(browser,same_key):
    args=message_arguments(browser);board=browser[1];current=actor(browser)
    barrier=threading.Barrier(2)
    def attempt(opted):
        barrier.wait()
        options={**args}
        if opted:options['refusal_mode']='capacity-v1'
        if not same_key and not opted:options['idempotency_key']='other-key'
        try:return board.reserve_message_execution(current,**options)
        except BoardError as error:return str(error)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(attempt,[False,True]))
    positive=[row for row in results if isinstance(row,dict)]
    assert positive and all(row['reserved'] is True for row in positive)
    if same_key:
        assert len(positive)==2 and positive[0]['execution']['id']==positive[1]['execution']['id']
    else:assert len(positive)==1 and 'execution_turn_conflict' in results
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==1


def test_corrupt_denial_document_or_cross_key_positive_does_not_get_adopted(browser):
    occupy(browser);args={**message_arguments(browser),'refusal_mode':'capacity-v1'};board=browser[1]
    response=board.reserve_message_execution(actor(browser),**args);increase(browser)
    with board._tx() as db:
        db.execute('UPDATE runtime_admission_refusals SET response=?',(json.dumps({'reserved':False,'refusal':False}),))
    with pytest.raises(BoardError,match='execution_admission_conflict'):
        board.reserve_message_execution(actor(browser),**args)
    with board._tx() as db:
        db.execute('UPDATE runtime_admission_refusals SET response=?',(
            json.dumps(response,ensure_ascii=False,sort_keys=True,separators=(',',':')),))
        denied=tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())
        db.execute('DELETE FROM runtime_admission_refusals')
    board.reserve_message_execution(actor(browser),**{**args,'idempotency_key':'different-key'})
    with board._tx() as db:db.execute('INSERT INTO runtime_admission_refusals VALUES(?,?,?,?,?,?,?,?,?,?)',denied)
    with pytest.raises(BoardError,match='execution_admission_conflict'):
        board.reserve_message_execution(actor(browser),**args)


def test_schema14_upgrade_preserves_all_existing_execution_bytes_and_ids(browser):
    occupy(browser);board=browser[1]
    with board._tx() as db:
        remove_provisioning_schema16_tables(db)
        db.execute('DROP TABLE runtime_admission_refusals');db.execute('PRAGMA user_version=14')
        executions=[tuple(row) for row in db.execute('SELECT * FROM runtime_executions')]
        claims=[tuple(row) for row in db.execute('SELECT * FROM tickets')]
    upgraded=Board(board.data_dir)
    with upgraded._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==16
        assert [tuple(row) for row in db.execute('SELECT * FROM runtime_executions')]==executions
        assert [tuple(row) for row in db.execute('SELECT * FROM tickets')]==claims
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
