"""One explicit independent review of an already published aggregate.

The source's author resources stay open until this call and all reviewer threads
have finished. A pending registry is uncertainty, never permission to retry work.
"""
from contextlib import ExitStack
from copy import deepcopy
import difflib
import math
import os
from pathlib import Path
import re
import threading
import time

from .continuation_pool_configuration import load_continuation
from .development_aggregate_review_source import PublishedAggregateReviewSource
from .development_pool_selection import file_document
from .dev_journal import PrivateDirectory,decode
from .dev_manifest import DevelopmentError,canonical,digest,check_content
from .dev_planner import _directory_pin
from .git_workspace import MAX_CHECKOUT_FILE_BYTES
from .git_publication import REPOSITORY,HOST
from .linux_planner_policy import _executable_pin
from .linux_input_snapshot import _path
from .review_lifecycle import DeliveryReviewLifecycle
from .review_pool import DeliveryReviewPoolDriver
from .worker_pool import WorkerPool,PoolOutcome

MAX_RECORD=128*1024


def require(value,code='continuation_review_changed'):
    if not value:raise DevelopmentError(code)


class ContinuationAggregateReviewer:
    """Constructor only reads selected operator documents and local pins."""
    def __init__(self,directory,*,source,configuration_path,root,owner_id,board_endpoint,name,
                 executable,cli_version,interpreter,deadline,max_seconds):
        require(type(source) is PublishedAggregateReviewSource,'continuation_review_source_required')
        require(type(max_seconds) is int and 1<=max_seconds<=7200
            and type(deadline) in (int,float) and math.isfinite(deadline) and deadline>0)
        self.source=source;self.directory=Path(_path(os.fspath(directory)));self.root=Path(_path(os.fspath(root)))
        self.parent_pin=_directory_pin(self.directory.parent)
        self.path=Path(configuration_path);self.owner_id,self.endpoint=owner_id,board_endpoint
        self.loaded,self.pins=load_continuation(self.path,root=self.root,owner_id=owner_id,
            board_endpoint=board_endpoint,max_seconds=max_seconds)
        matches=[item for item in self.loaded['configuration']['tasks'] if item['name']==name]
        require(len(matches)==1)
        self.task=deepcopy(matches[0]);self.name=name
        self.coder=deepcopy(next(j for j in self.loaded['batch']['jobs'] if j['name']==name))
        self.workflow=deepcopy(next(j for j in self.loaded['workflow']['jobs'] if j['name']==name))
        self.reviewer=self.workflow['reviewer'];self.max_seconds=max_seconds;self.deadline=float(deadline)
        self.executable=str(Path(executable).resolve(strict=True));self.interpreter=str(Path(interpreter).resolve(strict=True))
        self.cli_version=cli_version
        require(type(cli_version) is str and re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}',cli_version) is not None)
        self.tool_pins={p:_executable_pin(p) for p in (self.executable,self.interpreter)}
        self.pid,self.thread=os.getpid(),threading.current_thread()
        self.pool=self.lifecycle=self.job=None;self.pending=False;self.stopped=threading.Event()
        self._selection()
        self.admission_digest=digest(canonical({'request':self._request(),'deadline':self.deadline}))

    def _selection(self):
        source=self.source;receipt=source.receipt;scope=receipt['author_scope'];coder=self.coder
        require(receipt['status']=='complete' and receipt['accepted'] is False
            and receipt['operation_id']==source.operation_id
            and self.task['logical_task_id']==source.aggregate.task.configuration['logical_task_id']
            and scope['owner_id']==self.owner_id and scope['board_endpoint']==self.endpoint
            and all(scope[key]==coder[key] for key in ('agent_id','project_id','ticket_id'))
            and source.grant['owner_id']==self.owner_id
            and receipt['original_public_base']==coder['repository']['base_commit']
            and source.workspaces.grant['repository_id']==coder['repository']['repository_id']
            and self.reviewer['agent_id']!=scope['agent_id']
            and self.reviewer['connection_file']!=coder['connection_file'],
            'continuation_review_selection_changed')
        require(source.grant['repository']==REPOSITORY and source.grant['host']==HOST
            and source.grant['base_commit']==coder['repository']['base_commit']
            and receipt['pull_request']['head_sha']==receipt['delivery_sha']
            and receipt['pull_request']['base_sha']==coder['repository']['base_commit']
            and receipt['pull_request']['draft'] is True,'continuation_review_publication_changed')
        changes=source.frame['capture']['request']['changes']
        require(changes and all(row['path'] in self.workflow['paths'] for row in changes),
            'continuation_review_paths_changed')
        forbidden=[source.directory,source.aggregate.directory,source.workspaces.repository,
            source.workspaces.workspace_root,source.workspaces.state_root]
        require(not any(self.directory.is_relative_to(p) or Path(p).is_relative_to(self.directory) for p in forbidden),
            'continuation_review_roots_overlap')

    def _fresh(self):
        require(self.pid==os.getpid() and self.thread is threading.current_thread(),
            'continuation_review_owner_changed')
        require(digest(canonical({'request':self._request(),'deadline':self.deadline}))==self.admission_digest,
            'continuation_review_configuration_changed')
        require(_directory_pin(self.directory.parent)==self.parent_pin)
        loaded,pins=load_continuation(self.path,root=self.root,owner_id=self.owner_id,
            board_endpoint=self.endpoint,max_seconds=self.max_seconds)
        require(loaded==self.loaded and pins==self.pins,'continuation_review_configuration_changed')
        for p,pin in self.tool_pins.items():require(_executable_pin(p)==pin,'continuation_review_tools_changed')
        self._selection()
        return self.source.status(self.source.operation_id)

    def _request(self):
        return {'schema':'continuation-aggregate-review-v1','logical_task_id':self.task['logical_task_id'],
            'source_operation_id':self.source.operation_id,'publication_sha256':digest(canonical(self.source.receipt)),
            'source_frame_sha256':self.source.frame_sha256,'directory':str(self.directory),
            'parent_pin':self.parent_pin,'configuration_pins':self.pins,'name':self.name,
            'reviewer':deepcopy(self.reviewer),'executable':self.executable,'interpreter':self.interpreter,
            'tool_pins':self.tool_pins,'cli_version':self.cli_version,'max_seconds':self.max_seconds}

    @staticmethod
    def _read(private):
        raw=private.read('continuation-review.json',MAX_RECORD);value=decode(raw)
        require(canonical(value)==raw and type(value) is dict
            and set(value)=={'request','phase','job','result','pins'}
            and value['phase'] in ('preparing','running','complete'))
        return value

    def _material(self,receipt):
        source=self.source;record=source.frame['delivery'];snapshot=source.frame['capture']
        require(record['phase']=='sealed' and record['seal']['delivery_sha']==receipt['delivery_sha'])
        entries={row['path']:row for row in snapshot['files']}
        chunks=['Delivery '+receipt['delivery_sha']+'\nSnapshot '+receipt['snapshot_sha256']+'\n']
        for change in snapshot['request']['changes']:
            row=entries[change['path']]
            after=file_document(source.paths['deliveries']/source.operation_id/row['payload'],MAX_CHECKOUT_FILE_BYTES)[0]
            before=source.workspaces._git(['show',source.grant['base_commit']+':'+change['path']],cwd=source.workspaces.repository)
            require(digest(after)==change['sha256'] and row['mode']==change['mode'])
            chunks.append(''.join(difflib.unified_diff(check_content(before).splitlines(keepends=True),
                check_content(after).splitlines(keepends=True),fromfile='a/'+change['path'],tofile='b/'+change['path'])))
        material='\n'.join(chunks);require(len(material.encode())<=24000,'continuation_review_material_too_large')
        require(source.status(source.operation_id)==receipt)
        return material

    def stop(self):
        """Control-only cancellation; never a permission or cleanup assertion."""
        self.stopped.set();self.pending=True
        if self.lifecycle is not None and self.job is not None:self.lifecycle.stop(self.job['id'])
        elif self.pool is not None:self.pool.stop()

    def historical_receipt_pin(self):
        """Dated correlation for the service; never reconstructs a source/permit."""
        with PrivateDirectory(self.source.directory) as private:value=self._read(private)
        require(value['phase']=='complete' and value['request']==self._request()
            and type(value['result']) is dict and value['result'].get('status')=='complete',
            'continuation_review_pending')
        return {'schema':'continuation-independent-review-receipt-v1','status':'published',
            'logical_task_id':self.task['logical_task_id'],'source_operation_id':self.source.operation_id,
            'review_operation_id':value['job']['id'],'binding_sha256':digest(canonical(value['request'])),
            'ledger_pin':file_document(self.source.directory/'continuation-review.json',MAX_RECORD)[1],
            'review':deepcopy(value['result']),'accepted':False,'launchable':False}

    def _clean(self):
        status=self.pool.status();require(len(status['jobs'])==1 and status['jobs'][0]['id']==self.job['id'])
        job=status['jobs'][0]
        require(not status['live_threads'] and not status['capacity_held']
            and job['state']=='settled' and job['outcome']=='complete'
            and job['execution_active'] is False and job['cleanup_confirmed'] is True,
            'continuation_review_cleanup_unknown')
        with self.pool.mutex:job=deepcopy(self.pool._job(self.pool.journal.read(),job['id']))
        context=self.pool._context(job,create=False)
        require(self.driver._verify_cleanup(context,PoolOutcome(job['id'],job['session_id'],'complete',True)) is True,
            'continuation_review_cleanup_unknown')

    def _open(self):
        self.driver=DeliveryReviewPoolDriver(self.directory/'selections',connection_file=self.reviewer['connection_file'],
            owner_id=self.owner_id,agent_id=self.reviewer['agent_id'],board_endpoint=self.endpoint)
        self.pool=WorkerPool(self.directory/'pool',concurrency=1,ceiling=1,drivers={'review':self.driver.binding()})
        self.lifecycle=DeliveryReviewLifecycle(self.directory/'lifecycle',pool=self.pool,driver=self.driver,source=self.source,
            executable=self.executable,cli_version=self.cli_version,interpreter=self.interpreter)

    def run(self,should_stop=None):
        require(should_stop is None or callable(should_stop))
        receipt=self._fresh();request=self._request()
        # Workflow/pass time includes preparation and publication. The exact
        # reviewer timeout remains the separate single-model ceiling below.
        end=min(self.deadline,time.monotonic()+self.max_seconds,
            time.monotonic()+self.loaded['batch']['timeout_seconds'])
        def guard():
            try:cancel=bool(should_stop and should_stop())
            except BaseException:cancel=True
            require(not cancel and not self.stopped.is_set() and time.monotonic()<end,'continuation_review_stopped')
        with PrivateDirectory(self.source.directory) as source_private:
            try:os.stat('continuation-review.json',dir_fd=source_private.fd,follow_symlinks=False)
            except FileNotFoundError:value=None
            else:value=self._read(source_private)
            if value is not None:
                require(value['request']==request,'continuation_review_registry_conflict')
                require(value['phase']=='complete','continuation_review_pending')
                for p,pin in value['pins'].items():
                    actual=_directory_pin(p) if type(pin) is dict and set(pin)=={'path','identity'} else file_document(p)[1]
                    require(actual==pin,'continuation_review_history_changed')
                self._open()
                try:
                    self.job=value['job'];self.driver.attach_source(self.job['id'],self.source)
                    self._clean();result=self.lifecycle.status(self.job['id'])
                    require(result==value['result'] and result['status']=='complete')
                    require(self._fresh()==receipt)
                    return deepcopy(result)
                finally:self.pool.close()
            guard()
            require(not os.path.lexists(self.directory),'continuation_review_directory_exists')
            value={'request':request,'phase':'preparing','job':None,'result':None,'pins':{}}
            source_private.create('continuation-review.json',canonical(value))
            with PrivateDirectory(self.directory.parent) as parent:
                require(_directory_pin(self.directory.parent)==self.parent_pin)
                os.mkdir(self.directory.name,0o700,dir_fd=parent.fd);os.fsync(parent.fd)
            self._open();done=threading.Event();stop_errors=[]
            def monitor():
                while not done.wait(.05):
                    try:guard()
                    except BaseException:
                        try:self.stop()
                        except BaseException:stop_errors.append(True)
                        return
            thread=threading.Thread(target=monitor,daemon=True)
            try:
                material=self._material(receipt);guard()
                key='continuation-review-'+digest(canonical([self.task['logical_task_id'],receipt['publication_request_sha256']]))
                self.job=self.pool.submit(request_id=key,driver_id='review',payload={k:self.coder[k] for k in ('project_id','ticket_id')})
                value.update(job=deepcopy(self.job));source_private.replace('continuation-review.json',canonical(value))
                thread.start();guard();self._fresh()
                prepared=self.lifecycle.prepare(self.job,source_operation_id=self.source.operation_id,
                    instruction=self.reviewer['instruction'],material=material,model=self.reviewer['model'],
                    timeout=self.reviewer['timeout'],skills=self.reviewer['skills'])
                require(prepared.get('status')=='prepared','continuation_review_preparation_unknown')
                guard();self._fresh();value['phase']='running';source_private.replace('continuation-review.json',canonical(value))
                require(self.pool.dispatch_pending()==[self.job['id']])
                while self.pool.status()['live_threads']:
                    guard();time.sleep(.05)
                guard();self._clean();self._fresh();guard()
                result=self.lifecycle.publish(self.job['id'])
                require(result['status']=='complete');guard();self._clean();require(self._fresh()==receipt)
                paths=[self.directory,self.directory/'selections',self.directory/'lifecycle',self.directory/'pool',self.directory/'pool/workers']
                pins={str(p):_directory_pin(p) for p in paths}
                for p in (self.directory/'pool/pool-state.json',self.directory/'pool/pool-owner.lock'):
                    pins[str(p)]=file_document(p)[1]
                value.update(phase='complete',result=deepcopy(result),pins=pins)
                guard();source_private.replace('continuation-review.json',canonical(value));guard()
                return result
            except BaseException:
                self.stop();raise
            finally:
                done.set()
                if thread.ident is not None:thread.join(timeout=1)
                if self.pool.status()['live_threads']:
                    self.pool.shutdown(timeout=30)
                status=self.pool.status()
                self.pending=bool(status['live_threads'] or status['capacity_held'] or status['stop_unconfirmed']
                    or thread.is_alive() or stop_errors)
                if not self.pending:self.pool.close()
