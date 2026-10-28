"""Real temporary Board/Runtime/RunStore/transport, synthetic local CLI only."""
from copy import deepcopy
import json
from pathlib import Path
import time
import threading

import pytest

from blackoutburst_agents.continuation import ContinuationTask,ContinuationChecks,ContinuationError,USAGE_FIELDS
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.continuation_runner import ContinuationRunner
from blackoutburst_agents.dev_manifest import canonical,digest
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_guard import RuntimeGuard
from blackoutburst_agents import provider_disposition as provider
import test_runtime_guard_security as runtime_helpers
import test_codex_turn as turn_helpers
from test_provider_disposition import usage


@pytest.fixture
def case(monkeypatch):
    if runtime_helpers.Board is None:pytest.skip('Board sources not bundled')
    runtime=runtime_helpers.RuntimeGuardSecurityTests(methodName='runTest');runtime.setUp()
    runtime.root=runtime.root.resolve()
    turn=turn_helpers.CodexTurnTests(methodName='runTest');turn.setUp()
    count=runtime.root/'fake-model-calls';body=f'''
import uuid
session=args[-2] if 'resume' in args else str(uuid.uuid4())
def started():
    event({{'type':'thread.started','thread_id':session}})
    event({{'type':'turn.started'}})
with open({str(count)!r},'a') as capture:
    capture.write(json.dumps({{'session':session,'resume':'resume' in args}})+'\\n')
if 'provider failure' in prompt:
    started()
    event({{'type':'turn.failed','error':{{'message':'provider unavailable PRIVATE_CAUSE'}}}})
    raise SystemExit(1)
success()
'''
    executable=turn.fake_cli(body)
    store=RunStore(runtime.root/'continuation-runs',continuation_v1=True)
    active=[runtime.actor];guards=[];fault={'code':None};prompts=['success'];source=['a'*64]
    scope={'owner_id':'owner','agent_id':runtime.registration['identity']['id'],'project_id':runtime.project['id'],
        'ticket_id':runtime.ticket['id'],'device_id':runtime.actor['device_id'],'profile_version':runtime.registration['profile_version'],
        'resource':'repo:fixture','board_endpoint':runtime.client.endpoint,'source_release_sha256':'b'*64}
    def check(context):
        assert dict(context.scope)==scope
        if fault['code']:raise ContinuationError(fault['code'])
    def runtime_check(context):
        if guards and context.attempt_id==guards[-1].scope['board_session_id'] and guards[-1].operation:
            assert guards[-1].operation['phase']=='finished'
    checks=ContinuationChecks(scope=check,claim=check,source=check,fence=check,runtime=runtime_check,resume=check)
    limits={f'max_{key}':4 for key in USAGE_FIELDS};limits['max_wall_seconds']=60
    task=ContinuationTask.create(runtime.root/'continuation',logical_task_id='logical-task',scope=scope,cycle_limits=limits,
        initial_source_sha256=source[0],checks=checks)
    def factory(context):
        # Explicit normal claim handoff in this trusted fixture, never raw SQL.
        ticket=runtime.board.get_ticket(runtime.human,runtime.ticket['id'])['ticket']
        if ticket['claimant']:
            runtime.board.release_ticket(active[0],ticket['id'],ticket['version'],'release-'+context.attempt_id,'Clean fixture cycle')
        raw={key:value for key,value in runtime.actor.items() if key!='session_id'}
        runtime.board.register_agent(raw,context.attempt_id,'Fixture',[],'codex',False,'fixture')
        actor={**raw,'session_id':context.attempt_id};active[0]=actor
        ticket=runtime.board.get_ticket(runtime.human,ticket['id'])['ticket']
        runtime.board.claim_ticket(actor,ticket['id'],ticket['version'])
        client=runtime_helpers.Client(runtime.board,actor);client.session_id=context.attempt_id
        guard=RuntimeGuard(runtime.journal,client,{**runtime.scope,'board_session_id':context.attempt_id})
        guards.append(guard);return guard
    def cp(context):
        return {'schema':1,'source_sha256':source[0],'diff_sha256':'c'*64,'plan_sha256':'d'*64,
            'test_receipts':[],'provider_receipt':None}
    def prompt(context,index):return prompts[index] if index<len(prompts) else prompts[-1]
    controller=ContinuationRunner(task,store,directory=runtime.root/'controller',workspace=turn.workspace,executable=executable,
        model='fixture-model',runtime_factory=factory,checkpoint_factory=cp,prompt_factory=prompt)
    monkeypatch.setattr(provider,'read_usage',lambda *a,**k:usage(True))
    data={'runtime':runtime,'turn':turn,'store':store,'task':task,'controller':controller,'fault':fault,'prompts':prompts,
        'count':count,'source':source,'factory':factory,'cp':cp,'prompt':prompt,'executable':executable,'guards':guards}
    yield data
    controller.close();task.close();store.close();runtime.doCleanups();turn.doCleanups()


