"""INERT durable input preparation. No process, backend or recovery adoption.

The fresh input-only journal is intentionally incompatible with schema1. Its
manifest inventory precedes, and is separate from, the five reserved captures.
A complete receipt means only that input preparation finished. It is never a
Linux execution qualification, a test result, or evidence that nothing launched.
"""
from __future__ import annotations
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import uuid
from .dev_broker import DevelopmentBroker
from .dev_fence import DevelopmentFence
from .dev_journal import DevelopmentJournal, PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, absolute_directory, canonical, digest,
                           identity, opaque, prepare_manifest, signature)
from .linux_input_plan import _LinuxInputPlan
from .linux_input_snapshot import (_chain, _fence_record, _manifest,
    capture_input_snapshot_locked, validate_planner_snapshot)
from .linux_input_state import PASSES, PROFILE, unique_id
from .linux_test_protocol import (LinuxTestRequest, encode_input_packet,
                                   linux_backend_id, validate_linux_request)


def _fail(code='linux_input_admission_invalid'):
    raise DevelopmentError(code)


def prepare_linux_input_manifest(directory, **manifest_arguments):
    """Inventory into an exclusively NEW input-only private directory.

    No conversion API exists. A partial directory after any failure is retained
    and is never retried/adopted by this factory, even if its counters are zero.
    The existing manifest inventory's authorization/read limits remain in force.
    """
    path = Path(directory).absolute()
    parent = absolute_directory(str(path.parent))
    try:
        try: os.mkdir(path.name, 0o700, dir_fd=parent)
        except OSError: _fail('linux_input_fresh_directory_required')
        os.fsync(parent)
        pinned = identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False))
        with PrivateDirectory(path) as private:
            if private.identity != pinned: _fail()
            private_chain = _chain(str(path))
        result = prepare_manifest(path, **manifest_arguments)
        with PrivateDirectory(path) as private:
            if private.identity != pinned or _chain(str(path)) != private_chain: _fail()
            value = decode(private.read('state.json', 2*1024*1024))
            expected = {'schema':1, 'manifest_hash':result,
                'used':{'operations':0,'read_bytes':0,'patch_bytes':0,'tests':0}, 'operations':{}}
            if value != expected: _fail()
            private.replace('state.json', canonical({**value, 'schema':2,
                             'linux_input_origin':str(uuid.uuid4())}))
        return result
    finally: os.close(parent)


_CAPTURE_FACTORY = object()


