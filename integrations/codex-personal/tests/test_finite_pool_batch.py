"""Real four-job Pool/Board/Git composition; qualification/text are synthetic."""
from pathlib import Path
import os
import subprocess
import threading
import uuid

import pytest

from test_board_worker import Client
from test_git_workspace import git
from test_runtime_integration import ControlClient
from blackoutburst_agents import finite_pool_batch as module,linux_pool_preparation as preparation
from blackoutburst_agents.dev_manifest import Budgets,DevelopmentError,canonical,digest
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.linux_input_plan import pin_linux_input_plan
from blackoutburst_agents.linux_test_backend import PLAN_SCHEMA


def write(path,value):
    with PrivateDirectory(path.parent) as private:private.create(path.name,canonical(value))


@pytest.fixture
def batch_case(monkeypatch):
    from test_development_pool import PoolDevelopmentTests
    popen=subprocess.Popen
    fixture=PoolDevelopmentTests();fixture.setUp();fixture.pool.close();fixture.patchers[-1].stop()
    f=fixture;root=f.base/'addon';root.mkdir(mode=0o700)
    for name in ('batches','connections','repositories','checkouts','git-state','verifiers','config'):(root/name).mkdir(mode=0o700)
    raw_by_path={};jobs=[];batch_id=str(uuid.uuid4());f.native_calls=[];f.prepare_hook=lambda *_:None
    source=root/'repositories'/'synthetic';source.mkdir(mode=0o700)
    def only_git(argv,*args,**kwargs):
        assert argv[0]=='/usr/bin/git','No native/model process allowed'
        return popen(argv,*args,**kwargs)
    monkeypatch.setattr(subprocess,'Popen',only_git)
    git(source,'init','--quiet','--initial-branch=main');git(source,'config','user.name','Synthetic author')
    git(source,'config','user.email','synthetic@example.invalid');(source/'one.txt').write_text('one\n')
    git(source,'add','one.txt');git(source,'commit','--quiet','-m','Synthetic base');base=git(source,'rev-parse','HEAD')
    verifier=root/'verifiers'/'fixed.py'
    with PrivateDirectory(verifier.parent) as private:private.create(verifier.name,b'from pathlib import Path\nassert Path("one.txt").read_text()=="one\\n"\n')
    for index in range(4):
        name='coder'+str(index);device=f.board.create_device(f.human,name,[]);raw=f.board.authenticate(device['token'])
        registration=Client(f.board,raw,'seed-'+name).register();connection=root/'connections'/(name+'.json')
        write(connection,{'synthetic':name});raw_by_path[str(connection)]=raw
        resource='repo:'+name
        ticket=f.board.create_ticket(f.human,f.project['id'],name,resource=resource,idempotency_key=name)
        jobs.append({'name':name,'agent_id':registration['identity']['id'],'connection_file':str(connection),
            'repository':{'owner_id':'owner','repository_id':'synthetic-repository','repository':str(source),
                'workspace_root':str(root/'checkouts'/batch_id/name),'base_commit':base},
            'project_id':f.project['id'],'ticket_id':ticket['id'],'paths':['one.txt'],'resource':resource,
            'actions':['repo.read_file','repo.patch_file','repo.run_test'],'verifier_path':str(verifier),
            'verifier_sha256':digest(verifier.read_bytes()),'test_name':'unit','test_timeout_seconds':2,
            'test_max_output_bytes':64,'instruction':'Inspect only the selected synthetic file.',
            'model':'fixture-model','planner_budgets':PlannerBudgets().document(),'broker_budgets':Budgets().document()})
    f.board.set_policy(f.human,True,True,True,True,['repo.read_file','repo.patch_file','repo.run_test'],[job['resource'] for job in jobs])
    def client(path,*,session_id,timeout):
        raw=raw_by_path[str(path)];value=Client(f.board,raw,session_id)
        value.connection_identity=tuple(file_document(path)[1]['identity']);value.register_initial=value.register
        value.adopt_registration=lambda:setattr(value,'actor',f.board.actor_session(raw,session_id));value.close=lambda:None
        f.clients.append((threading.get_ident(),value));return value
    def control(client):
        value=ControlClient(f.board,client.raw,client.session_id);value.close=lambda:None;value.lost=f.loss
        f.controls.append((threading.get_ident(),value));return value
    monkeypatch.setattr(preparation.DevelopmentBoardClient,'from_connection_file',client)
    monkeypatch.setattr('blackoutburst_agents.development_pool.RuntimeBoardClient.from_worker',control)
    def public_read(path,*,maximum,expected=None):
        assert str(path)==str(verifier);raw,pin=file_document(path,maximum)
        if expected is not None and pin!=expected:raise DevelopmentError('verifier_changed')
        return raw,pin
    monkeypatch.setattr(preparation,'read_public',public_read)
    def plan(*,root,scripts_root,scripts,interpreter,unshare):
        inputs=pin_linux_input_plan(root=root,scripts_root=scripts_root,scripts=scripts).document()
        return {'schema':PLAN_SCHEMA,'root':root,'root_identity':inputs['root_chain'][-1]['identity'],
            'input_plan':inputs,'input_plan_sha256':digest(canonical(inputs)),'environment':{'synthetic':True}}
    monkeypatch.setattr(preparation,'pin_linux_execution_plan',plan)
    monkeypatch.setattr('blackoutburst_agents.linux_test_backend.validate_current_plan',lambda *_:None)
    def policy_plan(directory,*,workspace,checkout,executable,cli_version,interpreter):
        directory.mkdir(mode=0o700)
        write(directory/'planner-plan.json',{'workspace':str(workspace),'checkout':checkout,'executable':executable})
        write(directory/'planner-qualification.json',None)
    monkeypatch.setattr(preparation,'prepare_linux_planner_policy',policy_plan)
    class Backend:
        environment={'synthetic':True}
        def __init__(self,directory,*,manifest,authorize_scope):self.directory=Path(directory);self.manifest=manifest;self.authorize=authorize_scope
        def qualify(self):
            f.native_calls.append(('backend',self.manifest['scope']['session_id']));f.prepare_hook('backend',self)
            assert self.authorize(self.manifest['scope'],'repo.run_test',self.manifest['resource'])
            with PrivateDirectory(self.directory) as private:private.replace('qualification.json',canonical({'synthetic':True}))
            plan=decode(file_document(self.directory/'execution-plan.json')[0]);policy=self.directory.parent/'policy';planner=self.directory.parent/'planner'
            f.data[self.manifest['scope']['session_id']]={'plan':plan,'plan_hash':digest(canonical(plan)),
                'policy':str(policy),'policy_hash':digest(file_document(policy/'planner-plan.json')[0]),
                'planner':planner,'planner_identity':(planner.stat().st_dev,planner.stat().st_ino),
                'actions':[{'schema':1,'kind':'test','name':'unit'},{'schema':1,'kind':'final','summary':'verified'}]}
        def qualified_for(self,*_):return True
        def close(self):pass
    class Policy:
        environment={'synthetic':True}
        def __init__(self,directory):self.directory=Path(directory)
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def qualify(self):
            f.native_calls.append(('policy',str(self.directory)));f.prepare_hook('policy',self)
            with PrivateDirectory(self.directory) as private:private.replace('planner-qualification.json',canonical({'synthetic':True}))
        def validate_for(self,*_):return None
    monkeypatch.setattr(preparation,'SupervisedLinuxNamedPythonBackend',Backend);monkeypatch.setattr(preparation,'LinuxPlannerPolicy',Policy)
    tools={}
    for name in ('codex','python','unshare'):
        path=f.protected/name;path.write_bytes(b'never executed synthetic '+name.encode());path.chmod(0o700);tools[name]=path
    config={'schema':module.SCHEMA,'batch_id':batch_id,'owner_id':'owner','board_endpoint':Client.endpoint,
        'timeout_seconds':60,'jobs':jobs}
    path=root/'config'/'batch.json';write(path,config)
    batch=module.FiniteLinuxPoolBatch(root,owner_id='owner',board_endpoint=Client.endpoint,
        executable=tools['codex'],cli_version='codex-cli 0.163.0',interpreter=tools['python'],unshare=tools['unshare'])
    f.batch=batch;f.batch_root=root;f.config=config;f.config_path=path;f.jobs=jobs;f.source=source;f.tools=tools
    try:yield f
    finally:monkeypatch.undo();f.doCleanups()


