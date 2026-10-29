"""Explicit claim/preparation and visible, review-only publication for a Pool.

Preparation is one-shot and never resumes an ambiguous registration, claim or
qualification. Publication retains its exact CAS and reconciles a lost response
only against the exact server event attributed to the original reviewer session.
"""
from contextlib import ExitStack,contextmanager
from copy import deepcopy
import fcntl
import math
import os
from pathlib import Path
import re

from .client_lifecycle import own_client
from .context import ContextScope,build_context,_ticket_sources
from .development_pool_delivery import DevelopmentPoolResultDelivery,_historical_operation
from .development_pool_selection import file_document,job_identity
from .dev_journal import PrivateDirectory,decode
from .dev_manifest import DevelopmentError,canonical,digest,identity
from .dev_planner import _directory_pin
from .git_delivery import LocalGitDeliveryController,_inventory
from .linux_planner_policy import LinuxPlannerPolicy,prepare_linux_planner_policy
from .pool_journal import uuid_value
from .review_lifecycle_client import DeliveryReviewLifecycleClient
from .review_pool import DeliveryReviewPoolDriver,HistoricalPoolDeliveryTests,REVISION
from .review_worker import _opinion,_agent_id,ReviewRuntimeGuard
from .worker_pool import WorkerPool

MAX_RECORD=512*1024


def fail(code='review_lifecycle_unconfirmed'):
    raise DevelopmentError(code)


