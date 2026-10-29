"""Typed historical aggregate attachment; independent review never runs the author.

Only the real published producer can issue this read-only attachment. A frozen
projection is correlation, not permission: private source/Stop/fence, current
owner policy and the actual PR/head/destination are checked on each read. No
author SQLite connection is used by the review thread, and no archived result
can recreate publication or execution authority.
"""
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote
import os

from .development_aggregate_delivery import (AggregateDevelopmentDeliveryController,
    AggregateDevelopmentGitPublicationAuthority, _existing_record)
from .development_authorization import DevelopmentAuthorization
from .development_delivery import DevelopmentPublishedDelivery
from .development_review_handoff import DevelopmentReviewHandoff
from .development_pool_selection import file_document
from .dev_fence import DevelopmentFence, canonical_anchor
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest
from .dev_planner import _directory_pin
from .git_delivery import _inventory
from .git_publication import GitHubDraftPublisher, HOST, API, REMOTE
from .git_workspace import _operation

MAX_DOCUMENT = 8*1024*1024
_ISSUER = object()


def require(value, code='aggregate_review_source_changed'):
    if not value:
        raise DevelopmentError(code)


class PublishedAggregateReadView:
    """No publish API; only a fixed set of bounded read commands is admitted."""
    def __init_subclass__(cls, **kwargs):
        raise TypeError('Published read views cannot be subclassed')

    def __init__(self, source, publisher):
        require(type(source) is PublishedAggregateReviewSource and source._issuer is _ISSUER
            and type(publisher) is GitHubDraftPublisher and publisher is source.publisher,
            'aggregate_review_reader_required')
        self.source = source
        self.root, self.git, self.gh = publisher.root, publisher.git, publisher.gh
        self.python, self.guardian, self.timeout = publisher.python, publisher.guardian, publisher.timeout
        self.deliveries, self.workspaces = publisher.deliveries, publisher.workspaces
        self.grant, self._grant = deepcopy(publisher.grant), publisher._grant
        self.pins, self.authority_pin = deepcopy(publisher.pins), deepcopy(publisher.authority_pin)
        self.request = deepcopy(source.frame['publication_request'])

    def _roots(self):
        require(type(self) is PublishedAggregateReadView and self.source._issuer is _ISSUER)
        self.source._verify_frame()
        require(self.pins == self._pins() and self.grant == self._grant.document()
            and self.grant == self.source.grant
            and self.authority_pin == self.source.frame['authority_pin']
            and self.request == self.source.frame['publication_request'], 'aggregate_review_reader_changed')

    def _confirm_launch(self, argv, *, cwd, git):
        require(type(git) is bool, 'aggregate_review_command_refused')
        directory = self.root/self.source.operation_id
        query = API+'/pulls?state=all&per_page=100&head='+quote('Blackoutburst:'+self.request['branch'], safe='')+'&base='+quote(self.grant['base_branch'], safe='')
        allowed = ([str(self.git), 'ls-remote', '--refs', REMOTE,
            'refs/heads/'+self.grant['base_branch'], 'refs/heads/'+self.request['branch']] if git else None)
        if git:
            require(argv == allowed and Path(cwd) == directory/'transport', 'aggregate_review_command_refused')
        else:
            commands = [[str(self.gh), 'auth', 'status', '--hostname', HOST]] + [
                [str(self.gh), 'api', '--hostname', HOST, '--method', 'GET', path] for path in ('user', API, query)]
            require(argv in commands and Path(cwd) == directory, 'aggregate_review_command_refused')

    def _command(self, argv, *, cwd, git=False):
        self._roots()
        self._confirm_launch(argv, cwd=cwd, git=git)
        return self._run(argv, cwd=cwd, git=git)

    def _run(self, argv, *, cwd, git=False):
        return GitHubDraftPublisher._command(self, argv, cwd=cwd, git=git)

    def publish(self, *args, **kwargs):
        raise DevelopmentError('git_publication_readonly_view')

    def _authorize_effect(self, *args, **kwargs):
        raise DevelopmentError('git_publication_readonly_view')

    # Reuse the protected bounded runner and parsers, never Publisher's source
    # authority, mutation state machine, constructor or mutable effects.
    _pins = GitHubDraftPublisher._pins
    _tool = staticmethod(GitHubDraftPublisher._tool)
    _environment = GitHubDraftPublisher._environment
    _api = GitHubDraftPublisher._api
    _auth = GitHubDraftPublisher._auth
    _transport = GitHubDraftPublisher._transport
    _remote = GitHubDraftPublisher._remote
    _pr = GitHubDraftPublisher._pr
    _body = staticmethod(GitHubDraftPublisher._body)
    _lookup_pr = GitHubDraftPublisher._lookup_pr
    _read = GitHubDraftPublisher._read


