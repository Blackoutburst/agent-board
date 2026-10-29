"""Actual service/Board/Git/Worker/Runtime with host qualification and model simulated."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import threading
import uuid

import pytest

pytest.importorskip('blackoutburst_agents.development_provider_continuation')
pytest.importorskip('blackoutburst_agents.development_aggregate_delivery')
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
_REAL_LINUX_TEST_RUN=SupervisedLinuxNamedPythonBackend.run

from blackoutburst_agents import continuation_pool_service as module
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.dev_manifest import canonical,digest,Budgets
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from blackoutburst_agents.runtime_client import RuntimeBoardClient
from test_development_cycle_linux_preparation import synthetic_linux_boundaries,linux_lock_observation
from test_linux_pool_preparation import Fixture
from test_runtime_integration import ControlClient
from test_development_worker import FixtureClient
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.development_pool_selection import file_document


@pytest.fixture
def actual_service(tmp_path,monkeypatch,linux_lock_observation):
    f=Fixture(tmp_path,monkeypatch);synthetic_linux_boundaries(f,monkeypatch)
    monkeypatch.setattr('blackoutburst_agents.worker_pool.WorkerPool.dispatch_pending',
        lambda *_:pytest.fail('Continuation never dispatches a preparation registry'))
    monkeypatch.setattr('blackoutburst_agents.development_pool._DevelopmentRun.run',
        lambda *_:pytest.fail('Continuation never invokes the legacy development runner'))
    root=f.base/'owner-root';root.mkdir(mode=0o700)
    for name in ('config','connections','repositories','verifiers','checkouts','git-state'):
        (root/name).mkdir(mode=0o700)
    source=root/'repositories/source';f.source.rename(source);f.source=source
    connection=root/'connections/coder.json';connection.write_bytes(f.connection.read_bytes());connection.chmod(0o600)
    verifier=root/'verifiers/fixed.py';verifier.write_bytes(f.verifier.read_bytes());verifier.chmod(0o600)
    f.verifier=verifier
    ticket=f.board.create_ticket(f.human,f.project['id'],'Explicit service task',resource='repo:fixture',idempotency_key='service-task')
    ticket=f.board.update_ticket(f.human,ticket['id'],ticket['version'],assignee_agent_id=f.agent)
    batch_id=str(uuid.uuid4());task_id=str(uuid.uuid4())
    limits=PlannerBudgets(max_turns=16,max_actions=16,max_wall_seconds=600).document()
    job=dict(name='coder',agent_id=f.agent,connection_file=str(connection),
        repository=dict(owner_id='owner',repository_id='explicit-repository',repository=str(source),
            workspace_root=str(root/'checkouts'/batch_id/'coder'),base_commit=f.commit),
        project_id=f.project['id'],ticket_id=ticket['id'],paths=['one.py'],resource='repo:fixture',actions=f.actions,
        verifier_path=str(verifier),verifier_sha256=digest(verifier.read_bytes()),test_name='unit',
        test_timeout_seconds=2,test_max_output_bytes=1024,instruction='Explicit local fixture',model='fixture-model',
        planner_budgets=limits,broker_budgets=Budgets(max_operations=128,max_tests=10,max_patch_bytes=1048576).document())
    batch=dict(schema='linux-finite-pool-batch-v2',batch_id=batch_id,owner_id='owner',
        board_endpoint='https://board.test/mcp',timeout_seconds=700,jobs=[job])
    workflow=dict(schema='linux-cloud-workflow-v1',batch_id=batch_id,jobs=[dict(name='coder',paths=['one.py'],
        commit_message='Private fixture',pr_title='Explicit fixture',pr_body='Explicit tested fixture',
        reviewer=dict(agent_id='distinct-reviewer',connection_file=str(root/'connections/reviewer.json'),
            instruction='Explicit review',model='fixture-reviewer',timeout=30,skills=['review']))])
    config=dict(schema='linux-cloud-continuation-v1',batch_file=str(root/'config/batch.json'),
        workflow_file=str(root/'config/workflow.json'),concurrency=1,provider_backoff_seconds=30,
        tasks=[dict(name='coder',logical_task_id=task_id,checkpoint_after_actions=2)])
    path=root/'config/continuation.json'
    for p,v in [(path,config),(root/'config/batch.json',batch),(root/'config/workflow.json',workflow)]:
        p.write_bytes(canonical(v));p.chmod(0o600)
    actions=[dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 1\n'),content='value = 2\n'),
        dict(schema=1,kind='read',path='one.py')]
    import sys
    from blackoutburst_agents.codex_turn import execute_turn
    sequence=f.base/'service-actions.json';sequence.write_text(json.dumps(actions))
    calls=f.base/'service-calls.jsonl'
    cli=Path(f.tools['codex'])
    cli.write_text('#!'+sys.executable+'\n'+f"""
