"""Explicit, qualified Linux Python backend and live broker snapshot boundary.

Preparation never starts a process. Native observations cannot promote a stale
snapshot; only the original broker context can admit a completed test.
"""
from __future__ import annotations
from contextlib import contextmanager, closing
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import uuid

from .dev_journal import PrivateDirectory, MAX_STATE_BYTES, decode
from .dev_manifest import (DevelopmentError, canonical, digest, prepare_manifest,
                           sha256, validate_manifest)
from .dev_test_supervisor import (_control_clock, _fresh_control_window,
                                  _send_control_window, _validate_control_window)
from .linux_input_plan import pin_linux_input_plan
from .linux_input_snapshot import (capture_input_snapshot_locked, _chain, _fence_record,
                                   validate_planner_snapshot)
from .linux_test_protocol import (EXECUTION_REQUEST_SCHEMA, encode_input_packet,
    linux_execution_backend_id, validate_execution_request_shape, validate_linux_execution_request)
from .linux_test_namespace import (MAX_INPUT_PACKET, REQUIRED_CHECKS, public_runtime,
                                   read_public, seccomp_program)

PLAN_SCHEMA = 'blackoutburst-linux-execution-plan-v1'
NATIVE_SCHEMA = 'blackoutburst-linux-native-observation-v1'
MAX_PLAN_BYTES = 512 * 1024
MAX_JOB_BYTES = 1024 * 1024
CHECKS = REQUIRED_CHECKS | {'inputs_write'}
KIND = 'linux-python-execution-v1'
_CONTEXT_FACTORY = object()
_ATTEMPT_FACTORY = object()
_CAPTURE_FACTORY = object()
QUALIFICATION_CASES = frozenset({'normal', 'assertion_failed', 'output_limit', 'wall_timeout',
    'source_revoked', 'eof_running', 'controller_loss', 'supervisor_loss', 'replay_and_corruption'})
QUALIFICATION_SCRIPTS = {
    'normal': b'import json\nfrom pathlib import Path\nassert json.loads(Path("qualification.json").read_text()) == {"value":42}\n',
    'assertion_failed': b'raise AssertionError("fixed qualification failure")\n',
    'output_limit': b'print("x" * 8192)\n',
    'wall_timeout': b'while True: pass\n',
    'source_revoked': b'pass\n',
    'eof_running': b'while True: pass\n',
    'controller_loss': b'while True: pass\n',
    'supervisor_loss': b'while True: pass\n',
    'replay_and_corruption': b'pass\n',
}
JOB_FIELDS = {'schema', 'nonce', 'request', 'backend_directory', 'plan_sha256',
    'script_sha256', 'timeout_seconds', 'max_output_bytes', 'control', 'qualification', 'qualification_case'}


def require(ok, reason='linux_execution_invalid'):
    if not ok:
        raise DevelopmentError(reason)


def _environment():
    from .linux_planner_policy import linux_execution_identity
    return linux_execution_identity()


def _sources():
    from .linux_planner_policy import linux_source_pins
    return linux_source_pins()


def _scripts(specification):
    return {name: {key: item[key] for key in ('path', 'timeout_seconds', 'max_output_bytes')}
            for name, item in specification.items()}


def pin_linux_execution_plan(*, root, scripts_root, scripts, interpreter='/usr/bin/python3.10', unshare='/usr/bin/unshare'):
    """Public/local metadata only. This is a plan, never a qualification/grant."""
    environment = _environment()
    require(environment['host']['system'] == 'Linux' and environment['host']['machine'] == 'x86_64')
    input_plan = pin_linux_input_plan(root=root, scripts_root=scripts_root, scripts=scripts).document()
    interpreter = str(Path(interpreter).resolve(strict=True)); unshare = str(Path(unshare).resolve(strict=True))
    _, python_pin = read_public(interpreter, root_owned=True)
    _, unshare_pin = read_public(unshare, root_owned=True)
    from .linux_test_supervisor import BOOTSTRAP
    plan = {'schema': PLAN_SCHEMA, 'input_plan': input_plan,
        'input_plan_sha256': digest(canonical(input_plan)), 'root': input_plan['root'],
        'root_identity': input_plan['root_chain'][-1]['identity'],
        'environment': environment, 'sources': _sources(),
        'interpreter': interpreter, 'interpreter_pin': python_pin,
        'unshare': unshare, 'unshare_pin': unshare_pin, 'runtime': public_runtime(),
        'bootstrap_sha256': digest(BOOTSTRAP.encode()), 'seccomp_sha256': digest(canonical(seccomp_program())),
        'imports': ['json', 'pathlib'],
        'limits': {'max_input_bytes': 4 * 1024 * 1024, 'max_input_entries': 256,
            'input_tmpfs_bytes': 8 * 1024 * 1024, 'input_inodes': 512,
            'work_tmpfs_bytes': 4 * 1024 * 1024, 'work_inodes': 128,
            'memory_bytes_per_process': 256 * 1024 * 1024, 'max_processes': 3,
            'startup_seconds': 5, 'cleanup_seconds': 2, 'control_ttl_seconds': 1.5}}
    require(len(canonical(plan)) <= MAX_PLAN_BYTES, 'linux_execution_plan_limit')
    validate_current_plan(plan)
    return plan