def calls(case):return [] if not case['count'].exists() else case['count'].read_text().splitlines()


def test_two_real_cycles_preserve_history_and_new_work_request_provider_sessions(case):
    controller=case['controller']
    first=controller.run_cycle(request_id='first',turns=2)
    assert first['status']=='paused_cycle'
    second=controller.run_cycle(request_id='second',complete=True)
    assert second['status']=='complete' and len(calls(case))==3
    state=controller.task.snapshot()['state'];assert state['cycle_seq']==2 and state['used_total']['turns']==3
    rows=case['store']._db.execute('SELECT id,status,provider_session_id FROM works ORDER BY created_at').fetchall()
    assert len(rows)==2 and all(row['status']=='closed' for row in rows)
    assert len({row['provider_session_id'] for row in rows})==2
    turns=case['store']._db.execute('SELECT request_id FROM turns').fetchall();assert len({row['request_id'] for row in turns})==3
    runtime=case['runtime'].board.get_runtime(case['runtime'].human)
    assert all(row['state']=='finished' and row['cleanup_confirmed'] for row in runtime['executions'])


def test_provider_pause_reopen_and_fresh_wake_keeps_old_blocked_turn_review_flag(case):
    case['prompts'][:]=['success','provider failure'];controller=case['controller']
    result=controller.run_cycle(request_id='first',turns=2)
    assert result['status']=='paused_provider' and result['snapshot']['state']['used_total']['turns']==2
    old=case['store']._db.execute("SELECT * FROM turns WHERE status='blocked'").fetchone();assert old['requires_review']==1
    work_id=old['work_id'];assert case['store'].get_work(work_id)['status']=='closed'
    controller.close();case['task'].close();journal=ContinuationJournal.open(case['runtime'].root/'continuation')
    task=ContinuationTask(journal,case['task'].checks)
    controller=ContinuationRunner(task,case['store'],directory=case['runtime'].root/'controller',workspace=case['turn'].workspace,
        executable=case['executable'],model='fixture-model',runtime_factory=case['factory'],checkpoint_factory=case['cp'],prompt_factory=case['prompt'])
    case['task']=task;case['controller']=controller;case['prompts'][:]=['success']
    resumed=controller.wake(request_id='resume',complete=True)
    assert resumed['status']=='complete' and len(calls(case))==3
    assert dict(case['store']._turn(old['id']))==dict(old)


def test_duplicate_request_never_reexecutes_a_committed_cycle(case):
    controller=case['controller'];first=controller.run_cycle(request_id='same')
    assert controller.run_cycle(request_id='same')['historical'] is True
    assert len(calls(case))==1 and controller.task.snapshot()['state']['used_total']['turns']==1


@pytest.mark.parametrize('code',['continuation_scope_changed','continuation_profile_changed','continuation_source_changed',
    'continuation_claim_changed','continuation_fence_unknown','continuation_runtime_unknown'])
def test_fresh_drift_prevents_resume_without_another_model(case,code):
    case['controller'].run_cycle(request_id='first');case['fault']['code']=code
    result=case['controller'].run_cycle(request_id='second')
    assert result['status']=='needs_you' and len(calls(case))==1
    assert result['snapshot']['state']['phase']=='needs_you'


