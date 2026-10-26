"""Concrete opt-in Linux development driver for WorkerPool.

Successful code results remain private. A failed run may publish only closed
diagnostic telemetry on its selected ticket using its existing message rights.
Settled Pool capacity is never release of a Board claim, ticket completion or
Git delivery; notification never changes cleanup or execution authority.
"""
from contextlib import ExitStack, closing
import errno
from pathlib import Path
import threading

from .board_reader import BoardReadError
from .client_lifecycle import own_client
from .development_client import DevelopmentBoardClient, failure_notice
from .development_pool_selection import (DevelopmentSelections, REVISION, fail,
                                         file_document)
from .development_worker import DevelopmentBoardWorker, execution_factories
from .dev_journal import PrivateDirectory, decode
from .dev_fence import standalone_fence_wait
from .dev_manifest import DevelopmentError, canonical, digest
from .dev_planner import _directory_pin
from .dev_planner_journal import PlannerBudgets
from .dev_runtime import development_work_id
from .run_store import RunStore
from .runtime_client import RuntimeBoardClient
from .runtime_guard import RuntimeGuard
from .runtime_journal import RuntimeJournal
from .work_journal import WorkJournal
from .worker_pool import PoolDriverBinding, PoolOutcome


NOTICE_BOARD_ERRORS = frozenset({'invalid_failure_notice', 'failure_notice_unconfirmed',
    'connection_closed', 'redirect_blocked', 'authentication_failed', 'permission_denied',
    'http_error', 'response_too_large', 'invalid_response', 'timeout', 'connection_failed',
    'rpc_rejected', 'tool_rejected', 'session_mismatch'})
NOTICE_STORAGE_ERRORS = frozenset({'private_file_exists', 'private_read_failed',
    'private_write_failed', 'private_directory_changed', 'private_directory_unavailable',
    'insecure_private_directory', 'insecure_private_file', 'development_notice_receipt_conflict'})
NOTICE_ERRNOS = frozenset({errno.EPERM, errno.ENOENT, errno.EIO, errno.EBADF,
    errno.EACCES, errno.EEXIST, errno.ENOTDIR, errno.EINVAL, errno.EMFILE,
    errno.ENOSPC, errno.EROFS, errno.ETIMEDOUT})
NOTICE_OS_ERRORS = (OSError, PermissionError, FileNotFoundError, FileExistsError,
    NotADirectoryError, IsADirectoryError, BlockingIOError, InterruptedError,
    BrokenPipeError, ConnectionError, ConnectionAbortedError, ConnectionRefusedError,
    ConnectionResetError)


def failure_notice_diagnostic(phase, error, notice):
    """Closed local error facts; never inspect an arbitrary exception's text.

    Unknown subclasses cannot supply properties, coercions or serialization.
    A post RPC error is uncertain even if the server may have committed it.
    """
    if type(phase) is not str or phase not in {'post_rpc', 'receipt_write'}:
        return None
    category, code, number = 'unknown_error', 'unknown', None
    if type(error) is BoardReadError:
        category = 'board_read_error'
        value = vars(error).get('code')
        if type(value) is str and value in NOTICE_BOARD_ERRORS: code = value
    elif type(error) is DevelopmentError:
        category = 'storage_error'
        value = vars(error).get('code')
        if type(value) is str and value in NOTICE_STORAGE_ERRORS: code = value
    elif type(error) is TimeoutError:
        category, code = 'timeout_error', 'timeout'
    elif type(error) in NOTICE_OS_ERRORS:
        category, code = 'os_error', 'os_error'
        value = error.errno
        if type(value) is int and value in NOTICE_ERRNOS: number = value
    notice_code = failure_notice(None)['code']
    if type(notice) is dict:
        value = notice.get('code')
        # Reuse the existing closed notice codec, not arbitrary run prose.
        notice_code = failure_notice(value)['code']
    return {'schema': 1, 'status': 'notification_diagnostic',
        'kind': 'development_failure_notice', 'phase': phase,
        'category': category, 'code': code, 'errno': number,
        'delivery_state': 'confirmed' if phase == 'receipt_write' else 'unknown',
        'notice_code': notice_code}


