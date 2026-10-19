"""Actual family/claim/Runtime/migration paths, without model or native effects."""
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from board import Board, BoardError
from backup import create_backup, restore_backup
from test_delegation_families import family, open_family, member, reject


def claimed(f,opened,index=0):
    b=f['b'];actor=f['actors'][index+1]
    ticket=b.get_ticket(actor,member(f,opened,index)['ticket_id'])['ticket']
    return b.claim_ticket(actor,ticket['id'],ticket['version'])


def execution(f,ticket,index=0,key='turn'):
    return f['b'].reserve_execution(f['actors'][index+1],key,f['project']['id'],ticket['id'],
        'work-'+key,'turn-'+key,'codex_chatgpt','fixture-model')['execution']


def test_exact_children_share_parent_resource_without_unlocking_other_work(family):
    f=family;b=f['b'];opened=open_family(f)
    one,two=claimed(f,opened),claimed(f,opened,1)
    assert one['resource']==two['resource']==f['parent']['resource']
    assert one['delegation']['family_id']==two['delegation']['family_id']==opened['id']
    raw=b.authenticate(b.create_device(f['h'],'Separate profile',[])['token'])
    b.register_agent(raw,'outside-session','Outside',[],'codex',False,'outside')
    outsider=b.actor_session(raw,'outside-session')
    outside=b.create_ticket(f['h'],f['project']['id'],'Other work',resource=f['parent']['resource'],idempotency_key='outside')
    reject(b.claim_ticket,outsider,outside['id'],outside['version'])
    assert b.get_ticket(f['h'],outside['id'])['ticket']['claimant'] is None
    assert b.get_ticket(f['h'],f['parent']['id'])['ticket']['claimant']['session_id']=='coordinator'


def test_same_profile_wrong_session_cannot_immobilize_child(family):
    f=family;b=f['b'];opened=open_family(f)
    raw=b.authenticate(f['device']['token'])
    b.register_agent(raw,'same-profile-unbound','Same profile',[],'codex',False,'same-profile')
    wrong=b.actor_session(raw,'same-profile-unbound');child=b.get_ticket(wrong,member(f,opened)['ticket_id'])['ticket']
    reject(b.claim_ticket,wrong,child['id'],child['version'])
    assert b.get_ticket(f['h'],child['id'])['ticket']['claimant'] is None
    assert claimed(f,opened)['claimant']['session_id']=='child-one'


@pytest.mark.parametrize('actor_index',[0,1,2])
@pytest.mark.parametrize('cancelled',[False,True])
def test_dedicated_sessions_cannot_claim_or_advertise_legacy_work(family,actor_index,cancelled):
    f=family;b=f['b'];opened=open_family(f)
    if cancelled:b.stop_delegation_family(f['h'],opened['id'],opened['revision'])
    child=b.get_ticket(f['h'],member(f,opened)['ticket_id'])['ticket']
    legacy=b.create_task(f['h'],child['thread_id'],'Outside','Read fixture','Evidence','group','alice',
        action='repo.read_file',resource='fixture:other')
    actor=f['actors'][actor_index]
    with b._connection() as db:
        before=tuple(db.iterdump())
        row=db.execute('SELECT * FROM tasks WHERE id=?',(legacy['id'],)).fetchone()
        assert not b._task_authorized(db,actor,row)
    reject(b.claim_task,actor,legacy['id'])
    with b._connection() as db:assert tuple(db.iterdump())==before
    raw=b.authenticate(f['device']['token'])
    b.register_agent(raw,'ordinary-unbound','Ordinary',[],'codex',False,'same-profile')
    ordinary=b.actor_session(raw,'ordinary-unbound')
    assert b.claim_task(ordinary,legacy['id'])['claimed']


def ordinary_actor(f):
    b=f['b'];raw=b.authenticate(f['device']['token'])
    b.register_agent(raw,'ordinary-resource','Ordinary',[],'codex',False,'same-profile')
    return b.actor_session(raw,'ordinary-resource')