def test_provider_still_unavailable_returns_backoff_without_work_or_model(case,monkeypatch):
    case['prompts'][:]=['provider failure'];controller=case['controller'];controller.run_cycle(request_id='first')
    monkeypatch.setattr(provider,'read_usage',lambda *a,**k:usage(False))
    before=controller.task.snapshot();result=controller.wake(request_id='wake')
    assert result['status']=='paused_provider' and result['retry_after_seconds']==30
    assert controller.task.snapshot()==before and len(calls(case))==1
    assert len(case['store']._db.execute('SELECT id FROM works').fetchall())==1


def test_unknown_runtime_after_completed_turn_never_seals_or_resumes(case,monkeypatch):
    controller=case['controller']
    monkeypatch.setattr(controller,'_fresh_runtime',lambda *a:(_ for _ in ()).throw(ContinuationError('continuation_runtime_unknown')))
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    assert controller.task.snapshot()['state']['phase']=='needs_you' and len(calls(case))==1
    assert case['store']._db.execute('SELECT COUNT(*) FROM continuation_closures').fetchone()[0]==0
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='retry')
    assert len(calls(case))==1


@pytest.mark.parametrize('code',['continuation_profile_changed','continuation_source_changed','continuation_claim_changed','continuation_fence_unknown'])
def test_drift_during_checkpoint_capture_preserves_work_for_review(case,code):
    controller=case['controller'];original=controller.checkpoint_factory
    def checkpoint(context):case['fault']['code']=code;return original(context)
    controller.checkpoint_factory=checkpoint
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    assert controller.task.snapshot()['state']['phase']=='needs_you'
    assert len(calls(case))==1 and case['store']._db.execute('SELECT COUNT(*) FROM continuation_closures').fetchone()[0]==0


def test_stop_during_prompt_build_prevents_runtime_or_model_and_remains_dominant(case):
    controller=case['controller']
    def prompt(context,index):
        state=controller.task.snapshot()['state'];controller.task.stop(expected_revision=state['revision'],request_id='stop')
        return 'PRIVATE_PROMPT'
    controller.prompt_factory=prompt
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    assert not calls(case) and controller.task.snapshot()['state']['phase']=='stopped'
    assert not case['runtime'].board.get_runtime(case['runtime'].human)['executions']


@pytest.mark.parametrize('where',['seal','checkpoint','receipt'])
def test_lost_ack_cannot_create_an_extra_work_or_turn(case,monkeypatch,where):
    controller=case['controller']
    if where=='seal':
        original=case['store'].seal_continuation_work_v1
        def fail(*args,**kwargs):original(*args,**kwargs);raise RuntimeError('SYNTHETIC LOST ACK')
        monkeypatch.setattr(case['store'],'seal_continuation_work_v1',fail)
    elif where=='checkpoint':
        original=controller.task.checkpoint
        def fail(**kwargs):
            result=original(**kwargs)
            if kwargs['reason']!='needs_you':raise RuntimeError('SYNTHETIC LOST ACK')
            return result
        monkeypatch.setattr(controller.task,'checkpoint',fail)
    else:
        original=controller._record
        def fail(name,value):
            if name.startswith('receipt-'):raise RuntimeError('SYNTHETIC LOST ACK')
            return original(name,value)
        monkeypatch.setattr(controller,'_record',fail)
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    before=controller.task.snapshot()
    replay=controller.run_cycle(request_id='first')
    assert replay['historical'] is True and len(calls(case))==1
    assert controller.task.snapshot()==before
    assert case['store']._db.execute('SELECT COUNT(*) FROM works').fetchone()[0]==1
    assert case['store']._db.execute('SELECT COUNT(*) FROM turns').fetchone()[0]==1
    if where=='seal':assert before['state']['phase']=='needs_you'
    else:assert before['state']['phase']=='paused_cycle'


def test_provider_ack_loss_replay_is_metadata_without_wake_or_provider_reconsumption(case,monkeypatch):
    controller=case['controller'];case['prompts'][:]=['provider failure'];original=controller._record
    def fail(name,value):
        if name.startswith('receipt-'):raise RuntimeError('SYNTHETIC LOST ACK')
        return original(name,value)
    monkeypatch.setattr(controller,'_record',fail)
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    assert controller.task.snapshot()['state']['phase']=='paused_provider'
    replay=controller.run_cycle(request_id='first')
    assert replay['historical'] is True and len(calls(case))==1


