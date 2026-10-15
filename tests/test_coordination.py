from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import time

import pytest

from board import Board, BoardError


@pytest.fixture
def space(tmp_path):
    board=Board(tmp_path/'board', base_url='https://board.test')
    for owner in ('blackoutburst','thescreamer','outsider'):
        board.ensure_owner(owner, owner.title())
    human={'kind':'human','owner_id':'blackoutburst'}
    other={'kind':'human','owner_id':'thescreamer'}
    for actor in (human,other):
        board.set_policy(actor,True,True,True,True,[],[])
    project=board.create_project(human,'Coordination',['blackoutburst','thescreamer'],idempotency_key='p')
    device=board.create_device(human,'Work Mac',[])
    raw=board.authenticate(device['token'])
    board.register_agent(raw,'codex-one','Task A', ['python'],'codex', True,'installation')
    board.register_agent(raw,'codex-two','Task B', ['python'],'codex', True,'installation')
    first=board.actor_session(raw,'codex-one')
    second=board.actor_session(raw,'codex-two')
    return board,human,other,project,first,second,device


def new(space,title='Work',**kwargs):
    board,human,_,project,*_=space
    return board.create_ticket(human,project['id'],title,idempotency_key=title,**kwargs)


def reject(function,*args,expected_status=None,**kwargs):
    with pytest.raises(BoardError) as error:
        function(*args,**kwargs)
    if expected_status:
        assert error.value.status==expected_status


def test_identity_persists_across_chats_restart_and_distinguishes_clients_and_devices(space):
    board,human,_,project,first,second,device=space
    identities=board.list_agents(human,project['id'])
    assert len(identities)==1
    identity=identities[0]
    assert identity['name']=='Codex · Work Mac'
    assert len(identity['sessions'])==2
    raw=board.authenticate(device['token'])
    claude=board.register_agent(raw,'claude-chat','Claude chat',[],'claude-code',False,'installation')
    assert claude['identity']['id']!=identity['id']
    assert claude['identity']['name']=='Claude Code · Work Mac'
    board.rename_agent(human,identity['id'],'Codex · My work laptop')
    board=Board(board.data_dir,base_url=board.base_url)
    reconnected=board.register_agent(raw,'codex-three','New chat',[],'codex',True,'installation','Ignored rename')
    assert reconnected['identity']['id']==identity['id']
    assert reconnected['identity']['name']=='Codex · My work laptop'
    home=board.create_device(human,'Home PC',[])
    remote=board.register_agent(board.authenticate(home['token']),'home-chat','Home',[],'codex',True,'installation')
    assert remote['identity']['id']!=identity['id']
    assert remote['identity']['name']=='Codex · Home PC'


def test_dependency_graph_and_completion_gate(space):
    board,human,_,project,first,*_=space
    a=new(space,'First')
    b=new(space,'Second',depends_on=[a['id']])
    assert not b['ready'] and b['blocked_by'][0]['id']==a['id']
    reject(board.claim_ticket,first,b['id'],b['version'],expected_status=409)
    reject(board.update_ticket,human,b['id'],b['version'],status='done',outcome='x',evidence='x')
    reject(board.update_ticket,human,a['id'],a['version'],depends_on=[b['id']])
    private=board.create_project(human,'Private',['blackoutburst'],idempotency_key='private')
    foreign=board.create_ticket(human,private['id'],'Private task',idempotency_key='private-task')
    reject(board.update_ticket,human,b['id'],b['version'],depends_on=[foreign['id']])
    claimed=board.claim_next_ticket(first,'claim-first',project['id'])['ticket']
    assert claimed['id']==a['id']
    done=board.update_ticket(first,a['id'],claimed['version'],status='done',outcome='Implemented',evidence='Checks passed')
    assert done['status']=='done'
    assert board.get_work(first,project['id'])['ready'][0]['id']==b['id']
    assert board.claim_next_ticket(first,'claim-second',project['id'])['ticket']['id']==b['id']


def test_claim_next_is_atomic_and_retry_is_stable(space):
    board,human,_,project,first,second,*_=space
    task=new(space)
    barrier=threading.Barrier(2)
    def claim(actor):
        barrier.wait()
        return board.claim_next_ticket(actor,'claim-key',project['id'])
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(claim,[first,second]))
    assert sum(item['claimed'] for item in results)==1
    winner=first if results[0]['claimed'] else second
    retry=board.claim_next_ticket(winner,'claim-key',project['id'])
    assert retry['ticket']['id']==task['id'] and retry['retried']
    assert retry['ticket']['claimant']['agent_name']=='Codex · Work Mac'
    assert retry['ticket']['assignee_owner_id'] is None


