"""Real synthetic Board/Git; native qualification boundaries isolated, no CLI/model.

The fake qualifiers are test-only. Production exposes no qualifier injection.
"""
import os
from pathlib import Path
import threading
import uuid

import pytest

from test_board_worker import Board, Client
from test_git_workspace import git
from blackoutburst_agents import linux_pool_preparation as preparation
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import Budgets, DevelopmentError, canonical, digest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.development_pool import DevelopmentPoolDriver
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from blackoutburst_agents.linux_input_plan import pin_linux_input_plan
from blackoutburst_agents.linux_test_backend import PLAN_SCHEMA
from blackoutburst_agents.worker_pool import WorkerPool


def write(path, value):
    with PrivateDirectory(path.parent) as private:private.create(path.name,canonical(value))


class Fixture:
    def __init__(self, base, monkeypatch):
        self.base=base.resolve();os.chmod(self.base,0o700)
        self.board=Board(self.base/'board');self.board.ensure_owner('owner','Synthetic owner')
        self.board.ensure_owner('other','Synthetic other')
        self.human={'kind':'human','owner_id':'owner'}
        self.actions=['repo.read_file','repo.patch_file','repo.run_test']
        self.board.set_policy(self.human,True,True,True,True,self.actions,['repo:fixture'])
        self.project=self.board.create_project(self.human,'Fixture',['owner','other'],idempotency_key='project')
        device=self.board.create_device(self.human,'Fixture',[]);self.device=device['id']
        self.raw=self.board.authenticate(device['token'])
        self.seed=Client(self.board,self.raw);registration=self.seed.register()
        self.agent=registration['identity']['id'];self.profile_version=registration['profile_version']
        self.protected=self.base/'protected';self.protected.mkdir(mode=0o700)
        self.connection=self.protected/'connection.json';write(self.connection,{'synthetic':'never a usable credential'})
        self.driver=DevelopmentPoolDriver(self.base/'selections',connection_file=self.connection,
            owner_id='owner',agent_id=self.agent,board_endpoint=Client.endpoint)
        self.pool=WorkerPool(self.base/'pool',concurrency=4,drivers={'development':self.driver.binding()})
        self.source=self.base/'source';self.source.mkdir(mode=0o700)
        git(self.source,'init','--quiet','--initial-branch=main')
        git(self.source,'config','user.name','Synthetic author')
        git(self.source,'config','user.email','synthetic@example.invalid')
        (self.source/'one.py').write_text('value = 1\n')
        git(self.source,'add','one.py');git(self.source,'commit','--quiet','-m','Synthetic base')
        self.commit=git(self.source,'rev-parse','HEAD')
        self.work=self.base/'checkouts';self.work.mkdir(mode=0o700)
        self.state=self.base/'git-state';self.state.mkdir(mode=0o700)
        self.workspaces=LocalGitWorkspaceController(self.state,grant=LocalRepositoryGrant(
            'owner','explicit-repository',str(self.source),str(self.work),self.commit))
        self.verifier=self.protected/'approved.py'
        with PrivateDirectory(self.protected) as private:private.create('approved.py',b'from pathlib import Path\nassert Path("one.py").read_text() == "value = 1\\n"\n')
        self.tools={}
        for name in ('codex','python','unshare'):
            path=self.protected/name;path.write_bytes(b'never executed synthetic public '+name.encode());path.chmod(0o700)
            self.tools[name]=str(path)
        self.preparer=preparation.LinuxPoolJobPreparer(self.base/'preparations',pool=self.pool,driver=self.driver,workspaces=self.workspaces)
        self.clients=[];self.events=[];self.hook=lambda stage:None
        monkeypatch.setattr(preparation.DevelopmentBoardClient,'from_connection_file',self.client)
        # /private/tmp is intentionally not a trusted public runtime ancestor.
        # Only this original fixture verifier reader is substituted; every copy,
        # manifest, stable pin, repository and actual Git command remains real.
        monkeypatch.setattr(preparation,'read_public',self.verifier_read)
        monkeypatch.setattr(preparation,'pin_linux_execution_plan',self.plan)
        monkeypatch.setattr('blackoutburst_agents.linux_test_backend.validate_current_plan',lambda *_:None)
        monkeypatch.setattr(preparation,'prepare_linux_planner_policy',self.policy_plan)
        fixture=self
        class Backend:
            environment={'synthetic':'same system context'}
            def __init__(self,directory,*,manifest,authorize_scope):
                self.directory=Path(directory);self.manifest=manifest;self.authorize=authorize_scope
            def qualify(self):
                fixture.events.append('backend');fixture.hook('backend')
                assert self.authorize(self.manifest['scope'],'repo.run_test',self.manifest['resource'])
                with PrivateDirectory(self.directory) as private:private.replace('qualification.json',canonical({'synthetic':True}))
            def qualified_for(self,manifest_hash,suite):
                return manifest_hash==digest(canonical(self.manifest)) and suite==self.manifest['tests'][0]
            def close(self):pass
        class Policy:
            environment=Backend.environment
            def __init__(self,directory):self.directory=Path(directory)
            def __enter__(self):return self
            def __exit__(self,*_):pass
            def qualify(self):
                fixture.events.append('policy');fixture.hook('policy')
                with PrivateDirectory(self.directory) as private:private.replace('planner-qualification.json',canonical({'synthetic':True}))
            def validate_for(self,executable,workspace):
                plan=decode(file_document(self.directory/'planner-plan.json')[0])
                assert plan['executable']==executable and plan['workspace']==workspace
                assert list(Path(workspace).iterdir())==[]
        monkeypatch.setattr(preparation,'SupervisedLinuxNamedPythonBackend',Backend)
        monkeypatch.setattr(preparation,'LinuxPlannerPolicy',Policy)

    def verifier_read(self,path,*,maximum,expected=None):
        assert path==str(self.verifier)
        raw,pin=file_document(path,maximum)
        if expected is not None and pin!=expected:raise DevelopmentError('linux_runtime_changed')
        return raw,pin

    def client(self,path,*,session_id,timeout):
        assert not self.pool.mutex._is_owned(), 'RPC under global Pool mutex'
        value=Client(self.board,self.raw,session_id)
        value.connection_identity=file_document(path)[1]['identity']
        value.register_initial=value.register;value.close=lambda:None
        self.clients.append(value);self.hook('registration-'+str(len(self.clients)))
        return value

    def plan(self,*,root,scripts_root,scripts,interpreter,unshare):
        self.events.append('plan')
        self.hook('plan')
        inputs=pin_linux_input_plan(root=root,scripts_root=scripts_root,scripts=scripts).document()
        return {'schema':PLAN_SCHEMA,'root':root,'root_identity':inputs['root_chain'][-1]['identity'],
            'input_plan':inputs,'input_plan_sha256':digest(canonical(inputs)),
            'environment':{'synthetic':'same system context'}}

    def policy_plan(self,directory,*,workspace,checkout,executable,cli_version,interpreter):
        directory.mkdir(mode=0o700)
        write(directory/'planner-plan.json',{'workspace':str(workspace),'checkout':checkout,
            'executable':executable,'cli_version':cli_version,'interpreter':interpreter})
        write(directory/'planner-qualification.json',None)

    def selected(self):
        ticket=self.board.create_ticket(self.human,self.project['id'],'Explicit selection',
            resource='repo:fixture',idempotency_key=str(uuid.uuid4()))
        job=self.pool.submit(request_id=str(uuid.uuid4()),driver_id='development',
            payload={'project_id':self.project['id'],'ticket_id':ticket['id']})
        args=dict(project_id=self.project['id'],ticket_id=ticket['id'],paths=['one.py'],resource='repo:fixture',
            actions=self.actions,verifier_path=str(self.verifier),verifier_sha256=digest(self.verifier.read_bytes()),
            test_name='approved',test_timeout_seconds=2,test_max_output_bytes=64,
            executable=self.tools['codex'],cli_version='codex-cli 0.163.0',interpreter=self.tools['python'],
            unshare=self.tools['unshare'],instruction='Inspect only the selected synthetic file.',model='explicit-fixture-model',
            planner_budgets=PlannerBudgets(),broker_budgets=Budgets())
        return job,ticket,args

    def prepare(self,job,args):return self.preparer.prepare(job['id'],**args)
    def update(self,ticket,**fields):
        current=self.board.get_ticket(self.human,ticket['id'])['ticket']
        return self.board.update_ticket(self.human,ticket['id'],current['version'],**fields)


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    value=Fixture(tmp_path,monkeypatch)
    yield value
    value.pool.close()