def test_human_owner_pause_is_never_overridden_by_fresh_provider_availability(case):
    controller=case['controller'];case['prompts'][:]=['provider failure'];controller.run_cycle(request_id='first')
    runtime=case['runtime'];limits=runtime.board.get_runtime(runtime.human)['limits']
    runtime.board.update_runtime_limits(runtime.human,limits['version'],limits['max_concurrent'],True)
    with pytest.raises(ContinuationError):controller.wake(request_id='wake')
    assert len(calls(case))==1 and runtime.board.get_runtime(runtime.human)['limits']['paused'] is True


def test_changed_controller_configuration_cannot_adopt_existing_checkpoint(case):
    controller=case['controller'];controller.run_cycle(request_id='first')
    with pytest.raises(ContinuationError):ContinuationRunner(case['task'],case['store'],directory=case['runtime'].root/'controller',
        workspace=case['turn'].workspace,executable=case['executable'],model='different-model',runtime_factory=case['factory'],
        checkpoint_factory=case['cp'],prompt_factory=case['prompt'])
    assert len(calls(case))==1


def test_serialized_checkpoint_and_receipts_contain_no_private_response_prompt_or_runtime_paths(case):
    controller=case['controller'];case['prompts'][:]=['provider failure PRIVATE_PROMPT'];controller.run_cycle(request_id='first')
    raw=''.join(path.read_text() for path in (case['runtime'].root/'controller').iterdir())
    assert 'PRIVATE_PROMPT' not in raw and 'PRIVATE_CAUSE' not in raw and 'Final private reply' not in raw
    assert str(case['runtime'].root) not in raw and str(case['turn'].workspace) not in raw


def test_unknown_earlier_runtime_in_same_cycle_blocks_the_final_boundary(case):
    controller=case['controller'];original=controller.checkpoint_factory
    def checkpoint(context):
        guard=case['guards'][-1];operation=guard.operation
        rows=guard.journal.list_status();older=[guard.journal.get(row['id']) for row in rows
            if row['id']!=operation['id']]
        assert len(older)==1
        key=older[0]['key'];call=guard.client.call
        def uncertain(name,args):
            value=call(name,args)
            if name=='lookup_execution' and args['idempotency_key']==key:
                value=deepcopy(value);value['execution']['cleanup_confirmed']=False
            return value
        guard.client.call=uncertain
        return original(context)
    controller.checkpoint_factory=checkpoint
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='two',turns=2)
    assert controller.task.snapshot()['state']['phase']=='needs_you' and len(calls(case))==2
    assert case['store']._db.execute('SELECT COUNT(*) FROM continuation_closures').fetchone()[0]==0


def test_other_thread_cannot_replace_wake_evidence_or_launch_a_cycle(case):
    controller=case['controller'];case['prompts'][:]=['provider failure'];controller.run_cycle(request_id='first')
    errors=[]
    def wake():
        try:controller.wake(request_id='other-thread')
        except Exception as error:errors.append(error)
    worker=threading.Thread(target=wake);worker.start();worker.join()
    assert len(errors)==1 and controller._wake is None and len(calls(case))==1
    assert controller.task.snapshot()['state']['phase']=='paused_provider'


def test_legacy_store_is_not_adopted_or_migrated_by_the_controller(case):
    legacy=RunStore(case['runtime'].root/'legacy-store')
    try:
        with pytest.raises(ContinuationError):ContinuationRunner(case['task'],legacy,directory=case['runtime'].root/'legacy-controller',
            workspace=case['turn'].workspace,executable=case['executable'],model='fixture-model',runtime_factory=case['factory'],
            checkpoint_factory=case['cp'],prompt_factory=case['prompt'])
        assert legacy._db.execute('PRAGMA user_version').fetchone()[0]==2
        assert not (case['runtime'].root/'legacy-controller').exists()
    finally:legacy.close()


