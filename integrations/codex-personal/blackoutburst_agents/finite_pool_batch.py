"""One explicit four-job Linux batch; interrupted batches are never replayed.

This library creates only the existing development driver, pool and preparer.
Installation/UID/source verification belongs to the protected additive entry.
"""
from copy import deepcopy
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading
import time

from .dev_journal import PrivateDirectory,decode
from .dev_manifest import Budgets,DevelopmentError,canonical,digest,identity,opaque,relative_path,sha256
from .dev_planner import _directory_pin, _reasoning_configuration
from .dev_planner_journal import PlannerBudgets
from .development_pool import DevelopmentPoolDriver
from .development_pool_selection import file_document
from .git_workspace import LocalGitWorkspaceController,LocalRepositoryGrant
from .linux_input_snapshot import _path
from .linux_planner_policy import _executable_pin,linux_source_pins
from .linux_pool_preparation import LinuxPoolJobPreparer
from .pool_journal import uuid_value,identifier
from .worker_pool import WorkerPool

SCHEMA='linux-finite-pool-batch-v1'
FLEXIBLE_SCHEMA='linux-finite-pool-batch-v2'
MAX_BATCH=128*1024
JOB_FIELDS={'name','agent_id','connection_file','repository','project_id','ticket_id',
    'paths','resource','actions','verifier_path','verifier_sha256','test_name',
    'test_timeout_seconds','test_max_output_bytes','instruction','model','planner_budgets','broker_budgets'}


def fail(code='finite_pool_batch_invalid'):
    raise DevelopmentError(code)


def child(path,root):
    value=Path(_path(os.fspath(path)))
    if value==root or not value.is_relative_to(root):fail('finite_pool_path_refused')
    return str(value)


def load_batch(path,*,root,owner_id,board_endpoint,max_seconds):
    raw,pin=file_document(path,MAX_BATCH);value=decode(raw)
    if (type(value) is not dict or set(value)!={
            'schema','batch_id','owner_id','board_endpoint','timeout_seconds','jobs'}
            or value['schema'] not in (SCHEMA,FLEXIBLE_SCHEMA) or value['owner_id']!=owner_id or value['board_endpoint']!=board_endpoint
            or type(value['timeout_seconds']) is not int or not 1<=value['timeout_seconds']<=max_seconds
            or type(value['jobs']) is not list
            or not 1<=len(value['jobs'])<=4 or value['schema']==SCHEMA and len(value['jobs'])!=4):fail()
    uuid_value(value['batch_id']);names=set();agents=set();tickets=set();resources=set();connections=set()
    for job in value['jobs']:
        if type(job) is not dict or set(job) not in (JOB_FIELDS,JOB_FIELDS|{'reasoning_effort'}):fail()
        if 'reasoning_effort' in job:
            if job['reasoning_effort'] is None:fail('invalid_planner_reasoning_effort')
            _reasoning_configuration(job['reasoning_effort'])
        identifier(job['name']);opaque(job['agent_id']);opaque(job['project_id']);opaque(job['ticket_id'])
        opaque(job['resource']);opaque(job['model'],'invalid_development_model',128)
        for seen,key in ((names,'name'),(agents,'agent_id'),(tickets,'ticket_id'),(resources,'resource')):
            if job[key] in seen:fail('finite_pool_jobs_not_distinct')
            seen.add(job[key])
        connection=child(job['connection_file'],root/'connections')
        if connection in connections:fail('finite_pool_jobs_not_distinct')
        connections.add(connection)
        child(job['verifier_path'],root/'verifiers');sha256(job['verifier_sha256'])
        repo=job['repository']
        if type(repo) is not dict or set(repo)!={'owner_id','repository_id','repository','workspace_root','base_commit'}:fail()
        grant=LocalRepositoryGrant(**repo).document()
        if grant['owner_id']!=owner_id or grant['workspace_root']!=str(root/'checkouts'/value['batch_id']/job['name']):fail()
        child(grant['repository'],root/'repositories')
        if type(job['paths']) is not list or not 1<=len(job['paths'])<=128 or len(set(job['paths']))!=len(job['paths']):fail()
        for path in job['paths']:relative_path(path)
        if type(job['actions']) is not list:fail()
        if type(job['planner_budgets']) is not dict or type(job['broker_budgets']) is not dict:fail()
        PlannerBudgets(**job['planner_budgets']).document();Budgets(**job['broker_budgets']).document()
    return value,pin