def validate_current_plan(plan):
    expected = {'schema', 'input_plan', 'input_plan_sha256', 'root', 'root_identity',
        'environment', 'sources', 'interpreter', 'interpreter_pin', 'unshare', 'unshare_pin',
        'runtime', 'bootstrap_sha256', 'seccomp_sha256', 'imports', 'limits'}
    require(type(plan) is dict and set(plan) == expected and plan['schema'] == PLAN_SCHEMA
            and len(canonical(plan)) <= MAX_PLAN_BYTES, 'linux_execution_plan_changed')
    require(plan['environment'] == _environment() and plan['sources'] == _sources(), 'linux_execution_environment_changed')
    from .linux_test_supervisor import BOOTSTRAP
    require(plan['bootstrap_sha256'] == digest(BOOTSTRAP.encode())
            and plan['seccomp_sha256'] == digest(canonical(seccomp_program()))
            and plan['imports'] == ['json', 'pathlib'], 'linux_execution_source_changed')
    limits = {'max_input_bytes': 4194304, 'max_input_entries': 256, 'input_tmpfs_bytes': 8388608,
        'input_inodes': 512, 'work_tmpfs_bytes': 4194304, 'work_inodes': 128,
        'memory_bytes_per_process': 268435456, 'max_processes': 3, 'startup_seconds': 5,
        'cleanup_seconds': 2, 'control_ttl_seconds': 1.5}
    require(type(plan['limits']) is dict and plan['limits'] == limits
            and all(type(plan['limits'][k]) is type(v) for k, v in limits.items()), 'linux_execution_plan_changed')
    original = plan['input_plan']
    require(type(original) is dict and plan['input_plan_sha256'] == digest(canonical(original)))
    observed = pin_linux_input_plan(root=original['root'], scripts_root=original['scripts_root'],
                                    scripts=_scripts(original['scripts'])).document()
    require(observed == original and plan['root'] == original['root']
            and plan['root_identity'] == original['root_chain'][-1]['identity'], 'linux_execution_script_changed')
    for path, pin in ((plan['interpreter'], plan['interpreter_pin']), (plan['unshare'], plan['unshare_pin'])):
        read_public(path, root_owned=True, expected=pin)
    require(plan['interpreter'] == str(Path(sys.executable).resolve(strict=True)),
            'linux_execution_interpreter_changed')
    require(type(plan['runtime']) is dict and 1 <= len(plan['runtime']) <= 256)
    total = 0
    for path, pin in plan['runtime'].items():
        _, current = read_public(path, root_owned=True, expected=pin,
            maximum=min(64*1024*1024,128*1024*1024-total),allow_empty=True)
        total += current['size']
        require(total <= 128 * 1024 * 1024, 'linux_execution_runtime_limit')
    return plan


def prepare_linux_execution_manifest(directory, *, execution_plan, **arguments):
    validate_current_plan(execution_plan)
    directory = Path(directory)
    require(directory.is_absolute() and '..' not in directory.parts)
    os.mkdir(directory, 0o700)  # Fresh directory only; never adopts even empty schema1/2.
    digest_value = prepare_manifest(directory, **arguments)
    with PrivateDirectory(directory) as private:
        manifest = validate_manifest(decode(private.read('manifest.json', 128 * 1024)))
        require(manifest['root'] == execution_plan['root']
                and manifest['root_identity'] == execution_plan['root_identity'])
        for suite in manifest['tests']:
            require(suite['backend_id'] == linux_execution_backend_id(digest(canonical(execution_plan))))
        private.replace('state.json', canonical({'schema': 3, 'manifest_hash': digest_value,
            'used': {'operations': 0, 'read_bytes': 0, 'patch_bytes': 0, 'tests': 0}, 'operations': {},
            'linux_execution_origin': str(uuid.uuid4()),
            'linux_execution_plan_sha256': digest(canonical(execution_plan)), 'linux_execution': {}}))
    return digest_value


def prepare_linux_backend(directory, *, manifest, plan):
    validate_manifest(manifest); validate_current_plan(plan)
    require(manifest['root'] == plan['root'] and manifest['root_identity'] == plan['root_identity'])
    expected_id = linux_execution_backend_id(digest(canonical(plan)))
    for suite in manifest['tests']:
        script = plan['input_plan']['scripts'].get(suite['name'])
        require(script is not None and suite['backend_id'] == expected_id
                and all(suite[key] == script[key] for key in ('timeout_seconds', 'max_output_bytes')))
    # Private control state must never be mounted as approved source or script.
    directory = Path(directory)
    require(directory.is_absolute() and '..' not in directory.parts)
    parent = _chain(str(directory.parent))
    for chain in (plan['input_plan']['root_chain'], plan['input_plan']['scripts_chain']):
        require(chain[-1]['identity'] not in [item['identity'] for item in parent],
                'linux_execution_directory_overlap')
    os.mkdir(directory, 0o700)
    private_chain = _chain(str(directory))
    for chain in (plan['input_plan']['root_chain'], plan['input_plan']['scripts_chain']):
        require(private_chain[-1]['identity'] not in [item['identity'] for item in chain],
                'linux_execution_directory_overlap')
    with PrivateDirectory(directory) as private:
        private.create('execution-plan.json', canonical(plan))
        private.create('backend-manifest.json', canonical(manifest))
        private.create('qualification.json', canonical(None))
    return digest(canonical(plan))