class _CapturePermit:
    """Private, acquisition-bound five-pass limiter; never stored or adopted."""
    def __init__(self, factory, broker, lease, operation):
        if factory is not _CAPTURE_FACTORY: _fail()
        self._broker,self._lease,self._operation = broker,lease,operation
        self._active,self._count = True,0
        self._pid,self._thread = os.getpid(),threading.current_thread()

    def __reduce_ex__(self, protocol): raise TypeError('Capture permits cannot be serialized')
    def __copy__(self): raise TypeError('Capture permits cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('Capture permits cannot be copied')

    def take(self, snapshot):
        try:
            if (not self._active or self._count >= PASSES or self._pid != os.getpid()
                    or self._thread is not threading.current_thread()
                    or snapshot._broker is not self._broker or snapshot._lock_lease is not self._lease):
                _fail('linux_input_capture_permit_invalid')
            state = self._broker.journal._linux_input_state_for_lease(self._lease)
            op = self._operation
            if (state['operations'].get(op['id']) is not op or op['phase'] != 'intent'
                    or snapshot._operation_id != op['detail']['workflow_id']
                    or snapshot._planner_snapshot['sha256'] != op['detail']['planner_sha256']
                    or digest(canonical(snapshot._record)) != op['detail']['fence_sha256']):
                _fail('linux_input_capture_permit_invalid')
            self._count += 1
        except BaseException:
            self._active = False
            raise


class _PreparedInputs:
    """Ephemeral observation handle, never serializable or a reusable admission."""
    def __init__(self, broker, lease, plan, private, chain, snapshot, request, packet):
        self._broker,self._lease,self._plan = broker,lease,plan
        self._private,self._chain,self._snapshot = private,chain,snapshot
        self._request,self._packet = request,packet
        self._active,self._step = True,0
        self._pid,self._thread = os.getpid(),threading.current_thread()
        self._pins = {}
        for name,data in (('plan.json',canonical(plan.document())),
                          ('snapshot.json',canonical(snapshot.document())),
                          ('request.json',canonical(request)),('input.packet',packet)):
            private.create(name,data)
            self._pins[name] = (digest(data), len(data), list(signature(os.stat(name,dir_fd=private.fd,follow_symlinks=False))))
        self._check()

    def __reduce_ex__(self, protocol): raise TypeError('Prepared input handles cannot be serialized')
    def __copy__(self): raise TypeError('Prepared input handles cannot be copied')
    def __deepcopy__(self, memo): raise TypeError('Prepared input handles cannot be copied')

    def _check(self):
        try:
            if (not self._active or self._pid != os.getpid()
                    or self._thread is not threading.current_thread()): _fail('linux_input_attempt_not_held')
            self._broker.journal.require_lock_lease(self._lease)
            self._private.verify()
            if _chain(str(self._private.path)) != self._chain: _fail('linux_input_directory_changed')
            self._plan.verify()
            self._snapshot.document()
            self._check_payload()
        except BaseException:
            self._active = False
            raise

    def _check_payload(self):
        self._private.verify()
        if _chain(str(self._private.path)) != self._chain: _fail('linux_input_directory_changed')
        for name,(hashed,size,inode) in self._pins.items():
            data = self._private.read(name,size)
            if (len(data) != size or digest(data) != hashed
                    or list(signature(os.stat(name,dir_fd=self._private.fd,follow_symlinks=False))) != inode):
                _fail('linux_input_payload_changed')

    def request_document(self):
        self._check()
        return decode(canonical(self._request))

    def input_packet(self):
        self._check()
        return self._packet

    def observe_boundary(self, boundary):
        """Two ordered INPUT observations only; no claim of execution/ACK/result."""
        self._check()
        if self._step >= 2 or boundary != ('before_source','after_result')[self._step]:
            self._active = False
            _fail('linux_input_boundary_order')
        try:
            self._snapshot.revalidate()
            self._check()
            self._step += 1
            return {'boundary':boundary, 'input_unchanged':True, 'execution_supported':False}
        except BaseException:
            self._active = False
            raise


def _review(journal, state, operation):
    if operation['phase'] != 'intent': return
    operation['phase'] = 'needs_review'
    operation['receipt'] = {'operation_id':operation['id'], 'kind':'linux_input',
        'status':'needs_review', 'result':'needs_review', 'reason':'preparation_interrupted',
        'execution_supported':False, 'adoptable':False}
    journal.save(state)


@contextmanager
def prepare_linux_input_attempt_locked(broker, *, lock_lease, fence, binding,
        workflow_id, planner_snapshot, plan, name, key):
    """Five bounded captures under ONE exact live lease, no executor or replay.

    Full broker authorization precedes each capture; plan/script reads have
    independent fixed bounds. Reservation+intent is one atomic state write
    before the first workspace capture. Errors retain the charge and intent or
    needs_review; a persisted partial payload can never become a live handle.
    """
    if (type(broker) is not DevelopmentBroker or type(broker.journal) is not DevelopmentJournal
            or type(fence) is not DevelopmentFence or type(plan) is not _LinuxInputPlan
            or broker.enabled is not True): _fail()
    opaque(name,maximum=64)
    unique_id(workflow_id)
    if type(binding) is not dict or type(binding.get('scope')) is not dict: _fail()
    operation_id = broker._operation_id(key)
    journal = broker.journal
    state = journal._linux_input_state_for_lease(lock_lease)
    journal.require_settled(state)
    validate_planner_snapshot(planner_snapshot,broker.manifest)
    plan.verify(); document = plan.document()
    if document['root'] != broker.manifest['root'] or name not in document['scripts']: _fail()
    suite = next((item for item in broker.manifest['tests'] if item['name'] == name), None)
    script = document['scripts'][name]
    if (suite is None or suite['backend_id'] != linux_backend_id(plan.sha256)
            or any(suite[field] != script[field] for field in ('timeout_seconds','max_output_bytes'))): _fail()
    record = _fence_record(broker,fence,binding,workflow_id)
    broker._authorize('repo.read_file'); broker._authorize('repo.run_test')
    journal._linux_input_state_for_lease(lock_lease)
    if operation_id in state['operations']: _fail('linux_input_attempt_not_reusable')
    limits = _manifest(broker.manifest)
    reserve = PASSES * limits['max_file_bytes'] * len(broker.manifest['files'])
    # Charge validation occurs before directory creation or workspace capture.
    candidate = decode(canonical(state))
    journal.charge(candidate, read_bytes=reserve)
    directory_name = 'linux-input-' + operation_id
    try: os.mkdir(directory_name,0o700,dir_fd=journal.private.fd)
    except OSError: _fail('linux_input_fresh_directory_required')
    os.fsync(journal.private.fd)
    private = PrivateDirectory(journal.private.path / directory_name)
    handle,operation,permit = None,None,None
    try:
        chain = _chain(str(private.path))
        detail = {'profile':PROFILE, 'origin':state['linux_input_origin'],
            'workflow_id':workflow_id, 'plan_sha256':plan.sha256,
            'scope_sha256':digest(canonical(broker.manifest['scope'])),
            'planner_sha256':planner_snapshot['sha256'], 'fence_sha256':digest(canonical(record)),
            'prior_state_sha256':journal._state_hash, 'nonce':str(uuid.uuid4()), 'name':name,
            'timeout_seconds':suite['timeout_seconds'], 'max_output_bytes':suite['max_output_bytes'],
            'reserved_read_bytes':reserve, 'capture_passes':PASSES,
            'directory_identity':list(private.identity), 'directory_chain_sha256':digest(canonical(chain))}
        operation = {'id':operation_id, 'kind':'linux_input', 'phase':'intent',
            'request_hash':digest(canonical({'kind':'linux_input',**detail})), 'detail':detail,'receipt':None}
        journal.charge(state,read_bytes=reserve)
        state['operations'][operation_id] = operation
        journal.save(state)
        permit = _CapturePermit(_CAPTURE_FACTORY,broker,lock_lease,operation)
        with capture_input_snapshot_locked(broker,lock_lease=lock_lease,fence=fence,binding=binding,
                operation_id=workflow_id,planner_snapshot=planner_snapshot,authorize_reads=True,
                _capture_permit=permit) as snapshot:
            snapshot_document = snapshot.document()
            packet = encode_input_packet({path:snapshot.read(path) for path in broker.manifest['files']},
                manifest=broker.manifest,snapshot=snapshot_document,planner_snapshot=planner_snapshot)
            request = LinuxTestRequest(operation_id,workflow_id,journal.manifest_hash,plan.sha256,
                detail['scope_sha256'],digest(canonical(snapshot_document)),digest(packet),planner_snapshot['sha256'],
                broker.manifest['root'],tuple(broker.manifest['root_identity']),name,
                suite['timeout_seconds'],suite['max_output_bytes']).document()
            validate_linux_request(request,manifest=broker.manifest,snapshot=snapshot_document,
                planner_snapshot=planner_snapshot,expected_plan_sha256=plan.sha256,input_packet=packet)
            handle = _PreparedInputs(broker,lock_lease,plan,private,chain,snapshot,request,packet)
            yield handle
            handle._check()
            if handle._step != 2: _fail('linux_input_boundary_order')
        # The snapshot's final (fifth) capture succeeded before terminal metadata.
        if permit._count != PASSES: _fail('linux_input_capture_permit_invalid')
        handle._active = False
        plan.verify(); journal.require_lock_lease(lock_lease); handle._check_payload(); private.verify()
        if _chain(str(private.path)) != chain: _fail('linux_input_directory_changed')
        preparation = {'schema':PROFILE, 'operation_id':operation_id, 'origin':state['linux_input_origin'],
            'request_sha256':digest(canonical(request)), 'input_snapshot_sha256':request['input_snapshot_sha256'],
            'packet_sha256':digest(packet), 'boundaries':['before_source','after_result','context_exit'],
            'execution_supported':False,'adoptable':False}
        private.create('preparation.json',canonical(preparation))
        operation['phase'] = 'complete'
        operation['receipt'] = {'operation_id':operation_id,'kind':'linux_input','status':'complete',
            'result':'prepared_input','execution_supported':False,'adoptable':False,
            **{field:preparation[field] for field in ('request_sha256','input_snapshot_sha256','packet_sha256','boundaries')},
            'preparation_sha256':digest(canonical(preparation))}
        journal.save(state)
    except BaseException:
        # Never turn a failed/ambiguous write into a terminal claim. If the
        # durable state no longer matches, leave its intent for human review.
        if operation is not None:
            try: _review(journal,state,operation)
            except (DevelopmentError,OSError): pass
        raise
    finally:
        if handle is not None: handle._active = False
        if permit is not None: permit._active = False
        private.close()


def inspect_linux_input_attempt(broker, key):
    """Read-only bounded metadata; never reconciles or reconstructs a handle."""
    if type(broker) is not DevelopmentBroker: _fail()
    broker._authorize('repo.read_file')
    with broker.journal.lock() as state:
        if state['schema'] != 2: _fail('linux_input_fresh_journal_required')
        operation = state['operations'].get(broker._operation_id(key))
        if operation is None: return {'found':False, 'execution_supported':False, 'adoptable':False}
        return {'found':True, 'operation_id':operation['id'], 'phase':operation['phase'],
            'result':'prepared_input' if operation['phase'] == 'complete' else 'requires_review',
            'reserved_read_bytes':operation['detail']['reserved_read_bytes'],
            'execution_supported':False,'adoptable':False}
