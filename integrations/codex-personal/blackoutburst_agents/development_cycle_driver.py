"""Explicit private development generations; no daemon or final publication.

Each generation retains its own immutable budgets, work/session and fence.
A private source seal is historical correlation, never permission to resume.
Unknown capture/release ACKs cannot be escaped with another key or model turn.
"""
from contextlib import closing,contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import threading

from .context import ContextScope,build_context,_hash,_select,_TICKET
from .continuation import ContinuationTask,CycleStart,LiveCyclePermit
from .dev_journal import PrivateDirectory,DevelopmentJournal,decode
from .dev_manifest import DevelopmentError,canonical,digest
from .dev_planner import _directory_pin,_reasoning_configuration
from .dev_runtime import development_work_id
from .development_authorization import DevelopmentAuthorization
from .development_checkpoint_proof import DevelopmentCheckpointRequest
from .private_development_checkpoint import PrivateDevelopmentCheckpointController,checkpoint_id_for
from .runtime_guard import RuntimeGuard
from .git_workspace import _operation


def require(value,code='development_cycle_unconfirmed'):
    if not value:raise DevelopmentError(code)


_EXECUTION_ISSUER=object()


class _DevelopmentCycleExecution:
    """Current-process logical control, never reconstructable from metadata."""
    def __init__(self,issuer,task,worker,operation_id,context):
        require(issuer is _EXECUTION_ISSUER)
        self.task,self.worker,self.operation_id,self.context=task,worker,operation_id,context
        self.pid,self.thread=os.getpid(),threading.current_thread()
    def __reduce_ex__(self,_):raise TypeError('Live development cycles cannot be serialized')
    def __copy__(self):raise TypeError('Live development cycles cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Live development cycles cannot be copied')
    def verify(self,worker,operation_id):
        require(worker is self.worker and operation_id==self.operation_id and self.pid==os.getpid()
            and self.thread is threading.current_thread(),'development_cycle_execution_changed')
        snapshot=self.task.snapshot();state=snapshot['state'];cycle=state['current_cycle']
        require(snapshot['state_sha256']==self.context.state_sha256 and state['phase']=='running'
            and state['stop_requested'] is False,'development_cycle_logical_stop')
        require(self.task._time(cycle)<cycle['started_at']+self.task.configuration['cycle_limits']['max_wall_seconds'],
            'development_cycle_wall_limit')
        # The Runtime callback attests terminal/never-started boundaries. An
        # active model is checked by the unchanged concrete Runtime guards.
        for index in (0,1,2,3,5):
            require(self.task.checks.callbacks[index](self.context) is None,
                'development_cycle_check_result_invalid')
        require(self.task.snapshot()==snapshot,'development_cycle_logical_stop')
        require(self.task._time(cycle)<cycle['started_at']+self.task.configuration['cycle_limits']['max_wall_seconds'],
            'development_cycle_wall_limit')


def check_worker_cycle(worker,operation_id):
    value=getattr(worker,'_checkpoint_execution',None)
    if value is not None:
        require(type(value) is _DevelopmentCycleExecution,'development_cycle_execution_changed')
        value.verify(worker,operation_id)


class _DevelopmentCyclePreparation:
    def __init__(self,issuer,task,preparer,operation_id,snapshot):
        require(issuer is _EXECUTION_ISSUER)
        self.task,self.preparer,self.operation_id,self.snapshot=task,preparer,operation_id,snapshot
        self.pid,self.thread=os.getpid(),threading.current_thread()
    def __reduce_ex__(self,_):raise TypeError('Live development preparation cannot be serialized')
    def __copy__(self):raise TypeError('Live development preparation cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Live development preparation cannot be copied')
    def verify(self,preparer,operation_id,*,fresh=False):
        require(preparer is self.preparer and operation_id==self.operation_id and self.pid==os.getpid()
            and self.thread is threading.current_thread(),'development_cycle_preparation_changed')
        current=self.task.snapshot()
        require(current==self.snapshot and current['state']['phase']=='paused_cycle'
            and current['state']['stop_requested'] is False,'development_cycle_logical_stop')
        if fresh:self.task.checks.verify(self.task._context(current,purpose='start'))
        require(self.task.snapshot()==current,'development_cycle_logical_stop')


def check_preparer_cycle(preparer,operation_id,*,fresh=False):
    value=getattr(preparer,'_checkpoint_preparation',None)
    if value is not None:
        require(type(value) is _DevelopmentCyclePreparation,'development_cycle_preparation_changed')
        value.verify(preparer,operation_id,fresh=fresh)


@contextmanager
def _preparing(task,preparer,operation_id):
    require(getattr(preparer,'_checkpoint_preparation',None) is None,'development_cycle_busy')
    value=_DevelopmentCyclePreparation(_EXECUTION_ISSUER,task,preparer,operation_id,task.snapshot())
    preparer._checkpoint_preparation=value
    try:
        value.verify(preparer,operation_id,fresh=True);yield
    finally:
        require(preparer._checkpoint_preparation is value,'development_cycle_preparation_changed')
        preparer._checkpoint_preparation=None


