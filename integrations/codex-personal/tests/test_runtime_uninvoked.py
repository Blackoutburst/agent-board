"""Temporary real Board/Runtime, reply-loss faults and a never-invoked model."""
from copy import copy,deepcopy
import pickle

import pytest

from blackoutburst_agents import runtime_uninvoked as module
from blackoutburst_agents.runtime_guard import RuntimeControlError,RuntimeGuard
from blackoutburst_agents.runtime_client import runtime_failure
from test_review_worker import review_case,space,run


def state(case,result):
    op=case['journal'].get(result['operation_id'])
    turns=case['store'].status(op['payload']['work_id'])['turns']
    assert len(turns)==1 and turns[0]['status']=='blocked'
    value=case['store'].turn_result(turns[0]['id'])
    value=deepcopy(value)
    diagnostic=value['verification'].pop('runtime_failure',None)
    if diagnostic is not None:assert runtime_failure(diagnostic)==diagnostic
    assert value=={'status':'blocked','reason':'runtime_start_requires_review','text':'',
        'uncertain':True,'verification':{'cleanup_confirmed':True}}
    records=case['runtime'].list_status()
    assert len(records)==1 and not case['calls']
    return case['runtime'].get(records[0]['id']),case['board'].get_runtime(case['human'])['executions']


def fault(case,boundary,cleanup_fault=None):
    original=case['client'].call;fired=[]
    selected='reserve_execution' if boundary.startswith('reserve') else 'start_execution'
    def call(name,args):
        if name==selected and not fired:
            fired.append(True)
            if boundary.endswith('after'):original(name,args)
            raise TimeoutError('synthetic admission reply loss')
        if name=='lookup_execution' and cleanup_fault=='lookup':
            raise TimeoutError('synthetic lookup reply loss')
        if name=='lookup_execution' and cleanup_fault in ('id','device_id','profile_version','work_id'):
            result=deepcopy(original(name,args))
            result['execution'][cleanup_fault]=(999 if cleanup_fault=='profile_version' else 'foreign')
            return result
        if name=='finish_execution' and cleanup_fault in ('finish_before','finish_after'):
            if cleanup_fault=='finish_after':original(name,args)
            raise TimeoutError('synthetic finish reply loss')
        return original(name,args)
    case['client'].call=call


@pytest.mark.parametrize('boundary',['reserve_before','reserve_after','start_before','start_after'])
def test_new_failed_admission_closes_only_its_unused_slot(review_case,boundary):
    case=review_case;fault(case,boundary)
    result=run(case);local,remote=state(case,result)
    assert result['reason']=='review_execution_requires_reconciliation'
    assert local['start_confirmed']==0
    if boundary=='reserve_before':
        assert not remote and local['phase']=='needs_review' and local['receipt'] is None
    else:
        assert len(remote)==1 and local['phase']=='finished'
        assert remote[0]['state']=='finished' and remote[0]['cleanup_confirmed'] is True
        assert remote[0]['outcome']=='blocked' and remote[0]['reason']=='execution_failed'
        assert local['receipt']['id']==remote[0]['id']
        assert local['receipt']['state']=='finished'
    assert case['board'].get_runtime(case['human'])['active_count']==0
    assert case['client'].calls.count('reserve_execution')==int(boundary!='reserve_before')
    assert case['client'].calls.count('start_execution')==int(boundary=='start_after')
    assert case['client'].calls.count('finish_execution')==int(boundary!='reserve_before')


@pytest.mark.parametrize('cleanup_fault',['lookup','finish_before','finish_after'])
def test_lost_cleanup_response_stays_unconfirmed(review_case,cleanup_fault):
    case=review_case;fault(case,'start_before',cleanup_fault)
    result=run(case);local,remote=state(case,result)
    assert local['phase']=='needs_review' and local['start_confirmed']==0
    assert len(remote)==1
    assert remote[0]['state']==('finished' if cleanup_fault=='finish_after' else 'reserved')
    assert local['receipt']['state']=='reserved'


@pytest.mark.parametrize('field',['id','device_id','profile_version','work_id'])
def test_changed_reservation_identity_is_refused_before_finish(review_case,field):
    case=review_case;fault(case,'start_before',field)
    result=run(case);local,remote=state(case,result)
    assert local['phase']=='needs_review' and remote[0]['state']=='reserved'
    assert 'finish_execution' not in case['client'].calls


def test_plain_data_cannot_authorize_cleanup(review_case,monkeypatch):
    case=review_case;captured=[];before=RuntimeGuard.before
    def inspect(guard,work,turn):
        captured.append((guard,deepcopy(work),turn))
        return before(guard,work,turn)
    monkeypatch.setattr(RuntimeGuard,'before',inspect)
    assert run(case)['status']=='complete'
    guard,work,turn=captured[0];calls=len(case['client'].calls)
    for proof in ({'cleanup_confirmed':True},None,object(),object.__new__(module._Entry)):
        with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
            guard.finish_uninvoked(work,turn,proof)
    assert len(case['client'].calls)==calls


