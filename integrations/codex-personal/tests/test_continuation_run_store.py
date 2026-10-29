"""Fresh schema3, real temporary stores/Runtime/Board and synthetic CLI only."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid
from contextlib import contextmanager

import pytest

from blackoutburst_agents.run_store import RunStore, StoreError
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents import provider_disposition as provider
from blackoutburst_agents.continuation import ContinuationTask, ContinuationChecks, USAGE_FIELDS
import test_codex_turn as turns
import test_runtime_guard_security as runtimes


@pytest.fixture
def case():
    if runtimes.Board is None:pytest.skip('Board sources unavailable')
    turn=turns.CodexTurnTests(methodName='runTest');turn.setUp()
    rt=runtimes.RuntimeGuardSecurityTests(methodName='runTest');rt.setUp()
    mode=turn.directory/'mode';mode.write_text('success')
    executable=turn.fake_cli(f"if open({str(mode)!r}).read()=='success':\n    success()\nelse:\n    started()\n    event({{'type':'turn.failed','error':{{'message':'quota PRIVATE'}}}})\n    raise SystemExit(1)")
    store=RunStore(rt.root/'continuation-runs',continuation_v1=True)
    work=store.create_work(**{key:rt.work[key] for key in ('owner_id','agent_id','project_id','ticket_id','workspace','model')},executable=str(executable))
    guard=rt.guard();runner=PersonalRunner(store,runtime_guard=guard)
    calls=[];refs={};faults=set()
    def checked(name):
        def check(context):
            calls.append(name)
            if name in faults:raise StoreError('continuation_'+name+'_unknown')
            if name=='runtime':
                executions=rt.board.get_runtime(rt.human)['executions']
                assert all(row['state']=='finished' and row['cleanup_confirmed'] for row in executions)
            if name=='provider':
                assert context.provider_receipt_sha256==refs['sha']
                closure=store.continuation_closure(work['id'])
                if closure is None:assert store.continuation_state_sha256(work['id'])==refs['pre']
                else:
                    assert closure['pre_state_sha256']==refs['pre']
                    assert closure['provider_receipt_sha256']==refs['sha']
                    assert closure['post_state_sha256']==store.continuation_state_sha256(work['id'])
        return check
    checks=ContinuationChecks(**{name:checked(name) for name in ('scope','claim','source','fence','runtime','resume')},
        provider_observation=checked('provider'))
    scope={'owner_id':rt.scope['owner_id'],'agent_id':rt.scope['agent_id'],'project_id':rt.scope['project_id'],
        'ticket_id':rt.scope['ticket_id'],'device_id':rt.registration['identity']['device_id'],'profile_version':1,
        'resource':'repo:synthetic','board_endpoint':rt.scope['board_endpoint'],'source_release_sha256':'a'*64}
    limits={**{'max_'+key:32 for key in USAGE_FIELDS},'max_wall_seconds':600}
    task=ContinuationTask.create(rt.root.resolve()/'logical-task',logical_task_id='logical-task',scope=scope,
        cycle_limits=limits,initial_source_sha256='b'*64,checks=checks)
    start=task.start_cycle(expected_revision=0,request_id='start')
    with start.permit.use() as context:
        binding={key:getattr(context,key) for key in ('generation_id','cycle_id','attempt_id')}
    binding.update(task_id='logical-task',scope_sha256=provider._sha(guard.scope))
    with store.work_lock(work['id']):store.enroll_continuation_work_v1(work['id'],**binding)
    result={'turn':turn,'rt':rt,'store':store,'work':work,'guard':guard,'runner':runner,'mode':mode,
        'task':task,'binding':binding,'calls':calls,'refs':refs,'faults':faults}
    try:yield result
    finally:
        task.close();store.close();rt.doCleanups();turn.doCleanups()


def completed(case, request='completed'):
    case['mode'].write_text('success')
    return case['runner'].run(case['work']['id'],request,'PRIVATE prompt',timeout=3)


def provider_failed(case):
    case['mode'].write_text('failed');receipts=[]
    attempt=provider.enroll_provider_attempt(**case['binding'],work_id=case['work']['id'],
        request_id='provider',on_receipt=receipts.append)
    response=case['runner'].run(case['work']['id'],'provider','PRIVATE prompt',timeout=3,provider_attempt=attempt)
    assert len(receipts)==1
    proof=receipts[0]
    with case['store'].work_lock(case['work']['id']):
        pre=case['store'].continuation_state_sha256(case['work']['id'])
        metadata=provider.consume_provider_receipt(proof,**case['binding'],work_id=case['work']['id'],
            turn_id=response['turn_id'],request_id='provider',result_sha256=provider._sha(response['result']),work_state_sha256=pre)
    assert metadata['work_state_sha256']==pre
    case['refs'].update(pre=pre,sha=hashlib.sha256(proof.metadata_json.encode()).hexdigest())
    return response,proof,pre


def checkpoint(case, *, proof=None):
    return {'schema':1,'source_sha256':'b'*64,'diff_sha256':'c'*64,'plan_sha256':'d'*64,'test_receipts':[],
        'provider_receipt':None if proof is None else {'work_id':case['work']['id'],
            'turn_id':json.loads(proof.metadata_json)['turn_id'],'receipt_sha256':case['refs']['sha']}}


def used(turns):return {**dict.fromkeys(USAGE_FIELDS,0),'turns':turns}


def test_schema3_requires_explicit_fresh_owner_and_reopen_optin(tmp_path):
    legacy=RunStore(tmp_path/'legacy');legacy.close()
    before=(tmp_path/'legacy/runs.sqlite3').read_bytes()
    with pytest.raises(StoreError,match='continuation_fresh_state_required'):RunStore(tmp_path/'legacy',continuation_v1=True)
    assert (tmp_path/'legacy/runs.sqlite3').read_bytes()==before
    fresh=RunStore(tmp_path/'fresh',continuation_v1=True)
    assert fresh._db.execute('PRAGMA user_version').fetchone()[0]==3
    assert fresh._db.execute('PRAGMA application_id').fetchone()[0]!=0
    fresh.close()
    with pytest.raises(StoreError):RunStore(tmp_path/'fresh')
    reopened=RunStore(tmp_path/'fresh',continuation_v1=True,existing_only=True);reopened.close()


def test_full_multiturn_cas_covers_earlier_results_and_context(case):
    completed(case,'first');completed(case,'second')
    store=case['store'];work=case['work']['id']
    with store.work_lock(work):
        before=store.continuation_state_sha256(work)
        first=store.status(work)['turns'][0]['id'];raw=store._turn(first)['result_json']
        with store._transaction() as db:
            db.execute('UPDATE turns SET result_json=? WHERE id=?',(raw.replace('Final private reply','Earlier changed reply'),first))
        assert store.continuation_state_sha256(work)!=before
        with pytest.raises(StoreError,match='interruption_state_conflict'):store.interruption_state_sha256(work)
        previous=store.continuation_state_sha256(work)
        store._db.execute('INSERT INTO context_guards VALUES(?,?)',(work,'synthetic changed guard bytes'))
        assert store.continuation_state_sha256(work)!=previous


def test_provider_seal_preserves_entire_multiturn_history_and_acks(case):
    completed(case,'first');completed(case,'second')
    response,proof,pre=provider_failed(case)
    store=case['store'];work=case['work']['id'];task=case['task'];intent=str(uuid.uuid4())
    with store.work_lock(work):
        prior=copy.deepcopy(store._continuation_state(work))
        cp=checkpoint(case,proof=proof)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=used(3),reason='provider')
        closure=store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,
            reason='provider',boundary=boundary,provider_receipt=proof)
        assert closure['historical'] is False
        after=store._continuation_state(work)
        assert after['turns']==prior['turns'] and after['context_guard']==prior['context_guard']
        assert after['work']=={**prior['work'],'status':'closed','updated_at':closure['closed_at']}
        assert after['turns'][-1]['requires_review']==1
        assert after['turns'][-1]['status']=='blocked'
        assert closure['post_state_sha256']==store.continuation_state_sha256(work)!=pre
        committed=task.checkpoint(expected_revision=1,request_id='pause',checkpoint=cp,cycle_used=used(3),reason='provider',boundary=boundary)
        assert committed['snapshot']['state']['phase']=='paused_provider'
        assert case['calls'].count('provider')==3
        calls=list(case['calls'])
        repeated=store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='provider',boundary=None)
        assert repeated['historical'] and repeated['launchable'] is False and calls==case['calls']
        assert repeated['closure_sha256']==closure['closure_sha256']
        with pytest.raises(StoreError,match='continuation_closure_conflict'):
            store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),reason='provider',boundary=None)
    next_work=store.create_work(**{key:case['work'][key] for key in
        ('owner_id','agent_id','project_id','ticket_id','workspace','executable','model')})
    assert next_work['id']!=work and next_work['provider_session_id'] is None
    assert store.get_work(work)['provider_session_id']==response['result']['session_id']


@pytest.mark.parametrize('reason',['cycle','complete'])
def test_clean_completed_cycle_closes_without_changing_turns(case,reason):
    completed(case,'first');completed(case,'second')
    store=case['store'];work=case['work']['id'];task=case['task']
    with store.work_lock(work):
        before=store._continuation_state(work);pre=store.continuation_state_sha256(work);cp=checkpoint(case)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=used(2),reason=reason)
        closure=store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),reason=reason,boundary=boundary)
        assert closure['provider_receipt_sha256'] is None
        assert store._continuation_state(work)['turns']==before['turns']
        result=task.checkpoint(expected_revision=1,request_id='seal',checkpoint=cp,cycle_used=used(2),reason=reason,boundary=boundary)
        assert result['snapshot']['state']['phase']==('complete' if reason=='complete' else 'paused_cycle')


@pytest.mark.parametrize('name',['scope','claim','source','fence','runtime'])
def test_changed_fresh_boundary_cannot_close_work(case,name):
    completed(case)
    store=case['store'];work=case['work']['id'];task=case['task']
    with store.work_lock(work):
        pre=store.continuation_state_sha256(work)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case),cycle_used=used(1),reason='cycle')
        case['faults'].add(name)
        with pytest.raises(Exception):store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),reason='cycle',boundary=boundary)
        assert store.get_work(work)['status']=='open' and store.continuation_closure(work) is None


def test_serialized_provider_or_stale_cas_cannot_close(case):
    response,proof,pre=provider_failed(case)
    store=case['store'];work=case['work']['id'];task=case['task']
    with store.work_lock(work):
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case,proof=proof),cycle_used=used(1),reason='provider')
        with pytest.raises(ValueError):store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),reason='provider',boundary=boundary,provider_receipt=json.loads(proof.metadata_json))
        with pytest.raises(StoreError,match='continuation_state_conflict'):
            store.seal_continuation_work_v1(work,expected_state_sha256='0'*64,intent_id=str(uuid.uuid4()),reason='provider',boundary=boundary,provider_receipt=proof)
        assert store.get_work(work)['status']=='needs_review' and store.continuation_closure(work) is None


def test_ledgers_are_append_only_and_state_limit_refuses(case,monkeypatch):
    store=case['store'];work=case['work']['id']
    with pytest.raises(sqlite3.IntegrityError):store._db.execute('DELETE FROM continuation_enrollments')
    completed(case,'one');completed(case,'two')
    import blackoutburst_agents.run_store as module
    monkeypatch.setattr(module,'CONTINUATION_MAX_TURNS',1)
    with store.work_lock(work):
        with pytest.raises(StoreError,match='continuation_state_too_large'):store.continuation_state_sha256(work)


def test_enrollment_never_adopts_an_existing_executed_work(case):
    completed(case)
    store=case['store'];work=case['work']['id']
    with store.work_lock(work):
        original=store.enroll_continuation_work_v1(work,**case['binding'])
        assert original['historical'] and original['launchable'] is False
        changed={**case['binding'],'attempt_id':str(uuid.uuid4())}
        with pytest.raises(StoreError,match='continuation_enrollment_conflict'):
            store.enroll_continuation_work_v1(work,**changed)


def test_unenrolled_schema3_work_cannot_launch_any_turn(tmp_path):
    store=RunStore(tmp_path/'runs',continuation_v1=True)
    try:
        import sys
        work=store.create_work(owner_id='owner',agent_id='agent',project_id='project',ticket_id='ticket',
            workspace=str(tmp_path),executable=sys.executable)
        with store.work_lock(work['id']):
            with pytest.raises(StoreError,match='continuation_enrollment_required'):
                store.begin_turn(work['id'],'unregistered','prompt')
            assert store.status(work['id'])['turns']==[]
    finally:store.close()


@pytest.mark.parametrize('fault',['cancel','cleanup','result_status','running'])
def test_clean_cycle_never_normalizes_uncertain_history(case,fault):
    response=completed(case)
    store=case['store'];work=case['work']['id'];task=case['task']
    with store.work_lock(work):
        turn=store._turn(response['turn_id'])
        if fault=='cancel':store._db.execute('UPDATE turns SET cancel_requested=1 WHERE id=?',(turn['id'],))
        elif fault=='running':store._db.execute("UPDATE turns SET status='running' WHERE id=?",(turn['id'],))
        else:
            result=json.loads(turn['result_json'])
            if fault=='cleanup':result['verification']['cleanup_confirmed']=False
            else:result['status']='blocked'
            from blackoutburst_agents.run_store import _result_json
            store._db.execute('UPDATE turns SET result_json=? WHERE id=?',(_result_json(result),turn['id']))
        pre=store.continuation_state_sha256(work)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case),cycle_used=used(1),reason='cycle')
        with pytest.raises(StoreError,match='continuation_predecessor_invalid'):
            store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),reason='cycle',boundary=boundary)
        assert store.get_work(work)['status']=='open' and store.continuation_closure(work) is None


def test_commit_ack_loss_is_read_only_idempotent_closure_not_a_permit(case,monkeypatch):
    completed(case)
    store=case['store'];work=case['work']['id'];task=case['task'];intent=str(uuid.uuid4())
    with store.work_lock(work):
        pre=store.continuation_state_sha256(work)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case),cycle_used=used(1),reason='cycle')
        transaction=store._transaction
        @contextmanager
        def lost():
            with transaction() as db:yield db
            raise OSError('synthetic commit acknowledgement lost')
        monkeypatch.setattr(store,'_transaction',lost)
        with pytest.raises(OSError):
            store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='cycle',boundary=boundary)
        calls=list(case['calls'])
        historical=store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='cycle',boundary=None)
        assert historical['historical'] and historical['launchable'] is False
        assert case['calls']==calls and task.snapshot()['state']['phase']=='running'
        assert store.get_work(work)['status']=='closed'


def test_post_seal_state_drift_invalidates_historical_ack(case):
    completed(case)
    store=case['store'];work=case['work']['id'];task=case['task'];intent=str(uuid.uuid4())
    with store.work_lock(work):
        pre=store.continuation_state_sha256(work)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case),cycle_used=used(1),reason='cycle')
        store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='cycle',boundary=boundary)
        store._db.execute('UPDATE turns SET cancel_requested=1 WHERE work_id=?',(work,))
        with pytest.raises(StoreError,match='continuation_closure_invalid'):
            store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='cycle',boundary=None)


def test_reopen_retains_enrollment_and_exact_closure_without_reexecuting(case):
    completed(case)
    store=case['store'];work=case['work']['id'];task=case['task'];intent=str(uuid.uuid4())
    with store.work_lock(work):
        pre=store.continuation_state_sha256(work)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case),cycle_used=used(1),reason='cycle')
        first=store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='cycle',boundary=boundary)
    state_dir=store.state_dir
    store.close()
    # The fixture closes this handle again safely; the reopened handle is local.
    reopened=RunStore(state_dir,continuation_v1=True,existing_only=True)
    try:
        with reopened.work_lock(work):
            assert reopened.continuation_enrollment(work)==store_binding(case)
            historical=reopened.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=intent,reason='cycle',boundary=None)
            assert historical['closure_sha256']==first['closure_sha256'] and historical['historical']
            assert historical['launchable'] is False
    finally:reopened.close()


def test_replace_cannot_bypass_append_only_ledgers(case):
    completed(case)
    store=case['store'];work=case['work']['id'];task=case['task']
    with store.work_lock(work):
        pre=store.continuation_state_sha256(work)
        boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(case),cycle_used=used(1),reason='cycle')
        store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),reason='cycle',boundary=boundary)
        for table in ('continuation_enrollments','continuation_closures'):
            row=dict(store._db.execute('SELECT * FROM '+table+' WHERE work_id=?',(work,)).fetchone())
            query='INSERT OR REPLACE INTO '+table+' VALUES('+','.join('?' for _ in row)+')'
            with pytest.raises(sqlite3.IntegrityError,match='append_only'):store._db.execute(query,tuple(row.values()))
        store._db.execute('PRAGMA recursive_triggers=OFF')
        with pytest.raises(StoreError,match='state_schema_invalid'):store.continuation_state_sha256(work)


def store_binding(case):
    return {**case['binding'],'schema':'runstore-continuation-enrollment-v1','work_id':case['work']['id'],
        'work_configuration_sha256':provider._sha(case['store']._continuation_work_config(case['work']))}
