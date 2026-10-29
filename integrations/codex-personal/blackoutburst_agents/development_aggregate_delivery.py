"""Original-base deliveries from a real final tested development generation.

Private checkpoints are historical source lineage only. Tests and Runtime
evidence always come from the final ordinary DevelopmentDeliveryProof. Local
completion, publication and independent review are separate explicit steps.
"""
from contextlib import closing, contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import uuid

from .continuation import ContinuationTask
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, RepositoryFiles, canonical, digest, signature
from .dev_planner import _directory_pin
from .dev_runtime import development_work_id
from .development_delivery import DevelopmentDeliveryProof
from .development_git_authority import DevelopmentGitPublicationAuthority
from .git_delivery import LocalGitDeliveryController, _inventory, object_id
from .git_workspace import LocalGitWorkspaceController, MAX_CHECKOUT_FILE_BYTES, _operation, _ancestry
from .private_development_checkpoint import PrivateDevelopmentCheckpointController, checkpoint_id_for

MAX_DOCUMENT = 8*1024*1024


def require(value, code='development_aggregate_changed'):
    if not value:
        raise DevelopmentError(code)


def _copy(value):
    return decode(canonical(value))


def _file_map(snapshot):
    return {row['path']: {key: row[key] for key in ('mode', 'bytes', 'sha256', 'blob_oid')}
            for row in snapshot['files']}


@contextmanager
def _existing_record(path, lock, *, shared=False):
    """Read an existing receipt under its lock without repairing partial state."""
    with PrivateDirectory(path) as private:
        fd = os.open(lock, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=private.fd)
        try:
            private.validate_file(fd)
            before = signature(os.fstat(fd))
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)|fcntl.LOCK_NB)
            require(signature(os.stat(lock, dir_fd=private.fd, follow_symlinks=False)) == before)
            yield private
            private.verify()
            require(signature(os.fstat(fd)) == before
                and signature(os.stat(lock, dir_fd=private.fd, follow_symlinks=False)) == before)
        finally:
            os.close(fd)


class AggregateDevelopmentDeliveryProof:
    """A concrete two-workspace proof, never a converted private checkpoint."""
    def __init__(self, controller):
        require(type(controller) is AggregateDevelopmentDeliveryController)
        self.controller = controller
        self.worker = controller.final_proof.worker
        self.operation_id = controller.operation_id
        self.scope = deepcopy(controller.final_proof.scope)
        self.configuration = deepcopy(controller.final_proof.configuration)
        self.workspaces = controller.original_workspaces

    def evidence(self, snapshot):
        controller = self.controller
        controller._logical()
        lineage = controller._lineage()
        source = controller._source_capture()
        final = controller.final_proof.evidence(source)
        results = controller._model_results()
        require(type(snapshot) is dict and snapshot.get('schema') == 'local-captured-tree-v1')
        request = snapshot['request']
        require(request['operation_id'] == self.operation_id and request['scope'] == source['request']['scope']
            and request['base_commit'] == controller.original_base
            and _file_map(snapshot) == _file_map(source), 'development_aggregate_tree_changed')
        with PrivateDirectory(self.workspaces.state_root/self.operation_id) as private:
            workspace = self.workspaces._read(private)
        self.workspaces._ready(workspace)
        require(workspace['request']['checkout'] == request['checkout']
            and workspace['request_sha256'] == request['workspace_request_sha256'])
        actual, _ = _inventory(request['checkout'])
        require(set(actual) == set(_file_map(snapshot)), 'development_aggregate_tree_changed')
        for row in snapshot['files']:
            mode, raw = actual[row['path']]
            require(mode == row['mode'] and len(raw) == row['bytes'] and digest(raw) == row['sha256']
                and object_id(raw, 'blob', self.workspaces.grant['base_commit']) == row['blob_oid'],
                'development_aggregate_tree_changed')
        require(controller._lineage() == lineage and controller._source_capture() == source)
        require(controller.final_proof.evidence(source) == final)
        require(controller._model_results() == results)
        controller._logical()
        snapshot_sha = digest(canonical(snapshot))
        binding = {'schema': 'development-aggregate-tested-binding-v1',
            'operation_id': self.operation_id, 'original_public_base': controller.original_base,
            'lineage_sha256': digest(canonical(lineage)), 'final_evidence_sha256': digest(canonical(final)),
            'final_model_results_sha256': results,
            'final_snapshot_sha256': digest(canonical(source)), 'aggregate_snapshot_sha256': snapshot_sha}
        return {'schema': 'controller-test-evidence-v1', 'snapshot_sha256': snapshot_sha,
            'receipts': [{'name': row['name'], 'sha256': digest(canonical({**binding,
                'final_test_receipt_sha256': row['sha256']}))} for row in final['receipts']]}

    def __call__(self, *, operation_id, scope, snapshot, evidence):
        require(operation_id == self.operation_id and scope == snapshot['request']['scope'])
        require(evidence == self.evidence(snapshot))
        return True


