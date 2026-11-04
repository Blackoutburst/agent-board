"""Explicit new review of a sealed historical delivery, then normal queue work.

The installed continuation unit is the execution boundary. Planning only reads
operator documents. A durable pending attempt never dispatches again. Only an
actually published independent opinion and fresh cleanup allow the separate
queue acknowledgement; old Core/source/Runtime records are never repaired.
"""
from contextlib import contextmanager
from copy import deepcopy
import ctypes
import fcntl
import os
from pathlib import Path
import time

from .continuation_pool_configuration import load_queue_continuation
from .development_pool_selection import file_document,job_identity
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, identity, opaque, sha256, signature
from .finite_pool_batch import child
from .pool_journal import uuid_value
from .worker_pool import WorkerPool, PoolOutcome

SCHEMA='linux-cloud-review-recovery-v1'
FIELDS={'schema','recovery_id','logical_task_id','source_operation_id','prior_review_job_id',
    'expected_source_sha256','delivery_sha','ticket_version','revocation_event_id','owner_id',
    'project_id','ticket_id','reviewer_agent_id','connection_file','model','timeout','skills',
    'instruction','next_configuration_file','next_configuration_sha256'}
MAX_RECORD=512*1024


def require(value,code='review_recovery_changed'):
    if not value:raise DevelopmentError(code)


def load_review_recovery(path,*,root,owner_id,board_endpoint,max_seconds):
    """Inert operator scope; no source, journal, connection, registration or RPC."""
    root=Path(root);path=child(path,root/'config')
    raw,pin=file_document(path,MAX_RECORD);value=decode(raw)
    if type(value) is not dict or value.get('schema')!=SCHEMA:return None
    require(set(value)==FIELDS,'review_recovery_configuration_invalid')
    for key in ('recovery_id','logical_task_id','source_operation_id','prior_review_job_id'):uuid_value(value[key])
    require(len({value[k] for k in ('recovery_id','logical_task_id','source_operation_id','prior_review_job_id')})==4)
    for key in ('expected_source_sha256','next_configuration_sha256'):sha256(value[key])
    require(type(value['delivery_sha']) is str and len(value['delivery_sha'])==40)
    sha256(value['delivery_sha']+'0'*24)
    for key in ('owner_id','project_id','ticket_id','reviewer_agent_id','model'):opaque(value[key])
    require(value['owner_id']==owner_id and value['connection_file']==str(root/'connections/reviewer/codex.json'))
    for key in ('ticket_version','revocation_event_id'):
        require(type(value[key]) is int and 1<=value[key]<2**63)
    require(type(value['timeout']) is int and 1<=value['timeout']<=min(300,max_seconds))
    require(type(value['instruction']) is str and value['instruction'].strip()
        and '\x00' not in value['instruction'] and len(value['instruction'].encode())<=12000)
    # Historical admission and lifecycle must register identical capabilities.
    require(value['skills']==['review'],'review_recovery_skills_changed')
    next_path=child(value['next_configuration_file'],root/'config')
    require(next_path!=path)
    following=load_queue_continuation(next_path,root=root,owner_id=owner_id,
        board_endpoint=board_endpoint,max_seconds=max_seconds)
    require(following is not None,'review_recovery_next_queue_required')
    _,pins=following
    require(pins['configuration']['sha256']==value['next_configuration_sha256'])
    return value,{'configuration':pin,**{'next_'+k:v for k,v in pins.items()}}


def review_recovery_plan(path,**scope):
    loaded=load_review_recovery(path,**scope)
    if loaded is None:return None
    value,pins=loaded
    return {**{k:value[k] for k in ('schema','recovery_id','logical_task_id','source_operation_id',
        'prior_review_job_id','delivery_sha','ticket_version','revocation_event_id','reviewer_agent_id','model','timeout')},
        'status':'plan','configuration_sha256':pins['configuration']['sha256'],
        'next_configuration_sha256':value['next_configuration_sha256'],
        'activated':False,'launchable':False,'execution_qualified':False}