import json,sys,uuid
from pathlib import Path
args=sys.argv[1:]
if args==['--version']:print('codex-cli 1.2.3');raise SystemExit(0)
if args==['login','status']:print('Logged in using ChatGPT',file=sys.stderr);raise SystemExit(0)
sys.stdin.read();path=Path({str(sequence)!r});values=json.loads(path.read_text());value=values.pop(0);path.write_text(json.dumps(values))
session=args[-2] if 'resume' in args else str(uuid.uuid4())
with open({str(calls)!r},'a') as record:record.write(json.dumps({{'session':session}})+'\\n')
def event(value):print(json.dumps(value),flush=True)
event({{'type':'thread.started','thread_id':session}});event({{'type':'turn.started'}})
if value is None:event({{'type':'turn.failed','error':{{'message':'quota UNKNOWN_PRIVATE_CAUSE'}}}});raise SystemExit(1)
event({{'type':'item.completed','item':{{'type':'agent_message','text':json.dumps(value)}}}});event({{'type':'turn.completed'}})
""");cli.chmod(0o700)
    original=DevelopmentBoardWorker.__init__
    def worker(self,*args,**kwargs):
        return original(self,*args,**{**kwargs,'_transport':execute_turn,'_test_anchor':f.base/'anchor'})
    monkeypatch.setattr(DevelopmentBoardWorker,'__init__',worker)
    def runtime(client):
        control=ControlClient(f.board,f.raw,client.session_id);control.close=lambda:None;return control
    monkeypatch.setattr(RuntimeBoardClient,'from_worker',runtime)
    def connected(path,*,session_id,timeout):
        client=FixtureClient(f.board,f.raw,session_id)
        client.connection_identity=tuple(file_document(path)[1]['identity']);client.close=lambda:None
        client.adopt_registration=lambda:setattr(client,'actor',f.board.actor_session(f.raw,session_id))
        client.release_checkpoint_claim=lambda **values:client.call('release_ticket',{**values,'status':'todo','unassign_self':False})
        return client
    monkeypatch.setattr(DevelopmentBoardClient,'from_connection_file',connected)
    monkeypatch.setattr(module,'linux_source_pins',lambda:{'synthetic_test_only':'fixed source pins'})
    def construct():
        return module.ContinuationPoolService(root,configuration_path=path,owner_id='owner',board_endpoint=batch['board_endpoint'],
            executable=f.tools['codex'],interpreter=f.tools['python'],unshare=f.tools['unshare'],
            gh_executable=f.tools['python'],cli_version='codex-cli 1.2.3',max_seconds=900)
    yield f,root,task_id,ticket,construct,calls,sequence
    f.pool.close()


def test_actual_first_pass_checkpoints_retires_and_preserves_logical_state(actual_service,monkeypatch):
    f,root,task_id,ticket,construct,calls,actions=actual_service
    result=construct().run_once()
    assert result['status']=='pass_complete',result
    assert result['tasks'][0]['phase']=='paused_cycle',result
    assert len(calls.read_text().splitlines())==2 and json.loads(actions.read_text())==[]
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert current['status']=='todo' and current['claimant'] is None
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
        state=journal.snapshot()['state']
        assert state['phase']=='paused_cycle' and state['used_total']['turns']==2
    assert f.board.get_runtime(f.human)['active_count']==0


def test_new_service_reopens_clean_checkpoint_and_runs_only_new_generation(actual_service):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    first=construct().run_once();assert first['status']=='pass_complete',first
    first_op=first['tasks'][0]['operation_id']
    old={str(path):path.read_bytes() for path in root.rglob('*') if path.is_file()
        and path.name in ('planner.json','state.json','generation.json')}
    sequence.write_text(json.dumps([dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 2\n'),content='value = 3\n'),
        dict(schema=1,kind='read',path='one.py')]))
    second=construct().run_once()
    assert second['status']=='pass_complete' and second['tasks'][0]['phase']=='paused_cycle',second
    assert second['tasks'][0]['operation_id']!=first_op and len(calls.read_text().splitlines())==4
    assert all(Path(path).read_bytes()==value for path,value in old.items())
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
        state=journal.snapshot()['state']
        assert state['cycle_seq']==2 and state['used_total']['turns']==4 and state['used_total']['actions']==4
    sessions=[json.loads(line)['session'] for line in calls.read_text().splitlines()]
    assert sessions[0]==sessions[1] and sessions[2]==sessions[3] and sessions[0]!=sessions[2]
    assert f.board.get_runtime(f.human)['active_count']==0


def test_provider_failure_is_paused_and_immediate_reopen_only_observes_backoff(actual_service):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    actions=json.loads(sequence.read_text());sequence.write_text(json.dumps([actions[0],None]))
    first=construct().run_once()
    assert first['status']=='pass_complete' and first['tasks'][0]['phase']=='paused_provider',first
    assert len(calls.read_text().splitlines())==2
    before={str(path):path.read_bytes() for path in root.rglob('*') if path.is_file()}
    second=construct().run_once()
    assert second['status']=='pass_complete' and second['tasks'][0]['phase']=='backoff',second
    assert len(calls.read_text().splitlines())==2
    assert before=={str(path):path.read_bytes() for path in root.rglob('*') if path.is_file()}
    assert f.board.get_runtime(f.human)['active_count']==0
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None


def test_human_logical_stop_is_visible_during_provider_backoff(actual_service):
    from blackoutburst_agents.continuation import ContinuationTask,ContinuationChecks
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    sequence.write_text(json.dumps([None]));first=construct().run_once()
    assert first['tasks'][0]['phase']=='paused_provider',first
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    def no_permission_needed_for_stop(*_):raise AssertionError('Stop must not admit work')
    with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
        checks=ContinuationChecks(**{key:no_permission_needed_for_stop for key in ('scope','claim','source','fence','runtime','resume')})
        task=ContinuationTask(journal,checks);state=task.snapshot()['state']
        task.stop(expected_revision=state['revision'],request_id='human-stop')
    second=construct().run_once()
    assert second['tasks'][0]['phase']=='stopped' and len(calls.read_text().splitlines())==1
    assert len(list((root/'continuations'/key/'generations').iterdir()))==1


def test_revoked_scope_blocks_next_generation_without_model_or_claim(actual_service):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    first=construct().run_once();assert first['tasks'][0]['phase']=='paused_cycle',first
    f.board.set_policy(f.human,True,False,True,True,[],[])
    sequence.write_text(json.dumps([dict(schema=1,kind='read',path='one.py')]))
    second=construct().run_once()
    assert second['status']=='needs_you' and len(calls.read_text().splitlines())==2
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is None
    third=construct().run_once()
    assert third['tasks'][0]['reason']=='continuation_pool_pending' and len(calls.read_text().splitlines())==2


def test_external_process_stop_cancels_real_model_before_patch(actual_service):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    cli=Path(f.tools['codex'])
    text=cli.read_text().replace("event({'type':'thread.started','thread_id':session});event({'type':'turn.started'})", 
        "event({'type':'thread.started','thread_id':session});event({'type':'turn.started'});__import__('time').sleep(1)")
    cli.write_text(text);cli.chmod(0o700)
    signalled=[]
    def requested():
        if not calls.exists():return False
        if not signalled:signalled.append(__import__('time').monotonic())
        return True
    result=construct().run_once(should_stop=requested)
    assert signalled and __import__('time').monotonic()-signalled[0]<5,result
    assert result['status'] in ('needs_you','stopped')
    assert not any(p.read_bytes()==b'value = 2\n' for p in (root/'checkouts').rglob('one.py'))
    assert f.board.get_runtime(f.human)['active_count']==0
    replay=construct().run_once()
    assert replay['tasks'][0]['reason']=='continuation_pool_pending'
    assert len(calls.read_text().splitlines())==1


def test_fresh_provider_negative_only_observes_then_positive_creates_new_generation(actual_service,monkeypatch):
    from blackoutburst_agents import provider_disposition
    from blackoutburst_agents.dev_journal import PrivateDirectory,decode
    from test_provider_disposition import usage
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    actions=json.loads(sequence.read_text());sequence.write_text(json.dumps([actions[0],None]))
    first=construct().run_once();assert first['tasks'][0]['phase']=='paused_provider',first
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}));directory=root/'continuations'/key
    def scheduled_now():
        # A scheduling timestamp is deliberately not authority. The fresh
        # official observation and every normal admission guard still run.
        with PrivateDirectory(directory) as private:
            cursor=decode(private.read('cursor.json',512*1024));cursor['not_before']=1.
            private.replace('cursor.json',canonical(cursor))
    observed=[]
    def unavailable(*args,**kwargs):observed.append('negative');return usage(False)
    monkeypatch.setattr(provider_disposition,'read_usage',unavailable)
    scheduled_now();second=construct().run_once()
    assert second['tasks'][0]['phase']=='paused_provider' and observed==['negative'],second
    assert len(calls.read_text().splitlines())==2 and len(list((directory/'generations').iterdir()))==1
    sequence.write_text(json.dumps([dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 2\n'),content='value = 3\n'),
        dict(schema=1,kind='read',path='one.py')]))
    def available(*args,**kwargs):observed.append('positive');return usage(True)
    monkeypatch.setattr(provider_disposition,'read_usage',available)
    scheduled_now();third=construct().run_once()
    assert third['status']=='pass_complete' and third['tasks'][0]['phase']=='paused_cycle',third
    assert len(calls.read_text().splitlines())==4 and len(list((directory/'generations').iterdir()))==2
    with ContinuationJournal.open(directory/'logical') as journal:
        state=journal.snapshot()['state']
        assert state['cycle_seq']==2 and state['used_total']['turns']==4 and state['used_total']['actions']==3
    assert observed.count('positive')>=3 and f.board.get_runtime(f.human)['active_count']==0


@pytest.mark.parametrize('generations',[1,2])
def test_actual_final_delivery_uses_original_base_and_releases_for_independent_review(actual_service,monkeypatch,generations):
    import subprocess
    from blackoutburst_agents.git_publication import GitHubDraftPublisher,REPOSITORY,REMOTE,API
    from test_development_aggregate_delivery import native_receipt
    from test_git_workspace import git
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    batch_path=root/'config/batch.json';batch=json.loads(batch_path.read_text());job=batch['jobs'][0]
    verifier=Path(job['verifier_path'])
    verifier.write_bytes(b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n')
    job['verifier_sha256']=digest(verifier.read_bytes());batch_path.write_bytes(canonical(batch))
    config_path=root/'config/continuation.json';config=json.loads(config_path.read_text())
    config['tasks'][0]['checkpoint_after_actions']=3 if generations==2 else 8
    config_path.write_bytes(canonical(config))
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'run',_REAL_LINUX_TEST_RUN)
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'_run_job',native_receipt)
    remote=f.base/'remote.git';git(f.base,'clone','--bare','--no-hardlinks',str(f.source),str(remote))
    state={'pushes':0,'posts':0,'prs':[]}
    def command(publisher,argv,*,cwd,git=False):
        publisher._confirm_launch(argv,cwd=cwd,git=git)
        if git:
            assert REMOTE in argv and argv[1] in ('push','ls-remote')
            if argv[1]=='push':state['pushes']+=1
            result=subprocess.run([str(remote) if part==REMOTE else part for part in argv],cwd=cwd,
                env={'PATH':'/usr/bin:/bin','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null','GIT_ALLOW_PROTOCOL':'file'},
                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            return result.returncode,result.stdout,result.stderr
        if argv[1:]==['auth','status','--hostname','github.com']:return 0,b'synthetic official authentication\n',b''
        assert argv[1:5]==['api','--hostname','github.com','--method']
        method,path=argv[5:7]
        if path=='user':value={'login':'synthetic-user'}
        elif path==API:value=dict(full_name=REPOSITORY,html_url='https://github.com/'+REPOSITORY,permissions={'push':True},archived=False,disabled=False)
        elif method=='GET' and path.startswith(API+'/pulls?'):value=state['prs']
        elif method=='POST' and path==API+'/pulls':
            state['posts']+=1;payload=json.loads(Path(argv[8]).read_bytes());record=json.loads((Path(cwd)/'publication.json').read_bytes())
            value=dict(number=17,html_url='https://github.com/'+REPOSITORY+'/pull/17',state='open',draft=True,
                user={'login':'synthetic-user'},title=payload['title'],body=payload['body'],
                head=dict(ref=payload['head'],sha=record['request']['delivery_sha'],repo={'full_name':REPOSITORY}),
                base=dict(ref='main',sha=f.commit,repo={'full_name':REPOSITORY}))
            state['prs'].append(value)
        else:raise AssertionError('No unexpected network command')
        return 0,canonical(value),b''
    monkeypatch.setattr(GitHubDraftPublisher,'_command',command)
    if generations==2:
        first_actions=json.loads(sequence.read_text());sequence.write_text(json.dumps(first_actions+[dict(schema=1,kind='read',path='one.py')]))
        first=construct().run_once();assert first['tasks'][0]['phase']=='paused_cycle',first
        before={str(p):p.read_bytes() for p in root.rglob('planner.json')}
        expected=b'value = 2\n'
    else:before={};expected=b'value = 1\n'
    sequence.write_text(json.dumps([dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(expected),content='value = 3\n'),
        dict(schema=1,kind='test',name='unit'),dict(schema=1,kind='final',summary='Verified final aggregate')]))
    result=construct().run_once()
    assert result['status']=='pass_complete' and result['tasks'][0]['phase']=='awaiting_independent_review',result
    assert (state['pushes'],state['posts'])==(1,1)
    assert git(remote,'show',state['prs'][0]['head']['sha']+':one.py')=='value = 3'
    assert git(remote,'rev-parse',state['prs'][0]['head']['sha']+'^')==f.commit
    assert all(Path(path).read_bytes()==raw for path,raw in before.items())
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert current['status']=='review' and current['claimant'] is None and current['assignee_agent_id'] is None
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
        logical=journal.snapshot()['state']
        assert logical['phase']=='complete' and logical['used_total']['turns']==3*generations
    assert f.board.get_runtime(f.human)['active_count']==0
    replay=construct().run_once()
    assert replay['tasks'][0]['phase']=='awaiting_independent_review' and (state['pushes'],state['posts'])==(1,1)


def test_unassigned_ticket_refuses_before_any_model_without_auto_assignment(actual_service):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    f.board.update_ticket(f.human,ticket['id'],current['version'],assignee_agent_id=None)
    result=construct().run_once()
    assert result['status']=='needs_you' and not calls.exists()
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert current['status']=='todo' and current['claimant'] is None and current['assignee_agent_id'] is None
    assert f.board.get_runtime(f.human)['active_count']==0


def test_final_publication_refuses_changes_outside_workflow_paths(actual_service,monkeypatch):
    from blackoutburst_agents.git_publication import GitHubDraftPublisher
    from test_development_aggregate_delivery import native_receipt
    from test_git_workspace import git
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    (f.source/'outside.py').write_text('original\n')
    git(f.source,'add','outside.py');git(f.source,'commit','--quiet','-m','Explicit second approved code path')
    f.commit=git(f.source,'rev-parse','HEAD')
    batch_path=root/'config/batch.json';batch=json.loads(batch_path.read_text());job=batch['jobs'][0]
    job['repository']['base_commit']=f.commit;job['paths'].append('outside.py')
    verifier=Path(job['verifier_path']);verifier.write_bytes(b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n')
    job['verifier_sha256']=digest(verifier.read_bytes());batch_path.write_bytes(canonical(batch))
    config_path=root/'config/continuation.json';config=json.loads(config_path.read_text())
    config['tasks'][0]['checkpoint_after_actions']=8;config_path.write_bytes(canonical(config))
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'run',_REAL_LINUX_TEST_RUN)
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'_run_job',native_receipt)
    monkeypatch.setattr(GitHubDraftPublisher,'_command',lambda *_a,**_k:pytest.fail('Out-of-workflow changes cannot reach publication'))
    sequence.write_text(json.dumps([dict(schema=1,kind='patch',path='outside.py',expected_sha256=digest(b'original\n'),content='code-scope allowed\n'),
        dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 1\n'),content='value = 3\n'),
        dict(schema=1,kind='test',name='unit'),dict(schema=1,kind='final',summary='Actual tested code; stricter publication gate must refuse')]))
    result=construct().run_once()
    assert result['status']=='needs_you' and len(calls.read_text().splitlines())==4,result
    assert not list(root.rglob('publication.json'))
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
        state=journal.snapshot()['state']
        assert state['phase']=='needs_you' and state['checkpoint'] is None and state['used_total']['turns']==4
    assert f.board.get_ticket(f.human,ticket['id'])['ticket']['status']=='in_progress'
    assert f.board.get_runtime(f.human)['active_count']==0


def test_clean_paused_checkpoint_can_outlive_a_stopped_preparation_registry_without_reset(actual_service):
    from blackoutburst_agents.development_pool import DevelopmentPoolDriver
    from blackoutburst_agents.worker_pool import WorkerPool
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    first=construct().run_once();assert first['tasks'][0]['phase']=='paused_cycle',first
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    generation=next((root/'continuations'/key/'generations').iterdir())
    job=json.loads((root/'config/batch.json').read_text())['jobs'][0]
    driver=DevelopmentPoolDriver(generation/'selections',connection_file=job['connection_file'],
        owner_id='owner',agent_id=job['agent_id'],board_endpoint='https://board.test/mcp')
    with closing(WorkerPool(generation/'pool',concurrency=1,ceiling=1,drivers={'development':driver.binding()})) as pool:
        pool.stop();assert pool.status()['jobs'][0]['state']=='cancelled'
    registry=(generation/'pool/pool-state.json').read_bytes()
    sequence.write_text(json.dumps([dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 2\n'),content='value = 3\n'),
        dict(schema=1,kind='read',path='one.py')]))
    second=construct().run_once()
    assert second['tasks'][0]['phase']=='paused_cycle',second
    assert len(calls.read_text().splitlines())==4 and (generation/'pool/pool-state.json').read_bytes()==registry
    assert f.board.get_runtime(f.human)['active_count']==0
