"""New continuation metadata only; no legacy state, model, process or RPC."""
from contextlib import contextmanager
from copy import deepcopy
import copy
import os
import pickle
import sqlite3
import threading
import uuid

import pytest

from blackoutburst_agents.continuation import (ContinuationTask,ContinuationChecks,ContinuationError,
    ContinuationJournal,USAGE_FIELDS,validate_configuration,validate_checkpoint,validate_state)
from blackoutburst_agents.dev_manifest import DevelopmentError


@pytest.fixture
def case(tmp_path):
    scope=dict(owner_id='owner',agent_id='agent',project_id='project',ticket_id='ticket',device_id='device',
        profile_version=3,resource='repo:fixture',board_endpoint='https://board.test/mcp',source_release_sha256='a'*64)
    limits={'max_'+key:10 for key in USAGE_FIELDS};limits['max_wall_seconds']=60
    calls=[];fault={'callback':None,'result':None};clock=[100.]
    def callback(name):
        def check(context):
            calls.append((name,context))
            assert dict(context.scope)==scope
            if fault['callback']==name:
                if isinstance(fault['result'],Exception):raise fault['result']
                return fault['result']
        return check
    callbacks={key:callback(key) for key in ['scope','claim','source','fence','runtime','resume','provider_observation','quota_observation']}
    checks=ContinuationChecks(**callbacks)
    task=ContinuationTask.create(tmp_path/'continuation',logical_task_id='logical-one',scope=scope,cycle_limits=limits,
        initial_source_sha256='b'*64,checks=checks,clock=lambda:clock[0])
    data=dict(task=task,checks=checks,scope=scope,limits=limits,calls=calls,fault=fault,clock=clock,path=tmp_path/'continuation')
    yield data
    task.close()


def checkpoint():return dict(schema=1,source_sha256='b'*64,diff_sha256='c'*64,plan_sha256='d'*64,test_receipts=[],provider_receipt=None)
def usage(n=1):return dict.fromkeys(USAGE_FIELDS,n)
def start(case,key='start',revision=0):return case['task'].start_cycle(expected_revision=revision,request_id=key)
def seal(case,reason='cycle',key='seal',used=None):
    task=case['task'];revision=task.snapshot()['state']['revision'];cp=checkpoint();used=usage() if used is None else used
    boundary=task.clean_boundary(expected_revision=revision,checkpoint=cp,cycle_used=used,reason=reason)
    return task.checkpoint(expected_revision=revision,request_id=key,checkpoint=cp,cycle_used=used,reason=reason,boundary=boundary)


def test_cycle_boundary_and_new_generation_preserve_old_usage(case):
    first=start(case);assert first.permit is not None and first.receipt['launchable'] is False
    with first.permit.use() as context:assert context.purpose=='execute'
    with pytest.raises(ContinuationError):
        with first.permit.use():pass
    old=deepcopy(case['task'].journal.events())
    paused=seal(case);assert paused['snapshot']['state']['phase']=='paused_cycle'
    second=start(case,'next',2);assert second.permit is not None
    value=second.receipt['snapshot']['state'];before=first.receipt['snapshot']['state']
    assert value['cycle_seq']==value['attempt_seq']==2 and value['used_total']==usage()
    assert value['current_cycle']['used']==usage(0)
    for key in ['generation_id','cycle_id','attempt_id']:assert value['current_cycle'][key]!=before['current_cycle'][key]
    assert case['task'].journal.events(limit=2)==old
    assert case['task'].configuration['cycle_limits']==case['limits']


def test_duplicate_checkpoint_does_not_charge_twice_or_make_a_permit(case):
    initial=start(case);replay=start(case)
    assert replay.permit is None and replay.receipt['historical'] is True
    assert replay.receipt['receipt']==initial.receipt['receipt']
    paused=seal(case);again=case['task'].checkpoint(expected_revision=1,request_id='seal',checkpoint=checkpoint(),
        cycle_used=usage(),reason='cycle',boundary=None)
    assert again['historical'] is True and again['snapshot']==paused['snapshot']
    assert again['snapshot']['state']['used_total']==usage()
    with pytest.raises(ContinuationError,match='request_conflict'):
        case['task'].checkpoint(expected_revision=1,request_id='seal',checkpoint=checkpoint(),cycle_used=usage(2),reason='cycle')