class PublishedAggregateReviewSource:
    def __init__(self, aggregate, publisher):
        require(type(aggregate) is AggregateDevelopmentDeliveryController
            and type(publisher) is GitHubDraftPublisher and publisher.deliveries is aggregate.deliveries
            and type(publisher.authority) is AggregateDevelopmentGitPublicationAuthority
            and publisher.authority.controller is aggregate, 'aggregate_review_concrete_source_required')
        self.aggregate, self.publisher = aggregate, publisher
        self.operation_id = aggregate.operation_id
        self.workspaces, self.paths = aggregate.original_workspaces, dict(aggregate.paths)
        self.grant = deepcopy(publisher.grant)
        self.directory = aggregate.directory/'review-source'
        with PrivateDirectory(self.directory, create=True):
            pass
        self.pins = {'root': _directory_pin(self.directory), 'aggregate': aggregate.pin,
            'delivery_root': _directory_pin(self.paths['deliveries']), 'review_root': _directory_pin(self.paths['reviews'])}
        worker = aggregate.final_proof.worker
        verified = DevelopmentPublishedDelivery(aggregate.proof, aggregate.deliveries, publisher)
        published = verified.verify(worker, self.operation_id, aggregate.final_proof.scope)
        op = worker.journal.get(self.operation_id)
        require(op['stage'] == 'complete' and op['payload'].get('publication_confirmed') is True
            and op['payload']['publication']['delivery'] == published, 'aggregate_review_published_source_required')
        handoff = DevelopmentReviewHandoff(worker, verified).status(self.operation_id)
        require(handoff['status'] == 'complete', 'aggregate_review_handoff_required')
        with PrivateDirectory(aggregate.directory) as private:
            value = aggregate._read(private)
        require(value['phase'] == 'complete')
        aggregate._verified_seal(value)
        manifest, broker, planner, _, _ = aggregate.final_proof._documents(op)
        self.client = worker.client  # Transport only; never a Worker/SQLite operation in the reader.
        self.frame = self._capture_frame(aggregate, publisher, op, manifest, broker, planner, value)
        self.frame_sha256 = digest(canonical(self.frame))
        self.receipt = {'schema': 'published-aggregate-review-source-v1', 'operation_id': self.operation_id,
            'status': 'complete', 'delivery_sha': published['delivery_sha'], 'tree_sha': published['tree_sha'],
            'snapshot_sha256': published['snapshot_sha256'], 'original_public_base': aggregate.original_base,
            'pull_request': deepcopy(published['pull_request']), 'author_scope': deepcopy(op['scope']),
            'publication_request_sha256': published['publication_request_sha256'],
            'lineage_sha256': digest(canonical(aggregate._lineage())),
            'test_evidence_sha256': digest(canonical(value['seal']['test_evidence'])),
            'completion': deepcopy(value['completion']), 'handoff': deepcopy(handoff['result']), 'accepted': False}
        self._issuer = _ISSUER
        self.reader = PublishedAggregateReadView(self, publisher)
        self._verify_frame()
        raw = canonical({'receipt': self.receipt, 'frame_sha256': self.frame_sha256})
        require(len(raw) <= MAX_DOCUMENT)
        with PrivateDirectory(self.directory) as private:
            try:
                private.create('source.lock', b'')
            except DevelopmentError as error:
                if error.code != 'private_file_exists':
                    raise
                require(private.read('source.json', MAX_DOCUMENT) == raw, 'aggregate_review_attachment_uncertain')
                return
            private.create('source.json', raw)

    def _capture_frame(self, aggregate, publisher, op, manifest, broker, planner, value):
        worker = aggregate.final_proof.worker
        databases = [Path(worker.journal.directory)/'work-journal.sqlite3', Path(worker.store.state_dir)/'runs.sqlite3',
            Path(worker.runtime.directory)/'runtime.sqlite3', aggregate.task.journal.private.path/'continuation.sqlite3']
        files = [aggregate.directory/'aggregate.json', Path(worker.journal.directory)/('development-aggregate-binding-'+self.operation_id+'.json'),
            Path(worker.journal.directory)/('development-checkpoint-intent-'+self.operation_id+'.json'),
            Path(worker.journal.directory)/('review-handoff-'+self.operation_id+'.json')]
        for name in ('manifest.json', 'state.json', 'planner.json'):
            files.append(Path(op['configuration']['pins']['broker']['path'])/name)
        for name in ('execution-plan.json', 'backend-manifest.json'):
            files.append(Path(op['configuration']['pins']['backend']['path'])/name)
        for operation_id in broker['linux_execution']:
            root = Path(op['configuration']['pins']['backend']['path'])/('execution-'+operation_id)
            files.extend(root/name for name in ('job.json', 'native-receipt.json', 'state.json',
                'source-admission.json', 'source-window.json', 'source-window.ready'))
        files.append(Path(op['configuration']['pins']['planner_policy']['path'])/'planner-plan.json')
        final_root = Path(manifest['root'])
        files.extend(final_root/'.git'/name for name in ('HEAD', 'index', 'config'))
        final_files, _ = _inventory(final_root)
        final_snapshot = {path: {'mode': mode, 'sha256': digest(raw), 'bytes': len(raw)}
            for path, (mode, raw) in final_files.items()}
        data = {str(path): file_document(path, 32*1024*1024)[1] for path in databases+files}
        require(len(data) <= 1024)
        registration = op['payload']['registration']
        selected_registration = {key: deepcopy(registration[key]) for key in ('identity', 'session', 'profile', 'profile_version')}
        # Never retain profile instructions or source/model bodies in this projection.
        selected_registration['identity'] = {key: registration['identity'][key] for key in ('id', 'owner_id', 'device_id')}
        selected_registration['profile'] = {key: registration['profile'][key] for key in ('id', 'owner_id', 'status', 'version')}
        selected_registration['session'] = {key: registration['session'][key] for key in ('id', 'owner_id', 'agent_id', 'device_id', 'profile_version')}
        with PrivateDirectory(publisher.root/self.operation_id) as private:
            publication = publisher._read(private)
        with PrivateDirectory(aggregate.original_workspaces.state_root/self.operation_id) as private:
            workspace = aggregate.original_workspaces._read(private)
        with PrivateDirectory(aggregate.paths['deliveries']/self.operation_id) as private:
            delivery = aggregate.deliveries._read(private)
        return {'databases': [str(path) for path in databases], 'files': data, 'scope': deepcopy(op['scope']),
            'final_root': str(final_root), 'final_identity': manifest['root_identity'], 'final_snapshot': final_snapshot,
            'registration': selected_registration, 'floor': {key: deepcopy(manifest[key]) for key in ('scope', 'actions', 'resource')},
            'stop_paths': [str(Path(worker.journal.directory)/('development-stop-'+self.operation_id+'.json')),
                str(Path(op['configuration']['pins']['broker']['path'])/'planner-stop.json')],
            'fence_root': str(canonical_anchor() if worker.test_anchor is None else worker.test_anchor),
            'fence_key': DevelopmentFence.key(op['scope'], manifest['root_identity']),
            'fence_row': {'binding': {**deepcopy(op['payload']['parent_binding']),
                'configuration_hash': digest(canonical(planner['configuration']))}, 'operation_id': self.operation_id},
            'workspace': workspace, 'delivery': delivery, 'capture': deepcopy(value['capture']),
            'evidence': deepcopy(value['seal']['test_evidence']), 'publication_request': deepcopy(publication['request']),
            'publication_request_sha256': publication['request_sha256'], 'authority_pin': deepcopy(publisher.authority_pin)}

    def _verify_frame(self):
        require(self._issuer is _ISSUER and digest(canonical(self.frame)) == self.frame_sha256)
        require(self.paths == self.aggregate.paths and self.workspaces is self.aggregate.original_workspaces
            and self.grant == self.publisher.grant and self.pins == {'root': _directory_pin(self.directory),
                'aggregate': self.aggregate.pin, 'delivery_root': _directory_pin(self.paths['deliveries']),
                'review_root': _directory_pin(self.paths['reviews'])})
        for path in self.frame['databases']:
            for suffix in ('-wal', '-shm', '-journal'):
                require(not os.path.lexists(path+suffix), 'aggregate_review_writer_pending')
        for path, pin in self.frame['files'].items():
            require(file_document(path, 32*1024*1024)[1] == pin)
        for path in self.frame['stop_paths']:
            require(not os.path.lexists(path), 'aggregate_review_source_stopped')
        require(_directory_pin(self.frame['final_root'])['identity'] == self.frame['final_identity'])
        final_files, _ = _inventory(self.frame['final_root'])
        require({path: {'mode': mode, 'sha256': digest(raw), 'bytes': len(raw)}
            for path, (mode, raw) in final_files.items()} == self.frame['final_snapshot'])
        with _existing_record(Path(self.frame['fence_root']), 'fence.lock', shared=True) as private:
            state = DevelopmentFence._validate(object.__new__(DevelopmentFence), decode(private.read('fence.json', 2*1024*1024)))
            require(state['records'].get(self.frame['fence_key']) == self.frame['fence_row'], 'aggregate_review_fence_changed')
        self.workspaces._roots()
        with PrivateDirectory(self.workspaces.state_root/self.operation_id) as workspace, \
             PrivateDirectory(self.paths['deliveries']/self.operation_id) as private:
            require(self.workspaces._read(workspace) == self.frame['workspace'])
            self.workspaces._ready(self.frame['workspace'])
            stored = self.aggregate.deliveries._read(private)
            require(stored == self.frame['delivery'])
            self.aggregate.deliveries._validate(private, stored, self.frame['workspace'], Path(stored['request']['checkout']))
        authorization = DevelopmentAuthorization(self.client, scope=self.frame['scope'], manifest=self.frame['floor'],
            registration=self.frame['registration'], stop_requested=lambda: False)
        authorization._registration_identity()
        authorization._policy()

    def _published_observation(self):
        reader = self.reader
        reader._roots()
        with PrivateDirectory(reader.root/self.operation_id) as private:
            value = reader._read(private)
            require(value['phase'] == 'complete' and value['request'] == self.frame['publication_request']
                and value['request_sha256'] == self.frame['publication_request_sha256'])
            reader._auth(private.path, expected_login=value['login'])
            require(reader._remote(private, value) == value['request']['delivery_sha'])
            found = reader._lookup_pr(private, value)
            require(found == value['pr'] == self.receipt['pull_request'])
            reader._transport(private, value)
            require(reader._read(private) == value)
        reader._roots()
        return found

    def status(self, operation_id):
        require(_operation(operation_id) == self.operation_id)
        self._verify_frame()
        with _existing_record(self.directory, 'source.lock') as private:
            require(private.read('source.json', MAX_DOCUMENT) == canonical({'receipt': self.receipt, 'frame_sha256': self.frame_sha256}))
        self._published_observation()
        self._verify_frame()
        return deepcopy(self.receipt)

    def descriptor(self, operation_id):
        return {'kind': 'published-aggregate-review-source-v1', 'directory': str(self.directory), 'pins': deepcopy(self.pins),
            'local_grant': deepcopy(self.workspaces.grant), 'git_pins': deepcopy(self.workspaces.pins),
            'grant': deepcopy(self.grant), 'operation_id': operation_id, 'receipt': self.status(operation_id)}

    def verify_tests(self, *, operation_id, scope, snapshot, evidence, expected_receipt):
        require(self.status(operation_id) == expected_receipt and snapshot == self.frame['capture']
            and snapshot['request']['operation_id'] == operation_id and snapshot['request']['scope'] == scope
            and digest(canonical(snapshot)) == expected_receipt['snapshot_sha256'] and evidence == self.frame['evidence'])
        self._verify_frame()
        return True
