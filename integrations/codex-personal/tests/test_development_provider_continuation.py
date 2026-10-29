"""Real terminal CLI/Board/Runtime/Git; no provider API or native qualification."""
from copy import deepcopy
import json

import pytest

from blackoutburst_agents.codex_turn import execute_turn
from blackoutburst_agents.dev_manifest import digest,canonical
from blackoutburst_agents.development_provider_continuation import DevelopmentProviderContinuation
from blackoutburst_agents.dev_runtime import development_work_id
from test_development_cycle_driver import cycle_case
from test_development_cycle_linux_preparation import linux_lock_observation
import test_codex_turn as turn_helpers


@pytest.fixture
def provider_case(cycle_case,monkeypatch):
    case=cycle_case;helper=turn_helpers.CodexTurnTests(methodName='runTest');helper.setUp()
    case.addCleanup(helper.doCleanups)
    case.sequence=case.base/'terminal-actions.json';case.calls=case.base/'terminal-calls.jsonl'
    case.sequence.write_text(json.dumps([case.action('patch',path='main.py',expected_sha256=digest(b'value = 1\n'),
        content='value = 2\n'),None]))
    body=f'''
import uuid
from pathlib import Path
path=Path({str(case.sequence)!r})
values=json.loads(path.read_text());value=values.pop(0);path.write_text(json.dumps(values))
session=args[-2] if 'resume' in args else str(uuid.uuid4())
with open({str(case.calls)!r},'a') as capture:capture.write(json.dumps({{'session':session,'resume':'resume' in args}})+'\\n')
event({{'type':'thread.started','thread_id':session}});event({{'type':'turn.started'}})
if value is None:
    event({{'type':'turn.failed','error':{{'message':'quota PRIVATE_UNKNOWN_CAUSE'}}}})
    raise SystemExit(1)
message(json.dumps(value));event({{'type':'turn.completed'}})
'''
    case.executable=helper.fake_cli(body)
    case.policy.executable=str(case.executable.resolve())
    case.worker.transport=execute_turn
    case.provider=DevelopmentProviderContinuation(case.task,case.worker,case.producer)
    return case


def test_patch_then_real_terminal_failed_pauses_and_retires_without_reopening_work(provider_case):
    case=provider_case
    result=case.provider.run_checkpoint(case.operation,request_id='provider-first',after_actions=2)
    assert result['status']=='paused_provider',result
    assert result['cause']=='unknown' and result['retry_after_seconds']==30
    state=case.task.snapshot()['state'];assert state['phase']=='paused_provider'
    assert state['used_total']['turns']==2 and state['used_total']['actions']==1
    assert state['checkpoint']['provider_receipt'] is not None
    op=case.worker.journal.get(case.operation);assert op['stage']=='running'
    observation=op['payload']['checkpoint_capture']['observation']
    assert observation['schema']=='development-provider-checkpoint-observation-v1'
    assert observation['passed_tests_promoted'] is False
    work=case.store.status(development_work_id(case.operation))
    assert work['work']['status']=='needs_review'
    assert [turn['status'] for turn in work['turns']]==['completed','blocked']
    assert work['turns'][-1]['requires_review'] and work['turns'][-1]['resolution'] is None
    assert case.worker._stopped(case.operation) and case.read_ticket()['claimant'] is None
    assert (case.root/'main.py').read_bytes()==b'value = 2\n'
    assert len(case.calls.read_text().splitlines())==2 and not case.backend.calls
    assert case.board.get_runtime(case.human)['active_count']==0
    assert 'PRIVATE_UNKNOWN_CAUSE' not in json.dumps(observation)
    before=deepcopy(work)
    again=case.provider.run_checkpoint(case.operation,request_id='provider-first',after_actions=2)
    assert again['historical'] is True and case.store.status(development_work_id(case.operation))==before
    assert len(case.calls.read_text().splitlines())==2
    assert case.board.get_runtime(case.human)['limits']['paused'] is False


