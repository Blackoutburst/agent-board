"""Untyped prose never pauses the whole owner in the proven opt-in path."""
import uuid

import pytest

from blackoutburst_agents import provider_disposition as provider, codex_turn
from blackoutburst_agents.continuation import ContinuationChecks
from blackoutburst_agents.runtime_guard import RuntimeControlError
from test_continuation_run_store import case, provider_failed, checkpoint, used


def test_two_provider_pause_cycles_do_not_globally_pause_from_quota_prose(case):
    case['turn'].fake_cli("""
        import uuid
        session=str(uuid.uuid4())
        event({'type':'thread.started','thread_id':session})
        event({'type':'turn.started'})
        event({'type':'turn.failed','error':{'message':'quota usage limit PRIVATE'}})
        raise SystemExit(1)
    """)
    task=case['task'];store=case['store'];old_work=[];sessions=[]
    def check_provider(context):
        work=case['work']['id'];refs=case['refs']
        assert context.provider_receipt_sha256==refs['sha']
        closure=store.continuation_closure(work)
        if closure is None:assert store.continuation_state_sha256(work)==refs['pre']
        else:
            assert closure['pre_state_sha256']==refs['pre']
            assert closure['provider_receipt_sha256']==refs['sha']
            assert closure['post_state_sha256']==store.continuation_state_sha256(work)
    original=task.checks
    task.checks=ContinuationChecks(**dict(zip(('scope','claim','source','fence','runtime','resume'),original.callbacks)),
        provider_observation=check_provider)
    for number in range(2):
        response,proof,pre=provider_failed(case)
        assert response['result']['reason']=='execution_failed'
        assert case['rt'].board.get_runtime(case['rt'].human)['limits']['paused'] is False
        work=case['work']['id'];sessions.append(response['result']['session_id']);old_work.append(work)
        cp=checkpoint(case,proof=proof);revision=task.snapshot()['state']['revision']
        with store.work_lock(work):
            boundary=task.clean_boundary(expected_revision=revision,checkpoint=cp,cycle_used=used(1),reason='provider')
            store.seal_continuation_work_v1(work,expected_state_sha256=pre,intent_id=str(uuid.uuid4()),
                reason='provider',boundary=boundary,provider_receipt=proof)
            paused=task.checkpoint(expected_revision=revision,request_id='pause-'+str(number),checkpoint=cp,
                cycle_used=used(1),reason='provider',boundary=boundary)
            assert paused['snapshot']['state']['phase']=='paused_provider'
            assert store._turn(response['turn_id'])['requires_review']==1
        if number==0:
            started=task.start_cycle(expected_revision=paused['snapshot']['state']['revision'],request_id='next-cycle')
            with started.permit.use() as context:
                binding={key:getattr(context,key) for key in ('generation_id','cycle_id','attempt_id')}
            binding.update(task_id='logical-task',scope_sha256=provider._sha(case['guard'].scope))
            work=store.create_work(**{key:case['work'][key] for key in
                ('owner_id','agent_id','project_id','ticket_id','workspace','executable','model')})
            with store.work_lock(work['id']):store.enroll_continuation_work_v1(work['id'],**binding)
            case.update(work=work,binding=binding)
    assert len(set(old_work))==len(set(sessions))==2
    assert task.snapshot()['state']['used_total']['turns']==2
    runtime=case['rt'].board.get_runtime(case['rt'].human)
    assert runtime['limits']['paused'] is False
    assert len(runtime['executions'])==2
    assert all(row['state']=='finished' and row['reason']=='execution_failed' for row in runtime['executions'])
    assert all(store.get_work(work)['status']=='closed' for work in old_work)


def test_human_owner_pause_still_blocks_before_model_and_is_never_unpaused(case):
    marker=case['turn'].directory/'inference-started'
    case['turn'].fake_cli(f"open({str(marker)!r},'w').write('must not run')\nsuccess()")
    board=case['rt'].board;human=case['rt'].human
    limits=board.get_runtime(human)['limits']
    board.update_runtime_limits(human,limits['version'],limits['max_concurrent'],True)
    receipts=[]
    attempt=provider.enroll_provider_attempt(**case['binding'],work_id=case['work']['id'],
        request_id='human-paused',on_receipt=receipts.append)
    with pytest.raises(RuntimeControlError):
        case['runner'].run(case['work']['id'],'human-paused','PRIVATE prompt',timeout=3,provider_attempt=attempt)
    assert receipts==[] and not marker.exists()
    assert board.get_runtime(human)['limits']['paused'] is True
    assert board.get_runtime(human)['executions']==[]


def test_omitted_optin_keeps_the_legacy_quota_runtime_policy(case):
    case['mode'].write_text('failed')
    response=case['runner'].run(case['work']['id'],'legacy','PRIVATE prompt',timeout=3)
    assert response['result']['reason']=='quota_or_rate_limit'
    runtime=case['rt'].board.get_runtime(case['rt'].human)
    assert runtime['limits']['paused'] is True
    assert runtime['executions'][0]['reason']=='quota_or_rate_limit'


def test_unproven_optin_cleanup_keeps_review_and_cannot_neutralize_prose(case,monkeypatch):
    case['mode'].write_text('failed');receipts=[]
    monkeypatch.setattr(codex_turn,'_group_absent',lambda process:False)
    attempt=provider.enroll_provider_attempt(**case['binding'],work_id=case['work']['id'],
        request_id='unconfirmed-group',on_receipt=receipts.append)
    response=case['runner'].run(case['work']['id'],'unconfirmed-group','PRIVATE prompt',timeout=3,provider_attempt=attempt)
    assert receipts==[] and response['result']['reason']=='quota_or_rate_limit'
    assert case['store'].get_work(case['work']['id'])['status']=='needs_review'