def test_clean_provider_and_typed_quota_are_distinct(case):
    start(case);value=seal(case,'provider');assert value['snapshot']['state']['phase']=='paused_provider'
    start(case,'second',2);value=seal(case,'quota','quota');assert value['snapshot']['state']['phase']=='paused_quota'
    names=[name for name,_ in case['calls']]
    assert 'provider_observation' in names and 'quota_observation' in names


@pytest.mark.parametrize('reason,missing',[('provider','provider_observation'),('quota','quota_observation')])
def test_unconfirmed_provider_or_quota_cannot_make_a_clean_pause(case,reason,missing):
    start(case);setattr(case['checks'],missing,None)
    with pytest.raises(ContinuationError):
        case['task'].clean_boundary(expected_revision=1,checkpoint=checkpoint(),cycle_used=usage(),reason=reason)
    result=case['task'].checkpoint(expected_revision=1,request_id='no-proof',checkpoint=checkpoint(),cycle_used=usage(),reason=reason)
    assert result['snapshot']['state']['phase']=='needs_you'
    assert result['snapshot']['state']['checkpoint'] is None
    with pytest.raises(ContinuationError):start(case,'no-resume',2)


@pytest.mark.parametrize('callback,code',[('scope','continuation_scope_changed'),('source','continuation_source_changed'),
    ('claim','continuation_claim_changed'),('fence','continuation_fence_unknown'),('runtime','continuation_runtime_unknown')])
def test_resume_rechecks_scope_source_claim_fence_and_exact_own_runtime(case,callback,code):
    start(case);seal(case)
    case['fault'].update(callback=callback,result=ContinuationError(code))
    value=start(case,'drift',2)
    assert value.permit is None and value.receipt['snapshot']['state']['phase']=='needs_you'
    assert value.receipt['snapshot']['state']['reason']==code
    assert value.receipt['snapshot']['state']['cycle_seq']==1


@pytest.mark.parametrize('value',[True,False,{'allow':True}])
def test_callback_boolean_or_serialized_permission_never_authorizes(case,value):
    case['fault'].update(callback='scope',result=value)
    result=start(case)
    assert result.permit is None and result.receipt['snapshot']['state']['phase']=='needs_you'


def test_profile_drift_and_stop_after_permit_issuance_block_driver(case):
    initial=start(case);case['task'].stop(expected_revision=1,request_id='stop')
    called=[]
    with pytest.raises(ContinuationError):
        with initial.permit.use():called.append(True)
    assert not called
    old=start(case);assert old.permit is None and old.receipt['snapshot']['state']['phase']=='stopped'
    with pytest.raises(ContinuationError):start(case,'new',2)


def test_stop_committed_during_readonly_fresh_check_wins_start_cas(case):
    once=[]
    def stop(_context):
        if not once:once.append(True);case['task'].stop(expected_revision=0,request_id='stop-first')
    case['checks'].callbacks=(*case['checks'].callbacks[:5],stop)
    with pytest.raises(ContinuationError,match='revision_conflict'):start(case)
    assert case['task'].snapshot()['state']['phase']=='stopped'


def test_checkpoint_downgrade_preserves_accounting_and_never_stores_unverified_proof(case):
    start(case)
    result=case['task'].checkpoint(expected_revision=1,request_id='unsafe',checkpoint=checkpoint(),cycle_used=usage(),
        reason='cycle',boundary={'allow':True,'cleanup_confirmed':True})
    state=result['snapshot']['state'];assert state['phase']=='needs_you' and state['checkpoint'] is None
    assert state['used_total']==usage() and state['current_cycle']['used']==usage()


def test_stale_boundary_and_decreasing_usage_refuse(case):
    start(case);task=case['task']
    boundary=task.clean_boundary(expected_revision=1,checkpoint=checkpoint(),cycle_used=usage(),reason='checkpoint')
    first=task.checkpoint(expected_revision=1,request_id='cp',checkpoint=checkpoint(),cycle_used=usage(),reason='checkpoint',boundary=boundary)
    with pytest.raises(ContinuationError):task.checkpoint(expected_revision=2,request_id='regressed',checkpoint=checkpoint(),cycle_used=usage(0),reason='cycle')
    for function in (copy.copy,copy.deepcopy,pickle.dumps):
        with pytest.raises(TypeError):function(boundary)
    assert first['snapshot']['state']['phase']=='running'


