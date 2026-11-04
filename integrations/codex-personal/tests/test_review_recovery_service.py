"""Real queue admission, Pool scheduling and filesystem history; synthetic review seam."""
from copy import deepcopy
import fcntl
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

from blackoutburst_agents import review_recovery_service as module
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.development_queue import queue_tick_plan
from blackoutburst_agents.worker_pool import WorkerPool,PoolDriverBinding,PoolOutcome
from test_development_queue import case


def write(path,value):
    for parent in reversed((path.parent,*path.parent.parents)):
        if not parent.exists():parent.mkdir(mode=0o700)
    path.write_bytes(canonical(value));path.chmod(0o600)


@pytest.fixture
def recovery(case,monkeypatch):
    root,catalog_path,catalog,client,queue=case
    catalog['entries'][0]['planner_budgets']['max_wall_seconds']=30
    write(catalog_path,catalog)
    connection=root/'connections/reviewer/codex.json';write(connection,{'synthetic':'no credentials'})
    workflow=root/'config/next-workflow.json'
    write(workflow,dict(schema='linux-cloud-workflow-v1',batch_id=str(uuid.uuid4()),jobs=[dict(name='code',
        paths=['main.py'],commit_message='Selected change',pr_title='Selected change',pr_body='Selected change',
        reviewer=dict(agent_id='reviewer',connection_file=str(connection),instruction='Review',model='model',
            skills=['review'],timeout=30))]))
    plan=queue_tick_plan(root,str(catalog_path),str(workflow),checkpoint_after_actions=2)
    following=root/'config/next-standing.json'
    write(following,dict(schema='linux-cloud-queue-continuation-v1',catalog_file=str(catalog_path),
        workflow_file=str(workflow),approved_sha256=plan['approval_sha256'],checkpoint_after_actions=2,provider_backoff_seconds=60))
    value=dict(schema=module.SCHEMA,recovery_id=str(uuid.uuid4()),logical_task_id=str(uuid.uuid4()),
        source_operation_id=str(uuid.uuid4()),prior_review_job_id=str(uuid.uuid4()),expected_source_sha256='a'*64,
        delivery_sha='d'*40,ticket_version=12,revocation_event_id=3506,owner_id='owner',project_id='project',
        ticket_id='ticket',reviewer_agent_id='reviewer',connection_file=str(connection),model='model',timeout=30,
        skills=['review'],instruction='PRIVATE_REVIEW_INSTRUCTION',next_configuration_file=str(following),
        next_configuration_sha256=digest(following.read_bytes()))
    target=root/'continuations'/digest(canonical(dict(owner_id='owner',logical_task_id=value['logical_task_id'])))
    source_path=target/'aggregate/delivery/review-source/source.json'
    write(source_path,dict(receipt=dict(operation_id=value['source_operation_id'],delivery_sha=value['delivery_sha'],
        author_scope=dict(owner_id='owner',agent_id='coder',project_id='project',ticket_id='ticket'))))
    value['expected_source_sha256']=digest(source_path.read_bytes())
    request=str(uuid.uuid4());preview=queue.preview(catalog_path)
    admission=queue.admit(catalog_path,request_id=request,approved_sha256=preview['approval_sha256'])
    core=root/'config'/('queue-continuation-'+request+'.json')
    write(core,dict(tasks=[dict(logical_task_id=value['logical_task_id'])]))
    queue_path=root/'admissions'/('queue-'+digest(canonical(dict(owner_id='owner',agent_id='coder'))))
    write(queue_path/'tick.json',dict(schema='development-queue-tick-state-v1',phase='active',
        active=dict(request_id=request,logical_task_id=value['logical_task_id'],preview_sha256=preview['approval_sha256'],
            configuration_pin=file_document(core)[1],admission_sha256=digest(canonical({k:v for k,v in admission.items() if k!='historical'}))),
        approval_sha256=plan['approval_sha256'],session_id='old-session',last_receipt=None))
    (queue_path/'tick.lock').write_bytes(b'');(queue_path/'tick.lock').chmod(0o600)
    path=root/'config/continuation.json';write(path,value)
    calls=[];next_calls=[];proof=dict(fixed='fixture-proof')
    real_next=module.ReviewRecoveryService._next
    def service():
        return module.ReviewRecoveryService(root,configuration_path=path,owner_id='owner',board_endpoint=client.endpoint,
            executable=str(Path(sys.executable).resolve()),cli_version='codex-cli 1.2.3',interpreter=str(Path(sys.executable).resolve()),
            unshare=str(Path(sys.executable).resolve()),gh_executable=str(Path(sys.executable).resolve()),max_seconds=300)
    def install_seam(cleaned=True,crash=False):
        class Driver:
            def _verify_cleanup(self,context,outcome):calls.append('fresh-cleanup');return cleaned
        class Lifecycle:
            def publish(self,operation):
                calls.append('publish')
                return dict(operation_id=operation,status='complete',ticket_id='ticket',ticket_version=14,
                    delivery_sha=value['delivery_sha'],pull_request=dict(url='https://github.com/Blackoutburst/agent-board/pull/94'))
            def stop(self,*_):calls.append('stop')
        def start(s,private):
            calls.append('start')
            class Run:
                def __init__(self,context):self.context=context
                def run(self):calls.append('model');return PoolOutcome(self.context.operation_id,self.context.session_id,'complete',cleaned)
                def close(self):pass
            binding=PoolDriverBinding('fixture-v1',Run,lambda ctx:None,lambda ctx,outcome:cleaned)
            s.pool=WorkerPool(s.directory/'pool',concurrency=1,ceiling=1,drivers={'review':binding})
            s.job=s.pool.submit(request_id='review-'+value['recovery_id'],driver_id='review',payload={})
            s.state['job']=deepcopy(s.job);private.replace('registry.json',canonical(s.state))
            s.driver=Driver();s.lifecycle=Lifecycle()
            if crash:raise DevelopmentError('fixture_after_intent')
        monkeypatch.setattr(module.ReviewRecoveryService,'_start_review',start)
        monkeypatch.setattr(module.ReviewRecoveryService,'_proof',lambda s:deepcopy(proof))
        def following(s,receipt,stop):
            next_calls.append(receipt)
            return dict(status='pass_complete',queue=dict(phase='empty'),tasks=[],review_recovery=receipt)
        monkeypatch.setattr(module.ReviewRecoveryService,'_next',following)
        def rename(parent,old,new):
            try:os.stat(new,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:pass
            else:raise AssertionError('archive must not replace')
            os.rename(old,new,src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
        monkeypatch.setattr(module,'_rename_noreplace',rename)
    return SimpleNamespace(root=root,path=path,value=value,service=service,target=target,queue_path=queue_path,
        calls=calls,next_calls=next_calls,install_seam=install_seam,proof=proof,client=client,real_next=real_next)


def test_plan_reads_only_operator_documents_without_source_credentials_state_or_rpc(recovery,monkeypatch):
    f=recovery
    before={str(p):p.read_bytes() for p in f.root.rglob('*') if p.is_file()}
    original=module.file_document
    def observed(path,*args,**kwargs):
        assert str(path).startswith(str(f.root/'config')) or str(path).startswith(str(f.root/'verifiers'))
        return original(path,*args,**kwargs)
    monkeypatch.setattr(module,'file_document',observed)
    import socket,subprocess
    monkeypatch.setattr(socket,'socket',lambda *_a,**_k:pytest.fail('plan network'))
    monkeypatch.setattr(subprocess,'Popen',lambda *_a,**_k:pytest.fail('plan process'))
    result=module.review_recovery_plan(f.path,root=f.root,owner_id='owner',board_endpoint='http://localhost/mcp',max_seconds=300)
    assert result['status']=='plan' and result['activated'] is result['launchable'] is result['execution_qualified'] is False
    assert 'PRIVATE_' not in json.dumps(result)
    assert before=={str(p):p.read_bytes() for p in f.root.rglob('*') if p.is_file()}


@pytest.mark.parametrize('field,bad',[('connection_file','/tmp/other.json'),('next_configuration_file','/tmp/other.json'),
    ('next_configuration_sha256','f'*64),('ticket_version',True),('revocation_event_id',0),('timeout',301),('skills',[])])
def test_changed_operator_selection_refuses_before_state_or_effects(recovery,field,bad):
    f=recovery;value={**f.value,field:bad};write(f.path,value)
    with pytest.raises((DevelopmentError,ValueError)):f.service()
    assert not (f.root/'review-recoveries').exists()


def test_actual_pool_one_dispatch_then_ack_archive_and_dated_cache_without_new_model(recovery):
    f=recovery;f.install_seam();before={str(p):p.read_bytes() for p in f.target.rglob('*') if p.is_file()}
    result=f.service().run_once()
    assert result['queue']['phase']=='empty' and result['review_recovery']['historical'] is False
    assert f.calls.count('model')==1 and f.calls.count('fresh-cleanup')==2 and f.calls.count('publish')==1
    assert not f.queue_path.exists()
    archive=f.root/'admissions'/('completed-by-review-'+f.value['recovery_id'])
    assert archive.is_dir() and (archive/'tick.json').exists()
    assert before=={str(p):p.read_bytes() for p in f.target.rglob('*') if p.is_file()}
    second=f.service().run_once()
    assert second['review_recovery']['historical'] is True and f.calls.count('model')==1
    assert len(f.next_calls)==2


def test_crash_after_durable_job_never_replays_dispatch_or_admission(recovery):
    f=recovery;f.install_seam(crash=True)
    with pytest.raises(DevelopmentError,match='fixture_after_intent'):f.service().run_once()
    calls=list(f.calls);result=f.service().run_once()
    assert result['reason']=='review_recovery_pending' and f.calls==calls and not f.next_calls
    assert f.queue_path.exists()


def test_uncertain_cleanup_keeps_old_queue_and_never_publishes_or_advances(recovery):
    f=recovery;f.install_seam(cleaned=False)
    with pytest.raises(DevelopmentError,match='review_recovery_cleanup_unconfirmed'):f.service().run_once()
    assert f.queue_path.exists() and 'publish' not in f.calls and not f.next_calls
    assert f.service().run_once()['reason']=='review_recovery_pending'


def test_held_old_queue_refuses_before_new_source_or_model(recovery):
    f=recovery;f.install_seam();fd=os.open(f.queue_path/'tick.lock',os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(DevelopmentError,match='review_recovery_queue_busy'):f.service().run_once()
    finally:os.close(fd)
    assert not f.calls and f.queue_path.exists()


def test_real_following_queue_observes_empty_with_new_session_after_review_cache(recovery,monkeypatch):
    f=recovery;f.install_seam();f.client.ready=False
    monkeypatch.setattr(module.ReviewRecoveryService,'_next',f.real_next)
    from blackoutburst_agents.development_queue_client import DevelopmentQueueDiscoveryClient
    sessions=[]
    class Client:
        def __init__(self,path,session_id):
            assert str(path)==f.client.connection_path
            self.connection_path=f.client.connection_path;self.connection_pin=f.client.connection_pin
            self.session_id=session_id;self.endpoint=f.client.endpoint;sessions.append(session_id)
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def registration(self,**kwargs):
            f.client.session_id=self.session_id
            return f.client.registration(**kwargs)
        def call(self,*args):return f.client.call(*args)
    monkeypatch.setattr(DevelopmentQueueDiscoveryClient,'from_connection_file',
        lambda path,*,session_id:Client(path,session_id))
    first=f.service().run_once();second=f.service().run_once()
    assert first['status']==second['status']=='pass_complete'
    assert first['queue']['phase']==second['queue']['phase']=='empty'
    assert not first['tasks'] and not second['tasks']
    assert first['review_recovery']['historical'] is False and second['review_recovery']['historical'] is True
    assert sessions and all(s!='old-session' for s in sessions)
    assert f.calls.count('model')==1


def test_unrelated_active_logical_task_is_never_retired(recovery):
    f=recovery;f.install_seam()
    tick=json.loads((f.queue_path/'tick.json').read_bytes())
    tick['active']['logical_task_id']=str(uuid.uuid4());write(f.queue_path/'tick.json',tick)
    with pytest.raises(DevelopmentError,match='review_recovery_changed'):f.service().run_once()
    assert f.queue_path.exists() and not f.calls and not f.next_calls


def test_changed_dated_proof_cannot_advance_queue_or_rerun_review(recovery):
    f=recovery;f.install_seam();f.service().run_once()
    before=list(f.calls);next_before=list(f.next_calls)
    f.proof['fixed']='changed'
    with pytest.raises(DevelopmentError,match='review_recovery_changed'):f.service().run_once()
    assert f.calls==before and f.next_calls==next_before


@pytest.mark.skipif(sys.platform!='linux',reason='renameat2 is the Linux installed-unit primitive')
def test_linux_atomic_archive_refuses_existing_destination_without_overwrite(tmp_path):
    (tmp_path/'old').mkdir();(tmp_path/'existing').mkdir()
    (tmp_path/'existing/sentinel').write_bytes(b'keep')
    fd=os.open(tmp_path,os.O_RDONLY|os.O_DIRECTORY)
    try:
        with pytest.raises(DevelopmentError,match='review_recovery_archive_refused'):
            module._rename_noreplace(fd,'old','existing')
        assert (tmp_path/'old').is_dir() and (tmp_path/'existing/sentinel').read_bytes()==b'keep'
        module._rename_noreplace(fd,'old','new')
        assert not (tmp_path/'old').exists() and (tmp_path/'new').is_dir()
    finally:os.close(fd)
