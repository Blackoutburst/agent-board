"""One explicitly claimed delivery review per Pool job; private opinion only.

The author source is historical. No author worker, claim, model, Runtime
reservation or publication is reopened. Reopening the driver requires explicitly
reattaching the same concrete source before dispatch or reconciliation.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
import math
from pathlib import Path
import threading

from .client_lifecycle import own_client
from .context import ContextScope, _profile
from .development_pool_delivery import DevelopmentPoolResultDelivery
from .development_pool_selection import file_document, job_identity
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, check_content, digest, opaque
from .dev_planner import _directory_pin
from .git_delivery import LocalGitDeliveryController
from .linux_planner_policy import _executable_pin
from .linux_review_binding import LinuxDeliveryReviewBinding
from .review_client import DeliveryReviewBoardClient
from .review_worker import DeliveryReviewWorker, ReviewRuntimeGuard
from .run_store import RunStore
from .runtime_client import RuntimeBoardClient
from .runtime_journal import RuntimeJournal
from .work_journal import WorkJournal, _scope
from .worker_pool import PoolDriverBinding, PoolOutcome

REVISION = 'delivery-review-pool-v1'
MAX_SELECTION = 128*1024
FIELDS = {'schema','operation_id','worker_id','session_id','scope','registration',
          'source_operation_id','context_sha256','instruction_file','material_file',
          'planner_policy_directory','executable','model','timeout'}


def fail(code='review_pool_selection_changed'):
    raise DevelopmentError(code)


class HistoricalPoolDeliveryTests:
    """Concrete read-only historical test guard for the completed source.

    This does not assert current author authority. The review worker obtains
    its independent, current claim and Runtime authority through its own client.
    """
    def __init__(self, source, operation_id, receipt):
        if type(source) is not DevelopmentPoolResultDelivery:
            fail('review_pool_concrete_source_required')
        self.source,self.operation_id,self.receipt = source,operation_id,deepcopy(receipt)

    def __call__(self, *, operation_id, scope, snapshot, evidence):
        if operation_id != self.operation_id or self.source.status(operation_id) != self.receipt:
            fail('review_pool_source_changed')
        root=self.source.paths['operations']/operation_id
        expected=decode(file_document(root/'evidence.json',512*1024)[0])
        captured=decode(file_document(root/'captured.json',512*1024)[0])
        if (snapshot.get('request',{}).get('scope') != scope
                or snapshot['request'].get('operation_id') != operation_id
                or digest(canonical(snapshot)) != self.receipt['snapshot_sha256']
                or captured != {'snapshot_sha256':self.receipt['snapshot_sha256']}
                or evidence != expected or evidence.get('snapshot_sha256') != self.receipt['snapshot_sha256']):
            fail('review_pool_source_changed')
        return True


class _ObservedReviewRuntime:
    def __init__(self, client, journal, bound, context):
        self.client,self.journal,self.bound,self.context = client,journal,bound,context
        self.endpoint,self.session_id=client.endpoint,client.session_id
        self.active=set()

    def call(self, name, args):
        value=self.client.call(name,args)
        if name in ('start_execution','heartbeat_execution','finish_execution','lookup_execution'):
            receipt=value.get('execution') if name=='lookup_execution' and type(value) is dict else value
            if type(receipt) is not dict or type(receipt.get('id')) is not str:fail('review_pool_runtime_unconfirmed')
            rows=self.journal.db.execute("SELECT id FROM executions WHERE json_extract(receipt,'$.id')=? LIMIT 2",(receipt['id'],)).fetchall()
            if len(rows)!=1:fail('review_pool_runtime_unconfirmed')
            self.validate(self.journal.get(rows[0]['id']),receipt)
            if receipt['state']=='running':self.active.add(receipt['id'])
            elif receipt['state']=='finished' and receipt['cleanup_confirmed'] is True:self.active.discard(receipt['id'])
            self.context.report_execution(bool(self.active))
        return value

    def validate(self, record, receipt):
        selected=self.bound['selection'];scope=selected['scope'];config=record['configuration']
        if config['scope']!=scope or config['work_id']!=self.context.operation_id:
            fail('review_pool_runtime_scope_changed')
        guard=ReviewRuntimeGuard(self.journal,self.client,scope,self.bound['review']['request']['selection'],self.context.operation_id)
        guard.operation=record;value=guard.metadata(receipt)
        registration=selected['registration']
        if (value['device_id']!=registration['identity']['device_id']
                or value['profile_version']!=registration['profile']['version']):fail('review_pool_runtime_scope_changed')
        return value


class DeliveryReviewPoolDriver:
    def __init__(self, selection_directory, *, connection_file, owner_id, agent_id, board_endpoint):
        self.directory=str(Path(selection_directory).absolute())
        with PrivateDirectory(self.directory,create=True):pass
        self.pin=_directory_pin(self.directory)
        self.connection_file=str(Path(connection_file))
        _,self.connection_pin=file_document(self.connection_file)
        self.fixed=dict(owner_id=opaque(owner_id),agent_id=opaque(agent_id),board_endpoint=board_endpoint)
        self.sources={};self.mutex=threading.RLock()

    def binding(self):
        return PoolDriverBinding(REVISION,self._factory,self._reconcile,self._verify_cleanup)

    @staticmethod
    def _descriptor(source, operation_id):
        if type(source) is not DevelopmentPoolResultDelivery:fail('review_pool_concrete_source_required')
        receipt=source.status(operation_id)
        if receipt.get('status')!='complete':fail('review_pool_source_incomplete')
        return dict(directory=str(source.directory),pins=source.pins,pool=source.pool_pin,
                    local_grant=source.workspaces.grant,git_pins=source.workspaces.pins,
                    grant=source.grant,operation_id=operation_id,receipt=receipt)

    def attach_source(self, operation_id, source):
        """Explicit in-memory attachment; no worker/client construction or RPC."""
        with self.mutex,PrivateDirectory(self.directory) as private:
            bound=decode(private.read(operation_id+'.json',MAX_SELECTION))
            if self._descriptor(source,bound['selection']['source_operation_id'])!=bound['source']:
                fail('review_pool_source_changed')
            self.sources[operation_id]=source

    def _source(self, bound):
        with self.mutex:source=self.sources.get(bound['selection']['operation_id'])
        if source is None:fail('review_pool_source_attachment_required')
        if self._descriptor(source,bound['selection']['source_operation_id'])!=bound['source']:
            fail('review_pool_source_changed')
        return source

    def _deliveries(self, bound):
        source=self._source(bound)
        return LocalGitDeliveryController(source.workspaces,source.paths['deliveries'],source.paths['reviews'],
            test_guard=HistoricalPoolDeliveryTests(source,bound['selection']['source_operation_id'],bound['source']['receipt']))

    def _validate(self, selected, job, payload):
        identity=job_identity(job)
        if (type(selected) is not dict or set(selected)!=FIELDS or type(selected['schema']) is not int
                or selected['schema']!=1 or type(payload) is not dict or set(payload)!={'project_id','ticket_id'}):fail()
        if any(selected[name]!=identity[key] for name,key in
               (('operation_id','id'),('worker_id','worker_id'),('session_id','session_id'))):fail()
        scope={**self.fixed,**payload,'board_session_id':identity['session_id']};_scope(scope)
        if selected['scope']!=scope:fail()
        job_identity(dict(id=selected['source_operation_id'],worker_id=identity['worker_id'],session_id=identity['session_id']))
        registration=selected['registration']
        try:
            _profile(registration,ContextScope(**{key:scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}))
            if (registration['identity']['id']!=scope['agent_id'] or registration['session']['id']!=scope['board_session_id']
                    or registration['session']['agent_id']!=scope['agent_id'] or 'review' not in registration['session']['skills']):fail()
        except (KeyError,TypeError):fail('review_pool_registration_required')
        opaque(selected['model'],'invalid_review_model',128)
        if (type(selected['timeout']) not in (float,int) or not math.isfinite(selected['timeout'])
                or not 1<=selected['timeout']<=300):fail()
        instruction,instruction_pin=file_document(selected['instruction_file'],12000)
        material,material_pin=file_document(selected['material_file'],24000)
        instruction,material=check_content(instruction),check_content(material)
        if not instruction.strip() or not material.strip():fail()
        executable=str(Path(selected['executable']))
        if not Path(executable).is_absolute():fail()
        # Public executable can be outside a private directory. Pin its real
        # bytes and ancestry to the controller selection before dispatch.
        executable=str(Path(executable).resolve(strict=True))
        if not Path(executable).is_file():fail()
        policy=Path(selected['planner_policy_directory'])
        policy_pin=_directory_pin(policy)
        plan_raw,plan_pin=file_document(policy/'planner-plan.json',128*1024)
        _,qualification_pin=file_document(policy/'planner-qualification.json',128*1024)
        plan=decode(plan_raw)
        if canonical(plan)!=plan_raw or plan.get('executable')!=executable:fail('review_pool_policy_changed')
        workspace=plan.get('workspace')
        if type(workspace) is not str or not Path(workspace).is_absolute():fail('review_pool_policy_changed')
        if file_document(self.connection_file)[1]!=self.connection_pin:fail('review_pool_connection_changed')
        return dict(selection=deepcopy(selected),instruction=instruction,material=material,
                    instruction_pin=instruction_pin,material_pin=material_pin,executable=executable,
                    executable_pin=_executable_pin(executable),
                    policy_pin=policy_pin,policy_plan=plan_pin,policy_qualification=qualification_pin,
                    workspace=workspace,
                    connection=deepcopy(self.connection_pin))

    def bind_selection(self, job, approved_selection_file, *, delivery_source):
        if job.get('state')!='queued' or job.get('driver_revision')!=REVISION:fail()
        selected=decode(file_document(approved_selection_file,MAX_SELECTION)[0])
        payload={key:selected['scope'][key] for key in ('project_id','ticket_id')}
        bound=self._validate(selected,job,payload)
        bound['source']=self._descriptor(delivery_source,selected['source_operation_id'])
        # Binding is a bounded local preparation; it creates only the independent
        # read-only review export. The controller already owns the review claim.
        with self.mutex:self.sources[job['id']]=delivery_source
        deliveries=self._deliveries(bound)
        bound['review']=deliveries.prepare_review(operation_id=selected['source_operation_id'],
            review_operation_id=job['id'],reviewer_scope=selected['scope'],
            context_sha256=selected['context_sha256'],material=bound['material'])
        raw=canonical(bound)
        if len(raw)>MAX_SELECTION:fail('review_pool_selection_limit')
        with self.mutex,PrivateDirectory(self.directory) as private:
            if _directory_pin(self.directory)!=self.pin:fail()
            try:private.create(job['id']+'.json',raw)
            except DevelopmentError as error:
                if error.code!='private_file_exists' or private.read(job['id']+'.json',MAX_SELECTION)!=raw:fail()
        return dict(operation_id=job['id'],session_id=job['session_id'],status='bound',selection_sha256=digest(raw))

    def _load(self, context):
        if _directory_pin(self.directory)!=self.pin:fail()
        with self.mutex,PrivateDirectory(self.directory) as private:raw=private.read(context.operation_id+'.json',MAX_SELECTION)
        bound=decode(raw);selected=bound['selection']
        current=self._validate(selected,dict(id=context.operation_id,worker_id=context.worker_id,session_id=context.session_id),context.payload)
        if canonical(bound)!=raw or any(bound.get(key)!=value for key,value in current.items()):fail()
        deliveries=self._deliveries(bound)
        deliveries.review_guard(selected['source_operation_id'],context.operation_id,scope=selected['scope'],
            selection=bound['review']['request']['selection'],context={'fingerprint':selected['context_sha256']},
            material=bound['material'],workspace=bound['review']['workspace'])
        return bound,deliveries

    def _open(self, resources, context, bound, deliveries, *, existing=False, observe=False):
        if existing:
            for kind,name in (('work','work-journal.sqlite3'),('runs','runs.sqlite3'),('runtime','runtime.sqlite3')):
                file_document(Path(context.journals[kind])/name,32*1024*1024)
        journal=resources.enter_context(WorkJournal(context.journals['work']))
        store=resources.enter_context(closing(RunStore(context.journals['runs'],existing_only=existing)))
        runtime=resources.enter_context(RuntimeJournal(context.journals['runtime']))
        client=own_client(resources,DeliveryReviewBoardClient.from_connection_file(self.connection_file,session_id=context.session_id,timeout=3))
        if (client.endpoint!=bound['selection']['scope']['board_endpoint']
                or list(client.connection_identity)!=bound['connection']['identity']
                or file_document(self.connection_file)[1]!=bound['connection']):fail('review_pool_connection_changed')
        client.pin_registration(bound['selection']['registration'])
        control=own_client(resources,RuntimeBoardClient.from_worker(client))
        if observe:control=_ObservedReviewRuntime(control,runtime,bound,context)
        binding=resources.enter_context(LinuxDeliveryReviewBinding(deliveries,
            operation_id=bound['selection']['source_operation_id'],review_operation_id=context.operation_id,
            planner_policy_directory=bound['selection']['planner_policy_directory']))
        if (binding.workspace!=bound['workspace'] or binding.executable!=bound['executable']
                or binding.policy._plan['checkout']!=bound['review']['workspace']):fail('review_pool_policy_changed')
        return DeliveryReviewWorker(journal,store,client,runtime,control,
            delivery_guard=binding.guard,runner_factory=binding.runner_factory)

    def _cleanup(self, worker, context, bound):
        selected=bound['selection'];op=worker.journal.get(context.operation_id)
        expected=dict(kind='delivery_review_v1',work_phase='review',selection=bound['review']['request']['selection'],
            instruction=bound['instruction'],material=bound['material'],model=selected['model'],timeout=selected['timeout'],
            workspace=bound['workspace'],executable=bound['executable'])
        if op['scope']!=selected['scope'] or op['configuration']!=expected:fail('review_pool_operation_changed')
        rows=worker.run_store._db.execute('SELECT id FROM works LIMIT 2').fetchall()
        if len(rows)>1 or rows and rows[0]['id']!=context.operation_id:fail('review_pool_cleanup_unconfirmed')
        turns=[] if not rows else worker.run_store.status(context.operation_id)['turns']
        work=None if not rows else worker.run_store.get_work(context.operation_id)
        if work is not None:
            guard=ReviewRuntimeGuard(worker.runtime_journal,worker.runtime_client,selected['scope'],
                                    bound['review']['request']['selection'],context.operation_id)
            if (work['model']!=selected['model'] or work['workspace']!=bound['workspace']
                    or work['executable']!=bound['executable']):fail('review_pool_cleanup_unconfirmed')
        for turn in turns:
            result=worker.run_store.turn_result(turn['id'])
            if (turn['status'] not in ('completed','blocked','cancelled')
                    or turn['status']=='completed' and turn.get('requires_review')
                    or result.get('cleanup_failed') is True or result.get('verification',{}).get('cleanup_confirmed') is not True):
                fail('review_pool_cleanup_unconfirmed')
            if (turn['request_id']!='delivery-review-'+context.operation_id
                    or turn['work_id']!=context.operation_id):fail('review_pool_cleanup_unconfirmed')
        records=[worker.runtime_journal.get(row['id']) for row in worker.runtime_journal.list_status()]
        if len(records)!=len(turns) or len(records)>1:fail('review_pool_cleanup_unconfirmed')
        observer=_ObservedReviewRuntime(worker.runtime_client,worker.runtime_journal,bound,context)
        for record in records:
            if (record['phase']!='finished' or record['receipt'] is None
                    or record['configuration']!=guard.configuration(work,turns[0]['id'])):fail('review_pool_cleanup_unconfirmed')
            found=worker.runtime_client.call('lookup_execution',{'idempotency_key':record['key']})
            if type(found) is not dict or found.get('found') is not True:fail('review_pool_cleanup_unconfirmed')
            value=observer.validate(record,found.get('execution'))
            if value!=record['receipt'] or value['state']!='finished' or value['cleanup_confirmed'] is not True:
                fail('review_pool_cleanup_unconfirmed')
        return True

    def _factory(self, context):return _ReviewRun(self,context)

    def open_completed_source(self, resources, context):
        """Open real historical inputs on the writer's thread, without RPC.

        The local controller supplies this job's existing PoolContext and owns
        the ExitStack. No cached reviewer run or Runtime replay is invoked.
        Returns the correlated ``(LocalGitDeliveryController, DeliveryReviewWorker)``.
        """
        if type(resources) is not ExitStack:fail('review_pool_resources_required')
        bound,deliveries=self._load(context)
        with PrivateDirectory(context.journals['queue']) as private:
            expected=dict(operation_id=context.operation_id,session_id=context.session_id,
                status='complete',opinion_private=True,ticket_published=False,cleanup_confirmed=True)
            if private.read('review-result.json',MAX_SELECTION)!=canonical(expected):fail('review_pool_opinion_incomplete')
        worker=self._open(resources,context,bound,deliveries,existing=True)
        if worker.journal.get(context.operation_id)['stage']!='complete':fail('review_pool_opinion_incomplete')
        return deliveries,worker

    def _reconcile(self, context):
        bound,deliveries=self._load(context)
        with ExitStack() as resources:
            worker=self._open(resources,context,bound,deliveries,existing=True)
            cleaned=self._cleanup(worker,context,bound)
        return PoolOutcome(context.operation_id,context.session_id,'needs_review',cleaned)

    def _verify_cleanup(self, context, outcome):
        if (outcome.operation_id!=context.operation_id or outcome.session_id!=context.session_id
                or outcome.cleanup_confirmed is not True):return False
        bound,deliveries=self._load(context)
        with ExitStack() as resources:
            return self._cleanup(self._open(resources,context,bound,deliveries,existing=True),context,bound)


class _ReviewRun:
    def __init__(self, driver, context):
        self.driver,self.context=driver,context
        self.bound,deliveries=driver._load(context)
        self.resources=ExitStack();self.closed=False
        try:self.worker=driver._open(self.resources,context,self.bound,deliveries,observe=True)
        except BaseException:self.resources.close();raise

    def run(self):
        context,bound=self.context,self.bound;selected=bound['selection']
        if context.should_stop():fail('review_pool_stopped')
        with PrivateDirectory(context.journals['queue']) as private:
            private.create('review-run-intent.json',canonical(dict(operation_id=context.operation_id,
                session_id=context.session_id,selection_sha256=digest(canonical(bound)))))
        result=self.worker.run(scope=selected['scope'],request_id='pool-review-'+context.operation_id,
            operation_id=context.operation_id,selection=bound['review']['request']['selection'],
            instruction=bound['instruction'],material=bound['material'],workspace=bound['workspace'],
            executable=bound['executable'],model=selected['model'],timeout=selected['timeout'],should_cancel=context.should_stop)
        cleaned=False
        try:cleaned=self.driver._cleanup(self.worker,context,bound)
        except Exception:pass
        status='complete' if result.get('status')=='complete' else 'needs_review'
        with PrivateDirectory(context.journals['queue']) as private:
            private.create('review-result.json',canonical(dict(operation_id=context.operation_id,session_id=context.session_id,
                status=status,opinion_private=True,ticket_published=False,cleanup_confirmed=cleaned)))
        return PoolOutcome(context.operation_id,context.session_id,status,cleaned)

    def close(self):
        if not self.closed:self.closed=True;self.resources.close()