@pytest.mark.parametrize('when',['before_commit','lost_ack'])
def test_crash_transaction_and_lost_ack_do_not_replay_a_cycle(case,monkeypatch,when):
    journal=case['task'].journal;original=journal._transaction
    @contextmanager
    def fault():
        with original():
            yield
            if when=='before_commit':raise RuntimeError('synthetic crash')
        if when=='lost_ack':raise RuntimeError('synthetic lost ACK')
    with monkeypatch.context() as patch:
        patch.setattr(journal,'_transaction',fault)
        with pytest.raises(RuntimeError):start(case)
    current=case['task'].snapshot()['state']
    if when=='before_commit':assert current['revision']==0 and start(case).permit is not None
    else:assert current['revision']==1 and start(case).permit is None


def test_reopen_is_metadata_only_and_running_cycle_is_not_adopted(case):
    first=start(case);path=case['path'];case['task'].close()
    journal=ContinuationJournal.open(path)
    task=ContinuationTask(journal,case['checks'],clock=lambda:100.)
    case['task']=task
    replay=task.start_cycle(expected_revision=0,request_id='start');assert replay.permit is None and replay.receipt['historical']
    with pytest.raises(ContinuationError):task.start_cycle(expected_revision=1,request_id='fresh-while-running')
    assert task.snapshot()==first.receipt['snapshot']


def test_append_only_schema_and_pages_have_no_configured_global_cycle_limit(case):
    task=case['task'];old=[]
    for number in range(20):
        revision=task.snapshot()['state']['revision']
        start(case,'start-'+str(number),revision);seal(case,key='pause-'+str(number))
    state=task.snapshot()['state'];assert state['cycle_seq']==20 and state['used_total']==usage(20)
    assert len(task.journal.events(after_revision=0,limit=3))==3
    with pytest.raises(sqlite3.DatabaseError):task.journal.db.execute("UPDATE events SET state_json='{}'")
    with pytest.raises(sqlite3.DatabaseError):task.journal.db.execute('DELETE FROM events')
    with pytest.raises(sqlite3.DatabaseError):task.journal.db.execute('INSERT OR REPLACE INTO events SELECT * FROM events WHERE revision=1')
    with pytest.raises(sqlite3.DatabaseError):task.journal.db.execute('INSERT OR REPLACE INTO task SELECT * FROM task')
    with pytest.raises(ContinuationError):ContinuationJournal.create(case['path'],task.configuration,state)


@pytest.mark.parametrize('callback,code',[('scope','continuation_profile_changed'),
    ('claim','continuation_claim_changed'),('source','continuation_source_changed'),
    ('fence','continuation_fence_unknown'),('runtime','continuation_runtime_unknown')])
def test_fresh_drift_after_permit_issue_never_enters_driver(case,callback,code):
    value=start(case);case['fault'].update(callback=callback,result=ContinuationError(code));entered=[]
    with pytest.raises(ContinuationError,match=code):
        with value.permit.use():entered.append(True)
    assert entered==[] and case['task'].snapshot()['state']['revision']==1
    case['fault'].update(callback=None,result=None)
    with pytest.raises(ContinuationError):
        with value.permit.use():entered.append(True)
    assert entered==[]


@pytest.mark.parametrize('callback,code',[('scope','continuation_profile_changed'),
    ('source','continuation_source_changed'),('fence','continuation_fence_unknown'),
    ('runtime','continuation_runtime_unknown')])
def test_drift_after_clean_capture_downgrades_without_storing_checkpoint(case,callback,code):
    start(case);task=case['task'];cp=checkpoint()
    boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(),reason='cycle')
    case['fault'].update(callback=callback,result=ContinuationError(code))
    result=task.checkpoint(expected_revision=1,request_id='drift',checkpoint=cp,cycle_used=usage(),reason='cycle',boundary=boundary)
    state=result['snapshot']['state']
    assert state['phase']=='needs_you' and state['reason']==code and state['checkpoint'] is None
    assert state['used_total']==usage() and state['current_cycle']['sealed'] is False
    with pytest.raises(ContinuationError):start(case,'no-auto-retry',2)


def test_callback_context_binds_new_source_usage_and_only_metadata(case):
    start(case);task=case['task'];cp=checkpoint();cp['source_sha256']='e'*64
    cp['provider_receipt']={'work_id':str(uuid.uuid4()),'turn_id':str(uuid.uuid4()),'receipt_sha256':'f'*64}
    boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(2),reason='provider')
    contexts=[context for _,context in case['calls'] if context.purpose=='provider']
    assert contexts and all(context.source_sha256=='e'*64 and dict(context.cycle_used)==usage(2)
        and context.provider_receipt_sha256=='f'*64 for context in contexts)
    with pytest.raises(TypeError):contexts[0].scope['owner_id']='foreign'
    value=task.checkpoint(expected_revision=1,request_id='provider',checkpoint=cp,cycle_used=usage(2),reason='provider',boundary=boundary)
    assert value['snapshot']['state']['checkpoint']==cp
    resumed=start(case,'next',2)
    with resumed.permit.use() as context:
        assert context.source_sha256=='e'*64 and context.checkpoint_sha256==value['snapshot']['state']['checkpoint_sha256']


