"""Real Pool/Board/Work/RunStore/Runtime, qualified policy and models synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

import pytest

from test_board_worker import Board,Client
from test_runtime_integration import ControlClient
from blackoutburst_agents import team_role_pool as module
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.team_role_contract import role_result
from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.worker_pool import WorkerPool
from blackoutburst_agents.pool_journal import PoolError


def write(path,value):
    with PrivateDirectory(path.parent) as private:private.create(path.name,canonical(value))


@pytest.fixture
def role_case(tmp_path,monkeypatch):
    base=tmp_path.resolve();board=Board(base/'board',base_url='https://board.test');board.ensure_owner('owner','Owner')
    human={'kind':'human','owner_id':'owner'};resource='team:manager'
    board.set_policy(human,True,True,True,True,[],[resource])
    board.update_runtime_limits(human,board.get_runtime(human)['limits']['version'],4,False)
    project=board.create_project(human,'Team',['owner'],idempotency_key='project')
    ticket=board.create_ticket(human,project['id'],'Plan explicit protected slots',resource=resource,idempotency_key='manager')
    device=board.create_device(human,'Synthetic manager',[]);raw=board.authenticate(device['token'])
    seed=Client(board,raw,'seed');registration=seed.register()
    root=base/'operator';root.mkdir(mode=0o700)
    connection=root/'connection.json';write(connection,{'synthetic':'never usable credentials'})
    inputs=root/'slots.json';catalog={'schema':'team-manager-slots-v1','slots':[
        {'slot_id':'coder0','resource':'repo:coder0','agent_id':'coder-profile-0','model':'explicit-child-model'},
        {'slot_id':'coder1','resource':'repo:coder1','agent_id':'coder-profile-1','model':'explicit-child-model'}]}
    write(inputs,catalog)
    policy_dir=root/'policy';policy_dir.mkdir(mode=0o700);workspace=root/'empty';workspace.mkdir(mode=0o700)
    for name in ('planner-plan.json','planner-attempt.json','planner-qualification.json'):write(policy_dir/name,{'synthetic':name})
    f=type('Case',(),{})();f.board=board;f.human=human;f.project=project;f.ticket=ticket;f.root=root
    f.connection=connection;f.inputs=inputs;f.policy_dir=policy_dir;f.workspace=workspace;f.clients=[];f.controls=[]
    f.calls=[];f.during=None;f.cleanup=True;f.policy_valid=True;f.claim_loss=False
    f.output={'schema':'team-manager-plan-v1','steps':[
        {'step_id':'first','slot_id':'coder0','title':'First isolated task','acceptance_criteria':'First tests pass','depends_on':[]},
        {'step_id':'second','slot_id':'coder1','title':'Independent task','acceptance_criteria':'Second tests pass','depends_on':['first']}]}
    def client(path,*,session_id,timeout):
        assert str(path)==str(connection)
        value=Client(board,raw,session_id);value.connection_identity=tuple(file_document(connection)[1]['identity'])
        value.register_initial=value.register;value.close=lambda:None
        original=value.call
        def call(name,args):
            result=original(name,args)
            if name=='claim_ticket' and f.claim_loss:f.claim_loss=False;raise TimeoutError('synthetic lost claim')
            return result
        value.call=call;f.clients.append(value);return value
    monkeypatch.setattr(module.DevelopmentBoardClient,'from_connection_file',client)
    def control(client):
        value=ControlClient(board,raw,client.session_id);value.close=lambda:None
        f.controls.append(value);return value
    monkeypatch.setattr(module.RuntimeBoardClient,'from_worker',control)
    class Policy:
        fingerprint='b'*64
        executable=str(Path(sys.executable).resolve())
        def __init__(self,directory):assert str(directory)==str(policy_dir);self.workspace=str(workspace)
        def __enter__(self):return self
        def __exit__(self,*_):self.close()
        def close(self):pass
        def validate_for(self,executable,root):
            assert executable==self.executable and root==self.workspace and not list(workspace.iterdir())
            if not f.policy_valid:raise DevelopmentError('synthetic_policy_unqualified')
    monkeypatch.setattr(module,'LinuxPlannerPolicy',Policy)
    def transport(settings,prompt,root,*,planner_policy,**callbacks):
        assert settings.model=='explicit-manager-model'
        callbacks['before_prompt']();callbacks['on_started'](999999999)
        sid=callbacks['resume_session_id'] or str(uuid.uuid4());callbacks['on_session'](sid)
        f.calls.append({'thread':threading.get_ident(),'prompt':prompt,'resume':callbacks['resume_session_id']})
        if f.during:f.during(callbacks)
        return {'status':'completed','reason':'completed','session_id':sid,'text':json.dumps(f.output),
            'verification':{'tool_free':True,'cleanup_confirmed':f.cleanup}}
    monkeypatch.setattr(module,'execute_turn',transport)
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('No native/model subprocess'))
    driver=module.TeamRolePoolDriver(root/'selections',role='manager',connection_file=connection,
        owner_id='owner',agent_id=registration['identity']['id'],board_endpoint=Client.endpoint)
    pool=WorkerPool(root/'pool',concurrency=2,ceiling=4,drivers={'manager':driver.binding()})
    job=pool.submit(request_id='manager',driver_id='manager',payload={'project_id':project['id'],'ticket_id':ticket['id']})
    selection={'schema':1,'role':'manager','operation_id':job['id'],'worker_id':job['worker_id'],'session_id':job['session_id'],
        'scope':{**driver.fixed,'project_id':project['id'],'ticket_id':ticket['id'],'board_session_id':job['session_id']},
        'resource':resource,'model':'explicit-manager-model','instruction':'Produce only a private plan for these slots.',
        'timeout':10,'planner_policy_directory':str(policy_dir),'input_file':str(inputs)}
    selected=root/'selection.json';write(selected,selection)
    f.driver=driver;f.pool=pool;f.job=job;f.selection=selection;f.selected=selected;f.catalog=catalog
    try:yield f
    finally:
        pool.shutdown(timeout=5)
        try:pool.close()
        except Exception:pass


def finished(f):
    f.pool.dispatch_pending();deadline=time.monotonic()+20
    while f.pool.status()['live_threads'] and time.monotonic()<deadline:time.sleep(.01)
    assert f.pool.status()['live_threads']==0
    return f.pool.status()


def operation(f):
    job=f.pool.journal.read()['jobs'][0];context=f.pool._context(job)
    with WorkJournal(context.journals['work']) as journal:return journal.get(job['id'])


def test_manager_runs_real_journals_and_runtime_but_only_retains_private_closed_plan(role_case):
    f=role_case;before=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
    assert f.driver.bind_selection(f.job,f.selected)['status']=='bound'
    result=finished(f);op=operation(f)
    assert result['jobs'][0]['outcome']=='complete',(result,op)
    assert result['capacity_held']==0 and op['stage']=='complete'
    assert op['payload']['private_result']==f.output and len(f.calls)==1
    ticket=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
    assert ticket['status']=='in_progress' and ticket['outcome']==before['outcome'] and ticket['evidence']==before['evidence']
    assert not {'update_ticket','release_ticket','create_ticket','post_message'} & {name for client in f.clients for name,_ in client.calls}
    rows=f.board.get_runtime(f.human)['executions']
    assert len(rows)==1 and rows[0]['state']=='finished' and rows[0]['cleanup_confirmed'] is True
    assert f.calls[0]['thread']!=threading.get_ident() and f.calls[0]['resume'] is None


def test_lost_claim_response_does_not_repeat_claim_or_model(role_case):
    f=role_case;f.driver.bind_selection(f.job,f.selected);f.claim_loss=True
    result=finished(f)
    assert result['jobs'][0]['outcome']=='complete',(result,operation(f))
    assert sum(name=='claim_ticket' for client in f.clients for name,_ in client.calls)==1 and len(f.calls)==1


@pytest.mark.parametrize('change',['input','policy','connection','profile','ticket'])
def test_changed_selection_or_authority_never_starts_model(role_case,change):
    f=role_case;f.driver.bind_selection(f.job,f.selected)
    if change=='input':
        with PrivateDirectory(f.inputs.parent) as private:private.replace(f.inputs.name,canonical({**f.catalog,'slots':f.catalog['slots'][:1]}))
    elif change=='connection':
        with PrivateDirectory(f.connection.parent) as private:private.replace(f.connection.name,canonical({'synthetic':'changed connection'}))
    elif change=='policy':f.policy_valid=False
    elif change=='profile':
        profile=f.board.get_agent_profile(f.human,f.driver.fixed['agent_id'])
        f.board.update_agent_profile(f.human,profile['id'],profile['version'],instructions='Changed after selection')
    else:
        ticket=f.board.get_ticket(f.human,f.ticket['id'])['ticket'];f.board.update_ticket(f.human,ticket['id'],ticket['version'],description='Changed selected scope')
    result=finished(f)
    assert result['jobs'][0]['outcome']!='complete' and f.calls==[]


def test_pool_stop_during_model_cancels_and_never_seals_plan(role_case):
    f=role_case;f.driver.bind_selection(f.job,f.selected)
    def stopping(callbacks):f.pool.stop(f.job['id']);assert callbacks['should_cancel']() is True
    f.during=stopping;result=finished(f)
    assert result['jobs'][0]['outcome']=='needs_review' and 'private_result' not in operation(f)['payload']
    assert f.pool.status()['capacity_held']==0


def test_unknown_real_cleanup_retains_capacity_and_never_replays_model(role_case):
    f=role_case;f.driver.bind_selection(f.job,f.selected);f.cleanup=False
    result=finished(f)
    assert result['capacity_held']==1 and result['retained']==1
    with pytest.raises(PoolError,match='pool_reconciliation_unconfirmed'):f.pool.reconcile(f.job['id'])
    assert len(f.calls)==1 and f.pool.status()['capacity_held']==1


@pytest.mark.parametrize('change',['cycle','unknown_slot','unknown_dependency','duplicate_step','extra_permission'])
def test_manager_untrusted_plan_cannot_escape_catalog_or_closed_dag(role_case,change):
    f=role_case;f.driver.bind_selection(f.job,f.selected)
    if change=='cycle':f.output['steps'][0]['depends_on']=['second']
    elif change=='unknown_slot':f.output['steps'][0]['slot_id']='unapproved'
    elif change=='unknown_dependency':f.output['steps'][0]['depends_on']=['missing']
    elif change=='duplicate_step':f.output['steps'][1]['step_id']='first'
    else:f.output['steps'][0]['actions']=['repo.patch_file']
    result=finished(f)
    assert result['jobs'][0]['outcome']=='needs_review' and 'private_result' not in operation(f)['payload']
    assert result['capacity_held']==0 and len(f.calls)==1
