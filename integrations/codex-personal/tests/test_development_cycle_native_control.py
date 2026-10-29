"""Real SDK control boundaries; native/CLI and control latency are synthetic."""
import json

import pytest

from blackoutburst_agents import continuation_pool_service as service
from blackoutburst_agents.development_authorization import DevelopmentAuthorization
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.dev_test_supervisor import CONTROL_TTL,_fresh_control_window
from blackoutburst_agents.linux_test_backend import (CHECKS,NATIVE_SCHEMA,
    SupervisedLinuxNamedPythonBackend,validate_native_receipt)
from test_continuation_pool_service_integration import actual_service,linux_lock_observation,_REAL_LINUX_TEST_RUN


@pytest.mark.parametrize('fault',[None,'permission','source','fence','core_stop'])
def test_logical_observations_are_outside_native_windows_without_skipping_actual_guards(actual_service,monkeypatch,fault):
    f,root,task,ticket,construct,calls,sequence=actual_service
    path=root/'config/continuation.json';value=decode(path.read_bytes());value['tasks'][0]['checkpoint_after_actions']=3;path.write_bytes(canonical(value))
    path=root/'config/batch.json';value=decode(path.read_bytes());job=value['jobs'][0]
    job['planner_budgets'].update(max_turns=8,max_actions=8)
    job['broker_budgets'].update(max_operations=24,max_tests=2)
    path.write_bytes(canonical(value))
    sequence.write_text(json.dumps([{'schema':1,'kind':'read','path':'one.py'},
        {'schema':1,'kind':'patch','path':'one.py','expected_sha256':digest(b'value = 1\n'),'content':'value = 2\n'},
        {'schema':1,'kind':'test','name':'unit'}]))
    clock=[100.];scope_calls=[];snapshots=[];windows=[];generations=[];native_calls=[]
    original_scope=service._LiveChecks.scope
    def scope(self,context):
        scope_calls.append(True);clock[0]+=.8
        return original_scope(self,context)
    monkeypatch.setattr(service._LiveChecks,'scope',scope)
    original_snapshot=DevelopmentAuthorization.snapshot
    def snapshot(self,*args,**kwargs):
        snapshots.append(True)
        return original_snapshot(self,*args,**kwargs)
    monkeypatch.setattr(DevelopmentAuthorization,'snapshot',snapshot)
    original_open=service._Generation.open_task
    def opened(self,*args,**kwargs):
        result=original_open(self,*args,**kwargs);generations.append(self);return result
    monkeypatch.setattr(service._Generation,'open_task',opened)
    def native(backend,request,inputs,script,attempt,*,qualification,qualification_case=None):
        assert qualification is False and qualification_case is None
        native_calls.append(True);generation=generations[-1]
        if fault=='permission':f.board.set_policy(f.human,True,True,True,True,[],[])
        elif fault=='core_stop':
            state=generation.task.snapshot()['state']
            generation.task.stop(expected_revision=state['revision'],request_id='stop-before-native-source')
        elif fault=='fence':
            with PrivateDirectory(generation.worker.test_anchor) as private:
                state=decode(private.read('fence.json',2*1024*1024))
                rows=[row for row in state['records'].values() if row['operation_id']==generation.operation]
                assert len(rows)==1;rows[0]['operation_id']='00000000-0000-0000-0000-000000000000'
                private.replace('fence.json',canonical(state))
        before_scope=len(scope_calls);before_snapshots=len(snapshots);started=clock[0];failure=None
        try:_fresh_control_window(attempt.authorize,nonce='a'*32,sequence=1,clock=lambda:clock[0])
        except DevelopmentError as error:failure=error.code
        windows.append({'scope_calls':len(scope_calls)-before_scope,'fresh_snapshots':len(snapshots)-before_snapshots,
            'elapsed':clock[0]-started,'failure':failure})
        released=failure is None
        if released:
            if fault=='source':
                from pathlib import Path
                (Path(backend.plan['root'])/'one.py').write_bytes(b'external change\n')
            try:attempt.before_source()
            except DevelopmentError:released=False
        receipt={'schema':NATIVE_SCHEMA,'nonce':'a'*32,'request_sha256':digest(canonical(request)),
            'plan_sha256':digest(canonical(backend.plan)),'source_released':released,'source_release_intent':released,
            'exit_code':0 if released else None,'reason':'completed' if released else 'linux_authorization_expired',
            'checks':{key:True for key in CHECKS} if released else {},'launcher_attempted':True,
            'output_bytes':0,'output_sha256':digest(b''),'cleanup':{'init_identity_received':True,'init_terminal':True,
            'launcher_reaped':True,'cleanup_confirmed':True,'numeric_pid_signalled':False},
            'identity':{'namespace_identity':[1,2],'acquired_by':'SCM_RIGHTS_validated','numeric_pid_signalled':False},
            'broker_snapshot_admitted':False}
        validate_native_receipt(receipt,{'nonce':'a'*32,'request':request,'max_output_bytes':request['max_output_bytes']},backend.plan)
        return receipt
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'run',_REAL_LINUX_TEST_RUN)
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'_run_job',native)
    result=construct().run_once()
    assert len(windows)==len(native_calls)==1 and len(calls.read_text().splitlines())==3
    assert CONTROL_TTL==1.5 and windows[0]['scope_calls']==0 and windows[0]['elapsed']==0
    planners=list((root/'continuations').glob('*/generations/*/preparation/*/broker/planner.json'))
    assert len(planners)==1;planner=decode(planners[0].read_bytes())
    if fault is None:
        assert windows[0]['fresh_snapshots']>=1 and windows[0]['failure'] is None
        assert result['tasks'][0]['phase']=='paused_cycle',result
        assert [row['kind'] for row in planner['history']]==['read','patch','test']
        assert f.board.get_runtime(f.human)['active_count']==0
    else:
        assert result['tasks'][0]['phase'] in ('needs_you','stopped'),result
        assert [row['kind'] for row in planner['history']]==['read','patch']
        assert planner['step']['tool_finished'] is False and planner['step']['receipt'] is None
        assert not any(path.is_dir() for path in (generations[-1].target_directory/'checkpoints').iterdir())
        # No waiver: a negative Native before SOURCE remains for cleanup/review.
        assert f.board.get_ticket(f.human,ticket['id'])['ticket']['claimant'] is not None
    print(json.dumps({'fault':fault,'window':windows[0],'result':result['tasks'][0]['phase']},sort_keys=True))
