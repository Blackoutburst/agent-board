"""Fixed-name private planner journal, preallocated steps and persistent budgets."""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import math
import os
import threading
import uuid

from .dev_journal import PrivateDirectory, decode, _linux_lock_supported, _linux_lock_observation
from .dev_manifest import DevelopmentError, canonical, digest, identity

MAX_PLANNER_STATE = 2 * 1024 * 1024
PHASES = frozenset({'ready', 'model_intent', 'action_ready', 'tool_intent',
                    'tool_receipt', 'review_ready', 'needs_review'})


@dataclass(frozen=True)
class PlannerBudgets:
    max_turns: int = 16
    max_actions: int = 16
    max_wall_seconds: int = 1800
    max_prompt_bytes: int = 65536
    max_response_bytes: int = 65536
    max_total_bytes: int = 1048576
    turn_timeout_seconds: int = 120

    def document(self):
        bounds = {'max_turns': (1, 64), 'max_actions': (1, 64),
            'max_wall_seconds': (1, 7200), 'max_prompt_bytes': (1, 65536),
            'max_response_bytes': (1, 65536), 'max_total_bytes': (1, 1048576),
            'turn_timeout_seconds': (1, 600)}
        for key, (low, high) in bounds.items():
            if type(getattr(self, key)) is not int or not low <= getattr(self, key) <= high:
                raise DevelopmentError('invalid_planner_budget')
        return vars(self).copy()