@contextmanager
def _executing(task,worker,operation_id,context):
    require(getattr(worker,'_checkpoint_execution',None) is None,'development_cycle_busy')
    value=_DevelopmentCycleExecution(_EXECUTION_ISSUER,task,worker,operation_id,context)
    worker._checkpoint_execution=value
    try:
        value.verify(worker,operation_id);yield
    finally:
        require(worker._checkpoint_execution is value,'development_cycle_execution_changed')
        worker._checkpoint_execution=None


def _read(private,name):
    raw=private.read(name,128*1024);value=decode(raw)
    require(canonical(value)==raw)
    return value


def _exists(private,name):
    try:os.stat(name,dir_fd=private.fd,follow_symlinks=False);return True
    except FileNotFoundError:return False


@contextmanager
def _driver_lock(private,operation_id):
    """Separate controller lock; identity cleanup owns its WorkJournal lock."""
    name='development-cycle-lock-'+operation_id
    try:descriptor=os.open(name,os.O_RDWR|os.O_NOFOLLOW|os.O_CREAT|os.O_EXCL,0o600,dir_fd=private.fd)
    except FileExistsError:descriptor=os.open(name,os.O_RDWR|os.O_NOFOLLOW,dir_fd=private.fd)
    try:
        private.validate_file(descriptor);before=os.fstat(descriptor)
        try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise DevelopmentError('development_cycle_busy') from None
        require(os.stat(name,dir_fd=private.fd,follow_symlinks=False)==before)
        yield
        private.verify();private.validate_file(descriptor)
        require(os.fstat(descriptor)==before and os.stat(name,dir_fd=private.fd,follow_symlinks=False)==before)
    finally:os.close(descriptor)


def _request_metadata(request):
    values={key:getattr(request,key) for key in ('task_id','generation_id','cycle_id','attempt_id',
        'original_public_base','source_release_sha256','parent_checkpoint_sha256','after_actions')}
    for key in ('generation_id','cycle_id','attempt_id'):_operation(values[key])
    require(len({values[key] for key in ('generation_id','cycle_id','attempt_id')})==3
        and type(request.after_actions) is int and 1<=request.after_actions<=64)
    canonical(values)
    return values


def _test_caps(manifest,plan=None):
    if plan is None:return {'tests':deepcopy(manifest['tests']),'profile':'legacy-named-test'}
    from .linux_test_protocol import linux_execution_backend_id
    require(all(row['backend_id']==linux_execution_backend_id(digest(canonical(plan))) for row in manifest['tests']))
    suites=[]
    for row in manifest['tests']:
        script=plan['input_plan']['scripts'][row['name']]
        require(all(script[key]==row[key] for key in ('timeout_seconds','max_output_bytes')))
        suites.append({'name':row['name'],'script_sha256':script['sha256'],
            'timeout_seconds':row['timeout_seconds'],'max_output_bytes':row['max_output_bytes']})
    return {'tests':sorted(suites,key=lambda row:row['name']),'imports':plan['imports'],'limits':plan['limits']}


def validate_worker_checkpoint_caps(op,manifest,backend,controller):
    """After claim and before Planner/model: compare the effective plan."""
    plan=None
    if manifest._linux_execution_profile:
        from .linux_test_backend import SupervisedLinuxNamedPythonBackend
        require(type(backend) is SupervisedLinuxNamedPythonBackend)
        backend._pins();plan=backend.plan
    m=manifest.manifest;tests=_test_caps(m,plan)
    caps={'paths':sorted(m['files']),'tests':tests,'actions':m['actions'],'resource':m['resource'],
        'broker_limits':m['budgets'],'planner_limits':op['payload']['planner_budgets']}
    require(digest(canonical(tests))==controller.configuration['global_test_caps_sha256']
        and digest(canonical(caps))==controller.configuration['static_caps_sha256'],
        'development_cycle_caps_changed')