def test_real_board_git_binding_no_claim_runtime_or_dispatch(fixture):
    f=fixture;job,ticket,args=f.selected();result=f.prepare(job,args)
    assert result['status']=='bound' and result['historical'] is False
    assert f.events==['plan','backend','policy'] and len(f.clients)==3
    assert all({name for name,_ in client.calls}<={'get_work','get_ticket','get_thread'} for client in f.clients)
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert current['status']=='todo' and current['claimant'] is None
    assert f.board.get_runtime(f.human)['active_count']==0
    assert f.pool.status()['capacity_held']==0 and f.pool.status()['driver_calls']==0
    directory=f.base/'preparations'/job['id']
    intent=decode(file_document(directory/'intent.json',preparation.MAX_DOCUMENT)[0])
    assert intent['request']['grant']==f.workspaces.grant and intent['request']['resource']=='repo:fixture'
    registered=decode(file_document(directory/'registration.json')[0])['scope']
    assert registered['session_id']==job['session_id'] and registered['agent_id']==f.agent
    assert registered['device_id']==f.device and registered['profile_version']==f.profile_version
    selected=decode(file_document(directory/'selection.json')[0]);assert selected['scope']['board_session_id']==job['session_id']
    manifest=decode(file_document(directory/'broker'/'manifest.json')[0]);assert manifest['scope']==registered
    copied=Path(manifest['root'])/'one.py';assert copied.read_bytes()==(f.source/'one.py').read_bytes()
    copied.write_text('private edit\n');assert (f.source/'one.py').read_text()=='value = 1\n'
    assert (directory/'scripts'/'verifier.py').read_bytes()==f.verifier.read_bytes()
    assert list((directory/'planner').iterdir())==[]
    assert f.preparer.status(job['id'])['status']=='prepared'
    for path in directory.rglob('*'):
        assert path.stat().st_mode&0o777==(0o700 if path.is_dir() else 0o600)


