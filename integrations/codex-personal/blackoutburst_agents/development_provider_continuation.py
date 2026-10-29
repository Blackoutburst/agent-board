"""Opt-in clean provider failures; never infer quota from prose or replay work."""
from copy import deepcopy
import json
import os
import threading

from .dev_manifest import DevelopmentError,canonical,digest
from .provider_disposition import (enroll_provider_attempt,consume_provider_receipt,
    _consumed_provider_receipt_metadata,observe_provider_availability,classify_provider_availability)

_ISSUER=object()


def require(value,code='development_provider_unconfirmed'):
    if not value:raise DevelopmentError(code)


class _ProviderFailure:
    """A live Runner receipt, not reconstructable from a saved projection."""
    def __init__(self,issuer,session,receipt,metadata):
        require(issuer is _ISSUER)
        self.session,self.receipt,self.metadata=session,receipt,metadata
        self.receipt_sha256=digest(receipt.metadata_json.encode('utf-8'))
        self.pid,self.thread=os.getpid(),threading.current_thread()
    def __copy__(self):raise TypeError('Live provider evidence cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Live provider evidence cannot be copied')
    def __reduce_ex__(self,_):raise TypeError('Live provider evidence cannot be serialized')
    def verify(self,planner,request,state,stored):
        self.session.verify(planner)
        require(self.pid==os.getpid() and self.thread is threading.current_thread()
            and _consumed_provider_receipt_metadata(self.receipt)==self.metadata)
        m=self.metadata;step=state['step'];turns={row['id']:row for row in stored['turns']}
        require(state['phase']=='model_intent' and type(step) is dict and step['model_turn_id']==m['turn_id']
            and step['request_id']==m['request_id'] and step['action'] is None and step['receipt'] is None
            and step['source_snapshot'] is None and step['tool_finished'] is False and m['turn_id'] in turns
            and all(getattr(request,key)==m[key] for key in ('task_id','generation_id','cycle_id','attempt_id'))
            and stored['work']['id']==m['work_id'] and stored['work']['status']=='needs_review'
            and digest(canonical(stored))==m['work_state_sha256'])
        turn=turns[m['turn_id']];result=planner.store.turn_result(turn['id'])
        require(turn['status']=='blocked' and turn['requires_review'] and not turn['cancel_requested']
            and turn['resolution'] is None and turn['request_id']==step['request_id']
            and turn['provider_session_id']==stored['work']['provider_session_id']
            and digest(canonical(result))==m['result_sha256'] and result.get('status')=='blocked'
            and result.get('text') is None and result.get('reason')=='execution_failed'
            and all(result.get('verification',{}).get(key) is True for key in
                    ('tool_free','session_verified','cleanup_confirmed')))
        record=planner.runtime_journal.find(planner.model_guard.configuration(stored['work'],turn['id']))
        require(record is not None and record['phase']=='finished'
            and record['receipt']['id']==m['runtime_execution_id']
            and digest(canonical(record['receipt']))==m['runtime_receipt_sha256'])
        return turn,result,record


class DevelopmentProviderSession:
    """Explicit current-attempt enrollment. It neither retries nor grants access."""
    def __init__(self,issuer,task,worker,context):
        require(issuer is _ISSUER)
        self.task,self.worker,self.context=task,worker,context
        self.pid,self.thread=os.getpid(),threading.current_thread()
        self.failure=None;self._pending=None
    def __copy__(self):raise TypeError('Live provider session cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Live provider session cannot be copied')
    def __reduce_ex__(self,_):raise TypeError('Live provider session cannot be serialized')
    def verify(self,planner):
        require(self.pid==os.getpid() and self.thread is threading.current_thread()
            and planner.store is self.worker.store and planner.runtime_journal is self.worker.runtime
            and planner.client is self.worker.control and planner.parent_operation_id is not None
            and self.task.snapshot()['state_sha256']==self.context.state_sha256)
    def enroll(self,planner,state):
        self.verify(planner);require(self.failure is None and self._pending is None)
        step=state['step'];require(state['phase']=='model_intent' and step['model_turn_id'] is None)
        context=self.context;work_id=planner.work['id'];key=step['request_id'];scope_sha=digest(canonical(planner.scope))
        def received(receipt):
            planner.store._require_lock(work_id)
            raw=json.loads(receipt.metadata_json);turn_id=raw['turn_id']
            metadata=consume_provider_receipt(receipt,task_id=context.logical_task_id,generation_id=context.generation_id,
                cycle_id=context.cycle_id,attempt_id=context.attempt_id,work_id=work_id,turn_id=turn_id,
                request_id=key,scope_sha256=scope_sha,
                result_sha256=digest(canonical(planner.store.turn_result(turn_id))),
                work_state_sha256=digest(canonical(planner.store.status(work_id))))
            require(self._pending is None)
            self._pending=_ProviderFailure(_ISSUER,self,receipt,metadata)
        return enroll_provider_attempt(task_id=context.logical_task_id,generation_id=context.generation_id,
            cycle_id=context.cycle_id,attempt_id=context.attempt_id,work_id=work_id,request_id=key,
            scope_sha256=scope_sha,on_receipt=received)
    def take(self,planner,state,response):
        self.verify(planner)
        if self._pending is None:return None
        require(response['cached'] is False and response['turn_id']==self._pending.metadata['turn_id'])
        self.failure=self._pending;self._pending=None
        state['step']['model_turn_id']=response['turn_id']
        # The failed step remains model_intent with no accepted action. The
        # ordinary cached/reconcile paths still refuse it on any later entry.
        planner.journal.save(state)
        return self.failure


