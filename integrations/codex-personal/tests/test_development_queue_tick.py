"""Queue cursor/CLI crash boundaries; real Core compositions live in the sibling test."""
from copy import deepcopy
import json
import signal
import uuid

import pytest

from blackoutburst_agents import cli, continuation_pool_service, development_queue_cli
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import canonical,digest,DevelopmentError
from blackoutburst_agents.development_queue import DevelopmentQueueAdmission
from test_development_queue import case,replace


@pytest.fixture
def tick_case(case,monkeypatch):
    root,path,catalog,client,controller=case
    catalog['timeout_seconds']=2000
    replace(path,catalog)
    workflow=root/'config/workflow.json'
    value=dict(schema='linux-cloud-workflow-v1',batch_id=str(uuid.uuid4()),jobs=[dict(name='code',paths=['main.py'],
        commit_message='Approved source',pr_title='Approved PR',pr_body='Approved review required',
        reviewer=dict(agent_id='reviewer',connection_file=str(root/'connections/reviewer.json'),
            instruction='Review the sealed evidence',model='review-model',timeout=120,skills=['review']))])
    replace(workflow,value) if workflow.exists() else workflow.write_bytes(canonical(value))
    workflow.chmod(0o600)
    calls=[];phases=['paused_cycle'];observed=[]
    class Service:
        def __init__(self,owner_root,*,configuration_path,**kwargs):
            assert owner_root==root
            self.config=decode(configuration_path.read_bytes());self.kwargs=kwargs
            self.batch=decode(__import__('pathlib').Path(self.config['batch_file']).read_bytes())
            calls.append((deepcopy(self.config),deepcopy(self.batch)))
        def run_once(self,*,should_stop):
            assert not should_stop()
            observed.append(True)
            phase=phases[0] if len(phases)==1 else phases.pop(0)
            row=dict(name='code',logical_task_id=self.config['tasks'][0]['logical_task_id'],phase=phase,
                operation_id='11111111-1111-4111-8111-111111111111',state_sha256='a'*64,
                reason='',next_observation_at=123 if phase=='paused_provider' else None,launchable=False)
            if phase=='independent_review_published':
                row.update(review_operation_id='22222222-2222-4222-8222-222222222222',
                    review_receipt_sha256='b'*64,accepted=False,historical=len(calls)>1)
            return dict(schema='continuation-pool-pass-v1',status='needs_you' if phase=='needs_you' else 'pass_complete',
                tasks=[row],activated=True,pass_complete_is_project_completion=False)
    monkeypatch.setattr(continuation_pool_service,'ContinuationPoolService',Service)
    monkeypatch.setattr('blackoutburst_agents.finite_pool_batch.FiniteLinuxPoolBatch.run',
        lambda *_a,**_k:pytest.fail('tick must never replay finite work'))
    plan=controller.tick_plan(path,str(workflow))
    args=dict(approved_sha256=plan['approval_sha256'],executable='/fixture/codex',cli_version='codex-cli 1.2.3',
        interpreter='/fixture/python',unshare='/fixture/unshare',gh_executable='/fixture/gh')
    def run(**changes):return controller.tick(path,str(workflow),**{**args,**changes})
    return root,path,catalog,client,controller,workflow,args,run,calls,phases,observed


def state(t):
    root,_,catalog,*_=t
    key=digest(canonical({k:catalog[k] for k in ('owner_id','agent_id')}))
    path=root/'admissions'/('queue-'+key)/'tick.json'
    return path,decode(path.read_bytes())


def test_standing_plan_ignores_tickets_and_performs_no_rpc_or_creation(tick_case):
    root,path,_,client,c,workflow,*_=tick_case
    original=c.tick_plan(path,str(workflow))
    client.ticket['version']+=100;client.project['version']+=1
    assert c.tick_plan(path,str(workflow))==original
    assert client.calls==[] and not (root/'admissions').exists()


def standing_wrapper(t):
    root,path,_,_,c,workflow,*_=t
    value=dict(schema='linux-cloud-queue-continuation-v1',catalog_file=str(path),workflow_file=str(workflow),
        approved_sha256=c.tick_plan(path,str(workflow))['approval_sha256'],checkpoint_after_actions=3,
        provider_backoff_seconds=60)
    wrapper=root/'config/standing-continuation.json';wrapper.write_bytes(canonical(value));wrapper.chmod(0o600)
    return wrapper,value