def test_exact_completed_retry_local_only(fixture,monkeypatch):
    f=fixture;job,_,args=f.selected();first=f.prepare(job,args)
    monkeypatch.setattr(preparation.DevelopmentBoardClient,'from_connection_file',lambda *_a,**_k:pytest.fail('retry contacted Board'))
    monkeypatch.setattr(f.workspaces,'materialize',lambda *_:pytest.fail('retry cloned'))
    monkeypatch.setattr(preparation,'pin_linux_execution_plan',lambda **_:pytest.fail('retry replanned'))
    second=f.prepare(job,args)
    assert second=={**first,'historical':True} and f.events==['plan','backend','policy']


@pytest.mark.parametrize('field,value',[('resource','other'),('model','other'),('instruction','Changed explicit request'),('paths',['missing.py'])])
def test_changed_request_never_adopted(fixture,field,value):
    f=fixture;job,_,args=f.selected();f.prepare(job,args)
    with pytest.raises(DevelopmentError,match='linux_pool_request_conflict'):f.prepare(job,{**args,field:value})
    assert f.events==['plan','backend','policy'] and len(f.clients)==3


def test_failed_qualification_is_retained_never_retried(fixture):
    f=fixture;job,_,args=f.selected()
    def fail(stage):
        if stage=='backend':raise TimeoutError('private diagnostic')
    f.hook=fail
    with pytest.raises(TimeoutError):f.prepare(job,args)
    status=f.preparer.status(job['id']);assert status['status']=='needs_review'
    assert status['failure']['stage']=='backend-qualification'
    assert status['failure']['reason']=='linux_pool_preparation_failed'
    assert 'private diagnostic' not in canonical(status).decode()
    f.hook=lambda _:None
    with pytest.raises(DevelopmentError,match='linux_pool_preparation_incomplete'):f.prepare(job,args)
    assert f.events==['plan','backend'] and len(f.clients)==1
    assert not (f.driver.selections.directory and Path(f.driver.selections.directory)/(job['id']+'.json')).exists()


@pytest.mark.parametrize('when',['backend','policy'])
@pytest.mark.parametrize('change',['ticket','policy','profile','device','stop','context'])
def test_drift_during_each_qualification_refuses_next_boundary(fixture,when,change):
    f=fixture;job,ticket,args=f.selected()
    def alter(stage):
        if stage!=when:return
        if change=='ticket':f.update(ticket,description='changed during qualification')
        elif change=='policy':f.board.set_policy(f.human,True,True,True,False,f.actions,['repo:fixture'])
        elif change=='profile':f.board.update_agent_profile(f.human,f.agent,f.profile_version,instructions='changed')
        elif change=='device':f.board.revoke_device(f.human,f.device)
        elif change=='stop':f.pool.stop(job['id'])
        else:
            project=f.board.get_project(f.human,f.project['id'])['project']
            f.board.update_project(f.human,project['id'],project['version'],context='changed')
    f.hook=alter
    with pytest.raises(Exception):f.prepare(job,args)
    assert not (Path(f.driver.selections.directory)/(job['id']+'.json')).exists()
    assert f.preparer.status(job['id'])['status']=='needs_review'
    if when=='backend':assert 'policy' not in f.events