class DeliveryReviewLifecycle:
    def __init__(self,directory,*,pool,driver,source,executable,cli_version,interpreter='/usr/bin/python3.10'):
        from .development_aggregate_review_source import PublishedAggregateReviewSource
        if (type(pool) is not WorkerPool or type(driver) is not DeliveryReviewPoolDriver
                or type(source) not in (DevelopmentPoolResultDelivery,PublishedAggregateReviewSource)):
            fail('review_lifecycle_concrete_handles_required')
        self.pool,self.driver,self.source=pool,driver,source
        self.directory=Path(directory).absolute()
        forbidden=(pool.journal.private.path,Path(driver.directory),source.directory,source.workspaces.repository,
                   source.workspaces.workspace_root,source.workspaces.state_root)
        if any(self.directory.is_relative_to(root) or root.is_relative_to(self.directory) for root in forbidden):
            fail('review_lifecycle_roots_overlap')
        with PrivateDirectory(self.directory,create=True):pass
        self.pin=_directory_pin(self.directory)
        self.executable,self.interpreter=str(Path(executable).resolve(strict=True)),str(Path(interpreter).resolve(strict=True))
        self.cli_version=cli_version

    def _job(self,operation_id,*,queued=False,complete=False):
        uuid_value(operation_id)
        with self.pool.mutex:
            self.pool._ready();state=self.pool.journal.read();job=deepcopy(self.pool._job(state,operation_id))
            binding=self.pool.drivers.get(job['driver_id'])
            if (binding is None or binding.revision!=REVISION or getattr(binding.factory,'__self__',None) is not self.driver
                    or job['driver_revision']!=REVISION):fail('review_lifecycle_driver_changed')
            if queued and job['state']!='queued':fail('review_lifecycle_queued_job_required')
            if complete and (job['state']!='settled' or job['outcome']!='complete' or job['cleanup_confirmed'] is not True
                    or job['execution_active'] is not False or operation_id in self.pool.launching or operation_id in self.pool.calling
                    or operation_id in self.pool.reconciling or self.pool.threads.get(operation_id) is not None
                    and self.pool.threads[operation_id].is_alive()):fail('review_lifecycle_completed_review_required')
            return state,job

    @contextmanager
    def _locked(self,operation_id,*,create=False):
        uuid_value(operation_id)
        if _directory_pin(self.directory)!=self.pin:fail('review_lifecycle_roots_changed')
        fresh=False
        with PrivateDirectory(self.directory) as parent:
            if create:
                try:os.mkdir(operation_id,0o700,dir_fd=parent.fd);os.fsync(parent.fd);fresh=True
                except FileExistsError:pass
        with PrivateDirectory(self.directory/operation_id) as private:
            descriptor=None
            try:
                descriptor=os.open('lifecycle.lock',os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if fresh else 0),0o600,dir_fd=private.fd)
                private.validate_file(descriptor);pin=identity(os.fstat(descriptor))
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                def held():
                    private.verify();private.validate_file(descriptor)
                    if identity(os.stat('lifecycle.lock',dir_fd=private.fd,follow_symlinks=False))!=pin:fail()
                held();yield private,held,fresh
            except OSError:fail('review_lifecycle_busy_or_incomplete')
            finally:
                if descriptor is not None:os.close(descriptor)

    @staticmethod
    def _read(private,name):
        raw=private.read(name+'.json',MAX_RECORD);value=decode(raw)
        if canonical(value)!=raw:fail('review_lifecycle_record_changed')
        return value

    @classmethod
    def _remember(cls,private,name,value):
        raw=canonical(value)
        if len(raw)>MAX_RECORD:fail('review_lifecycle_record_limit')
        try:private.create(name+'.json',raw)
        except DevelopmentError as error:
            if error.code!='private_file_exists' or cls._read(private,name)!=value:fail('review_lifecycle_intent_changed')

    @staticmethod
    def _has(private,name):
        try:os.stat(name+'.json',dir_fd=private.fd,follow_symlinks=False);return True
        except FileNotFoundError:return False

    def _local(self,private,operation_id):
        state,job=self._job(operation_id)
        if state['stopped'] or self.pool.admissions_blocked or self.pool.stop_unconfirmed or job['stop_requested']:
            fail('review_lifecycle_stopped')
        if self._has(private,'stop'):fail('review_lifecycle_stopped')
        if _directory_pin(self.directory)!=self.pin or file_document(self.driver.connection_file)[1]!=self.driver.connection_pin:
            fail('review_lifecycle_connection_changed')
        # File/identity checks above can perform I/O. A Stop arriving during
        # them must not be forgotten by the final effect guard.
        state,job=self._job(operation_id)
        if (state['stopped'] or self.pool.admissions_blocked or self.pool.stop_unconfirmed or job['stop_requested']
                or self._has(private,'stop')):fail('review_lifecycle_stopped')
        return job

    def _client(self,resources,session_id,registration=None,*,effect_guard=None):
        value=own_client(resources,DeliveryReviewLifecycleClient.from_connection_file(self.driver.connection_file,session_id=session_id,timeout=3))
        if (value.endpoint!=self.driver.fixed['board_endpoint'] or list(value.connection_identity)!=self.driver.connection_pin['identity']
                or file_document(self.driver.connection_file)[1]!=self.driver.connection_pin):fail('review_lifecycle_connection_changed')
        if registration is not None:value.pin_registration(registration)
        if effect_guard is not None:value.set_effect_guard(effect_guard)
        return value

    @staticmethod
    def _policy(client,scope):
        policy=client.call('get_work',dict(project_id=scope['project_id'],limit=1))['policy']
        if any(policy.get(key) is not True for key in ('enabled','allow_read','allow_requests')):
            fail('review_lifecycle_permission_denied')
        return policy

    def prepare(self,job,*,source_operation_id,instruction,material,model,timeout=120,skills=None):
        try:return self._prepare(job,source_operation_id=source_operation_id,instruction=instruction,material=material,
                                 model=model,timeout=timeout,skills=skills)
        except DevelopmentError:raise
        except Exception:fail('review_lifecycle_preparation_unconfirmed')

    def _prepare(self,job,*,source_operation_id,instruction,material,model,timeout=120,skills=None):
        identity_job=job_identity(job);operation_id=identity_job['id'];uuid_value(source_operation_id)
        current=self._job(operation_id,queued=True)[1]
        if any(current[key]!=value for key,value in identity_job.items()):fail()
        if type(current['payload']) is not dict or set(current['payload'])!={'project_id','ticket_id'}:fail()
        for value,maximum in ((instruction,12000),(material,24000),(model,128)):
            if type(value) is not str or not value.strip() or '\x00' in value or len(value.encode())>maximum:
                fail('review_lifecycle_invalid_request')
        if (type(timeout) not in (int,float) or not math.isfinite(timeout) or not 1<=timeout<=300
                or type(self.cli_version) is not str or re.fullmatch(r'codex-cli [0-9][A-Za-z0-9._+-]{0,79}',self.cli_version) is None):
            fail('review_lifecycle_invalid_request')
        skills=['review'] if skills is None else skills
        if (type(skills) is not list or not 1<=len(skills)<=32
                or any(type(item) is not str or not item or len(item)>100 for item in skills)
                or 'review' not in skills or len(set(skills))!=len(skills)):fail('review_lifecycle_invalid_request')
        request=dict(job=current,source=self.driver._descriptor(self.source,source_operation_id),instruction=instruction,
            material=material,model=model,timeout=timeout,skills=skills,
            executable=self.executable,cli_version=self.cli_version,interpreter=self.interpreter,
            connection=self.driver.connection_pin,root=self.pin)
        with self._locked(operation_id,create=True) as (private,held,fresh):
            if not fresh:
                if self._read(private,'prepare-intent')!=request:fail('review_lifecycle_intent_changed')
                return dict(operation_id=operation_id,status='retained',reason='explicit_preparation_reconciliation_required')
            self._remember(private,'prepare-intent',request);self._local(private,operation_id);held()
            scope={**self.driver.fixed,**current['payload'],'board_session_id':current['session_id']}
            with ExitStack() as resources:
                client=self._client(resources,current['session_id'],effect_guard=lambda:self._local(private,operation_id))
                registration=client.register_initial(skills=request['skills'])
                # The same strict reader used by the eventual worker validates
                # that registration is this selected profile/session, not a
                # legacy fallback identity returned by a misbound device.
                from .board_worker import BoardWorker
                if BoardWorker(None,None,client)._registration(scope)!=registration:fail()
                self._remember(private,'registration',registration)
                self._policy(client,scope);self._local(private,operation_id)
                response=client.call('get_ticket',{'ticket_id':scope['ticket_id']})
                project,_selected=_ticket_sources(response,ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}))
                ticket=response['ticket']
                receipt=request['source']['receipt']
                if (project.get('archived') is not False or ticket.get('status')!='review' or ticket.get('claimant') is not None
                        or ticket.get('blocked_by') or ticket.get('waiting_on_human')
                        or ticket.get('version')!=receipt['handoff']['version']):fail('review_lifecycle_source_ticket_changed')
                claim=dict(ticket_id=scope['ticket_id'],expected_version=ticket['version'])
                self._remember(private,'claim-intent',claim);held();self._local(private,operation_id)
                if self.driver._descriptor(self.source,source_operation_id)!=request['source']:fail('review_lifecycle_source_changed')
                claimed=client.call('claim_ticket',claim)
                if claimed.get('version')!=ticket['version']+1:fail('review_lifecycle_claim_unconfirmed')
                self._remember(private,'claimed',claimed)
                context=build_context(client.call,scope=ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}),registration=registration)
                self._remember(private,'context',context);self._local(private,operation_id)
                private.create('instruction.txt',instruction.encode('utf-8'));private.create('material.txt',material.encode('utf-8'))
                deliveries=LocalGitDeliveryController(self.source.workspaces,self.source.paths['deliveries'],self.source.paths['reviews'],
                    test_guard=HistoricalPoolDeliveryTests(self.source,source_operation_id,receipt))
                self._remember(private,'export-intent',dict(source_operation_id=source_operation_id,context_sha256=context['fingerprint'],material_sha256=digest(material.encode())))
                held();self._local(private,operation_id)
                exported=deliveries.prepare_review(operation_id=source_operation_id,review_operation_id=operation_id,
                    reviewer_scope=scope,context_sha256=context['fingerprint'],material=material)
                self._remember(private,'export',exported)
                workspace=private.path/'empty';os.mkdir(workspace,0o700,dir_fd=private.fd);os.fsync(private.fd)
                policy_directory=private.path/'policy'
                self._remember(private,'qualification-intent',dict(workspace=str(workspace),checkout=exported['workspace'],policy_directory=str(policy_directory)))
                held();self._local(private,operation_id)
                prepare_linux_planner_policy(policy_directory,workspace=workspace,checkout=exported['workspace'],executable=self.executable,
                    cli_version=self.cli_version,interpreter=self.interpreter)
                with LinuxPlannerPolicy(policy_directory) as policy:
                    held();self._local(private,operation_id)
                    policy.qualify();policy.validate_for(self.executable,str(workspace))
                self._remember(private,'qualified',dict(policy_plan=file_document(policy_directory/'planner-plan.json')[1],
                    qualification=file_document(policy_directory/'planner-qualification.json')[1]))
                selection=dict(schema=1,operation_id=operation_id,worker_id=current['worker_id'],session_id=current['session_id'],scope=scope,
                    registration=registration,source_operation_id=source_operation_id,context_sha256=context['fingerprint'],
                    instruction_file=str(private.path/'instruction.txt'),material_file=str(private.path/'material.txt'),
                    planner_policy_directory=str(policy_directory),executable=self.executable,model=model,timeout=timeout)
                self._remember(private,'selection',selection);held();self._local(private,operation_id)
                if self.driver._descriptor(self.source,source_operation_id)!=request['source']:fail('review_lifecycle_source_changed')
                result=self.driver.bind_selection(current,private.path/'selection.json',delivery_source=self.source)
                self._remember(private,'prepared',result)
                return dict(operation_id=operation_id,status='prepared',selection_sha256=result['selection_sha256'])

    def status(self,operation_id):
        """Historical metadata only, never current effect authorization."""
        with self._locked(operation_id) as (private,held,_):
            intent=self._read(private,'prepare-intent');held()
            if intent['job']['id']!=operation_id:fail()
            if self._has(private,'published'):
                _,job=self._job(operation_id,complete=True);context=self.pool._context(job,create=False)
                if intent['source']!=self.driver._descriptor(self.source,intent['source']['operation_id']):fail('review_lifecycle_source_changed')
                op=_historical_operation(context.journals['work'],_directory_pin(context.journals['work']),operation_id)
                return self._published(private,op,intent,context)
            return dict(operation_id=operation_id,status='prepared' if self._has(private,'prepared') else 'retained',
                        publication_pending=self._has(private,'publication-intent'),stop_requested=self._has(private,'stop'))

    def stop(self,operation_id):
        error=None
        try:
            # A separate immutable file remains writable while preparation or
            # publication holds lifecycle.lock. Pool.stop owns actual cancellation.
            uuid_value(operation_id)
            with PrivateDirectory(self.directory) as parent:
                try:os.mkdir(operation_id,0o700,dir_fd=parent.fd);os.fsync(parent.fd)
                except FileExistsError:pass
            with PrivateDirectory(self.directory/operation_id) as private:
                self._remember(private,'stop',dict(operation_id=operation_id,stop=True))
        except BaseException as caught:error=caught
        finally:result=self.pool.stop(operation_id)
        if error is not None:fail('review_lifecycle_stop_record_unconfirmed')
        return result

    def _review(self,resources,operation_id):
        _,job=self._job(operation_id,complete=True);context=self.pool._context(job,create=False)
        self.driver.attach_source(operation_id,self.source)
        deliveries,worker=self.driver.open_completed_source(resources,context)
        op=worker.journal.get(operation_id)
        if op['stage']!='complete' or op['configuration']['kind']!='delivery_review_v1':fail('review_lifecycle_opinion_incomplete')
        config,payload=op['configuration'],op['payload'];selection=config['selection'];scope=op['scope']
        if (payload.get('opinion')!={**_opinion(payload.get('result_text')),'selection':selection}
                or digest(config['material'].encode())!=selection['material_sha256']):fail('review_lifecycle_opinion_changed')
        work=worker.run_store.get_work(payload['work_id']);turns=worker.run_store.status(work['id'])['turns']
        if (work['id']!=operation_id or work['agent_id']!=_agent_id(scope,selection,operation_id)
                or any(work[key]!=scope[key] for key in ('owner_id','project_id','ticket_id'))
                or work['workspace']!=config['workspace'] or work['executable']!=config['executable']
                or work['model']!=config['model'] or len(turns)!=1):fail('review_lifecycle_opinion_changed')
        turn=turns[0];result=worker.run_store.turn_result(turn['id'])
        if (turn['id']!=payload['turn_id'] or turn['request_id']!=payload['run_request_id']
                or turn['status']!='completed' or turn.get('requires_review') or result.get('status')!='completed'
                or result.get('uncertain') is True or result.get('cleanup_failed') is True
                or result.get('text')!=payload['result_text'] or type(result.get('verification')) is not dict
                or any(result['verification'].get(key) is not True for key in ('cleanup_confirmed','tool_free'))):
            fail('review_lifecycle_opinion_changed')
        guard=ReviewRuntimeGuard(worker.runtime_journal,worker.runtime_client,scope,selection,operation_id)
        record=worker.runtime_journal.find(guard.configuration(work,turn['id']))
        if (record is None or record['phase']!='finished' or record['start_confirmed']!=1
                or len(worker.runtime_journal.list_status())!=1):fail('review_lifecycle_runtime_unconfirmed')
        guard.operation=record;receipt=guard.metadata(record['receipt'])
        if receipt['state']!='finished' or receipt['cleanup_confirmed'] is not True or receipt['outcome']!='completed':
            fail('review_lifecycle_runtime_unconfirmed')
        if worker.journal.get(operation_id)!=op or worker.runtime_journal.get(record['id'])!=record:fail()
        return job,context,deliveries,worker,op

    @staticmethod
    def _post_context(client,op,publication):
        scope=op['scope'];observed=[]
        def read(name,args):
            value=client.call(name,args)
            if name=='get_ticket':observed.append(deepcopy(value))
            return value
        context=build_context(read,scope=ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}),
                              registration=client.register())
        expected=_ticket_sources({'project':observed[0]['project'],'ticket':publication['ticket']},
            ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}))[1]
        for response in observed:
            _,selected=_ticket_sources(response,ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}))
            selected.update(version=expected['version'],outcome=expected['outcome'],evidence=expected['evidence'])
            if selected!=expected:fail('review_lifecycle_context_changed')
        sources=deepcopy(context['sources']);sources['ticket']=publication['context_sources']['ticket']
        if sources!=publication['context_sources']:fail('review_lifecycle_context_changed')

    @staticmethod
    def _activity(client,ticket_id,after=0):
        value=client.call('list_activity',dict(ticket_id=ticket_id,after=after,limit=10,order='asc' if after else 'desc'))
        if type(value) is not dict or type(value.get('items')) is not list or len(value['items'])>10:
            fail('review_lifecycle_activity_unconfirmed')
        return value['items']

    @staticmethod
    def _changes(operation_id,ticket,op,source):
        opinion=op['payload']['opinion'];fingerprint=digest(canonical(op));marker='Cloud review '+operation_id
        outcome=ticket['outcome']+('\n\n' if ticket['outcome'] else '')+marker+' ['+opinion['verdict']+']\n'+opinion['text']
        audit=marker+'\nDelivery commit: '+source['delivery_sha']+'\nDraft pull request: '+source['pull_request']['url']+'\nReview record SHA256: '+fingerprint+'\nPrivate opinion: '+opinion['verdict']+'\n'+opinion['text']
        evidence=ticket['evidence']+('\n\n' if ticket['evidence'] else '')+audit
        return dict(status='review',release_claim=True,outcome=outcome,evidence=evidence)

    def _publication(self,private,op,prepared):
        publication=self._read(private,'publication-intent');scope=op['scope'];context=self._read(private,'context')
        _,ticket=_ticket_sources({'project':{'id':scope['project_id'],'version':1,'participants':[scope['owner_id']]},
                                 'ticket':publication['ticket']},ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}))
        expected_rpc=dict(ticket_id=scope['ticket_id'],expected_version=ticket['version'],
            changes=self._changes(op['id'],publication['ticket'],op,prepared['source']['receipt']))
        if (set(publication)!={'key','scope','review_record_sha256','source','ticket','rpc','after','context_sources'}
                or publication['review_record_sha256']!=digest(canonical(op)) or publication['scope']!=scope
                or publication['source']!=prepared['source'] or publication['key']!='cloud-review-'+op['id']
                or type(publication['after']) is not int or publication['after']<0
                or publication['rpc']!=expected_rpc or context['fingerprint']!=op['configuration']['selection']['context_sha256']
                or publication['context_sources']!=context['sources']
                or digest(canonical(ticket))!=context['sources']['ticket']['sha256']):fail('review_lifecycle_intent_changed')
        return publication

    @staticmethod
    def _event(event,publication):
        scope=publication['scope'];rpc=publication['rpc']
        return (type(event) is dict and event.get('event')=='ticket_updated'
            and event.get('ticket_id')==scope['ticket_id'] and type(event.get('id')) is int and event['id']>publication['after']
            and event.get('detail')==dict(ticket_id=scope['ticket_id'],version=rpc['expected_version']+1,fields=sorted(rpc['changes']))
            and all(event.get('actor',{}).get(key)==value for key,value in dict(kind='agent',owner_id=scope['owner_id'],
                agent_id=scope['agent_id'],session_id=scope['board_session_id']).items()))

    @staticmethod
    def _result(op,publication):
        source=publication['source']['receipt'];scope=op['scope']
        return dict(operation_id=op['id'],status='complete',ticket_id=scope['ticket_id'],ticket_version=publication['rpc']['expected_version']+1,
            delivery_sha=source['delivery_sha'],pull_request=source['pull_request'],verdict=op['payload']['opinion']['verdict'],
            review_record_sha256=digest(canonical(op)),publication_key=publication['key'])

    def _proof_files(self,context):
        files={}
        for kind,name in (('work','work-journal.sqlite3'),('runs','runs.sqlite3'),('runtime','runtime.sqlite3')):
            directory=Path(context.journals[kind])
            with PrivateDirectory(directory) as private:
                for suffix in ('-journal','-wal','-shm'):
                    try:os.stat(name+suffix,dir_fd=private.fd,follow_symlinks=False)
                    except FileNotFoundError:continue
                    fail('review_lifecycle_journal_pending')
            path=directory/name;files[str(path)]=file_document(path,32*1024*1024)[1]
        paths=[Path(context.journals['queue'])/'review-result.json',Path(self.driver.directory)/(context.operation_id+'.json'),
               self.source.paths['reviews']/context.operation_id/'review.json']
        for path in paths:files[str(path)]=file_document(path,128*1024)[1]
        export=self.source.paths['reviews']/context.operation_id/'export'
        return dict(files=files,export=str(export),export_inventory_sha256=_inventory(export,readonly=True)[1])

    def _published(self,private,op,prepared,context):
        publication=self._publication(private,op,prepared);published=self._read(private,'published')
        expected=self._result(op,publication)
        if (op['stage']!='complete' or set(published)!={'result','event','rpc','proof_files'} or published['result']!=expected
                or published['rpc']!=publication['rpc'] or not self._event(published['event'],publication)
                or op['configuration']['selection']['delivery_sha']!=publication['source']['receipt']['delivery_sha']
                or published['proof_files']!=self._proof_files(context)):fail('review_lifecycle_receipt_changed')
        return deepcopy(expected)

    def publish(self,operation_id):
        try:return self._publish(operation_id)
        except DevelopmentError:raise
        except Exception:fail('review_lifecycle_publication_unconfirmed')

    def _publish(self,operation_id):
        with self._locked(operation_id) as (private,held,_),ExitStack() as resources:
            prepared=self._read(private,'prepare-intent');self._read(private,'prepared')
            job,context,deliveries,worker,op=self._review(resources,operation_id)
            if (prepared['job']['id']!=operation_id or prepared['source']!=self.driver._descriptor(self.source,prepared['source']['operation_id'])
                    or op['configuration']['selection']['delivery_sha']!=prepared['source']['receipt']['delivery_sha']):fail('review_lifecycle_source_changed')
            self._local(private,operation_id);scope=op['scope'];client=self._client(resources,context.session_id,self._read(private,'registration'),
                effect_guard=lambda:self._local(private,operation_id))
            # Once complete, retain a historical receipt without replaying the
            # old review, its claim or its authorization reservation.
            if self._has(private,'published'):
                return self._published(private,op,prepared,context)
            opinion=op['payload']['opinion'];fingerprint=digest(canonical(op))
            if not self._has(private,'publication-intent'):
                worker._snapshot(op);worker._runtime_proof(op)
                self._policy(client,scope);self._local(private,operation_id)
                response=client.call('get_ticket',{'ticket_id':scope['ticket_id']});ticket=response['ticket']
                activities=self._activity(client,scope['ticket_id']);after=max((row['id'] for row in activities),default=0)
                source=prepared['source']['receipt']
                changes=self._changes(operation_id,ticket,op,source)
                # Validation is local and happens before a durable effect intent.
                from .worker_client import _arguments
                rpc=_arguments('update_ticket',dict(ticket_id=scope['ticket_id'],expected_version=ticket['version'],changes=changes))
                publication=dict(key='cloud-review-'+operation_id,scope=scope,review_record_sha256=fingerprint,
                    source=prepared['source'],ticket=deepcopy(ticket),rpc=rpc,after=after,
                    context_sources=worker._snapshot(op)['sources'])
                self._remember(private,'publication-intent',publication)
            publication=self._publication(private,op,prepared)
            self._policy(client,scope);self._local(private,operation_id)
            current=client.call('get_ticket',{'ticket_id':scope['ticket_id']})['ticket']
            rpc=publication['rpc']
            if current['version']==rpc['expected_version']:
                if current!=publication['ticket']:fail('review_lifecycle_ticket_changed')
                worker._snapshot(op);worker._runtime_proof(op)
                if worker._snapshot(op)['sources']!=publication['context_sources']:fail('review_lifecycle_context_changed')
                self._policy(client,scope);held();self._local(private,operation_id)
                try:client.call('update_ticket',rpc)
                except Exception:pass  # Reconcile the original CAS, never change its version or text.
                current=client.call('get_ticket',{'ticket_id':scope['ticket_id']})['ticket']
            if (current['version']!=rpc['expected_version']+1 or current.get('claimant') is not None
                    or current.get('lease_expires_at') is not None
                    or any(current.get(key)!=rpc['changes'][key] for key in ('status','outcome','evidence'))):
                fail('review_lifecycle_publication_unconfirmed')
            events=self._activity(client,scope['ticket_id'],publication['after'])
            matches=[row for row in events if self._event(row,publication)]
            if len(matches)!=1:fail('review_lifecycle_publication_event_unconfirmed')
            self._post_context(client,op,publication)
            self._policy(client,scope);held();self._local(private,operation_id)
            result=self._result(op,publication)
            proof_files=self._proof_files(context)
            self._remember(private,'published',dict(result=result,event=matches[0],rpc=rpc,proof_files=proof_files))
            return result