class FiniteLinuxPoolBatch:
    """Protected local operator configuration, no Board/message job selection."""
    def __init__(self,root,*,owner_id,board_endpoint,executable,cli_version,interpreter,unshare,max_seconds=14400):
        self.root=Path(_path(os.fspath(root)));self.owner_id=opaque(owner_id);self.endpoint=board_endpoint
        self.max_seconds=max_seconds;self.cli_version=cli_version
        if type(max_seconds) is not int or not 1<=max_seconds<=14400:fail()
        self.directories={}
        for name in ('','batches','connections','repositories','checkouts','git-state','verifiers','config'):
            path=self.root/name
            with PrivateDirectory(path):pass
            self.directories[str(path)]=_directory_pin(path)
        self.tools={name:_executable_pin(os.fspath(value)) for name,value in
            (('executable',executable),('interpreter',interpreter),('unshare',unshare))}
        self.sources=linux_source_pins();self.pool=None

    def _local(self,path,pin,*,full=False,connections=None):
        for value,expected in self.directories.items():
            with PrivateDirectory(value):pass
            if _directory_pin(value)!=expected:fail('finite_pool_root_changed')
        if file_document(path,MAX_BATCH)[1]!=pin:fail('finite_pool_configuration_changed')
        for name,expected in (connections or {}).items():
            if file_document(name)[1]!=expected:fail('finite_pool_connection_changed')
        if full:
            if linux_source_pins()!=self.sources:fail('finite_pool_source_changed')
            for expected in self.tools.values():
                if _executable_pin(expected['path'])!=expected:fail('finite_pool_tool_changed')

    @contextmanager
    def _locked(self,batch_id):
        uuid_value(batch_id);fresh=False
        with PrivateDirectory(self.root/'batches') as parent:
            try:os.mkdir(batch_id,0o700,dir_fd=parent.fd);os.fsync(parent.fd);fresh=True
            except FileExistsError:pass
        with PrivateDirectory(self.root/'batches'/batch_id) as private:
            fd=None
            try:
                fd=os.open('batch.lock',os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if fresh else 0),0o600,dir_fd=private.fd)
                private.validate_file(fd);pin=identity(os.fstat(fd))
                if fresh:os.fsync(fd);os.fsync(private.fd)
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:fail('finite_pool_batch_busy')
                def held():
                    private.verify();private.validate_file(fd)
                    if identity(os.fstat(fd))!=pin or identity(os.stat('batch.lock',dir_fd=private.fd,follow_symlinks=False))!=pin:fail()
                held();yield private,fresh,held
            except FileNotFoundError:fail('finite_pool_batch_incomplete')
            finally:
                if fd is not None:os.close(fd)

    @staticmethod
    def _record(private,name,value):
        if private.fd is None or identity(os.fstat(private.fd))!=private.identity:fail('finite_pool_record_directory_closed')
        raw=canonical(value)
        if len(raw)>MAX_BATCH:fail('finite_pool_record_limit')
        private.create(name,raw)

    def run(self,batch_file,*,should_stop=None,workflow=None):
        """Explicit first invocation only; existing intent returns history only."""
        path=child(batch_file,self.root/'config')
        value,pin=load_batch(path,root=self.root,owner_id=self.owner_id,board_endpoint=self.endpoint,max_seconds=self.max_seconds)
        if workflow is not None:
            from .cloud_workflow import CloudLinuxWorkflow
            if type(workflow) is not CloudLinuxWorkflow:fail('finite_pool_concrete_workflow_required')
            workflow.validate_batch(value,self.root,self.owner_id,self.endpoint)
        if should_stop is not None and not callable(should_stop):fail()
        connections={job['connection_file']:file_document(job['connection_file'])[1] for job in value['jobs']}
        request={'batch':value,'configuration':pin,'roots':self.directories,'tools':self.tools,
                 'cli_version':self.cli_version,'sources':self.sources,'connections':connections}
        if workflow is not None:request['workflow']=workflow.descriptor()
        request=decode(canonical(request));request_sha=digest(canonical(request))
        self._local(path,pin,full=True,connections=connections)
        with self._locked(value['batch_id']) as (private,fresh,held):
            if not fresh:
                intent=decode(private.read('intent.json',MAX_BATCH))
                if intent!={'request':request,'request_sha256':request_sha}:fail('finite_pool_batch_conflict')
                try:result=decode(private.read('complete.json',MAX_BATCH))
                except DevelopmentError:
                    return {'status':'needs_review','reason':'interrupted_batch','batch_id':value['batch_id'],
                            'request_sha256':request_sha,'historical':True}
                if (type(result) is not dict or result.get('batch_id')!=value['batch_id']
                        or result.get('request_sha256')!=request_sha):fail()
                if workflow is not None and result.get('status')=='complete':
                    completed=decode(private.read('workflow-complete.json',MAX_BATCH))
                    if (result.get('workflow')!=completed or private.read('workflow-intent.json',MAX_BATCH)
                            !=canonical({'request_sha256':request_sha})):fail('finite_pool_workflow_history_changed')
                    workflow.verify_completed(private.path,value,completed,tools=self.tools,cli_version=self.cli_version)
                return {**result,'historical':True}
            self._record(private,'intent.json',{'request':request,'request_sha256':request_sha})
            self._local(path,pin,full=True,connections=connections);held()
            drivers={};pool=None;companion=None;done=threading.Event();stop=threading.Event();monitor_ready=threading.Event();errors=[]
            deadline=time.monotonic()+value['timeout_seconds'];stage='constructing'
            def stopping():
                if stop.is_set():return True
                try:return time.monotonic()>=deadline or bool(should_stop and should_stop())
                except Exception:return True
            def monitor():
                # Its own held directory never becomes dir_fd=None when the
                # main frame closes after a bounded join. Late work is Stop
                # only and cannot write through a closed/reused parent handle.
                try:
                    if done.is_set():return
                    with PrivateDirectory(private.path) as owned:
                        owned.verify();monitor_ready.set()
                        while not done.wait(.05):
                            try:
                                self._local(path,pin,connections=connections);owned.verify()
                                if workflow is not None:workflow.check()
                                if identity(os.fstat(owned.fd))!=owned.identity:fail()
                                requested=stopping()
                            except Exception:requested=True
                            if done.is_set():return
                            if requested:
                                stop.set()
                                try:
                                    self._record(owned,'stop-intent.json',{'batch_id':value['batch_id']})
                                except Exception:errors.append('finite_pool_stop_unconfirmed')
                                # Pool.stop owns its own durable Stop/Event bracket.
                                # A failed auxiliary batch marker cannot suppress it.
                                try:pool.stop()
                                except Exception:errors.append('finite_pool_stop_unconfirmed')
                                if workflow is not None:
                                    try:workflow.stop()
                                    except Exception:errors.append('finite_pool_stop_unconfirmed')
                                return
                except Exception:
                    errors.append('finite_pool_stop_unconfirmed');stop.set()
                    try:pool.stop()
                    except Exception:errors.append('finite_pool_stop_unconfirmed')
                    if workflow is not None:
                        try:workflow.stop()
                        except Exception:errors.append('finite_pool_stop_unconfirmed')
                finally:monitor_ready.set()
            try:
                if stopping():fail('finite_pool_stopped')
                for job in value['jobs']:
                    directory=private.path/('selection-'+job['name'])
                    drivers[job['name']]=DevelopmentPoolDriver(directory,connection_file=job['connection_file'],
                        owner_id=self.owner_id,agent_id=job['agent_id'],board_endpoint=self.endpoint)
                bindings={name:driver.binding() for name,driver in drivers.items()}
                if workflow is not None:bindings.update(workflow.bindings(private.path))
                pool=WorkerPool(private.path/'pool',concurrency=len(value['jobs']),ceiling=len(value['jobs']),drivers=bindings);self.pool=pool
                companion=threading.Thread(target=monitor,name='finite-pool-stop',daemon=False);companion.start()
                if not monitor_ready.wait(timeout=1) or stopping() or errors:fail('finite_pool_stop_unconfirmed')
                submitted=[]
                stage='submitting'
                self._record(private,'submit-intent.json',{'request_sha256':request_sha})
                for job in value['jobs']:
                    if stopping():fail('finite_pool_stopped')
                    submitted.append(pool.submit(request_id=value['batch_id']+':'+job['name'],driver_id=job['name'],
                        payload={key:job[key] for key in ('project_id','ticket_id')}))
                self._record(private,'submitted.json',submitted)
                stage='preparing';prepared=[];readiness=[]
                for parent in (self.root/'git-state',self.root/'checkouts'):
                    os.mkdir(parent/value['batch_id'],0o700)
                    fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                    try:os.fsync(fd)
                    finally:os.close(fd)
                for job,submitted_job in zip(value['jobs'],submitted):
                    if stopping():fail('finite_pool_stopped')
                    self._local(path,pin,full=True,connections=connections);held()
                    state=self.root/'git-state'/value['batch_id']/job['name'];checkout=self.root/'checkouts'/value['batch_id']/job['name']
                    # Exclusive fresh roots: never adopt a prior failed batch.
                    os.mkdir(state,0o700);os.mkdir(checkout,0o700)
                    for parent in (state.parent,checkout.parent):
                        fd=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
                        try:os.fsync(fd)
                        finally:os.close(fd)
                    controller=LocalGitWorkspaceController(state,grant=LocalRepositoryGrant(**job['repository']))
                    preparer=LinuxPoolJobPreparer(private.path/('preparation-'+job['name']),pool=pool,
                        driver=drivers[job['name']],workspaces=controller,driver_id=job['name'])
                    arguments={key:deepcopy(job[key]) for key in JOB_FIELDS-{'name','agent_id','connection_file','repository'}}
                    arguments.update(_reasoning_configuration(job.get('reasoning_effort')))
                    arguments.update(planner_budgets=PlannerBudgets(**job['planner_budgets']),
                        broker_budgets=Budgets(**job['broker_budgets']),cli_version=self.cli_version,
                        **{key:item['path'] for key,item in self.tools.items()})
                    prepared.append(preparer.prepare(submitted_job['id'],**arguments))
                    readiness.append((preparer,submitted_job['id'],arguments,prepared[-1]))
                self._record(private,'prepared.json',prepared)
                stage='dispatching'
                if stopping():fail('finite_pool_stopped')
                self._local(path,pin,full=True,connections=connections);held()
                for preparer,operation,arguments,confirmed in readiness:
                    status=preparer.status(operation)
                    if (status!={'operation_id':operation,'preparation_sha256':confirmed['preparation_sha256'],'status':'prepared'}
                            or preparer.prepare(operation,**arguments)!={**confirmed,'historical':True}):
                        fail('finite_pool_preparation_unconfirmed')
                if stopping():fail('finite_pool_stopped')
                self._record(private,'dispatch-intent.json',{'request_sha256':request_sha,'operations':[job['id'] for job in submitted]})
                pool.dispatch_pending()  # Exactly once, never from reopen/recovery.
                stage='running'
                while pool.status()['live_threads']:
                    if stopping():
                        stop.set();pool.stop();break
                    time.sleep(.05)
                if stopping() or errors:pool.shutdown(timeout=30)
                status=pool.status()
                clean=(status['live_threads']==0 and status['capacity_held']==0
                    and len(status['jobs'])==len(value['jobs']) and all(job['state']=='settled' and job['outcome']=='complete'
                        and job['cleanup_confirmed'] is True for job in status['jobs']))
                workflow_result=None
                if workflow is not None and clean and not stopping() and not errors:
                    stage='delivering'
                    self._local(path,pin,full=True,connections=connections);held();workflow.check(full=True)
                    self._record(private,'workflow-intent.json',{'request_sha256':request_sha})
                    workflow_result=workflow.deliver(pool=pool,drivers=drivers,batch=value,submitted=submitted,
                        tools=self.tools,cli_version=self.cli_version,should_stop=stopping)
                    self._record(private,'workflow-complete.json',workflow_result)
                    clean=workflow_result.get('status')=='complete'
                    status=pool.status()
                done.set()
                companion.join(timeout=1)
                if companion.is_alive():errors.append('finite_pool_stop_unconfirmed')
                result={'batch_id':value['batch_id'],'request_sha256':request_sha,'status':'complete' if clean and not stopping() and not errors else 'needs_review',
                    'reason':('workflow_verified' if workflow is not None else 'private_results_verified') if clean and not stopping() and not errors else 'cleanup_or_result_requires_review',
                    'pool':status,'ticket_published':workflow_result is not None and clean,'historical':False}
                if workflow_result is not None:result['workflow']=workflow_result
                self._record(private,'complete.json',result);return result
            except Exception as error:
                if workflow is not None:
                    try:workflow.stop()
                    except Exception:pass
                if pool is not None:
                    try:pool.shutdown(timeout=30)
                    except Exception:pass
                self._record(private,'failed.json',{'batch_id':value['batch_id'],'request_sha256':request_sha,
                    'stage':stage,'reason':error.code if isinstance(error,DevelopmentError) else 'finite_pool_effect_unconfirmed'})
                raise
            finally:
                done.set()
                if companion is not None:
                    companion.join(timeout=1)
                    if companion.is_alive():errors.append('finite_pool_stop_unconfirmed')
                if pool is not None:
                    try:pool.close()
                    except Exception:pass  # Live workers retain their lease and unknown capacity.