@pytest.mark.parametrize('change',['in_progress','assigned','dependency','human_wait','archived'])
def test_ineligible_ticket_refuses_before_git(fixture,change):
    f=fixture;job,ticket,args=f.selected()
    if change=='in_progress':f.update(ticket,status='in_progress')
    elif change=='assigned':
        f.update(ticket,assignee_owner_id='other')
    elif change=='dependency':
        other=f.board.create_ticket(f.human,f.project['id'],'Dependency',idempotency_key='dependency')
        f.update(ticket,depends_on=[other['id']])
    elif change=='human_wait':f.board.request_human_input(f.human,ticket['id'],'owner','Question','question')
    else:f.board.update_project(f.human,f.project['id'],f.project['version'],archived=True)
    with pytest.raises(Exception):f.prepare(job,args)
    assert f.events==[] and not any(f.work.iterdir())


def test_binding_response_loss_not_adopted(fixture,monkeypatch):
    f=fixture;job,_,args=f.selected();bind=f.driver.bind_selection
    def lose(*values):bind(*values);raise TimeoutError('lost local completion')
    monkeypatch.setattr(f.driver,'bind_selection',lose)
    with pytest.raises(TimeoutError):f.prepare(job,args)
    assert (Path(f.driver.selections.directory)/(job['id']+'.json')).exists()
    assert f.preparer.status(job['id'])['status']=='needs_review'
    with pytest.raises(DevelopmentError,match='linux_pool_preparation_incomplete'):f.prepare(job,args)
    assert len(f.clients)==3


def test_completed_retry_rechecks_original_verifier_and_qualification_pins(fixture):
    f=fixture;job,_,args=f.selected();f.prepare(job,args)
    qualification=f.base/'preparations'/job['id']/'backend'/'qualification.json'
    qualification.write_text('{"synthetic":false}')
    with pytest.raises(DevelopmentError):f.prepare(job,args)
    assert len(f.clients)==3


def test_same_job_busy_and_other_job_unblocked(fixture):
    f=fixture;one,_,_=f.selected();two,_,_=f.selected();observed=[]
    with f.preparer._locked(one['id'],create=True):
        def probe():
            try:
                with f.preparer._locked(one['id']):pass
            except DevelopmentError as error:observed.append(error.code)
            with f.preparer._locked(two['id'],create=True):observed.append('other-job-held')
        thread=threading.Thread(target=probe);thread.start();thread.join(timeout=2)
        assert not thread.is_alive()
    assert observed==['linux_pool_preparation_busy','other-job-held']


@pytest.mark.parametrize('parameter,value',[('cli_version','bad version'),('test_timeout_seconds',61),('verifier_sha256','0'*64),('actions',['repo.read_file']),('model','x'*129)])
def test_bad_explicit_operator_inputs_refuse_before_registration(fixture,parameter,value):
    f=fixture;job,_,args=f.selected()
    with pytest.raises(DevelopmentError):f.prepare(job,{**args,parameter:value})
    assert f.clients==[] and f.events==[]


def test_concrete_handle_mandatory(fixture):
    f=fixture
    for name in ('pool','driver','workspaces'):
        values={'pool':f.pool,'driver':f.driver,'workspaces':f.workspaces};values[name]=object()
        with pytest.raises(DevelopmentError,match='linux_pool_concrete_handles_required'):
            preparation.LinuxPoolJobPreparer(f.base/'refused',**values)


def test_intent_precedes_registration_git_and_both_qualifications(fixture):
    f=fixture;job,_,args=f.selected()
    def observe(stage):
        directory=f.base/'preparations'/job['id']
        intent=decode(file_document(directory/'intent.json',preparation.MAX_DOCUMENT)[0])
        assert intent['request']['job']['id']==job['id']
        if stage.startswith('registration-'):assert (directory/'01-registration-intent.json').exists()
        if stage=='plan':assert (directory/'02-git-intent.json').exists() and (directory/'03-resources-intent.json').exists()
        if stage=='backend':assert (directory/'04-backend-qualification-intent.json').exists()
        if stage=='policy':assert (directory/'05-policy-qualification-intent.json').exists()
    f.hook=observe;f.prepare(job,args)