class LinuxExecutionContext:
    """One current concrete planner step. Not data and not recoverable from disk."""
    def __init__(self, factory, planner, state):
        require(factory is _CONTEXT_FACTORY)
        from .dev_planner import DevelopmentPlanner
        require(type(planner) is DevelopmentPlanner and planner._state is state)
        self.planner, self.state = planner, state
        self._pid, self._thread, self._active = os.getpid(), threading.current_thread(), True
        self.step = decode(canonical(state['step']))
        self.binding = decode(canonical(planner.binding))
        self.workflow_id = planner.operation_id
        self.backend = planner.broker.test_backend
        require(type(self.backend) is SupervisedLinuxNamedPythonBackend)
        self._check()

    def __reduce_ex__(self, _): raise TypeError('Live execution context is not serializable')
    def __copy__(self): raise TypeError('Live execution context is not copyable')
    def __deepcopy__(self, _): raise TypeError('Live execution context is not copyable')

    def _check(self, broker=None, key=None, name=None):
        require(self._active and self._pid == os.getpid() and self._thread is threading.current_thread(),
                'linux_execution_context_expired')
        planner = self.planner
        current = planner.journal.read()
        require(planner._state is self.state and planner.binding == self.binding
                and current['phase'] == self.state['phase'] == 'tool_intent'
                and current['step'] == self.state['step'] == self.step
                and self.step['action']['kind'] == 'test'
                and self.step['receipt'] is None and self.step['tool_finished'] is False
                and planner.broker.test_backend is self.backend, 'linux_execution_step_changed')
        validate_planner_snapshot(self.step['source_snapshot'], planner.broker.manifest)
        if broker is not None:
            require(broker is planner.broker and key == self.step['action_key']
                    and name == self.step['action']['name'], 'linux_execution_context_mismatch')
        return True

    def authorize(self):
        self._check()
        self.planner._linux_context_fresh(self.state)
        require(not self.planner.active_guard or not self.planner.active_guard.should_cancel(), 'planner_runtime_stopped')
        self._check()
        return True

    def authorize_capture(self):
        self._check()
        shared = self.planner._linux_capture_fresh(self.state)
        self._check()
        return shared

    def capture_complete(self, shared):
        self._check()
        self.planner._linux_capture_complete(self.state, shared)
        self._check()

    def refresh_after_fence_wait(self, local, since):
        self._check()
        self.planner._linux_capture_after_wait(self.state,local,since)
        self._check()

    def close(self):
        self._active = False


def execution_context(planner, state):
    return LinuxExecutionContext(_CONTEXT_FACTORY, planner, state)


class _ExecutionCapturePermit:
    def __init__(self, factory, broker, lease, key, reservation, context):
        require(factory is _CAPTURE_FACTORY)
        require(type(context) is LinuxExecutionContext and context.planner.broker is broker)
        self.broker, self.lease, self.key, self.reservation = broker, lease, key, decode(canonical(reservation))
        self.context = context
        self.count = 0
    def take(self, snapshot):
        self.broker.journal.require_lock_lease(self.lease)
        journal = self.broker.journal
        state = journal._lock_state
        durable = decode(journal.private.read('state.json', MAX_STATE_BYTES))
        require(digest(canonical(durable)) == journal._state_hash and canonical(durable) == canonical(state),
                'linux_execution_state_changed')
        require(state['schema'] == 3 and self.count < 5
                and state['linux_execution'][self.key]['reservation'] == self.reservation
                and snapshot._broker is self.broker and snapshot._lock_lease is self.lease)
        self.count += 1
    def authorize_capture(self, snapshot):
        require(snapshot._capture_permit is self and snapshot._broker is self.broker)
        return self.context.authorize_capture()
    def capture_complete(self, snapshot, shared):
        require(snapshot._capture_permit is self and snapshot._broker is self.broker)
        self.context.capture_complete(shared)
    def refresh_after_fence_wait(self, snapshot, since):
        require(snapshot._capture_permit is self and snapshot._broker is self.broker)
        self.context.refresh_after_fence_wait(snapshot._held_metadata,since)


class LinuxExecutionAttempt:
    def __init__(self, factory, broker, context, lease, snapshot, request, packet, permit):
        require(factory is _ATTEMPT_FACTORY)
        self.broker, self.context, self.lease, self.snapshot = broker, context, lease, snapshot
        self.request, self.packet, self.permit = decode(canonical(request)), packet, permit
        self._active, self._boundary, self.native = True, 0, None
    def _check(self):
        require(self._active and self.context._check())
        self.broker.journal.require_lock_lease(self.lease)
        self.snapshot.document()
    def authorize(self):
        self._check(); self.context.authorize(); self._check(); return True
    def before_source(self):
        self._check(); require(self._boundary == 0, 'linux_source_boundary_replayed')
        self.snapshot.revalidate(); self._check(); self._boundary = 1
        return True
    def after_result(self, native):
        self._check(); require(self._boundary == 1, 'linux_result_boundary_invalid')
        self.snapshot.revalidate(); self._check(); self._boundary = 2; self.native = native
    def close(self):
        self._active = False; self.context.close()