def test_pure_standing_facade_plan_matches_cli_hash_without_reading_connection_or_rpc(tick_case,monkeypatch):
    from blackoutburst_agents import development_queue as module
    from blackoutburst_agents.continuation_pool_configuration import continuation_plan
    root,_,catalog,client,*_=tick_case
    wrapper,value=standing_wrapper(tick_case);original=module.file_document
    def no_secret(path,*args,**kwargs):
        assert str(path)!=catalog['connection_file'],'plan must not read connection contents'
        return original(path,*args,**kwargs)
    monkeypatch.setattr(module,'file_document',no_secret)
    result=continuation_plan(str(wrapper),root=root,owner_id=catalog['owner_id'],
        board_endpoint=catalog['board_endpoint'],max_seconds=3000)
    assert result['approval_sha256']==value['approved_sha256'] and result['schema']==value['schema']
    assert result['status']=='plan' and result['execution_qualified'] is result['activated'] is result['launchable'] is False
    assert client.calls==[] and not (root/'admissions').exists()
    assert 'instruction' not in json.dumps(result) and 'connection_file' not in result


@pytest.mark.parametrize('change',[{'allow':True},{'checkpoint_after_actions':True},{'approved_sha256':'f'*64}])
def test_standing_wrapper_is_closed_before_client_or_execution(tick_case,change):
    from blackoutburst_agents.continuation_pool_configuration import continuation_plan
    root,_,catalog,client,*_=tick_case
    wrapper,value=standing_wrapper(tick_case);replace(wrapper,{**value,**change})
    with pytest.raises(DevelopmentError):
        continuation_plan(str(wrapper),root=root,owner_id=catalog['owner_id'],
            board_endpoint=catalog['board_endpoint'],max_seconds=3000)
    assert client.calls==[] and not (root/'admissions').exists()


def test_empty_and_external_stop_never_construct_service_or_admit(tick_case):
    root,_,_,client,_,_,_,run,calls,*_=tick_case
    assert run(should_stop=lambda:True)['phase']=='stopped'
    assert not (root/'admissions').exists() and client.calls==[]
    client.ready=False
    assert run()['phase']=='empty' and calls==[]
    assert state(tick_case)[1]['phase']=='idle'
    assert list((root/'config').glob('queue-admission-*'))==[]


def test_active_passes_never_rediscover_and_archive_before_next_admission(tick_case):
    root,path,catalog,client,_,_,_,run,calls,phases,_=tick_case
    phases[:]=['paused_cycle','paused_provider','independent_review_published']
    first=run();assert first['phase']=='paused_cycle'
    get_work=sum(name=='get_work' for name,_ in client.calls)
    client.ready=False
    second=run();assert second['phase']=='paused_provider'
    assert second['request_id']==first['request_id'] and len(calls)==2
    assert sum(name=='get_work' for name,_ in client.calls)==get_work
    final=run();assert final['phase']=='independent_review_published'
    pointer,value=state(tick_case)
    assert value['phase']=='idle' and value['active'] is None
    archive=pointer.parent/value['last_receipt']['file']
    assert digest(archive.read_bytes())==value['last_receipt']['sha256']
    assert decode(archive.read_bytes())['accepted'] is False
    assert run()['phase']=='empty' and len(calls)==3
    assert len(list((root/'config').glob('queue-admission-*')))==1
    # New standing scope/base is not interpreted while an admission is active.
    assert all(calls[0][1]==call[1] for call in calls)


def test_unassigned_ready_ticket_is_skipped_without_auto_assignment(tick_case):
    _,_,_,client,_,_,_,run,calls,*_=tick_case
    client.ticket['assignee_agent_id']=None
    assert run()['phase']=='empty' and calls==[]
    assert all(name in ('register','get_project','get_work','get_ticket') for name,_ in client.calls)


@pytest.mark.parametrize('confirmed',[False,True])
def test_admission_lost_ack_keeps_same_uuid_and_never_runs_pending_intent(tick_case,monkeypatch,confirmed):
    root,_,_,_,_,_,_,run,calls,*_=tick_case
    original=PrivateDirectory.create;once=[False]
    def interrupted(private,name,raw):
        if name=='complete.json' and private.path.parent==root/'admissions' and not once[0]:
            once[0]=True
            if confirmed:original(private,name,raw)
            raise DevelopmentError('fixture_ack_lost')
        return original(private,name,raw)
    monkeypatch.setattr(PrivateDirectory,'create',interrupted)
    with pytest.raises(DevelopmentError,match='fixture_ack_lost'):run()
    before=state(tick_case)[1]
    assert before['phase']=='admitting' and calls==[]
    if confirmed:
        assert run()['request_id']==before['active']['request_id'] and len(calls)==1
    else:
        with pytest.raises(DevelopmentError,match='development_queue_admission_pending'):run()
        assert calls==[] and state(tick_case)[1]==before


