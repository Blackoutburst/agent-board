"""One protected explicit coder -> draft PR -> independent visible review.

This is local operator configuration, never a queue listener or model-selected
authority. The existing concrete components own each mutation protocol.
"""
from copy import deepcopy
import difflib
from pathlib import Path
import threading
import time

from .development_pool_delivery import DevelopmentPoolResultDelivery
from .development_pool_selection import file_document
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, RepositoryFiles, canonical,
    check_content, digest, opaque, relative_path, validate_manifest)
from .dev_planner import _directory_pin
from .finite_pool_batch import FLEXIBLE_SCHEMA, child
from .git_delivery import MAX_CHANGES, MAX_CHECKOUT_FILE_BYTES
from .git_publication import GitHubPublicationGrant, REPOSITORY
from .git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from .linux_planner_policy import _executable_pin
from .pool_journal import identifier, uuid_value

SCHEMA='linux-cloud-workflow-v1'
MAX_CONFIG=128*1024


def fail(code='cloud_workflow_configuration_changed'):
    raise DevelopmentError(code)


def _text(value, maximum):
    if type(value) is not str or not value.strip() or len(value.encode())>maximum:fail()
    check_content(value.encode())
    return value


def load_workflow(path, *, root, batch):
    """Bounded private operator data; no imports of reviewer code or RPC."""
    raw,pin=file_document(child(path,root/'config'),MAX_CONFIG)
    value=decode(raw)
    if (type(value) is not dict or set(value)!={'schema','batch_id','jobs'} or value['schema']!=SCHEMA
            or value['batch_id']!=batch['batch_id'] or batch['schema']!=FLEXIBLE_SCHEMA
            or type(value['jobs']) is not list or len(value['jobs'])!=len(batch['jobs'])):fail()
    uuid_value(value['batch_id']);names=set();by_name={job['name']:job for job in batch['jobs']}
    if any('review-'+name in by_name for name in by_name):fail('cloud_workflow_driver_collision')
    for job in value['jobs']:
        if type(job) is not dict or set(job)!={'name','paths','commit_message','pr_title','pr_body','reviewer'}:fail()
        identifier(job['name'])
        identifier('review-'+job['name']);identifier(value['batch_id']+':review:'+job['name'])
        if job['name'] not in by_name or job['name'] in names:fail()
        names.add(job['name']);coder=by_name[job['name']]
        if (type(job['paths']) is not list or not 1<=len(job['paths'])<=MAX_CHANGES
                or len(set(job['paths']))!=len(job['paths']) or not set(job['paths'])<=set(coder['paths'])):fail()
        for path in job['paths']:relative_path(path)
        _text(job['commit_message'],4096);_text(job['pr_title'],200);_text(job['pr_body'],16000)
        if any(ord(c)<32 or ord(c)==127 for c in job['pr_title']) or '<!-- blackoutburst-delivery:' in job['pr_body']:fail()
        reviewer=job['reviewer']
        if type(reviewer) is not dict or set(reviewer)!={'agent_id','connection_file','instruction','model','timeout','skills'}:fail()
        opaque(reviewer['agent_id']);opaque(reviewer['model'],'invalid_review_model',128)
        if (reviewer['agent_id']==coder['agent_id'] or reviewer['connection_file']==coder['connection_file']
                or type(reviewer['timeout']) is not int or not 1<=reviewer['timeout']<=300):fail('cloud_workflow_distinct_reviewer_required')
        child(reviewer['connection_file'],root/'connections');_text(reviewer['instruction'],12000)
        skills=reviewer['skills']
        if (type(skills) is not list or not 1<=len(skills)<=32 or 'review' not in skills
                or any(type(skill) is not str or not skill or len(skill)>100 for skill in skills)
                or len(set(skills))!=len(skills)):fail()
    return value,pin