def test_four_explicit_jobs_really_prepare_and_run_together_with_verified_private_results(batch_case):
    f=batch_case;barrier=threading.Barrier(4);seen=set();lock=threading.Lock();observations=[]
    def during(session,callbacks):
        with lock:first=session not in seen;seen.add(session)
        if first:
            barrier.wait(timeout=10)
            observations.append((f.batch.pool.status()['executing_reported'],f.board.get_runtime(f.human)['active_count']))
            barrier.wait(timeout=10)
    f.during=during;result=f.batch.run(f.config_path)
    assert result['status']=='complete',result
    assert observations==[(4,4)]*4 and len({thread for _,thread in f.calls})==4
    assert len(f.calls)==8 and len(f.native_calls)==8
    assert result['pool']['capacity_held']==0 and result['pool']['live_threads']==0
    assert len({job['id'] for job in result['pool']['jobs']})==4
    assert len({job['worker_id'] for job in result['pool']['jobs']})==4
    assert len({job['session_id'] for job in result['pool']['jobs']})==4
    runtime=f.board.get_runtime(f.human)
    assert len(runtime['executions'])==16 and all(row['state']=='finished' and row['cleanup_confirmed'] is True for row in runtime['executions'])
    assert (f.source/'one.txt').read_text()=='one\n' and result['ticket_published'] is False
    assert all(f.board.get_ticket(f.human,job['ticket_id'])['ticket']['status']=='in_progress' for job in f.jobs)