def test_skill_resource_assignment_and_worker_opt_in(space):
    board,human,_,project,first,second,device=space
    specialized=new(space,'Needs Android',required_skills=['android'])
    task=new(space,'Shared resource',resource='repo:main')
    competing=new(space,'Other task',resource='repo:main')
    first_claim=board.claim_ticket(first,task['id'],task['version'])
    assert not board.get_work(second,project['id'])['ready']
    reject(board.claim_ticket,second,competing['id'],competing['version'],expected_status=409)
    board.update_ticket(first,task['id'],first_claim['version'],release_claim=True)
    raw=board.authenticate(device['token'])
    observer=board.register_agent(raw,'observer','Read only',[], 'codex',False,'installation')
    reject(board.claim_next_ticket,board.actor_session(raw,'observer'),'not-authorized',expected_status=403)
    claude=board.register_agent(raw,'claude','Claude',[], 'claude-code',True,'claude')
    targeted=new(space,'Codex only',assignee_agent_id=observer['identity']['id'])
    reject(board.claim_ticket,board.actor_session(raw,'claude'),targeted['id'],targeted['version'],expected_status=403)
    assert specialized['id'] not in [t['id'] for t in board.get_work(first)['ready']]


def test_expired_claim_requires_recovery_and_fences_old_updates(space):
    board,_,_,_,first,second,*_=space
    task=new(space)
    claim=board.claim_ticket(first,task['id'],task['version'])
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',(time.time()-1,task['id']))
    reject(board.renew_ticket,first,task['id'],claim['version'],expected_status=409)
    reject(board.update_ticket,first,task['id'],claim['version'],progress='Old writer',expected_status=409)
    reject(board.claim_ticket,second,task['id'],claim['version'],expected_status=409)
    assert board.get_work(second)['recovery'][0]['id']==task['id']
    recovered=board.claim_ticket(second,task['id'],claim['version'],'Inspected checkout and remote effects; no pending writes')
    assert recovered['version']>claim['version']
    reject(board.update_ticket,first,task['id'],recovered['version'],progress='Old writer',expected_status=403)
    renewed=board.renew_ticket(second,task['id'],recovered['version'],'Recovered and testing')
    assert renewed['progress']=='Recovered and testing'


def test_human_request_blocks_releases_notifies_and_resumes(space):
    board,human,other,project,first,second,*_=space
    task=new(space)
    claim=board.claim_ticket(first,task['id'],task['version'])
    request=board.request_human_input(first,task['id'],'thescreamer','Which account should own the project?','input-key')
    waiting=board.get_ticket(human,task['id'])['ticket']
    assert waiting['waiting_on_human'] and waiting['claimant'] is None
    assert board.request_human_input(first,task['id'],'thescreamer','Which account should own the project?','input-key')['id']==request['id']
    assert not board.get_work(second,project['id'])['ready']
    reject(board.claim_ticket,second,task['id'],waiting['version'],expected_status=409)
    reject(board.resolve_human_request,human,request['id'],'Not my question',expected_status=403)
    board.resolve_human_request(other,request['id'],'Use the team account')
    resumed=board.get_work(second,project['id'])['ready'][0]
    assert resumed['id']==task['id']
    discussion=board.get_thread(human,task['thread_id'])['messages']
    assert len(discussion)==2
    assert discussion[0]['author']['agent_name']=='Codex · Work Mac'
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_key=?",('human-request:'+request['id'],)).fetchone()[0]==1
    events=board.list_activity(human,ticket_id=task['id'])
    assert {'ticket_created','ticket_claimed','human_input_requested','human_input_resolved'}.issubset({e['event'] for e in events})


def test_pairing_is_hashed_expiring_atomic_and_human_only(space):
    board,human,_,_,first,*_=space
    reject(board.create_pairing,first,'Device',expected_status=403)
    pairing=board.create_pairing(human,'Home PC')
    with sqlite3.connect(board.db_path) as db:
        assert pairing['code'] not in db.execute('SELECT code_hash FROM pairing_codes').fetchone()[0]
    barrier=threading.Barrier(2)
    def exchange(_):
        barrier.wait()
        try: return board.exchange_pairing(pairing['code'])
        except BoardError: return None
    with ThreadPoolExecutor(2) as pool:
        result=list(pool.map(exchange,[1,2]))
    assert sum(item is not None for item in result)==1
    connected=next(item for item in result if item)
    assert board.authenticate(connected['token'])['owner_id']=='blackoutburst'
    expires=board.create_pairing(human,'Expired')
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE pairing_codes SET expires_at=0 WHERE consumed_at IS NULL')
    reject(board.exchange_pairing,expires['code'])


def test_project_and_owner_boundaries(space):
    board,human,other,project,first,*_=space
    task=new(space)
    outsider={'kind':'human','owner_id':'outsider'}
    reject(board.get_ticket,outsider,task['id'],expected_status=404)
    reject(board.list_agents,outsider,project['id'],expected_status=404)
    reject(board.list_activity,outsider,project_id=project['id'],expected_status=404)
    reject(board.update_project,first,project['id'],project['version'],automation_enabled=False,expected_status=403)
    board.update_project(human,project['id'],project['version'],automation_enabled=False)
    assert board.get_work(first,project['id'])['ready']==[]
    board.set_policy(human,True,True,True,False,[],[])
    reject(board.claim_next_ticket,first,'denied',project['id'],expected_status=403)


