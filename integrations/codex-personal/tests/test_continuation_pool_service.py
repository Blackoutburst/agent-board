"""Scheduler IO/stop/lease tests; no production executor injection."""
from copy import deepcopy
import json
import os
import sys
import threading
import time
import types
import uuid

import pytest

from blackoutburst_agents import continuation_pool_service as module
from blackoutburst_agents.dev_manifest import canonical,DevelopmentError
from test_continuation_pool_configuration import case


@pytest.fixture
def service_case(case,monkeypatch):
    root,path,config,batch,workflow,write,_=case
    monkeypatch.setattr(module,'_executable_pin',lambda value:{'path':str(value),'sha256':'c'*64})
    monkeypatch.setattr(module,'linux_source_pins',lambda:{'synthetic':'sources'})
    def construct():
        return module.ContinuationPoolService(root,configuration_path=path,owner_id='owner',
            board_endpoint=batch['board_endpoint'],executable='/synthetic/codex',cli_version='codex-cli 1.2.3',
            interpreter='/synthetic/python',unshare='/synthetic/unshare',gh_executable='/synthetic/gh',max_seconds=90)
    return construct,case


def available_apis(monkeypatch):
    # Scheduler-only unit tests. Concrete resource integration is separate.
    for name,attribute in [('development_provider_continuation','DevelopmentProviderContinuation'),
            ('development_aggregate_delivery','AggregateDevelopmentDeliveryController')]:
        value=types.ModuleType(name);setattr(value,attribute,object)
        monkeypatch.setitem(sys.modules,'blackoutburst_agents.'+name,value)