def test_proof_is_one_use_and_cannot_escape_before_exception(review_case,monkeypatch):
    case=review_case;fault(case,'start_after');finish=RuntimeGuard.finish_uninvoked;captured=[]
    def inspect(guard,work,turn,proof):
        captured.append((guard,work,turn,proof))
        for fn in (copy,deepcopy,pickle.dumps):
            with pytest.raises(TypeError):fn(proof)
        assert finish(guard,work,turn,proof) is True
        calls=len(case['client'].calls)
        with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
            finish(guard,work,turn,proof)
        assert len(case['client'].calls)==calls
    monkeypatch.setattr(RuntimeGuard,'finish_uninvoked',inspect)
    result=run(case);local,remote=state(case,result)
    assert local['phase']=='finished' and remote[0]['state']=='finished'
    guard,work,turn,proof=captured[0]
    with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
        finish(guard,work,turn,proof)


def test_preexisting_runtime_record_never_receives_new_cleanup_proof(review_case,monkeypatch):
    case=review_case;before=RuntimeGuard.before
    def existing(guard,work,turn):
        guard.journal.begin(guard.configuration(work,turn))
        return before(guard,work,turn)
    monkeypatch.setattr(RuntimeGuard,'before',existing)
    result=run(case);local,remote=state(case,result)
    assert not remote and local['phase']=='reserving'
    assert not set(case['client'].calls)&{'lookup_execution','finish_execution'}


@pytest.mark.parametrize('boundary',['reserve_after','start_after'])
def test_real_review_pool_observer_retains_true_cleanup_without_an_opinion(boundary,monkeypatch):
    from test_review_lifecycle import ReviewLifecycleTests
    from test_runtime_integration import ControlClient
    fixture=ReviewLifecycleTests()
    try:
        case=fixture.fixture();assert fixture.prepare(case)['status']=='prepared'
        original=ControlClient.call;fired=[]
        selected='reserve_execution' if boundary.startswith('reserve') else 'start_execution'
        def call(client,name,args):
            result=original(client,name,args)
            if name==selected and client.session_id==case.review_job['session_id'] and not fired:
                fired.append(True);raise TimeoutError('synthetic reply lost after actual Board update')
            return result
        monkeypatch.setattr(ControlClient,'call',call)
        assert case.review_pool.dispatch_pending()==[case.review_job['id']]
        case.review_pool.threads[case.review_job['id']].join(timeout=20)
        assert not case.review_pool.threads[case.review_job['id']].is_alive()
        job=case.review_pool.status()['jobs'][0]
        assert (job['state'],job['outcome'],job['cleanup_confirmed'],job['execution_active'])==('settled','needs_review',True,False)
        assert not case.review_turns
        assert case.board.get_runtime(case.human)['active_count']==0
        assert not any(name=='update_ticket' for name,_ in case.lifecycle_calls)
    finally:fixture.doCleanups()


def test_real_start_ack_is_kept_when_a_later_before_guard_refuses(review_case,monkeypatch):
    case=review_case;before=RuntimeGuard.before
    def refuse(guard,work,turn):
        before(guard,work,turn)
        raise RuntimeControlError('synthetic subsequent admission check failed')
    monkeypatch.setattr(RuntimeGuard,'before',refuse)
    result=run(case);local,remote=state(case,result)
    assert local['phase']=='finished' and local['start_confirmed']==1
    assert remote[0]['state']=='finished' and remote[0]['outcome']=='blocked'


@pytest.mark.parametrize('changed',['work','client','thread'])
def test_issued_proof_cannot_change_its_bound_scope(review_case,monkeypatch,changed):
    import contextvars
    import threading
    case=review_case;fault(case,'start_before');finish=RuntimeGuard.finish_uninvoked
    def inspect(guard,work,turn,proof):
        calls=len(case['client'].calls)
        if changed=='work':
            with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
                finish(guard,{**work,'model':'another-model'},turn,proof)
        elif changed=='client':
            original=guard.client;guard.client=object()
            try:
                with pytest.raises(RuntimeControlError,match='runtime_uninvoked_proof_invalid'):
                    finish(guard,work,turn,proof)
            finally:guard.client=original
        else:
            failures=[]
            def thread():
                try:finish(guard,work,turn,proof)
                except RuntimeControlError as error:failures.append(str(error))
            # Even an explicitly copied ContextVar cannot transfer the proof.
            context=contextvars.copy_context();worker=threading.Thread(target=lambda:context.run(thread))
            worker.start();worker.join(timeout=2)
            assert not worker.is_alive() and failures==['runtime_uninvoked_proof_invalid']
        assert len(case['client'].calls)==calls
        return finish(guard,work,turn,proof)
    monkeypatch.setattr(RuntimeGuard,'finish_uninvoked',inspect)
    result=run(case);local,remote=state(case,result)
    assert local['phase']=='finished' and remote[0]['state']=='finished'
