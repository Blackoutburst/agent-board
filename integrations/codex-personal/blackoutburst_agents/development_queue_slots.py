"""Bounded N-slot passes composed from existing standing queue admissions.

Every slot is its own private owner root with one coder profile, discovery
session, durable tick pointer, admissions, generated Core, checkouts, logs and
lease. Each slot still admits only through `DevelopmentQueueAdmission.tick`,
whose selected ticket is claimed later by `claim_selected_queue_ticket`. This
module adds no claim, Runtime, model or grant path. It only narrows what an
idle slot may discover while another slot holds the same resource, and bounds
how many slots run at once. The cap is the owner's local control (1-8); it
never proves provider or subscription capacity.

This module is deliberately outside the native Linux source roster; it does
not run inside a qualified generation and only calls existing public APIs.
"""
from copy import deepcopy
import os
from pathlib import Path
import threading
import time

from .board_reader import BoardReadError
from .continuation_pool_configuration import load_queue_continuation, MAX_CONFIGURATION
from .continuation_pool_service import (ContinuationPoolService, JOIN_SECONDS, MAX_RECORD, STATES,
    _TARGET_OBSERVATION, _TargetObservation, _target_failure)
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, opaque, signature
from .development_pool_selection import file_document
from .finite_pool_batch import child
from .linux_input_snapshot import _path
from .linux_planner_policy import _executable_pin, linux_source_pins
from .pool_journal import PoolError, identifier, uuid_value

SCHEMA = 'linux-cloud-queue-slots-v1'
MAX_SLOTS = 16
SLOT_CEILING = 8  # Same local ceiling as WorkerPool; never provider capacity.
# Closed transport outcomes of a slot's own discovery connection. They leave
# that slot's durable pointer untouched; a later pass retries the same boundary.
CONNECTION_EXPIRED = frozenset({'authentication_failed', 'permission_denied'})
CONNECTION_UNAVAILABLE = frozenset({'connection_failed', 'connection_closed', 'timeout', 'http_error'})
SLOT_PHASES = frozenset({'not_started', 'waiting_capacity', 'resource_busy', 'empty', 'stopped', 'needs_you',
                         'connection_expired', 'connection_unavailable'})
ATTENTION = frozenset({'needs_you', 'connection_expired'})


def fail(code='continuation_pool_target_failed'):
    raise DevelopmentError(code)


def require(value, code='continuation_pool_target_failed'):
    if not value:
        fail(code)


def slot_root(root, name):
    """Derived from the owner root and a closed identifier, never configured."""
    return Path(root) / 'slots' / identifier(name)


def load_queue_slots(path, *, root, owner_id, board_endpoint, max_seconds):
    """Local operator documents only: no client, credentials, RPC or grant."""
    try:
        root = Path(_path(os.fspath(root))); path = child(path, root / 'config')
        raw, pin = file_document(path, MAX_CONFIGURATION); value = decode(raw)
        require(type(value) is dict and value.get('schema') == SCHEMA
                and set(value) == {'schema', 'concurrency', 'slots'}
                and type(value['slots']) is list and 1 <= len(value['slots']) <= MAX_SLOTS,
                'continuation_pool_configuration_invalid')
        require(type(value['concurrency']) is int
                and 1 <= value['concurrency'] <= min(SLOT_CEILING, len(value['slots'])),
                'continuation_pool_configuration_invalid')
        slots = []; names = set(); agents = set(); pins = {'configuration': pin}
        for slot in value['slots']:
            require(type(slot) is dict and set(slot) == {'name', 'continuation_file'}
                    and type(slot['continuation_file']) is str and slot['name'] not in names,
                    'continuation_pool_configuration_invalid')
            directory = slot_root(root, slot['name']); names.add(slot['name'])
            standing = load_queue_continuation(slot['continuation_file'], root=directory, owner_id=owner_id,
                board_endpoint=board_endpoint, max_seconds=max_seconds)
            require(standing is not None, 'continuation_pool_configuration_invalid')
            loaded, slot_pins = standing; catalog = loaded['plan']['catalog']
            # The Board admits one unfinished queue ticket per profile; a shared
            # profile could only make a second slot wait or be refused.
            require(catalog['agent_id'] not in agents, 'continuation_pool_configuration_invalid')
            agents.add(catalog['agent_id'])
            pins.update({slot['name'] + ':' + key: item for key, item in slot_pins.items()})
            slots.append({'name': slot['name'], 'root': str(directory), 'path': str(Path(slot['continuation_file'])),
                          'loaded': loaded, 'pins': slot_pins})
        require(len({item['path'] for item in pins.values()}) == len(pins), 'continuation_pool_configuration_invalid')
        for observed in pins.values():
            require(file_document(observed['path'], MAX_CONFIGURATION)[1] == observed,
                    'continuation_pool_configuration_changed')
        return deepcopy({'configuration': value, 'slots': slots}), deepcopy(pins)
    except (TypeError, ValueError, KeyError, OverflowError, PoolError):
        raise DevelopmentError('continuation_pool_configuration_invalid') from None