def test_serialized_provider_receipt_cannot_make_a_clean_boundary(case,monkeypatch):
    controller=case['controller'];case['prompts'][:]=['provider failure']
    original=provider.consume_provider_receipt
    import blackoutburst_agents.continuation_runner as module
    def serialized(receipt,**bindings):return original(json.loads(receipt.metadata_json),**bindings)
    monkeypatch.setattr(module,'consume_provider_receipt',serialized)
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    assert controller.task.snapshot()['state']['phase']=='needs_you'
    assert case['store']._db.execute('SELECT COUNT(*) FROM continuation_closures').fetchone()[0]==0
    turn=case['store']._db.execute('SELECT * FROM turns').fetchone()
    assert turn['status']=='blocked' and turn['requires_review']==1


@pytest.mark.parametrize('reason',['provider','cycle','complete'])
def test_revocation_after_durable_seal_never_reports_pause_or_completion(case,monkeypatch,reason):
    controller=case['controller']
    if reason=='provider':case['prompts'][:]=['provider failure']
    seal=case['store'].seal_continuation_work_v1
    def revoked(*args,**kwargs):
        result=seal(*args,**kwargs);case['fault']['code']='continuation_scope_changed';return result
    monkeypatch.setattr(case['store'],'seal_continuation_work_v1',revoked)
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first',complete=reason=='complete')
    assert controller.task.snapshot()['state']['phase']=='needs_you'
    assert case['store']._db.execute('SELECT status FROM works').fetchone()[0]=='closed'
    replay=controller.run_cycle(request_id='first',complete=reason=='complete')
    assert replay['historical'] is True and replay['snapshot']['state']['phase']=='needs_you' and len(calls(case))==1


def test_another_controller_without_a_permit_cannot_mark_the_active_cycle_for_review(case):
    controller=case['controller']
    other=ContinuationRunner(case['task'],case['store'],directory=case['runtime'].root/'controller',workspace=case['turn'].workspace,
        executable=case['executable'],model='fixture-model',runtime_factory=case['factory'],checkpoint_factory=case['cp'],prompt_factory=case['prompt'])
    original=controller.prompt_factory
    def nested(context,index):
        before=controller.task.snapshot()
        with pytest.raises(ContinuationError):other.run_cycle(request_id='unowned')
        assert controller.task.snapshot()==before
        return original(context,index)
    controller.prompt_factory=nested
    try:assert controller.run_cycle(request_id='first')['status']=='paused_cycle'
    finally:other.close()
    assert len(calls(case))==1


@pytest.mark.parametrize('changed',[99.,160.])
def test_prompt_build_cannot_move_clock_backwards_or_beyond_cycle_limit(case,changed):
    controller=case['controller'];clock=[100.];controller.task.clock=lambda:clock[0]
    def prompt(context,index):clock[0]=changed;return 'PRIVATE_PROMPT'
    controller.prompt_factory=prompt
    with pytest.raises(ContinuationError):controller.run_cycle(request_id='first')
    assert not calls(case) and controller.task.snapshot()['state']['phase']=='needs_you'
    assert not case['runtime'].board.get_runtime(case['runtime'].human)['executions']


def test_real_quota_prose_soft_pause_and_wake_do_not_toggle_owner_runtime_pause(case):
    # The opt-in transport deliberately reports an unknown terminal cause.
    # No heuristic prose is permission to pause or unpause the whole owner.
    executable=case['executable']
    executable.write_text(executable.read_text().replace('provider unavailable PRIVATE_CAUSE','quota PRIVATE_CAUSE'))
    controller=case['controller'];case['prompts'][:]=['provider failure']
    first=controller.run_cycle(request_id='quota')
    assert first['status']=='paused_provider'
    assert case['runtime'].board.get_runtime(case['runtime'].human)['limits']['paused'] is False
    case['prompts'][:]=['success']
    second=controller.wake(request_id='after-quota',complete=True)
    assert second['status']=='complete' and len(calls(case))==2
    assert case['runtime'].board.get_runtime(case['runtime'].human)['limits']['paused'] is False
    old=case['store']._db.execute("SELECT * FROM turns WHERE status='blocked'").fetchone()
    assert old['requires_review']==1 and old['resolution'] is None