def test_open_refuses_existing_legacy_resource_lock_without_creating_family(family):
    f=family;b=f['b'];ordinary=ordinary_actor(f)
    legacy=b.create_task(f['h'],f['parent']['thread_id'],'Legacy owner','Read fixture','Evidence','group','alice',
        action='repo.read_file',resource='fixture:repo')
    assert b.claim_task(ordinary,legacy['id'])['claimed']
    with b._connection() as db:before=tuple(db.iterdump())
    assert reject(open_family,f)=='Delegation resource is held by a legacy task'
    with b._connection() as db:assert tuple(db.iterdump())==before


def test_open_refuses_unresolved_parent_runtime_from_previous_owner(family):
    f=family;b=f['b'];parent=f['parent']
    old=b.reserve_execution(f['actors'][0],'previous-owner',f['project']['id'],parent['id'],
        'previous-work','previous-turn','codex_chatgpt','fixture-model')['execution']
    parent=b.update_ticket(f['h'],parent['id'],parent['version'],status='todo',release_claim=True,
        assignee_owner_id='bob',assignee_agent_id=None)
    raw=b.authenticate(b.create_device(f['other'],'Bob fixture',[])['token']);actors=[]
    for session in ('bob-coordinator','bob-one','bob-two'):
        b.register_agent(raw,session,'Bob fixture',[],'codex',False,'bob-profile')
        actors.append(b.actor_session(raw,session))
    parent=b.claim_ticket(actors[0],parent['id'],parent['version'])
    children=[{**child,'session_id':actors[index+1]['session_id']} for index,child in enumerate(f['args']['children'])]
    args={**f['args'],'expected_version':parent['version'],'children':children}
    with b._connection() as db:before=tuple(db.iterdump())
    assert reject(b.open_delegation_family,actors[0],**args)=='Finish and confirm coordinator cleanup before delegation'
    with b._connection() as db:assert tuple(db.iterdump())==before
    b.finish_execution(f['actors'][0],old['id'],old['revision'],'cancelled',True)
    assert b.open_delegation_family(actors[0],**args)['owner_id']=='bob'


@pytest.mark.parametrize('kind',['legacy','ticket'])
def test_family_resource_survives_stop_and_claim_release_until_runtime_cleanup(family,kind):
    f=family;b=f['b'];opened=open_family(f);child=claimed(f,opened);ordinary=ordinary_actor(f)
    row=execution(f,child)
    pending=b.finish_execution(f['actors'][1],row['id'],row['revision'],'cancelled',False,'cleanup_unconfirmed')
    if kind=='legacy':
        outside=b.create_task(f['h'],child['thread_id'],'Legacy outsider','Read fixture','Evidence','group','alice',
            action='repo.read_file',resource='fixture:repo')
    else:outside=b.create_ticket(f['h'],f['project']['id'],'Ticket outsider',resource='fixture:repo',idempotency_key='outside')
    def blocked():
        with b._connection() as db:before=tuple(db.iterdump())
        if kind=='legacy':assert not b.claim_task(ordinary,outside['id'])['claimed']
        else:reject(b.claim_ticket,ordinary,outside['id'],outside['version'])
        with b._connection() as db:assert tuple(db.iterdump())==before
    blocked()
    b.stop_delegation_family(f['h'],opened['id'],opened['revision']);blocked()
    b.release_ticket(f['actors'][1],child['id'],child['version'],'release-child','Stopped',status='todo')
    parent=b.get_ticket(f['h'],f['parent']['id'])['ticket']
    b.release_ticket(f['actors'][0],parent['id'],parent['version'],'release-parent','Stopped',status='todo')
    blocked()
    assert b.get_runtime(f['h'])['active_count']==1
    b.finish_execution(f['actors'][1],row['id'],pending['revision'],'cancelled',True,'cleanup_unconfirmed')
    if kind=='legacy':assert b.claim_task(ordinary,outside['id'])['claimed']
    else:assert b.claim_ticket(ordinary,outside['id'],outside['version'])['claimant']['session_id']==ordinary['session_id']


