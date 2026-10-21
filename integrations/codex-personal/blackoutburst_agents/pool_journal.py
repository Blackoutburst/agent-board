"""Private atomic metadata for one local pool owner; never execution authority."""
from copy import deepcopy
import fcntl
import os
import re
import threading
import uuid

from .dev_journal import PrivateDirectory, decode, MAX_STATE_BYTES
from .dev_manifest import canonical
from .work_journal import _encode


class PoolError(Exception):
    pass


HELD = frozenset({'dispatching', 'running', 'retained'})
STATES = HELD | {'queued', 'settled', 'cancelled'}
REASONS = frozenset({'', 'owner_restarted', 'driver_unavailable', 'driver_changed', 'dispatch_unconfirmed',
    'driver_failed', 'cleanup_unconfirmed', 'cleanup_verification_failed', 'stop_before_driver', 'stop_before_dispatch',
    'driver_finished', 'reconciled', 'reconciliation_failed'})


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise PoolError('invalid_pool_identifier')
    return value


def uuid_value(value):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
        return value
    except (ValueError, TypeError, AttributeError):
        raise PoolError('invalid_pool_identity') from None


def payload_value(value):
    if type(value) is not dict:
        raise PoolError('invalid_pool_payload')
    try:
        return decode(_encode(value, 4096, 'invalid_pool_payload').encode())
    except Exception:
        raise PoolError('invalid_pool_payload') from None


class PoolJournal:
    """A process ownership lease plus short atomic metadata updates.

    The lease prevents two controllers from dispatching the same durable intent.
    Its mutex only covers bounded local metadata I/O, never driver/worker calls.
    There is no automatic dispatch, cleanup inference or recovery execution.
    """
    def __init__(self, directory, *, concurrency=None, ceiling=None):
        self.private = PrivateDirectory(directory, create=True)
        self.mutex, self.pid, self.fd = threading.RLock(), os.getpid(), None
        try:
            self.fd = os.open('pool-owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=self.private.fd)
            self.private.validate_file(self.fd)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            info = os.fstat(self.fd)
            self.owner_identity = (info.st_dev, info.st_ino)
            try:
                descriptor = os.open('pool-state.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.private.fd)
            except FileNotFoundError:
                limit = 8 if ceiling is None else ceiling
                count = 1 if concurrency is None else concurrency
                self._limits(count, limit)
                self.private.create('pool-state.json', canonical({'schema': 1, 'id': str(uuid.uuid4()),
                    'concurrency': count, 'ceiling': limit, 'stopped': False, 'revision': 1, 'jobs': []}))
            else:
                os.close(descriptor)
            current = self.read()
            self._limits(current['concurrency'] if concurrency is None else concurrency,
                         current['ceiling'] if ceiling is None else ceiling)
            if ((ceiling is not None and ceiling != current['ceiling'])
                    or (concurrency is not None and concurrency != current['concurrency'])):
                raise PoolError('pool_configuration_conflict')
            def recover(state):
                for job in state['jobs']:
                    if job['state'] in ('dispatching', 'running'):
                        job.update(state='retained', reason='owner_restarted', execution_active=None,
                                   cleanup_confirmed=None, revision=job['revision'] + 1)
            if any(job['state'] in ('dispatching', 'running') for job in current['jobs']):
                self.change(recover)
        except BaseException as error:
            if self.fd is not None:
                os.close(self.fd); self.fd = None
            self.private.close()
            if isinstance(error, PoolError):
                raise
            raise PoolError('pool_owner_unavailable') from None

    @staticmethod
    def _limits(count, limit):
        if type(count) is not int or type(limit) is not int or not 1 <= count <= limit <= 8:
            raise PoolError('invalid_pool_concurrency')

    def _owner(self):
        if self.fd is None or self.pid != os.getpid():
            raise PoolError('pool_owner_unavailable')
        self.private.verify(); self.private.validate_file(self.fd)
        info = os.stat('pool-owner.lock', dir_fd=self.private.fd, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != self.owner_identity:
            raise PoolError('pool_owner_changed')

    @staticmethod
    def validate(state):
        try:
            if (type(state) is not dict or set(state) !=
                    {'schema', 'id', 'concurrency', 'ceiling', 'stopped', 'revision', 'jobs'}
                    or type(state['schema']) is not int or state['schema'] != 1 or type(state['stopped']) is not bool
                    or type(state['revision']) is not int or state['revision'] < 1
                    or type(state['jobs']) is not list or len(state['jobs']) > 256):
                raise ValueError
            uuid_value(state['id']); PoolJournal._limits(state['concurrency'], state['ceiling'])
            seen = {key: set() for key in ('id', 'worker_id', 'session_id', 'request_id')}
            identities = {state['id']}
            fields = {'id', 'worker_id', 'session_id', 'request_id', 'driver_id', 'driver_revision', 'payload',
                      'state', 'stop_requested', 'execution_active', 'cleanup_confirmed', 'reason', 'revision', 'outcome'}
            for job in state['jobs']:
                if (type(job) is not dict or set(job) != fields or job['state'] not in STATES
                        or type(job['stop_requested']) is not bool
                        or job['execution_active'] is not None and type(job['execution_active']) is not bool
                        or job['cleanup_confirmed'] is not None and type(job['cleanup_confirmed']) is not bool
                        or job['reason'] not in REASONS or type(job['revision']) is not int or job['revision'] < 1
                        or job['outcome'] not in (None, 'complete', 'blocked', 'cancelled', 'needs_review')):
                    raise ValueError
                for key in ('id', 'worker_id', 'session_id'):
                    uuid_value(job[key])
                if len({job[key] for key in ('id', 'worker_id', 'session_id')}) != 3:
                    raise ValueError
                for key in ('id', 'worker_id', 'session_id'):
                    if job[key] in identities:
                        raise ValueError
                    identities.add(job[key])
                for key in ('request_id', 'driver_id', 'driver_revision'):
                    identifier(job[key])
                if payload_value(job['payload']) != job['payload']:
                    raise ValueError
                for key, values in seen.items():
                    if job[key] in values:
                        raise ValueError
                    values.add(job[key])
                if job['state'] == 'settled' and job['cleanup_confirmed'] is not True:
                    raise ValueError
                if job['state'] in ('queued', 'cancelled') and (job['cleanup_confirmed'] is not None
                        or job['execution_active'] is not False):
                    raise ValueError
            if sum(job['state'] in HELD for job in state['jobs']) > state['ceiling']:
                raise ValueError
            return state
        except Exception:
            raise PoolError('pool_journal_corrupt') from None

    def read(self):
        with self.mutex:
            self._owner()
            try:
                return self.validate(decode(self.private.read('pool-state.json', MAX_STATE_BYTES)))
            except PoolError:
                raise
            except Exception:
                raise PoolError('pool_journal_unavailable') from None

    def change(self, mutate):
        with self.mutex:
            state = self.read()
            result = mutate(state)  # Internal metadata mutation only, never a driver.
            state['revision'] += 1
            self.validate(state)
            raw = canonical(state)
            if len(raw) > MAX_STATE_BYTES:
                raise PoolError('pool_journal_full')
            try:
                self.private.replace('pool-state.json', raw)
            except Exception:
                raise PoolError('pool_write_unconfirmed') from None
            return deepcopy(result)

    def close(self):
        with self.mutex:
            if self.fd is None:
                return
            self._owner()
            os.close(self.fd); self.fd = None
            self.private.close()
