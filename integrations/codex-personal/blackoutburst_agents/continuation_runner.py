"""Explicit bounded PersonalRunner cycles; no queue, daemon or development tool.

The caller supplies protected prompt/checkpoint and Runtime factories. Each
cycle uses a fresh work and session; old results and review flags are retained.
An uncertain intent is review-only. Saved receipt JSON never grants a restart.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import uuid

from .continuation import (ContinuationTask,ContinuationChecks,ContinuationError,
    USAGE_FIELDS,validate_checkpoint,require)
from .continuation_journal import identifier
from .dev_journal import PrivateDirectory,decode
from .dev_manifest import canonical,digest,DevelopmentError
from .provider_disposition import (enroll_provider_attempt,consume_provider_receipt,
    observe_provider_availability,classify_provider_availability,_consumed_provider_receipt_metadata)
from .run_store import RunStore,CONTINUATION_MAX_TURNS
from .runner import PersonalRunner
from .runtime_guard import RuntimeGuard
from .runtime_journal import RuntimeJournal


class ContinuationRunner:
    """One explicit cycle per call. Factories are trusted code, not Board data.

    runtime_factory(context) returns the concrete RuntimeGuard for a new Board
    session. checkpoint_factory(context) returns bounded source/test references;
    prompt_factory(context,index) supplies private text only to PersonalRunner.
    This controller cannot execute DevelopmentPlanner actions or publish work.
    """
    def __init__(self,task,store,*,directory,workspace,executable,model,runtime_factory,
            checkpoint_factory,prompt_factory,backoff_seconds=30):
        require(type(task) is ContinuationTask and type(store) is RunStore
            and all(callable(item) for item in (runtime_factory,checkpoint_factory,prompt_factory))
            and type(backoff_seconds) is int and 1<=backoff_seconds<=300)
        require(getattr(store,'continuation_v1',False) is True,'continuation_store_optin_required')
        identifier(model)
        self.store=store;self.runtime_factory=runtime_factory;self.checkpoint_factory=checkpoint_factory
        self.prompt_factory=prompt_factory;self.workspace=str(Path(workspace).resolve(strict=True))
        self.executable=str(Path(executable).resolve(strict=True));self.model=model;self.backoff_seconds=backoff_seconds
        require(Path(self.workspace).is_dir() and Path(self.executable).is_file())
        self.private=PrivateDirectory(directory,create=True);self.mutex=threading.Lock()
        self._provider=None;self._wake=None;self._binding=None
        self.pid,self.thread=os.getpid(),threading.current_thread()
        original=task.checks
        def runtime(context):
            require(original.callbacks[4](context) is None,'continuation_check_result_invalid')
            if context.purpose not in {'start','execute'} and self._binding is not None:
                self._terminal_work(self._binding['guard'],self._binding['work_id'])
        def provider(context):
            if original.provider_observation is not None:
                require(original.provider_observation(context) is None,'continuation_check_result_invalid')
            self._provider_check(context)
        def resume(context):
            require(original.callbacks[5](context) is None,'continuation_check_result_invalid')
            if self._wake is not None:self._wake_check(context)
        checks=ContinuationChecks(**dict(zip(('scope','claim','source','fence'),original.callbacks[:4])),runtime=runtime,
            resume=resume,provider_observation=provider,quota_observation=original.quota_observation)
        self.task=ContinuationTask(task.journal,checks,clock=task.clock)
        self.configuration_sha256=digest(canonical({'workspace':self.workspace,'executable':self.executable,
            'model':model,'task_configuration':task.configuration}))
        config={'schema':'continuation-runner-configuration-v1','configuration_sha256':self.configuration_sha256}
        try:
            try:self._record('configuration.json',config)
            except DevelopmentError as error:
                if error.code!='private_file_exists':raise
                require(self._read('configuration.json')==config,'continuation_configuration_changed')
        except BaseException:
            self.private.close();raise

    def close(self):self.private.close()

    def _owner(self):
        require(os.getpid()==self.pid and threading.current_thread() is self.thread,'continuation_process_changed')

    def _record(self,name,value):
        raw=canonical(value)
        require(len(raw)<=65536)
        self.private.create(name,raw)
        return digest(raw)

    def _read(self,name):
        raw=self.private.read(name,65536);value=decode(raw)
        require(canonical(value)==raw)
        return value

    def _request(self,request_id,snapshot,turns,complete):
        identifier(request_id);name='request-'+digest(request_id.encode('ascii'))+'.json'
        value={'schema':'continuation-runner-request-v1','request_id':request_id,
            'expected_revision':snapshot['state']['revision'],'state_sha256':snapshot['state_sha256'],
            'turns':turns,'complete':complete,'configuration_sha256':self.configuration_sha256}
        try:self._record(name,value)
        except DevelopmentError as error:
            if error.code!='private_file_exists':raise
            old=self._read(name)
            require(type(old) is dict and set(old)==set(value)
                and all(old[key]==value[key] for key in ('schema','request_id','turns','complete','configuration_sha256')),
                'continuation_request_conflict')
            value=old
        return value

    def _wake_binding(self,snapshot):
        return digest(canonical({'task_configuration':self.task.configuration,'state_sha256':snapshot['state_sha256'],
            'checkpoint_sha256':snapshot['state']['checkpoint_sha256'],'configuration_sha256':self.configuration_sha256}))

    def _wake_check(self,context):
        observation,binding,state_sha=self._wake
        if context.purpose=='start':require(context.state_sha256==state_sha,'continuation_provider_unconfirmed')
        value=classify_provider_availability(observation,binding_sha256=binding,enabled=True)
        require(value['classification']=='available' and value['wake_candidate'] is True,'continuation_provider_unconfirmed')

    def _stop(self):
        snapshot=self.task.snapshot();state=snapshot['state']
        if state['phase']=='stopped':return True
        return bool(self._binding is not None and (state['phase']!='running'
            or snapshot['state_sha256']!=self._binding['context'].state_sha256))

    def _fresh_runtime(self,guard,work,turn_id):
        require(type(guard) is RuntimeGuard and type(guard.journal) is RuntimeJournal
            and guard.control_failed is False,'continuation_runtime_unknown')
        operation=guard.journal.find(guard.configuration(work,turn_id))
        require(operation is not None and operation['phase']=='finished'
            and operation['start_confirmed']==1,'continuation_runtime_unknown')
        guard.operation=operation
        found=guard.client.call('lookup_execution',{'idempotency_key':operation['key']})
        require(type(found) is dict and found.get('found') is True,'continuation_runtime_unknown')
        receipt=guard.metadata(found.get('execution'))
        require(receipt==operation['receipt'] and receipt['state']=='finished' and receipt['cleanup_confirmed'] is True
            and receipt['stop_requested'] is False and receipt['should_stop'] is True
            and receipt['control_reason']=='finished'
            and receipt['device_id']==self.task.configuration['scope']['device_id']
            and receipt['profile_version']==self.task.configuration['scope']['profile_version'],'continuation_runtime_unknown')
        return receipt

    def _before_prompt(self,context,guard):
        require(not self._stop(),'continuation_stopped')
        # Runner already admitted this exact new turn. The core Runtime callback
        # attests a clean boundary, so inspect actual active admission here.
        for index in (0,1,2,3,5):
            require(self.task.checks.callbacks[index](context) is None,'continuation_check_result_invalid')
        operation=guard.operation
        require(operation is not None and operation['phase']=='active' and operation['start_confirmed']==1
            and operation['configuration']['work_id']==self._binding['work_id']
            and guard.control_failed is False,'continuation_runtime_unknown')
        found=guard.client.call('lookup_execution',{'idempotency_key':operation['key']})
        require(type(found) is dict and found.get('found') is True,'continuation_runtime_unknown')
        receipt=guard.metadata(found.get('execution'))
        require(receipt['state']=='running' and receipt['should_stop'] is False and receipt['stop_requested'] is False
            and receipt['device_id']==self.task.configuration['scope']['device_id']
            and receipt['profile_version']==self.task.configuration['scope']['profile_version'],'continuation_runtime_unknown')
        require(not self._stop(),'continuation_stopped')

    def _terminal_work(self,guard,work_id):
        self.store._require_lock(work_id)
        state_sha=self.store.continuation_state_sha256(work_id)
        stored=self.store.status(work_id)
        require(0<len(stored['turns'])<=CONTINUATION_MAX_TURNS,'continuation_runtime_unknown')
        for turn in stored['turns']:self._fresh_runtime(guard,stored['work'],turn['id'])
        require(self.store.continuation_state_sha256(work_id)==state_sha,'continuation_runtime_unknown')

    def _provider_check(self,context):
        evidence=self._provider;binding=self._binding
        require(evidence is not None and binding is not None,'continuation_provider_unconfirmed')
        metadata=evidence['metadata'];guard=binding['guard'];work=self.store.get_work(metadata['work_id'])
        require(_consumed_provider_receipt_metadata(evidence['receipt'])==metadata,'continuation_provider_unconfirmed')
        require(metadata['task_id']==context.logical_task_id and metadata['generation_id']==context.generation_id
            and metadata['cycle_id']==context.cycle_id and metadata['attempt_id']==context.attempt_id
            and context.provider_receipt_sha256==evidence['receipt_sha256']
            and metadata['scope_sha256']==digest(canonical(guard.scope)),'continuation_provider_unconfirmed')
        receipt=self._fresh_runtime(guard,work,metadata['turn_id'])
        require(digest(canonical(receipt))==metadata['runtime_receipt_sha256'] and not self._stop(),
            'continuation_provider_unconfirmed')
        closure=self.store.continuation_closure(work['id'])
        if closure is None:
            require(self.store.continuation_state_sha256(work['id'])==metadata['work_state_sha256'],
                'continuation_provider_unconfirmed')
        else:
            require(closure['intent_id']==evidence.get('intent_id') and closure['reason']=='provider'
                and closure['pre_state_sha256']==metadata['work_state_sha256']
                and closure['provider_receipt_sha256']==evidence['receipt_sha256'],'continuation_provider_unconfirmed')

    def _checkpoint(self,context,*,provider=None):
        checkpoint=validate_checkpoint(self.checkpoint_factory(context))
        if provider is not None:
            checkpoint['provider_receipt']={'work_id':provider['metadata']['work_id'],'turn_id':provider['metadata']['turn_id'],
                'receipt_sha256':provider['receipt_sha256']}
        return validate_checkpoint(checkpoint)

    def _review(self,used,attempt_id):
        snapshot=self.task.snapshot();state=snapshot['state']
        if state['phase']!='running' or state['current_cycle']['attempt_id']!=attempt_id:return snapshot
        cp=state['checkpoint'] or {'schema':1,'source_sha256':self.task.configuration['initial_source_sha256'],
            'diff_sha256':'0'*64,'plan_sha256':'0'*64,'test_receipts':[],'provider_receipt':None}
        return self.task.checkpoint(expected_revision=state['revision'],request_id='review-'+state['current_cycle']['attempt_id'],
            checkpoint=cp,cycle_used=used,reason='needs_you')['snapshot']

    def _seal(self,context,work,used,reason,*,provider=None):
        self.store._require_lock(work['id']);self._provider=provider
        before=self.task.snapshot();revision=before['state']['revision']
        require(before['state']['phase']=='running' and context.attempt_id==before['state']['current_cycle']['attempt_id']
            and before['state_sha256']==context.state_sha256)
        checkpoint=self._checkpoint(context,provider=provider)
        state_sha=self.store.continuation_state_sha256(work['id']);intent_id=str(uuid.uuid4())
        if provider is not None:provider['intent_id']=intent_id
        boundary=self.task.clean_boundary(expected_revision=revision,checkpoint=checkpoint,cycle_used=used,reason=reason)
        intent={'schema':'continuation-runner-seal-v1','task_id':context.logical_task_id,'generation_id':context.generation_id,
            'cycle_id':context.cycle_id,'attempt_id':context.attempt_id,'work_id':work['id'],
            'expected_work_state_sha256':state_sha,'core_state_sha256':before['state_sha256'],
            'checkpoint':checkpoint,'checkpoint_sha256':digest(canonical(checkpoint)),'cycle_used':used,
            'reason':reason,'intent_id':intent_id,'configuration_sha256':self.configuration_sha256}
        intent_sha=self._record('seal-'+context.attempt_id+'.json',intent)
        sealed=self.store.seal_continuation_work_v1(work['id'],expected_state_sha256=state_sha,intent_id=intent_id,
            reason=reason,boundary=boundary,provider_receipt=None if provider is None else provider['receipt'])
        require(self._read('seal-'+context.attempt_id+'.json')==intent,'continuation_configuration_changed')
        result=self.task.checkpoint(expected_revision=revision,request_id='checkpoint-'+context.attempt_id,
            checkpoint=checkpoint,cycle_used=used,reason=reason,boundary=boundary)
        require(result['snapshot']['state']['phase']=={'provider':'paused_provider','cycle':'paused_cycle','complete':'complete'}[reason],
            'continuation_boundary_unknown')
        self._record('receipt-'+context.attempt_id+'.json',{'schema':'continuation-runner-receipt-v1','intent_sha256':intent_sha,
            'seal_receipt_sha256':digest(canonical(sealed)),'core_receipt':result['receipt']})
        return result

    def wake(self,*,request_id,turns=1,complete=False):
        self._owner()
        identifier(request_id)
        snapshot=self.task.snapshot();phase=snapshot['state']['phase']
        require(phase in {'paused_provider','paused_quota'},'continuation_wake_refused')
        binding=self._wake_binding(snapshot)
        observation=observe_provider_availability(self.executable,binding_sha256=binding,timeout=30)
        candidate=classify_provider_availability(observation,binding_sha256=binding,enabled=True)
        if candidate['classification']=='provider_unavailable':
            return {'status':phase,'retry_after_seconds':self.backoff_seconds,'snapshot':snapshot,'launchable':False}
        require(candidate['classification']=='available','continuation_provider_unconfirmed')
        self._wake=(observation,binding,snapshot['state_sha256'])
        try:return self.run_cycle(request_id=request_id,turns=turns,complete=complete)
        finally:self._wake=None

    def run_cycle(self,*,request_id,turns=1,complete=False):
        self._owner()
        identifier(request_id)
        require(type(turns) is int and 1<=turns<=min(CONTINUATION_MAX_TURNS,self.task.configuration['cycle_limits']['max_turns'])
            and type(complete) is bool,'continuation_cycle_limit')
        require(self.mutex.acquire(blocking=False),'continuation_busy')
        used=dict.fromkeys(USAGE_FIELDS,0);owned_attempt=None
        try:
            before=self.task.snapshot();phase=before['state']['phase']
            request=self._request(request_id,before,turns,complete)
            replay=self.task._lookup(request['expected_revision'],request_id,{'kind':'start','expected_revision':request['expected_revision']})
            if replay is not None:return {'status':'historical',**replay}
            require(phase not in {'paused_provider','paused_quota'} or self._wake is not None,'continuation_wake_required')
            start=self.task.start_cycle(expected_revision=request['expected_revision'],request_id=request_id)
            if start.permit is None:return {'status':'historical' if start.receipt['historical'] else 'needs_you',**start.receipt}
            context=start.permit.context;work_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'continuation-work:'+context.attempt_id))
            owned_attempt=context.attempt_id
            with start.permit.use():
                guard=self.runtime_factory(context)
                require(type(guard) is RuntimeGuard and type(guard.journal) is RuntimeJournal
                    and guard.scope['board_session_id']==context.attempt_id
                    and all(guard.scope[key]==context.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint')),
                    'continuation_scope_changed')
                scope_sha=digest(canonical(guard.scope));identity={key:guard.scope[key] for key in ('board_endpoint','board_session_id','agent_id')}
                self._binding={'guard':guard,'work_id':work_id,'context':context}
                self._record('attempt-'+context.attempt_id+'.json',{'schema':'continuation-runner-attempt-v1',
                    'task_id':context.logical_task_id,'generation_id':context.generation_id,'cycle_id':context.cycle_id,
                    'attempt_id':context.attempt_id,'work_id':work_id,'scope_sha256':scope_sha,
                    'board_session_id':guard.scope['board_session_id'],'core_state_sha256':context.state_sha256,
                    'configuration_sha256':self.configuration_sha256})
                cycle=start.receipt['snapshot']['state']['current_cycle']
                deadline=cycle['started_at']+self.task.configuration['cycle_limits']['max_wall_seconds']
                require(self.task._time(cycle)<deadline and not self._stop(),'continuation_cycle_limit')
                self.task.checks.verify(context)
                work=self.store.create_work(owner_id=guard.scope['owner_id'],agent_id='board-agent-'+digest(canonical(identity)),
                    project_id=guard.scope['project_id'],ticket_id=guard.scope['ticket_id'],workspace=self.workspace,
                    executable=self.executable,model=self.model,work_id=work_id)
                with self.store.work_lock(work_id):
                    require(self.store.status(work_id)['turns']==[] and work['provider_session_id'] is None)
                    self.store.enroll_continuation_work_v1(work_id,task_id=context.logical_task_id,generation_id=context.generation_id,
                        cycle_id=context.cycle_id,attempt_id=context.attempt_id,scope_sha256=scope_sha)
                runner=PersonalRunner(self.store,runtime_guard=guard)
                sealed=[]
                for index in range(turns):
                    require(not self._stop(),'continuation_stopped')
                    self.task.checks.verify(context)
                    remaining=deadline-self.task._time(cycle)
                    require(remaining>0,'continuation_cycle_limit')
                    key='continuation-'+context.attempt_id+':'+str(index)
                    def on_receipt(receipt):
                        self.store._require_lock(work_id)
                        raw=json.loads(receipt.metadata_json);turn_id=raw['turn_id']
                        metadata=consume_provider_receipt(receipt,task_id=context.logical_task_id,generation_id=context.generation_id,
                            cycle_id=context.cycle_id,attempt_id=context.attempt_id,work_id=work_id,turn_id=turn_id,request_id=key,
                            scope_sha256=scope_sha,result_sha256=digest(canonical(self.store.turn_result(turn_id))),
                            work_state_sha256=self.store.continuation_state_sha256(work_id))
                        provider={'receipt':receipt,'metadata':metadata,'receipt_sha256':digest(receipt.metadata_json.encode())}
                        sealed.append(self._seal(context,work,used,'provider',provider=provider))
                    attempt=enroll_provider_attempt(task_id=context.logical_task_id,generation_id=context.generation_id,
                        cycle_id=context.cycle_id,attempt_id=context.attempt_id,work_id=work_id,request_id=key,
                        scope_sha256=scope_sha,on_receipt=on_receipt)
                    def before_prompt():
                        self._before_prompt(context,guard)
                        require(self.task._time(cycle)<deadline,'continuation_cycle_limit')
                    prompt=self.prompt_factory(context,index)
                    require(not self._stop(),'continuation_stopped')
                    remaining=deadline-self.task._time(cycle)
                    require(remaining>0,'continuation_cycle_limit')
                    used['turns']+=1
                    response=runner.run(work_id,key,prompt,timeout=min(120,remaining),
                        should_cancel=self._stop,before_prompt=before_prompt,provider_attempt=attempt)
                    if sealed:return {'status':'paused_provider',**sealed[0]}
                    require(response['cached'] is False and response['result'].get('status')=='completed','continuation_provider_unconfirmed')
                    with self.store.work_lock(work_id):
                        self._fresh_runtime(guard,self.store.get_work(work_id),response['turn_id'])
                with self.store.work_lock(work_id):result=self._seal(context,work,used,'complete' if complete else 'cycle')
                return {'status':'complete' if complete else 'paused_cycle',**result}
        except Exception:
            try:
                if owned_attempt is not None:self._review(used,owned_attempt)
            except Exception:pass  # Unknown accounting/storage remains review-only.
            raise ContinuationError('continuation_runner_requires_review') from None
        finally:
            self._provider=None;self._binding=None;self.mutex.release()