@pytest.mark.parametrize('change',['title','description','acceptance_criteria','resource','assignment','blocked','done','foreign_claim'])
def test_pending_child_drift_stops_running_runtime_with_closed_reason_and_cleanup(family,change):
    f=family;b=f['b'];opened=open_family(f);child=claimed(f,opened)
    row=execution(f,child);live=b.start_execution(f['actors'][1],row['id'],row['revision'])
    fields={'title':'Changed','description':'Changed','acceptance_criteria':'Changed','resource':'fixture:other'}
    if change in fields:b.update_ticket(f['h'],child['id'],child['version'],**{change:fields[change]})
    elif change=='assignment':b.update_ticket(f['h'],child['id'],child['version'],assignee_owner_id='bob',assignee_agent_id=None)
    elif change=='blocked':b.update_ticket(f['h'],child['id'],child['version'],status='blocked')
    elif change=='done':b.update_ticket(f['h'],child['id'],child['version'],status='done',release_claim=True,outcome='Human changed',evidence='Fixture')
    else:
        with b._tx() as db:
            claimant=json.loads(db.execute('SELECT claimant FROM tickets WHERE id=?',(child['id'],)).fetchone()[0])
            claimant['session_id']='child-two'
            db.execute('UPDATE tickets SET claimant=? WHERE id=?',(json.dumps(claimant),child['id']))
    control=b.heartbeat_execution(f['actors'][1],row['id'],live['revision'])
    assert control['should_stop'] and control['control_reason']=='authorization_changed'
    assert b.get_delegation_family(f['actors'][0],opened['id'])['should_stop']
    assert b.finish_execution(f['actors'][1],row['id'],control['revision'],'cancelled',True,'policy_changed')['state']=='finished'
    assert b.get_runtime(f['h'])['active_count']==0


def test_family_admissions_count_once_and_last_allowed_start_is_valid(family):
    f=family;b=f['b'];opened=open_family(f);one=claimed(f,opened);two=claimed(f,opened,1)
    b.update_runtime_limits(f['h'],1,4,False)
    first=execution(f,one,key='first');b.finish_execution(f['actors'][1],first['id'],first['revision'],'cancelled',True)
    actor=f['actors'][2]
    last=execution(f,two,index=1,key='last')
    assert b.reserve_execution(actor,'last',f['project']['id'],two['id'],
        'work-last','turn-last','codex_chatgpt','fixture-model')['retried']
    live=b.start_execution(actor,last['id'],last['revision'])
    b.finish_execution(actor,last['id'],live['revision'],'completed',True,'completed')
    assert reject(execution,f,one,key='beyond')=='Delegation budget exhausted'
    assert b.get_runtime(f['h'])['active_count']==0


def test_concurrent_runtime_reservations_cannot_overspend_last_family_turn(family):
    f=family;b=f['b'];opened=open_family(f,limits={**f['args']['limits'],'max_concurrent':4})
    b.update_runtime_limits(f['h'],1,4,False)
    children=[claimed(f,opened,index) for index in (0,1)]
    initial=execution(f,children[0],key='already-spent')
    b.finish_execution(f['actors'][1],initial['id'],initial['revision'],'cancelled',True)
    barrier=threading.Barrier(2)
    def reserve(index):
        barrier.wait(timeout=5)
        try:return execution(f,children[index],index,key='race-'+str(index))
        except BoardError as error:return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(reserve,(0,1)))
    admitted=[value for value in results if isinstance(value,dict)]
    assert len(admitted)==1 and results.count('Delegation budget exhausted')==1
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0]==2
    assert b.get_runtime(f['h'])['active_count']==1
    winner=admitted[0];actor=next(actor for actor in f['actors'] if actor['session_id']==winner['session_id'])
    started=b.start_execution(actor,winner['id'],winner['revision'])
    b.finish_execution(actor,winner['id'],started['revision'],'completed',True,'completed')
    assert b.get_runtime(f['h'])['active_count']==0