@contextmanager
def prepare_execution_attempt_locked(broker, *, context, key, name):
    require(type(context) is LinuxExecutionContext)
    context._check(broker, key, name)
    backend, journal = context.backend, broker.journal
    lease = journal.held_lock_lease()
    state = journal._lock_state
    require(state['schema'] == 3 and state['linux_execution_plan_sha256'] == backend.document['plan_hash'])
    operation_id = broker._operation_id(key)
    operation = state['operations'][operation_id]
    require(operation['phase'] == 'intent' and operation['admission']['dispatch'] == 'pending'
            and operation_id in broker._test_admissions and operation_id not in state['linux_execution'])
    context.authorize(); backend._pins()
    reservation = {'workflow_id': context.workflow_id,
        'planner_snapshot_sha256': context.step['source_snapshot']['sha256'],
        'fence_sha256': digest(canonical(_fence_record(broker, context.planner.fence, context.binding, context.workflow_id))),
        'input_plan_sha256': backend.plan['input_plan_sha256'], 'execution_plan_sha256': backend.document['plan_hash'],
        'action_sha256': digest(canonical(context.step['action'])), 'action_key_sha256': digest(key.encode()),
        'prior_state_sha256': journal._state_hash, 'capture_passes': 5,
        'reserved_read_bytes': 5 * (min(broker.manifest['budgets']['max_file_bytes'], 1048576) + 1) * len(broker.manifest['files'])}
    journal.charge(state, operations=0, read_bytes=reservation['reserved_read_bytes'], tests=1)
    record = {'phase': 'reserved', 'reservation': reservation, 'request': None,
              'native_receipt_sha256': None, 'admitted_snapshot_sha256': None}
    state['linux_execution'][operation_id] = record
    journal.save(state)  # Whole conservative charge + intent precede first read.
    permit = _ExecutionCapturePermit(_CAPTURE_FACTORY, broker, lease, operation_id, reservation, context)
    attempt = None
    try:
        with capture_input_snapshot_locked(broker, lock_lease=lease, fence=context.planner.fence,
                binding=context.binding, operation_id=context.workflow_id,
                planner_snapshot=context.step['source_snapshot'], authorize_reads=True,
                _capture_permit=permit) as snapshot:
            observed = snapshot.document()
            packet = encode_input_packet({path: snapshot.read(path) for path in broker.manifest['files']},
                manifest=broker.manifest, snapshot=observed, planner_snapshot=context.step['source_snapshot'])
            request = {'schema': EXECUTION_REQUEST_SCHEMA, 'operation_id': operation_id,
                'workflow_id': context.workflow_id, 'manifest_sha256': journal.manifest_hash,
                'plan_sha256': backend.document['plan_hash'], 'scope_sha256': digest(canonical(broker.manifest['scope'])),
                'input_snapshot_sha256': digest(canonical(observed)), 'input_packet_sha256': digest(packet),
                'planner_snapshot_sha256': reservation['planner_snapshot_sha256'], 'root': broker.manifest['root'],
                'root_identity': broker.manifest['root_identity'], 'name': name,
                'timeout_seconds': operation['detail']['timeout_seconds'],
                'max_output_bytes': operation['detail']['max_output_bytes'],
                'input_plan_sha256': backend.plan['input_plan_sha256'], 'fence_sha256': reservation['fence_sha256']}
            validate_linux_execution_request(request, manifest=broker.manifest, snapshot=observed,
                planner_snapshot=context.step['source_snapshot'], execution_plan_sha256=backend.document['plan_hash'],
                input_plan_sha256=backend.plan['input_plan_sha256'], input_packet=packet)
            record.update(phase='captured', request=request); journal.save(state)
            attempt = LinuxExecutionAttempt(_ATTEMPT_FACTORY, broker, context, lease, snapshot, request, packet, permit)
            yield attempt
            require(attempt._boundary == 2, 'linux_result_boundary_invalid')
        require(permit.count == 5, 'linux_capture_count_changed')
        context.authorize(); backend._pins()
    finally:
        if attempt is not None:
            attempt._active = False
        context.close()


def validate_native_receipt(value, job, plan):
    """Native observation only. This deliberately cannot produce PASSED broker proof."""
    fields = {'schema', 'nonce', 'request_sha256', 'plan_sha256', 'source_released',
        'source_release_intent', 'exit_code', 'reason', 'checks', 'launcher_attempted',
        'output_bytes', 'output_sha256', 'cleanup', 'identity', 'broker_snapshot_admitted'}
    require(type(value) is dict and set(value) == fields and value['schema'] == NATIVE_SCHEMA
            and value['nonce'] == job['nonce']
            and value['request_sha256'] == digest(canonical(job['request']))
            and value['plan_sha256'] == digest(canonical(plan))
            and value['broker_snapshot_admitted'] is False)
    for name in ('source_released', 'source_release_intent', 'launcher_attempted'):
        require(type(value[name]) is bool)
    require(type(value['output_bytes']) is int and 0 <= value['output_bytes'] <= job['max_output_bytes'])
    sha256(value['output_sha256'])
    require(value['exit_code'] is None or type(value['exit_code']) is int and -255 <= value['exit_code'] <= 255)
    require(type(value['checks']) is dict and (value['checks'] == {} or
            set(value['checks']) == CHECKS and all(type(v) is bool for v in value['checks'].values())))
    cleanup = value['cleanup']
    require(type(cleanup) is dict and set(cleanup) == {'init_identity_received', 'init_terminal',
        'launcher_reaped', 'cleanup_confirmed', 'numeric_pid_signalled'}
        and all(type(v) is bool for v in cleanup.values()) and cleanup['numeric_pid_signalled'] is False)
    require(cleanup['init_identity_received'] is (value['identity'] is not None)
            and (not cleanup['init_terminal'] or cleanup['init_identity_received'])
            and (value['launcher_attempted'] or not cleanup['launcher_reaped']))
    if value['identity'] is not None:
        require(type(value['identity']) is dict and set(value['identity']) ==
                    {'namespace_identity', 'acquired_by', 'numeric_pid_signalled'}
                and value['identity']['acquired_by'] == 'SCM_RIGHTS_validated'
                and value['identity']['numeric_pid_signalled'] is False)
        namespace=value['identity']['namespace_identity']
        require(type(namespace) is list and len(namespace)==2
                and all(type(item) is int and item>0 for item in namespace))
    if value['source_release_intent']:
        require(value['launcher_attempted'] and value['identity'] is not None
                and set(value['checks'])==CHECKS and all(value['checks'].values()))
    if value['source_released']:
        require(value['source_release_intent'] and value['launcher_attempted']
                and set(value['checks']) == CHECKS and all(value['checks'].values())
                and type(value['identity']) is dict and set(value['identity']) ==
                    {'namespace_identity', 'acquired_by', 'numeric_pid_signalled'}
                and value['identity']['acquired_by'] == 'SCM_RIGHTS_validated'
                and value['identity']['numeric_pid_signalled'] is False)
        namespace = value['identity']['namespace_identity']
        require(type(namespace) is list and len(namespace) == 2
                and all(type(item) is int and item > 0 for item in namespace))
    else:
        require(value['exit_code'] is None and value['reason'] != 'completed')
    if cleanup['cleanup_confirmed'] and value['launcher_attempted']:
        require(cleanup['init_identity_received'] and cleanup['init_terminal'] and cleanup['launcher_reaped'])
    if not value['launcher_attempted']:
        require(not value['source_released'] and not value['source_release_intent'] and value['exit_code'] is None)
    allowed = {'completed', 'script_failed', 'linux_authorization_expired', 'linux_startup_expired',
        'linux_source_boundary_missing', 'linux_kernel_guard_failed', 'linux_script_wall_expired',
        'linux_output_limit', 'linux_native_unconfirmed'}
    require(type(value['reason']) is str and value['reason'] in allowed)
    if value['reason'] == 'completed':
        require(value['source_released'] and value['exit_code'] == 0 and cleanup['cleanup_confirmed'])
    return decode(canonical(value))


