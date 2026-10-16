"""Opt-in foreground supervisor for fixed ticket subscriptions only.

A tick executes at most one recorded batch. There is no queue selection, inbox,
service installer or inference on an empty stream. All effects still belong to
EventJournal, WorkJournal, RunStore and RuntimeGuard.
"""
from contextlib import contextmanager, ExitStack
import time

from .board_reader import BoardReadError
from .board_worker import BoardWorker
from .context import ContextScope, _profile
from .event_client import EventBoardClient, HumanEventBoardClient
from .event_drain import EventDrainer
from .event_cli import _public_result
from .event_journal import EventJournal
from .host_config import HostError, connection_digest, digest, scope_for, worker_configuration
from .run_store import RunStore
from .work_journal import WorkJournal
from .runtime_client import RuntimeBoardClient
from .runtime_guard import RuntimeGuard
from .runtime_journal import RuntimeJournal


class PinnedClient:
    def __init__(self, client, host, target, scope, cancelled):
        self.client, self.host, self.target, self.scope = client, host, target, scope
        self.cancelled, self.failure = cancelled, None
        self.endpoint, self.session_id = client.endpoint, client.session_id

    def register(self):
        try:
            if connection_digest(self.target['connection_file']) != self.target['connection_sha256']:
                raise HostError('host_connection_changed')
            registration = self.client.register()
            profile = _profile(registration, ContextScope(**{key:self.scope[key] for key in
                ('owner_id','agent_id','project_id','ticket_id')}))
            if (self.endpoint != self.scope['board_endpoint'] or self.session_id != self.scope['board_session_id']
                    or profile.get('session_id') != self.scope['board_session_id']
                    or profile.get('preferences',{}).get('version') != self.target['profile_version']
                    or registration.get('policy',{}).get('allow_requests') is not True):
                raise HostError('host_profile_changed')
            # No timestamps/session liveness in this digest. Policy and device
            # are included so re-registering cannot silently broaden access.
            hashed = digest({'profile':profile, 'policy':registration['policy'],
                             'device_id':registration['identity']['device_id']})
            self.host.pin_profile(self.target['id'], hashed)
            return registration
        except BoardReadError as error:
            if error.code in ('timeout','connection_failed'):
                raise
            self.failure = 'authorization_changed'
            raise BoardReadError('host_authorization_changed') from None
        except Exception:
            self.failure = 'authorization_changed'
            raise BoardReadError('host_authorization_changed') from None

    def call(self, name, arguments):
        if name not in ('get_ticket','get_thread','list_activity','lookup_human_request') and self.cancelled():
            raise BoardReadError('host_stopped')
        return self.client.call(name, arguments)


class _ExactWorker:
    def __init__(self, worker, operation):
        self.worker, self.operation = worker, operation
        self.journal = worker.journal

    def run(self, **kwargs):
        if (kwargs.get('scope') != self.operation['scope']
                or kwargs.get('request_id') != self.operation['request_id']):
            raise HostError('host_reference_conflict')
        with self.journal.lock(self.operation['scope']):
            current = self.journal.find(self.operation['scope'], self.operation['request_id'])
            if current is None or current['id'] != self.operation['id']:
                raise HostError('host_reference_conflict')
        return self.worker.run(**kwargs)


@contextmanager
def default_resources(host, target, scope, cancelled):
    with ExitStack() as resources:
        kind = HumanEventBoardClient if target.get('human_owner_id') else EventBoardClient
        raw = kind.from_connection_file(target['connection_file'], session_id=scope['board_session_id'])
        client = PinnedClient(raw, host, target, scope, cancelled)
        events = resources.enter_context(EventJournal(host.directory / 'events'))
        work = resources.enter_context(WorkJournal(host.directory / 'work'))
        runs = RunStore(host.directory / 'runs'); resources.callback(runs.close)
        runtime = resources.enter_context(RuntimeJournal(host.directory / 'runtime'))
        runtime_client = RuntimeBoardClient.from_worker(raw)
        worker = BoardWorker(work, runs, client, runtime_guard_factory=lambda exact:
                             RuntimeGuard(runtime, runtime_client, exact))
        yield EventDrainer(events, worker, client)