class CloudLinuxWorkflow:
    """Concrete finite extension; no effectful callback or attestation injection."""
    def __init__(self,path,*,root,batch,owner_id,board_endpoint,gh_executable,python_executable):
        self.root=Path(root);self.path=child(path,self.root/'config')
        self.batch=deepcopy(batch);self.owner_id=owner_id;self.endpoint=board_endpoint
        self.configuration,self.pin=load_workflow(self.path,root=self.root,batch=self.batch)
        self.connections={job['reviewer']['connection_file']:file_document(job['reviewer']['connection_file'])[1]
            for job in self.configuration['jobs']}
        self.tools={'gh':_executable_pin(str(gh_executable)),'python':_executable_pin(str(python_executable))}
        self.mutex=threading.RLock();self.stopped=threading.Event();self.active_delivery=None;self.active_review=None
        self.review_drivers={};self.directory=None;self.directory_pin=None

    def validate_batch(self,value,root,owner_id,endpoint):
        if (value!=self.batch or Path(root)!=self.root or owner_id!=self.owner_id or endpoint!=self.endpoint
                or value['owner_id']!=owner_id or value['board_endpoint']!=endpoint):fail()
        self.check(full=True)

    def descriptor(self):
        self.check()
        return dict(schema=SCHEMA,configuration=deepcopy(self.configuration),file=deepcopy(self.pin),
            connections=deepcopy(self.connections),tools=deepcopy(self.tools))

    def check(self,*,full=False):
        if file_document(self.path,MAX_CONFIG)[1]!=self.pin:fail()
        for path,pin in self.connections.items():
            if file_document(path)[1]!=pin:fail('cloud_workflow_connection_changed')
        if self.directory is not None and _directory_pin(self.directory)!=self.directory_pin:fail()
        if full:
            for pin in self.tools.values():
                if _executable_pin(pin['path'])!=pin:fail('cloud_workflow_tool_changed')

    def bindings(self,batch_directory):
        """Construct only explicit reviewer bindings before the Pool is opened."""
        from .review_pool import DeliveryReviewPoolDriver
        self.check()
        directory=Path(batch_directory)/'workflow'
        if self.directory is not None:
            if self.directory!=directory:fail('cloud_workflow_already_bound')
            return {'review-'+name:driver.binding() for name,driver in self.review_drivers.items()}
        self.directory=directory
        with PrivateDirectory(self.directory,create=True):pass
        self.directory_pin=_directory_pin(self.directory)
        bindings={}
        for job in self.configuration['jobs']:
            name=job['name'];reviewer=job['reviewer'];key='review-'+name;identifier(key)
            if key in {item['name'] for item in self.batch['jobs']}:fail('cloud_workflow_driver_collision')
            driver=DeliveryReviewPoolDriver(self.directory/('selection-'+name),connection_file=reviewer['connection_file'],
                owner_id=self.owner_id,agent_id=reviewer['agent_id'],board_endpoint=self.endpoint)
            self.review_drivers[name]=driver;bindings[key]=driver.binding()
        return bindings

    def stop(self):
        """Companion-thread safe: signal the actual publication/reviewer work."""
        self.stopped.set()
        with self.mutex:delivery=self.active_delivery;review=self.active_review
        errors=[]
        if delivery is not None:
            try:delivery[0].stop(delivery[1])
            except Exception:errors.append('delivery_stop_unconfirmed')
        if review is not None:
            try:review[0].stop(review[1])
            except Exception:errors.append('review_stop_unconfirmed')
        if errors:fail('cloud_workflow_stop_unconfirmed')

    def _guard(self,should_stop):
        self.check()
        if self.stopped.is_set() or should_stop():fail('cloud_workflow_stopped')

    def _postimages(self,pool,driver,job,paths):
        """Operator paths + current actual bytes, never model-declared hashes."""
        with pool.mutex:
            pool._ready();current=deepcopy(pool._job(pool.journal.read(),job['id']))
            if any(current[key]!=job[key] for key in ('id','worker_id','session_id','driver_id','driver_revision')):fail()
        context=pool._context(current,create=False);bound=driver.selections.load(context)
        manifest=validate_manifest(decode(file_document(Path(bound['pins']['broker']['path'])/'manifest.json',128*1024)[0]))
        if not set(paths)<=set(manifest['files']):fail('cloud_workflow_paths_outside_manifest')
        changes=[]
        with_repository=RepositoryFiles(manifest['root'],manifest['root_identity'])
        try:
            for path in sorted(paths):
                raw,pin=with_repository.read(path,MAX_CHECKOUT_FILE_BYTES)
                mode='100755' if pin[-1]&0o111 else '100644'
                postimage=digest(raw)
                if postimage!=manifest['files'][path]['sha256']:
                    changes.append(dict(path=path,sha256=postimage,mode=mode))
            with_repository.verify()
        finally:with_repository.close()
        if not changes:fail('cloud_workflow_no_selected_change')
        return changes

    def _material(self,source,operation_id,changes):
        """Bounded exact diff from sealed source, inspected again by review guard."""
        receipt=source.status(operation_id)
        record=decode(file_document(source.paths['deliveries']/operation_id/'delivery.json',8*1024*1024)[0])
        if record['seal']['delivery_sha']!=receipt['delivery_sha']:fail()
        entries={row['path']:row for row in record['snapshot']['files']}
        base=source.workspaces.grant['base_commit'];chunks=['Delivery '+receipt['delivery_sha']+'\nSnapshot '+receipt['snapshot_sha256']+'\n']
        for change in changes:
            path=change['path'];row=entries[path]
            after=file_document(source.paths['deliveries']/operation_id/row['payload'],MAX_CHECKOUT_FILE_BYTES)[0]
            before=source.workspaces._git(['show',base+':'+path],cwd=source.workspaces.repository)
            if digest(after)!=change['sha256'] or row['mode']!=change['mode']:fail()
            chunks.append(''.join(difflib.unified_diff(check_content(before).splitlines(keepends=True),
                check_content(after).splitlines(keepends=True),fromfile='a/'+path,tofile='b/'+path)))
        material='\n'.join(chunks)
        if len(material.encode())>24000:fail('cloud_workflow_review_material_too_large')
        if source.status(operation_id)!=receipt:fail()
        return material

    def verify_completed(self,batch_directory,batch,result,*,tools,cli_version):
        """Cross-check dated component receipts only; no registration/RPC/run."""
        from .development_pool import DevelopmentPoolDriver
        from .review_lifecycle import DeliveryReviewLifecycle
        from .worker_pool import WorkerPool
        from .pool_journal import PoolJournal, MAX_STATE_BYTES
        if (batch!=self.batch or type(result) is not dict or set(result)!={'schema','status','jobs','ticket_published'}
                or result['schema']!=SCHEMA or result['status']!='complete' or result['ticket_published'] is not True
                or type(result['jobs']) is not list or len(result['jobs'])!=len(batch['jobs'])):fail()
        batch_directory=Path(batch_directory)
        state_path=batch_directory/'pool'/'pool-state.json'
        raw,state_pin=file_document(state_path,MAX_STATE_BYTES)
        state=PoolJournal.validate(decode(raw))
        expected_ids={value[key] for value in result['jobs'] for key in ('source_operation_id','review_operation_id')}
        if (state['stopped'] or state['concurrency']!=len(batch['jobs']) or state['ceiling']!=len(batch['jobs'])
                or len(expected_ids)!=2*len(batch['jobs']) or {job['id'] for job in state['jobs']}!=expected_ids
                or any(job['state']!='settled' or job['outcome']!='complete' or job['cleanup_confirmed'] is not True
                    or job['execution_active'] is not False or job['stop_requested'] for job in state['jobs'])):
            fail('cloud_workflow_pool_history_changed')
        by_id={job['id']:job for job in state['jobs']}
        for coder,item in zip(batch['jobs'],result['jobs']):
            payload={key:coder[key] for key in ('project_id','ticket_id')}
            source_job=by_id[item['source_operation_id']];review_job=by_id[item['review_operation_id']]
            if (source_job['payload']!=payload or review_job['payload']!=payload or source_job['driver_id']!=coder['name']
                    or review_job['driver_id']!='review-'+coder['name']
                    or source_job['request_id']!=batch['batch_id']+':'+coder['name']
                    or review_job['request_id']!=batch['batch_id']+':review:'+coder['name']):fail()
        _,owner_lock_pin=file_document(batch_directory/'pool'/'pool-owner.lock',1)
        # All roots must already exist. Constructors are never used to repair a
        # lost prefix during historical receipt validation.
        for path in (batch_directory/'pool',batch_directory/'pool'/'workers',batch_directory/'workflow',*(batch_directory/('selection-'+job['name']) for job in batch['jobs']),
                *(batch_directory/'workflow'/('selection-'+job['name']) for job in batch['jobs'])):
            with PrivateDirectory(path):pass
        bindings=self.bindings(batch_directory);drivers={}
        for job in batch['jobs']:
            drivers[job['name']]=DevelopmentPoolDriver(batch_directory/('selection-'+job['name']),
                connection_file=job['connection_file'],owner_id=self.owner_id,agent_id=job['agent_id'],board_endpoint=self.endpoint)
            bindings[job['name']]=drivers[job['name']].binding()
        pool=WorkerPool(batch_directory/'pool',drivers=bindings)
        try:
            if (file_document(state_path,MAX_STATE_BYTES)[1]!=state_pin
                    or file_document(batch_directory/'pool'/'pool-owner.lock',1)[1]!=owner_lock_pin):fail()
            for coder,item in zip(batch['jobs'],result['jobs']):
                if type(item) is not dict or set(item)!={'name','source_operation_id','review_operation_id','delivery','review'} or item['name']!=coder['name']:fail()
                name=coder['name'];source_root=self.directory/('delivery-'+name);review_root=self.directory/('review-'+name)
                for path in (source_root,review_root,*(source_root/key for key in ('operations','deliveries','reviews','publications'))):
                    with PrivateDirectory(path):pass
                source=self._source(pool,drivers[name],batch,coder)
                if source.status(item['source_operation_id'])!=item['delivery']:fail('cloud_workflow_delivery_history_changed')
                lifecycle=DeliveryReviewLifecycle(review_root,pool=pool,driver=self.review_drivers[name],source=source,
                    executable=tools['executable']['path'],cli_version=cli_version,interpreter=tools['interpreter']['path'])
                if lifecycle.status(item['review_operation_id'])!=item['review']:fail('cloud_workflow_review_history_changed')
        finally:pool.close()

    def _source(self,pool,driver,batch,coder):
        workspaces=LocalGitWorkspaceController(self.root/'git-state'/batch['batch_id']/coder['name'],
            grant=LocalRepositoryGrant(**coder['repository']))
        return DevelopmentPoolResultDelivery(self.directory/('delivery-'+coder['name']),pool=pool,driver=driver,
            workspaces=workspaces,publication_grant=GitHubPublicationGrant(self.owner_id,REPOSITORY,'main',coder['repository']['base_commit']),
            gh_executable=self.tools['gh']['path'],python_executable=self.tools['python']['path'],driver_id=coder['name'])

    def deliver(self,*,pool,drivers,batch,submitted,tools,cli_version,should_stop):
        from .review_lifecycle import DeliveryReviewLifecycle
        if (batch!=self.batch or self.directory is None or type(submitted) is not list or len(submitted)!=len(batch['jobs'])
                or len({job['id'] for job in submitted})!=len(submitted)):fail()
        with pool.mutex:
            pool._ready();state=pool.journal.read()
            for coder,selected_job in zip(batch['jobs'],submitted):
                actual=pool._job(state,selected_job['id'])
                if (any(actual[key]!=selected_job[key] for key in ('id','worker_id','session_id','driver_id','driver_revision'))
                        or actual['driver_id']!=coder['name'] or actual['request_id']!=batch['batch_id']+':'+coder['name']
                        or actual['payload']!={key:coder[key] for key in ('project_id','ticket_id')}):
                    fail('cloud_workflow_selected_job_changed')
        selected={item['name']:item for item in self.configuration['jobs']}
        results=[]
        for coder,job in zip(batch['jobs'],submitted):
            self._guard(should_stop);self.check(full=True)
            specification=selected[coder['name']];reviewer=specification['reviewer'];name=coder['name']
            source=self._source(pool,drivers[name],batch,coder)
            with self.mutex:self.active_delivery=(source,job['id'])
            try:
                self._guard(should_stop)
                changes=self._postimages(pool,drivers[name],job,specification['paths'])
                publication=source.deliver(job['id'],changes=changes,commit_message=specification['commit_message'],
                    pr_title=specification['pr_title'],pr_body=specification['pr_body'])
            finally:
                with self.mutex:self.active_delivery=None
            self._guard(should_stop)
            if publication.get('status')!='complete':fail('cloud_workflow_publication_unconfirmed')
            material=self._material(source,job['id'],changes)
            review=pool.submit(request_id=batch['batch_id']+':review:'+name,driver_id='review-'+name,
                payload={key:coder[key] for key in ('project_id','ticket_id')})
            lifecycle=DeliveryReviewLifecycle(self.directory/('review-'+name),pool=pool,driver=self.review_drivers[name],
                source=source,executable=tools['executable']['path'],cli_version=cli_version,interpreter=tools['interpreter']['path'])
            with self.mutex:self.active_review=(lifecycle,review['id'])
            try:
                self._guard(should_stop)
                prepared=lifecycle.prepare(review,source_operation_id=job['id'],instruction=reviewer['instruction'],
                    material=material,model=reviewer['model'],timeout=reviewer['timeout'],skills=reviewer['skills'])
                if prepared.get('status')!='prepared':fail('cloud_workflow_review_preparation_unconfirmed')
                self._guard(should_stop);pool.dispatch_pending()
                while pool.status()['live_threads']:
                    self._guard(should_stop);time.sleep(.05)
                self._guard(should_stop)
                opinion=lifecycle.publish(review['id'])
                if opinion.get('status')!='complete':fail('cloud_workflow_review_publication_unconfirmed')
            finally:
                with self.mutex:self.active_review=None
            self._guard(should_stop)
            results.append(dict(name=name,source_operation_id=job['id'],review_operation_id=review['id'],
                delivery=publication,review=opinion))
        status=pool.status()
        if status['live_threads'] or status['capacity_held'] or any(job['state']!='settled'
                or job['outcome']!='complete' or job['cleanup_confirmed'] is not True for job in status['jobs']):
            fail('cloud_workflow_cleanup_unconfirmed')
        return dict(schema=SCHEMA,status='complete',jobs=results,ticket_published=True)