def test_first_failed_turn_has_clean_source_checkpoint_without_fabricated_history(provider_case):
    case=provider_case;case.sequence.write_text(json.dumps([None]))
    result=case.provider.run_checkpoint(case.operation,request_id='first-turn-failure',after_actions=1)
    assert result['status']=='paused_provider',result
    state=case.task.snapshot()['state'];assert state['used_total']['turns']==1 and state['used_total']['actions']==0
    assert (case.root/'main.py').read_bytes()==b'value = 1\n' and case.read_ticket()['claimant'] is None
    assert len(case.calls.read_text().splitlines())==1


@pytest.mark.parametrize('failure',['json','tool','cleanup','serialized','stale','scope'])
def test_uncertain_or_uncorrelated_failure_is_needs_you_without_checkpoint_or_release(provider_case,monkeypatch,failure):
    case=provider_case
    from blackoutburst_agents import development_provider_continuation as module,codex_turn
    if failure in ('json','tool'):
        script=case.executable.read_text();marker="event({'type':'turn.failed','error':{'message':'quota PRIVATE_UNKNOWN_CAUSE'}})"
        replacement="print('not JSON',flush=True)" if failure=='json' else "event({'type':'item.completed','item':{'type':'command_execution','command':'fixture event only'}})"
        assert marker in script;case.executable.write_text(script.replace(marker,replacement))
    elif failure=='cleanup':monkeypatch.setattr(codex_turn,'_group_absent',lambda _:False)
    elif failure=='serialized':
        monkeypatch.setattr(module,'consume_provider_receipt',lambda receipt,**kwargs:json.loads(receipt.metadata_json))
    elif failure=='stale':
        original=module._ProviderFailure.verify
        def stale(self,*args,**kwargs):
            object.__setattr__(self.receipt,'monotonic_at',-10000)
            return original(self,*args,**kwargs)
        monkeypatch.setattr(module._ProviderFailure,'verify',stale)
    else:
        original=module.DevelopmentProviderSession.take
        def revoke(self,*args,**kwargs):
            value=original(self,*args,**kwargs)
            if value is not None:case.logical_fault='continuation_scope_changed'
            return value
        monkeypatch.setattr(module.DevelopmentProviderSession,'take',revoke)
    result=case.provider.run_checkpoint(case.operation,request_id='uncertain-'+failure,after_actions=2)
    assert result['status']=='needs_you',result
    assert case.task.snapshot()['state']['phase']=='needs_you' and case.task.snapshot()['state']['checkpoint'] is None
    assert 'checkpoint_capture' not in case.worker.journal.get(case.operation)['payload']
    assert case.release_calls==[] and len(case.calls.read_text().splitlines())==2


def test_provider_record_cannot_be_relabelled_as_ready_v1(provider_case):
    from blackoutburst_agents.dev_manifest import DevelopmentError
    case=provider_case;case.provider.run_checkpoint(case.operation,request_id='provider',after_actions=2)
    observation=deepcopy(case.worker.journal.get(case.operation)['payload']['checkpoint_capture']['observation'])
    observation['schema']='development-checkpoint-observation-v1'
    with pytest.raises(DevelopmentError):case.producer._proof_caps(observation)


def test_reopened_paused_metadata_never_rehydrates_provider_evidence_or_permit(provider_case):
    from blackoutburst_agents.continuation import ContinuationTask
    from blackoutburst_agents.continuation_journal import ContinuationJournal
    case=provider_case;first=case.provider.run_checkpoint(case.operation,request_id='before-reopen',after_actions=2)
    reopened=ContinuationTask(ContinuationJournal(case.task.journal.private.path),case.task.checks,clock=case.task.clock)
    case.addCleanup(reopened.close)
    controller=DevelopmentProviderContinuation(reopened,case.worker,case.producer)
    assert controller._session is None and controller.status(case.operation)['historical'] is True
    assert controller.status(case.operation)['checkpoint']==first['checkpoint']
    assert controller.run_checkpoint(case.operation,request_id='before-reopen',after_actions=2)['historical'] is True
    assert len(case.calls.read_text().splitlines())==2