def test_assignment_consistency_and_removed_member_work_visibility(space):
    board,human,other,project,first,*_=space
    identity=board.list_agents(human,project['id'])[0]['id']
    reject(new,space,'Contradictory assignment',assignee_owner_id='thescreamer',assignee_agent_id=identity)
    task=new(space,'Assigned work',assignee_agent_id=identity)
    reject(board.update_ticket,human,task['id'],task['version'],assignee_owner_id='thescreamer')
    board.claim_ticket(first,task['id'],task['version'])
    assert len(board.get_work(first)['assigned'])==1
    with sqlite3.connect(board.db_path) as db:
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',(project['id'],'blackoutburst'))
    assert board.get_work(first)['assigned']==[]


def test_windows_agent_delegates_to_linux_agent_then_resumes_without_human(space):
    board,human,other,project,windows,_,device=space
    board.register_agent(board.authenticate(device['token']),'codex-one','Windows task',['windows'],'codex',True,'installation')
    linux_device=board.create_device(other,'Linux server',[])
    linux_raw=board.authenticate(linux_device['token'])
    board.register_agent(linux_raw,'linux-worker','Linux worker',['linux'],'claude-code',True,'linux')
    linux=board.actor_session(linux_raw,'linux-worker')
    parent=new(space,'Windows parent',required_skills=['windows'],resource='parent-workspace')
    claimed=board.claim_ticket(windows,parent['id'],parent['version'])
    delegated=board.delegate_task(windows,parent['id'],claimed['version'],'Run Linux tool',
        'Run the Linux-only verification and attach the exact output.',['linux'],'delegate-linux',
        'The tool succeeds and output is available.','linux-tool')
    assert delegated['parent']['work_state']=='blocked'
    assert delegated['parent']['claimant'] is None
    assert delegated['parent']['depends_on']==[delegated['child']['id']]
    assert not board.get_work(windows)['ready']
    retry=board.delegate_task(windows,parent['id'],claimed['version'],'Run Linux tool',
        'Run the Linux-only verification and attach the exact output.',['linux'],'delegate-linux',
        'The tool succeeds and output is available.','linux-tool')
    assert retry['child']['id']==delegated['child']['id']
    assert retry['retried']
    linux_claim=board.claim_next_ticket(linux,'linux-claim',project['id'])['ticket']
    assert linux_claim['id']==delegated['child']['id']
    assert linux_claim['claimant']['agent_name']=='Claude Code · Linux server'
    board.update_ticket(linux,linux_claim['id'],linux_claim['version'],status='done',
        outcome='Linux tool completed',evidence='Exit status 0; exact output recorded')
    ready=board.get_work(windows,project['id'])['ready']
    assert len(ready)==1 and ready[0]['id']==parent['id']
    assert ready[0]['assignee_agent_id']==claimed['claimant']['agent_id']
    assert board.get_work(linux,project['id'])['ready']==[]
    resumed=board.claim_next_ticket(windows,'resume-parent',project['id'])['ticket']
    assert resumed['id']==parent['id']
    assert resumed['claimant']['agent_id']==claimed['claimant']['agent_id']


def test_schema_v2_upgrade_preserves_data_and_active_claims(tmp_path):
    from pathlib import Path
    path=tmp_path/'board'
    path.mkdir()
    database=path/'board.sqlite3'
    fixture=Path(__file__).parent/'fixtures'/'schema_v2.sql'
    policy=json.dumps({'enabled':True,'allow_read':True,'allow_reply':True,'allow_requests':True,'actions':[],'resources':[]})
    with sqlite3.connect(database) as db:
        db.executescript(fixture.read_text())
        db.execute('INSERT INTO owners VALUES(?,?,NULL,?)',('blackoutburst','Blackoutburst',policy))
        db.execute('INSERT INTO projects(id,name,description,context,repository_url,environment,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            ('old-project','Existing project','Keep this','Original context','','',1,1))
        db.execute('INSERT INTO project_members VALUES(?,?)',('old-project','blackoutburst'))
        db.execute('INSERT INTO threads VALUES(?,?,?,?)',('old-thread','Original discussion',1,1))
        db.execute('INSERT INTO thread_members VALUES(?,?)',('old-thread','blackoutburst'))
        db.execute("""INSERT INTO tickets(id,project_id,number,thread_id,title,description,acceptance_criteria,priority,status,claimant,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",('old-ticket','old-project',1,'old-thread','Existing task','Keep progress','Keep checks','normal','in_progress',
                json.dumps({'kind':'agent','owner_id':'blackoutburst','device_id':'old-device','session_id':'old-session'}),1,1))
        db.execute("INSERT INTO messages(id,thread_id,body,author,created_at,mentions,attachments) VALUES(?,?,?,?,?,'[]','[]')",('old-message','old-thread','Keep discussion',json.dumps({'kind':'human','owner_id':'blackoutburst'}),1))
    upgraded=Board(path)
    with sqlite3.connect(upgraded.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==3
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert db.execute('SELECT body FROM messages').fetchone()[0]=='Keep discussion'
        assert db.execute('SELECT title,lease_expires_at FROM tickets').fetchone()[0]=='Existing task'
        assert db.execute('SELECT lease_expires_at FROM tickets').fetchone()[0]>time.time()
        assert db.execute('SELECT context FROM projects').fetchone()[0]=='Original context'
