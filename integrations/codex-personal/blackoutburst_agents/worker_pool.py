"""Explicit local pool core, with no development driver or autonomous listener."""
from copy import deepcopy
from dataclasses import dataclass
import os
import threading
import time
from types import MappingProxyType
import uuid

from .dev_journal import PrivateDirectory
from .pool_journal import PoolJournal, PoolError, HELD, identifier, uuid_value, payload_value


@dataclass(frozen=True)
class PoolOutcome:
    operation_id: str
    session_id: str
    status: str
    cleanup_confirmed: bool


@dataclass(frozen=True)
class PoolDriverBinding:
    """Protected local code, not a document/hash/approval flag supplied by Board.

    factory(context) returns a fresh per-thread driver with run() and close().
    reconcile(context) may inspect/reconcile existing effects only, never run.
    verify_cleanup(context, outcome) must freshly verify real underlying receipts.
    There is intentionally no built-in driver, model, admission or network call.
    """
    revision: str
    factory: object
    reconcile: object
    verify_cleanup: object

    def __post_init__(self):
        identifier(self.revision)
        if not all(callable(value) for value in (self.factory, self.reconcile, self.verify_cleanup)):
            raise PoolError('invalid_pool_driver')


@dataclass(frozen=True)
class PoolContext:
    operation_id: str
    worker_id: str
    session_id: str
    payload: dict
    journals: object
    should_stop: object
    report_execution: object