def test_archive_lost_pointer_ack_reuses_historical_core_without_another_admission(tick_case,monkeypatch):
    _,_,_,client,_,_,_,run,calls,phases,_=tick_case
    phases[:]=['independent_review_published']
    original=PrivateDirectory.replace;once=[False]
    def lost(private,name,raw):
        value=decode(raw)
        if name=='tick.json' and value['phase']=='idle' and value['last_receipt'] and not once[0]:
            once[0]=True;raise DevelopmentError('fixture_ack_lost')
        return original(private,name,raw)
    monkeypatch.setattr(PrivateDirectory,'replace',lost)
    with pytest.raises(DevelopmentError,match='fixture_ack_lost'):run()
    pointer,before=state(tick_case);reads=len(client.calls)
    assert len(list(pointer.parent.glob('finished-*')))==1
    result=run()
    assert result['request_id']==before['active']['request_id'] and len(calls)==2
    assert len(client.calls)==reads and state(tick_case)[1]['phase']=='idle'
    assert len(list(pointer.parent.glob('finished-*')))==1


def test_active_scope_change_or_bad_approval_cannot_select_new_task(tick_case):
    _,path,catalog,_,controller,workflow,args,run,calls,*_=tick_case
    with pytest.raises(DevelopmentError,match='development_queue_approval_changed'):
        run(approved_sha256='f'*64)
    assert calls==[]
    run();before=deepcopy(state(tick_case)[1])
    catalog['entries'][0]['repository']['base_commit']='b'*40;replace(path,catalog)
    new=controller.tick_plan(path,str(workflow))
    with pytest.raises(DevelopmentError,match='development_queue_approval_changed'):
        run(approved_sha256=new['approval_sha256'])
    assert state(tick_case)[1]==before and len(calls)==1


def test_standing_pin_race_after_full_read_cannot_become_lightweight_baseline(tick_case,monkeypatch):
    from blackoutburst_agents import development_queue as module
    _,path,catalog,_,_,_,_,run,calls,*_=tick_case
    original=module.file_document;reads=[0]
    def race(target,*args,**kwargs):
        observed=original(target,*args,**kwargs)
        if str(target)==str(path):
            reads[0]+=1
            if reads[0]==2:
                catalog['entries'][0]['instruction']='Different instruction outside standing approval'
                replace(path,catalog)
        return observed
    monkeypatch.setattr(module,'file_document',race)
    with pytest.raises(DevelopmentError,match='development_queue_approval_changed'):run()
    assert reads[0]==2 and calls==[]


def test_needs_you_keeps_active_and_does_not_advance_queue(tick_case):
    _,_,_,client,_,_,_,run,calls,phases,_=tick_case
    phases[:]=['needs_you'];first=run();reads=len(client.calls)
    second=run()
    assert second['request_id']==first['request_id'] and second['phase']=='needs_you'
    assert len(client.calls)==reads and state(tick_case)[1]['phase']=='active'


def test_cli_standing_preview_and_tick_leave_legacy_modes_and_signal_handlers_intact(tick_case,monkeypatch,capsys):
    root,path,_,client,_,workflow,_,_,_,phases,_=tick_case
    class Open:
        def __enter__(self):return client
        def __exit__(self,*_):pass
    monkeypatch.setattr(development_queue_cli.DevelopmentQueueDiscoveryClient,'from_connection_file',lambda *_a,**_k:Open())
    common=['--root',str(root),'--catalog',str(path),'--session',client.session_id]
    assert cli.main(['development-queue','preview',*common,'--workflow',str(workflow)])==0
    plan=json.loads(capsys.readouterr().out)
    assert set(plan)=={'schema','approval_sha256','binding','activated','launchable'} and client.calls==[]
    before={n:signal.getsignal(n) for n in (signal.SIGTERM,signal.SIGINT)}
    phases[:]=['needs_you']
    assert cli.main(['development-queue','tick',*common,'--workflow',str(workflow),
        '--approve-sha256',plan['approval_sha256'],'--executable','/fixture/codex','--cli-version','codex-cli 1.2.3',
        '--interpreter','/fixture/python','--unshare','/fixture/unshare','--gh-executable','/fixture/gh'])==3
    assert json.loads(capsys.readouterr().out)['phase']=='needs_you'
    assert {n:signal.getsignal(n) for n in before}==before