def _validate_next_inputs(op,parameters,observation,*,preparer=None,operation_id=None):
    """Static approved inputs only; no backend construction or qualification."""
    from . import linux_pool_preparation as preparation
    from .development_pool_selection import file_document
    fields={'project_id','ticket_id','paths','resource','actions','verifier_path','verifier_sha256','test_name',
        'test_timeout_seconds','test_max_output_bytes','executable','cli_version','interpreter','unshare',
        'instruction','model','planner_budgets','broker_budgets'}
    require(set(parameters) in (fields,fields|{'reasoning_effort'}),'development_cycle_parameters_changed')
    raw,_=file_document(Path(op['configuration']['pins']['backend']['path'])/'execution-plan.json',512*1024)
    old=decode(raw);require(digest(canonical(old))==op['configuration']['linux_execution_plan_sha256'])
    plan=old
    if preparer is not None and operation_id is not None:
        with preparer._locked(operation_id) as (private,_,held):
            require(not _exists(private,'failed.json'),'development_cycle_preparation_uncertain')
            intent=preparer._read(private,'intent.json');complete=preparer._read(private,'complete.json');held()
            preparer._complete(complete,intent['request_sha256'],operation_id,intent['request']['job']['session_id'])
            for path,pin in complete['files'].items():require(file_document(path,512*1024)[1]==pin)
            plan=decode(file_document(private.path/'backend'/'execution-plan.json',512*1024)[0])
    with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
        m=manifest.manifest;tests=_test_caps(m,old)
    require(digest(canonical(tests))==observation['test_caps_sha256'] and len(tests['tests'])==1)
    test=tests['tests'][0]
    verifier,_=preparation.read_public(parameters['verifier_path'],maximum=65536)
    require(digest(verifier)==parameters['verifier_sha256']==test['script_sha256']
        and parameters['test_name']==test['name']
        and parameters['test_timeout_seconds']==test['timeout_seconds']
        and parameters['test_max_output_bytes']==test['max_output_bytes']
        and parameters['instruction']==op['configuration']['instruction'],
        'development_cycle_caps_changed')
    for key in ('interpreter','unshare'):
        require(parameters[key]==old[key],'development_cycle_tools_changed')
    from .linux_planner_policy import LinuxPlannerPolicy
    with LinuxPlannerPolicy(op['configuration']['pins']['planner_policy']['path']) as policy:
        policy.validate_for(policy.executable,policy.workspace)
        require(policy.fingerprint==op['configuration']['policy_hash']
            and parameters['executable']==policy.executable and parameters['cli_version']==policy._plan['cli_version'],
            'development_cycle_tools_changed')
    if plan!=old:
        for key in ('environment','sources','interpreter','interpreter_pin','unshare','unshare_pin',
                    'runtime','bootstrap_sha256','seccomp_sha256','imports','limits'):
            require(plan[key]==old[key],'development_cycle_caps_changed')
        script=plan['input_plan']['scripts'][test['name']]
        require(set(plan['input_plan']['scripts'])=={test['name']} and script['sha256']==test['script_sha256']
            and all(script[key]==test[key] for key in ('timeout_seconds','max_output_bytes')),
            'development_cycle_caps_changed')
        with closing(DevelopmentJournal(Path(preparer.directory)/operation_id/'broker')) as journal:
            new=journal.manifest
            require(digest(canonical(_test_caps(new,plan)))==observation['test_caps_sha256']
                and new['actions']==m['actions'] and new['budgets']==m['budgets']
                and new['resource']==m['resource'] and sorted(new['files'])==sorted(m['files']),
                'development_cycle_caps_changed')


def run_worker_checkpoint(worker,operation_id,*,request,checkpoint_controller,parent_checkpoint_id=None):
    """One immutable opt-in intent before the normal Worker admission path."""
    from .development_worker import DevelopmentBoardWorker
    require(type(worker) is DevelopmentBoardWorker and type(request) is DevelopmentCheckpointRequest
        and request.worker is worker and type(checkpoint_controller) is PrivateDevelopmentCheckpointController,
        'development_cycle_concrete_handles_required')
    _operation(operation_id);controller=checkpoint_controller
    controller._roots()  # Configuration/hash/root drift refuses before a model.
    op=worker.journal.get(operation_id);worker._pin_controller(op)
    require(op['stage'] not in ('needs_review','complete','abandoned','result_ready','publishing')
        and op['configuration'].get('controller_scope') is None,'development_cycle_stage_refused')
    params=_request_metadata(request);config=controller.configuration
    require(all(params[key]==config[key] for key in ('task_id','original_public_base','source_release_sha256'))
        and all(op['scope'][key]==config[key] for key in ('owner_id','project_id','ticket_id')))
    parent=None if parent_checkpoint_id is None else controller.verify(parent_checkpoint_id)
    require(request.parent_checkpoint_sha256==(None if parent is None else parent['checkpoint_sha256']))
    if parent is not None:
        require(parent['operation_id']!=operation_id
            and controller.workspaces.grant['repository']==parent['checkout']
            and controller.workspaces.grant['base_commit']==parent['commit_sha'],
            'development_cycle_parent_changed')
        with controller._record(parent_checkpoint_id) as (private,_):
            previous=controller._read(private)['intent']['proof']
        require(op['scope']['board_session_id']!=previous['scope']['session_id']
            and all(params[key]!=previous[key] for key in ('generation_id','cycle_id','attempt_id')),
            'development_cycle_identity_reused')
    with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
        require(manifest.manifest['resource']==config['resource']
            and sorted(manifest.manifest['files'])==config['global_paths']
            and manifest.manifest['base_sha']==controller.workspaces.grant['base_commit'])
    intent={'schema':'development-worker-checkpoint-intent-v1','operation_id':operation_id,
        'request':params,'controller_sha256':controller.configuration_sha256,
        'controller_root':str(controller.root),'controller_pin':controller.pin,
        'parent_checkpoint_id':parent_checkpoint_id,'checkpoint_id':checkpoint_id_for(operation_id)}
    name='development-checkpoint-intent-'+operation_id+'.json'
    with PrivateDirectory(worker.journal.directory) as private:
        if _exists(private,name):
            require(_read(private,name)==intent,'development_cycle_request_conflict')
            capture=op['payload'].get('checkpoint_capture')
            require(type(capture) is dict,'development_cycle_capture_uncertain')
            sealed=controller.verify(intent['checkpoint_id'])
            require({**capture['checkpoint'],'historical':True}==sealed)
            return {**worker._public(op),'status':'checkpoint_ready','checkpoint':sealed,
                    'historical':True,'launchable':False}
        require('checkpoint_capture' not in op['payload'],'development_cycle_request_conflict')
        private.create(name,canonical(intent))
    return worker._run(operation_id,checkpoint=(request,controller,parent_checkpoint_id))