def lookup_linux_native_cleanup(directory, *, manifest, record, operation_id, workflow_id,
                                expected_plan_sha256):
    """Identity-only historical observation. No grants, runtime probes or replay.

    Current environment/source/qualification are intentionally not consulted:
    revocation must not prevent confirming an already observed exact cleanup.
    This function never constructs a backend and never returns PASSED evidence.
    """
    from .linux_execution_state import identifier
    identifier(workflow_id); sha256(operation_id); sha256(expected_plan_sha256)
    require(type(record) is dict and type(record.get('reservation')) is dict)
    reservation = record['reservation']; request = record.get('request')
    validate_execution_request_shape(request)
    require(reservation['workflow_id'] == request['workflow_id'] == workflow_id
            and request['operation_id'] == operation_id
            and request['manifest_sha256'] == digest(canonical(manifest))
            and request['plan_sha256'] == reservation['execution_plan_sha256'] == expected_plan_sha256
            and request['root'] == manifest['root'] and request['root_identity'] == manifest['root_identity']
            and request['scope_sha256'] == digest(canonical(manifest['scope'])))
    with PrivateDirectory(directory) as backend:
        plan = decode(backend.read('execution-plan.json', MAX_PLAN_BYTES))
        require(type(plan) is dict and plan.get('schema') == PLAN_SCHEMA
                and digest(canonical(plan)) == expected_plan_sha256
                and decode(backend.read('backend-manifest.json', 128 * 1024)) == manifest)
        with PrivateDirectory(backend.path / ('execution-' + operation_id)) as private:
            job = validate_job_shape(decode(private.read('job.json', MAX_JOB_BYTES)),private.path)
            require(job.get('request') == request
                    and job.get('schema') == KIND and job.get('qualification') is False
                    and job.get('qualification_case') is None
                    and job.get('plan_sha256') == expected_plan_sha256
                    and job.get('backend_directory') == str(backend.path)
                    and job.get('max_output_bytes') == request['max_output_bytes'])
            native = read_native_observation(private,job,plan)
            require(native is not None,'development_cleanup_receipt_missing')
            raw = canonical(native)
            if record.get('native_receipt_sha256') is not None:
                require(record['native_receipt_sha256'] == digest(raw))
            require(native['cleanup']['cleanup_confirmed'] is True,
                    'development_cleanup_receipt_missing')
    return {'cleanup_confirmed': True, 'status': 'blocked',
            'native_receipt_sha256': digest(raw), 'broker_snapshot_admitted': False}


def _state(value, job):
    expected = {'schema', 'nonce', 'request_sha256', 'phase', 'boundary_sequence',
                'source_challenge', 'native_receipt_sha256'}
    phases = {'helper_launch_intent', 'namespace_launch_intent', 'source_boundary_requested',
              'source_release_intent', 'source_ack_written', 'complete'}
    require(type(value) is dict and set(value) == expected and type(value['schema']) is int
            and value['schema'] == 1 and value['nonce'] == job['nonce']
            and value['request_sha256'] == digest(canonical(job['request'])) and value['phase'] in phases)
    require(value['boundary_sequence'] is None or type(value['boundary_sequence']) is int
            and 1 <= value['boundary_sequence'] <= 65536)
    require(value['source_challenge'] is None or type(value['source_challenge']) is str
            and len(value['source_challenge']) == 32 and all(c in '0123456789abcdef' for c in value['source_challenge']))
    if value['native_receipt_sha256'] is not None:
        sha256(value['native_receipt_sha256'])
    require(value['phase'] != 'complete' or value['native_receipt_sha256'] is not None)
    return value


def validate_job_shape(job, directory):
    require(type(job) is dict and set(job) == JOB_FIELDS and job['schema'] == KIND
            and type(job['qualification']) is bool and type(job['nonce']) is str
            and len(job['nonce']) == 32 and all(c in '0123456789abcdef' for c in job['nonce']))
    validate_execution_request_shape(job['request'])
    sha256(job['script_sha256']);sha256(job['plan_sha256'])
    require(type(job['backend_directory']) is str and len(job['backend_directory']) <= 4096
            and Path(job['backend_directory']).is_absolute() and '..' not in Path(job['backend_directory']).parts
            and Path(directory).parent == Path(job['backend_directory'])
            and Path(directory).name == 'execution-' + job['request']['operation_id']
            and type(job['timeout_seconds']) is int and job['timeout_seconds'] == job['request']['timeout_seconds']
            and type(job['max_output_bytes']) is int and job['max_output_bytes'] == job['request']['max_output_bytes']
            and job['plan_sha256'] == job['request']['plan_sha256'])
    require(type(job['control']) is dict)
    _validate_control_window(job['control'],nonce=job['nonce'],now=job['control'].get('validated_at'),allow_expired=True)
    require(job['qualification_case'] in QUALIFICATION_CASES if job['qualification']
            else job['qualification_case'] is None)
    return job