class DevelopmentProviderCheckpointReady:
    def __init__(self,proof):
        self.status='provider_checkpoint_ready';self.operation_id=proof.planner.operation_id;self.proof=proof


def _provider_checkpoint(planner,request,failure):
    from .development_checkpoint_proof import (_read_work,_provider_projection,DevelopmentProviderCheckpointProof)
    planner.journal.checkpoint_lock()
    with planner.broker.journal.lock() as broker,_read_work(planner.store,planner.work['id']):
        metadata=_provider_projection(planner,request,broker,failure)
    return DevelopmentProviderCheckpointReady(DevelopmentProviderCheckpointProof._issue(planner,request,metadata,failure))


class DevelopmentProviderContinuation:
    """One explicit cycle/wake. Old failed work is retained and never resumed."""
    def __init__(self,task,worker,checkpoints,*,backoff_seconds=30):
        from .continuation import ContinuationTask,ContinuationChecks
        from .development_cycle_driver import DevelopmentCycleDriver
        require(type(task) is ContinuationTask and type(backoff_seconds) is int and 1<=backoff_seconds<=3600)
        self.original_task,self.worker,self.checkpoints=task,worker,checkpoints
        self.pid,self.thread=os.getpid(),threading.current_thread()
        self.mutex=threading.Lock();self._session=None;self._wake=None;self._wake_execute=None
        self.backoff_seconds=backoff_seconds
        original=task.checks
        def provider(context):
            if original.provider_observation is not None:
                require(original.provider_observation(context) is None)
            self._provider_check(context)
        def resume(context):
            require(original.callbacks[5](context) is None)
            if self._wake is not None:self._wake_check(context)
            elif self.task.snapshot()['state']['phase']=='paused_provider':
                raise DevelopmentError('development_provider_wake_required')
        checks=ContinuationChecks(**dict(zip(('scope','claim','source','fence','runtime'),original.callbacks[:5])),
            resume=resume,provider_observation=provider,quota_observation=original.quota_observation)
        self.task=ContinuationTask(task.journal,checks,clock=task.clock)
        self.driver=DevelopmentCycleDriver(self.task,worker,checkpoints)
        self.maintenance=DevelopmentCycleDriver(task,worker,checkpoints)
    def _owner(self):
        require(self.pid==os.getpid() and self.thread is threading.current_thread(),'development_provider_process_changed')
    def _provider_check(self,context):
        require(self._session is not None and self._session.failure is not None)
        failure=self._session.failure;m=failure.metadata
        require(_consumed_provider_receipt_metadata(failure.receipt)==m
            and all(m[key]==getattr(context,key) for key in ('generation_id','cycle_id','attempt_id'))
            and m['task_id']==context.logical_task_id and context.provider_receipt_sha256==failure.receipt_sha256)
        op,cp,observation=self.driver._operation(self._operation_id)
        require(observation['schema']=='development-provider-checkpoint-observation-v1'
            and observation['provider_receipt']=={'work_id':m['work_id'],'turn_id':m['turn_id'],
                'receipt_sha256':failure.receipt_sha256}
            and observation['source_snapshot_sha256']==context.source_sha256
            and digest(canonical(self.worker.store.status(m['work_id'])))==m['work_state_sha256']
            and digest(canonical(self.worker.store.turn_result(m['turn_id'])))==m['result_sha256'])
        self.driver._cleanup(op)
    def _wake_binding(self,snapshot):
        return digest(canonical({'configuration':self.task.configuration,'state_sha256':snapshot['state_sha256'],
            'checkpoint_sha256':snapshot['state']['checkpoint_sha256']}))
    def _wake_check(self,context):
        observation,binding,old_sha=self._wake
        value=classify_provider_availability(observation,binding_sha256=binding,enabled=True)
        require(value['classification']=='available' and value['wake_candidate'] is True,
            'development_provider_wake_unconfirmed')
        if context.purpose=='execute':require(context.state_sha256==self._wake_execute)
        else:require(context.state_sha256==old_sha)
    def status(self,operation_id):
        """Durable dated metadata only. This never restores a live receipt."""
        from .dev_journal import PrivateDirectory
        from .git_workspace import _operation
        self._owner();_operation(operation_id)
        with PrivateDirectory(self.worker.journal.directory) as private:
            name='development-provider-run-'+operation_id+'.json'
            try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
            except FileNotFoundError:return {'operation_id':operation_id,'status':'not_started','launchable':False}
            record=self._read(private,name)
            require(type(record) is dict and set(record)=={'schema','phase','request','expected_revision','state_sha256','result'}
                and record['schema']=='development-provider-run-v1' and record['request']['operation_id']==operation_id
                and record['phase'] in ('running','complete'))
            if record['phase']!='complete':return {'operation_id':operation_id,'status':'needs_you','launchable':False,
                'reason':'development_provider_run_uncertain'}
            require(type(record['result']) is dict)
            wake_name='development-provider-wake-'+operation_id+'.json'
            try:os.stat(wake_name,dir_fd=private.fd,follow_symlinks=False)
            except FileNotFoundError:pass
            else:
                wake=self._read(private,wake_name)
                require(wake.get('schema')=='development-provider-wake-v1'
                    and wake.get('request',{}).get('operation_id')==operation_id
                    and wake.get('phase') in ('preparing','prepared_waiting','running','complete'))
                if wake['phase']=='complete':
                    return {**wake['result'],'previous_operation_id':operation_id,'historical':True,'launchable':False}
                if wake['phase']!='prepared_waiting':return {'operation_id':operation_id,'status':'needs_you',
                    'reason':'development_provider_wake_uncertain','launchable':False}
            return {**record['result'],'historical':True,'launchable':False}
    @staticmethod
    def _read(private,name):
        from .dev_journal import decode
        raw=private.read(name,128*1024);value=decode(raw);require(canonical(value)==raw);return value
    def run_checkpoint(self,operation_id,*,request_id,parent_checkpoint_id=None,after_actions=1):
        from .dev_journal import PrivateDirectory
        from .git_workspace import _operation
        from .continuation_journal import identifier
        self._owner();_operation(operation_id);identifier(request_id)
        require(self.mutex.acquire(blocking=False),'development_provider_busy')
        try:
            snapshot=self.task.snapshot()
            request={'operation_id':operation_id,'request_id':request_id,'parent_checkpoint_id':parent_checkpoint_id,
                'after_actions':after_actions,'task_configuration_sha256':digest(canonical(self.task.configuration))}
            name='development-provider-run-'+operation_id+'.json'
            with PrivateDirectory(self.worker.journal.directory) as private:
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:
                    require(snapshot['state']['phase']!='paused_provider' or self._wake is not None,
                        'development_provider_wake_required')
                    require(self.worker.journal.get(operation_id)['stage']=='new'
                        and not self.worker.store._db.execute('SELECT id FROM works LIMIT 1').fetchall()
                        and not self.worker.runtime.list_status(),'development_provider_new_generation_required')
                    record={'schema':'development-provider-run-v1','phase':'running','request':request,
                        'expected_revision':snapshot['state']['revision'],'state_sha256':snapshot['state_sha256'],'result':None}
                    private.create(name,canonical(record))
                else:
                    record=self._read(private,name)
                    require(record.get('request')==request,'development_provider_request_conflict')
                    require(record.get('phase')=='complete','development_provider_run_uncertain')
                    return {**record['result'],'historical':True,'launchable':False}
            start=self.task.start_cycle(expected_revision=record['expected_revision'],request_id=request_id)
            if start.permit is None:
                result={'operation_id':operation_id,'status':start.receipt['snapshot']['state']['phase'],'launchable':False}
            else:
                self._wake_execute=start.permit.context.state_sha256
                self._operation_id=operation_id
                self._session=DevelopmentProviderSession(_ISSUER,self.task,self.worker,start.permit.context)
                result=self.driver.run_checkpoint(operation_id,start=start,parent_checkpoint_id=parent_checkpoint_id,
                    after_actions=after_actions,provider_session=self._session)
                if result['status']=='paused_provider':
                    retired=self.maintenance.retire_checkpoint(operation_id)
                    require(retired['phase']=='retired')
                    result={**result,'retirement_sha256':digest(canonical(retired['request'])),
                        'retry_after_seconds':self.backoff_seconds,'cause':'unknown'}
            with PrivateDirectory(self.worker.journal.directory) as private:
                require(self._read(private,name)==record,'development_provider_request_conflict')
                private.replace(name,canonical({**record,'phase':'complete','result':result}))
            return result
        finally:
            self._session=None;self._wake_execute=None;self.mutex.release()

    def wake(self,operation_id,*,next_controller,preparer,next_operation_id,parameters,request_id,after_actions=1):
        """Prepare/admit a new concrete generation; never run the failed one.

        The caller supplies normal already-constructed new Worker handles,
        bound to the submitted preparer's context. No pool/credential/permit is
        reconstructed from the historical receipt. A pending run stays review.
        """
        from .dev_journal import PrivateDirectory
        from .dev_runtime import development_work_id
        from .dev_planner_journal import PlannerBudgets
        from .dev_planner import _reasoning_configuration,_directory_pin
        from .linux_pool_preparation import LinuxPoolJobPreparer
        from .git_workspace import _operation
        from .private_development_checkpoint import checkpoint_id_for
        from .continuation_journal import identifier
        self._owner();_operation(operation_id);_operation(next_operation_id);identifier(request_id)
        require(type(after_actions) is int and 1<=after_actions<=64)
        require(type(next_controller) is DevelopmentProviderContinuation and next_controller is not self
            and next_controller.worker is not self.worker
            and next_controller.original_task.journal is self.original_task.journal
            and next_controller.original_task.configuration==self.original_task.configuration
            and next_controller.original_task.clock is self.original_task.clock
            and type(preparer) is LinuxPoolJobPreparer and type(parameters) is dict,
            'development_provider_new_generation_required')
        require(self.mutex.acquire(blocking=False),'development_provider_busy')
        try:
            parent=self.checkpoints.verify(checkpoint_id_for(operation_id))
            metadata={key:(value.document() if key in ('planner_budgets','broker_budgets') else deepcopy(value))
                for key,value in parameters.items()}
            request={'operation_id':operation_id,'next_operation_id':next_operation_id,'request_id':request_id,
                'after_actions':after_actions,'parent_checkpoint_sha256':parent['checkpoint_sha256'],
                'parameters_sha256':digest(canonical(metadata)),'preparer':preparer.pin,
                'worker_pins':{'journal':_directory_pin(next_controller.worker.journal.directory),
                    'runs':_directory_pin(next_controller.worker.store.state_dir),
                    'runtime':_directory_pin(next_controller.worker.runtime.directory)}}
            name='development-provider-wake-'+operation_id+'.json'
            with PrivateDirectory(self.worker.journal.directory) as private:
                run=self._read(private,'development-provider-run-'+operation_id+'.json')
                require(run.get('schema')=='development-provider-run-v1' and run.get('phase')=='complete'
                    and run.get('result',{}).get('status')=='paused_provider',
                    'development_provider_run_uncertain')
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:prior=None
                else:
                    prior=self._read(private,name)
                    require(prior.get('request')==request,'development_provider_wake_conflict')
                    if prior.get('phase')=='complete':return {**prior['result'],'historical':True,'launchable':False}
            paused=self.maintenance._paused();require(paused['state']['phase']=='paused_provider')
            op,parent,_=self.maintenance._operation(operation_id)
            with PrivateDirectory(self.worker.journal.directory) as private:
                _,retirement=self.maintenance._retirement(private,op)
                require(retirement is not None and retirement['phase']=='retired',
                    'development_provider_retirement_required')
            require(next_operation_id!=operation_id and next_controller.checkpoints.workspaces is preparer.workspaces
                and next_controller.checkpoints.configuration==self.checkpoints.configuration,
                'development_provider_new_generation_required')
            retired=self.maintenance.retire_checkpoint(operation_id);require(retired['phase']=='retired')
            executable=self.worker.store.status(development_work_id(operation_id))['work']['executable']
            def observed():
                snapshot=self.maintenance._paused();binding=self._wake_binding(snapshot)
                value=observe_provider_availability(executable,binding_sha256=binding,timeout=30)
                candidate=classify_provider_availability(value,binding_sha256=binding,enabled=True)
                require(self.task.snapshot()==snapshot,'development_provider_wake_changed')
                return snapshot,binding,value,candidate
            snapshot,binding,observation,candidate=observed()
            if candidate['classification']=='provider_unavailable':
                return {'status':'paused_provider','retry_after_seconds':self.backoff_seconds,'launchable':False}
            require(candidate['classification']=='available','development_provider_wake_unconfirmed')
            with PrivateDirectory(self.worker.journal.directory) as private:
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:
                    record={'schema':'development-provider-wake-v1','phase':'preparing','request':request,'result':None}
                    private.create(name,canonical(record))
                else:
                    record=self._read(private,name)
                    require(record.get('request')==request,'development_provider_wake_conflict')
                    if record.get('phase')=='complete':return {**record['result'],'historical':True,'launchable':False}
                    require(record.get('phase')=='prepared_waiting','development_provider_wake_uncertain')
            self.maintenance.prepare_next(operation_id,preparer=preparer,next_operation_id=next_operation_id,parameters=parameters)
            _,job=preparer._job(next_operation_id,op['scope']['project_id'],op['scope']['ticket_id'])
            context=preparer.pool._context(job);bound=preparer.driver.selections.load(context)
            selection=bound['selection'];worker=next_controller.worker
            require(worker.client.session_id==job['session_id'] and worker.client.session_id!=self.worker.client.session_id
                and worker.client.endpoint==op['scope']['board_endpoint']
                and str(worker.journal.directory)==str(context.journals['work'])
                and str(worker.store.state_dir)==str(context.journals['runs'])
                and str(worker.runtime.directory)==str(context.journals['runtime']),
                'development_provider_new_generation_required')
            worker.prepare(scope=selection['scope'],request_id='development-provider-wake-'+next_operation_id,
                instruction=bound['instruction'],broker_directory=selection['broker_directory'],
                backend_directory=selection['backend_directory'],planner_policy_directory=selection['planner_policy_directory'],
                model=selection['model'],budgets=PlannerBudgets(**selection['budgets']),operation_id=next_operation_id,
                **_reasoning_configuration(selection.get('reasoning_effort')))
            preparer.driver._operation(worker,context,bound)
            require(worker.journal.get(next_operation_id)['stage']=='new'
                and not worker.store._db.execute('SELECT id FROM works LIMIT 1').fetchall()
                and not worker.runtime.list_status(),'development_provider_new_generation_required')
            snapshot,binding,observation,candidate=observed()
            if candidate['classification']=='provider_unavailable':
                with PrivateDirectory(self.worker.journal.directory) as private:
                    require(self._read(private,name)==record,'development_provider_wake_conflict')
                    private.replace(name,canonical({**record,'phase':'prepared_waiting'}))
                return {'status':'paused_provider','retry_after_seconds':self.backoff_seconds,'launchable':False}
            require(candidate['classification']=='available','development_provider_wake_unconfirmed')
            with PrivateDirectory(self.worker.journal.directory) as private:
                require(self._read(private,name)==record,'development_provider_wake_conflict')
                record={**record,'phase':'running'};private.replace(name,canonical(record))
            next_controller._wake=(observation,binding,snapshot['state_sha256'])
            try:
                result=next_controller.run_checkpoint(next_operation_id,request_id=request_id,
                    parent_checkpoint_id=parent['checkpoint_id'],after_actions=after_actions)
            finally:next_controller._wake=None
            with PrivateDirectory(self.worker.journal.directory) as private:
                require(self._read(private,name)==record,'development_provider_wake_conflict')
                private.replace(name,canonical({**record,'phase':'complete','result':result}))
            return result
        finally:self.mutex.release()