def test_verifier_mutation_during_backend_refuses_policy(fixture):
    f=fixture;job,_,args=f.selected()
    f.hook=lambda stage:f.verifier.write_text('raise AssertionError("changed")\n') if stage=='backend' else None
    with pytest.raises(Exception):f.prepare(job,args)
    assert 'policy' not in f.events
    assert not (Path(f.driver.selections.directory)/(job['id']+'.json')).exists()


def test_ticket_drift_after_plan_refuses_before_backend(fixture):
    f=fixture;job,ticket,args=f.selected()
    f.hook=lambda stage:f.update(ticket,title='Changed before qualification') if stage=='plan' else None
    with pytest.raises(DevelopmentError):f.prepare(job,args)
    assert f.events==['plan']


def test_stop_between_final_fresh_check_and_binding_refuses(fixture,monkeypatch):
    f=fixture;job,_,args=f.selected();record=f.preparer._record
    def stop_after_selection(private,name,value):
        record(private,name,value)
        if name=='selection.json':f.pool.stop(job['id'])
    monkeypatch.setattr(f.preparer,'_record',stop_after_selection)
    with pytest.raises(Exception):f.prepare(job,args)
    assert not (Path(f.driver.selections.directory)/(job['id']+'.json')).exists()


@pytest.mark.parametrize('target',['executable','connection','git-grant'])
def test_operator_pin_changes_during_qualification_refuse(fixture,target):
    f=fixture;job,_,args=f.selected()
    def change(stage):
        if stage!='policy':return
        if target=='executable':Path(f.tools['codex']).write_bytes(b'changed executable')
        elif target=='connection':f.connection.write_text('{"synthetic":"changed"}')
        else:f.workspaces.grant['repository_id']='changed grant'
    f.hook=change
    with pytest.raises(Exception):f.prepare(job,args)
    assert not (Path(f.driver.selections.directory)/(job['id']+'.json')).exists()


def test_different_resource_mapping_fails_before_git(fixture):
    f=fixture;job,_,args=f.selected()
    f.board.set_policy(f.human,True,True,True,True,f.actions,['repo:fixture','repo:other'])
    with pytest.raises(DevelopmentError,match='linux_pool_ticket_not_ready'):f.prepare(job,{**args,'resource':'repo:other'})
    assert f.events==[] and not any(f.work.iterdir())


def test_future_native_qualifications_environment_mismatch_refuses(fixture,monkeypatch):
    f=fixture;job,_,args=f.selected()
    monkeypatch.setattr(preparation.LinuxPlannerPolicy,'environment',{'synthetic':'different cgroup/seccomp context'})
    with pytest.raises(DevelopmentError,match='linux_pool_environment_changed'):f.prepare(job,args)
    assert not (Path(f.driver.selections.directory)/(job['id']+'.json')).exists()


def test_interrupted_intent_not_adopted_and_status_stays_historical(fixture):
    f=fixture;job,_,args=f.selected()
    class Interrupted(BaseException):pass
    def die(stage):
        if stage=='plan':raise Interrupted()
    f.hook=die
    with pytest.raises(Interrupted):f.prepare(job,args)
    assert f.preparer.status(job['id'])['status']=='needs_review'
    f.hook=lambda _:None
    with pytest.raises(DevelopmentError,match='linux_pool_preparation_incomplete'):f.prepare(job,args)
    assert f.events==['plan'] and len(f.clients)==1


def test_wrong_client_endpoint_refuses_before_registration(fixture):
    f=fixture;job,_,args=f.selected()
    def wrong(stage):
        if stage=='registration-1':f.clients[-1].endpoint='https://wrong.test/mcp'
    f.hook=wrong
    with pytest.raises(DevelopmentError,match='linux_pool_registration_changed'):f.prepare(job,args)
    assert f.clients[0].register_count==0 and f.events==[]


def test_corrupt_completion_cannot_redirect_reads_to_an_unapproved_private_file(fixture,monkeypatch):
    f=fixture;job,_,args=f.selected();f.prepare(job,args)
    directory=f.base/'preparations'/job['id'];complete=decode(file_document(directory/'complete.json')[0])
    complete['files']['/unapproved/private/credential.json']={'path':'/unapproved/private/credential.json','identity':[1,2],'sha256':'0'*64}
    with PrivateDirectory(directory) as private:private.replace('complete.json',canonical(complete))
    read=preparation.file_document
    def bounded(path,*values):
        assert str(path)!='/unapproved/private/credential.json'
        return read(path,*values)
    monkeypatch.setattr(preparation,'file_document',bounded)
    with pytest.raises(DevelopmentError):f.prepare(job,args)
    with pytest.raises(DevelopmentError):f.preparer.status(job['id'])
    assert len(f.clients)==3