def read_native_observation(private,job,plan):
    """Bounded exact terminal chain, without current grants or process lookup."""
    state=_state(decode(private.read('state.json',8192)),job)
    if state['phase']!='complete':return None
    raw=private.read('native-receipt.json',128*1024)
    require(digest(raw)==state['native_receipt_sha256'])
    native=validate_native_receipt(decode(raw),job,plan)
    require(canonical(native)==raw)
    if native['source_release_intent']:
        from .linux_test_supervisor import source_window_ready
        require(source_window_ready(private))
        admission=decode(private.read('source-admission.json',4096))
        approval=decode(private.read('source-window.json',2048))
        require(type(admission) is dict and set(admission)==
                {'nonce','request_sha256','identity','control','phase'}
                and admission['nonce']==job['nonce'] and admission['phase']=='source_release_intent'
                and admission['request_sha256']==digest(canonical(job['request']))
                and admission['identity']==native['identity'] and type(admission['control']) is dict)
        window=admission['control']
        # An isolated archived window cannot attest the intervening sequence.
        # Reuse only the time/shape validator; the retained challenge binds this
        # exact SOURCE window, never a renewed/current authorization.
        require(type(window.get('sequence')) is int and 1 <= window['sequence'] <= 65536)
        _validate_control_window({**window,'sequence':1},nonce=job['nonce'],
                                 now=window.get('validated_at'),allow_expired=True)
        require(state['boundary_sequence'] is not None and state['source_challenge'] is not None
                and window['sequence']>state['boundary_sequence']
                and type(approval) is dict and set(approval)==
                    {'nonce','challenge','request_sha256','window_sha256'}
                and approval['nonce']==job['nonce'] and approval['challenge']==state['source_challenge']
                and approval['request_sha256']==digest(canonical(job['request']))
                and approval['window_sha256']==digest(canonical(window)))
    return native


def load_supervisor_job(directory):
    """Helper entry revalidates public pins before it can create a namespace."""
    private = PrivateDirectory(directory)
    try:
        job = validate_job_shape(decode(private.read('job.json', MAX_JOB_BYTES)),private.path)
        with PrivateDirectory(job['backend_directory']) as backend_private:
            plan = decode(backend_private.read('execution-plan.json', MAX_PLAN_BYTES))
            validate_current_plan(plan)
            require(job['plan_sha256'] == digest(canonical(plan)) == job['request']['plan_sha256'])
            if not job['qualification']:
                require(job['qualification_case'] is None)
                script = plan['input_plan']['scripts'].get(job['request']['name'])
                require(script is not None and script['sha256'] == job['script_sha256'])
            else:
                require(job['qualification_case'] in QUALIFICATION_CASES
                        and digest(QUALIFICATION_SCRIPTS[job['qualification_case']]) == job['script_sha256'])
        require(digest(private.read('script.py', 65536)) == job['script_sha256']
                and digest(private.read('input.packet', MAX_INPUT_PACKET)) == job['request']['input_packet_sha256'])
        state = _state(decode(private.read('state.json', 8192)), job)
        require(state['phase'] == 'helper_launch_intent', 'linux_job_already_attempted')
        return private, job, plan, state
    except BaseException:
        private.close()
        raise