def test_healthy_provider_mode_keeps_normal_result_ready_contract(provider_case):
    from blackoutburst_agents.dev_journal import decode
    case=provider_case;case.sequence.write_text(json.dumps([case.action('patch',path='main.py',expected_sha256=digest(b'value = 1\n'),
        content='value = 2\n'),case.action('test',name='unit'),case.action('final',summary='verified')]))
    result=case.provider.run_checkpoint(case.operation,request_id='healthy-final',after_actions=8)
    assert result['status']=='review_ready' and result['stage']=='result_ready',result
    op=case.worker.journal.get(case.operation)
    assert op['payload']['planner_receipt']['status']=='review_ready'
    assert not any('provider' in key for key in op['configuration'])
    intent=decode((case.worker.journal.directory/('development-checkpoint-intent-'+case.operation+'.json')).read_bytes())
    assert intent['schema']=='development-worker-provider-checkpoint-intent-v1'
    assert intent['provider_mode']=='terminal-failure-v1'
    assert case.task.snapshot()['state']['phase']=='running'  # Final aggregation owns completion.
    assert all(turn['status']=='completed' for turn in case.store.status(development_work_id(case.operation))['turns'])


def test_failed_provider_checkpoint_is_never_a_delivery_result(provider_case):
    from blackoutburst_agents.development_delivery import DevelopmentDeliveryProof
    from blackoutburst_agents.dev_manifest import DevelopmentError
    case=provider_case;case.provider.run_checkpoint(case.operation,request_id='not-final',after_actions=2)
    proof=DevelopmentDeliveryProof(case.worker,case.workspaces,case.operation)
    with pytest.raises(DevelopmentError):proof._operation()


def test_an_old_finite_failed_work_cannot_be_enrolled_after_the_failure(provider_case):
    from blackoutburst_agents.dev_manifest import DevelopmentError
    case=provider_case;case.worker.run(case.operation)
    before=deepcopy(case.store.status(development_work_id(case.operation)))
    with pytest.raises(DevelopmentError,match='development_provider_new_generation_required'):
        case.provider.run_checkpoint(case.operation,request_id='late-optin',after_actions=2)
    assert case.store.status(development_work_id(case.operation))==before
    assert case.task.snapshot()['state']['cycle_seq']==0
    assert len(case.calls.read_text().splitlines())==2