class _ObservedRuntime:
    """Observe the real channel's correlated receipts, never a run() return bit."""
    def __init__(self, client, journal, bound, context):
        self.client, self.journal, self.bound, self.context = client, journal, bound, context
        self.endpoint, self.session_id = client.endpoint, client.session_id
        self.active = set()

    def call(self, name, arguments):
        result = self.client.call(name, arguments)
        if name in ('start_execution', 'heartbeat_execution', 'finish_execution', 'lookup_execution'):
            value = result.get('execution') if name == 'lookup_execution' and type(result) is dict else result
            if type(value) is not dict or type(value.get('id')) is not str:
                fail('development_pool_runtime_unconfirmed')
            rows = self.journal.db.execute("SELECT id FROM executions WHERE json_extract(receipt,'$.id')=? LIMIT 2",
                                           (value['id'],)).fetchall()
            if len(rows) != 1: fail('development_pool_runtime_unconfirmed')
            record = self.journal.get(rows[0]['id'])
            self.validate(record, value)
            if value['state'] == 'running': self.active.add(value['id'])
            elif value['state'] == 'finished' and value['cleanup_confirmed'] is True:
                self.active.discard(value['id'])
            self.context.report_execution(bool(self.active))
        return result

    def validate(self, record, value):
        scope = self.bound['selection']['scope']
        configuration = record['configuration']
        if (configuration['scope'] != scope
                or configuration['work_id'] != development_work_id(self.context.operation_id)):
            fail('development_pool_runtime_scope_mismatch')
        validator = RuntimeGuard(self.journal, self.client, scope)
        validator.operation = record
        receipt = validator.metadata(value)
        manifest = self.bound['manifest_scope']
        if any(receipt[key] != manifest[key] for key in ('device_id', 'profile_version')):
            fail('development_pool_runtime_scope_mismatch')
        return receipt


class _StopCompanion:
    """Local cooperative stop only; no RPC and no SQLite object crosses threads."""
    def __init__(self, context):
        self.context, self.done, self.error = context, threading.Event(), None
        self.thread = threading.Thread(target=self._run, name='development-stop-' + context.worker_id,
                                       daemon=False)
    def start(self): self.thread.start()
    def _run(self):
        try:
            while not self.done.wait(0.025):
                if self.context.should_stop():
                    with WorkJournal(self.context.journals['work']) as journal:
                        DevelopmentBoardWorker(journal, None, None, None, None).stop(self.context.operation_id)
                    return
        except BaseException:
            self.error = 'development_pool_stop_unconfirmed'
    def close(self):
        self.done.set()
        self.thread.join(timeout=1)
        if self.thread.is_alive() or self.error: fail('development_pool_stop_unconfirmed')