class SupervisedLinuxNamedPythonBackend:
    kind = KIND
    def __init__(self, directory, *, manifest, authorize_scope):
        require(callable(authorize_scope))
        self.private = PrivateDirectory(directory)
        self.authorize_scope = authorize_scope
        self.manifest = validate_manifest(decode(canonical(manifest)))
        self._pid, self._thread, self._busy = os.getpid(), threading.current_thread(), False
        self._lock_fd = None; self._lock_identity = None
        try:
            self.plan = decode(self.private.read('execution-plan.json', MAX_PLAN_BYTES))
            self.document = {'plan_hash': digest(canonical(self.plan)), 'kind': KIND}
            self.backend_id = linux_execution_backend_id(self.document['plan_hash'])
            self.supervision = {'kind': KIND, 'sources': self.plan['sources'],
                'environment': self.plan['environment'], 'bootstrap_sha256': self.plan['bootstrap_sha256']}
            self._chain = _chain(str(self.private.path))
            self._pins()
            require(decode(self.private.read('backend-manifest.json', 128 * 1024)) == self.manifest)
            self._lock_fd = os.open('execution.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                                    0o600, dir_fd=self.private.fd)
            self.private.validate_file(self._lock_fd)
            info=os.fstat(self._lock_fd);self._lock_identity=(info.st_dev,info.st_ino)
        except BaseException:
            self.close(); raise
    @property
    def environment(self):
        return decode(canonical(self.plan['environment']))
    def close(self):
        if self._lock_fd is not None:
            try:
                info=os.fstat(self._lock_fd)
                if (info.st_dev,info.st_ino)==self._lock_identity:os.close(self._lock_fd)
            except OSError:pass
            self._lock_fd = None
        if self.private.fd is not None:
            try:
                info=os.fstat(self.private.fd)
                if (info.st_dev,info.st_ino)!=self.private.identity:self.private.fd=None
            except OSError:self.private.fd=None
        self.private.close()
    def _pins(self):
        require(self._pid == os.getpid() and self._thread is threading.current_thread(), 'linux_backend_identity_changed')
        self.private.verify()
        require(_chain(str(self.private.path)) == self._chain
                and digest(self.private.read('execution-plan.json', MAX_PLAN_BYTES)) == self.document['plan_hash'])
        validate_current_plan(self.plan)
        require(self.manifest['root'] == self.plan['root'] and self.manifest['root_identity'] == self.plan['root_identity'])
    def _authorize(self):
        require(self.authorize_scope(self.manifest['scope'], 'repo.run_test', self.manifest['resource']) is True,
                'permission_revoked')
        return True
    def qualified_for(self, manifest_hash, suite):
        try:
            self._pins()
            value = decode(self.private.read('qualification.json', 128 * 1024))
            matches = (type(value) is dict and set(value) == {'schema', 'plan_sha256', 'environment_sha256',
                'checks', 'cleanup_confirmed', 'native_receipts'}
                and value['schema'] == 'blackoutburst-linux-execution-qualification-v1'
                and value['plan_sha256'] == self.document['plan_hash']
                and value['environment_sha256'] == digest(canonical(self.environment))
                and type(value['checks']) is dict and set(value['checks']) == QUALIFICATION_CASES
                and all(v is True for v in value['checks'].values()) and value['cleanup_confirmed'] is True
                and type(value['native_receipts']) is dict and set(value['native_receipts']) == QUALIFICATION_CASES
                and all(type(v) is str and len(v) == 64 for v in value['native_receipts'].values())
                and manifest_hash == digest(canonical(self.manifest)) and suite in self.manifest['tests']
                and suite['backend_id'] == self.backend_id)
            if not matches:
                return False
            from .linux_execution_qualification import validate_qualification_evidence
            return validate_qualification_evidence(self, value) is True
        except (DevelopmentError, OSError, ValueError, KeyError, TypeError):
            return False

    @contextmanager
    def _locked(self):
        require(not self._busy, 'linux_backend_busy')
        self._busy = True
        locked = False
        try:
            self.private.validate_file(self._lock_fd)
            info = os.stat('execution.lock', dir_fd=self.private.fd, follow_symlinks=False)
            require((info.st_dev, info.st_ino) == self._lock_identity ==
                    (os.fstat(self._lock_fd).st_dev, os.fstat(self._lock_fd).st_ino))
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB); locked = True
            yield
        finally:
            if locked:
                info=os.fstat(self._lock_fd)
                require((info.st_dev,info.st_ino)==self._lock_identity,'linux_backend_lock_changed')
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._busy = False

    def _job_directory(self, request):
        validate_execution_request_shape(request)
        return self.private.path / ('execution-' + request['operation_id'])

    def _read_native(self, request):
        with PrivateDirectory(self._job_directory(request)) as private:
            job = validate_job_shape(decode(private.read('job.json', MAX_JOB_BYTES)),private.path)
            require(job['request'] == request and job['plan_sha256'] == self.document['plan_hash'])
            return read_native_observation(private,job,self.plan)

    def lookup(self, request):
        """Historical native observation; never an admission for current input."""
        require(type(request) is dict)
        return self._read_native(request)

    def lookup_not_started(self, request):
        # A missing helper receipt is never proof of non-launch.
        return None

    def run(self, attempt):
        require(type(attempt) is LinuxExecutionAttempt and attempt.context.backend is self
                and attempt._active, 'linux_execution_attempt_required')
        attempt._check()
        require(self.qualified_for(digest(canonical(self.manifest)),
            next(item for item in self.manifest['tests'] if item['name'] == attempt.request['name'])),
            'test_backend_unqualified')
        original = self.plan['input_plan']
        from .dev_manifest import RepositoryFiles
        with closing(RepositoryFiles(original['scripts_root'], original['scripts_chain'][-1]['identity'])) as files:
            script, _ = files.read(original['scripts'][attempt.request['name']]['path'], 65536)
            require(digest(script) == original['scripts'][attempt.request['name']]['sha256'])
        with self._locked():
            return self._run_job(attempt.request, attempt.packet, script, attempt, qualification=False)

    def _run_job(self, request, inputs, script, context, *, qualification, qualification_case=None):
        from .linux_test_supervisor import helper_command
        def authorize_current():
            self._pins()
            return context.authorize()
        def source_current():
            self._pins()
            return context.before_source()
        self._pins(); context.authorize()
        directory = self._job_directory(request)
        os.mkdir(directory, 0o700)  # Exclusive forever; an interrupted attempt is not retryable.
        private = PrivateDirectory(directory)
        child = None
        try:
            private.create('script.py', script); private.create('input.packet', inputs)
            os.mkdir('root', 0o700, dir_fd=private.fd); os.fsync(private.fd)
            nonce = uuid.uuid4().hex
            first = _fresh_control_window(authorize_current, nonce=nonce, sequence=1)
            job = {'schema': KIND, 'nonce': nonce, 'request': request,
                'backend_directory': str(self.private.path), 'plan_sha256': self.document['plan_hash'],
                'script_sha256': digest(script), 'timeout_seconds': request['timeout_seconds'],
                'max_output_bytes': request['max_output_bytes'], 'control': first, 'qualification': qualification,
                'qualification_case': qualification_case}
            private.create('job.json', canonical(job))
            state = {'schema': 1, 'nonce': nonce, 'request_sha256': digest(canonical(request)),
                'phase': 'helper_launch_intent', 'boundary_sequence': None,
                'source_challenge': None, 'native_receipt_sha256': None}
            private.create('state.json', canonical(state))
            self._pins()
            # Durable ambiguity is already committed; never replay even on Popen error.
            child = subprocess.Popen(helper_command(self.plan, 'supervise', directory),
                cwd=private.path, env={'LANG': 'C.UTF-8', 'PATH': '/usr/bin:/bin'},
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=True)
            os.set_blocking(child.stdin.fileno(), False)
            previous, revoked, boundary_sent = first, False, False
            deadline = _control_clock() + request['timeout_seconds'] + 12
            try:
                _send_control_window(child.stdin.fileno(), first)
            except (DevelopmentError, OSError):
                revoked = True
            while _control_clock() < deadline:
                state = _state(decode(private.read('state.json', 8192)), job)
                if qualification:
                    context.observe_controller_state(child, state, request)
                if state['phase'] == 'complete':
                    child.wait(timeout=2)
                    native = self._read_native(request)
                    require(native is not None, 'linux_native_receipt_missing')
                    return native
                if child.poll() is not None:
                    raise DevelopmentError('linux_supervisor_lost')
                if not revoked:
                    try:
                        if state['phase'] in ('source_boundary_requested','source_release_intent'):
                            require(boundary_sent or state['phase']=='source_boundary_requested',
                                    'linux_source_boundary_unconfirmed')
                            if not boundary_sent:
                                fresh = _fresh_control_window(source_current, nonce=nonce,
                                    sequence=previous['sequence'] + 1, previous=previous)
                                private.create('source-window.json', canonical({'nonce': nonce,
                                    'challenge': state['source_challenge'],
                                    'request_sha256': digest(canonical(request)),
                                    'window_sha256': digest(canonical(fresh))}))
                                # The final JSON name can be visible during its
                                # write. Publish an empty no-replace marker only
                                # after its bytes and directory entry are durable.
                                private.create('source-window.ready', b'')
                                _send_control_window(child.stdin.fileno(), fresh, previous=previous)
                                previous, boundary_sent = fresh, True
                        else:
                            fresh = _fresh_control_window(authorize_current, nonce=nonce,
                                sequence=previous['sequence'] + 1, previous=previous)
                            _send_control_window(child.stdin.fileno(), fresh, previous=previous)
                            previous = fresh
                    except (DevelopmentError, OSError):
                        revoked = True
                if revoked and not child.stdin.closed:
                    child.stdin.close(); deadline = min(deadline, _control_clock() + 5)
                time.sleep(0.02)
            raise DevelopmentError('linux_supervisor_cleanup_unconfirmed')
        finally:
            if child is not None and not child.stdin.closed:
                child.stdin.close()
            if child is not None and child.poll() is None:
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass  # Unconfirmed stays durable; do not kill the cleaner or replay.
            private.close()

    def _qualify_case(self, case, *, fault_authorizer=None):
        require(case in QUALIFICATION_CASES, 'linux_qualification_case_invalid')
        require(callable(fault_authorizer) if case in ('controller_loss','supervisor_loss')
                else fault_authorizer is None, 'linux_qualification_observer_required')
        script = QUALIFICATION_SCRIPTS[case]
        snapshot_hash = digest(b'fixed-linux-qualification-input')
        body = b'{"value":42}'
        header = canonical({'schema': 'blackoutburst-linux-test-inputs-v1',
            'snapshot_sha256': snapshot_hash, 'files': [{'path': 'qualification.json', 'offset': 0,
                'bytes': len(body), 'sha256': digest(body)}]})
        packet = b'BB_LINUX_INPUT_V1\n' + len(header).to_bytes(8, 'big') + header + body
        request = {'schema': EXECUTION_REQUEST_SCHEMA, 'operation_id': digest(uuid.uuid4().bytes),
            'workflow_id': str(uuid.uuid4()), 'manifest_sha256': digest(canonical(self.manifest)),
            'plan_sha256': self.document['plan_hash'], 'scope_sha256': digest(canonical(self.manifest['scope'])),
            'input_snapshot_sha256': snapshot_hash, 'input_packet_sha256': digest(packet),
            'planner_snapshot_sha256': snapshot_hash, 'root': self.manifest['root'],
            'root_identity': self.manifest['root_identity'], 'name': 'qualification-' + case,
            'timeout_seconds': 1 if case == 'wall_timeout' else 5, 'max_output_bytes': 4096,
            'input_plan_sha256': self.plan['input_plan_sha256'], 'fence_sha256': digest(b'qualification-no-broker')}
        context = _QualificationContext(self, case, fault_authorizer=fault_authorizer)
        native = self._run_job(request, packet, script, context, qualification=True, qualification_case=case)
        return {'request': request, 'native': native, 'native_receipt_sha256': digest(canonical(native))}

    def qualify(self):
        """Explicit final-source matrix; never a model, login or Board call."""
        from .linux_execution_qualification import qualify_execution_backend
        with self._locked():
            self._pins()
            require(decode(self.private.read('qualification.json',128*1024)) is None,
                    'linux_qualification_already_attempted')
            try:
                os.stat('qualification-matrix',dir_fd=self.private.fd,follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise DevelopmentError('linux_qualification_already_attempted')
            evidence = qualify_execution_backend(self)
            require(type(evidence) is dict and set(evidence) == {'checks', 'native_receipts', 'cleanup_confirmed'}
                    and set(evidence['checks']) == QUALIFICATION_CASES
                    and all(value is True for value in evidence['checks'].values())
                    and evidence['cleanup_confirmed'] is True, 'linux_execution_unqualified')
            self._pins()
            receipt = {'schema': 'blackoutburst-linux-execution-qualification-v1',
                'plan_sha256': self.document['plan_hash'], 'environment_sha256': digest(canonical(self.environment)),
                **evidence}
            self.private.replace('qualification.json', canonical(receipt))
            return decode(canonical(receipt))


class _QualificationContext:
    """Controller-authored fixture control. Never accepted by broker.run_test."""
    def __init__(self, backend, case, *, fault_authorizer=None):
        self.backend, self.case, self.stage = backend, case, None
        self.fault_authorizer,self.fault_observed=fault_authorizer,False
    def authorize(self):
        self.backend._authorize()
        require(not (self.case == 'eof_running' and self.stage == 'source_ack_written'),
                'linux_qualification_eof')
        return True
    def before_source(self):
        require(self.case != 'source_revoked', 'linux_qualification_source_revoked')
        return self.authorize()
    def observe_controller_state(self, child, state, request):
        self.stage = state['phase']
        if self.stage == 'source_ack_written' and not self.fault_observed:
            if self.case in ('controller_loss','supervisor_loss'):
                require(callable(self.fault_authorizer)
                        and self.fault_authorizer(self.case,child,decode(canonical(state)),
                                                 decode(canonical(request))) is True,
                        'linux_qualification_observer_required')
                self.fault_observed=True
            if self.case == 'controller_loss':
                # Only the explicit qualification driver may request self-loss.
                os.kill(os.getpid(), 9)
            if self.case == 'supervisor_loss' and child.poll() is None:
                child.kill()  # Exact owned unreaped helper, no journal PID.