def test_boundary_cannot_cross_generation_or_checkpoint_payload(case):
    start(case);task=case['task'];cp=checkpoint()
    boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(),reason='cycle')
    changed=deepcopy(cp);changed['diff_sha256']='e'*64
    result=task.checkpoint(expected_revision=1,request_id='different',checkpoint=changed,cycle_used=usage(),reason='cycle',boundary=boundary)
    assert result['snapshot']['state']['phase']=='needs_you' and result['snapshot']['state']['checkpoint'] is None


def test_checkpoint_cas_never_recounts_an_older_boundary(case):
    start(case);task=case['task'];cp=checkpoint()
    first=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(),reason='checkpoint')
    stale=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(2),reason='cycle')
    task.checkpoint(expected_revision=1,request_id='one',checkpoint=cp,cycle_used=usage(),reason='checkpoint',boundary=first)
    with pytest.raises(ContinuationError,match='revision_conflict'):
        task.checkpoint(expected_revision=1,request_id='two',checkpoint=cp,cycle_used=usage(2),reason='cycle',boundary=stale)
    assert task.snapshot()['state']['used_total']==usage()


@pytest.mark.parametrize('reason',['cycle','provider','quota','complete'])
def test_pause_or_finish_cannot_promote_unknown_runtime(case,reason):
    start(case);case['fault'].update(callback='runtime',result={'cleanup_confirmed':True})
    with pytest.raises(ContinuationError):
        case['task'].clean_boundary(expected_revision=1,checkpoint=checkpoint(),cycle_used=usage(),reason=reason)
    result=case['task'].checkpoint(expected_revision=1,request_id='unknown',checkpoint=checkpoint(),cycle_used=usage(),reason=reason)
    assert result['snapshot']['state']['phase']=='needs_you' and result['snapshot']['state']['checkpoint'] is None


def test_clean_cycle_limit_pauses_and_plain_checkpoint_cannot_extend_a_cycle(case):
    start(case);case['clock'][0]=160.
    result=seal(case);assert result['snapshot']['state']['phase']=='paused_cycle'
    start(case,'second',2);case['clock'][0]=220.
    result=seal(case,'checkpoint','late')
    assert result['snapshot']['state']['phase']=='needs_you' and result['snapshot']['state']['reason']=='continuation_cycle_limit'
    assert result['snapshot']['state']['checkpoint']==checkpoint()
    assert result['snapshot']['state']['used_total']==usage(2)


def test_wrong_thread_cannot_consume_a_permit(case):
    permit=start(case).permit;errors=[];entered=[]
    def worker():
        try:
            with permit.use():entered.append(True)
        except Exception as error:errors.append(error)
    thread=threading.Thread(target=worker);thread.start();thread.join()
    assert not entered and len(errors)==1
    with permit.use():entered.append(True)
    assert entered==[True]


def test_reopen_preserves_paused_checkpoint_without_replaying_old_attempt(case):
    first=start(case);paused=seal(case);case['task'].close()
    case['task']=ContinuationTask(ContinuationJournal.open(case['path']),case['checks'],clock=lambda:100.)
    replay=start(case);assert replay.permit is None and replay.receipt['snapshot']==paused['snapshot']
    second=start(case,'new',2)
    assert second.permit is not None and second.receipt['snapshot']['state']['used_total']==usage()
    assert second.receipt['snapshot']['state']['current_cycle']['attempt_id']!=first.receipt['snapshot']['state']['current_cycle']['attempt_id']