def queue_slots_plan(path, *, root, owner_id, board_endpoint, max_seconds):
    """Closed, non-activating projection; never instructions or secrets."""
    loaded, pins = load_queue_slots(path, root=root, owner_id=owner_id, board_endpoint=board_endpoint,
                                    max_seconds=max_seconds)
    rows = []
    for slot in loaded['slots']:
        plan = slot['loaded']['plan']; catalog = plan['catalog']
        rows.append({'name': slot['name'], 'configuration_sha256': slot['pins']['configuration']['sha256'],
            'approval_sha256': plan['approval_sha256'], 'catalog_sha256': slot['pins']['catalog']['sha256'],
            'workflow_sha256': slot['pins']['workflow']['sha256'], 'agent_id': catalog['agent_id'],
            'project_id': catalog['project_id'], 'entries': [e['entry_id'] for e in catalog['entries']],
            'resources': [e['resource'] for e in catalog['entries']]})
    return {'schema': SCHEMA, 'status': 'plan', 'configuration_sha256': pins['configuration']['sha256'],
            'concurrency': loaded['configuration']['concurrency'], 'slots': rows,
            'activated': False, 'execution_qualified': False, 'launchable': False}


def _catalog_resources(slot):
    return frozenset(e['resource'] for e in slot['loaded']['plan']['catalog']['entries'])


def slot_holding(slot):
    """Durable (holds_work, resources) from the slot's own tick pointer.

    An unreadable pointer, or an admission whose intent is not yet written,
    conservatively holds every catalog resource. Nothing is repaired here.
    """
    catalog = slot['loaded']['plan']['catalog']; everything = _catalog_resources(slot)
    key = digest(canonical({k: catalog[k] for k in ('owner_id', 'agent_id')}))
    admissions = Path(slot['root']) / 'admissions'; pointer = admissions / ('queue-' + key) / 'tick.json'
    try: os.lstat(pointer)
    except FileNotFoundError: return False, frozenset()  # Never ticked: no admission exists.
    except OSError: return True, everything
    try:
        state = decode(file_document(pointer, MAX_RECORD)[0])
        if type(state) is not dict or state.get('phase') not in ('idle', 'admitting', 'active'):
            return True, everything
        if state['phase'] == 'idle' and state.get('active') is None: return False, frozenset()
        intent = admissions / uuid_value(state['active']['request_id']) / 'intent.json'
        try: os.lstat(intent)
        except FileNotFoundError: return True, everything
        selected = decode(file_document(intent, 3 * MAX_RECORD)[0])['preview']['selected']
        if selected is None: return True, frozenset()
        return True, (frozenset({selected['resource']}) & everything) or everything
    except Exception:
        return True, everything


INVENTORY = 'inventory.json'
INVENTORY_SCHEMA = 'linux-cloud-queue-slots-inventory-v1'


def _binding(slot):
    catalog = slot['loaded']['plan']['catalog']
    return {'agent_id': catalog['agent_id'], 'catalog_sha256': slot['pins']['catalog']['sha256'],
            'approval_sha256': slot['loaded']['plan']['approval_sha256'],
            'resources': sorted(_catalog_resources(slot))}


def _pointer_holds(pointer):
    """Whether one durable tick pointer still holds work. Unreadable = holds."""
    try: os.lstat(pointer)
    except FileNotFoundError: return False
    except OSError: return True
    try:
        state = decode(file_document(pointer, MAX_RECORD)[0])
        return not (type(state) is dict and state.get('phase') == 'idle' and state.get('active') is None)
    except Exception:
        return True


def _queue_pointers(admissions):
    """Every `queue-*/tick.json` under one admissions directory; odd entries hold."""
    try: entries = sorted(os.scandir(admissions), key=lambda entry: entry.name)
    except FileNotFoundError: return []
    except OSError: fail('continuation_pool_configuration_invalid')
    found = []
    for entry in entries:
        if not entry.name.startswith('queue-'): continue
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False): fail('continuation_pool_configuration_invalid')
        found.append(Path(admissions) / entry.name / 'tick.json')
    return found