class HostSupervisor:
    def __init__(self, journal, *, resource_factory=default_resources, clock=time.monotonic):
        self.journal, self.resource_factory, self.clock = journal, resource_factory, clock
        self.message_resource_factory = None

    def _receipt(self, status, reason, **values):
        return {'status':status, 'reason':reason, **values,
                'remaining_dispatches':self.journal.control()['remaining']}

    def _proof(self, drainer, target, scope, batch, dispatch):
        subscription = drainer.journal.get_subscription(batch['subscription_id'])
        configuration = worker_configuration(target)
        if (subscription['scope'] != scope or subscription['configuration'] != configuration
                or batch['request_id'] != dispatch['request_id'] or batch['id'] != dispatch['batch_id']):
            raise HostError('host_reference_conflict')
        with drainer.worker.journal.lock(scope):
            if not dispatch['worker_id']:
                raise HostError('host_reference_conflict')
            operation = drainer.worker.journal.begin(scope, batch['request_id'], configuration,
                                                      operation_id=dispatch['worker_id'])
            if operation['scope'] != scope or operation['configuration'] != configuration:
                raise HostError('host_reference_conflict')
            self.journal.link_worker(batch['id'], operation['id'])
        return operation

    def tick(self, *, should_cancel=None):
        self.journal._require_lock()
        value = self.journal.control()
        if not value['enabled']:
            return self._receipt('disabled', value['reason'])
        if value['configuration']['schema_version']==2:
            from .message_host import MessageHostSupervisor, default_message_resources
            return MessageHostSupervisor(self.journal,resource_factory=self.message_resource_factory or default_message_resources).tick(should_cancel=should_cancel)
        activation, config = value['activation'], value['configuration']

        def cancelled():
            if should_cancel is not None and should_cancel():
                self.journal.stop()
                return True
            return self.journal.cancelled(activation)

        if cancelled(): return self._receipt('disabled','stopped')
        unfinished = [row for row in self.journal.dispatches() if row['state'] != 'complete']
        if any(row['state'] == 'needs_review' for row in unfinished):
            self.journal.stop('needs_review')
            return self._receipt('needs_review','child_needs_review')
        targets = config['targets']
        targets = targets[value['next_target']:] + targets[:value['next_target']]
        if unfinished:
            # Reconcile the exact prior effect before admitting any other work.
            targets = [target for target in targets if target['id'] == unfinished[0]['target_id']]
        last = self._receipt('idle','no_pending_events')
        try:
            for target in targets:
                if cancelled(): return self._receipt('disabled','stopped')
                scope = scope_for(config,target)
                with self.resource_factory(self.journal,target,scope,cancelled) as drainer:
                    options = {'scope':scope, **{key:target[key] for key in
                        ('instruction','workspace','executable','model','timeout','after')},
                        'max_pages':config['max_pages'], 'limit':config['page_limit'],
                        'human_owner_id':target.get('human_owner_id'), 'runtime_managed':True,
                        'should_cancel':cancelled}
                    if 'routing' in target:
                        options.update(model_policy=target['routing']['policy'], task_class=target['routing']['task_class'])
                    old = unfinished[0] if unfinished else None
                    if old:
                        batch = drainer.journal.get_batch(old['batch_id'])
                        operation = self._proof(drainer,target,scope,batch,old)
                        if batch['status'] == 'complete':
                            if operation['stage'] != 'complete': raise HostError('host_reference_conflict')
                            if (batch.get('receipt') or {}).get('published_status') == 'blocked':
                                self.journal.finish_dispatch(batch['id'],'needs_review',{'status':'needs_review','reason':'child_blocked'})
                                self.journal.stop('needs_review')
                                return self._receipt('needs_review','child_blocked')
                            result = {'status':'complete','reason':'child_reconciled','worker_operation_id':operation['id']}
                            self.journal.finish_dispatch(batch['id'],'complete',result)
                            self.journal.rotate()
                            return self._receipt('complete','child_reconciled',batch_id=batch['id'])
                    preview = _public_result(drainer.drain(**options,execute=False))
                    self.journal.save_target(target['id'],preview)
                    if getattr(drainer.client,'failure',None):
                        self.journal.stop('authorization_changed')
                        return self._receipt('blocked','authorization_changed')
                    if preview['status'] in ('blocked','needs_review'):
                        self.journal.stop('needs_review')
                        return self._receipt('needs_review',preview['reason'])
                    if cancelled(): return self._receipt('disabled','stopped')
                    if preview['status'] != 'pending' or not preview.get('batch_id'):
                        last = self._receipt(preview['status'],preview['reason'])
                        continue
                    batch = drainer.journal.get_batch(preview['batch_id'])
                    if old and batch['id'] != old['batch_id']: raise HostError('host_reference_conflict')
                    if not old:
                        if self.journal.control()['remaining'] == 0:
                            self.journal.stop('budget_exhausted')
                            return self._receipt('disabled','budget_exhausted')
                        registration = drainer._register(scope,execute=True)
                        project,ticket = drainer._ticket(scope)
                        waiting = drainer._ready(project,ticket,scope,registration)
                        if waiting:
                            last = self._receipt('waiting',waiting); continue
                    dispatch = self.journal.reserve_dispatch(target['id'],batch,activation)
                    operation = self._proof(drainer,target,scope,batch,dispatch)
                    original = drainer.worker
                    drainer.worker = _ExactWorker(original,operation)
                    try: result = _public_result(drainer.drain(**options,execute=True))
                    finally: drainer.worker = original
                    if result.get('batch_id',batch['id']) != batch['id']: raise HostError('host_reference_conflict')
                    state = 'complete' if result['status'] == 'complete' else (
                        'needs_review' if result['status'] in ('blocked','needs_review','cancelled') else 'pending')
                    final_batch = drainer.journal.get_batch(batch['id'])
                    child_blocked = (final_batch.get('receipt') or {}).get('published_status') == 'blocked'
                    if child_blocked or getattr(drainer.client,'failure',None):
                        state = 'needs_review'
                    self.journal.finish_dispatch(batch['id'],state,result)
                    self.journal.save_target(target['id'],result)
                    if state == 'needs_review':
                        self.journal.stop('needs_review')
                        return self._receipt('needs_review','child_blocked' if child_blocked else result['reason'],
                            batch_id=batch['id'],worker_operation_id=operation['id'])
                    if state == 'complete': self.journal.rotate()
                    if cancelled(): return self._receipt('disabled','stopped',batch_id=batch['id'])
                    return self._receipt(result['status'],result['reason'],batch_id=batch['id'],
                                         worker_operation_id=operation['id'])
            self.journal.rotate()
            if self.journal.control()['remaining'] == 0 and not unfinished:
                self.journal.stop('budget_exhausted')
                return self._receipt('disabled','budget_exhausted')
            return last
        except Exception:
            # A handled refusal is not a simulated process crash. Record it so
            # a later enable cannot reinterpret an explicitly rejected child
            # as an interrupted preallocation. The durable child ID independently
            # enforces ownership even if these diagnostic writes fail.
            try:
                for dispatch in self.journal.dispatches():
                    if dispatch['state'] == 'pending':
                        self.journal.finish_dispatch(dispatch['batch_id'],'needs_review',
                            {'status':'needs_review','reason':'host_reconciliation_required'})
            except Exception:
                pass  # Control reason also fences enable until reconciliation.
            self.journal.stop('needs_review')
            return self._receipt('needs_review','host_reconciliation_required')

    def reconcile(self, batch_id):
        """Offline acknowledgement of an already resolved, exactly linked child.

        Use worker/events/runtime recovery first. This never abandons a worker,
        clears a remote reservation or claims that cleanup happened.
        """
        self.journal._require_lock()
        control = self.journal.control()
        if control['enabled']:
            raise HostError('host_must_be_stopped')
        if control['configuration']['schema_version']==2:
            from .message_host import MessageHostSupervisor
            return MessageHostSupervisor(self.journal).reconcile(batch_id)
        dispatch = next((row for row in self.journal.dispatches() if row['batch_id'] == batch_id),None)
        if dispatch is None or not dispatch['worker_id']:
            raise HostError('host_reference_conflict')
        target = next(target for target in control['configuration']['targets'] if target['id'] == dispatch['target_id'])
        scope = scope_for(control['configuration'],target)
        with EventJournal(self.journal.directory/'events') as events, WorkJournal(self.journal.directory/'work') as work:
            with events.lock(scope), work.lock(scope):
                batch = events.get_batch(batch_id)
                subscription = events.get_subscription(batch['subscription_id'])
                operation = work.get(dispatch['worker_id'])
                if (batch['status'] != 'complete' or operation['stage'] not in ('complete','abandoned')
                        or operation['scope'] != scope or operation['request_id'] != dispatch['request_id']
                        or batch['request_id'] != dispatch['request_id']
                        or batch['subscription_id'] != dispatch['subscription_id']
                        or subscription['scope'] != scope
                        or operation['configuration'] != worker_configuration(target)
                        or subscription['configuration'] != operation['configuration']
                        or (batch.get('receipt') or {}).get('operation_id',operation['id']) != operation['id']
                        or (batch.get('resolution') or {}).get('operation_id',operation['id']) != operation['id']):
                    raise HostError('host_child_not_resolved')
                self.journal.finish_dispatch(batch_id,'complete',{'status':'complete','reason':'human_reconciled',
                    'worker_operation_id':operation['id']})
        return self._receipt('complete','human_reconciled',batch_id=batch_id)

    def run(self, *, should_cancel=None, wait=None, max_ticks=None):
        """Foreground, bounded on useful work by the durable activation budget.

        max_ticks/wait are injection points for deterministic tests, not hidden
        CLI or environment bypasses. Runtime/worker bound each model turn.
        """
        wait = wait or time.sleep
        ticks, failures = 0, 0
        while True:
            result = self.tick(should_cancel=should_cancel)
            ticks += 1
            if result['status'] in ('disabled','needs_review','blocked') or (max_ticks is not None and ticks >= max_ticks):
                return result
            failures = min(failures+1,4) if result['reason'] in ('board_unavailable','drain_state_unavailable') else 0
            interval = self.journal.control()['configuration']['interval_seconds']
            delay = min(300,interval * (2 ** failures))
            if result['status'] == 'more_available': delay = 1
            deadline = self.clock()+delay
            while self.clock() < deadline:
                if should_cancel is not None and should_cancel():
                    self.journal.stop(); return self._receipt('disabled','stopped')
                if not self.journal.control()['enabled']: return self._receipt('disabled','stopped')
                wait(min(1,max(0,deadline-self.clock())))