def test_completed_batch_never_reprepares_or_redispatches(batch_case,monkeypatch):
    f=batch_case;first=f.batch.run(f.config_path)
    monkeypatch.setattr(module,'WorkerPool',lambda *_a,**_k:pytest.fail('reopened pool to replay completed batch'))
    assert f.batch.run(f.config_path)=={**first,'historical':True}
    assert len(f.calls)==8 and len(f.native_calls)==8


def test_prepare_loss_after_real_binding_never_dispatches_and_crash_retry_is_history_only(batch_case,monkeypatch):
    f=batch_case;bind=preparation.LinuxPoolJobPreparer.prepare;calls=[]
    def lose(self,*args,**kwargs):
        result=bind(self,*args,**kwargs);calls.append(result)
        if result['historical'] is False:raise TimeoutError('lost completion after bind')
        return result
    monkeypatch.setattr(preparation.LinuxPoolJobPreparer,'prepare',lose)
    with pytest.raises(TimeoutError):f.batch.run(f.config_path)
    assert calls and f.calls==[]
    root=f.batch_root/'batches'/f.config['batch_id']
    assert not (root/'dispatch-intent.json').exists()
    assert list(root.glob('selection-*/*.json'))
    result=f.batch.run(f.config_path)
    assert result['status']=='needs_review' and result['reason']=='interrupted_batch'
    assert len(calls)==1 and f.calls==[]


def test_stop_during_qualification_retains_attempt_and_starts_no_turn(batch_case):
    f=batch_case;stop=threading.Event()
    f.prepare_hook=lambda stage,_:stop.set() if stage=='backend' else None
    with pytest.raises(Exception):f.batch.run(f.config_path,should_stop=stop.is_set)
    assert f.calls==[] and not (f.batch_root/'batches'/f.config['batch_id']/'dispatch-intent.json').exists()
    assert f.batch.run(f.config_path)['reason']=='interrupted_batch'


def test_config_mutation_during_preparation_stops_before_dispatch(batch_case):
    f=batch_case
    def mutate(stage,_):
        if stage=='backend':
            with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical({**f.config,'timeout_seconds':61}))
    f.prepare_hook=mutate
    with pytest.raises(Exception):f.batch.run(f.config_path)
    assert f.calls==[] and not (f.batch_root/'batches'/f.config['batch_id']/'dispatch-intent.json').exists()


@pytest.mark.parametrize('change',['duplicate_profile','duplicate_resource','outside_connection','outside_repository','outside_verifier','unknown_field','not_four'])
def test_closed_operator_config_refuses_before_intent_or_registration(batch_case,change):
    f=batch_case;value=decode(canonical(f.config))
    if change=='duplicate_profile':value['jobs'][1]['agent_id']=value['jobs'][0]['agent_id']
    elif change=='duplicate_resource':value['jobs'][1]['resource']=value['jobs'][0]['resource']
    elif change=='outside_connection':value['jobs'][0]['connection_file']=str(f.protected/'unapproved.json')
    elif change=='outside_repository':value['jobs'][0]['repository']['repository']=str(f.base/'old-host')
    elif change=='outside_verifier':value['jobs'][0]['verifier_path']=str(f.protected/'old-private.py')
    elif change=='unknown_field':value['jobs'][0]['routing']='untrusted'
    else:value['jobs'].pop()
    with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical(value))
    with pytest.raises(Exception):f.batch.run(f.config_path)
    assert f.clients==[] and f.native_calls==[] and list((f.batch_root/'batches').iterdir())==[]


def test_stop_before_start_writes_no_submit_or_native_intent(batch_case):
    f=batch_case
    with pytest.raises(DevelopmentError,match='finite_pool_stopped'):f.batch.run(f.config_path,should_stop=lambda:True)
    root=f.batch_root/'batches'/f.config['batch_id']
    assert (root/'intent.json').exists() and not (root/'submit-intent.json').exists()
    assert f.clients==[] and f.native_calls==[]


def test_all_four_completion_receipts_required_before_dispatch(batch_case):
    f=batch_case
    def remove_first(stage,_):
        if stage=='policy' and len(f.native_calls)==8:
            root=f.batch_root/'batches'/f.config['batch_id']
            next(root.glob('preparation-coder0/*/complete.json')).unlink()
    f.prepare_hook=remove_first
    with pytest.raises(Exception):f.batch.run(f.config_path)
    assert len(f.native_calls)==8 and f.calls==[]
    assert not (f.batch_root/'batches'/f.config['batch_id']/'dispatch-intent.json').exists()