def test_construction_is_inert_without_connection_reads(service_case):
    construct,(root,*_)=service_case
    before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    construct()
    assert not (root/'continuations').exists() and not (root/'connections').exists()
    assert before=={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_missing_new_api_has_no_intent_or_fallback(service_case,monkeypatch):
    construct,(root,*_)=service_case;service=construct()
    monkeypatch.setitem(sys.modules,'blackoutburst_agents.development_provider_continuation',None)
    with pytest.raises(DevelopmentError,match='continuation_pool_api_unavailable'):service.run_once()
    assert not (root/'continuations').exists()


def test_configuration_drift_refuses_before_private_state(service_case):
    construct,(root,path,config,batch,workflow,write,_)=service_case
    service=construct();config['checkpoint_after_actions']='UNTRUSTED';write()
    with pytest.raises(DevelopmentError,match='continuation_pool_configuration_changed'):service.run_once()
    assert not (root/'continuations').exists()


def test_selected_target_once_in_own_thread(service_case,monkeypatch):
    construct,(root,path,config,batch,*_)=service_case;available_apis(monkeypatch);calls=[]
    def target(self,root,target,job,control):
        assert threading.current_thread() is not self.thread
        calls.append((target['logical_task_id'],job['agent_id']))
        return self._projection(target,phase='paused_cycle')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    result=construct().run_once()
    assert calls==[(config['tasks'][0]['logical_task_id'],batch['jobs'][0]['agent_id'])]
    assert result['status']=='pass_complete' and not result['pass_complete_is_project_completion']
    assert 'PRIVATE_' not in json.dumps(result)


def test_pending_intent_never_replayed_and_changed_config_never_freshens_namespace(service_case):
    construct,(root,path,config,batch,workflow,write,_)=service_case;service=construct()
    target=config['tasks'][0];job=batch['jobs'][0]
    with service._lease() as (owned,held):
        key=module.digest(canonical({'owner_id':'owner','logical_task_id':target['logical_task_id']}))
        directory=owned.path/key;directory.mkdir(mode=0o700)
        with module.PrivateDirectory(directory) as private:
            module._write(private,'binding.json',service._binding(target,job))
            module._write(private,'cursor.json',{'schema':'continuation-pool-cursor-v1','phase':'pending',
                'generation':str(uuid.uuid4()),'result':None,'not_before':None})
        before={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        result=service._target(owned.path,target,job,module._Pass(service,None))
        assert result['phase']=='needs_you' and result['reason']=='continuation_pool_pending'
        assert before=={str(p):p.read_bytes() for p in directory.rglob('*') if p.is_file()}
        config['tasks'][0]['checkpoint_after_actions']=1;write();other=construct()
        with pytest.raises(DevelopmentError,match='continuation_pool_binding_changed'):
            other._target(owned.path,target,job,module._Pass(other,None))


def test_owner_flock_blocks_another_service(service_case,monkeypatch):
    construct,_=service_case;first,other=construct(),construct();available_apis(monkeypatch)
    with first._lease():
        with pytest.raises(DevelopmentError,match='continuation_pool_owner_busy'):other.run_once()


def test_stop_before_admission_never_opens_task(service_case,monkeypatch):
    construct,_=service_case;available_apis(monkeypatch)
    monkeypatch.setattr(module.ContinuationPoolService,'_target',lambda *_:pytest.fail('admitted after Stop'))
    result=construct().run_once(should_stop=lambda:True)
    assert result['status']=='stopped' and result['tasks'][0]['phase']=='not_started'


def test_global_deadline_cancels_active_target_and_blocks_later_one(service_case,monkeypatch):
    construct,(root,path,config,batch,workflow,write,_)=service_case
    job=deepcopy(batch['jobs'][0]);job.update(name='second',agent_id='second-agent',ticket_id='second-ticket',resource='repo:second',
        connection_file=str(root/'connections/second.json'))
    job['repository']['workspace_root']=str(root/'checkouts'/batch['batch_id']/'second');batch['jobs'].append(job)
    spec=deepcopy(workflow['jobs'][0]);spec['name']='second';workflow['jobs'].append(spec)
    config['tasks'].append(dict(name='second',logical_task_id=str(uuid.uuid4()),checkpoint_after_actions=1));write()
    service=construct();available_apis(monkeypatch);calls=[];original=module._Pass.__init__
    def short(self,*args):original(self,*args);self.deadline=time.monotonic()+.06
    monkeypatch.setattr(module._Pass,'__init__',short)
    def target(self,root,target,job,control):
        calls.append(target['name'])
        while not control.stopping():time.sleep(.005)
        return self._projection(target,phase='stopped')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    before=time.monotonic();result=service.run_once()
    assert time.monotonic()-before<1 and calls==['coder']
    assert result['status']=='stopped' and result['tasks'][1]['phase']=='not_started'


def test_unjoined_thread_keeps_owner_lease_and_blocks_reentry(service_case,monkeypatch):
    construct,_=service_case;service=construct();available_apis(monkeypatch)
    original=module._Pass.__init__;finished=threading.Event();entered=threading.Event()
    def short(self,*args):original(self,*args);self.deadline=time.monotonic()+.03
    monkeypatch.setattr(module._Pass,'__init__',short);monkeypatch.setattr(module,'JOIN_SECONDS',.03)
    def target(self,root,target,job,control):
        entered.set();finished.wait(2)
        return self._projection(target,phase='stopped')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    try:
        result=service.run_once()
        assert entered.is_set() and result['status']=='needs_you' and service._retained_lease
        with pytest.raises(DevelopmentError,match='continuation_pool_cleanup_unknown'):service.run_once()
        with pytest.raises(DevelopmentError,match='continuation_pool_owner_busy'):construct().run_once()
    finally:
        finished.set()
        for thread in threading.enumerate():
            if thread.name.startswith('continuation-'):thread.join(2)
        if isinstance(service._retained_lease,tuple):
            owned,fd=service._retained_lease;os.close(fd);owned.close()


def test_unknown_exception_stays_closed_metadata(service_case,monkeypatch):
    construct,_=service_case;available_apis(monkeypatch)
    def target(*_):raise RuntimeError('PRIVATE_MODEL_TEXT')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    result=construct().run_once()
    assert result['status']=='needs_you' and 'PRIVATE_' not in json.dumps(result)
    assert result['tasks'][0]['reason']=='continuation_pool_target_failed'


@pytest.mark.parametrize('field',['job','tools','deadline'])
def test_mutable_local_configuration_never_escapes_original_pins(service_case,field):
    construct,(root,*_)=service_case;service=construct()
    if field=='job':service.loaded['batch']['jobs'][0]['model']='unselected-model'
    elif field=='tools':service.tools['executable']['path']='/different/codex'
    else:service.max_seconds=900
    with pytest.raises(DevelopmentError,match='continuation_pool_configuration_changed'):service.run_once()
    assert not (root/'continuations').exists()


def test_missing_existing_owner_lock_is_uncertain_and_never_recreated(service_case,monkeypatch):
    construct,(root,*_)=service_case;service=construct();available_apis(monkeypatch)
    (root/'continuations').mkdir(mode=0o700)
    with pytest.raises(DevelopmentError,match='continuation_pool_pending'):service.run_once()
    assert not (root/'continuations'/'owner.lock').exists()


@pytest.mark.parametrize('mutation',['missing','replacement','hardlink','empty'])
def test_stop_never_constructs_or_repairs_an_unconfirmed_work_database(service_case,monkeypatch,tmp_path,mutation):
    from blackoutburst_agents.work_journal import WorkJournal
    construct,_=service_case;service=construct();control=module._Pass(service,None)
    directory=tmp_path.resolve()/'worker'
    with WorkJournal(directory):pass
    operation=str(uuid.uuid4());scope={'owner_id':'owner'}
    control.add_worker(directory,operation,scope)
    db=directory/'work-journal.sqlite3'
    if mutation=='missing':db.unlink()
    elif mutation=='replacement':
        db.rename(directory/'old.sqlite3')
        with WorkJournal(directory):pass
    elif mutation=='hardlink':os.link(db,directory/'alias.sqlite3')
    else:db.write_bytes(b'')
    before={str(p):p.read_bytes() for p in directory.iterdir() if p.is_file()}
    monkeypatch.setattr('blackoutburst_agents.work_journal.WorkJournal',lambda *_:pytest.fail('unconfirmed DB constructor'))
    control.stop()
    assert control.stop_failed and control.cancel.is_set()
    assert before=={str(p):p.read_bytes() for p in directory.iterdir() if p.is_file()}


def test_failed_stop_keeps_lease_even_after_target_thread_returns(service_case,monkeypatch):
    construct,_=service_case;service=construct();available_apis(monkeypatch)
    def target(self,root,target,job,control):
        control.stop_failed=True
        return self._projection(target,phase='stopped')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    try:
        assert service.run_once()['status']=='needs_you'
        with pytest.raises(DevelopmentError,match='continuation_pool_owner_busy'):construct().run_once()
    finally:
        owned,fd=service._retained_lease;os.close(fd);owned.close()


def test_abrupt_target_interruption_never_reports_pass_complete_or_prints_raw_text(service_case,monkeypatch,capsys):
    construct,_=service_case;service=construct();available_apis(monkeypatch)
    def target(*_):raise SystemExit('PRIVATE_INTERRUPTION_BODY')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    try:
        result=service.run_once()
        assert result['status']=='needs_you' and result['tasks'][0]['reason']=='continuation_pool_cleanup_unknown'
        assert service._retained_lease and 'PRIVATE_' not in str(capsys.readouterr())
    finally:
        owned,fd=service._retained_lease;os.close(fd);owned.close()


def test_owner_lock_change_stops_and_joins_before_releasing_local_owner_handle(service_case,monkeypatch):
    construct,(root,*_)=service_case;service=construct();available_apis(monkeypatch)
    entered=threading.Event();finished=threading.Event()
    def target(self,root,target,job,control):
        (root/'owner.lock').rename(root/'owner-lock-replaced')
        # The scheduler sees the name replacement; cancellation must still
        # reach this own thread before the owner context is unwound.
        entered.set()
        while not control.cancel.wait(.01):pass
        finished.set();return self._projection(target,phase='stopped')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    with pytest.raises(DevelopmentError):service.run_once()
    assert entered.is_set() and finished.is_set()
    assert not any(thread.name=='continuation-coder' and thread.is_alive() for thread in threading.enumerate())