class WorkerPool:
    def __init__(self, directory, *, concurrency=None, ceiling=None, drivers=None):
        self.drivers = MappingProxyType(dict(drivers or {}))
        for key, binding in self.drivers.items():
            identifier(key)
            if type(binding) is not PoolDriverBinding:
                raise PoolError('invalid_pool_driver')
        self.journal = PoolJournal(directory, concurrency=concurrency, ceiling=ceiling)
        try:
            self.mutex = threading.RLock()
            self.threads, self.stops, self.calling, self.reconciling, self.launching = {}, {}, set(), set(), set()
            self.closed = False
            self.admissions_blocked, self.stop_unconfirmed = False, False
            with PrivateDirectory(self.journal.private.path/'workers', create=True):
                pass
        except BaseException:
            try:self.journal.close()
            except BaseException:pass
            self.closed = True
            raise

    def _ready(self):
        if self.closed:
            raise PoolError('pool_closed')

    @staticmethod
    def _job(state, operation_id):
        for job in state['jobs']:
            if job['id'] == operation_id:
                return job
        raise PoolError('pool_operation_not_found')

    @staticmethod
    def _public(job):
        return {key: deepcopy(value) for key, value in job.items() if key != 'payload'}

    def _binding(self, job):
        binding = self.drivers.get(job['driver_id'])
        if binding is None:
            raise PoolError('driver_unavailable')
        if binding.revision != job['driver_revision']:
            raise PoolError('driver_changed')
        return binding

    def submit(self, *, request_id, driver_id, payload):
        identifier(request_id); identifier(driver_id); payload = payload_value(payload)
        binding = self.drivers.get(driver_id)
        if binding is None:
            raise PoolError('driver_unavailable')
        with self.mutex:
            self._ready()
            def insert(state):
                for job in state['jobs']:
                    if job['request_id'] == request_id:
                        if (job['driver_id'], job['driver_revision'], job['payload']) != (driver_id, binding.revision, payload):
                            raise PoolError('pool_request_conflict')
                        return self._public(job)
                if state['stopped']:
                    raise PoolError('pool_stopped')
                if self.admissions_blocked:
                    raise PoolError('pool_stopped')
                if len(state['jobs']) >= 256:
                    raise PoolError('pool_journal_full')
                job = {'id': str(uuid.uuid4()), 'worker_id': str(uuid.uuid4()), 'session_id': str(uuid.uuid4()),
                    'request_id': request_id, 'driver_id': driver_id, 'driver_revision': binding.revision,
                    'payload': payload, 'state': 'queued', 'stop_requested': False, 'execution_active': False,
                    'cleanup_confirmed': None, 'reason': '', 'revision': 1, 'outcome': None}
                state['jobs'].append(job)
                return self._public(job)
            return self.journal.change(insert)

    def _update(self, operation_id, *, expected_revision=None, **changes):
        def update(state):
            job = self._job(state, operation_id)
            if expected_revision is not None and job['revision'] != expected_revision:
                raise PoolError('pool_revision_changed')
            job.update(changes, revision=job['revision'] + 1)
            return deepcopy(job)
        return self.journal.change(update)

    def dispatch_pending(self):
        """One explicit scheduling pass; no worker/factory call under the mutex."""
        started = []
        while True:
            with self.mutex:
                self._ready()
                def reserve(state):
                    if (state['stopped'] or self.admissions_blocked
                            or sum(job['state'] in HELD for job in state['jobs']) >= state['concurrency']):
                        return None
                    for job in state['jobs']:
                        if job['state'] != 'queued':
                            continue
                        if self.stops.get(job['id']) is not None and self.stops[job['id']].is_set():
                            job.update(state='cancelled', stop_requested=True, reason='stop_before_dispatch',
                                       revision=job['revision'] + 1)
                            continue
                        try:
                            self._binding(job)
                        except PoolError as error:
                            job.update(reason=str(error), revision=job['revision'] + 1)
                            continue
                        job.update(state='dispatching', reason='', revision=job['revision'] + 1)
                        return deepcopy(job)
                job = self.journal.change(reserve)
                if job is None:
                    break
                self.stops.setdefault(job['id'], threading.Event())
                thread = threading.Thread(target=self._execute, args=(job['id'],), name='pool-' + job['worker_id'], daemon=False)
                self.threads[job['id']] = thread
                self.launching.add(job['id'])
            try:
                thread.start()  # Durable intent exists, and the mutex is free.
                started.append(job['id'])
            except Exception:
                with self.mutex:
                    self._update(job['id'], state='retained', reason='dispatch_unconfirmed', execution_active=None)
            finally:
                with self.mutex:
                    self.launching.discard(job['id'])
        return started

    def _context(self, job, *, create=False):
        root = self.journal.private.path/'workers'/job['worker_id']
        if create:
            root.mkdir(mode=0o700)  # Exclusive: an old directory is never adopted by a new launch.
        if create:
            with PrivateDirectory(root):
                pass
        else:
            try:
                os.stat(root, follow_symlinks=False)
            except FileNotFoundError:
                pass  # A read-only reconciliation may need to prove a launch never occurred.
            else:
                with PrivateDirectory(root):
                    pass
        paths = {name: root/name for name in ('queue', 'work', 'runs', 'runtime')}
        if create:
            for path in paths.values():
                with PrivateDirectory(path, create=True):
                    pass
            with PrivateDirectory(root) as owned:
                os.fsync(owned.fd)
            with PrivateDirectory(root.parent) as owned:
                os.fsync(owned.fd)
        with self.mutex:
            stop = self.stops.setdefault(job['id'], threading.Event())
            if job['stop_requested']:
                stop.set()
        def report(active):
            if type(active) is not bool:
                raise PoolError('invalid_execution_report')
            with self.mutex:
                current = self._job(self.journal.read(), job['id'])
                if current['state'] not in ('dispatching', 'running'):
                    raise PoolError('pool_execution_not_owned')
                self._update(job['id'], execution_active=active)
        return PoolContext(job['id'], job['worker_id'], job['session_id'], deepcopy(job['payload']),
                           MappingProxyType(paths), stop.is_set, report)

    @staticmethod
    def _outcome(job, outcome):
        if (type(outcome) is not PoolOutcome or outcome.operation_id != job['id']
                or outcome.session_id != job['session_id'] or type(outcome.cleanup_confirmed) is not bool
                or outcome.status not in ('complete', 'blocked', 'cancelled', 'needs_review')):
            raise PoolError('invalid_pool_outcome')
        return outcome

    def _finish(self, job, outcome, confirmed, *, reconciliation=False):
        if confirmed:
            return self._update(job['id'], expected_revision=job['revision'], state='settled',
                cleanup_confirmed=True, execution_active=False, outcome=outcome.status,
                reason='reconciled' if reconciliation else 'driver_finished')
        return self._update(job['id'], expected_revision=job['revision'], state='retained',
            cleanup_confirmed=False if outcome and not outcome.cleanup_confirmed else None,
            execution_active=None, outcome=outcome.status if outcome else None,
            reason='cleanup_unconfirmed' if outcome and not outcome.cleanup_confirmed else 'cleanup_verification_failed')

    def _execute(self, operation_id):
        driver = None
        try:
            with self.mutex:
                job = deepcopy(self._job(self.journal.read(), operation_id))
                if job['state'] != 'dispatching':
                    return
                if job['stop_requested'] or self.stops[operation_id].is_set():
                    self._update(operation_id, state='cancelled', execution_active=False, reason='stop_before_driver')
                    return
                binding = self._binding(job)
                self._update(operation_id, state='running')
            context = self._context(job, create=True)
            if context.should_stop():
                with self.mutex:
                    self._update(operation_id, state='cancelled', reason='stop_before_driver', execution_active=False)
                return  # No driver was called; do not invent a cleanup receipt.
            driver = binding.factory(context)
            if not callable(getattr(driver, 'run', None)) or not callable(getattr(driver, 'close', None)):
                raise PoolError('invalid_pool_driver')
            if context.should_stop():
                closing, driver = driver, None
                closing.close()
                with self.mutex:
                    self._update(operation_id, state='retained', reason='stop_before_driver', execution_active=None)
                return  # Factory effects require explicit verified reconciliation.
            with self.mutex:
                self.calling.add(operation_id)
            try:
                outcome = self._outcome(job, driver.run())
            finally:
                try:
                    driver.close()
                finally:
                    driver = None
                    with self.mutex:
                        self.calling.discard(operation_id)
            confirmed = outcome.cleanup_confirmed and binding.verify_cleanup(context, outcome) is True
            with self.mutex:
                current = deepcopy(self._job(self.journal.read(), operation_id))
                self._finish(current, outcome, confirmed)
        except BaseException:
            if driver is not None:
                try:driver.close()
                except BaseException:pass
            with self.mutex:
                self.calling.discard(operation_id)
                try:
                    self._update(operation_id, state='retained', cleanup_confirmed=None,
                                 execution_active=None, reason='driver_failed')
                except Exception:
                    pass  # Keep durable dispatch/running ambiguity and its capacity charge.

    def stop(self, operation_id=None):
        with self.mutex:
            self._ready()
            if operation_id is not None:
                uuid_value(operation_id)
            else:
                self.admissions_blocked = True
            def stop_jobs(state):
                if operation_id is None:
                    state['stopped'] = True
                else:
                    self._job(state, operation_id)
                selected = []
                for job in state['jobs']:
                    if (operation_id is None or job['id'] == operation_id) and job['state'] in HELD | {'queued'}:
                        job.update(stop_requested=True, revision=job['revision'] + 1)
                        if job['state'] == 'queued':
                            job.update(state='cancelled', reason='stop_before_dispatch')
                        selected.append(job['id'])
                return selected
            try:
                selected = self.journal.change(stop_jobs)
                self.stop_unconfirmed = False
            except Exception as error:
                if isinstance(error, PoolError) and str(error) == 'pool_operation_not_found':
                    raise
                self.stop_unconfirmed = True
                selected = list(self.stops) if operation_id is None else [operation_id]
                for key in selected:
                    self.stops.setdefault(key, threading.Event()).set()
                raise PoolError('pool_stop_unconfirmed') from None
            for key in selected:
                self.stops.setdefault(key, threading.Event()).set()
        return self.status()

    def set_concurrency(self, value):
        with self.mutex:
            self._ready()
            def change(state):
                self.journal._limits(value, state['ceiling']); state['concurrency'] = value
            self.journal.change(change)
        return self.status()

    def resume_admissions(self):
        with self.mutex:
            self._ready()
            def resume(state):
                if any(job['state'] in HELD for job in state['jobs']):
                    raise PoolError('pool_cleanup_pending')
                state['stopped'] = False
            self.journal.change(resume)
            self.admissions_blocked, self.stop_unconfirmed = False, False
        return self.status()

    def reconcile(self, operation_id):
        with self.mutex:
            self._ready(); uuid_value(operation_id)
            job = deepcopy(self._job(self.journal.read(), operation_id))
            if (self.threads.get(operation_id) is not None and self.threads[operation_id].is_alive()
                    or operation_id in self.reconciling):
                raise PoolError('pool_worker_still_alive')
            if job['state'] not in HELD:
                raise PoolError('pool_reconciliation_not_needed')
            binding = self._binding(job)
            self.reconciling.add(operation_id)
        try:
            context = self._context(job)
            outcome = self._outcome(job, binding.reconcile(context))
            confirmed = outcome.cleanup_confirmed and binding.verify_cleanup(context, outcome) is True
            with self.mutex:
                return self._public(self._finish(job, outcome, confirmed, reconciliation=True))
        except Exception:
            with self.mutex:
                self._update(job['id'], expected_revision=job['revision'], state='retained',
                             execution_active=None, reason='reconciliation_failed')
            raise PoolError('pool_reconciliation_unconfirmed') from None
        finally:
            with self.mutex:
                self.reconciling.discard(operation_id)

    def status(self):
        with self.mutex:
            self._ready(); state = self.journal.read()
            jobs = state['jobs']
            return {'pool_id': state['id'], 'configured': state['concurrency'], 'ceiling': state['ceiling'],
                'stopped': state['stopped'] or self.admissions_blocked,
                'stop_persisted': state['stopped'], 'stop_unconfirmed': self.stop_unconfirmed,
                'live_threads': sum(thread.is_alive() for thread in self.threads.values()),
                'driver_calls': len(self.calling), 'executing_reported': sum(job['execution_active'] is True for job in jobs),
                'execution_unknown': sum(job['state'] in HELD and job['execution_active'] is None for job in jobs),
                'capacity_held': sum(job['state'] in HELD for job in jobs),
                'retained': sum(job['state'] == 'retained' for job in jobs),
                'queued': sum(job['state'] == 'queued' for job in jobs),
                'jobs': [self._public(job) for job in jobs]}

    def shutdown(self, timeout=1):
        if type(timeout) not in (int, float) or not 0 <= timeout <= 30:
            raise PoolError('invalid_pool_timeout')
        self.stop(); deadline = time.monotonic() + timeout
        with self.mutex:
            threads = list(self.threads.values())
        for thread in threads:
            if thread.ident is not None:
                thread.join(max(0, deadline - time.monotonic()))
        return self.status()

    def close(self):
        with self.mutex:
            if self.closed:
                return
            if self.reconciling or self.launching or any(thread.is_alive() for thread in self.threads.values()):
                raise PoolError('pool_workers_alive')
            self.journal.close(); self.closed = True