@pytest.mark.parametrize('mutation',['extra_file','wal','missing_trigger','state_noncanonical','state_hash','database_inode','lock_inode'])
def test_private_journal_or_schema_mutation_refuses_before_any_driver(case,mutation):
    task=case['task'];start(case)
    if mutation in {'extra_file','wal'}:
        path=case['path']/('unexpected.json' if mutation=='extra_file' else 'continuation.sqlite3-wal')
        path.write_bytes(b'{}');path.chmod(0o600)
    elif mutation in {'database_inode','lock_inode'}:
        path=case['path']/('continuation.sqlite3' if mutation=='database_inode' else 'continuation.lock')
        data=path.read_bytes();path.unlink();path.write_bytes(data);path.chmod(0o600)
    else:
        task.journal.db.execute('DROP TRIGGER events_no_update')
        if mutation!='missing_trigger':
            from blackoutburst_agents.continuation_journal import SQL
            if mutation=='state_noncanonical':task.journal.db.execute("UPDATE events SET state_json=state_json||' ' WHERE revision=1")
            else:task.journal.db.execute("UPDATE events SET event_sha256=? WHERE revision=1",('f'*64,))
            task.journal.db.execute(SQL['events_no_update'])
    with pytest.raises((DevelopmentError,OSError)):task.snapshot()


@pytest.mark.parametrize('field,value',[('model_output','private response'),('allow',True),('cleanup_confirmed',True)])
def test_checkpoint_cannot_store_model_content_or_serialized_execution_permissions(field,value):
    cp=checkpoint();cp[field]=value
    with pytest.raises(ContinuationError):validate_checkpoint(cp)


@pytest.mark.parametrize('url',['https://[invalid','http://board.test','https://owner:secret@board.test','https://board.test?token=secret'])
def test_configuration_rejects_invalid_or_credential_bearing_endpoint(case,url):
    configuration=deepcopy(case['task'].configuration);configuration['scope']['board_endpoint']=url
    with pytest.raises(ContinuationError):validate_configuration(configuration)


def test_partial_checkpoints_charge_only_new_usage_and_pause_keeps_entire_history(case):
    start(case);task=case['task']
    first=seal(case,'checkpoint','first',usage(2));second=seal(case,'cycle','second',usage(5))
    assert first['snapshot']['state']['used_total']==usage(2)
    assert second['snapshot']['state']['used_total']==usage(5)
    old_events=task.journal.events();start(case,'next',3);seal(case,key='third',used=usage(4))
    assert task.snapshot()['state']['used_total']==usage(9)
    assert task.journal.events(limit=len(old_events))==old_events


@pytest.mark.parametrize('mutation',['bool_cycle_seq','bool_attempt_seq','running_without_cycle','ready_with_cycle','stopped_without_stop'])
def test_persisted_state_cannot_confuse_sequence_or_stop_types(case,mutation):
    state=deepcopy(start(case).receipt['snapshot']['state'])
    if mutation.startswith('bool_'):state['current_cycle'][mutation[5:]]=True
    elif mutation=='running_without_cycle':state['current_cycle']=None
    elif mutation=='ready_with_cycle':state['phase']='ready'
    else:state['phase']='stopped'
    with pytest.raises(ContinuationError):validate_state(state,case['task'].configuration)


def test_controller_drives_clean_cycles_and_unknown_provider_pause_with_real_bindings(case):
    """Controller-shaped fixture; no provider, execution journal or tool effect."""
    task=case['task'];runtime={};source=['b'*64];allowed={'profile':3,'claim':True,'fence':True};provider={}
    def scope(context):
        if context.scope['profile_version']!=allowed['profile']:raise ContinuationError('continuation_profile_changed')
    def claim(context):
        if not allowed['claim']:raise ContinuationError('continuation_claim_changed')
    def source_check(context):
        if context.source_sha256!=source[0]:raise ContinuationError('continuation_source_changed')
    def fence(context):
        if not allowed['fence']:raise ContinuationError('continuation_fence_unknown')
    def own_runtime(context):
        if context.attempt_id is not None and runtime.get(context.attempt_id) not in {'never_started','terminal_clean'}:
            raise ContinuationError('continuation_runtime_unknown')
    def provider_check(context):
        if provider.get(context.attempt_id)!=context.provider_receipt_sha256:
            raise ContinuationError('continuation_provider_unconfirmed')
    task.checks=ContinuationChecks(scope=scope,claim=claim,source=source_check,fence=fence,runtime=own_runtime,
        resume=lambda context:None,provider_observation=provider_check)
    checkpoints=[]
    for number,reason in enumerate(['cycle','provider','complete']):
        revision=task.snapshot()['state']['revision'];value=task.start_cycle(expected_revision=revision,request_id=f'cycle-{number}')
        cycle=value.receipt['snapshot']['state']['current_cycle'];attempt=cycle['attempt_id']
        runtime[attempt]='never_started'
        with value.permit.use() as context:
            assert context.generation_id==cycle['generation_id'] and context.attempt_id==attempt
            runtime[attempt]='running'
            source[0]=str(number+1)*64
            cp=checkpoint();cp['source_sha256']=source[0]
            cp['test_receipts']=[dict(test_name='fixed-test',receipt_sha256='e'*64,source_sha256=source[0],outcome='passed')]
            runtime[attempt]='terminal_clean'
        if reason=='provider':
            cp['provider_receipt']={'work_id':str(uuid.uuid4()),'turn_id':str(uuid.uuid4()),'receipt_sha256':'f'*64}
            provider[attempt]='f'*64
        boundary=task.clean_boundary(expected_revision=revision+1,checkpoint=cp,cycle_used=usage(),reason=reason)
        paused=task.checkpoint(expected_revision=revision+1,request_id=f'checkpoint-{number}',checkpoint=cp,
            cycle_used=usage(),reason=reason,boundary=boundary)
        checkpoints.append(paused['snapshot']['state']['checkpoint_sha256'])
        replay=task.start_cycle(expected_revision=revision,request_id=f'cycle-{number}')
        assert replay.permit is None and replay.receipt['historical']
    state=task.snapshot()['state']
    assert state['phase']=='complete' and state['used_total']==usage(3) and len(set(checkpoints))==3
    assert {event['state']['phase'] for event in task.journal.events()} >= {'paused_cycle','paused_provider','complete'}
    assert all(state=='terminal_clean' for state in runtime.values())


