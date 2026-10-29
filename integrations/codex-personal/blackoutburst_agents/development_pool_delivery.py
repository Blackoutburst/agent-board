"""Explicit delivery of ONE already settled private development Pool result.

No dispatch, qualification, model, test, cleanup reconciliation or reviewer
selection occurs here. Existing concrete components own all mutation protocols.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from contextvars import ContextVar
import fcntl
import os
from pathlib import Path
import sqlite3

from .development_delivery import DevelopmentDeliveryProof, DevelopmentPublishedDelivery
from .development_git_authority import DevelopmentGitPublicationAuthority
from .development_pool import DevelopmentPoolDriver
from .development_pool_selection import REVISION, file_document
from .development_review_handoff import DevelopmentReviewHandoff
from .development_worker import DevelopmentBoardWorker
from .dev_fence import DevelopmentFence, canonical_anchor, standalone_fence_wait
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, absolute_directory, canonical, check_content, digest, identity, signature, validate_manifest
from .dev_planner import _directory_pin
from .dev_runtime import development_work_id
from .git_delivery import LocalGitDeliveryController, _changes
from .git_publication import GitHubDraftPublisher, GitHubPublicationGrant, REPOSITORY, sha
from .git_workspace import LocalGitWorkspaceController, _path
from .pool_journal import uuid_value
from .runtime_guard import RuntimeGuard
from .work_journal import WorkJournal, _APPLICATION_ID, _COLUMNS
from .worker_pool import WorkerPool

MAX_DOCUMENT = 512*1024
STEPS = ('intent','capture-intent','captured','evidence','seal-intent','sealed',
         'publication-intent','published','board-intent','board','handoff-intent','handoff','complete')


_DELIVERY_PHASE=ContextVar('development_delivery_phase',default='delivery')
DELIVERY_PHASES=frozenset({'delivery','board_handoff'})
DELIVERY_CODES={
    'board_read_error':frozenset({'connection_closed','timeout','connection_failed','redirect_blocked',
        'authentication_failed','permission_denied','http_error','invalid_response','rpc_rejected',
        'tool_rejected','session_mismatch','protocol_mismatch','response_too_large',
        'development_ticket_frame_unavailable','development_registration_required','unknown',
        'connection_failed_remote_disconnected','connection_failed_reset','connection_failed_broken_pipe',
        'connection_failed_aborted','connection_failed_incomplete_read','connection_failed_ssl_error',
        'connection_failed_other_os','connection_failed_http_state','connection_failed_value_error'}),
    'journal_error':frozenset({'scope_busy','scope_lock_unavailable','scope_lock_required',
        'journal_unavailable','journal_corrupt','operation_not_found','insecure_journal_file','unknown'}),
    'os_error':frozenset({'os_error'}),'key_error':frozenset({'missing_key'}),
    'type_error':frozenset({'invalid_type'}),'value_error':frozenset({'invalid_value'}),
    'runtime_error':frozenset({'runtime_error'}),'unknown_error':frozenset({'unknown'})}


def delivery_boundary_diagnostic(value):
    """Validate a copied closed tuple, never arbitrary exception content."""
    if (type(value) is not tuple or len(value)!=3 or any(type(item) is not str for item in value)
            or value[0] not in DELIVERY_PHASES or value[1] not in DELIVERY_CODES
            or value[2] not in DELIVERY_CODES[value[1]]):return None
    return {'status':'delivery_diagnostic','kind':'development_result_delivery',
        'phase':value[0],'category':value[1],'code':value[2]}


def _delivery_failure(error,phase):
    from .board_reader import BoardReadError, connection_failure_diagnostic
    from .work_journal import JournalError
    category,code='unknown_error','unknown'
    if type(error) in (BoardReadError,JournalError):
        category='board_read_error' if type(error) is BoardReadError else 'journal_error'
        candidate=vars(error).get('code')
        code=candidate if type(candidate) is str and candidate in DELIVERY_CODES[category] else 'unknown'
        if type(error) is BoardReadError and code=='connection_failed':
            kind=connection_failure_diagnostic(error)
            diagnostic='connection_failed_'+kind if kind is not None else None
            if diagnostic in DELIVERY_CODES[category]:code=diagnostic
    elif type(error) in (OSError,PermissionError,FileNotFoundError,FileExistsError,NotADirectoryError,
            IsADirectoryError,BlockingIOError,InterruptedError,BrokenPipeError,TimeoutError,
            ConnectionError,ConnectionAbortedError,ConnectionRefusedError,ConnectionResetError):
        category,code='os_error','os_error'
    elif type(error) in (KeyError,TypeError,ValueError,RuntimeError):
        category,code={KeyError:('key_error','missing_key'),TypeError:('type_error','invalid_type'),
            ValueError:('value_error','invalid_value'),RuntimeError:('runtime_error','runtime_error')}[type(error)]
    result=DevelopmentError('development_pool_delivery_unavailable')
    value=(phase,category,code)
    if delivery_boundary_diagnostic(value) is not None:result._development_delivery_diagnostic=value
    return result


def fail(code='development_pool_delivery_changed'):
    raise DevelopmentError(code)


def _historical_operation(directory, pin, operation_id):
    """Read one existing DELETE-mode WorkJournal row without its constructor.

    No migration, recovery, lock creation or journal-mode change is allowed.
    The held private database identity and its signature bracket the fixed
    read-only SQL. A concurrent writer or a sidecar causes a bounded refusal.
    """
    with PrivateDirectory(directory) as private:
        if _directory_pin(directory)!=pin: fail()
        name='work-journal.sqlite3'; descriptor=None; db=None
        def guard():
            private.verify(); private.validate_file(descriptor)
            if (signature(os.stat(name,dir_fd=private.fd,follow_symlinks=False))!=before
                    or signature(os.fstat(descriptor))!=before or _directory_pin(directory)!=pin): fail()
            for suffix in ('-journal','-wal','-shm'):
                try: os.stat(name+suffix,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError: continue
                fail('development_pool_delivery_journal_pending')
        try:
            descriptor=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
            private.validate_file(descriptor); before=signature(os.fstat(descriptor))
            if not 0<os.fstat(descriptor).st_size<=32*1024*1024: fail()
            guard()
            db=sqlite3.connect((Path(directory)/name).as_uri()+'?mode=ro',uri=True,timeout=0,isolation_level=None)
            db.row_factory=sqlite3.Row
            def readonly(action, first, second, *_):
                if action in (sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ): return sqlite3.SQLITE_OK
                if (action==sqlite3.SQLITE_PRAGMA and
                        ((first in ('user_version','application_id','journal_mode') and second is None)
                         or first=='table_info' and second=='operations')): return sqlite3.SQLITE_OK
                return sqlite3.SQLITE_DENY
            db.set_authorizer(readonly)
            if (db.execute('PRAGMA journal_mode').fetchone()[0]!='delete'
                    or db.execute('PRAGMA user_version').fetchone()[0] not in (1,2)
                    or db.execute('PRAGMA application_id').fetchone()[0]!=_APPLICATION_ID
                    or tuple(row['name'] for row in db.execute('PRAGMA table_info(operations)'))!=_COLUMNS): fail()
            row=db.execute('SELECT * FROM operations WHERE id=?',(operation_id,)).fetchone()
            if row is None: fail('development_pool_delivery_operation_missing')
            result=WorkJournal._decode(row); guard(); return result
        finally:
            if db is not None: db.close()
            if descriptor is not None: os.close(descriptor)


def _existing_fence(op, manifest, planner):
    """Read the canonical existing standalone row; never create/bind/attach.

    The shared OS lock covers only bounded local metadata. No RPC, Git or
    worker effect is performed while it is held. Unavailable/contended refuses.
    """
    descriptor = None
    with PrivateDirectory(canonical_anchor()) as private:
        try:
            descriptor = os.open('fence.lock',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=private.fd)
            private.validate_file(descriptor); pin = identity(os.fstat(descriptor))
            fcntl.flock(descriptor,fcntl.LOCK_SH|fcntl.LOCK_NB)
            state = DevelopmentFence._validate(object.__new__(DevelopmentFence),decode(private.read('fence.json',2*1024*1024)))
            expected = dict(scope=op['scope'],root_identity=manifest['root_identity'],journal=op['configuration']['pins']['broker'],
                configuration_hash=digest(canonical(planner['configuration'])),
                child_scope=dict(work_id=development_work_id(op['id']),runs=op['configuration']['pins']['runs'],runtime=op['configuration']['pins']['runtime']),
                parent_scope=op['payload']['parent_binding']['parent_scope'])
            row = state['records'].get(DevelopmentFence.key(op['scope'],manifest['root_identity']))
            if row != dict(binding=expected,operation_id=op['id']): fail('development_pool_delivery_fence_changed')
            private.verify(); private.validate_file(descriptor)
            if identity(os.stat('fence.lock',dir_fd=private.fd,follow_symlinks=False))!=pin: fail()
        except OSError: fail('development_pool_delivery_fence_unavailable')
        finally:
            if descriptor is not None: os.close(descriptor)


class DevelopmentPoolResultDelivery:
    def __init__(self, directory, *, pool, driver, workspaces, publication_grant,
                 gh_executable, python_executable, driver_id='development'):
        if (type(pool) is not WorkerPool or type(driver) is not DevelopmentPoolDriver
                or type(workspaces) is not LocalGitWorkspaceController or type(publication_grant) is not GitHubPublicationGrant):
            fail('development_pool_delivery_concrete_handles_required')
        binding = pool.drivers.get(driver_id)
        grant = publication_grant.document()
        if (binding is None or binding.revision!=REVISION or getattr(binding.factory,'__self__',None) is not driver
                or workspaces.grant['owner_id']!=driver.selections.fixed['owner_id']
                or grant['owner_id']!=workspaces.grant['owner_id'] or grant['base_commit']!=workspaces.grant['base_commit']):
            fail('development_pool_delivery_scope_mismatch')
        self.pool,self.driver,self.workspaces,self.driver_id = pool,driver,workspaces,driver_id
        self.grant,self._grant = grant,publication_grant
        self.directory,self.gh,self.python = _path(os.fspath(directory)),_path(os.fspath(gh_executable)),_path(os.fspath(python_executable))
        forbidden = (workspaces.repository,workspaces.workspace_root,workspaces.state_root,
                     pool.journal.private.path,Path(driver.selections.directory))
        if any(self.directory.is_relative_to(root) or root.is_relative_to(self.directory) for root in forbidden):
            fail('development_pool_delivery_roots_overlap')
        self.paths = {name:self.directory/name for name in ('operations','deliveries','reviews','publications')}
        parent = absolute_directory(self.directory.parent)
        try:
            try: os.mkdir(self.directory.name,0o700,dir_fd=parent); os.fsync(parent)
            except FileExistsError: pass
        finally: os.close(parent)
        with PrivateDirectory(self.directory): pass
        for path in self.paths.values():
            with PrivateDirectory(path,create=True): pass
        self.pins = {name:_directory_pin(path) for name,path in {'root':self.directory,**self.paths}.items()}
        self.pool_pin = _directory_pin(pool.journal.private.path)

    def _job(self, operation_id, *, historical=False):
        uuid_value(operation_id)
        with self.pool.mutex:
            self.pool._ready(); state = self.pool.journal.read(); job = deepcopy(self.pool._job(state,operation_id))
            if (job['state']!='settled' or job['outcome']!='complete' or job['cleanup_confirmed'] is not True
                    or job['execution_active'] is not False or job['stop_requested']
                    or job['driver_id']!=self.driver_id or job['driver_revision']!=REVISION
                    or operation_id in self.pool.launching or operation_id in self.pool.calling or operation_id in self.pool.reconciling
                    or self.pool.threads.get(operation_id) is not None and self.pool.threads[operation_id].is_alive()
                    or not historical and (state['stopped'] or self.pool.admissions_blocked or self.pool.stop_unconfirmed)):
                fail('development_pool_delivery_private_result_required')
            return state['id'],job

    @contextmanager
    def _locked(self, operation_id, *, create=False):
        uuid_value(operation_id)
        if self.pins!={name:_directory_pin(path) for name,path in {'root':self.directory,**self.paths}.items()}: fail()
        fresh = False
        with PrivateDirectory(self.paths['operations']) as parent:
            if create:
                try: os.mkdir(operation_id,0o700,dir_fd=parent.fd); os.fsync(parent.fd); fresh=True
                except FileExistsError: pass
        with PrivateDirectory(self.paths['operations']/operation_id) as private:
            descriptor = None
            try:
                descriptor = os.open('delivery.lock',os.O_RDWR|os.O_NOFOLLOW|(os.O_CREAT|os.O_EXCL if fresh else 0),0o600,dir_fd=private.fd)
                private.validate_file(descriptor); pin = identity(os.fstat(descriptor))
                fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
                def held():
                    private.verify(); private.validate_file(descriptor)
                    if identity(os.stat('delivery.lock',dir_fd=private.fd,follow_symlinks=False))!=pin: fail()
                held(); yield private,held
            except OSError: fail('development_pool_delivery_busy_or_incomplete')
            finally:
                if descriptor is not None: os.close(descriptor)

    @staticmethod
    def _read(private, name):
        raw = private.read(name+'.json',MAX_DOCUMENT); value = decode(raw)
        if canonical(value)!=raw: fail()
        return value

    @classmethod
    def _remember(cls, private, name, value):
        raw = canonical(value)
        if len(raw)>MAX_DOCUMENT: fail('development_pool_delivery_limit')
        try: private.create(name+'.json',raw)
        except DevelopmentError as error:
            if error.code!='private_file_exists' or cls._read(private,name)!=value: fail('development_pool_delivery_intent_conflict')

    @staticmethod
    def _names(private):
        names = set(os.listdir(private.fd)); allowed = {'delivery.lock'}|{name+'.json' for name in STEPS}
        if not names<=allowed: fail('development_pool_delivery_record_changed')
        present = [name for name in STEPS if name+'.json' in names]
        if present!=list(STEPS[:len(present)]): fail('development_pool_delivery_incomplete')
        return present

    def _queue(self, context, bound):
        wanted = {'development-prepare-intent.json':dict(operation_id=context.operation_id,session_id=context.session_id,selection_sha256=digest(canonical(bound))),
            'development-run-intent.json':dict(operation_id=context.operation_id,session_id=context.session_id),
            'development-result.json':dict(operation_id=context.operation_id,session_id=context.session_id,
                code_status='private_review_ready',ticket_published=False,cleanup_confirmed=True)}
        with PrivateDirectory(context.journals['queue']) as private:
            for name,value in wanted.items():
                if private.read(name,128*1024)!=canonical(value): fail('development_pool_delivery_private_receipt_changed')

    def _complete(self, private, request):
        """Cross-check the historical chain, without claiming live freshness."""
        seal,published,board,handoff = (self._read(private,name) for name in ('sealed','published','board','handoff'))
        evidence = self._read(private,'evidence'); pr = published['pull_request']; job = request['job']
        sha(seal['delivery_sha']);sha(seal['tree_sha'])
        if (seal['snapshot_sha256']!=self._read(private,'captured')['snapshot_sha256'] or seal['test_evidence']!=evidence
                or seal['message']!=request['commit_message'] or seal['base_commit']!=request['grant']['base_commit']
                or published['operation_id']!=job['id'] or published['status']!='complete' or published['phase']!='complete'
                or published['delivery_sha']!=seal['delivery_sha'] or published['branch']!='codex/delivery-'+job['id']
                or set(pr)!={'number','url','head_sha','base_sha','draft'} or type(pr['number']) is not int or pr['number']<1
                or pr['url']!='https://github.com/'+REPOSITORY+'/pull/'+str(pr['number']) or pr['draft'] is not True
                or pr['head_sha']!=seal['delivery_sha'] or pr['base_sha']!=request['grant']['base_commit']): fail()
        for name,value in (
                ('capture-intent',dict(request_sha256=digest(canonical(request)))),
                ('seal-intent',dict(snapshot_sha256=seal['snapshot_sha256'],test_evidence_sha256=digest(canonical(evidence)),message=request['commit_message'])),
                ('publication-intent',dict(delivery_sha=seal['delivery_sha'],title=request['pr_title'],body=request['pr_body'])),
                ('board-intent',dict(publication_sha256=digest(canonical(published)))),
                ('handoff-intent',dict(board_sha256=digest(canonical(board))))):
            if self._read(private,name)!=value: fail()
        publication = board['publication']; delivery = publication['delivery']
        expected_handoff = dict(ticket_id=job['payload']['ticket_id'],version=board['version']+1,status='review',assignee_agent_id=None)
        if (board['version']!=publication['expected_version']+1 or publication['changes']['status']!='review'
                or publication['changes']['release_claim'] is not True or delivery['delivery_sha']!=seal['delivery_sha']
                or delivery['tree_sha']!=seal['tree_sha'] or delivery['snapshot_sha256']!=seal['snapshot_sha256']
                or delivery['pull_request']!=pr or handoff!=dict(operation_id=job['id'],status='complete',result=expected_handoff)):
            fail()
        self._components(request,seal,published,board,handoff)
        expected = dict(operation_id=job['id'],session_id=job['session_id'],status='complete',delivery_sha=seal['delivery_sha'],
            tree_sha=seal['tree_sha'],snapshot_sha256=seal['snapshot_sha256'],pull_request=pr,handoff=expected_handoff)
        if self._read(private,'complete')!=expected: fail('development_pool_delivery_receipt_changed')
        return deepcopy(expected)

    def _components(self, request, seal, published, board, handoff):
        """Cross the real durable component receipts, with no constructor/RPC.

        These are historical receipts, not a new commit/remote/Board freshness
        proof. Missing records are never recreated from the pipeline's copies.
        """
        job=request['job']; operation_id=job['id']; authority=request['authority']
        scope={**self.driver.selections.fixed,**job['payload'],'board_session_id':job['session_id']}
        work=self.pool.journal.private.path/'workers'/job['worker_id']/'work'
        if (request['roots']!=self.pins or request['pool']!=self.pool_pin
                or request['local_grant']!=self.workspaces.grant or request['git_pins']!=self.workspaces.pins
                or request['grant']!=self.grant or authority['operation_id']!=operation_id
                or authority['scope']!=scope or authority['journal']['path']!=str(work)
                or self._job(operation_id,historical=True)!=(request['pool_id'],job)): fail()
        observations=[]
        def read(path, maximum):
            raw,pin=file_document(path,maximum); value=decode(raw)
            if canonical(value)!=raw: fail()
            observations.append((path,maximum,pin)); return value
        op=_historical_operation(work,authority['journal'],operation_id)
        payload=op['payload']; config=op['configuration']
        manifest=validate_manifest(read(Path(config['pins']['broker']['path'])/'manifest.json',128*1024))
        manifest_scope=manifest['scope']
        if (any(manifest_scope[key]!=scope[key] for key in ('owner_id','agent_id','project_id','ticket_id'))
                or manifest_scope['session_id']!=scope['board_session_id']
                or digest(canonical(manifest))!=authority['manifest_sha256']): fail()
        with PrivateDirectory(self.workspaces.state_root/operation_id) as workspace_private:
            workspace=self.workspaces._read(workspace_private)
        actual=read(self.paths['deliveries']/operation_id/'delivery.json',8*1024*1024)
        git_request=dict(operation_id=operation_id,scope=manifest_scope,changes=request['changes'],
            workspace_request_sha256=workspace['request_sha256'],checkout=authority['root'],
            base_commit=request['grant']['base_commit'])
        if (set(actual)!={'schema','phase','request','snapshot','snapshot_sha256','metadata_sha256','seal','reason'}
                or actual['schema']!='local-git-delivery-v1' or actual['phase']!='sealed'
                or actual['request']!=git_request or workspace['phase']!='ready'
                or workspace['request']['scope']!=manifest_scope or workspace['request']['checkout']!=authority['root']
                or actual['snapshot']['schema']!='local-captured-tree-v1' or actual['snapshot']['request']!=git_request
                or actual['snapshot_sha256']!=digest(canonical(actual['snapshot']))
                or actual['snapshot_sha256']!=seal['snapshot_sha256'] or actual['seal']!=seal): fail()
        publication=read(self.paths['publications']/operation_id/'publication.json',128*1024)
        publication_request=dict(operation_id=operation_id,grant=request['grant'],local_grant=request['local_grant'],
            delivery_sha=seal['delivery_sha'],tree_sha=seal['tree_sha'],snapshot_sha256=seal['snapshot_sha256'],
            scope=manifest_scope,branch='codex/delivery-'+operation_id,title=request['pr_title'],body=request['pr_body'],authority=authority)
        if (set(publication)!={'schema','phase','request','request_sha256','pins','login','transport_identity','transport_metadata','pr','reason'}
                or publication['schema']!='github-draft-publication-v1' or publication['phase']!='complete'
                or publication['request']!=publication_request or publication['request_sha256']!=digest(canonical(publication_request))
                or publication['pins']!=request['publisher_pins'] or GitHubDraftPublisher._public(publication)!=published): fail()
        if (op['scope']!=scope or op['stage']!='complete' or config['pins']['journal']!=authority['journal']
                or digest(canonical(config))!=request['configuration_sha256']
                or digest(canonical(config))!=authority['configuration_sha256']
                or digest(canonical(payload['registration']))!=authority['registration_sha256']
                or digest(canonical(payload['context']))!=authority['context_sha256']
                or payload['claim_id']!=authority['claim_id'] or payload['claim_version']!=authority['claim_version']
                or config['manifest_hash']!=authority['manifest_sha256']
                or payload.get('publication_confirmed') is not True or payload['publication']!=board['publication']): fail()
        # _request is a pure projection of this completed operation; it does
        # not dereference its instance, read a client or acquire a worker lock.
        handoff_request=DevelopmentReviewHandoff._request(None,op)
        actual_handoff=read(work/DevelopmentReviewHandoff._name(operation_id),128*1024)
        if actual_handoff!=dict(schema='development-review-handoff-v1',phase='complete',request=handoff_request,
                request_sha256=digest(canonical(handoff_request)),result=handoff['result']): fail()
        # Recheck every held source after the last read, including the exact
        # WorkJournal row. No callback can repair or manufacture this chain.
        if _historical_operation(work,authority['journal'],operation_id)!=op: fail()
        for path,maximum,pin in observations:
            if file_document(path,maximum)[1]!=pin: fail()

    @staticmethod
    def _runtime(worker, op):
        rows = worker.runtime.list_status()
        if not 1<=len(rows)<=128: fail('development_pool_delivery_runtime_unconfirmed')
        for row in rows:
            record = worker.runtime.get(row['id']); config = record['configuration']
            if (config['scope']!=op['scope'] or config['work_id']!=development_work_id(op['id'])
                    or record['phase']!='finished' or record['start_confirmed']!=1
                    or type(record['receipt']) is not dict): fail('development_pool_delivery_runtime_unconfirmed')
            found = worker.control.call('lookup_execution',{'idempotency_key':record['key']})
            if type(found) is not dict or found.get('found') is not True: fail('development_pool_delivery_runtime_unconfirmed')
            guard = RuntimeGuard(None,worker.control,op['scope']); guard.operation = record
            receipt = guard.metadata(found.get('execution'))
            if (receipt!=record['receipt'] or receipt['state']!='finished' or receipt['cleanup_confirmed'] is not True
                    or receipt.get('outcome')!='completed' or receipt.get('reason')!='completed'):
                fail('development_pool_delivery_runtime_unconfirmed')

    def _fresh(self, worker, context, bound, request, private, held, *, execution=False):
        worker._local_control(context.operation_id)
        pool_id,job = self._job(context.operation_id)
        if (pool_id!=request['pool_id'] or job!=request['job'] or _directory_pin(self.pool.journal.private.path)!=self.pool_pin
                or self.driver.selections.load(context)!=bound or self.grant!=self._grant.document()): fail()
        self._queue(context,bound)
        op = self.driver._operation(worker,context,bound)
        if (op['stage'] not in ('result_ready','publishing','complete')
                or digest(canonical(op['configuration']))!=request['configuration_sha256']): fail()
        with PrivateDirectory(bound['pins']['broker']['path']) as broker:
            manifest = decode(broker.read('manifest.json',128*1024)); planner = decode(broker.read('planner.json',2*1024*1024))
        _existing_fence(op,manifest,planner)
        self._runtime(worker,op)
        if self._job(context.operation_id)!=(pool_id,job) or self.driver.selections.load(context)!=bound: fail()
        if self._read(private,'intent')!={'request':request,'request_sha256':digest(canonical(request))}: fail()
        held(); worker._local_control(context.operation_id)
        if execution and op['stage']=='result_ready':
            authority = DevelopmentGitPublicationAuthority(worker,context.operation_id)
            frame = dict(operation_id=context.operation_id,grant=self.grant,scope=bound['manifest_scope'],authority=authority.descriptor())
            authority.before_effect(operation_id=context.operation_id,grant=self.grant,request=frame)
            authority.confirm_local(operation_id=context.operation_id,grant=self.grant,request=frame)
        return op

    def deliver(self, operation_id, *, changes, commit_message, pr_title, pr_body):
        """Explicitly deliver only this selected archived result; never dispatch."""
        token=_DELIVERY_PHASE.set('delivery')
        try:
            return self._deliver(operation_id,changes=changes,commit_message=commit_message,pr_title=pr_title,pr_body=pr_body)
        except DevelopmentError: raise
        except Exception as error:raise _delivery_failure(error,_DELIVERY_PHASE.get()) from None
        finally:_DELIVERY_PHASE.reset(token)

    def _deliver(self, operation_id, *, changes, commit_message, pr_title, pr_body):
        self._job(operation_id,historical=True)
        changes = _changes(changes)
        if any(row['sha256'] is None for row in changes): fail('development_pool_delivery_existing_files_only')
        for value,maximum in ((commit_message,4096),(pr_title,200),(pr_body,16000)):
            if type(value) is not str or not value.strip() or len(value.encode())>maximum: fail('development_pool_delivery_text_refused')
            check_content(value.encode())
        if ('\0' in commit_message or any(ord(c)<32 or ord(c)==127 for c in pr_title)
                or '<!-- blackoutburst-delivery:' in pr_body): fail('development_pool_delivery_text_refused')
        with self._locked(operation_id,create=True) as (private,held), ExitStack() as resources:
            names = self._names(private)
            pool_id,job = self._job(operation_id,historical=True)
            context = self.pool._context(job,create=False); bound = self.driver.selections.load(context)
            self._queue(context,bound)
            if 'complete' not in names: self._job(operation_id)
            worker = self.driver._open(resources,context,bound,existing=True,observe=False); worker.client.adopt_registration()
            op = self.driver._operation(worker,context,bound)
            if op['payload']['claimed_ticket'].get('assignee_agent_id')!=op['scope']['agent_id']:
                fail('development_pool_delivery_author_assignment_required')
            proof = DevelopmentDeliveryProof(worker,self.workspaces,operation_id)
            deliveries = LocalGitDeliveryController(self.workspaces,self.paths['deliveries'],self.paths['reviews'],test_guard=proof)
            authority = DevelopmentGitPublicationAuthority(worker,operation_id)
            publisher = GitHubDraftPublisher(deliveries,self.paths['publications'],grant=self._grant,
                gh_executable=self.gh,python_executable=self.python,authority=authority)
            request = dict(schema='development-pool-result-delivery-v1',pool_id=pool_id,pool=self.pool_pin,job=job,
                selection_sha256=digest(canonical(bound)),configuration_sha256=digest(canonical(op['configuration'])),
                authority=authority.descriptor(),local_grant=deepcopy(self.workspaces.grant),git_pins=deepcopy(self.workspaces.pins),
                grant=self.grant,publisher_pins=deepcopy(publisher.pins),roots=self.pins,changes=changes,
                commit_message=commit_message,pr_title=pr_title,pr_body=pr_body)
            intent = dict(request=request,request_sha256=digest(canonical(request)))
            self._remember(private,'intent',intent)
            if 'complete' in names: return self._complete(private,request)
            # Replaying after Board publication can only read already-complete
            # Git/PR components. It must not recreate a lost capture/commit/PR.
            if op['stage'] in ('publishing','complete'):
                for path,phase in ((self.paths['deliveries']/operation_id/'delivery.json','sealed'),
                                   (self.paths['publications']/operation_id/'publication.json','complete')):
                    if decode(file_document(path,8*1024*1024)[0]).get('phase')!=phase: fail()
            self._remember(private,'capture-intent',dict(request_sha256=intent['request_sha256']))
            self._fresh(worker,context,bound,request,private,held,execution=True)
            snapshot = deliveries.capture(operation_id=operation_id,scope=bound['manifest_scope'],changes=changes)
            snapshot_sha = digest(canonical(snapshot))
            self._remember(private,'captured',dict(snapshot_sha256=snapshot_sha))
            evidence = proof.evidence(snapshot); self._remember(private,'evidence',evidence)
            self._remember(private,'seal-intent',dict(snapshot_sha256=snapshot_sha,test_evidence_sha256=digest(canonical(evidence)),message=commit_message))
            self._fresh(worker,context,bound,request,private,held,execution=True)
            seal = deliveries.finalize(operation_id=operation_id,snapshot_sha256=snapshot_sha,test_evidence=evidence,message=commit_message)
            self._remember(private,'sealed',seal)
            self._remember(private,'publication-intent',dict(delivery_sha=seal['delivery_sha'],title=pr_title,body=pr_body))
            self._fresh(worker,context,bound,request,private,held,execution=True)
            result = publisher.publish(operation_id=operation_id,delivery_sha=seal['delivery_sha'],title=pr_title,body=pr_body)
            if result['status']!='complete': fail('development_pool_delivery_publication_unconfirmed')
            self._remember(private,'published',result)
            link = DevelopmentPublishedDelivery(proof,deliveries,publisher)
            self._remember(private,'board-intent',dict(publication_sha256=digest(canonical(result))))
            _DELIVERY_PHASE.set('board_handoff')
            self._fresh(worker,context,bound,request,private,held)
            with standalone_fence_wait(): result = worker.publish(operation_id,delivery_publication=link)
            op = worker.journal.get(operation_id)
            if result['stage']!='complete' or op['payload'].get('publication_confirmed') is not True:
                fail('development_pool_delivery_board_unconfirmed')
            board = dict(publication=op['payload']['publication'],version=op['payload']['publication']['expected_version']+1)
            self._remember(private,'board',board)
            self._remember(private,'handoff-intent',dict(board_sha256=digest(canonical(board))))
            self._fresh(worker,context,bound,request,private,held)
            with standalone_fence_wait(): handoff = DevelopmentReviewHandoff(worker,link).release(operation_id)
            if handoff['status']!='complete': fail('development_pool_delivery_handoff_unconfirmed')
            self._remember(private,'handoff',handoff)
            complete = dict(operation_id=operation_id,session_id=context.session_id,status='complete',
                delivery_sha=seal['delivery_sha'],tree_sha=seal['tree_sha'],snapshot_sha256=snapshot_sha,
                pull_request=publisher.status(operation_id)['pull_request'],handoff=handoff['result'])
            held(); self._remember(private,'complete',complete)
            return self._complete(private,request)

    def status(self, operation_id):
        """Historical local receipt only; no reopening workers or RPC."""
        try: return self._status(operation_id)
        except DevelopmentError: raise
        except Exception: fail('development_pool_delivery_record_unavailable')

    def _status(self, operation_id):
        with self._locked(operation_id) as (private,held):
            names = self._names(private); intent = self._read(private,'intent'); held()
            if intent['request']['job']['id']!=operation_id or digest(canonical(intent['request']))!=intent['request_sha256']: fail()
            return self._complete(private,intent['request']) if 'complete' in names else dict(operation_id=operation_id,status='retained',stage=names[-1])

    def stop(self, operation_id):
        """Stop this delivery locally via the exact existing worker journal."""
        _,job = self._job(operation_id,historical=True); context = self.pool._context(job,create=False)
        file_document(Path(context.journals['work'])/'work-journal.sqlite3',32*1024*1024)
        with WorkJournal(context.journals['work']) as journal:
            op = journal.get(operation_id)
            expected = {**self.driver.selections.fixed,**job['payload'],'board_session_id':job['session_id']}
            if op['scope']!=expected or op['configuration']['pins']['journal']!=_directory_pin(journal.directory): fail()
            return DevelopmentBoardWorker(journal,None,None,None,None).stop(operation_id)