@pytest.mark.parametrize('wake_case',['success','stale','scope','identity','after_preparation','ack','owner_pause'])
def test_real_linux_provider_wake_prepares_new_worker_scope_and_never_replays_failed_turn(tmp_path,monkeypatch,linux_lock_observation,wake_case):
    from contextlib import ExitStack,closing
    from pathlib import Path
    import uuid
    import sys
    from test_linux_pool_preparation import Fixture
    from test_development_cycle_linux_preparation import synthetic_linux_boundaries
    from test_development_worker import FixtureClient,DevelopmentBoardWorker
    from test_runtime_integration import ControlClient
    from blackoutburst_agents import linux_pool_preparation as preparation,provider_disposition as provider
    from blackoutburst_agents.continuation import ContinuationTask,ContinuationChecks
    from blackoutburst_agents.dev_journal import decode
    from blackoutburst_agents.dev_planner_journal import PlannerBudgets
    from blackoutburst_agents.git_workspace import LocalRepositoryGrant,LocalGitWorkspaceController
    from blackoutburst_agents.private_development_checkpoint import PrivateDevelopmentCheckpointController
    from blackoutburst_agents.development_pool import DevelopmentPoolDriver
    from blackoutburst_agents.worker_pool import WorkerPool
    from blackoutburst_agents.work_journal import WorkJournal
    from blackoutburst_agents.run_store import RunStore
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
    from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
    from test_provider_disposition import usage
    f=Fixture(tmp_path,monkeypatch);synthetic_linux_boundaries(f,monkeypatch)
    sequence=f.base/'provider-sequence.json';calls=f.base/'provider-calls.jsonl'
    sequence.write_text(json.dumps([dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 1\n'),content='value = 2\n'),None,
        dict(schema=1,kind='patch',path='one.py',expected_sha256=digest(b'value = 2\n'),content='value = 3\n'),
        dict(schema=1,kind='read',path='one.py')]))
    cli=Path(f.tools['codex'])
    cli.write_text('#!'+sys.executable+'\n'+f'''
import json,sys,uuid
from pathlib import Path
args=sys.argv[1:]
if args==['--version']:print('codex-cli 0.163.0');raise SystemExit(0)
if args==['login','status']:print('Logged in using ChatGPT',file=sys.stderr);raise SystemExit(0)
prompt=sys.stdin.read();path=Path({str(sequence)!r});values=json.loads(path.read_text());value=values.pop(0);path.write_text(json.dumps(values))
session=args[-2] if 'resume' in args else str(uuid.uuid4())
with open({str(calls)!r},'a') as capture:capture.write(json.dumps({{'session':session,'resume':'resume' in args}})+'\\n')
def event(value):print(json.dumps(value),flush=True)
event({{'type':'thread.started','thread_id':session}});event({{'type':'turn.started'}})
if value is None:event({{'type':'turn.failed','error':{{'message':'quota UNKNOWN_PRIVATE_CAUSE'}}}});raise SystemExit(1)
event({{'type':'item.completed','item':{{'type':'agent_message','text':json.dumps(value)}}}});event({{'type':'turn.completed'}})
''');cli.chmod(0o700)
    with ExitStack() as resources:
        resources.callback(f.pool.close)
        job,ticket,parameters=f.selected();parameters['planner_budgets']=PlannerBudgets(max_wall_seconds=600)
        f.prepare(job,parameters);directory=Path(f.preparer.directory)/job['id']
        manifest=decode((directory/'broker/manifest.json').read_bytes());plan=decode((directory/'backend/execution-plan.json').read_bytes())
        selection=decode((directory/'selection.json').read_bytes())
        client=FixtureClient(f.board,f.raw,job['session_id']);control=ControlClient(f.board,f.raw,job['session_id'])
        journal=resources.enter_context(closing(WorkJournal(f.base/'provider-work')))
        store=resources.enter_context(closing(RunStore(f.base/'provider-runs')))
        runtime=resources.enter_context(closing(RuntimeJournal(f.base/'provider-runtime')))
        worker=DevelopmentBoardWorker(journal,store,runtime,client,control,backend_factory=SupervisedLinuxNamedPythonBackend,
            policy_factory=LinuxPlannerPolicy,_transport=execute_turn,_test_anchor=f.base/'anchor')
        worker.prepare(scope=selection['scope'],request_id='first-provider-linux',instruction=parameters['instruction'],
            broker_directory=directory/'broker',backend_directory=directory/'backend',planner_policy_directory=directory/'policy',
            model=parameters['model'],budgets=parameters['planner_budgets'],operation_id=job['id'])
        op=journal.get(job['id']);registration=op['payload']['registration']
        tests={'tests':[{'name':parameters['test_name'],'script_sha256':parameters['verifier_sha256'],
            'timeout_seconds':parameters['test_timeout_seconds'],'max_output_bytes':parameters['test_max_output_bytes']}],
            'imports':plan['imports'],'limits':plan['limits']}
        caps={'paths':['one.py'],'tests':tests,'actions':manifest['actions'],'resource':manifest['resource'],
            'broker_limits':manifest['budgets'],'planner_limits':op['configuration']['budgets']}
        configuration={'schema':'private-development-checkpoint-config-v1','task_id':'provider-linux',
            'owner_id':'owner','project_id':f.project['id'],'ticket_id':ticket['id'],'repository_id':f.workspaces.grant['repository_id'],
            'resource':'repo:fixture','original_public_base':f.commit,'global_paths':['one.py'],
            'global_test_caps_sha256':digest(canonical(tests)),'static_caps_sha256':digest(canonical(caps)),'source_release_sha256':'b'*64}
        cp_root=f.base/'checkpoints';cp_root.mkdir(mode=0o700)
        checkpoints=PrivateDevelopmentCheckpointController(f.workspaces,cp_root,configuration=configuration)
        scope={key:selection['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint')}
        scope.update(device_id=registration['identity']['device_id'],profile_version=registration['profile_version'],resource='repo:fixture',source_release_sha256='b'*64)
        task=resources.enter_context(closing(ContinuationTask.create(f.base/'logical-provider',logical_task_id='provider-linux',scope=scope,
            cycle_limits={'max_turns':64,'max_actions':64,'max_operations':128,'max_tests':10,'max_read_bytes':10000000,
                'max_patch_bytes':1048576,'max_wall_seconds':600},initial_source_sha256=digest(canonical({'one.py':digest(b'value = 1\n')})),
            checks=ContinuationChecks(**{key:lambda _:None for key in ('scope','claim','source','fence','runtime','resume')}))))
        client.release_checkpoint_claim=lambda **values:client.call('release_ticket',{**values,'status':'todo','unassign_self':False})
        old=DevelopmentProviderContinuation(task,worker,checkpoints)
        pause=old.run_checkpoint(job['id'],request_id='provider-linux-first',after_actions=2)
        assert pause['status']=='paused_provider',pause
        parent=pause['checkpoint'];old_work=deepcopy(store.status(development_work_id(job['id'])))
        old_documents={name:(directory/'broker'/name).read_bytes() for name in ('state.json','planner.json')}
        state=f.base/'next-git';roots=f.base/'next-checkouts';state.mkdir(mode=0o700);roots.mkdir(mode=0o700)
        workspaces=LocalGitWorkspaceController(state,grant=LocalRepositoryGrant('owner',configuration['repository_id'],
            parent['checkout'],str(roots),parent['commit_sha']))
        pool_driver=DevelopmentPoolDriver(f.base/'next-selections',connection_file=f.connection,owner_id='owner',agent_id=f.agent,board_endpoint=client.endpoint)
        pool=resources.enter_context(closing(WorkerPool(f.base/'next-pool',concurrency=1,drivers={'development':pool_driver.binding()})))
        preparer=preparation.LinuxPoolJobPreparer(f.base/'next-preparation',pool=pool,driver=pool_driver,workspaces=workspaces)
        next_job=pool.submit(request_id='provider-next',driver_id='development',payload={'project_id':f.project['id'],'ticket_id':ticket['id']})
        context=pool._context(pool._job(pool.journal.read(),next_job['id']),create=True)
        new_client=FixtureClient(f.board,f.raw,next_job['session_id']);new_control=ControlClient(f.board,f.raw,next_job['session_id'])
        new_journal=resources.enter_context(closing(WorkJournal(context.journals['work'])))
        new_store=resources.enter_context(closing(RunStore(context.journals['runs'])))
        new_runtime=resources.enter_context(closing(RuntimeJournal(context.journals['runtime'])))
        new_worker=DevelopmentBoardWorker(new_journal,new_store,new_runtime,new_client,new_control,
            backend_factory=SupervisedLinuxNamedPythonBackend,policy_factory=LinuxPlannerPolicy,_transport=execute_turn,_test_anchor=f.base/'anchor')
        new_client.release_checkpoint_claim=lambda **values:new_client.call('release_ticket',{**values,'status':'todo','unassign_self':False})
        new_checkpoints=PrivateDevelopmentCheckpointController(workspaces,cp_root,configuration=configuration)
        new_checks_seen=[]
        def new_scope(context):
            prepared=new_worker.journal.get(next_job['id'])
            assert prepared['scope']['board_session_id']==new_client.session_id
            new_checks_seen.append(prepared['id'])
        next_checks=ContinuationChecks(scope=new_scope,**{key:lambda _:None for key in ('claim','source','fence','runtime','resume')})
        next_task=ContinuationTask(task.journal,next_checks,clock=task.clock)
        new=DevelopmentProviderContinuation(next_task,new_worker,new_checkpoints)
        monkeypatch.setattr(provider,'read_usage',lambda *a,**k:usage(False))
        waiting=old.wake(job['id'],next_controller=new,preparer=preparer,next_operation_id=next_job['id'],parameters=parameters,
            request_id='provider-linux-next',after_actions=2)
        assert waiting['status']=='paused_provider' and waiting['retry_after_seconds']==30
        assert len(calls.read_text().splitlines())==2 and f.events==['plan','backend','policy']
        monkeypatch.setattr(provider,'read_usage',lambda *a,**k:usage(True))
        kwargs=dict(next_controller=new,preparer=preparer,next_operation_id=next_job['id'],parameters=parameters,
            request_id='provider-linux-next',after_actions=2)
        if wake_case in ('stale','scope','identity'):
            from blackoutburst_agents import development_provider_continuation as module
            from blackoutburst_agents.dev_manifest import DevelopmentError
            if wake_case=='stale':
                original=module.observe_provider_availability
                def stale(*args,**kwargs):
                    value=original(*args,**kwargs);object.__setattr__(value,'monotonic_at',-10000);return value
                monkeypatch.setattr(module,'observe_provider_availability',stale)
            elif wake_case=='scope':f.board.set_policy(f.human,True,False,True,True,[],[])
            else:kwargs['next_controller']=old
            with pytest.raises(Exception):old.wake(job['id'],**kwargs)
            assert len(calls.read_text().splitlines())==2 and f.events==['plan','backend','policy']
            assert store.status(development_work_id(job['id']))==old_work
            return
        if wake_case=='after_preparation':
            answers=iter([True,False]);monkeypatch.setattr(provider,'read_usage',lambda *a,**k:usage(next(answers)))
            assert old.wake(job['id'],**kwargs)['status']=='paused_provider'
            assert len(calls.read_text().splitlines())==2 and f.events==['plan','backend','policy']*2
            monkeypatch.setattr(provider,'read_usage',lambda *a,**k:usage(True))
        if wake_case=='ack':
            original=new.run_checkpoint
            def uncertain(*args,**kwargs):
                original(*args,**kwargs);raise BaseException('synthetic ACK lost after actual new cycle')
            monkeypatch.setattr(new,'run_checkpoint',uncertain)
            with pytest.raises(BaseException):old.wake(job['id'],**kwargs)
            assert old.status(job['id'])['status']=='needs_you'
            before=calls.read_bytes()
            with pytest.raises(Exception):old.wake(job['id'],**kwargs)
            assert calls.read_bytes()==before and store.status(development_work_id(job['id']))==old_work
            return
        if wake_case=='owner_pause':
            limits=f.board.get_runtime(f.human)['limits']
            f.board.update_runtime_limits(f.human,limits['version'],limits['max_concurrent'],True)
            result=old.wake(job['id'],**kwargs)
            assert result['status']=='needs_you' and len(calls.read_text().splitlines())==2
            assert f.board.get_runtime(f.human)['limits']['paused'] is True
            assert store.status(development_work_id(job['id']))==old_work
            return
        result=old.wake(job['id'],next_controller=new,preparer=preparer,next_operation_id=next_job['id'],parameters=parameters,
            request_id='provider-linux-next',after_actions=2)
        assert result['status']=='paused_cycle',result
        assert task.snapshot()['state']['used_total']['turns']==4 and task.snapshot()['state']['used_total']['actions']==3
        assert store.status(development_work_id(job['id']))==old_work
        assert {name:(directory/'broker'/name).read_bytes() for name in old_documents}==old_documents
        assert worker._stopped(job['id']) and journal.get(job['id'])['stage']=='running'
        assert new_client.session_id!=client.session_id and new_store.state_dir!=store.state_dir
        records=[json.loads(line) for line in calls.read_text().splitlines()]
        assert len(records)==4 and records[0]['session']==records[1]['session']!=records[2]['session']==records[3]['session']
        assert records[2]['resume'] is False and pool.status()['driver_calls']==0
        assert new_checks_seen and set(new_checks_seen)=={next_job['id']}
        monkeypatch.setattr(provider,'read_usage',lambda *a,**k:pytest.fail('cached wake reread the provider or ran again'))
        assert old.wake(job['id'],**kwargs)['historical'] is True
        assert len(calls.read_text().splitlines())==4