class DevelopmentPoolDriver:
    """Protected local binding; constructors never register or launch anything.

    bind_selection is explicit and must finish before dispatch_pending. The
    factory, run, reconciliation and verification all reopen their own handles.
    There is no injected runner/provider/backend or production fixture callback.
    """
    def __init__(self, selection_directory, *, connection_file, owner_id, agent_id, board_endpoint):
        self.selections = DevelopmentSelections(selection_directory, connection_file=connection_file,
            owner_id=owner_id, agent_id=agent_id, board_endpoint=board_endpoint)

    def binding(self):
        return PoolDriverBinding(REVISION, self._factory, self._reconcile, self._verify_cleanup)

    def bind_selection(self, job, approved_selection_file):
        raw, _ = file_document(approved_selection_file)
        selection = decode(raw)
        if (type(selection) is not dict or type(selection.get('scope')) is not dict
                or not {'project_id', 'ticket_id'} <= set(selection['scope'])): fail()
        payload = {key: selection['scope'][key] for key in ('project_id', 'ticket_id')}
        return self.selections.bind(job, approved_selection_file, payload)

    def _factory(self, context):
        return _DevelopmentRun(self, context)

    def _open(self, resources, context, bound, *, existing=False, observe=False):
        if existing:
            # These are evidence, not an invitation to create empty journals.
            for kind, name in (('work', 'work-journal.sqlite3'), ('runs', 'runs.sqlite3'),
                               ('runtime', 'runtime.sqlite3')):
                file_document(Path(context.journals[kind]) / name, 32 * 1024 * 1024)
        journal = resources.enter_context(WorkJournal(context.journals['work']))
        store = resources.enter_context(closing(RunStore(context.journals['runs'], existing_only=existing)))
        runtime = resources.enter_context(RuntimeJournal(context.journals['runtime']))
        client = own_client(resources, DevelopmentBoardClient.from_connection_file(
            self.selections.connection_file, session_id=context.session_id, timeout=3))
        if (client.endpoint != bound['selection']['scope']['board_endpoint']
                or list(client.connection_identity) != bound['connection']['identity']
                or file_document(self.selections.connection_file)[1] != bound['connection']):
            fail('development_pool_connection_changed')
        control = own_client(resources, RuntimeBoardClient.from_worker(client))
        if observe: control = _ObservedRuntime(control, runtime, bound, context)
        worker = DevelopmentBoardWorker(journal, store, runtime, client, control,
                                       **execution_factories('linux-python-execution-v1'))
        return worker

    @staticmethod
    def _operation(worker, context, bound):
        op = worker.journal.get(context.operation_id)
        config = op['configuration']
        expected = {'journal': context.journals['work'], 'runs': context.journals['runs'],
                    'runtime': context.journals['runtime'],
                    **{name: pin['path'] for name, pin in bound['pins'].items()}}
        if (op['scope'] != bound['selection']['scope'] or config.get('kind') != 'development'
                or config.get('controller_scope') is not None
                or config.get('execution_profile') != 'linux-python-execution-v1'
                or config.get('manifest_hash') != bound['documents']['manifest']['sha256']
                or config.get('linux_execution_plan_sha256') != bound['documents']['backend_plan']['sha256']
                or config.get('policy_hash') != bound['documents']['policy_plan']['sha256']
                or config.get('model') != bound['selection']['model']
                or config.get('instruction') != bound['instruction']
                or config.get('budgets') != bound['selection']['budgets']
                or any(config['pins'].get(name) != _directory_pin(path) for name, path in expected.items())):
            fail('development_pool_work_scope_mismatch')
        return op

    def _cleanup(self, worker, context, bound, *, reconcile):
        self._operation(worker, context, bound)
        observer = _ObservedRuntime(worker.control, worker.runtime, bound, context)
        records = [worker.runtime.get(row['id']) for row in worker.runtime.list_status()]
        if len(records) > 128: fail('development_pool_cleanup_unconfirmed')
        work_id = development_work_id(context.operation_id)
        works = worker.store._db.execute('SELECT id FROM works LIMIT 2').fetchall()
        if len(works) != 1 or works[0]['id'] != work_id:
            fail('development_pool_cleanup_unconfirmed')
        for record in records:
            configuration = record['configuration']
            if (configuration['scope'] != bound['selection']['scope']
                    or configuration['work_id'] != work_id):
                fail('development_pool_cleanup_unconfirmed')
            if record['receipt'] is not None: observer.validate(record, record['receipt'])
        if not reconcile:
            for record in records:
                if (record['phase'] != 'finished' or record['receipt'] is None
                        or record['receipt'].get('cleanup_confirmed') is not True):
                    fail('development_pool_cleanup_unconfirmed')
        # Existing cleanup validates planner, RunStore turns, exact broker/native
        # effects and their reservation keys. It can finish only proved cleanup.
        result = worker.reconcile_cleanup(context.operation_id)
        if (result.get('operation_id') != context.operation_id or result.get('cleanup_confirmed') is not True):
            fail('development_pool_cleanup_unconfirmed')
        for record in records:
            current = worker.runtime.get(record['id'])
            if current['phase'] != 'finished': fail('development_pool_cleanup_unconfirmed')
            found = worker.control.call('lookup_execution', {'idempotency_key': current['key']})
            if type(found) is not dict or found.get('found') is not True:
                fail('development_pool_cleanup_unconfirmed')
            receipt = observer.validate(current, found.get('execution'))
            if (receipt['state'] != 'finished' or receipt['cleanup_confirmed'] is not True
                    or receipt != current['receipt']): fail('development_pool_cleanup_unconfirmed')
        return True

    def _reconcile(self, context):
        bound = self.selections.load(context)
        with ExitStack() as resources:
            worker = self._open(resources, context, bound, existing=True)
            worker.client.adopt_registration()
            with standalone_fence_wait():
                self._cleanup(worker, context, bound, reconcile=True)
        return PoolOutcome(context.operation_id, context.session_id, 'needs_review', True)

    def _verify_cleanup(self, context, outcome):
        if (outcome.operation_id != context.operation_id or outcome.session_id != context.session_id
                or outcome.cleanup_confirmed is not True): return False
        bound = self.selections.load(context)
        with ExitStack() as resources:
            worker = self._open(resources, context, bound, existing=True)
            worker.client.adopt_registration()
            with standalone_fence_wait():
                return self._cleanup(worker, context, bound, reconcile=False)