@pytest.mark.parametrize('scope',['general','child','parent'])
def test_all_message_scopes_are_refused_to_dedicated_family_sessions_before_capacity_debit(family,scope):
    f=family;b=f['b'];opened=open_family(f);actor=f['actors'][0 if scope=='parent' else 1]
    if scope=='general':thread=b.create_thread(f['h'],'General',['alice'],'general')
    else:
        ticket=f['parent'] if scope=='parent' else b.get_ticket(actor,member(f,opened)['ticket_id'])['ticket']
        thread={'id':ticket['thread_id']}
    source=b.post_message(f['h'],thread['id'],'Reply',mentions=['agent:'+opened['agent_id']],idempotency_key='question-'+scope)
    sha=b.get_message(actor,thread['id'],source['id'])['source_sha256']
    reject(b.reserve_message_execution,actor,'general',thread['id'],source['id'],sha,'general-work','general-turn','codex_chatgpt','fixture-model')
    assert b.get_runtime(f['h'])['active_count']==0


def test_scope_change_between_reservation_and_start_never_activates(family):
    f=family;b=f['b'];opened=open_family(f);child=claimed(f,opened);row=execution(f,child)
    b.update_ticket(f['h'],child['id'],child['version'],description='Changed after admission')
    reject(b.start_execution,f['actors'][1],row['id'],row['revision'])
    assert b.get_execution_control(f['actors'][1],row['id'])['should_stop']
    b.finish_execution(f['actors'][1],row['id'],row['revision'],'cancelled',True)


def test_schema_ten_upgrade_is_atomic_and_preserves_old_runtime_rows(family,monkeypatch):
    import board as module
    f=family;b=f['b'];parent=f['parent']
    old=b.reserve_execution(f['actors'][0],'old',f['project']['id'],parent['id'],'old-work','old-turn','codex_chatgpt','fixture-model')['execution']
    with b._tx() as db:
        db.execute('DROP TABLE mcp_page_cursors');db.execute('ALTER TABLE message_feeds DROP COLUMN pending_view');db.execute('ALTER TABLE message_feeds DROP COLUMN pending_compact_json');
        db.execute('DROP TABLE delegation_members');db.execute('DROP TABLE delegation_families');db.execute('PRAGMA user_version=10')
        before=[tuple(row) for row in db.execute('SELECT * FROM runtime_executions')]
    original=module.migrate_delegation_families
    def fail(db):original(db);raise RuntimeError('synthetic family migration failure')
    with monkeypatch.context() as patch:
        patch.setattr(module,'migrate_delegation_families',fail)
        with pytest.raises(RuntimeError,match='synthetic family migration failure'):Board(b.data_dir)
    with sqlite3.connect(b.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==10
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='delegation_families'").fetchone()
        assert db.execute('SELECT * FROM runtime_executions').fetchall()==before
    restored=Board(b.data_dir)
    assert restored.lookup_execution(f['actors'][0],'old')['execution']==old
    with restored._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==13
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_backup_restores_family_claims_pending_runtime_and_private_plan(family,tmp_path):
    f=family;b=f['b'];opened=open_family(f);child=claimed(f,opened);row=execution(f,child)
    archive=tmp_path/'family.zip';assert create_backup(b.data_dir,archive)['schema']['user_version']==13
    destination=tmp_path/'restored';restore_backup(archive,destination);restored=Board(destination)
    assert restored.get_delegation_family(f['actors'][0],opened['id'])==opened
    assert restored.lookup_execution(f['actors'][1],'turn')['execution']==row
    assert restored.get_runtime(f['h'])['active_count']==1
    assert restored.get_ticket(f['h'],child['id'])['ticket']['claimant']['session_id']=='child-one'
