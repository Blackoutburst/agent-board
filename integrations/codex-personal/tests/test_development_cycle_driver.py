"""Real Board/Worker/Planner/Runtime/Git; model and native execution are fake."""
from copy import deepcopy
from pathlib import Path
import uuid

import pytest

from blackoutburst_agents.continuation import ContinuationTask,ContinuationChecks
from blackoutburst_agents.dev_manifest import canonical,digest,DevelopmentError
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.dev_journal import DevelopmentJournal
from blackoutburst_agents.dev_runtime import development_work_id
from blackoutburst_agents.development_cycle_driver import DevelopmentCycleDriver
from blackoutburst_agents.private_development_checkpoint import PrivateDevelopmentCheckpointController
from test_development_checkpoint_proof import checkpoint_case as checkpoint_fixture,actions,git


@pytest.fixture
def cycle_case(request,monkeypatch):
    import test_development_worker as helpers
    from blackoutburst_agents.dev_planner_journal import PlannerBudgets
    original=helpers.DevelopmentWorkerTests.prepare
    def prepare(self,**kwargs):
        return original(self,**{'budgets':PlannerBudgets(max_wall_seconds=600),**kwargs})
    monkeypatch.setattr(helpers.DevelopmentWorkerTests,'prepare',prepare)
    fixture=checkpoint_fixture.__wrapped__(request);case=next(fixture)
    case.worker.planner_factory=DevelopmentPlanner;case.actions=actions(case)
    op=case.worker.journal.get(case.operation)
    journal=DevelopmentJournal(case.broker_dir)
    try:m=deepcopy(journal.manifest)
    finally:journal.close()
    tests={'tests':m['tests'],'profile':'legacy-named-test'}
    caps={'paths':sorted(m['files']),'tests':tests,'actions':m['actions'],'resource':m['resource'],
          'broker_limits':m['budgets'],'planner_limits':op['configuration']['budgets']}
    cfg={'schema':'private-development-checkpoint-config-v1','task_id':'logical-task',
        **{key:case.scope[key] for key in ('owner_id','project_id','ticket_id')},
        'repository_id':case.workspaces.grant['repository_id'],'resource':m['resource'],
        'original_public_base':case.base_commit,'global_paths':sorted(m['files']),
        'global_test_caps_sha256':digest(canonical(tests)),'static_caps_sha256':digest(canonical(caps)),
        'source_release_sha256':'b'*64}
    case.checkpoint_root=case.base/'cycle-checkpoints';case.checkpoint_root.mkdir(mode=0o700)
    case.producer=PrivateDevelopmentCheckpointController(case.workspaces,case.checkpoint_root,configuration=cfg)
    registration=op['payload']['registration'];case.logical_fault=None
    def scope(context):
        if case.logical_fault:raise DevelopmentError(case.logical_fault)
        policy=case.board.get_work(case.client.actor,project_id=case.project['id'],limit=1)['policy']
        if policy['enabled'] is not True or policy['allow_requests'] is not True:raise DevelopmentError('scope revoked')
    checks=ContinuationChecks(scope=scope,claim=lambda _:None,source=lambda _:None,fence=lambda _:None,
        runtime=lambda _:None,resume=lambda _:None)
    limits={'max_turns':64,'max_actions':64,'max_operations':128,'max_tests':10,
        'max_read_bytes':10000000,'max_patch_bytes':1048576,'max_wall_seconds':600}
    scope_cfg={key:case.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint')}
    scope_cfg.update(device_id=registration['identity']['device_id'],profile_version=registration['profile_version'],
        resource=m['resource'],source_release_sha256='b'*64)
    case.task=ContinuationTask.create(case.base/'logical',logical_task_id='logical-task',scope=scope_cfg,
        cycle_limits=limits,initial_source_sha256=digest(canonical({'main.py':digest(b'value = 1\n')})),checks=checks)
    case.addCleanup(case.task.close)
    case.driver=DevelopmentCycleDriver(case.task,case.worker,case.producer)
    case.release_calls=[]
    def release(**kwargs):
        case.release_calls.append(deepcopy(kwargs))
        return case.client.call('release_ticket',{**kwargs,'status':'todo','unassign_self':False})
    case.client.release_checkpoint_claim=release
    def run():
        snapshot=case.task.snapshot()
        start=case.task.start_cycle(expected_revision=snapshot['state']['revision'],request_id='cycle-'+str(snapshot['state']['cycle_seq']))
        return case.driver.run_checkpoint(case.operation,start=start,after_actions=2)
    case.run_cycle=run
    try:yield case
    finally:
        try:next(fixture)
        except StopIteration:pass


def test_normal_worker_hook_captures_before_close_then_retires_without_partial_review(cycle_case):
    case=cycle_case;result=case.run_cycle()
    assert result['status']=='paused_cycle',result
    assert case.task.snapshot()['state']['used_total']['turns']==2 and len(case.inferences)==2
    op=case.worker.journal.get(case.operation);assert op['stage']=='running'
    old={name:(case.broker_dir/name).read_bytes() for name in ('state.json','planner.json')}
    retired=case.driver.retire_checkpoint(case.operation)
    assert retired['phase']=='retired' and retired['launchable'] is False
    assert case.read_ticket()['status']=='todo' and case.read_ticket()['claimant'] is None
    assert len(case.release_calls)==1 and case.worker._stopped(case.operation)
    assert case.worker.journal.get(case.operation)==op
    assert {name:(case.broker_dir/name).read_bytes() for name in old}==old
    assert case.driver.retire_checkpoint(case.operation)['historical'] is True
    assert len(case.release_calls)==1 and len(case.inferences)==2 and not case.backend.calls


def test_captured_worker_ack_replay_is_historical_and_never_advances_planner(cycle_case):
    case=cycle_case;case.run_cycle();op=case.worker.journal.get(case.operation)
    from blackoutburst_agents.development_checkpoint_proof import DevelopmentCheckpointRequest
    o=op['payload']['checkpoint_capture']['observation']
    request=DevelopmentCheckpointRequest(case.worker,**{key:o[key] for key in
        ('task_id','generation_id','cycle_id','attempt_id','original_public_base','source_release_sha256',
         'parent_checkpoint_sha256')},after_actions=2)
    result=case.worker.run_checkpoint(case.operation,request=request,checkpoint_controller=case.producer)
    assert result['historical'] is True and result['status']=='checkpoint_ready' and len(case.inferences)==2


@pytest.mark.parametrize('mutation',['scope','pending_runtime','expired_claim','source','logical_stop'])
def test_retirement_refuses_drift_without_release_or_new_effect(cycle_case,mutation):
    case=cycle_case;case.run_cycle()
    if mutation=='scope':case.board.set_policy(case.human,True,False,True,True,[],[])
    elif mutation=='pending_runtime':
        original=case.control.call
        def pending(name,args):
            result=original(name,args)
            if name=='lookup_execution':result['execution']['cleanup_confirmed']=False
            return result
        case.control.call=pending
    elif mutation=='expired_claim':case.worker.clock=lambda:10**20
    elif mutation=='source':(case.root/'main.py').write_text('drift\n')
    else:
        s=case.task.snapshot()['state'];case.task.stop(expected_revision=s['revision'],request_id='human-stop')
    with pytest.raises(Exception):case.driver.retire_checkpoint(case.operation)
    assert case.release_calls==[] and len(case.inferences)==2 and not case.backend.calls


def test_release_ack_loss_retains_same_intent_and_refuses_later_claim(cycle_case):
    case=cycle_case;case.run_cycle();case.client.lost.add('release_ticket')
    with pytest.raises(DevelopmentError,match='development_cycle_release_uncertain'):
        case.driver.retire_checkpoint(case.operation)
    key=case.release_calls[0]['idempotency_key'];assert case.read_ticket()['claimant'] is None
    ticket=case.read_ticket();case.board.claim_ticket(case.client.actor,ticket['id'],ticket['version'])
    with pytest.raises(DevelopmentError):case.driver.retire_checkpoint(case.operation)
    assert len(case.release_calls)==1 and case.release_calls[0]['idempotency_key']==key
    assert case.read_ticket()['claimant'] is not None and len(case.inferences)==2


def test_same_release_rpc_confirms_lost_ack_without_a_second_ticket_transition(cycle_case):
    case=cycle_case;case.run_cycle();case.client.lost.add('release_ticket')
    with pytest.raises(DevelopmentError,match='development_cycle_release_uncertain'):
        case.driver.retire_checkpoint(case.operation)
    version=case.read_ticket()['version']
    result=case.driver.retire_checkpoint(case.operation)
    assert result['phase']=='retired' and case.read_ticket()['version']==version
    assert len(case.release_calls)==2 and case.release_calls[0]==case.release_calls[1]
    assert len(case.inferences)==2


def test_logical_stop_prevents_old_permit_and_factory_before_any_claim(cycle_case):
    case=cycle_case;s=case.task.snapshot();start=case.task.start_cycle(expected_revision=0,request_id='start')
    state=case.task.snapshot()['state'];case.task.stop(expected_revision=state['revision'],request_id='stop')
    assert case.driver.run_checkpoint(case.operation,start=start)['status']=='stopped'
    assert len(case.inferences)==0 and case.read_ticket()['claimant'] is None


def test_effective_caps_are_checked_before_model_after_normal_claim(cycle_case):
    case=cycle_case
    original=case.worker._claim
    def narrowed(op,auth):
        result=original(op,auth)
        budgets={**result['payload']['planner_budgets'],'max_wall_seconds':1}
        return case.worker._save(result,planner_budgets=budgets)
    case.worker._claim=narrowed
    result=case.run_cycle()
    assert result['status']=='needs_you' and result['reason']=='development_cycle_caps_changed'
    assert not case.inferences and not case.backend.calls


def test_pending_capture_intent_cannot_be_replayed_with_a_new_model(cycle_case):
    case=cycle_case;original=case.worker._run
    def crash(*args,**kwargs):raise BaseException('synthetic crash before worker admission')
    case.worker._run=crash
    with pytest.raises(BaseException):case.run_cycle()
    case.worker._run=original
    # A running logical attempt cannot be restarted with an alternative start.
    with pytest.raises(Exception):case.run_cycle()
    assert case.inferences==[] and case.read_ticket()['claimant'] is None


def test_lost_ack_after_git_seal_before_worker_receipt_never_replays(cycle_case):
    from blackoutburst_agents.development_checkpoint_proof import DevelopmentCheckpointRequest
    case=cycle_case;original=case.worker._save
    def crash(op,stage=None,**values):
        if 'checkpoint_capture' in values:raise BaseException('synthetic lost capture ACK')
        return original(op,stage,**values)
    case.worker._save=crash
    start=case.task.start_cycle(expected_revision=0,request_id='start')
    context=start.permit.context
    with pytest.raises(BaseException):case.driver.run_checkpoint(case.operation,start=start,after_actions=2)
    case.worker._save=original
    request=DevelopmentCheckpointRequest(case.worker,context.logical_task_id,context.generation_id,context.cycle_id,
        context.attempt_id,case.base_commit,'b'*64,after_actions=2)
    with pytest.raises(DevelopmentError,match='development_cycle_capture_uncertain'):
        case.worker.run_checkpoint(case.operation,request=request,checkpoint_controller=case.producer)
    assert len(case.inferences)==2 and case.read_ticket()['status']=='in_progress'


def test_changed_retirement_intent_is_not_self_authorizing(cycle_case):
    from blackoutburst_agents.dev_journal import PrivateDirectory,decode
    case=cycle_case;case.run_cycle();case.client.lost.add('release_ticket')
    with pytest.raises(DevelopmentError):case.driver.retire_checkpoint(case.operation)
    with PrivateDirectory(case.worker.journal.directory) as private:
        name='development-cycle-retirement-'+case.operation+'.json'
        value=decode(private.read(name,128*1024));value['request']['expected_version']+=10
        value['request_sha256']=digest(canonical(value['request']))
        private.replace(name,canonical(value))
    with pytest.raises(DevelopmentError,match='development_cycle_retirement_changed'):
        case.driver.retire_checkpoint(case.operation)
    assert len(case.release_calls)==1 and len(case.inferences)==2


@pytest.mark.parametrize('parent_mode',['correct','missing'])
def test_two_real_generations_keep_old_history_counters_fence_and_original_base(cycle_case,parent_mode):
    from blackoutburst_agents.git_workspace import LocalGitWorkspaceController,LocalRepositoryGrant
    from blackoutburst_agents.dev_manifest import prepare_manifest,NamedTest
    from blackoutburst_agents.work_journal import WorkJournal
    from blackoutburst_agents.run_store import RunStore
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    from blackoutburst_agents.dev_fence import DevelopmentFence
    from test_development_worker import FixtureClient
    from test_runtime_integration import ControlClient
    from test_dev_planner import FakePolicy
    case=cycle_case;first=case.run_cycle()['checkpoint'];case.driver.retire_checkpoint(case.operation)
    old_root,old_op,old_broker=case.root,case.operation,case.broker_dir
    old_worker=case.worker;old_journal=case.journal
    old_state={name:(old_broker/name).read_bytes() for name in ('state.json','planner.json')}
    old_runtime=[old_worker.runtime.get(row['id']) for row in old_worker.runtime.list_status()]
    fence=DevelopmentFence(_test_anchor=case.base/'anchor')
    try:old_fence=fence.inspect_checkout(first['checkout'] and old_worker.journal.get(old_op)['payload']['parent_binding']['root_identity'])
    finally:fence.close()
    client=FixtureClient(case.board,case.raw,session=str(uuid.uuid4()))
    registration=client.register();del client.registration;client.register_count=0
    case.client=client
    case.scope={**case.scope,'board_session_id':client.session_id,'agent_id':registration['identity']['id']}
    case.manifest_scope={key:case.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
    case.manifest_scope.update(device_id=registration['identity']['device_id'],session_id=client.session_id,
        profile_version=registration['profile_version'])
    case.control=ControlClient(case.board,case.raw,client.session_id)
    state=case.base/'next-git-state';work=case.base/'next-checkouts'
    state.mkdir(mode=0o700);work.mkdir(mode=0o700)
    case.workspaces=LocalGitWorkspaceController(state,grant=LocalRepositoryGrant('owner','fixture-repository',
        str(old_root),str(work),first['commit_sha']))
    case.operation=str(uuid.uuid4());case.workspaces.plan(operation_id=case.operation,scope=case.manifest_scope)
    case.root=Path(case.workspaces.materialize(case.operation)['checkout'])
    case.broker_dir=case.base/'next-broker';case.backend_dir=case.base/'next-backend';case.policy_dir=case.base/'next-policy'
    case.backend_dir.mkdir(mode=0o700);case.policy_dir.mkdir(mode=0o700)
    case.planner_root=case.base/'next-planner';case.planner_root.mkdir(mode=0o700)
    case.policy=FakePolicy(case.planner_root);case.policy.close=lambda:None
    prepare_manifest(case.broker_dir,root=case.root,base_sha=first['commit_sha'],scope=case.manifest_scope,
        paths=['main.py'],resource='repo:fixture',authorize_scope=lambda *_:True,
        tests=[NamedTest('unit',backend_id=case.backend.backend_id)])
    case.journal=WorkJournal(case.base/'next-work');case.addCleanup(case.journal.close)
    case.store=RunStore(case.base/'next-runs');case.addCleanup(case.store.close)
    case.runtime=RuntimeJournal(case.base/'next-runtime');case.addCleanup(case.runtime.close)
    case.worker=case.make_worker();case.worker.planner_factory=DevelopmentPlanner
    case.producer=PrivateDevelopmentCheckpointController(case.workspaces,case.checkpoint_root,
        configuration=case.producer.configuration)
    case.driver=DevelopmentCycleDriver(case.task,case.worker,case.producer)
    case.prepare(operation_id=case.operation,request_id='generation-two')
    case.actions=[case.action('patch',path='main.py',expected_sha256=digest(b'value = 2\n'),content='value = 3\n'),
        case.action('read',path='main.py')]
    start=case.task.start_cycle(expected_revision=case.task.snapshot()['state']['revision'],request_id='cycle-two')
    result=case.driver.run_checkpoint(case.operation,start=start,
        parent_checkpoint_id=first['checkpoint_id'] if parent_mode=='correct' else None,after_actions=2)
    if parent_mode=='missing':
        assert result['status']=='needs_you' and len(case.inferences)==2
        assert case.read_ticket()['claimant'] is None and not case.backend.calls
        assert old_worker._stopped(old_op)
        return
    assert result['status']=='paused_cycle',result
    second=result['checkpoint']
    assert second['original_public_base']==first['original_public_base']==case.base_commit
    assert second['base_commit']==first['commit_sha'] and second['parent_checkpoint_sha256']==first['checkpoint_sha256']
    assert second['configuration_sha256']==first['configuration_sha256']
    assert len(case.inferences)==4 and case.task.snapshot()['state']['cycle_seq']==2
    assert case.task.snapshot()['state']['used_total']['turns']==4
    assert {name:(old_broker/name).read_bytes() for name in old_state}==old_state
    assert [old_worker.runtime.get(row['id']) for row in old_worker.runtime.list_status()]==old_runtime
    assert old_worker._stopped(old_op) and old_journal.get(old_op)['stage']=='running'
    assert old_worker.store.state_dir!=case.store.state_dir
    assert old_worker.journal.get(old_op)['scope']['board_session_id']!=case.scope['board_session_id']
    assert old_worker.store.status(development_work_id(old_op))['work']['provider_session_id']!=case.store.status(development_work_id(case.operation))['work']['provider_session_id']
    fence=DevelopmentFence(_test_anchor=case.base/'anchor')
    try:assert fence.inspect_checkout(old_worker.journal.get(old_op)['payload']['parent_binding']['root_identity'])==old_fence
    finally:fence.close()
    assert git(old_root,'rev-parse','HEAD')==case.base_commit
    assert git(case.root,'show',second['commit_sha']+':main.py')=='value = 3'


def test_targeted_client_release_is_closed_and_unavailable_to_model_channel():
    from blackoutburst_agents.development_client import DevelopmentBoardClient
    from blackoutburst_agents.board_reader import BoardReadError
    client=object.__new__(DevelopmentBoardClient);client._closed=False;client._registered=True;client.session_id='existing'
    calls=[];client._initialize=lambda:calls.append('initialize')
    client._tool=lambda name,args:calls.append((name,args)) or {'version':8}
    args=dict(ticket_id='ticket',expected_version=7,idempotency_key='development-checkpoint-release-'+str(uuid.uuid4()),
              reason='Private source retained; clean generation retired.')
    assert client.release_checkpoint_claim(**args)=={'version':8}
    assert calls==['initialize',('release_ticket',{**args,'status':'todo','unassign_self':False,'session_id':'existing'})]
    with pytest.raises(BoardReadError):client.call('release_checkpoint_claim',args)
    with pytest.raises(BoardReadError):client.call('release_ticket',{**args,'status':'todo','unassign_self':False})


@pytest.mark.parametrize('change',[{'expected_version':True},{'expected_version':0},{'reason':''},
    {'idempotency_key':'development-checkpoint-release-'+'-'*36}])
def test_targeted_client_refuses_malformed_release_before_transport(change):
    from blackoutburst_agents.development_client import DevelopmentBoardClient
    from blackoutburst_agents.board_reader import BoardReadError
    client=object.__new__(DevelopmentBoardClient);client._closed=False;client._registered=True
    client._initialize=lambda:pytest.fail('invalid release reached transport')
    args=dict(ticket_id='ticket',expected_version=7,idempotency_key='development-checkpoint-release-'+str(uuid.uuid4()),reason='retire')
    with pytest.raises(BoardReadError):client.release_checkpoint_claim(**{**args,**change})


@pytest.mark.parametrize('mutation',['worker_payload','model_result','fence'])
def test_durable_capture_drift_is_not_a_cleanup_or_release_proof(cycle_case,mutation):
    case=cycle_case;case.run_cycle()
    if mutation=='worker_payload':
        op=case.worker.journal.get(case.operation)
        with case.journal.lock(op['scope']):case.worker._save(op,reason='changed after capture')
    elif mutation=='model_result':
        # Synthetic corrupt evidence only, never a production recovery API.
        row=case.store._db.execute('SELECT id,result_json FROM turns LIMIT 1').fetchone()
        import json
        value=json.loads(row['result_json']);value['text']='tampered accepted action'
        case.store._db.execute('UPDATE turns SET result_json=? WHERE id=?',(json.dumps(value),row['id']))
    else:
        from blackoutburst_agents.dev_fence import DevelopmentFence
        fence=DevelopmentFence(_test_anchor=case.base/'anchor')
        try:
            with fence.lock():
                state=fence._read();next(iter(state['records'].values()))['binding']['configuration_hash']='f'*64
                fence.private.replace('fence.json',canonical(state))
        finally:fence.close()
    with pytest.raises(Exception):case.driver.retire_checkpoint(case.operation)
    assert case.release_calls==[] and len(case.inferences)==2


@pytest.mark.parametrize('mutation',['stop','deadline'])
def test_logical_control_during_model_refuses_before_the_first_patch(cycle_case,mutation):
    case=cycle_case
    def change(callbacks):
        snapshot=case.task.snapshot()
        if mutation=='stop':case.task.stop(expected_revision=snapshot['state']['revision'],request_id='during-model-stop')
        else:case.task.clock=lambda:snapshot['state']['current_cycle']['started_at']+601
    case.after_model=change
    result=case.run_cycle()
    assert result['status'] in ('needs_you','stopped'),result
    assert (case.root/'main.py').read_bytes()==b'value = 1\n' and len(case.inferences)==1
    assert case.release_calls==[] and not case.backend.calls
    assert getattr(case.worker,'_checkpoint_execution',None) is None


def test_original_core_scope_callback_revoked_during_model_moves_own_attempt_to_needs_you(cycle_case):
    case=cycle_case
    case.after_model=lambda _:setattr(case,'logical_fault','continuation_scope_changed')
    result=case.run_cycle()
    assert result['status']=='needs_you' and result['worker_status']=='needs_review',result
    assert case.task.snapshot()['state']['phase']=='needs_you'
    assert case.task.snapshot()['state']['used_total']['turns']==1
    assert case.task.snapshot()['state']['checkpoint'] is None
    assert (case.root/'main.py').read_bytes()==b'value = 1\n' and len(case.inferences)==1
    assert case.release_calls==[] and not case.backend.calls


def test_core_ceiling_smaller_than_approved_engine_budget_refuses_before_claim(cycle_case):
    case=cycle_case;config=case.task.configuration
    task=ContinuationTask.create(case.base/'small-core',logical_task_id='logical-task',scope=config['scope'],
        cycle_limits={**config['cycle_limits'],'max_turns':1,'max_actions':1},
        initial_source_sha256=config['initial_source_sha256'],checks=case.task.checks)
    case.addCleanup(task.close);driver=DevelopmentCycleDriver(task,case.worker,case.producer)
    start=task.start_cycle(expected_revision=0,request_id='small-start')
    assert driver.run_checkpoint(case.operation,start=start,after_actions=2)['status']=='needs_you'
    assert case.inferences==[] and case.read_ticket()['claimant'] is None and not case.backend.calls


def test_callback_cost_is_rechecked_before_entering_worker(cycle_case):
    case=cycle_case;original=case.task.checks.callbacks;calls=[]
    def expensive(context):
        original[0](context)
        if context.purpose=='execute':
            calls.append(True)
            if len(calls)==2:
                cycle=case.task.snapshot()['state']['current_cycle']
                case.task.clock=lambda:cycle['started_at']+601
    checks=ContinuationChecks(scope=expensive,claim=original[1],source=original[2],fence=original[3],
        runtime=original[4],resume=original[5])
    # A distinct original view is created before any permit is issued.
    task=ContinuationTask(case.task.journal,checks,clock=lambda:case.task.clock())
    driver=DevelopmentCycleDriver(task,case.worker,case.producer)
    start=task.start_cycle(expected_revision=0,request_id='slow-start')
    assert driver.run_checkpoint(case.operation,start=start,after_actions=2)['status']=='needs_you'
    assert not case.inferences and case.read_ticket()['claimant'] is None


def test_revocation_at_boundary_after_private_seal_does_not_leave_logical_running(cycle_case):
    case=cycle_case;original=case.producer.capture
    def revoked(**kwargs):
        result=original(**kwargs);case.logical_fault='continuation_scope_changed';return result
    case.producer.capture=revoked
    result=case.run_cycle()
    assert result['status']=='needs_you',result
    assert case.task.snapshot()['state']['phase']=='needs_you'
    assert case.task.snapshot()['state']['checkpoint'] is None
    assert case.task.snapshot()['state']['used_total']['turns']==2
    assert len(case.inferences)==2 and case.release_calls==[]


def test_mutated_checkpoint_controller_refuses_before_first_model(cycle_case):
    case=cycle_case;case.producer.configuration['source_release_sha256']='c'*64
    result=case.run_cycle()
    assert result['status']=='needs_you'
    assert case.read_ticket()['claimant'] is None and case.inferences==[]


def test_duplicate_consumed_permit_cannot_mark_a_healthy_active_invocation(cycle_case):
    case=cycle_case;start=case.task.start_cycle(expected_revision=0,request_id='single-invocation')
    observed=[]
    def duplicate(callbacks):
        if observed:return
        before=case.task.snapshot()
        with pytest.raises(DevelopmentError,match='development_cycle_live_permit_required'):
            case.driver.run_checkpoint(case.operation,start=start,after_actions=2)
        observed.append((before,case.task.snapshot()))
    case.after_model=duplicate
    result=case.driver.run_checkpoint(case.operation,start=start,after_actions=2)
    assert observed[0][0]==observed[0][1]
    assert result['status']=='paused_cycle' and len(case.inferences)==2