def _rename_noreplace(parent,source,destination):
    """Linux atomic same-parent archive; never replace any earlier history."""
    libc=ctypes.CDLL(None,use_errno=True)
    try:rename=libc.renameat2
    except AttributeError:raise DevelopmentError('review_recovery_atomic_archive_required') from None
    rename.argtypes=(ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint)
    rename.restype=ctypes.c_int
    require(rename(parent,os.fsencode(source),parent,os.fsencode(destination),1)==0,
        'review_recovery_archive_refused')
    os.fsync(parent)


class ReviewRecoveryService:
    def __init__(self,root,*,configuration_path,owner_id,board_endpoint,executable,cli_version,
                 interpreter,unshare,gh_executable,max_seconds):
        self.root=Path(root);self.path=Path(configuration_path)
        self.scope=dict(root=self.root,owner_id=owner_id,board_endpoint=board_endpoint,max_seconds=max_seconds)
        self.value,self.pins=load_review_recovery(self.path,**self.scope)
        self.directory=self.root/'review-recoveries'/self.value['recovery_id']
        self.target=self.root/'continuations'/digest(canonical(dict(owner_id=owner_id,
            logical_task_id=self.value['logical_task_id'])))
        self.tools=dict(executable=executable,cli_version=cli_version,interpreter=interpreter,
            unshare=unshare,gh_executable=gh_executable,max_seconds=max_seconds)
        self.signatures={pin['path']:signature(os.stat(pin['path'],follow_symlinks=False)) for pin in self.pins.values()}
        self.pool=self.driver=self.source=self.lifecycle=None;self.job=None

    def _pins(self):
        require(load_review_recovery(self.path,**self.scope)==(self.value,self.pins))

    @contextmanager
    def _registry(self):
        with PrivateDirectory(self.root/'review-recoveries',create=True) as parent:
            try:os.mkdir(self.directory.name,0o700,dir_fd=parent.fd);os.fsync(parent.fd)
            except FileExistsError:pass
        with PrivateDirectory(self.directory) as private:
            fd=os.open('owner.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=private.fd)
            try:
                private.validate_file(fd)
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise DevelopmentError('review_recovery_busy') from None
                yield private
            finally:os.close(fd)

    @contextmanager
    def _old_queue(self):
        source=file_document(self.target/'aggregate/delivery/review-source/source.json',MAX_RECORD)
        require(source[1]['sha256']==self.value['expected_source_sha256'])
        receipt=decode(source[0])['receipt'];author=receipt['author_scope']
        require(receipt['operation_id']==self.value['source_operation_id']
            and receipt['delivery_sha']==self.value['delivery_sha'] and author['owner_id']==self.value['owner_id'])
        key=digest(canonical({k:author[k] for k in ('owner_id','agent_id')}))
        name='queue-'+key
        with PrivateDirectory(self.root/'admissions') as parent,PrivateDirectory(parent.path/name) as private:
            fd=os.open('tick.lock',os.O_RDWR|os.O_NOFOLLOW,dir_fd=private.fd)
            try:
                private.validate_file(fd)
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise DevelopmentError('review_recovery_queue_busy') from None
                lock_pin=identity(os.fstat(fd));raw=private.read('tick.json',MAX_RECORD);state=decode(raw)
                active=state['active'];request=uuid_value(active['request_id'])
                require(state['schema']=='development-queue-tick-state-v1' and state['phase']=='active'
                    and active['logical_task_id']==self.value['logical_task_id'])
                configuration=self.root/'config'/('queue-continuation-'+request+'.json')
                document,config_pin=file_document(configuration,MAX_RECORD)
                require(config_pin==active['configuration_pin'])
                tasks=decode(document)['tasks'];require(len(tasks)==1 and tasks[0]['logical_task_id']==active['logical_task_id'])
                admission,admission_pin=file_document(parent.path/request/'complete.json',MAX_RECORD)
                require(digest(admission)==active['admission_sha256'])
                admitted=decode(admission)
                require(admitted['request_id']==request and admitted['status']=='admitted')
                batch_path=self.root/'config'/('queue-admission-'+request+'.json')
                batch_raw,batch_pin=file_document(batch_path,MAX_RECORD)
                require(admitted['batch_file']==str(batch_path) and admitted['batch_pin']==batch_pin)
                batch=decode(batch_raw);require(len(batch['jobs'])==1 and batch['owner_id']==author['owner_id'])
                require(all(batch['jobs'][0][k]==author[k] for k in ('agent_id','project_id','ticket_id')))
                control_pins=[source[1],config_pin,admission_pin,batch_pin]
                def held():
                    private.verify();parent.verify()
                    require(identity(os.stat('tick.lock',dir_fd=private.fd,follow_symlinks=False))==lock_pin
                        and private.read('tick.json',MAX_RECORD)==raw)
                    require(all(file_document(p['path'],MAX_RECORD)[1]==p for p in control_pins))
                held();yield parent,private,name,dict(active=deepcopy(active),tick_sha256=digest(raw)),held
            finally:os.close(fd)

    def _start_review(self,private):
        from .historical_review_source import HistoricalAggregateReviewSource
        from .review_pool import DeliveryReviewPoolDriver
        from .review_lifecycle import DeliveryReviewLifecycle
        value=self.value;owner=self.scope['owner_id'];endpoint=self.scope['board_endpoint']
        self.driver=DeliveryReviewPoolDriver(self.directory/'selections',connection_file=value['connection_file'],
            owner_id=owner,agent_id=value['reviewer_agent_id'],board_endpoint=endpoint)
        self.pool=WorkerPool(self.directory/'pool',concurrency=1,ceiling=1,drivers={'review':self.driver.binding()})
        self.job=self.pool.submit(request_id='review-recovery-'+value['recovery_id'],driver_id='review',
            payload={k:value[k] for k in ('project_id','ticket_id')})
        private.replace('registry.json',canonical(dict(self.state,job=self.job)))
        self.state['job']=deepcopy(self.job)
        self.source=HistoricalAggregateReviewSource(self.directory/'historical-source',
            prior_review_directory=self.target/'independent-review',prior_job_id=value['prior_review_job_id'],
            source_directory=self.target/'aggregate/delivery/review-source',publication_root=self.target/'aggregate/publications',
            workspace_state_root=self.target/'aggregate/git-state',git_executable='/usr/bin/git',
            expected_source_sha256=value['expected_source_sha256'],expected_delivery_sha=value['delivery_sha'],
            expected_ticket_version=value['ticket_version'],revocation_event_id=value['revocation_event_id'],
            job=self.job,connection_file=value['connection_file'],owner_id=owner,agent_id=value['reviewer_agent_id'],
            project_id=value['project_id'],ticket_id=value['ticket_id'],board_endpoint=endpoint,
            gh_executable=self.tools['gh_executable'],interpreter=self.tools['interpreter'])
        self.lifecycle=DeliveryReviewLifecycle(self.directory/'lifecycle',pool=self.pool,driver=self.driver,source=self.source,
            executable=self.tools['executable'],cli_version=self.tools['cli_version'],interpreter=self.tools['interpreter'])
        self._pins()
        prepared=self.lifecycle.prepare(self.job,source_operation_id=value['source_operation_id'],
            instruction=value['instruction'],material=self.source.material(),model=value['model'],
            timeout=value['timeout'],skills=value['skills'])
        require(prepared.get('status')=='prepared','review_recovery_preparation_pending')

    def _clean(self):
        status=self.pool.status();require(not status['live_threads'] and not status['capacity_held']
            and len(status['jobs'])==1,'review_recovery_cleanup_unconfirmed')
        job=status['jobs'][0]
        require(job['id']==self.job['id'] and job['state']=='settled' and job['outcome']=='complete'
            and job['cleanup_confirmed'] is True and job['execution_active'] is False,'review_recovery_cleanup_unconfirmed')
        with self.pool.mutex:job=deepcopy(self.pool._job(self.pool.journal.read(),job['id']))
        context=self.pool._context(job,create=False)
        require(self.driver._verify_cleanup(context,PoolOutcome(job['id'],job['session_id'],'complete',True)) is True,
            'review_recovery_cleanup_unconfirmed')

    def _proof(self):
        job=self.state['job'];worker=self.directory/'pool/workers'/job['worker_id']
        review=self.target/'aggregate/delivery/reviews'/job['id']
        paths=[self.directory/'lifecycle'/job['id']/'published.json',self.directory/'historical-source/admission.json',
            self.target/'aggregate/delivery/review-source/source.json',self.directory/'selections'/(job['id']+'.json'),
            review/'review.json',worker/'queue/review-result.json']
        paths += [worker/k/n for k,n in (('work','work-journal.sqlite3'),('runs','runs.sqlite3'),('runtime','runtime.sqlite3'))]
        from .git_delivery import _inventory
        return dict(files=[file_document(p,32*1024*1024)[1] for p in paths],
            export_inventory_sha256=_inventory(review/'export',readonly=True)[1])

    def _next(self,receipt,should_stop):
        from .continuation_pool_service import ContinuationPoolService
        self._pins()
        result=ContinuationPoolService(self.root,configuration_path=self.value['next_configuration_file'],
            owner_id=self.scope['owner_id'],board_endpoint=self.scope['board_endpoint'],**self.tools).run_once(should_stop=should_stop)
        return {**result,'review_recovery':receipt}

    def run_once(self,should_stop=None):
        deadline=time.monotonic()+self.tools['max_seconds']
        def stopping():
            require(all(signature(os.stat(p,follow_symlinks=False))==s for p,s in self.signatures.items()))
            return bool(time.monotonic()>=deadline or should_stop and should_stop())
        self._pins()
        with self._registry() as private:
            try:os.stat('registry.json',dir_fd=private.fd,follow_symlinks=False)
            except FileNotFoundError:self.state=None
            else:self.state=decode(private.read('registry.json',MAX_RECORD))
            binding=dict(configuration=self.pins,request=self.value)
            if self.state is not None:
                require(type(self.state) is dict and set(self.state)=={'binding','phase','job','result','proof','queue_ack_sha256'}
                    and self.state['binding']==binding and self.state['phase'] in ('pending','complete'))
                if self.state['phase']!='complete':return dict(status='needs_review',reason='review_recovery_pending',activated=False)
                self.job=self.state['job'];job_identity(self.job)
                require(self._proof()==self.state['proof'])
                ack=private.read('queue-ack.json',MAX_RECORD)
                require(digest(ack)==self.state['queue_ack_sha256'])
                archive=self.root/'admissions'/('completed-by-review-'+self.value['recovery_id'])
                require(digest(file_document(archive/'tick.json',MAX_RECORD)[0])==decode(ack)['queue']['tick_sha256'])
                return self._next({**self.state['result'],'historical':True,'accepted':False},stopping)
            if stopping():return dict(status='stopped',activated=False)
            self.state=dict(binding=binding,phase='pending',job=None,result=None,proof=None,queue_ack_sha256=None)
            private.create('registry.json',canonical(self.state))
            try:
                with self._old_queue() as (parent,old,name,queue,held):
                    self._start_review(private);held()
                    require(not stopping(),'review_recovery_stopped')
                    require(self.pool.dispatch_pending()==[self.job['id']])
                    while self.pool.status()['live_threads']:
                        if stopping():
                            self.lifecycle.stop(self.job['id']);self.pool.shutdown(timeout=30)
                            raise DevelopmentError('review_recovery_stopped')
                        time.sleep(.05)
                    self._pins();held();self._clean()
                    result=self.lifecycle.publish(self.job['id'])
                    require(result['status']=='complete' and result['delivery_sha']==self.value['delivery_sha'])
                    self._pins();held();self._clean()
                    ack=dict(schema='historical-review-queue-ack-v1',recovery_id=self.value['recovery_id'],
                        logical_task_id=self.value['logical_task_id'],source_operation_id=self.value['source_operation_id'],
                        prior_review_job_id=self.value['prior_review_job_id'],queue=queue,review=deepcopy(result),accepted=False)
                    require(not stopping(),'review_recovery_stopped')
                    proof=self._proof();raw=canonical(ack);private.create('queue-ack.json',raw);held()
                    destination='completed-by-review-'+self.value['recovery_id']
                    _rename_noreplace(parent.fd,name,destination)
                    require(identity(os.stat(destination,dir_fd=parent.fd,follow_symlinks=False))==old.identity,
                        'review_recovery_archive_changed')
                self.state.update(phase='complete',result=result,proof=proof,queue_ack_sha256=digest(raw))
                private.replace('registry.json',canonical(self.state))
            finally:
                if self.pool is not None:self.pool.close()
            return self._next({**result,'historical':False,'accepted':False},stopping)