class PlannerJournal:
    def __init__(self, directory, configuration, operation_id):
        self.private = PrivateDirectory(directory)
        self.configuration = decode(canonical(configuration))
        self.operation_id = operation_id
        self.fingerprint = digest(canonical(configuration))
        self._mutex = threading.Lock()
        self._checkpoint_owner,self._checkpoint_token = None,None
        self.fd = None
        try:
            self.fd = os.open('planner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                              0o600, dir_fd=self.private.fd)
            self.private.validate_file(self.fd)
        except OSError:
            self.close()
            raise DevelopmentError('planner_lock_unavailable') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.private.close()

    @contextmanager
    def lock(self):
        if not self._mutex.acquire(blocking=False):
            raise DevelopmentError('planner_busy')
        locked = False
        try:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                self.private.verify()
                current = os.stat('planner.lock', dir_fd=self.private.fd, follow_symlinks=False)
                if identity(current) != identity(os.fstat(self.fd)):
                    raise DevelopmentError('planner_lock_changed')
                self.private.validate_file(self.fd)
                self._checkpoint_owner,self._checkpoint_token = (os.getpid(),threading.current_thread()),object()
            except (OSError, TypeError):
                raise DevelopmentError('planner_busy') from None
            yield
        finally:
            self._checkpoint_owner,self._checkpoint_token = None,None
            if locked:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            self._mutex.release()

    def checkpoint_lock(self, token=None):
        """Observe this acquisition; never reacquire a lost kernel lock."""
        if (self._checkpoint_owner!=(os.getpid(),threading.current_thread())
                or self._checkpoint_token is None or token is not None and token is not self._checkpoint_token):
            raise DevelopmentError('development_checkpoint_lock_required')
        self.private.verify();self.private.validate_file(self.fd)
        before=os.fstat(self.fd)
        if identity(os.stat('planner.lock',dir_fd=self.private.fd,follow_symlinks=False))!=identity(before):
            raise DevelopmentError('planner_lock_changed')
        if _linux_lock_supported():_linux_lock_observation(self.fd,pid=os.getpid(),expected=identity(before))
        return self._checkpoint_token

    def prepare(self, *, now):
        if type(now) not in (float, int) or not math.isfinite(now):
            raise DevelopmentError('invalid_planner_clock')
        with self.lock():
            self.private.create('planner.json', canonical({'schema': 1,
                'operation_id': self.operation_id, 'configuration': self.configuration,
                'configuration_hash': self.fingerprint, 'phase': 'ready', 'reason': '',
                'started_at': now, 'deadline': now + self.configuration['budgets']['max_wall_seconds'],
                'used': {'turns': 0, 'actions': 0, 'bytes': 0},
                'step': None, 'last_result': None, 'history': [], 'tests': {}, 'review': None}))

    def read(self):
        state = decode(self.private.read('planner.json', MAX_PLANNER_STATE))
        self.validate(state)
        return state

    def validate(self, state):
        try:
            if (type(state) is not dict or set(state) != {'schema', 'operation_id', 'configuration',
                    'configuration_hash', 'phase', 'reason', 'started_at', 'deadline',
                    'used', 'step', 'last_result', 'history', 'tests', 'review'}
                    or type(state['schema']) is not int or state['schema'] != 1
                    or state['operation_id'] != self.operation_id
                    or state['configuration'] != self.configuration
                    or state['configuration_hash'] != self.fingerprint or state['phase'] not in PHASES
                    or type(state['reason']) is not str or len(state['reason']) > 100
                    or any(type(state[key]) not in (float, int) or not math.isfinite(state[key])
                           for key in ('started_at', 'deadline'))
                    or state['deadline'] != state['started_at'] + self.configuration['budgets']['max_wall_seconds']
                    or type(state['used']) is not dict or set(state['used']) != {'turns', 'actions', 'bytes'}
                    or any(type(v) is not int or not 0 <= v <= self.configuration['budgets']['max_total_bytes' if k == 'bytes' else 'max_' + k]
                           for k, v in state['used'].items())
                    or type(state['history']) is not list or len(state['history']) > state['used']['turns']
                    or type(state['tests']) is not dict
                    or (state['last_result'] is not None and type(state['last_result']) is not dict)
                    or (state['review'] is not None and type(state['review']) is not dict)):
                raise ValueError
            step = state['step']
            if step is not None:
                if (type(step) is not dict or set(step) != {'id', 'request_id', 'action_key', 'prompt',
                        'action', 'model_turn_id', 'receipt', 'source_snapshot', 'tool_finished'}
                        or str(uuid.UUID(step['id'])) != step['id']
                        or step['request_id'] != 'dev-model-' + step['id']
                        or step['action_key'] != 'dev-action-' + step['id']
                        or type(step['prompt']) is not str
                        or len(step['prompt'].encode()) > self.configuration['budgets']['max_prompt_bytes']
                        or type(step['tool_finished']) is not bool
                        or any(step[key] is not None and type(step[key]) is not dict
                               for key in ('action', 'receipt', 'source_snapshot'))):
                    raise ValueError
                if step['model_turn_id'] is not None and str(uuid.UUID(step['model_turn_id'])) != step['model_turn_id']:
                    raise ValueError
            elif state['phase'] in ('model_intent', 'action_ready', 'tool_intent', 'tool_receipt'):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise DevelopmentError('planner_journal_corrupt') from None

    def save(self, state):
        self.validate(state)
        data = canonical(state)
        if len(data) > MAX_PLANNER_STATE:
            raise DevelopmentError('planner_journal_full')
        self.private.replace('planner.json', data)

    def stopped(self):
        try:
            raw = self.private.read('planner-stop.json', 512)
        except DevelopmentError as error:
            if error.code != 'private_read_failed':
                raise
            try:
                os.stat('planner-stop.json', dir_fd=self.private.fd, follow_symlinks=False)
            except FileNotFoundError:
                return False
            raise
        if decode(raw) != {'schema': 1, 'operation_id': self.operation_id, 'stop': True}:
            raise DevelopmentError('planner_stop_corrupt')
        return True

    def request_stop(self):
        if self.stopped():
            return
        try:
            self.private.create('planner-stop.json', canonical({'schema': 1,
                'operation_id': self.operation_id, 'stop': True}))
        except DevelopmentError as error:
            if error.code != 'private_file_exists' or not self.stopped():
                raise

    def charge(self, state, **amounts):
        for key, amount in amounts.items():
            if type(amount) is not int or amount < 0 or key not in state['used']:
                raise DevelopmentError('invalid_planner_budget_charge')
            if state['used'][key] + amount > self.configuration['budgets']['max_total_bytes' if key == 'bytes' else 'max_' + key]:
                raise DevelopmentError('planner_' + key + '_budget_exhausted')
        for key, amount in amounts.items():
            state['used'][key] += amount