class _DevelopmentRun:
    def __init__(self, driver, context):
        self.driver, self.context = driver, context
        # Fail before registration, initialization or any remote call.
        self.bound = driver.selections.load(context)
        self.resources, self.companion = ExitStack(), None
        self.closed = False
        try: self.worker = driver._open(self.resources, context, self.bound, observe=True)
        except BaseException:
            self.resources.close()
            raise

    def run(self):
        context, selection = self.context, self.bound['selection']
        if context.should_stop(): fail('development_pool_stopped')
        with PrivateDirectory(context.journals['queue']) as private:
            private.create('development-prepare-intent.json', canonical({
                'operation_id': context.operation_id, 'session_id': context.session_id,
                'selection_sha256': digest(canonical(self.bound))}))
        with standalone_fence_wait():
            self.worker.prepare(scope=selection['scope'], request_id='pool-' + context.operation_id,
                instruction=self.bound['instruction'], broker_directory=selection['broker_directory'],
                backend_directory=selection['backend_directory'],
                planner_policy_directory=selection['planner_policy_directory'], model=selection['model'],
                budgets=PlannerBudgets(**selection['budgets']), operation_id=context.operation_id)
        self.driver._operation(self.worker, context, self.bound)
        self.companion = _StopCompanion(context)
        self.companion.start()
        if context.should_stop(): self.worker.stop(context.operation_id)
        with PrivateDirectory(context.journals['queue']) as private:
            private.create('development-run-intent.json', canonical({
                'operation_id': context.operation_id, 'session_id': context.session_id}))
        try:
            with standalone_fence_wait():
                result = self.worker.run(context.operation_id)
            self.companion.close(); self.companion = None
            cleaned = False
            try:
                with standalone_fence_wait():
                    cleaned = self.driver._cleanup(self.worker, context, self.bound, reconcile=True)
            except Exception: pass  # Unknown is retained; never manufacture cleanup.
            status = 'complete' if result.get('status') == 'review_ready' else 'needs_review'
            with PrivateDirectory(context.journals['queue']) as private:
                private.create('development-result.json', canonical({'operation_id': context.operation_id,
                    'session_id': context.session_id, 'code_status': 'private_review_ready' if status == 'complete' else 'needs_review',
                    'ticket_published': False, 'cleanup_confirmed': cleaned}))
        except Exception as error:
            self._failure_notice(error)
            raise
        if status!='complete' or cleaned is not True:
            self._failure_notice(result.get('reason') if status!='complete' else 'development_cleanup_requires_review',cleaned)
        return PoolOutcome(context.operation_id, context.session_id, status, cleaned)

    def _failure_notice(self, code, cleaned=False):
        """Best effort only: never alter execution outcome or invent cleanup.

        The intent precedes the one RPC. An explicit repeat can only reuse the
        same notice/key; Pool reconciliation never invokes this method.
        """
        phase, notice = None, None
        try:
            if type(code) is DevelopmentError:code=code.code
            client=self.worker.client
            if (type(client) is not DevelopmentBoardClient or client._registered is not True
                    or client._initialized is not True):return
            context=self.context;scope=self.bound['selection']['scope']
            if (client.endpoint!=scope['board_endpoint'] or client.session_id!=context.session_id
                    or file_document(self.driver.selections.connection_file)[1]!=self.bound['connection']):return
            op=self.driver._operation(self.worker,context,self.bound)
            ticket=op['payload']['initial_selected_ticket']
            if (ticket['id']!=scope['ticket_id'] or ticket['project_id']!=scope['project_id']):return
            notice=failure_notice(code,cleaned)
            intent={'schema':1,'operation_id':context.operation_id,'session_id':context.session_id,
                'ticket_id':scope['ticket_id'],'project_id':scope['project_id'],'thread_id':ticket['thread_id'],
                'idempotency_key':'development-failure-notice-'+context.operation_id,'notice':notice}
            with PrivateDirectory(context.journals['queue']) as private:
                try:private.create('development-failure-notice-intent.json',canonical(intent))
                except DevelopmentError as error:
                    if error.code!='private_file_exists':raise
                    if private.read('development-failure-notice-intent.json',4096)!=canonical(intent):return
                phase='post_rpc'
                receipt=client.post_failure_notice(thread_id=intent['thread_id'],
                    idempotency_key=intent['idempotency_key'],notice=notice)
                phase='receipt_write'
                record={'schema':1,'intent_sha256':digest(canonical(intent)),**receipt}
                try:private.create('development-failure-notice-receipt.json',canonical(record))
                except DevelopmentError as error:
                    if error.code!='private_file_exists':raise
                    if private.read('development-failure-notice-receipt.json',4096)!=canonical(record):
                        raise DevelopmentError('development_notice_receipt_conflict')
        except BaseException as error:
            try:self._notice_diagnostic(phase,error,notice)
            except BaseException:pass
            # Notification is optional even during interrupted shutdown. Its
            # failure must not replace an existing run error or Pool outcome.
            return

    def _notice_diagnostic(self, phase, error, notice):
        """Retain the first closed failure and emit it for the service journal.

        No receipt, intent, retry key or existing diagnostic is replaced. Even
        storage/stdout failure is subordinate to the original worker outcome.
        """
        try:
            value=failure_notice_diagnostic(phase,error,notice)
            if value is None:return
            raw=canonical(value)
        except BaseException:return
        try:
            with PrivateDirectory(self.context.journals['queue']) as private:
                try:private.create('development-failure-notice-diagnostic.json',raw)
                except DevelopmentError as error:
                    if error.code!='private_file_exists':raise
        except BaseException:pass
        try:print(raw.decode('utf-8'),flush=True)
        except BaseException:pass

    def close(self):
        if self.closed: return
        try:
            if self.companion is not None: self.companion.close()
        finally:
            self.closed = True
            self.resources.close()