class DevelopmentCycleDriver:
    """An explicit logical controller over concrete, independently prepared jobs.

    Original ContinuationTask checks/clock are retained. Only its real live
    permit admits execution. Retiring an operation never clears logical Stop.
    The supplied next preparer is trusted local code, never reconstructed from
    a checkpoint or a Board message, and still performs normal fresh checks.
    """
    def __init__(self,task,worker,checkpoints):
        from .development_worker import DevelopmentBoardWorker
        require(type(task) is ContinuationTask and type(worker) is DevelopmentBoardWorker
            and type(checkpoints) is PrivateDevelopmentCheckpointController,
            'development_cycle_concrete_handles_required')
        self.task,self.worker,self.checkpoints=task,worker,checkpoints
        config=checkpoints.configuration;scope=task.configuration['scope']
        require(task.configuration['logical_task_id']==config['task_id']
            and all(scope[key]==config[key] for key in ('owner_id','project_id','ticket_id','resource','source_release_sha256')))

    def _logical(self,*,phase=None,fresh=False):
        state=self.task.snapshot()
        require(state['state']['stop_requested'] is False and state['state']['phase'] not in ('stopped','needs_you','complete'),
                'development_cycle_logical_stop')
        if phase is not None:require(state['state']['phase']==phase,'development_cycle_logical_state_changed')
        if fresh:
            self.task.checks.verify(self.task._context(state,purpose='start'))
            require(self.task.snapshot()==state,'development_cycle_logical_state_changed')
        return state

    def _operation(self,operation_id):
        _operation(operation_id);op=self.worker.journal.get(operation_id);self.worker._pin_controller(op)
        config=op['configuration'];c=self.checkpoints.configuration;s=self.task.configuration['scope']
        require(config.get('controller_scope') is None and op['stage']=='running'
            and all(op['scope'][key]==s[key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint'))
            and _directory_pin(self.worker.journal.directory)==config['pins']['journal'])
        for pin in config['pins'].values():require(_directory_pin(pin['path'])==pin)
        capture=op['payload'].get('checkpoint_capture')
        require(type(capture) is dict and set(capture)=={'status','checkpoint','observation','previous_revision','previous_updated_at'}
            and capture['status']=='checkpoint_ready')
        observation=capture['observation'];self.checkpoints._proof_caps(observation)
        before=deepcopy(op);before['payload'].pop('checkpoint_capture')
        before.update(revision=capture['previous_revision'],updated_at=capture['previous_updated_at'])
        require(type(capture['previous_revision']) is int and op['revision']==capture['previous_revision']+1
            and digest(canonical(before))==observation['worker_operation_sha256'],
            'development_cycle_operation_changed')
        require(observation['operation_id']==operation_id and observation['scope']['device_id']==s['device_id']
            and observation['scope']['profile_version']==s['profile_version']
            and observation['source_release_sha256']==s['source_release_sha256'])
        cp=self.checkpoints.verify(checkpoint_id_for(operation_id))
        require({**capture['checkpoint'],'historical':True}==cp
            and cp['proof_sha256']==digest(canonical(observation)))
        # Stop has its own immutable files. It does not alter these receipts.
        with PrivateDirectory(config['pins']['broker']['path']) as private:
            planner_raw=private.read('planner.json',2*1024*1024)
            require(digest(planner_raw)==observation['planner_document_sha256']
                and digest(private.read('state.json',2*1024*1024))==observation['broker_document_sha256'])
        from .development_checkpoint_proof import _read_work
        with _read_work(self.worker.store,development_work_id(op['id'])):
            stored=self.worker.store.status(development_work_id(op['id']))
            require(digest(canonical(stored))==observation['work_state_sha256']
                and digest(canonical({turn['id']:digest(canonical(self.worker.store.turn_result(turn['id'])))
                    for turn in stored['turns']}))==observation['model_results_sha256'],
                'development_cycle_work_changed')
        from .dev_fence import DevelopmentFence
        binding={**deepcopy(op['payload']['parent_binding']),
            'configuration_hash':digest(canonical(decode(planner_raw)['configuration']))}
        fence=DevelopmentFence(_test_anchor=self.worker.test_anchor)
        try:fence.verify(binding,op['id'])
        finally:fence.close()
        return op,cp,observation

    def _cleanup(self,op):
        result=self.worker.reconcile_cleanup(op['id'])
        require(result.get('operation_id')==op['id'] and result.get('cleanup_confirmed') is True,
                'development_cycle_cleanup_unconfirmed')
        rows=self.worker.runtime.list_status();require(0<len(rows)<=128,'development_cycle_cleanup_unconfirmed')
        hashes=[]
        for row in rows:
            record=self.worker.runtime.get(row['id']);configuration=record['configuration']
            require(configuration['scope']==op['scope'] and configuration['work_id']==development_work_id(op['id'])
                and record['phase']=='finished' and type(record['receipt']) is dict
                and record['receipt'].get('cleanup_confirmed') is True,'development_cycle_cleanup_unconfirmed')
            guard=RuntimeGuard(self.worker.runtime,self.worker.control,op['scope']);guard.operation=record
            found=self.worker.control.call('lookup_execution',{'idempotency_key':record['key']})
            require(type(found) is dict and found.get('found') is True,'development_cycle_cleanup_unconfirmed')
            receipt=guard.metadata(found.get('execution'))
            require(receipt==record['receipt'] and receipt['state']=='finished' and receipt['cleanup_confirmed'] is True
                and receipt['device_id']==op['payload']['registration']['identity']['device_id']
                and receipt['profile_version']==op['payload']['registration']['profile_version'],
                'development_cycle_cleanup_unconfirmed')
            hashes.append(digest(canonical(record)))
        cleanup=digest(canonical(sorted(hashes)))
        require(cleanup==op['payload']['checkpoint_capture']['observation']['runtime_cleanup_sha256'],
            'development_cycle_cleanup_unconfirmed')
        return cleanup

    def _authorization(self,op,*,stopped=False):
        with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
            # stopped=True is waiting-state identity/policy maintenance only.
            # This object is never passed to a planner/backend or allowed().
            auth=DevelopmentAuthorization(self.worker.client,scope=op['scope'],manifest=manifest.manifest,
                registration=op['payload']['registration'],clock=self.worker.clock,
                stop_requested=None if stopped else lambda:self.worker._controller_cancelled(op['id']))
        auth.expected_context=deepcopy(op['payload']['context']);auth.claim_version=op['payload']['claim_version']
        auth.claim_id=op['payload']['claim_id']
        return auth

    def _cycle_caps(self,operation_id,after_actions):
        op=self.worker.journal.get(operation_id);limits=self.task.configuration['cycle_limits']
        planner=op['configuration']['budgets']
        require(type(after_actions) is int and 1<=after_actions<=min(limits['max_turns'],limits['max_actions'])
            and all(planner['max_'+key]<=limits['max_'+key] for key in ('turns','actions','wall_seconds')),
            'development_cycle_caps_changed')
        with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
            require(all(manifest.manifest['budgets']['max_'+key]<=limits['max_'+key]
                for key in ('operations','tests','read_bytes','patch_bytes')),'development_cycle_caps_changed')

    def _mark_review(self,operation_id,context):
        """Record a failed own attempt, never infer a clean boundary/Stop."""
        snapshot=self.task.snapshot();state=snapshot['state']
        if snapshot['state_sha256']!=context.state_sha256 or state['phase']!='running' or state['stop_requested']:
            return state['phase']
        op=self.worker.journal.get(operation_id);pins=op['configuration']['pins'];used=None
        with closing(DevelopmentJournal(pins['broker']['path'])) as broker:
            value=decode(broker.private.read('state.json',2*1024*1024));broker.validate_state(value)
            used={key:value['used'][key] for key in ('operations','tests','read_bytes','patch_bytes')}
            if _exists(broker.private,'planner.json'):
                raw=decode(broker.private.read('planner.json',2*1024*1024))
                from .dev_planner_journal import PlannerJournal
                validator=object.__new__(PlannerJournal);validator.operation_id=operation_id
                validator.configuration=raw['configuration'];validator.fingerprint=digest(canonical(validator.configuration))
                validator.validate(raw)
                require(raw['configuration']['scope']==op['scope']
                    and raw['configuration']['work_id']==development_work_id(operation_id)
                    and raw['configuration']['manifest_hash']==op['configuration']['manifest_hash'])
                used.update({key:raw['used'][key] for key in ('turns','actions')})
            else:
                rows=self.worker.store._db.execute('SELECT id FROM turns WHERE work_id=? LIMIT 1',
                    (development_work_id(operation_id),)).fetchall()
                require(not rows and not value['operations'],'development_cycle_usage_unknown')
                used.update(turns=0,actions=0)
        checkpoint=state['checkpoint'] or {'schema':1,'source_sha256':self.task.configuration['initial_source_sha256'],
            'diff_sha256':digest(b'no-accepted-development-checkpoint'),
            'plan_sha256':digest(canonical(op['configuration'])),'test_receipts':[],'provider_receipt':None}
        try:
            result=self.task.checkpoint(expected_revision=state['revision'],request_id='development-cycle-review-'+operation_id,
                checkpoint=checkpoint,cycle_used=used,reason='needs_you')
            return result['snapshot']['state']['phase']
        except Exception:
            current=self.task.snapshot()
            if current['state_sha256']!=snapshot['state_sha256']:return current['state']['phase']
            raise

    def run_checkpoint(self,operation_id,*,start,parent_checkpoint_id=None,after_actions=1):
        require(type(start) is CycleStart and type(start.permit) is LiveCyclePermit
            and start.permit.task is self.task,'development_cycle_live_permit_required')
        _operation(operation_id)
        try:
            return self._run_checkpoint(operation_id,start=start,parent_checkpoint_id=parent_checkpoint_id,
                after_actions=after_actions)
        except Exception:
            # Only the exact still-current attempt can be marked. Stop and a
            # concurrent state transition win; no exception text is retained.
            phase=self._mark_review(operation_id,start.permit.context)
            return {'operation_id':operation_id,'status':phase,'reason':'development_cycle_requires_review',
                'launchable':False}

    def _run_checkpoint(self,operation_id,*,start,parent_checkpoint_id=None,after_actions=1):
        self._logical(phase='running')
        with start.permit.use() as context:
            require(context.scope==self.task.configuration['scope'])
            self._cycle_caps(operation_id,after_actions)
            previous=self.task.snapshot()['state']['checkpoint']
            parent=None if parent_checkpoint_id is None else self.checkpoints.verify(parent_checkpoint_id)
            require((previous is None and parent is None) or
                (previous is not None and parent is not None and previous['diff_sha256']==parent['checkpoint_sha256']),
                'development_cycle_parent_changed')
            request=DevelopmentCheckpointRequest(self.worker,context.logical_task_id,context.generation_id,
                context.cycle_id,context.attempt_id,self.checkpoints.configuration['original_public_base'],
                self.checkpoints.configuration['source_release_sha256'],None if parent is None else
                parent['checkpoint_sha256'],after_actions)
            with _executing(self.task,self.worker,operation_id,context):
                result=self.worker.run_checkpoint(operation_id,request=request,checkpoint_controller=self.checkpoints,
                    parent_checkpoint_id=parent_checkpoint_id)
            if result['status']!='checkpoint_ready':
                if result['status']=='needs_review':
                    return {**result,'status':self._mark_review(operation_id,context),'worker_status':'needs_review'}
                return result
            op,cp,observation=self._operation(operation_id)
            require(all(observation[key]==getattr(context,key) for key in ('generation_id','cycle_id','attempt_id')))
            self._authorization(op).snapshot(require_claim=True);self._cleanup(op)
            checkpoint={'schema':1,'source_sha256':observation['source_snapshot_sha256'],
                'diff_sha256':cp['checkpoint_sha256'],'plan_sha256':observation['configuration_sha256'],
                'test_receipts':[],'provider_receipt':None}
            used={**{key:observation['used_planner'][key] for key in ('turns','actions')},
                **{key:observation['used_broker'][key] for key in ('operations','tests','read_bytes','patch_bytes')}}
            snapshot=self._logical(phase='running');revision=snapshot['state']['revision']
            boundary=self.task.clean_boundary(expected_revision=revision,checkpoint=checkpoint,cycle_used=used,reason='cycle')
            self._operation(operation_id);self._cleanup(op)
            event=self.task.checkpoint(expected_revision=revision,request_id='development-cycle-'+operation_id,
                checkpoint=checkpoint,cycle_used=used,reason='cycle',boundary=boundary)
            phase=event['snapshot']['state']['phase']
            return {**result,'status':phase,'logical_checkpoint_sha256':event['snapshot']['state']['checkpoint_sha256']}

    def _retirement(self,private,op):
        name='development-cycle-retirement-'+op['id']+'.json'
        if not _exists(private,name):return name,None
        value=_read(private,name);cp=self.checkpoints.verify(checkpoint_id_for(op['id']))
        request=self._retirement_request(op,cp)
        require(type(value) is dict and set(value)=={'schema','phase','request','request_sha256','cleanup_sha256','release'}
            and value['schema']=='development-cycle-retirement-v1' and value['phase'] in ('stopping','releasing','retired')
            and value['request']==request and value['request_sha256']==digest(canonical(request))
            and value['cleanup_sha256']==(None if value['phase']=='stopping' else
                op['payload']['checkpoint_capture']['observation']['runtime_cleanup_sha256'])
            and value['release']==(None if value['phase']!='retired' else {'ticket_id':op['scope']['ticket_id'],
                'version':request['expected_version']+1,'status':'todo','claimant':None}),
            'development_cycle_retirement_changed')
        return name,value

    @staticmethod
    def _retirement_request(op,cp):
        return {'operation_id':op['id'],'operation_sha256':digest(canonical(op)),
            'scope':deepcopy(op['scope']),'checkpoint':cp,'context_sources':deepcopy(op['payload']['context']['sources']),
            'expected_version':op['payload']['claim_version'],'claim_id':op['payload']['claim_id'],
            'ticket_sha256':_hash(_select(op['payload']['claimed_ticket'],_TICKET)),
            'idempotency_key':'development-checkpoint-release-'+op['id'],
            'reason':'Private source checkpoint '+cp['checkpoint_sha256']+' retained; clean generation retired.'}

    def _fresh_release(self,op,request,*,allow_post):
        current,cp,_=self._operation(op['id'])
        require(current==op and digest(canonical(op))==request['operation_sha256']
            and cp==request['checkpoint'] and self.worker._stopped(op['id']),
            'development_cycle_retirement_changed')
        self.worker._local_control(op['id'],inspection=True)
        auth=self._authorization(op,stopped=True);auth._registration_identity();auth._policy()
        observed=[]
        def call(name,args):
            result=self.worker.client.call(name,args)
            if name=='get_ticket':observed.append(deepcopy(result))
            return result
        scope=ContextScope(**{key:op['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id')})
        context=build_context(call,scope=scope,registration=op['payload']['registration'])
        require(len(observed)==2)
        states=[]
        for response in observed:
            auth._ticket_fields(response);ticket=response['ticket'];selected=_select(ticket,_TICKET)
            post=(ticket['version']==request['expected_version']+1 and ticket['claimant'] is None
                and ticket['lease_expires_at'] is None and ticket['status']=='todo'
                and ticket['assignee_agent_id']==op['scope']['agent_id'])
            if post:
                require(allow_post);selected.update(version=request['expected_version'],status='in_progress',
                    assignee_agent_id=op['payload']['claimed_ticket']['assignee_agent_id'])
            else:
                require(ticket['version']==request['expected_version'] and auth.own_claim(ticket)
                    and auth.claim_identity(ticket)==request['claim_id'],'development_cycle_claim_changed')
            require(_hash(selected)==request['ticket_sha256'],'development_cycle_claim_changed')
            states.append(post)
        require(states[0]==states[1],'development_cycle_claim_changed')
        sources=deepcopy(context['sources']);sources['ticket']=request['context_sources']['ticket']
        require(sources==request['context_sources'],'development_cycle_context_changed')
        auth._policy();auth._registration_identity();self._logical(phase='paused_cycle')
        return states[0]

    def retire_checkpoint(self,operation_id):
        self._logical(phase='paused_cycle');op,cp,observation=self._operation(operation_id)
        require(self.task.snapshot()['state']['checkpoint']['diff_sha256']==cp['checkpoint_sha256'])
        self.worker.client.adopt_registration()
        with PrivateDirectory(self.worker.journal.directory) as private,_driver_lock(private,operation_id):
            op,cp,observation=self._operation(operation_id);name,value=self._retirement(private,op)
            if value is None:
                require(not self.worker._stopped(operation_id),'development_cycle_stop_without_retirement')
                self._authorization(op).snapshot(require_claim=True)
                request=self._retirement_request(op,cp)
                value={'schema':'development-cycle-retirement-v1','phase':'stopping','request':request,
                    'request_sha256':digest(canonical(request)),'cleanup_sha256':None,'release':None}
                private.create(name,canonical(value))
            require(self._retirement(private,op)[1]==value,'development_cycle_retirement_changed')
            request=value['request']
            if value['phase']=='retired':
                return {**deepcopy(value),'historical':True,'launchable':False}
            require(value['phase'] in ('stopping','releasing'))
            self.worker.stop(operation_id)
            cleanup=self._cleanup(op);self._fresh_release(op,request,allow_post=value['phase']=='releasing')
            if value['phase']=='stopping':
                value={**value,'phase':'releasing','cleanup_sha256':cleanup};private.replace(name,canonical(value))
            require(_read(private,name)==value,'development_cycle_retirement_changed')
            self._cleanup(op);self._fresh_release(op,request,allow_post=True)
            try:
                response=self.worker.client.release_checkpoint_claim(ticket_id=op['scope']['ticket_id'],
                    expected_version=request['expected_version'],idempotency_key=request['idempotency_key'],reason=request['reason'])
            except Exception:raise DevelopmentError('development_cycle_release_uncertain') from None
            require(type(response) is dict and response.get('version')==request['expected_version']+1
                and response.get('claimant') is None and response.get('status')=='todo'
                and response.get('assignee_agent_id')==op['scope']['agent_id'],'development_cycle_release_uncertain')
            require(self._fresh_release(op,request,allow_post=True),'development_cycle_release_uncertain')
            self._cleanup(op);require(_read(private,name)==value,'development_cycle_retirement_changed')
            value={**value,'phase':'retired','release':{'ticket_id':op['scope']['ticket_id'],
                'version':request['expected_version']+1,'status':'todo','claimant':None}}
            private.replace(name,canonical(value))
            return {**deepcopy(value),'historical':False,'launchable':False}

    def prepare_next(self,operation_id,*,preparer,next_operation_id,parameters):
        """Prepare only; no dispatch, model, claim or old generation replay."""
        from .linux_pool_preparation import LinuxPoolJobPreparer
        require(type(preparer) is LinuxPoolJobPreparer and type(parameters) is dict,
            'development_cycle_concrete_handles_required')
        current=self._logical(phase='paused_cycle',fresh=True);op,cp,observation=self._operation(operation_id)
        require(current['state']['checkpoint']['diff_sha256']==cp['checkpoint_sha256'],
            'development_cycle_parent_changed')
        with PrivateDirectory(self.worker.journal.directory) as private:
            _,value=self._retirement(private,op)
            require(value is not None and value['phase']=='retired','development_cycle_retirement_required')
            require(self._fresh_release(op,value['request'],allow_post=True),'development_cycle_release_uncertain')
            self._cleanup(op)
        _operation(next_operation_id);require(next_operation_id!=operation_id,'development_cycle_identity_reused')
        grant=preparer.workspaces.grant;config=self.checkpoints.configuration
        require(grant['owner_id']==config['owner_id'] and grant['repository_id']==config['repository_id']
            and grant['repository']==cp['checkout'] and grant['base_commit']==cp['commit_sha'],
            'development_cycle_parent_changed')
        _,job=preparer._job(next_operation_id,op['scope']['project_id'],op['scope']['ticket_id'])
        require(job['session_id']!=op['scope']['board_session_id']
            and preparer.fixed=={key:op['scope'][key] for key in preparer.fixed},'development_cycle_identity_reused')
        roots=[Path(preparer.directory),preparer.workspaces.state_root,preparer.workspaces.workspace_root,
               Path(preparer.pool.journal.private.path),Path(preparer.driver.selections.directory)]
        old=[Path(pin['path']) for pin in op['configuration']['pins'].values()]+[Path(cp['checkout'])]
        require(all(not a.is_relative_to(b) and not b.is_relative_to(a) for a in roots for b in old),
            'development_cycle_roots_reused')
        with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as manifest:
            m=manifest.manifest
            require(parameters.get('project_id')==op['scope']['project_id'] and parameters.get('ticket_id')==op['scope']['ticket_id']
                and parameters.get('paths')==config['global_paths'] and parameters.get('resource')==m['resource']
                and sorted(parameters.get('actions',[]))==sorted(m['actions'])
                and parameters.get('model')==op['configuration']['model']
                and _reasoning_configuration(parameters.get('reasoning_effort'))==
                    _reasoning_configuration(op['configuration'].get('reasoning_effort'))
                and parameters['planner_budgets'].document()==op['payload']['planner_budgets']
                and parameters['broker_budgets'].document()==m['budgets'],'development_cycle_caps_changed')
            require(op['configuration'].get('execution_profile')=='linux-python-execution-v1',
                'development_cycle_linux_preparation_required')
        _validate_next_inputs(op,parameters,observation)
        self._logical(phase='paused_cycle',fresh=True)
        metadata={key:(value.document() if key in ('planner_budgets','broker_budgets') else deepcopy(value))
                  for key,value in parameters.items()}
        request={'operation_id':operation_id,'checkpoint_sha256':cp['checkpoint_sha256'],
            'next_operation_id':next_operation_id,'next_session_id':job['session_id'],
            'preparer':preparer.pin,'git_pins':preparer.git_pins,'connection':preparer.connection,
            'parameters_sha256':digest(canonical(metadata))}
        name='development-cycle-next-'+operation_id+'.json'
        with PrivateDirectory(self.worker.journal.directory) as private:
            if _exists(private,name):
                prior=_read(private,name)
                require(prior.get('request')==request,'development_cycle_preparation_conflict')
                require(prior.get('phase')=='complete','development_cycle_preparation_uncertain')
                _validate_next_inputs(op,parameters,observation,preparer=preparer,operation_id=next_operation_id)
                return {**prior['result'],'historical':True,'launchable':False}
            private.create(name,canonical({'schema':'development-cycle-next-v1','phase':'preparing',
                'request':request,'result':None}))
        with _preparing(self.task,preparer,next_operation_id):
            result=preparer.prepare(next_operation_id,**parameters)
        self._logical(phase='paused_cycle',fresh=True)
        self._operation(operation_id);self._cleanup(op)
        require(self._fresh_release(op,value['request'],allow_post=True),'development_cycle_release_uncertain')
        # The preparer's own immutable request and file pins are re-read.
        status=preparer.status(next_operation_id)
        require(result['operation_id']==next_operation_id and result['session_id']==job['session_id']
            and status['status']=='prepared' and 'failure' not in status,'development_cycle_preparation_unconfirmed')
        _validate_next_inputs(op,parameters,observation,preparer=preparer,operation_id=next_operation_id)
        result={**result,'parent_checkpoint_id':cp['checkpoint_id'],'parent_checkpoint_sha256':cp['checkpoint_sha256'],
            'original_public_base':cp['original_public_base'],'launchable':False}
        with PrivateDirectory(self.worker.journal.directory) as private:
            require(_read(private,name)=={'schema':'development-cycle-next-v1','phase':'preparing',
                'request':request,'result':None},'development_cycle_preparation_conflict')
            private.replace(name,canonical({'schema':'development-cycle-next-v1','phase':'complete',
                'request':request,'result':result}))
        return result