class AggregateDevelopmentDeliveryController:
    """One final generation, one original-base capture, no model or daemon."""
    def __init__(self, directory, *, task, final_proof, checkpoints, parent_checkpoint_id, original_workspaces):
        require(type(task) is ContinuationTask and type(final_proof) is DevelopmentDeliveryProof
            and type(checkpoints) is PrivateDevelopmentCheckpointController
            and type(original_workspaces) is LocalGitWorkspaceController,
            'development_aggregate_concrete_handles_required')
        self.task, self.final_proof, self.checkpoints = task, final_proof, checkpoints
        self.operation_id = _operation(final_proof.operation_id)
        self.parent_checkpoint_id = _operation(parent_checkpoint_id)
        self.original_workspaces = original_workspaces
        self.final_authority = DevelopmentGitPublicationAuthority(final_proof.worker, self.operation_id)
        self.original_base = checkpoints.configuration['original_public_base']
        self.directory = Path(directory)
        with PrivateDirectory(self.directory, create=True):
            pass
        self.pin = _directory_pin(self.directory)
        chain = _ancestry(self.directory)
        forbidden = [Path(pin['path']) for pin in final_proof.configuration['pins'].values()]
        forbidden += [checkpoints.root, final_proof.workspaces.repository, final_proof.workspaces.state_root,
            final_proof.workspaces.workspace_root, original_workspaces.repository,
            original_workspaces.state_root, original_workspaces.workspace_root]
        for root in forbidden:
            other = _ancestry(root)
            require(chain[-1] not in other and other[-1] not in chain, 'development_aggregate_roots_overlap')
        self.paths = {name: self.directory/name for name in ('source-deliveries', 'source-reviews', 'deliveries', 'reviews')}
        for path in self.paths.values():
            with PrivateDirectory(path, create=True):
                pass
        config, grant = checkpoints.configuration, original_workspaces.grant
        require(grant['base_commit'] == self.original_base
            and all(grant[key] == config[key] for key in ('owner_id', 'repository_id')))
        self.source_deliveries = LocalGitDeliveryController(final_proof.workspaces,
            self.paths['source-deliveries'], self.paths['source-reviews'], test_guard=final_proof)
        self.proof = AggregateDevelopmentDeliveryProof(self)
        self.deliveries = LocalGitDeliveryController(original_workspaces,
            self.paths['deliveries'], self.paths['reviews'], test_guard=self.proof)
        self._logical_state()
        self._lineage()
        self._bind_controller()
        self._logical()

    def _controller_binding(self):
        return {'schema': 'development-aggregate-controller-binding-v1', 'operation_id': self.operation_id,
            'root': {'path': str(self.directory), 'pin': self.pin},
            'original_git': {'grant': self.original_workspaces.grant, 'pins': self.original_workspaces.pins,
                'state': _directory_pin(self.original_workspaces.state_root),
                'checkouts': _directory_pin(self.original_workspaces.workspace_root)},
            'final_git': {'grant': self.final_proof.workspaces.grant, 'pins': self.final_proof.workspaces.pins,
                'state': _directory_pin(self.final_proof.workspaces.state_root),
                'checkouts': _directory_pin(self.final_proof.workspaces.workspace_root)},
            'task': {'pin': _directory_pin(self.task.journal.private.path),
                'configuration_sha256': digest(canonical(self.task.configuration))},
            'checkpoints': {'root': str(self.checkpoints.root), 'pin': self.checkpoints.pin,
                'configuration_sha256': self.checkpoints.configuration_sha256},
            'parent_checkpoint_id': self.parent_checkpoint_id,
            'final_configuration_sha256': digest(canonical(self.final_proof.configuration))}

    def _bind_controller(self):
        # One final Worker's slot cannot be escaped with another delivery root.
        # This does not alter its row, context, work, results or counters.
        self._logical_state(fresh=True)
        op = self.final_proof._operation()
        manifest, _, _, _, _ = self.final_proof._documents(op)
        grant = deepcopy(self.final_proof.workspaces.grant)
        require(self.final_authority.before_effect(operation_id=self.operation_id, grant=grant,
            request={'operation_id': self.operation_id, 'grant': grant, 'scope': manifest['scope'],
                'authority': self.final_authority.descriptor()}) is True)
        raw = canonical(self._controller_binding())
        require(len(raw) <= 128*1024)
        with PrivateDirectory(self.final_proof.worker.journal.directory) as private:
            name = 'development-aggregate-binding-'+self.operation_id+'.json'
            try:
                private.create(name, raw)
            except DevelopmentError as error:
                if error.code != 'private_file_exists':
                    raise
                require(private.read(name, 128*1024) == raw, 'development_aggregate_controller_conflict')

    def _logical(self, *, fresh=False):
        with PrivateDirectory(self.final_proof.worker.journal.directory) as private:
            require(private.read('development-aggregate-binding-'+self.operation_id+'.json', 128*1024) ==
                canonical(self._controller_binding()), 'development_aggregate_controller_changed')
        return self._logical_state(fresh=fresh)

    def _logical_state(self, *, fresh=False):
        require(_directory_pin(self.directory) == self.pin)
        require(self.operation_id == self.final_proof.operation_id
            and self.paths == {name: self.directory/name for name in ('source-deliveries', 'source-reviews', 'deliveries', 'reviews')})
        task, proof = self.task, self.final_proof
        snapshot = task.snapshot()
        state, config = snapshot['state'], self.checkpoints.configuration
        require(state['stop_requested'] is False and state['phase'] in ('running', 'complete')
            and state['current_cycle'] is not None, 'development_aggregate_logical_stop')
        scope = task.configuration['scope']
        require(task.configuration['logical_task_id'] == config['task_id']
            and all(scope[key] == config[key] for key in ('owner_id', 'project_id', 'ticket_id', 'resource', 'source_release_sha256'))
            and all(scope[key] == proof.scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id', 'board_endpoint')))
        with PrivateDirectory(proof.worker.journal.directory) as private:
            intent = decode(private.read('development-checkpoint-intent-'+self.operation_id+'.json', 128*1024))
        cycle = state['current_cycle']
        require(type(intent) is dict and set(intent) == {'schema', 'operation_id', 'request', 'controller_sha256',
            'controller_root', 'controller_pin', 'parent_checkpoint_id', 'checkpoint_id'}
            and intent['schema'] == 'development-worker-checkpoint-intent-v1'
            and type(intent['request']) is dict and set(intent['request']) == {'task_id', 'generation_id', 'cycle_id', 'attempt_id',
                'original_public_base', 'source_release_sha256', 'parent_checkpoint_sha256', 'after_actions'}
            and intent['checkpoint_id'] == checkpoint_id_for(self.operation_id)
            and intent['operation_id'] == self.operation_id and intent['controller_sha256'] == self.checkpoints.configuration_sha256
            and intent['controller_root'] == str(self.checkpoints.root) and intent['controller_pin'] == self.checkpoints.pin
            and intent['parent_checkpoint_id'] == self.parent_checkpoint_id
            and all(intent['request'][key] == cycle[key] for key in ('generation_id', 'cycle_id', 'attempt_id'))
            and intent['request']['task_id'] == state['logical_task_id']
            and intent['request']['original_public_base'] == self.original_base
            and intent['request']['source_release_sha256'] == config['source_release_sha256'])
        op = proof._operation()
        require(scope['device_id'] == op['payload']['registration']['identity']['device_id']
            and scope['profile_version'] == op['payload']['registration']['profile_version'])
        proof.worker._local_control(self.operation_id)
        if fresh:
            task.checks.verify(task._context(snapshot, purpose='complete'))
            require(task.snapshot() == snapshot, 'development_aggregate_logical_stop')
            proof.worker._local_control(self.operation_id)
        return snapshot

    def _model_results(self):
        proof = self.final_proof
        work_id = development_work_id(self.operation_id)
        with proof._read_work(work_id):
            stored = proof.worker.store.status(work_id)
            require(0 < len(stored['turns']) <= 64, 'development_aggregate_result_limit')
            raw = canonical({'work': stored, 'results': {row['id']: proof.worker.store.turn_result(row['id']) for row in stored['turns']}})
            require(len(raw) <= 16*1024*1024, 'development_aggregate_result_limit')
            return digest(raw)

    def _lineage(self):
        cp = self.checkpoints.verify(self.parent_checkpoint_id)
        config = self.checkpoints.configuration
        require(cp['configuration_sha256'] == self.checkpoints.configuration_sha256
            and cp['original_public_base'] == self.original_base
            and cp['commit_sha'] == self.final_proof.workspaces.grant['base_commit']
            and cp['checkout'] == self.final_proof.workspaces.grant['repository']
            and self.final_proof.workspaces.grant['repository_id'] == config['repository_id'])
        with PrivateDirectory(self.final_proof.worker.journal.directory) as private:
            intent = decode(private.read('development-checkpoint-intent-'+self.operation_id+'.json', 128*1024))
        require(intent['request']['parent_checkpoint_sha256'] == cp['checkpoint_sha256'])
        op = self.final_proof._operation()
        manifest, _, planner, _, documents = self.final_proof._documents(op)
        require(sorted(manifest['files']) == config['global_paths'] and manifest['resource'] == config['resource'])
        from .development_cycle_driver import _test_caps
        tests = _test_caps(manifest, decode(documents['backend']))
        caps = {'paths': sorted(manifest['files']), 'tests': tests, 'actions': manifest['actions'],
            'resource': manifest['resource'], 'broker_limits': manifest['budgets'],
            'planner_limits': planner['configuration']['budgets']}
        require(digest(canonical(tests)) == config['global_test_caps_sha256']
            and digest(canonical(caps)) == config['static_caps_sha256'], 'development_aggregate_caps_changed')
        self._fence(op, manifest, planner)
        return cp

    def _fence(self, op, manifest, planner):
        from .dev_fence import DevelopmentFence, canonical_anchor
        anchor = canonical_anchor() if self.final_proof.worker.test_anchor is None else Path(self.final_proof.worker.test_anchor)
        with _existing_record(anchor, 'fence.lock', shared=True) as private:
            state = DevelopmentFence._validate(object.__new__(DevelopmentFence), decode(private.read('fence.json', 2*1024*1024)))
            expected = {**deepcopy(op['payload']['parent_binding']),
                'configuration_hash': digest(canonical(planner['configuration']))}
            require(state['records'].get(DevelopmentFence.key(op['scope'], manifest['root_identity'])) ==
                {'binding': expected, 'operation_id': self.operation_id}, 'development_aggregate_fence_changed')

    def _fresh(self):
        self._logical(fresh=True)
        op = self.final_proof._operation()
        manifest, _, _, _, _ = self.final_proof._documents(op)
        grant = deepcopy(self.final_proof.workspaces.grant)
        request = {'operation_id': self.operation_id, 'grant': grant, 'scope': manifest['scope'],
            'authority': self.final_authority.descriptor()}
        require(self.final_authority.before_effect(operation_id=self.operation_id, grant=grant, request=request) is True)
        self._logical()

    @contextmanager
    def _locked(self):
        with PrivateDirectory(self.directory) as private:
            if 'aggregate.json' in os.listdir(private.fd):
                fd = os.open('aggregate.lock', os.O_RDWR|os.O_NOFOLLOW, dir_fd=private.fd)
            else:
                try:
                    fd = os.open('aggregate.lock', os.O_RDWR|os.O_NOFOLLOW|os.O_CREAT|os.O_EXCL, 0o600, dir_fd=private.fd)
                    os.fsync(fd)
                    os.fsync(private.fd)
                except FileExistsError:
                    fd = os.open('aggregate.lock', os.O_RDWR|os.O_NOFOLLOW, dir_fd=private.fd)
            try:
                private.validate_file(fd)
                pin = signature(os.fstat(fd))
                fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                require(signature(os.stat('aggregate.lock', dir_fd=private.fd, follow_symlinks=False)) == pin)
                yield private
                private.verify()
                require(signature(os.fstat(fd)) == pin
                    and signature(os.stat('aggregate.lock', dir_fd=private.fd, follow_symlinks=False)) == pin)
            finally:
                os.close(fd)

    @staticmethod
    def _read(private):
        raw = private.read('aggregate.json', MAX_DOCUMENT)
        value = decode(raw)
        require(canonical(value) == raw and type(value) is dict
            and set(value) == {'schema', 'phase', 'request', 'capture', 'seal', 'completion_intent', 'completion'}
            and value['schema'] == 'development-aggregate-delivery-v1'
            and value['phase'] in ('preparing', 'sealed', 'completing', 'complete'))
        return value

    def _verified_seal(self, value):
        request = value['request']
        require(type(request) is dict and set(request) == {'operation_id', 'parent_checkpoint_sha256',
            'original_public_base', 'final_configuration_sha256', 'original_grant', 'message', 'task_configuration_sha256'}
            and request['operation_id'] == self.operation_id
            and request['parent_checkpoint_sha256'] == self._lineage()['checkpoint_sha256']
            and request['original_public_base'] == self.original_base
            and request['final_configuration_sha256'] == digest(canonical(self.final_proof.configuration))
            and request['original_grant'] == self.original_workspaces.grant
            and request['task_configuration_sha256'] == digest(canonical(self.task.configuration)))
        with _existing_record(self.original_workspaces.state_root/self.operation_id, 'workspace.lock') as workspace, \
             _existing_record(self.paths['deliveries']/self.operation_id, 'delivery.lock') as delivery:
            stored = self.deliveries._sealed(self.operation_id, workspace, delivery)
            require(stored['snapshot'] == value['capture'] and stored['seal'] == value['seal']
                and stored['seal']['message'] == request['message'])
        return _copy(stored['seal'])

    def _source_capture(self):
        self.source_deliveries._roots()
        with _existing_record(self.paths['source-deliveries']/self.operation_id, 'delivery.lock') as private:
            value = self.source_deliveries._read(private)
            require(value['phase'] == 'captured')
            for row in value['snapshot']['files']:
                raw = private.read(row['payload'], MAX_CHECKOUT_FILE_BYTES)
                require(len(raw) == row['bytes'] and digest(raw) == row['sha256'])
            return _copy(value['snapshot'])

    @staticmethod
    def _changes(workspaces, root, files):
        before = {}
        raw = workspaces._git(['ls-tree', '-r', '-z', '--full-tree', workspaces.grant['base_commit']], cwd=root)
        for entry in raw.rstrip(b'\0').split(b'\0'):
            head, path = entry.split(b'\t', 1)
            mode, kind, oid = head.split()
            require(kind == b'blob')
            before[path.decode()] = (mode.decode(), oid.decode())
        require(set(before) == set(files), 'development_aggregate_tree_changed')
        return [{'path': path, 'mode': mode, 'sha256': digest(data)}
            for path, (mode, data) in sorted(files.items())
            if (mode, object_id(data, 'blob', workspaces.grant['base_commit'])) != before[path]]

    def seal(self, *, message):
        self._fresh()
        parent = self._lineage()
        final = self.final_proof
        final_op = final._operation()
        manifest, _, _, _, _ = final._documents(final_op)
        files, _ = _inventory(manifest['root'])
        changes = self._changes(final.workspaces, manifest['root'], files)
        require(all(row['path'] in manifest['files'] for row in changes))
        request = {'operation_id': self.operation_id, 'parent_checkpoint_sha256': parent['checkpoint_sha256'],
            'original_public_base': self.original_base, 'final_configuration_sha256': digest(canonical(final.configuration)),
            'original_grant': deepcopy(self.original_workspaces.grant), 'message': message,
            'task_configuration_sha256': digest(canonical(self.task.configuration))}
        with self._locked() as private:
            if 'aggregate.json' in os.listdir(private.fd):
                value = self._read(private)
                require(value['request'] == request and value['phase'] in ('sealed', 'completing', 'complete'),
                    'development_aggregate_uncertain')
                self._verified_seal(value)
                return {**_copy(value['seal']), 'historical': True}
            value = {'schema': 'development-aggregate-delivery-v1', 'phase': 'preparing',
                'request': request, 'capture': None, 'seal': None, 'completion_intent': None, 'completion': None}
            private.create('aggregate.json', canonical(value))
            source = self.source_deliveries.capture(operation_id=self.operation_id, scope=manifest['scope'], changes=changes)
            final.evidence(source)
            self._fresh()
            self.original_workspaces.plan(operation_id=self.operation_id, scope=manifest['scope'])
            original = self.original_workspaces.materialize(self.operation_id)
            root = Path(original['checkout'])
            initial, _ = _inventory(root)
            target = _file_map(source)
            require(set(initial) == set(target), 'development_aggregate_tree_changed')
            selected = set(self.checkpoints.configuration['global_paths'])
            with closing(RepositoryFiles(root)) as repository, \
                 _existing_record(self.paths['source-deliveries']/self.operation_id, 'delivery.lock') as captured:
                for row in source['files']:
                    mode, before = initial[row['path']]
                    require(mode == row['mode'], 'development_aggregate_mode_changed')
                    raw = captured.read(row['payload'], MAX_CHECKOUT_FILE_BYTES)
                    require(digest(raw) == row['sha256'])
                    if raw == before:
                        continue
                    require(row['path'] in selected, 'development_aggregate_unselected_change')
                    final.evidence(source)
                    self._fresh()
                    with repository.parent(row['path']) as (directory, name):
                        current, _ = repository.read_at(directory, name, MAX_CHECKOUT_FILE_BYTES)
                        require(current == before)
                        temp = '.aggregate-'+str(uuid.uuid4())
                        fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o700 if mode == '100755' else 0o600, dir_fd=directory)
                        try:
                            with os.fdopen(fd, 'wb', closefd=False) as stream:
                                stream.write(raw)
                                stream.flush()
                                os.fsync(fd)
                        finally:
                            os.close(fd)
                        repository.verify_parent(row['path'], directory)
                        os.rename(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
                        os.fsync(directory)
                        repository.verify_parent(row['path'], directory)
            final.evidence(source)
            self._fresh()
            actual, _ = _inventory(root)
            require({path: (mode, digest(data)) for path, (mode, data) in actual.items()} ==
                {row['path']: (row['mode'], row['sha256']) for row in source['files']}, 'development_aggregate_tree_changed')
            aggregate_changes = self._changes(self.original_workspaces, root, actual)
            require(all(row['path'] in selected for row in aggregate_changes))
            snapshot = self.deliveries.capture(operation_id=self.operation_id, scope=manifest['scope'], changes=aggregate_changes)
            evidence = self.proof.evidence(snapshot)
            self._fresh()
            seal = self.deliveries.finalize(operation_id=self.operation_id,
                snapshot_sha256=evidence['snapshot_sha256'], test_evidence=evidence, message=message)
            self.proof.evidence(snapshot)
            self._fresh()
            value.update(phase='sealed', capture=snapshot, seal=seal)
            private.replace('aggregate.json', canonical(value))
            return {**_copy(seal), 'historical': False}

    def complete_local(self):
        """Seal actual final counters once; ticket/review status is untouched."""
        with self._locked() as private:
            value = self._read(private)
            require(value['phase'] in ('sealed', 'completing', 'complete'), 'development_aggregate_not_sealed')
            self._verified_seal(value)
            self.proof.evidence(value['capture'])
            self._fresh()
            snapshot = self._logical(fresh=True)
            op = self.final_proof._operation()
            _, broker, planner, _, _ = self.final_proof._documents(op)
            source = self._source_capture()
            source_sha = planner['review']['snapshot_sha256']
            checkpoint = {'schema': 1, 'source_sha256': source_sha,
                'diff_sha256': digest(canonical(value['seal'])),
                'plan_sha256': digest(canonical(planner['configuration'])),
                'test_receipts': [{'test_name': name, 'receipt_sha256': row['receipt_sha256'],
                    'source_sha256': source_sha, 'outcome': 'passed'} for name, row in sorted(planner['review']['tests'].items())],
                'provider_receipt': None}
            used = {**{key: planner['used'][key] for key in ('turns', 'actions')},
                **{key: broker['used'][key] for key in ('operations', 'tests', 'read_bytes', 'patch_bytes')}}
            request_id = 'development-aggregate-complete-'+self.operation_id
            if value['phase'] == 'complete':
                require(snapshot['state']['phase'] == 'complete'
                    and snapshot['state']['checkpoint'] == checkpoint
                    and snapshot['state']['current_cycle']['used'] == used
                    and snapshot['state']['checkpoint_sha256'] == value['completion']['checkpoint_sha256']
                    and snapshot['state_sha256'] == value['completion']['state_sha256'])
                return {**_copy(value['completion']), 'historical': True, 'launchable': False}
            if value['phase'] == 'sealed':
                require(snapshot['state']['phase'] == 'running', 'development_aggregate_completion_uncertain')
                intent = {'expected_revision': snapshot['state']['revision'], 'request_id': request_id,
                    'checkpoint': checkpoint, 'cycle_used': used}
                value.update(phase='completing', completion_intent=intent)
                private.replace('aggregate.json', canonical(value))
            intent = value['completion_intent']
            require(type(intent) is dict and set(intent) == {'expected_revision', 'request_id', 'checkpoint', 'cycle_used'}
                and intent['request_id'] == request_id and intent['checkpoint'] == checkpoint and intent['cycle_used'] == used)
            revision = intent['expected_revision']
            payload = {'kind': 'checkpoint', 'expected_revision': revision, 'checkpoint': checkpoint,
                'cycle_used': used, 'reason': 'complete'}
            event = self.task._lookup(revision, request_id, payload)
            if event is None:
                require(snapshot['state']['phase'] == 'running' and snapshot['state']['revision'] == revision,
                    'development_aggregate_completion_uncertain')
                boundary = self.task.clean_boundary(expected_revision=revision, checkpoint=checkpoint, cycle_used=used, reason='complete')
                self.proof.evidence(value['capture'])
                require(self._source_capture() == source)
                event = self.task.checkpoint(expected_revision=revision, request_id=request_id,
                    checkpoint=checkpoint, cycle_used=used, reason='complete', boundary=boundary)
            require(event['snapshot']['state']['phase'] == 'complete', 'development_aggregate_completion_refused')
            self.proof.evidence(value['capture'])
            self._logical()
            completion = {'operation_id': self.operation_id, 'checkpoint_sha256': event['snapshot']['state']['checkpoint_sha256'],
                'state_sha256': event['snapshot']['state_sha256'], 'event_receipt': event['receipt']}
            value.update(phase='complete', completion=completion)
            private.replace('aggregate.json', canonical(value))
            return {**_copy(completion), 'historical': False, 'launchable': False}

    def publication_authority(self, grant):
        return AggregateDevelopmentGitPublicationAuthority(self, grant)

    def publish_and_handoff(self, publisher, *, title, body):
        """Existing durable PR, Board review CAS and author-unassignment paths."""
        from .git_publication import GitHubDraftPublisher
        from .development_delivery import DevelopmentPublishedDelivery
        from .development_review_handoff import DevelopmentReviewHandoff
        require(type(publisher) is GitHubDraftPublisher and publisher.deliveries is self.deliveries
            and type(publisher.authority) is AggregateDevelopmentGitPublicationAuthority
            and publisher.authority.controller is self, 'development_aggregate_publisher_required')
        publisher.authority.descriptor()
        with self._locked() as private:
            value = self._read(private)
            require(value['phase'] == 'complete', 'development_aggregate_completion_required')
            seal = self._verified_seal(value)
        published = publisher.publish(operation_id=self.operation_id, delivery_sha=seal['delivery_sha'], title=title, body=body)
        require(published['status'] == 'complete')
        verified = DevelopmentPublishedDelivery(self.proof, self.deliveries, publisher)
        worker = self.final_proof.worker
        board = worker.publish(self.operation_id, delivery_publication=verified)
        require(board['stage'] == 'complete', 'development_aggregate_board_publication_unconfirmed')
        handoff = DevelopmentReviewHandoff(worker, verified).release(self.operation_id)
        require(handoff['status'] == 'complete', 'development_aggregate_handoff_unconfirmed')
        return {'operation_id': self.operation_id, 'status': 'awaiting_independent_review',
            'publication': published, 'handoff': handoff, 'accepted': False}


class AggregateDevelopmentGitPublicationAuthority:
    """Exact original-base wrapper around the real final Worker's authority."""
    def __init__(self, controller, grant):
        from .git_publication import GitHubPublicationGrant
        require(type(controller) is AggregateDevelopmentDeliveryController and type(grant) is GitHubPublicationGrant,
            'development_aggregate_authority_required')
        self.controller, self.worker = controller, controller.final_proof.worker
        self.operation_id = controller.operation_id
        self.grant = grant.document()
        require(self.grant['owner_id'] == controller.checkpoints.configuration['owner_id']
            and self.grant['base_commit'] == controller.original_base, 'development_aggregate_public_base_changed')
        self.pin = self._descriptor()

    def _descriptor(self):
        controller = self.controller
        require(type(controller) is AggregateDevelopmentDeliveryController)
        snapshot = controller._logical()
        with PrivateDirectory(controller.directory) as private:
            value = controller._read(private)
        require(value['phase'] == 'complete' and snapshot['state']['phase'] == 'complete'
            and snapshot['state_sha256'] == value['completion']['state_sha256']
            and snapshot['state']['checkpoint_sha256'] == value['completion']['checkpoint_sha256'],
            'development_aggregate_completion_required')
        return {'schema': 'development-aggregate-git-publication-authority-v1', 'operation_id': self.operation_id,
            'original_public_base': controller.original_base, 'grant': deepcopy(self.grant),
            'final_authority': controller.final_authority.descriptor(),
            'lineage_sha256': digest(canonical(controller._lineage())),
            'seal_sha256': digest(canonical(value['seal'])), 'completion': deepcopy(value['completion'])}

    def descriptor(self):
        value = self._descriptor()
        require(value == self.pin, 'development_aggregate_authority_changed')
        return deepcopy(value)

    def _request(self, operation_id, grant, request):
        require(operation_id == self.operation_id and grant == self.grant and type(request) is dict
            and request.get('operation_id') == operation_id and request.get('grant') == grant
            and request.get('authority') == self.descriptor()
            and request.get('scope') == self.controller._source_capture()['request']['scope'],
            'development_aggregate_authority_changed')
        translated = {**deepcopy(grant), 'base_commit': self.controller.final_proof.workspaces.grant['base_commit']}
        inner = {**deepcopy(request), 'grant': translated, 'authority': self.controller.final_authority.descriptor()}
        return translated, inner

    def before_effect(self, *, operation_id, grant, request):
        translated, inner = self._request(operation_id, grant, request)
        self.controller._logical(fresh=True)
        require(self.controller.final_authority.before_effect(operation_id=operation_id,
            grant=translated, request=inner) is True)
        self.descriptor()
        return True

    def confirm_local(self, *, operation_id, grant, request):
        translated, inner = self._request(operation_id, grant, request)
        require(self.controller.final_authority.confirm_local(operation_id=operation_id,
            grant=translated, request=inner) is True)
        self.descriptor()
        self.controller._logical()
        return True
