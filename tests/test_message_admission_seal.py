"""Explicit seal via real temporary Board/MCP; no process, provider or model."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading

import pytest
from board import BoardError
from test_runtime_api import browser,rpc
from test_runtime_admission_refusal import message_arguments,actor,occupy,increase

METHOD='seal_message_execution_admission'


def test_mcp_sdk_seal_is_permanent_bound_and_does_not_create_or_finish_runtime(browser):
    from blackoutburst_agents.message_admission_seal import MessageAdmissionSealClient,request_sha256
    args=message_arguments(browser);raw=browser[1].authenticate(browser[3])
    client=MessageAdmissionSealClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
        'client':'codex','identity_key':'fixture','name':'Fixture'},'runtime-api',3)
    client._initialized=True
    def memory_rpc(method,params):
        response=browser[0].post('/mcp',headers={'Authorization':'Bearer '+browser[3],
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':7,'method':method,'params':params})
        assert response.status_code==200
        return response.json()['result']
    client._rpc=memory_rpc
    identity=dict(owner_id=browser[2]['owner_id'],agent_id=browser[6],device_id=raw['device_id'],profile_version=1)
    proof=client.seal(args,**identity);wire={**args,'session_id':'runtime-api'}
    assert proof['schema']=='message-runtime-admission-seal-v1'
    assert proof['request_sha256']==request_sha256(METHOD,wire)
    assert proof['admission_request_sha256']==request_sha256('reserve_message_execution',wire)
    assert not any(x in json.dumps(proof) for x in ('SYNTHETIC_PRIVATE_HUMAN','token','cleanup','finished'))
    increase(browser);assert client.seal(args,**identity)==proof
    assert rpc(browser,'lookup_execution',{'idempotency_key':args['idempotency_key']})=={'found':False}
    rpc(browser,'reserve_message_execution',args,error=True)
    rpc(browser,'reserve_message_execution',{**args,'refusal_mode':'capacity-v1'},error=True)
    with browser[1]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0
        rows=db.execute('SELECT * FROM runtime_admission_refusals').fetchall()
        assert len(rows)==1 and json.loads(rows[0]['response'])=={'reserved':False,'refusal':proof}
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='message_admission_sealed'").fetchone()[0]==1
    assert browser[1].get_runtime(browser[2])['active_count']==0


@pytest.mark.parametrize('state',['reserved','running','finished','needs_review'])
@pytest.mark.parametrize('alias',[False,True])
def test_any_real_reservation_prevents_absence_even_terminal_or_alias_key(browser,state,alias):
    args=message_arguments(browser);execution=rpc(browser,'reserve_message_execution',args)['execution']
    if state=='running':rpc(browser,'start_execution',{'execution_id':execution['id'],'expected_revision':1})
    elif state in ('finished','needs_review'):
        rpc(browser,'finish_execution',{'execution_id':execution['id'],'expected_revision':1,
            'status':'blocked','cleanup_confirmed':state=='finished','reason':'execution_failed'})
    with browser[1]._connection() as db:before=tuple(db.execute('SELECT * FROM runtime_executions').fetchone())
    if alias:args={**args,'idempotency_key':'different-key'}
    rpc(browser,METHOD,args,error=True)
    with browser[1]._connection() as db:
        assert tuple(db.execute('SELECT * FROM runtime_executions').fetchone())==before
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0


def test_capacity_refusal_is_not_reclassified_or_overwritten(browser):
    old=occupy(browser);args=message_arguments(browser)
    refusal=rpc(browser,'reserve_message_execution',{**args,'refusal_mode':'capacity-v1'})
    with browser[1]._connection() as db:before=tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())
    rpc(browser,METHOD,args,error=True);increase(browser);rpc(browser,METHOD,args,error=True)
    assert rpc(browser,'reserve_message_execution',{**args,'refusal_mode':'capacity-v1'})==refusal
    with browser[1]._connection() as db:
        assert tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())==before
        assert tuple(db.execute('SELECT * FROM runtime_executions').fetchone())==old


@pytest.mark.parametrize('change',[{'idempotency_key':'other'},{'turn_id':'other'},{'work_id':'other'},
    {'model':'other'},{'provider':'other'}])
def test_seal_key_payload_and_work_turn_conflicts_never_admit_or_replace(browser,change):
    args=message_arguments(browser);proof=rpc(browser,METHOD,args)
    with browser[1]._connection() as db:before=tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())
    rpc(browser,METHOD,{**args,**change},error=True)
    with browser[1]._connection() as db:
        assert tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())==before
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0
    assert rpc(browser,METHOD,args)==proof


@pytest.mark.parametrize('change',['policy','source','profile','device','session'])
@pytest.mark.parametrize('replay',[False,True])
def test_current_identity_source_and_permissions_required_before_write_or_replay(browser,change,replay):
    args=message_arguments(browser);board,human=browser[1:3]
    if replay:rpc(browser,METHOD,args)
    if change=='policy':board.set_policy(human,False,False,False,False,[],[])
    elif change=='source':
        with board._tx() as db:db.execute('UPDATE messages SET body=? WHERE id=?',('changed',args['message_id']))
    elif change=='profile':
        p=board.get_agent_profile(human,browser[6]);board.update_agent_profile(human,p['id'],p['version'],status='paused')
    elif change=='device':board.revoke_device(human,board.authenticate(browser[3])['device_id'])
    else:args={**args,'session_id':'unregistered-session'}
    if change=='device':
        response=browser[0].post('/mcp',headers={'Authorization':'Bearer '+browser[3],
            'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,
                'method':'tools/call','params':{'name':METHOD,'arguments':{'session_id':'runtime-api',**args}}})
        assert response.status_code==401
    else:rpc(browser,METHOD,args,error=True)
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==int(replay)
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0


@pytest.mark.parametrize('other_key',[False,True])
def test_seal_and_late_legacy_reservation_are_serialized_not_false_absence(browser,other_key):
    args=message_arguments(browser);board=browser[1];current=actor(browser);barrier=threading.Barrier(2)
    def call(seal):
        barrier.wait()
        try:
            return board.seal_message_execution_admission(current,**args) if seal else board.reserve_message_execution(
                current,**{**args,'idempotency_key':'late-alias' if other_key else args['idempotency_key']})
        except BoardError:return 'rejected'
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(call,[True,False]))
    with board._connection() as db:
        denials=db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]
        executions=db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]
    assert denials+executions==1
    assert sum(isinstance(r,dict) for r in results)==1
    assert results[1 if denials else 0]=='rejected'
    if denials:assert results[0]['reserved'] is False
    else:assert results[1]['reserved'] is True


def test_same_seal_concurrency_returns_one_proof_and_one_audit(browser):
    args=message_arguments(browser);board=browser[1];current=actor(browser);barrier=threading.Barrier(3)
    def call(_):barrier.wait();return board.seal_message_execution_admission(current,**args)
    with ThreadPoolExecutor(3) as pool:results=list(pool.map(call,range(3)))
    assert results[0]==results[1]==results[2]
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='message_admission_sealed'").fetchone()[0]==1


def test_cross_owner_and_cross_device_scope_never_return_another_owners_seal(browser):
    args=message_arguments(browser);first=rpc(browser,METHOD,args);board=browser[1]
    board.ensure_owner('other','Other');human={'kind':'human','owner_id':'other'}
    board.set_policy(human,True,True,True,False,[],[])
    device=board.create_device(human,'Other fixture',[]);raw=board.authenticate(device['token'])
    profile=board.register_agent(raw,'other-session','Other',[],'codex',False,'other')['identity']['id']
    current={**raw,'session_id':'other-session'}
    with pytest.raises(BoardError):board.seal_message_execution_admission(current,**args)
    thread=board.create_thread(human,'Other',['other'],idempotency_key='other')
    message=board.post_message(human,thread['id'],'OTHER_SYNTHETIC',mentions=['agent:'+profile],idempotency_key='other')
    source=board.get_message(board.actor_session(raw,'other-session'),thread['id'],message['id'])
    other=board.seal_message_execution_admission(current,**{**args,'thread_id':thread['id'],
        'message_id':message['id'],'source_sha256':source['source_sha256']})
    assert other['refusal']['owner_id']=='other' and other!=first
    with board._connection() as db:assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==2


@pytest.mark.parametrize('field,value',[('source_sha256',True),('work_id',False),('model',1),
    ('idempotency_key',''),('refusal_mode','capacity-v1')])
def test_closed_mcp_arguments_do_not_seal_invalid_or_modified_legacy_request(browser,field,value):
    rpc(browser,METHOD,{**message_arguments(browser),field:value},error=True)
    with browser[1]._connection() as db:assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0


@pytest.mark.parametrize('event',['denial','audit'])
def test_failed_atomic_commit_never_returns_a_seal_or_changes_capacity(browser,event):
    import sqlite3
    args=message_arguments(browser);old=occupy(browser);board=browser[1]
    with board._tx() as db:
        if event=='denial':
            db.execute('''CREATE TRIGGER synthetic_abort BEFORE INSERT ON runtime_admission_refusals
                BEGIN SELECT RAISE(ABORT,'synthetic'); END''')
        else:
            db.execute('''CREATE TRIGGER synthetic_abort BEFORE INSERT ON audit
                WHEN NEW.event='message_admission_sealed' BEGIN SELECT RAISE(ABORT,'synthetic'); END''')
    with pytest.raises(sqlite3.IntegrityError):board.seal_message_execution_admission(actor(browser),**args)
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='message_admission_sealed'").fetchone()[0]==0
        assert tuple(db.execute('SELECT * FROM runtime_executions').fetchone())==old
    assert board.get_runtime(browser[2])['active_count']==1


def test_lost_response_replays_existing_seal_without_new_audit_or_reservation(browser):
    args=message_arguments(browser);board=browser[1]
    # Discard the successful committed response, as if the transport were lost.
    board.seal_message_execution_admission(actor(browser),**args)
    proof=rpc(browser,METHOD,args)
    assert proof['reserved'] is False
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='message_admission_sealed'").fetchone()[0]==1


@pytest.mark.parametrize('mode',['key','turn'])
def test_ticket_reservation_collision_blocks_seal_and_reverse_order_also_refuses(browser,mode):
    from test_runtime_api import reservation
    args=message_arguments(browser);board=browser[1]
    # A ticket Runtime cannot be rewritten into message absence.
    reservation(browser,idempotency_key=args['idempotency_key'] if mode=='key' else 'ticket-key',
        work_id=args['work_id'] if mode=='turn' else 'ticket-work',
        turn_id=args['turn_id'] if mode=='turn' else 'ticket-turn')
    rpc(browser,METHOD,args,error=True)
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
    # An independent sealed message blocks a later ticket collision, too.
    sealed={**args,'idempotency_key':'sealed-key','work_id':'sealed-work','turn_id':'sealed-turn'}
    rpc(browser,METHOD,sealed)
    rpc(browser,'reserve_execution',{'project_id':browser[4]['id'],'ticket_id':browser[5]['id'],
        'provider':'codex_chatgpt','model':'fixture-model',
        'idempotency_key':'sealed-key' if mode=='key' else 'other-ticket-key',
        'work_id':'sealed-work' if mode=='turn' else 'other-work',
        'turn_id':'sealed-turn' if mode=='turn' else 'other-turn'},error=True)


@pytest.mark.parametrize('binding',['session','device'])
def test_same_owner_new_binding_cannot_adopt_or_evade_the_sealed_turn(browser,binding):
    args=message_arguments(browser);proof=rpc(browser,METHOD,args);board,human=browser[1:3]
    if binding=='device':raw=board.authenticate(board.create_device(human,'Second fixture',[])['token'])
    else:raw=board.authenticate(browser[3])
    board.register_agent(raw,'second-session','Fixture',[],'codex',False,'second' if binding=='device' else 'fixture')
    current={**raw,'session_id':'second-session'}
    with pytest.raises(BoardError):board.seal_message_execution_admission(current,**args)
    with pytest.raises(BoardError):board.reserve_message_execution(current,**args)
    with board._connection() as db:
        row=db.execute('SELECT * FROM runtime_admission_refusals').fetchone()
        assert json.loads(row['response'])==proof
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==0


def test_active_profile_version_change_cannot_rewrite_seal_after_explicit_registration(browser):
    args=message_arguments(browser);proof=rpc(browser,METHOD,args);board,human=browser[1:3]
    ticket=board.get_ticket(actor(browser),browser[5]['id'])['ticket']
    board.release_ticket(actor(browser),ticket['id'],ticket['version'],'release-fixture','Fixture finished',status='todo')
    profile=board.get_agent_profile(human,browser[6])
    board.update_agent_profile(human,browser[6],profile['version'],name='Changed name')
    rpc(browser,METHOD,args,error=True)  # stale session
    board.register_agent(board.authenticate(browser[3]),'runtime-api','Changed name',[],'codex',False,'fixture')
    rpc(browser,METHOD,args,error=True)  # fresh session is not the original proof identity
    with board._connection() as db:
        assert json.loads(db.execute('SELECT response FROM runtime_admission_refusals').fetchone()[0])==proof


@pytest.mark.parametrize('change',['invalid','positive'])
def test_corrupt_negative_or_cross_table_positive_never_replays_absence(browser,change):
    args=message_arguments(browser);rpc(browser,METHOD,args);board=browser[1]
    if change=='invalid':
        with board._tx() as db:db.execute('UPDATE runtime_admission_refusals SET response=?',('{"reserved":false}',))
    else:
        with board._tx() as db:
            row=tuple(db.execute('SELECT * FROM runtime_admission_refusals').fetchone())
            db.execute('DELETE FROM runtime_admission_refusals')
        rpc(browser,'reserve_message_execution',args)
        with board._tx() as db:db.execute('INSERT INTO runtime_admission_refusals VALUES(?,?,?,?,?,?,?,?,?,?)',row)
    rpc(browser,METHOD,args,error=True)
    rpc(browser,'reserve_message_execution',{**args,'refusal_mode':'capacity-v1'},error=True)


def test_strict_tool_schema_announces_no_unknown_properties(browser):
    response=browser[0].post('/mcp',headers={'Authorization':'Bearer '+browser[3],
        'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':2,'method':'tools/list'})
    schema=next(t['inputSchema'] for t in response.json()['result']['tools'] if t['name']==METHOD)
    assert schema['additionalProperties'] is False
    assert set(schema['properties'])=={'session_id',*message_arguments(browser)}
    assert set(schema['required'])==set(schema['properties'])


def test_seal_works_without_changing_full_paused_capacity_or_old_execution(browser):
    args=message_arguments(browser);old=occupy(browser);board,human=browser[1:3]
    current=board.get_runtime(human)['limits']
    board.update_runtime_limits(human,current['version'],current['max_concurrent'],True)
    with board._connection() as db:limits=tuple(db.execute('SELECT * FROM owner_execution_limits').fetchone())
    result=rpc(browser,METHOD,args)
    assert result['refusal']['reason']=='explicit_pretransport_abandonment'
    with board._connection() as db:
        assert tuple(db.execute('SELECT * FROM runtime_executions').fetchone())==old
        assert tuple(db.execute('SELECT * FROM owner_execution_limits').fetchone())==limits
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==1
    assert board.get_runtime(human)['active_count']==1


@pytest.mark.parametrize('read,reply',[(False,True),(True,False)])
def test_both_current_owner_permissions_are_required_for_new_seal(browser,read,reply):
    args=message_arguments(browser);board,human=browser[1:3]
    board.set_policy(human,read,reply,True,False,[],[])
    rpc(browser,METHOD,args,error=True)
    with board._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0


def test_human_identity_is_not_an_agent_session_seal_authority(browser):
    args=message_arguments(browser)
    with pytest.raises(BoardError):browser[1].seal_message_execution_admission(browser[2],**args)
    with browser[1]._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==0
