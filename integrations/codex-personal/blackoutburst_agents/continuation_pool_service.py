"""Explicit once-pass logical development continuation.

Only operator-selected tasks are visited. Pool jobs are preparation registries,
never dispatched here: each selected task owns its concrete handles in its own
thread, and actual model/tool Runtime guards still admit every execution.
An unfinished pass intent is evidence of uncertainty, never a replay request.
"""
from contextlib import ExitStack, closing, contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import threading
import time
import uuid

from .continuation_pool_configuration import load_continuation, MAX_CONFIGURATION
from .continuation import ContinuationTask, ContinuationChecks, PHASES
from .continuation_journal import ContinuationJournal
from .dev_journal import PrivateDirectory, decode, DevelopmentJournal
from .dev_manifest import Budgets, DevelopmentError, canonical, digest, identity, opaque, validate_manifest
from .dev_planner import _directory_pin, _reasoning_configuration
from .dev_planner_journal import PlannerBudgets
from .development_authorization import DevelopmentAuthorization
from .development_cycle_driver import DevelopmentCycleDriver, _test_caps
from .development_pool import DevelopmentPoolDriver
from .development_pool_selection import file_document
from .dev_runtime import development_work_id
from .git_workspace import LocalRepositoryGrant, LocalGitWorkspaceController
from .linux_input_snapshot import _path
from .linux_planner_policy import _executable_pin, linux_source_pins
from .linux_pool_preparation import LinuxPoolJobPreparer
from .pool_journal import uuid_value
from .private_development_checkpoint import PrivateDevelopmentCheckpointController, checkpoint_id_for
from .runtime_guard import RuntimeGuard
from .worker_pool import WorkerPool

MAX_RECORD=512*1024
JOIN_SECONDS=30
STATES=frozenset(PHASES)|{'awaiting_independent_review','independent_review_published','backoff','not_started'}
REASONS=frozenset({'','continuation_pool_configuration_changed','continuation_pool_source_changed',
    'continuation_pool_tools_changed','continuation_pool_owner_busy','continuation_pool_pending',
    'continuation_pool_stopped','continuation_pool_cleanup_unknown','continuation_pool_target_failed',
    'continuation_pool_binding_changed','continuation_pool_api_unavailable','continuation_pool_clock_changed'})
PERMISSION_FAILURES=frozenset({'development_grants_unavailable','development_ticket_unavailable',
    'development_registration_mismatch'})


def fail(code='continuation_pool_target_failed'):raise DevelopmentError(code)


def require(value,code='continuation_pool_target_failed'):
    if not value:fail(code)


def _read(private,name):
    raw=private.read(name,MAX_RECORD);value=decode(raw)
    require(canonical(value)==raw,'continuation_pool_binding_changed')
    return value


def _write(private,name,value,*,replace=False):
    raw=canonical(value);require(len(raw)<=MAX_RECORD)
    (private.replace if replace else private.create)(name,raw)


def _mkdir(path):
    # Callers supply a compiled child of held private roots, never Board text.
    os.mkdir(path,0o700)
    with PrivateDirectory(Path(path).parent) as parent:os.fsync(parent.fd)
    with PrivateDirectory(path):pass