def _current_pointer(slot):
    catalog = slot['loaded']['plan']['catalog']
    key = digest(canonical({k: catalog[k] for k in ('owner_id', 'agent_id')}))
    return Path(slot['root']) / 'admissions' / ('queue-' + key) / 'tick.json'


def reconcile_inventory(root, slots):
    """Refuse a pass whose configuration would forget durable held work.

    Called under the owner-root lease, before any slot discovers. Every queue
    pointer under the owner root is inspected, not only those of configured
    slots: a held pointer that no configured slot will resume (a removed slot,
    a slot whose profile changed, or a single standing continuation on the
    owner root itself) refuses the pass, as does a configured slot whose
    recorded binding (profile, catalog, approval) changed while it still holds
    work. Nothing is repaired or deleted; the operator restores the previous
    configuration or finishes the work. Only then is the inventory rewritten.
    """
    root = Path(root); base = root / 'slots'
    current = {str(_current_pointer(slot)) for slot in slots}
    pointers = list(_queue_pointers(root / 'admissions'))
    try: entries = sorted(os.scandir(base), key=lambda entry: entry.name)
    except FileNotFoundError: entries = []
    for entry in entries:
        if entry.name == INVENTORY and entry.is_file(follow_symlinks=False): continue
        if entry.is_symlink() or not entry.is_dir(follow_symlinks=False): fail('continuation_pool_configuration_invalid')
        pointers.extend(_queue_pointers(base / entry.name / 'admissions'))
    for pointer in pointers:
        if str(pointer) not in current and _pointer_holds(pointer): fail('continuation_pool_configuration_invalid')
    recorded = {}
    try:
        os.lstat(base / INVENTORY)
        value = decode(file_document(base / INVENTORY, MAX_RECORD)[0])
        require(type(value) is dict and value.get('schema') == INVENTORY_SCHEMA and type(value.get('slots')) is dict,
                'continuation_pool_configuration_invalid')
        recorded = value['slots']
    except FileNotFoundError: pass
    for slot in slots:
        previous = recorded.get(slot['name'])
        if previous is not None and previous != _binding(slot) and _pointer_holds(_current_pointer(slot)):
            fail('continuation_pool_configuration_invalid')
    inventory = {'schema': INVENTORY_SCHEMA, 'slots': {slot['name']: _binding(slot) for slot in slots}}
    if recorded != inventory['slots']:
        with PrivateDirectory(base, create=True) as private:
            exists = INVENTORY in os.listdir(base)
            (private.replace if exists else private.create)(INVENTORY, canonical(inventory))


class _Scheduler:
    """In-pass reservations; durable pointers supersede them once written."""
    def __init__(self, slots):
        self.slots = slots; self.mutex = threading.Lock(); self.reserved = {}

    def held(self, slot):
        holding, resources = slot_holding(slot)
        return resources if holding else self.reserved.get(slot['name'], frozenset())

    def reserve(self, slot, ready):
        """Return resources excluded for this slot and reserve what it may select."""
        with self.mutex:
            excluded = frozenset().union(*(self.held(other) for other in self.slots if other['name'] != slot['name']))
            visible = {row.get('resource') for row in ready if type(row) is dict}
            self.reserved[slot['name']] = frozenset(visible & _catalog_resources(slot)) - excluded
            return excluded

    def release(self, slot):
        with self.mutex: self.reserved.pop(slot['name'], None)

    def holders(self, running):
        with self.mutex:
            return {slot['name'] for slot in self.slots if slot['name'] in running or slot_holding(slot)[0]}


class _GuardedDiscovery:
    """The slot's own read-only discovery client, minus resources held elsewhere.

    The exclusion is frozen at the first ready list of this tick, so the
    admission's repeated previews see the same candidates. Narrowing can only
    remove an admission; it never adds one or touches claim tools.
    """
    def __init__(self, client, slot, scheduler):
        self._client, self._slot, self._scheduler = client, slot, scheduler
        self.endpoint, self.session_id = client.endpoint, client.session_id
        self.connection_path, self.connection_pin = client.connection_path, client.connection_pin
        self._excluded = None; self.filtered = False

    def registration(self, *, autonomous=False):
        return self._client.registration(autonomous=autonomous)

    def call(self, name, args):
        result = self._client.call(name, args)
        if name != 'get_work' or type(result) is not dict or type(result.get('ready')) is not list:
            return result  # The admission validates every shape itself.
        if self._excluded is None: self._excluded = self._scheduler.reserve(self._slot, result['ready'])
        kept = [row for row in result['ready'] if not (type(row) is dict and row.get('resource') in self._excluded)]
        self.filtered = self.filtered or len(kept) != len(result['ready'])
        return {**result, 'ready': kept}