def test_stop_after_boundary_capture_remains_dominant_without_claiming_cleanup(case):
    start(case);task=case['task'];cp=checkpoint()
    boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(),reason='cycle')
    task.stop(expected_revision=1,request_id='stop-after-capture')
    with pytest.raises(ContinuationError,match='revision_conflict'):
        task.checkpoint(expected_revision=1,request_id='late-boundary',checkpoint=cp,cycle_used=usage(),reason='cycle',boundary=boundary)
    state=task.snapshot()['state']
    assert state['phase']=='stopped' and state['checkpoint'] is None and state['current_cycle']['sealed'] is False


def test_stop_from_fresh_boundary_recheck_wins_atomic_checkpoint(case):
    start(case);task=case['task'];cp=checkpoint()
    boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(),reason='cycle')
    def stop(context):task.stop(expected_revision=1,request_id='stop-during-check')
    case['checks'].callbacks=(*case['checks'].callbacks[:4],stop,*case['checks'].callbacks[5:])
    with pytest.raises(ContinuationError,match='revision_conflict'):
        task.checkpoint(expected_revision=1,request_id='late-boundary',checkpoint=cp,cycle_used=usage(),reason='cycle',boundary=boundary)
    assert task.snapshot()['state']['phase']=='stopped'


@pytest.mark.parametrize('when',['before_commit','lost_ack'])
def test_checkpoint_crash_or_lost_ack_reopens_without_promoting_an_unknown_boundary(case,monkeypatch,when):
    start(case);task=case['task'];cp=checkpoint();journal=task.journal;original=journal._transaction
    boundary=task.clean_boundary(expected_revision=1,checkpoint=cp,cycle_used=usage(),reason='cycle')
    @contextmanager
    def fault():
        with original():
            yield
            if when=='before_commit':raise RuntimeError('synthetic crash')
        if when=='lost_ack':raise RuntimeError('synthetic lost ACK')
    with monkeypatch.context() as patch:
        patch.setattr(journal,'_transaction',fault)
        with pytest.raises(RuntimeError):task.checkpoint(expected_revision=1,request_id='cp',checkpoint=cp,
            cycle_used=usage(),reason='cycle',boundary=boundary)
    task.close();case['task']=ContinuationTask(ContinuationJournal.open(case['path']),case['checks'],clock=lambda:100.)
    task=case['task']
    if when=='before_commit':
        assert task.snapshot()['state']['revision']==1
        # A process interruption loses the live proof. Replaying metadata cannot
        # promote it: cleanup must be checked again to make a new proof.
        value=task.checkpoint(expected_revision=1,request_id='cp',checkpoint=cp,cycle_used=usage(),reason='cycle')
        assert value['snapshot']['state']['phase']=='needs_you' and value['snapshot']['state']['checkpoint'] is None
    else:
        value=task.checkpoint(expected_revision=1,request_id='cp',checkpoint=cp,cycle_used=usage(),reason='cycle')
        assert value['historical'] is True and value['snapshot']['state']['phase']=='paused_cycle'
        assert value['snapshot']['state']['used_total']==usage()