@pytest.mark.parametrize('boundary',['submit','dispatch'])
def test_crash_after_effect_intent_is_never_replayed(batch_case,monkeypatch,boundary):
    f=batch_case
    class Crash(BaseException):pass
    if boundary=='submit':
        original=module.WorkerPool.submit
        def die(self,**kwargs):original(self,**kwargs);raise Crash()
        monkeypatch.setattr(module.WorkerPool,'submit',die)
    else:monkeypatch.setattr(module.WorkerPool,'dispatch_pending',lambda _:(_ for _ in ()).throw(Crash()))
    with pytest.raises(Crash):f.batch.run(f.config_path)
    before=len(f.native_calls)
    assert f.batch.run(f.config_path)['reason']=='interrupted_batch'
    assert len(f.native_calls)==before and f.calls==[]


def test_stop_during_actual_turn_returns_no_false_complete(batch_case):
    f=batch_case;stop=threading.Event()
    def stopping(session,callbacks):stop.set();f.batch.pool.stop()
    f.during=stopping
    result=f.batch.run(f.config_path,should_stop=stop.is_set)
    assert result['status']=='needs_review' and result['pool']['stopped'] is True
    assert result['ticket_published'] is False and len(f.calls)<=4


def test_unknown_cleanup_remains_charged_not_successful_shutdown(batch_case):
    f=batch_case;f.cleanup=False
    result=f.batch.run(f.config_path)
    assert result['status']=='needs_review' and result['pool']['capacity_held']>0
    assert result['pool']['retained']>0
    assert any(job['cleanup_confirmed'] is not True for job in result['pool']['jobs'])


def test_root_replacement_refuses_before_registration(batch_case):
    f=batch_case;root=f.batch_root/'connections';root.rename(root.with_name('connections-old'));root.mkdir(mode=0o700)
    with pytest.raises(Exception):f.batch.run(f.config_path)
    assert f.clients==[] and f.native_calls==[]


@pytest.mark.parametrize('change',['tool','source'])
def test_public_tool_or_source_pin_change_refuses_before_any_effect(batch_case,monkeypatch,change):
    f=batch_case
    if change=='tool':f.tools['codex'].write_bytes(b'changed public synthetic executable')
    else:monkeypatch.setattr(module,'linux_source_pins',lambda:{'changed':'synthetic'})
    with pytest.raises(DevelopmentError):f.batch.run(f.config_path)
    assert f.clients==[] and f.native_calls==[] and list((f.batch_root/'batches').iterdir())==[]


def test_delayed_stop_monitor_cannot_write_after_its_main_directory_closes(batch_case,monkeypatch):
    f=batch_case;entered=threading.Event();release=threading.Event();original=f.batch._local
    def delayed(*args,**kwargs):
        if threading.current_thread().name=='finite-pool-stop':entered.set();release.wait(timeout=5)
        return original(*args,**kwargs)
    monkeypatch.setattr(f.batch,'_local',delayed)
    def failed(stage,_):
        if stage=='backend':
            assert entered.wait(timeout=2)
            raise TimeoutError('controlled preparation failure')
    f.prepare_hook=failed
    try:
        with pytest.raises(TimeoutError):f.batch.run(f.config_path)
    finally:release.set()
    for thread in threading.enumerate():
        if thread.name=='finite-pool-stop':thread.join(timeout=2)
    assert not (Path.cwd()/'stop-intent.json').exists()


def test_closed_record_directory_never_falls_back_to_working_directory(tmp_path):
    root=tmp_path/'private';root.mkdir(mode=0o700)
    private=PrivateDirectory(root);private.close()
    with pytest.raises(DevelopmentError,match='finite_pool_record_directory_closed'):
        module.FiniteLinuxPoolBatch._record(private,'must-not-create.json',{})
    assert not (Path.cwd()/'must-not-create.json').exists()


def test_stop_monitor_must_open_before_any_submit_or_native_preparation(batch_case,monkeypatch):
    f=batch_case;original=PrivateDirectory.__enter__;failed=threading.Event()
    def unavailable(private):
        if threading.current_thread().name=='finite-pool-stop':
            failed.set();private.close();raise OSError('synthetic unavailable Stop monitor')
        return original(private)
    monkeypatch.setattr(PrivateDirectory,'__enter__',unavailable)
    with pytest.raises(DevelopmentError,match='finite_pool_stop_unconfirmed'):f.batch.run(f.config_path)
    root=f.batch_root/'batches'/f.config['batch_id']
    assert failed.is_set() and f.clients==[] and f.native_calls==[] and f.calls==[]
    assert not (root/'submit-intent.json').exists() and not (root/'complete.json').exists()
    assert f.batch.run(f.config_path)['reason']=='interrupted_batch'