def _row(slot, phase, reason='', *, request_id=None, logical_task_id=None, task=None):
    return {'name': slot['name'], 'phase': phase, 'reason': reason, 'request_id': request_id,
            'logical_task_id': logical_task_id,
            'next_observation_at': None if task is None else task.get('next_observation_at'),
            'task': task, 'launchable': False}


def _result(slot, result, filtered):
    require(type(result) is dict and result.get('schema') == 'development-queue-tick-v1')
    service = result['service']; task = None
    if service is not None:
        tasks = service.get('tasks') if type(service) is dict else None
        require(type(tasks) is list and len(tasks) == 1 and tasks[0].get('logical_task_id') == result['logical_task_id'])
        task = deepcopy(tasks[0])
    phase = result['phase']
    if phase == 'empty' and filtered: phase = 'resource_busy'
    require(type(phase) is str and (phase in SLOT_PHASES or phase in STATES))
    return _row(slot, phase, '' if task is None else task.get('reason', ''),
                request_id=result['request_id'], logical_task_id=result['logical_task_id'], task=task)


class QueueSlotsService:
    """Inert constructor and one explicitly invoked, bounded multi-slot pass."""
    def __init__(self, root, *, configuration_path, owner_id, board_endpoint, executable, cli_version,
                 interpreter, unshare, gh_executable, max_seconds):
        self.root = Path(_path(os.fspath(root))); self.path = Path(configuration_path)
        self.owner_id, self.endpoint = opaque(owner_id), board_endpoint
        require(type(max_seconds) is int and 1 <= max_seconds <= 7200, 'continuation_pool_configuration_invalid')
        require(type(cli_version) is str and cli_version.startswith('codex-cli '), 'continuation_pool_configuration_invalid')
        self.max_seconds, self.cli_version = max_seconds, cli_version
        self.loaded, self.configuration_pins = load_queue_slots(configuration_path, root=self.root,
            owner_id=owner_id, board_endpoint=board_endpoint, max_seconds=max_seconds)
        self.tools = {key: _executable_pin(os.fspath(value)) for key, value in
            (('executable', executable), ('interpreter', interpreter), ('unshare', unshare), ('gh', gh_executable))}
        self.sources = linux_source_pins()
        self.thread = threading.current_thread(); self.pid = os.getpid(); self._mutex = threading.Lock()
        self._retained_lease = None  # Kept until process exit if cooperative joins fail.

    def _pins(self, *, full=False):
        for pin in self.configuration_pins.values():
            require(file_document(pin['path'], MAX_CONFIGURATION)[1] == pin, 'continuation_pool_configuration_changed')
        if full:
            require(linux_source_pins() == self.sources, 'continuation_pool_configuration_changed')
            for pin in self.tools.values():
                require(_executable_pin(pin['path']) == pin, 'continuation_pool_configuration_changed')

    def _tick(self, slot, stopping, scheduler):
        from .development_queue import DevelopmentQueueAdmission
        from .development_queue_client import DevelopmentQueueDiscoveryClient
        config = slot['loaded']['configuration']; catalog = slot['loaded']['plan']['catalog']
        # Same deterministic session as the single standing facade for this file.
        session = 'continuation-queue-' + digest(canonical({'owner_id': self.owner_id,
            'agent_id': catalog['agent_id'], 'configuration_path': slot['path']}))
        with DevelopmentQueueDiscoveryClient.from_connection_file(catalog['connection_file'], session_id=session) as client:
            guarded = _GuardedDiscovery(client, slot, scheduler)
            result = DevelopmentQueueAdmission(Path(slot['root']), guarded).tick(config['catalog_file'],
                config['workflow_file'], approved_sha256=config['approved_sha256'],
                executable=self.tools['executable']['path'], cli_version=self.cli_version,
                interpreter=self.tools['interpreter']['path'], unshare=self.tools['unshare']['path'],
                gh_executable=self.tools['gh']['path'], max_seconds=self.max_seconds,
                checkpoint_after_actions=config['checkpoint_after_actions'],
                provider_backoff_seconds=config['provider_backoff_seconds'], should_stop=stopping)
        return _result(slot, result, guarded.filtered)

    def run_once(self, *, should_stop=None):
        require(self.pid == os.getpid() and self.thread is threading.current_thread())
        require(should_stop is None or callable(should_stop))
        try: return self._run(should_stop)
        except Exception as error:
            observation = _TargetObservation(None); observation.stage = 'queue_tick'
            return {'schema': 'continuation-pool-slots-pass-v1', 'status': 'needs_you', 'slots': [], 'tasks': [],
                    'activated': True, 'pass_complete_is_project_completion': False,
                    'failure': _target_failure(error, observation)}

    def _run(self, should_stop):
        require(self._mutex.acquire(blocking=False), 'continuation_pool_owner_busy')
        try:
            require(self._retained_lease is None, 'continuation_pool_cleanup_unknown')
            before = signature(os.stat(self.path, follow_symlinks=False))
            self._pins(full=True)
            slots = self.loaded['slots']; cap = self.loaded['configuration']['concurrency']
            # The same owner-root lease as a single continuation pass.
            with ContinuationPoolService._lease(self) as (_, held):
                reconcile_inventory(self.root, slots)
                deadline = time.monotonic() + self.max_seconds
                cancel = threading.Event(); failed = []; results = {}; threads = {}
                scheduler = _Scheduler(slots)

                def stopping():
                    if cancel.is_set(): return True
                    try:
                        require(signature(os.stat(self.path, follow_symlinks=False)) == before,
                                'continuation_pool_configuration_changed')
                        value = time.monotonic() >= deadline or bool(should_stop and should_stop())
                    except Exception: value = True
                    if value: cancel.set()
                    return value

                def advance(slot):
                    observation = _TargetObservation(None); observation.stage = 'queue_tick'
                    token = _TARGET_OBSERVATION.set(observation)
                    try: results[slot['name']] = self._tick(slot, stopping, scheduler)
                    except Exception as error:
                        code = vars(error).get('code') if type(error) is BoardReadError else None
                        phase = ('connection_expired' if code in CONNECTION_EXPIRED else
                                 'connection_unavailable' if code in CONNECTION_UNAVAILABLE else 'needs_you')
                        results[slot['name']] = {**_row(slot, phase), 'failure': _target_failure(error, observation)}
                    except BaseException:
                        failed.append(True); cancel.set()
                        results[slot['name']] = _row(slot, 'needs_you', 'continuation_pool_cleanup_unknown')
                    finally:
                        scheduler.release(slot); _TARGET_OBSERVATION.reset(token)

                initially = {slot['name']: slot_holding(slot)[0] for slot in slots}
                # Slots already holding work continue first; idle slots may
                # then discover only while durable holders stay below the cap.
                pending = ([slot for slot in slots if initially[slot['name']]]
                           + [slot for slot in slots if not initially[slot['name']]])
                join_deadline = None
                try:
                    while pending or threads:
                        held()
                        if stopping():
                            if join_deadline is None: join_deadline = time.monotonic() + JOIN_SECONDS
                            for slot in pending: results[slot['name']] = _row(slot, 'not_started', 'continuation_pool_stopped')
                            pending = []
                        while pending and len(threads) < cap and not stopping():
                            slot = pending[0]
                            if not initially[slot['name']] and len(scheduler.holders(threads)) >= cap: break
                            pending.pop(0)
                            thread = threading.Thread(target=advance, args=(slot,), daemon=False,
                                                      name='continuation-slot-' + slot['name'])
                            threads[slot['name']] = thread; thread.start()
                        if pending and not threads and not stopping():
                            # Only held slots could free capacity; none is running.
                            for slot in pending: results[slot['name']] = _row(slot, 'waiting_capacity')
                            pending = []
                        for name, thread in tuple(threads.items()):
                            thread.join(timeout=.025)
                            if not thread.is_alive(): del threads[name]
                        if join_deadline is not None and time.monotonic() >= join_deadline and threads:
                            self._retained_lease = True
                            for slot in slots:
                                results.setdefault(slot['name'], _row(slot, 'needs_you', 'continuation_pool_cleanup_unknown'))
                            break
                except BaseException:
                    cancel.set(); limit = time.monotonic() + JOIN_SECONDS
                    for thread in tuple(threads.values()):
                        if thread.ident is not None: thread.join(timeout=max(0, limit - time.monotonic()))
                    if failed or any(thread.is_alive() for thread in threads.values()): self._retained_lease = True
                    fail('continuation_pool_cleanup_unknown')
                if failed: self._retained_lease = True
                ordered = [results.get(slot['name'], _row(slot, 'not_started', 'continuation_pool_stopped'))
                           for slot in slots]
                status = ('needs_you' if failed or any(row['phase'] in ATTENTION for row in ordered)
                          else 'stopped' if cancel.is_set() else 'pass_complete')
            self._pins(full=True)
            return {'schema': 'continuation-pool-slots-pass-v1', 'status': status, 'concurrency': cap,
                    'slots': ordered, 'tasks': [row['task'] for row in ordered if row.get('task') is not None],
                    'activated': True, 'pass_complete_is_project_completion': False}
        finally:
            self._mutex.release()