@contextmanager
def _existing_work_database(directory,expected=None):
    """Hold the existing owned DB inode across the normal journal opening."""
    with PrivateDirectory(directory) as private:
        fd=os.open('work-journal.sqlite3',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
        try:
            private.validate_file(fd);pin=identity(os.fstat(fd))
            require(0<os.fstat(fd).st_size<=32*1024*1024)
            if expected is not None:require(pin==expected,'continuation_pool_cleanup_unknown')
            require(identity(os.stat('work-journal.sqlite3',dir_fd=private.fd,follow_symlinks=False))==pin)
            yield pin
            private.verify();private.validate_file(fd)
            require(identity(os.fstat(fd))==pin==identity(os.stat('work-journal.sqlite3',dir_fd=private.fd,follow_symlinks=False)),
                'continuation_pool_cleanup_unknown')
        finally:os.close(fd)


class _Pass:
    """Cancellation controls never invoke author SQLite across threads."""
    def __init__(self,service,should_stop):
        self.service,self.should_stop=service,should_stop
        self.deadline=time.monotonic()+min(service.max_seconds,service.loaded['batch']['timeout_seconds'])
        self.cancel=threading.Event();self.mutex=threading.Lock();self.pools=[];self.workers=[];self.reviewers=[];self.stop_failed=False
        self.human_reports={}
    def stopping(self):
        if self.cancel.is_set():return True
        try:
            self.service._pins()
            value=time.monotonic()>=self.deadline or bool(self.should_stop and self.should_stop())
        except Exception:value=True
        if value:self.stop()
        return value
    def local_guard(self):
        # A stop-only predicate. Never perform cross-pool Stop or RPC while
        # the preparer may be inspecting its queue under the metadata mutex.
        try:requested=bool(self.should_stop and self.should_stop())
        except Exception:requested=True
        if self.cancel.is_set() or time.monotonic()>=self.deadline or requested:
            fail('continuation_pool_stopped')
    def guard(self):
        if self.stopping():fail('continuation_pool_stopped')
    def add_pool(self,pool):
        with self.mutex:self.pools.append(pool);cancelled=self.cancel.is_set()
        if cancelled:pool.stop()
    def remove_pool(self,pool):
        with self.mutex:
            if pool in self.pools:self.pools.remove(pool)
    def add_reviewer(self,reviewer):
        from .continuation_aggregate_review import ContinuationAggregateReviewer
        require(type(reviewer) is ContinuationAggregateReviewer)
        with self.mutex:self.reviewers.append(reviewer);cancelled=self.cancel.is_set()
        if cancelled:reviewer.stop()
    def remove_reviewer(self,reviewer):
        with self.mutex:
            if reviewer in self.reviewers:self.reviewers.remove(reviewer)
    def remember_human(self,name,report):
        with self.mutex:self.human_reports[name]=deepcopy(report)
    def decorate(self,name,projection):
        with self.mutex:report=deepcopy(self.human_reports.get(name))
        return projection if report is None else {**projection,'human_visibility':report}
    def add_worker(self,directory,operation_id,scope):
        with _existing_work_database(directory) as db_pin:
            record=(str(directory),_directory_pin(directory),db_pin,operation_id,deepcopy(scope))
        with self.mutex:self.workers.append(record);cancelled=self.cancel.is_set()
        if cancelled:self.stop()
        return record
    def remove_worker(self,record):
        with self.mutex:
            if record in self.workers:self.workers.remove(record)
    def stop(self):
        self.cancel.set()
        with self.mutex:pools,workers,reviewers=tuple(self.pools),tuple(self.workers),tuple(self.reviewers)
        for reviewer in reviewers:
            try:reviewer.stop()
            except Exception:self.stop_failed=True
        for pool in pools:
            try:pool.stop()
            except Exception:self.stop_failed=True
        # Identical to the existing concrete stop companion: only metadata
        # crosses threads. A fresh journal handle writes the normal immutable
        # Stop markers; no Runtime is finished or claim released by inference.
        from .work_journal import WorkJournal,JournalError
        from .development_worker import DevelopmentBoardWorker
        for directory,pin,db_pin,operation,scope in workers:
            try:
                require(_directory_pin(directory)==pin)
                with _existing_work_database(directory,db_pin),WorkJournal(directory) as journal:
                    try:op=journal.get(operation)
                    except JournalError as error:
                        if error.code=='operation_not_found':continue  # Before Worker.prepare.
                        raise
                    require(op['scope']==scope and op['configuration'].get('kind')=='development'
                        and op['configuration']['pins']['journal']==pin)
                    DevelopmentBoardWorker(journal,None,None,None,None).stop(operation)
            except Exception:self.stop_failed=True


_PREPARATION_ISSUER=object()


class _InitialPreparation:
    """An ephemeral stop-only check; never source/claim/permission authority."""
    def __init__(self,issuer,preparer,operation_id,control):
        require(issuer is _PREPARATION_ISSUER and type(preparer) is LinuxPoolJobPreparer
            and type(control) is _Pass,'continuation_pool_binding_changed')
        self.issuer=issuer
        self.preparer,self.operation_id,self.control=preparer,operation_id,control
        self.pid,self.thread=os.getpid(),threading.current_thread();self.active=True
    def __reduce_ex__(self,_):raise TypeError('Preparation controls cannot be serialized')
    def __copy__(self):raise TypeError('Preparation controls cannot be copied')
    def __deepcopy__(self,_):raise TypeError('Preparation controls cannot be copied')


def check_initial_preparation(preparer,operation_id,*,fresh=False):
    scope=getattr(preparer,'_continuation_initial_preparation',None)
    require(type(scope) is _InitialPreparation and scope.issuer is _PREPARATION_ISSUER and scope.active is True and scope.preparer is preparer
        and scope.operation_id==operation_id and scope.pid==os.getpid()
        and scope.thread is threading.current_thread(),'continuation_pool_binding_changed')
    scope.control.local_guard()
    if fresh:scope.control.service._pins()
    scope.control.local_guard()


@contextmanager
def _initial_preparing(control,preparer,operation_id):
    require(getattr(preparer,'_continuation_initial_preparation',None) is None)
    scope=_InitialPreparation(_PREPARATION_ISSUER,preparer,operation_id,control)
    preparer._continuation_initial_preparation=scope
    try:
        check_initial_preparation(preparer,operation_id,fresh=True)
        yield
        check_initial_preparation(preparer,operation_id,fresh=True)
    finally:
        scope.active=False;del preparer._continuation_initial_preparation


class ContinuationPoolService:
    """An inert constructor and one explicitly invoked, bounded scheduler pass."""
    def __init__(self,root,*,configuration_path,owner_id,board_endpoint,executable,cli_version,
                 interpreter,unshare,gh_executable,max_seconds):
        self.root=Path(_path(os.fspath(root)));self.path=Path(configuration_path)
        self.owner_id,self.endpoint=opaque(owner_id),board_endpoint
        self.max_seconds=max_seconds;self.cli_version=cli_version
        self.loaded,self.configuration_pins=load_continuation(configuration_path,root=self.root,
            owner_id=owner_id,board_endpoint=board_endpoint,max_seconds=max_seconds)
        require(type(cli_version) is str and cli_version.startswith('codex-cli '))
        self.tools={key:_executable_pin(os.fspath(value)) for key,value in
            (('executable',executable),('interpreter',interpreter),('unshare',unshare),('gh',gh_executable))}
        self.sources=linux_source_pins();self.source_sha=digest(canonical(self.sources))
        self.root_pin=_directory_pin(self.root)
        self.thread=threading.current_thread();self.pid=os.getpid();self._mutex=threading.Lock()
        self._retained_lease=None  # Kept until process exit if cooperative joins fail.
        self._retained_resources=[]  # Author/reader handles outlive an uncertain review join.
        self._sealed_settings=canonical(self._settings())

    def _settings(self):
        return {'root':str(self.root),'root_pin':self.root_pin,'configuration_path':str(self.path),
            'owner_id':self.owner_id,'endpoint':self.endpoint,'max_seconds':self.max_seconds,
            'loaded':self.loaded,'pins':self.configuration_pins,'tools':self.tools,
            'sources':self.sources,'source_sha256':self.source_sha,'cli_version':self.cli_version}

    def _pins(self,*,full=False):
        require(canonical(self._settings())==self._sealed_settings,'continuation_pool_configuration_changed')
        require(_directory_pin(self.root)==self.root_pin,'continuation_pool_binding_changed')
        for pin in self.configuration_pins.values():
            require(file_document(pin['path'],MAX_CONFIGURATION)[1]==pin,'continuation_pool_configuration_changed')
        if full:
            require(linux_source_pins()==self.sources,'continuation_pool_source_changed')
            for pin in self.tools.values():
                require(_executable_pin(pin['path'])==pin,'continuation_pool_tools_changed')

    def _binding(self,target,job):
        return {'schema':'continuation-pool-task-binding-v1','owner_id':self.owner_id,
            'selected':target,'job':job,'workflow':next(row for row in self.loaded['workflow']['jobs'] if row['name']==job['name']),
            'configuration_pins':self.configuration_pins,'tools':self.tools,'cli_version':self.cli_version,
            'source_sha256':self.source_sha,'cycle_limits':self.loaded['cycle_limits'][job['name']]}

    @contextmanager
    def _lease(self):
        path=self.root/'continuations'
        try:_mkdir(path);fresh=True
        except FileExistsError:
            fresh=False
            with PrivateDirectory(path):pass
        owned=PrivateDirectory(path);fd=None
        try:
            if fresh:
                fd=os.open('owner.lock',os.O_RDWR|os.O_NOFOLLOW|os.O_CREAT|os.O_EXCL,0o600,dir_fd=owned.fd)
                os.fsync(fd);os.fsync(owned.fd)
            else:
                try:fd=os.open('owner.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=owned.fd)
                except FileNotFoundError:fail('continuation_pool_pending')
            owned.validate_file(fd);pin=identity(os.fstat(fd))
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:fail('continuation_pool_owner_busy')
            def verify():
                owned.verify();owned.validate_file(fd)
                require(identity(os.fstat(fd))==pin==identity(os.stat('owner.lock',dir_fd=owned.fd,follow_symlinks=False)),
                    'continuation_pool_binding_changed')
            verify();yield owned,verify;verify()
        finally:
            if self._retained_lease is not None:
                # The unit's cgroup stop is the final guard. Do not admit a
                # second pass while a local thread may still own an effect.
                self._retained_lease=(owned,fd)
            else:
                if fd is not None:os.close(fd)
                owned.close()

    @staticmethod
    def _projection(target,*,phase='needs_you',reason='',operation_id=None,state_sha256=None,not_before=None):
        require(phase in STATES and reason in REASONS)
        if phase=='needs_you' and not reason:reason='continuation_pool_target_failed'
        if operation_id is not None:uuid_value(operation_id)
        return {'name':target['name'],'logical_task_id':target['logical_task_id'],'phase':phase,
            'reason':reason,'operation_id':operation_id,'state_sha256':state_sha256,
            'next_observation_at':not_before,'launchable':False}

    def _review_history(self,directory,target,job,cursor):
        """Pinned past opinion only; no source builder, registration or RPC."""
        receipt=cursor['review_receipt'];projection=cursor['result']
        require(type(receipt) is dict and set(receipt)=={'schema','status','logical_task_id','source_operation_id',
            'review_operation_id','binding_sha256','ledger_pin','review','accepted','launchable'}
            and receipt['schema']=='continuation-independent-review-receipt-v1' and receipt['status']=='published'
            and receipt['logical_task_id']==target['logical_task_id']
            and receipt['source_operation_id']==projection['operation_id']
            and receipt['accepted'] is False and receipt['launchable'] is False,
            'continuation_pool_binding_changed')
        uuid_value(receipt['source_operation_id']);uuid_value(receipt['review_operation_id'])
        path=directory/'aggregate/delivery/review-source/continuation-review.json'
        raw,pin=file_document(path,MAX_RECORD)
        require(pin==receipt['ledger_pin'],'continuation_pool_binding_changed')
        value=decode(raw)
        require(canonical(value)==raw and type(value) is dict and set(value)=={'request','phase','job','result','pins'}
            and value['phase']=='complete' and value['result']==receipt['review']
            and value['result'].get('status')=='complete' and value['result'].get('operation_id')==receipt['review_operation_id']
            and value['job'].get('id')==receipt['review_operation_id']
            and digest(canonical(value['request']))==receipt['binding_sha256'],'continuation_pool_binding_changed')
        request=value['request'];specification=next(row for row in self.loaded['workflow']['jobs'] if row['name']==job['name'])
        require(request.get('schema')=='continuation-aggregate-review-v1'
            and request.get('logical_task_id')==target['logical_task_id']
            and request.get('source_operation_id')==receipt['source_operation_id']
            and request.get('directory')==str(directory/'independent-review')
            and request.get('configuration_pins')==self.configuration_pins and request.get('name')==job['name']
            and request.get('reviewer')==specification['reviewer'],'continuation_pool_binding_changed')
        result=self._projection(target,phase='independent_review_published',operation_id=receipt['source_operation_id'],
            state_sha256=projection['state_sha256'])
        result.update(review_operation_id=receipt['review_operation_id'],review_receipt_sha256=digest(canonical(receipt)),
            accepted=False,historical=True)
        return result

    def run_once(self,*,should_stop=None):
        require(self.pid==os.getpid() and self.thread is threading.current_thread())
        require(should_stop is None or callable(should_stop))
        require(self._mutex.acquire(blocking=False),'continuation_pool_owner_busy')
        try:
            require(self._retained_lease is None,'continuation_pool_cleanup_unknown')
            self._pins(full=True)
            try:
                from .development_provider_continuation import DevelopmentProviderContinuation
                from .development_aggregate_delivery import AggregateDevelopmentDeliveryController
            except ImportError:fail('continuation_pool_api_unavailable')
            with self._lease() as (owned,held):
                control=_Pass(self,should_stop);targets=self.loaded['configuration']['tasks']
                jobs={job['name']:job for job in self.loaded['batch']['jobs']}
                results={};threads={};index=0;join_deadline=None
                def advance(target):
                    # Every concrete handle and callback is constructed here,
                    # not in the owner/scheduler thread.
                    try:results[target['name']]=self._target(owned.path,target,jobs[target['name']],control)
                    except Exception as error:
                        code=vars(error).get('code') if type(error) is DevelopmentError else None
                        results[target['name']]=control.decorate(target['name'],self._projection(target,
                            reason=code if code in REASONS else 'continuation_pool_target_failed'))
                    except BaseException:
                        # A thread's abrupt interruption is not a clean or
                        # never-started task. Do not let threading print it.
                        control.stop_failed=True;control.stop()
                        results[target['name']]=self._projection(target,reason='continuation_pool_cleanup_unknown')
                try:
                    while index<len(targets) or threads:
                        held()
                        if control.stopping():
                            if join_deadline is None:join_deadline=time.monotonic()+JOIN_SECONDS
                        else:
                            while (index<len(targets) and len(threads)<self.loaded['configuration']['concurrency']
                                    and not control.stopping()):
                                target=targets[index];index+=1
                                thread=threading.Thread(target=advance,args=(target,),name='continuation-'+target['name'],daemon=False)
                                threads[target['name']]=thread;thread.start()
                        for name,thread in tuple(threads.items()):
                            thread.join(timeout=.025)
                            if not thread.is_alive():del threads[name]
                        if join_deadline is not None and time.monotonic()>=join_deadline and threads:
                            self._retained_lease=True
                            for target in targets:
                                if target['name'] not in results:
                                    results[target['name']]=self._projection(target,reason='continuation_pool_cleanup_unknown')
                            break
                        if control.cancel.is_set() and not threads:break
                except BaseException:
                    control.stop();deadline=time.monotonic()+JOIN_SECONDS
                    for thread in tuple(threads.values()):
                        if thread.ident is not None:thread.join(timeout=max(0,deadline-time.monotonic()))
                    if control.stop_failed or any(thread.is_alive() for thread in threads.values()):
                        self._retained_lease=True
                    fail('continuation_pool_cleanup_unknown')
                for target in targets:
                    results.setdefault(target['name'],self._projection(target,phase='not_started',reason='continuation_pool_stopped'))
                ordered=[results[target['name']] for target in targets]
                if control.stop_failed:self._retained_lease=True
                status='needs_you' if control.stop_failed or any(row['phase']=='needs_you' for row in ordered) else 'stopped' if control.cancel.is_set() else 'pass_complete'
                return {'schema':'continuation-pool-pass-v1','status':status,'tasks':ordered,
                    'activated':True,'pass_complete_is_project_completion':False}
        finally:self._mutex.release()

    def _target(self,root,target,job,control):
        control.guard();self._pins(full=True)
        key=digest(canonical({'owner_id':self.owner_id,'logical_task_id':target['logical_task_id']}))
        directory=root/key;binding=self._binding(target,job)
        try:_mkdir(directory);fresh=True
        except FileExistsError:fresh=False
        with PrivateDirectory(directory) as private:
            if fresh:
                _write(private,'binding.json',binding)
                _mkdir(directory/'generations');_mkdir(directory/'checkpoints')
                cursor={'schema':'continuation-pool-cursor-v1','phase':'idle','generation':None,'result':None,'not_before':None}
                _write(private,'cursor.json',cursor)
            else:
                require(_read(private,'binding.json')==binding,'continuation_pool_binding_changed')
                cursor=_read(private,'cursor.json')
            require(type(cursor) is dict and set(cursor) in ({'schema','phase','generation','result','not_before'},
                {'schema','phase','generation','result','not_before','wake_generation'},
                {'schema','phase','generation','result','not_before','review_receipt'})
                and cursor['schema']=='continuation-pool-cursor-v1' and cursor['phase'] in ('idle','pending'))
            waiting_generation=cursor.get('wake_generation')
            if waiting_generation is not None:
                uuid_value(waiting_generation)
                require(cursor['phase']=='idle' and cursor['generation'] is not None
                    and waiting_generation!=cursor['generation'] and type(cursor['result']) is dict
                    and cursor['result'].get('phase')=='paused_provider','continuation_pool_binding_changed')
            if cursor['phase']=='pending':return self._projection(target,reason='continuation_pool_pending')
            if 'review_receipt' in cursor:
                require(cursor['generation'] is not None and type(cursor['result']) is dict
                    and cursor['result'].get('phase')=='independent_review_published','continuation_pool_binding_changed')
                with ContinuationJournal.open(directory/'logical') as journal:
                    observed=journal.snapshot()
                    if observed['state']['stop_requested'] or observed['state']['phase']=='stopped':
                        return self._projection(target,phase='stopped',state_sha256=observed['state_sha256'])
                    require(observed['state']['phase']=='complete'
                        and observed['state_sha256']==cursor['result'].get('state_sha256'),'continuation_pool_binding_changed')
                    historical=self._review_history(directory,target,job,cursor)
                    current=journal.snapshot();control.guard()
                    if current['state']['stop_requested'] or current['state']['phase']=='stopped':
                        return self._projection(target,phase='stopped',state_sha256=current['state_sha256'])
                    require(current==observed,'continuation_pool_binding_changed')
                    return historical
            now=time.time();require(type(now) in (int,float) and now>0,'continuation_pool_clock_changed')
            if cursor['not_before'] is not None:
                require(type(cursor['not_before']) in (int,float) and cursor['not_before']>0)
                if now<cursor['not_before']:
                    # Stop remains visible during a long provider backoff.
                    # Historical Core metadata creates neither a permit nor
                    # a Worker/client and cannot admit any execution.
                    with ContinuationJournal.open(directory/'logical') as journal:
                        observed=journal.snapshot()
                        if observed['state']['phase'] in ('stopped','needs_you','complete'):
                            return self._projection(target,phase=observed['state']['phase'],
                                state_sha256=observed['state_sha256'])
                    return self._projection(target,phase='backoff',not_before=cursor['not_before'])
            with ExitStack() as resources:
                old=None
                if cursor['generation'] is not None:
                    old=_Generation.open(self,directory,target,job,cursor['generation'],control,resources)
                    state=old.task.snapshot()
                    if state['state']['phase'] not in ('paused_cycle','paused_provider'):
                        phase=state['state']['phase']
                        if phase not in ('stopped','needs_you','complete'):
                            _write(private,'cursor.json',{**cursor,'phase':'pending'},replace=True)
                            return self._projection(target,reason='continuation_pool_pending',operation_id=old.operation,
                                state_sha256=state['state_sha256'])
                        if (phase=='complete' and type(cursor['result']) is dict
                                and cursor['result'].get('phase')=='awaiting_independent_review'
                                and cursor['result'].get('state_sha256')==state['state_sha256']):
                            phase='awaiting_independent_review'
                        return self._projection(target,phase=phase,operation_id=old.operation,
                            state_sha256=state['state_sha256'])
                    require(type(cursor['result']) is dict and cursor['result']['state_sha256']==state['state_sha256']
                        and cursor['result']['operation_id']==old.operation and cursor['result']['phase']==state['state']['phase'],
                        'continuation_pool_binding_changed')
                    require(old.provider.status(old.operation)['status']==state['state']['phase'])
                    if state['state']['phase']=='paused_provider' and not old.can_wake():
                        waiting=time.time()+self.loaded['configuration']['provider_backoff_seconds']
                        _write(private,'cursor.json',{**cursor,'not_before':waiting},replace=True)
                        return self._projection(target,phase='paused_provider',operation_id=old.operation,
                            state_sha256=state['state_sha256'],not_before=waiting)
                generation_id=waiting_generation or str(uuid.uuid4())
                # Persist BEFORE all preparation, registrations and model calls.
                pending={key:value for key,value in cursor.items() if key!='wake_generation'}
                pending.update(phase='pending',generation=generation_id,result=None,not_before=None)
                _write(private,'cursor.json',pending,replace=True)
                if waiting_generation is None:
                    new=_Generation.create(self,directory,target,job,generation_id,control,resources,old=old)
                else:
                    require(old is not None and old.task.snapshot()['state']['phase']=='paused_provider')
                    new=_Generation.open(self,directory,target,job,generation_id,control,resources,candidate_for=old)
                try:
                    if old is None:
                        new.prepare();new.open_worker();new.open_task(create=True)
                        result=new.provider.run_checkpoint(new.operation,request_id='continuation-'+new.operation,
                            after_actions=target['checkpoint_after_actions'])
                    elif old.task.snapshot()['state']['phase']=='paused_provider':
                        # Atomic provider wake owns preparation/admission. There is
                        # deliberately no WorkerPool dispatch on either side.
                        if waiting_generation is None:
                            new.open_worker(unprepared=True);new.open_task(create=False,old=old)
                        paused=old.task.snapshot()
                        result=old.provider.wake(old.operation,next_controller=new.provider,preparer=new.preparer,
                            next_operation_id=new.operation,parameters=new.arguments,
                            request_id='continuation-'+new.operation,after_actions=target['checkpoint_after_actions'])
                        if result['status']=='paused_provider' and new.task.snapshot()==paused:
                            # No new cycle was admitted. Keep the failed source
                            # generation, plus this exact candidate for a fresh
                            # normal wake; never orphan a prepared_waiting intent.
                            control.guard()
                            finished=time.time();require(type(finished) in (int,float) and finished>=now,
                                'continuation_pool_clock_changed')
                            not_before=finished+self.loaded['configuration']['provider_backoff_seconds']
                            projection=self._projection(target,phase='paused_provider',operation_id=old.operation,
                                state_sha256=paused['state_sha256'],not_before=not_before)
                            _write(private,'cursor.json',{'schema':'continuation-pool-cursor-v1','phase':'idle',
                                'generation':old.generation,'wake_generation':generation_id,
                                'result':projection,'not_before':not_before},replace=True)
                            return projection
                    else:
                        retired=old.provider.maintenance.retire_checkpoint(old.operation)
                        require(retired['phase']=='retired')
                        old.provider.maintenance.prepare_next(old.operation,preparer=new.preparer,
                            next_operation_id=new.operation,parameters=new.arguments)
                        new.open_worker();new.open_task(create=False,old=old)
                        result=new.provider.run_checkpoint(new.operation,request_id='continuation-'+new.operation,
                            parent_checkpoint_id=checkpoint_id_for(old.operation),after_actions=target['checkpoint_after_actions'])
                    control.guard()
                    if result['status']=='paused_cycle':
                        require(new.provider.maintenance.retire_checkpoint(new.operation)['phase']=='retired')
                    elif result['status']=='review_ready':
                        result=new.publish()
                    snapshot=new.task.snapshot();phase=result['status']
                    if phase=='needs_you':
                        new.human_handoff();snapshot=new.task.snapshot()
                        if snapshot['state']['stop_requested']:phase='stopped'
                    require(phase in STATES,'continuation_pool_target_failed')
                    require(snapshot['state']['phase']==phase or phase in ('awaiting_independent_review','independent_review_published')
                        and snapshot['state']['phase']=='complete')
                    finished=time.time();require(type(finished) in (int,float) and finished>=now,'continuation_pool_clock_changed')
                    not_before=finished+self.loaded['configuration']['provider_backoff_seconds'] if phase=='paused_provider' else None
                    projection=control.decorate(target['name'],self._projection(target,phase=phase,operation_id=new.operation,
                        state_sha256=snapshot['state_sha256'],not_before=not_before))
                    completed={'schema':'continuation-pool-cursor-v1','phase':'idle',
                        'generation':generation_id,'result':projection,'not_before':not_before}
                    if phase=='independent_review_published':
                        completed['review_receipt']=result['review_receipt']
                        projection.update(review_operation_id=result['review_receipt']['review_operation_id'],
                            review_receipt_sha256=digest(canonical(result['review_receipt'])),accepted=False,historical=False)
                    _write(private,'cursor.json',completed,replace=True)
                    return projection
                except Exception as error:
                    # The own running generation becomes NeedsYou with actual
                    # counters. This creates no accepted checkpoint or cleanup.
                    # An already sealed code completion is not reopened merely
                    # because a later publication/handoff is uncertain.
                    if type(new.task) is ContinuationTask and new.provider is not None:
                        try:
                            current=new.task.snapshot()
                            if current['state']['phase']=='running':
                                new.provider.driver._mark_review(new.operation,new.task._context(current,purpose='execute'))
                        except Exception:pass  # Pending service intent remains fail-closed.
                        if not (type(error) is DevelopmentError and vars(error).get('code')=='continuation_pool_stopped'):
                            new.human_handoff(error)
                    raise



class _LiveChecks:
    """Checks compiled against this generation's actual Worker and selection.

    A stored service cursor provides correlation only. Neither an idle flag nor
    a provider observation supplies permission to these callbacks.
    """
    def __init__(self,generation):self.g=generation;self.observed=None
    def checks(self):
        return ContinuationChecks(**{key:getattr(self,key) for key in
            ('scope','claim','source','fence','runtime','resume')})
    def operation(self):
        self.g.control.guard()
        return self.g.worker.journal.get(self.g.operation)
    def scope(self,context):
        op=self.operation();w=self.g.worker
        require(all(op['scope'][key]==context.scope[key] for key in
            ('owner_id','agent_id','project_id','ticket_id','board_endpoint')),
            'continuation_pool_binding_changed')
        with PrivateDirectory(op['configuration']['pins']['broker']['path']) as private:
            manifest=validate_manifest(decode(private.read('manifest.json',128*1024)))
            require(digest(canonical(manifest))==op['configuration']['manifest_hash'])
            auth=DevelopmentAuthorization(w.client,scope=op['scope'],manifest=manifest,
                registration=op['payload']['registration'],clock=w.clock,stop_requested=self.g.control.stopping)
            active='context' in op['payload'] and not w._stopped(op['id'])
            if active:
                auth.expected_context=deepcopy(op['payload']['context'])
                auth.claim_version=op['payload']['claim_version'];auth.claim_id=op['payload']['claim_id']
            observation=auth.snapshot(require_claim=active)
            registration=op['payload']['registration']
            require(registration['profile_version']==context.scope['profile_version']
                and registration['identity']['device_id']==context.scope['device_id'])
            self.observed=(context,observation,active,auth)
    def claim(self,context):
        require(self.observed is not None and self.observed[0] is context)
        ticket=self.observed[1]['ticket']
        # Self-unassignment at the final normal handoff requires an explicit
        # author assignment. A claim alone never supplies it.
        require(ticket.get('assignee_agent_id')==self.g.job['agent_id'],
            'continuation_pool_binding_changed')
        if not self.observed[2]:
            require((ticket['claimant'] is None and ticket['status']=='todo'
                and ticket.get('claim_expired') is not True) or self.observed[3].own_claim(ticket),
                'continuation_pool_binding_changed')
    def source(self,context):
        op=self.operation();w=self.g.worker;self.g.workspaces._roots()
        self.g.service._pins(full=True)
        # These readers take no broker/planner lock. Control can be called
        # while the ordinary SDK holds them during a confirmed action.
        # Their own source/test/permission guards continue to run unchanged.
        bound=self.g.driver.selections.load(self.g.context)
        self.g.driver._operation(w,self.g.context,bound)
    def fence(self,context):
        from .dev_fence import DevelopmentFence,canonical_anchor
        op=self.operation();w=self.g.worker;binding=op['payload'].get('parent_binding')
        anchor=canonical_anchor() if w.test_anchor is None else Path(w.test_anchor)
        try:os.stat(anchor,follow_symlinks=False)
        except FileNotFoundError:
            require(binding is None,'continuation_pool_binding_changed');return
        with PrivateDirectory(anchor) as private:
            fd=os.open('fence.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
            try:
                private.validate_file(fd);pin=identity(os.fstat(fd));fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
                state=DevelopmentFence._validate(object.__new__(DevelopmentFence),_read(private,'fence.json'))
                manifest=decode(file_document(Path(op['configuration']['pins']['broker']['path'])/'manifest.json')[0])
                found=state['records'].get(DevelopmentFence.key(op['scope'],manifest['root_identity']))
                if binding is None:require(found is None,'continuation_pool_binding_changed')
                else:
                    expected=deepcopy(binding)
                    path=Path(op['configuration']['pins']['broker']['path'])/'planner.json'
                    try:planner=decode(file_document(path,2*1024*1024)[0])
                    except FileNotFoundError:pass
                    else:expected['configuration_hash']=digest(canonical(planner['configuration']))
                    require(found=={'binding':expected,'operation_id':op['id']},'continuation_pool_binding_changed')
                private.verify();private.validate_file(fd)
                require(identity(os.fstat(fd))==pin==identity(os.stat('fence.lock',dir_fd=private.fd,follow_symlinks=False)))
            finally:os.close(fd)
    def runtime(self,context):
        op=self.operation();w=self.g.worker
        rows=w.runtime.list_status();require(len(rows)<=128)
        for row in rows:
            record=w.runtime.get(row['id']);config=record['configuration']
            require(config['scope']==op['scope'] and config['work_id']==development_work_id(op['id'])
                and record['phase']=='finished' and type(record['receipt']) is dict
                and record['receipt'].get('cleanup_confirmed') is True,'continuation_pool_cleanup_unknown')
            guard=RuntimeGuard(w.runtime,w.control,op['scope']);guard.operation=record
            found=w.control.call('lookup_execution',{'idempotency_key':record['key']})
            require(type(found) is dict and found.get('found') is True,'continuation_pool_cleanup_unknown')
            receipt=guard.metadata(found.get('execution'))
            require(receipt==record['receipt'] and receipt['state']=='finished'
                and receipt['cleanup_confirmed'] is True,'continuation_pool_cleanup_unknown')
        if not rows:
            # A never-started generation has no model work or pending Runtime.
            require(op['stage']=='new' and w.store._db.execute('SELECT id FROM works LIMIT 1').fetchone() is None,
                'continuation_pool_cleanup_unknown')
    def resume(self,context):
        self.g.control.guard();self.g.service._pins()
        require(context.logical_task_id==self.g.target['logical_task_id'])
        if self.g.task is not None:
            snapshot=self.g.task.snapshot()
            require(snapshot['state']['stop_requested'] is False
                and snapshot['state']['phase'] not in ('needs_you','stopped','complete'),
                'continuation_pool_stopped')


class _Generation:
    """One thread's concrete queued pool, Worker, source and journal handles."""
    @classmethod
    def create(cls,service,directory,target,job,generation,control,resources,*,old=None):
        result=cls();result._setup(service,directory,target,job,generation,control,resources)
        _mkdir(result.directory)
        if old is None:grant=deepcopy(job['repository'])
        else:
            cp=old.checkpoints.verify(checkpoint_id_for(old.operation))
            grant={**deepcopy(job['repository']),'repository':cp['checkout'],'base_commit':cp['commit_sha']}
        for name in ('git-state','checkouts'):
            parent=service.root/name/directory.name
            try:_mkdir(parent)
            except FileExistsError:
                with PrivateDirectory(parent):pass
            _mkdir(parent/generation)
        grant['workspace_root']=str(service.root/'checkouts'/directory.name/generation)
        result.grant=grant
        result.workspaces=LocalGitWorkspaceController(service.root/'git-state'/directory.name/generation,
            grant=LocalRepositoryGrant(**grant))
        result._pool(create=True)
        with PrivateDirectory(result.directory) as private:
            _write(private,'generation.json',{'schema':'continuation-pool-generation-v1','generation':generation,
                'grant':grant,'submitted':result.submitted,'connection':result.driver.selections.connection_pin})
        if old is not None:
            with PrivateDirectory(result.directory) as private:
                _write(private,'parent.json',{'checkpoint_id':cp['checkpoint_id'],'checkpoint_sha256':cp['checkpoint_sha256']})
        result.task=None
        result.checkpoint_configuration=None if old is None else deepcopy(old.checkpoints.configuration)
        result._worker_open=False
        return result
    @classmethod
    def open(cls,service,directory,target,job,generation,control,resources,*,candidate_for=None):
        uuid_value(generation);result=cls();result._setup(service,directory,target,job,generation,control,resources)
        with PrivateDirectory(result.directory) as private:value=_read(private,'generation.json')
        require(type(value) is dict and set(value)=={'schema','generation','grant','submitted','connection'}
            and value['schema']=='continuation-pool-generation-v1' and value['generation']==generation)
        result.grant=value['grant']
        # Existing metadata is not permission: exact source identity/caps are
        # revalidated below by Git, Core, provider and real Worker paths.
        require(result.grant['owner_id']==service.owner_id
            and result.grant['repository_id']==job['repository']['repository_id']
            and result.grant['workspace_root']==str(service.root/'checkouts'/directory.name/generation))
        result.workspaces=LocalGitWorkspaceController(service.root/'git-state'/directory.name/generation,
            grant=LocalRepositoryGrant(**result.grant))
        result.submitted=value['submitted'];result._pool(create=False)
        require(result.driver.selections.connection_pin==value['connection'])
        result.task=None;result._worker_open=False;result.checkpoint_configuration=None
        if candidate_for is None:
            result.open_worker(existing=True);result.open_task(create=False)
        else:
            cp=candidate_for.checkpoints.verify(checkpoint_id_for(candidate_for.operation))
            require(result.grant=={**deepcopy(job['repository']),'repository':cp['checkout'],
                'base_commit':cp['commit_sha'],'workspace_root':str(service.root/'checkouts'/directory.name/generation)})
            with PrivateDirectory(result.directory) as private:
                require(_read(private,'parent.json')=={'checkpoint_id':cp['checkpoint_id'],
                    'checkpoint_sha256':cp['checkpoint_sha256']})
            result.open_worker(existing=True,unprepared=True);result.open_task(create=False,old=candidate_for)
        return result
    def _setup(self,service,directory,target,job,generation,control,resources):
        uuid_value(generation)
        self.service,self.target,self.job,self.control,self.resources=service,target,job,control,resources
        self.target_directory=directory;self.directory=directory/'generations'/generation
        self.generation=generation;self.worker=None;self.task=None;self.provider=None;self._resources_retained=False
        self.arguments={key:deepcopy(job[key]) for key in ('project_id','ticket_id','paths','resource','actions',
            'verifier_path','verifier_sha256','test_name','test_timeout_seconds','test_max_output_bytes','instruction','model')}
        self.arguments.update(planner_budgets=PlannerBudgets(**job['planner_budgets']),
            broker_budgets=Budgets(**job['broker_budgets']),cli_version=service.cli_version,
            **{key:service.tools[key]['path'] for key in ('executable','interpreter','unshare')},
            **_reasoning_configuration(job.get('reasoning_effort')))
    def _pool(self,*,create):
        if not create:
            for path in (self.directory/'pool'/'pool-state.json',self.directory/'pool'/'pool-owner.lock'):
                file_document(path,MAX_RECORD)
            with PrivateDirectory(self.directory/'selections'):pass
            with PrivateDirectory(self.directory/'preparation'):pass
        self.driver=DevelopmentPoolDriver(self.directory/'selections',connection_file=self.job['connection_file'],
            owner_id=self.service.owner_id,agent_id=self.job['agent_id'],board_endpoint=self.service.endpoint)
        self.pool=self.resources.enter_context(closing(WorkerPool(self.directory/'pool',concurrency=1,ceiling=1,
            drivers={'development':self.driver.binding()})))
        self.control.add_pool(self.pool);self.resources.callback(self.control.remove_pool,self.pool)
        if create:
            submitted=self.pool.submit(request_id='continuation-'+self.generation,driver_id='development',
                payload={key:self.job[key] for key in ('project_id','ticket_id')})
            with self.pool.mutex:
                self.submitted=deepcopy(self.pool._job(self.pool.journal.read(),submitted['id']))
        else:
            with self.pool.mutex:
                current=self.pool._job(self.pool.journal.read(),self.submitted['id'])
                require(all(current[key]==self.submitted[key] for key in
                    ('id','worker_id','session_id','driver_id','driver_revision','payload','request_id')))
                require((current['state']=='queued' and not current['stop_requested']) or
                    (current['state']=='cancelled' and current['stop_requested'] is True
                        and current['reason']=='stop_before_dispatch' and current['execution_active'] is False))
                # Historical stopped preparation metadata stays stopped. It
                # grants nothing; clean Core/checkpoint/retirement + Runtime
                # proofs must still pass before a NEW queued generation.
        self.operation=self.submitted['id']
        self.context=self.pool._context(self.submitted if create else deepcopy(current),create=create)
        if not create:
            for kind,name in (('work','work-journal.sqlite3'),('runs','runs.sqlite3'),('runtime','runtime.sqlite3')):
                file_document(Path(self.context.journals[kind])/name,32*1024*1024)
        self.preparer=LinuxPoolJobPreparer(self.directory/'preparation',pool=self.pool,
            driver=self.driver,workspaces=self.workspaces)
    def prepare(self):
        self.control.guard()
        with _initial_preparing(self.control,self.preparer,self.operation):
            self.preparer.prepare(self.operation,**self.arguments)
        self.control.guard()
    def open_worker(self,*,existing=False,unprepared=False):
        require(not self._worker_open)
        if unprepared:
            bound={'selection':{'scope':{**self.driver.selections.fixed,
                **{key:self.job[key] for key in ('project_id','ticket_id')},'board_session_id':self.context.session_id}},
                'connection':self.driver.selections.connection_pin}
        else:bound=self.driver.selections.load(self.context)
        self.worker=self.driver._open(self.resources,self.context,bound,existing=existing,observe=False)
        self._worker_open=True
        worker_scope={**self.driver.selections.fixed,**{key:self.job[key] for key in ('project_id','ticket_id')},
            'board_session_id':self.context.session_id}
        record=self.control.add_worker(self.worker.journal.directory,self.operation,worker_scope)
        self.resources.callback(self.control.remove_worker,record)
        if existing and not unprepared:self.worker.client.adopt_registration()
        elif not unprepared:
            selection=bound['selection']
            self.worker.prepare(scope=selection['scope'],request_id='continuation-prepare-'+self.operation,
                instruction=bound['instruction'],broker_directory=selection['broker_directory'],
                backend_directory=selection['backend_directory'],planner_policy_directory=selection['planner_policy_directory'],
                model=selection['model'],budgets=PlannerBudgets(**selection['budgets']),operation_id=self.operation,
                **_reasoning_configuration(selection.get('reasoning_effort')))
    def open_task(self,*,create,old=None):
        try:from .development_provider_continuation import DevelopmentProviderContinuation
        except ImportError:fail('continuation_pool_api_unavailable')
        checks=_LiveChecks(self).checks();task_path=self.target_directory/'logical'
        if create:
            op=self.worker.journal.get(self.operation)
            with closing(DevelopmentJournal(op['configuration']['pins']['broker']['path'])) as broker:
                manifest=deepcopy(broker.manifest)
            plan=decode(file_document(Path(op['configuration']['pins']['backend']['path'])/'execution-plan.json',MAX_RECORD)[0])
            tests=_test_caps(manifest,plan)
            caps={'paths':sorted(manifest['files']),'tests':tests,'actions':manifest['actions'],
                'resource':manifest['resource'],'broker_limits':manifest['budgets'],'planner_limits':op['configuration']['budgets']}
            self.checkpoint_configuration={'schema':'private-development-checkpoint-config-v1',
                'task_id':self.target['logical_task_id'],'owner_id':self.service.owner_id,
                **{key:self.job[key] for key in ('project_id','ticket_id','resource')},
                'repository_id':self.grant['repository_id'],'original_public_base':self.job['repository']['base_commit'],
                'global_paths':sorted(self.job['paths']),'global_test_caps_sha256':digest(canonical(tests)),
                'static_caps_sha256':digest(canonical(caps)),'source_release_sha256':self.service.source_sha}
            with PrivateDirectory(self.target_directory) as private:
                _write(private,'checkpoint-configuration.json',self.checkpoint_configuration)
            registration=op['payload']['registration']
            scope={key:op['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id','board_endpoint')}
            scope.update(device_id=registration['identity']['device_id'],profile_version=registration['profile_version'],
                resource=self.job['resource'],source_release_sha256=self.service.source_sha)
            # This initial digest is factual selected-source content, not a
            # test pass or a future grant. All later sources have typed Proofs.
            initial=digest(canonical({path:row['sha256'] for path,row in manifest['files'].items()}))
            self.task=self.resources.enter_context(closing(ContinuationTask.create(task_path,
                logical_task_id=self.target['logical_task_id'],scope=scope,
                cycle_limits=self.service.loaded['cycle_limits'][self.job['name']],
                initial_source_sha256=initial,checks=checks)))
        else:
            with PrivateDirectory(self.target_directory) as private:
                config=_read(private,'checkpoint-configuration.json')
            require(self.checkpoint_configuration in (None,config))
            self.checkpoint_configuration=config
            if old is not None:
                self.task=ContinuationTask(old.task.journal,checks,clock=old.task.clock)
            else:
                journal=self.resources.enter_context(ContinuationJournal.open(task_path))
                self.task=ContinuationTask(journal,checks)
            require(self.task.configuration['logical_task_id']==self.target['logical_task_id']
                and self.task.configuration['cycle_limits']==self.service.loaded['cycle_limits'][self.job['name']]
                and all(self.task.configuration['scope'][key]==self.job[key] for key in
                    ('agent_id','project_id','ticket_id','resource'))
                and self.task.configuration['scope']['owner_id']==self.service.owner_id
                and self.task.configuration['scope']['source_release_sha256']==self.service.source_sha)
        config=self.checkpoint_configuration
        require(config['task_id']==self.target['logical_task_id']
            and config['owner_id']==self.service.owner_id
            and all(config[key]==self.job[key] for key in ('project_id','ticket_id','resource'))
            and config['repository_id']==self.job['repository']['repository_id']
            and config['original_public_base']==self.job['repository']['base_commit']
            and config['global_paths']==sorted(self.job['paths'])
            and config['source_release_sha256']==self.service.source_sha,'continuation_pool_binding_changed')
        self.checkpoints=PrivateDevelopmentCheckpointController(self.workspaces,self.target_directory/'checkpoints',
            configuration=config)
        self.provider=DevelopmentProviderContinuation(self.task,self.worker,self.checkpoints,
            backoff_seconds=self.service.loaded['configuration']['provider_backoff_seconds'])
    def can_wake(self):
        from .provider_disposition import observe_provider_availability,classify_provider_availability
        snapshot=self.task.snapshot();binding=self.provider._wake_binding(snapshot)
        observation=observe_provider_availability(self.service.tools['executable']['path'],binding_sha256=binding,timeout=30)
        candidate=classify_provider_availability(observation,binding_sha256=binding,enabled=True)
        require(self.task.snapshot()==snapshot)
        require(candidate['classification'] in ('available','provider_unavailable'))
        return candidate['classification']=='available'
    def human_handoff(self,error=None):
        """Best-effort closed visibility; never changes the original failure."""
        from .development_worker import DevelopmentBoardWorker
        if (type(self.worker) is not DevelopmentBoardWorker or type(self.task) is not ContinuationTask
                or self._resources_retained):return
        try:
            self.control.local_guard()
            state=self.task.snapshot()['state']
            if state['phase']!='needs_you' or state['stop_requested'] or self.worker._stopped(self.operation):return
            from .continuation_human_handoff import ContinuationHumanHandoff
            code=vars(error).get('code') if type(error) is DevelopmentError else None
            question='permissions' if code in PERMISSION_FAILURES else 'effects'
            self.control.local_guard()
            report=ContinuationHumanHandoff(self.worker,self.task,self.operation).publish(question_code=question)
            require(type(report) is dict)
            common={'schema','operation_id','status','human_question_confirmed','launchable'}
            extra={'request_id'} if report.get('status')=='question_confirmed' else {'message_id'} if report.get('status')=='notice_confirmed' else set()
            require(type(report) is dict and set(report)==common|extra
                and report['schema']=='continuation-human-visibility-v1' and report['operation_id']==self.operation
                and report['status'] in ('not_applicable','pending','notice_confirmed','question_confirmed')
                and type(report['human_question_confirmed']) is bool
                and report['human_question_confirmed']==(report['status']=='question_confirmed')
                and report['launchable'] is False)
            for field in extra:opaque(report[field],'continuation_pool_binding_changed',200)
            if report['status']!='not_applicable':self.control.remember_human(self.target['name'],report)
        except Exception:pass  # The original SDK error/Stop remains authoritative.
    def publish(self):
        # Exact final aggregation API is additive and intentionally mandatory.
        # An old SDK cannot silently publish only the last cycle's delta.
        try:
            from .development_aggregate_delivery import AggregateDevelopmentDeliveryController
            from .development_delivery import DevelopmentDeliveryProof
            from .development_aggregate_review_source import PublishedAggregateReviewSource
            from .continuation_aggregate_review import ContinuationAggregateReviewer
            from .git_publication import GitHubPublicationGrant,GitHubDraftPublisher,REPOSITORY
        except ImportError:fail('continuation_pool_api_unavailable')
        self.control.guard()
        path=self.target_directory/'aggregate';_mkdir(path)
        _mkdir(path/'git-state');_mkdir(path/'checkouts')
        grant=LocalRepositoryGrant(**{**self.job['repository'],'workspace_root':str(path/'checkouts')})
        original=LocalGitWorkspaceController(path/'git-state',grant=grant)
        final=DevelopmentDeliveryProof(self.worker,self.workspaces,self.operation)
        parent=self.task.snapshot()['state']['checkpoint']
        parent_id=None if parent is None else self._parent_checkpoint(parent)
        aggregate=AggregateDevelopmentDeliveryController(path/'delivery',task=self.task,final_proof=final,
            checkpoints=self.checkpoints,parent_checkpoint_id=parent_id,original_workspaces=original,
            final_intent_mode='provider_healthy_v1')
        specification=next(row for row in self.service.loaded['workflow']['jobs'] if row['name']==self.job['name'])
        aggregate.seal(message=specification['commit_message']);self.control.guard()
        # Preserve the workflow's narrower publication selection even if the
        # approved code manifest contained additional readable/editable paths.
        with aggregate._locked() as private:
            record=aggregate._read(private);aggregate._verified_seal(record)
            changes=record['capture']['request']['changes']
            require(all(row['path'] in specification['paths'] for row in changes),
                'continuation_pool_binding_changed')
        aggregate.complete_local();self.control.guard()
        _mkdir(path/'publications')
        publication_grant=GitHubPublicationGrant(self.service.owner_id,REPOSITORY,'main',self.job['repository']['base_commit'])
        publisher=GitHubDraftPublisher(aggregate.deliveries,path/'publications',grant=publication_grant,
            gh_executable=self.service.tools['gh']['path'],python_executable=self.service.tools['interpreter']['path'],
            authority=aggregate.publication_authority(publication_grant))
        published=aggregate.publish_and_handoff(publisher,title=specification['pr_title'],body=specification['pr_body'])
        require(published['status']=='awaiting_independent_review')
        self.control.guard()
        source=PublishedAggregateReviewSource(aggregate,publisher)
        reviewer=ContinuationAggregateReviewer(self.target_directory/'independent-review',source=source,
            configuration_path=self.service.path,root=self.service.root,owner_id=self.service.owner_id,
            board_endpoint=self.service.endpoint,name=self.job['name'],executable=self.service.tools['executable']['path'],
            cli_version=self.service.cli_version,interpreter=self.service.tools['interpreter']['path'],
            deadline=self.control.deadline,max_seconds=self.service.max_seconds)
        self.control.add_reviewer(reviewer);review_confirmed=False
        try:
            opinion=reviewer.run(should_stop=self.control.stopping)
            require(opinion.get('status')=='complete')
            receipt=reviewer.historical_receipt_pin()
            require(receipt['review']==opinion and receipt['source_operation_id']==self.operation
                and receipt['logical_task_id']==self.target['logical_task_id']
                and opinion['delivery_sha']==source.receipt['delivery_sha'],'continuation_pool_binding_changed')
            review_confirmed=True
        finally:
            if reviewer.pending or not review_confirmed:
                self.control.stop_failed=True
                self._resources_retained=True
                self.control.service._retained_resources.append((reviewer,self.resources.pop_all()))
                self.control.stop()
            else:self.control.remove_reviewer(reviewer)
        require(not reviewer.pending,'continuation_pool_cleanup_unknown')
        self.control.guard()
        return {'status':'independent_review_published','review_receipt':receipt}
    def _parent_checkpoint(self,parent):
        # Current generation has no private checkpoint when it returned final.
        # The parent is pinned before its construction, not found by scanning.
        with PrivateDirectory(self.directory) as private:value=_read(private,'parent.json')
        require(value['checkpoint_sha256']==parent['diff_sha256'])
        self.checkpoints.verify(value['checkpoint_id'])
        return value['checkpoint_id']
