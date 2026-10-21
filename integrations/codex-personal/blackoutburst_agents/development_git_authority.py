"""Concrete fresh Board authority for a standalone development Git effect.

This reads the existing sealed worker/manifest and calls its actual Board
authorization. It never registers, executes, qualifies, renews or publishes.
"""
from copy import deepcopy

from .development_authorization import DevelopmentAuthorization
from .development_worker import DevelopmentBoardWorker
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest, validate_manifest
from .dev_planner import _directory_pin
from .git_workspace import _operation


def fail(code='development_git_authority_changed'):
    raise DevelopmentError(code)


class DevelopmentGitPublicationAuthority:
    """A real selected worker's immutable pins plus a fresh effect check.

    descriptor() is local and remains readable after Board publication. Only
    before_effect() demands the original live execution claim and code grants.
    """
    def __init__(self, worker, operation_id):
        if type(worker) is not DevelopmentBoardWorker or worker.controller_scope is not None:
            fail('development_git_authority_controller_required')
        self.worker, self.operation_id = worker, _operation(operation_id)
        op, manifest = self._read()
        self.pin = self._descriptor(op,manifest)

    def _read(self):
        op = self.worker.journal.get(self.operation_id); self.worker._pin_controller(op)
        config, payload = op['configuration'], op['payload']
        if (op['stage'] not in ('result_ready','publishing','complete')
                or config.get('controller_scope') is not None
                or config.get('execution_profile') != 'linux-python-execution-v1'
                or config.get('pins',{}).get('journal') != _directory_pin(self.worker.journal.directory)
                or type(payload.get('context')) is not dict or type(payload.get('claim_id')) is not str
                or type(payload.get('claim_version')) is not int): fail()
        with PrivateDirectory(config['pins']['broker']['path']) as private:
            manifest = validate_manifest(decode(private.read('manifest.json',128*1024)))
        scope = op['scope']
        if (digest(canonical(manifest)) != config['manifest_hash']
                or any(manifest['scope'][key] != scope[key] for key in ('owner_id','agent_id','project_id','ticket_id'))
                or manifest['scope']['session_id'] != scope['board_session_id']
                or self.worker.client.endpoint != scope['board_endpoint']
                or self.worker.client.session_id != scope['board_session_id']): fail()
        return op,manifest

    @staticmethod
    def _descriptor(op, manifest):
        payload = op['payload']
        return dict(schema='development-git-publication-authority-v1',operation_id=op['id'],
            scope=deepcopy(op['scope']),journal=deepcopy(op['configuration']['pins']['journal']),
            configuration_sha256=digest(canonical(op['configuration'])),
            registration_sha256=digest(canonical(payload['registration'])),
            context_sha256=digest(canonical(payload['context'])),claim_id=payload['claim_id'],
            claim_version=payload['claim_version'],manifest_sha256=digest(canonical(manifest)),
            base_commit=manifest['base_sha'],root=manifest['root'],root_identity=manifest['root_identity'])

    def descriptor(self):
        op, manifest = self._read(); value = self._descriptor(op,manifest)
        if value != self.pin: fail()
        return deepcopy(value)

    def before_effect(self, *, operation_id, grant, request):
        try:
            op, manifest = self._read()
            if (operation_id != self.operation_id or self._descriptor(op,manifest) != self.pin
                    or type(grant) is not dict or grant.get('owner_id') != op['scope']['owner_id']
                    or grant.get('base_commit') != manifest['base_sha']
                    or type(request) is not dict or request.get('operation_id') != operation_id
                    or request.get('grant') != grant or request.get('scope') != manifest['scope']
                    or request.get('authority') != self.pin): fail()
            self.worker._local_control(operation_id)
            self.worker.client.adopt_registration()
            authorization = DevelopmentAuthorization(self.worker.client,scope=op['scope'],manifest=manifest,
                registration=op['payload']['registration'],clock=self.worker.clock,
                stop_requested=lambda:self.worker._local_cancelled(operation_id))
            authorization.expected_context = deepcopy(op['payload']['context'])
            authorization.claim_id,authorization.claim_version = op['payload']['claim_id'],op['payload']['claim_version']
            authorization()  # Real policy + exact context + live original claim.
            # Authorization's snapshot reads policy first. Observe it again
            # after its context/claim reads, before an independent Git effect.
            authorization._policy()
            self.worker._local_control(operation_id)
            if self.descriptor() != self.pin: fail()
            return True
        except DevelopmentError: raise
        except Exception: fail('development_git_authority_unavailable')

    def confirm_local(self, *, operation_id, grant, request):
        """Final local bracket after every source/transport/intent read.

        No Board request follows the previously fresh authorization. The final
        observation here is the real local Stop marker, after pin/config reads.
        """
        try:
            op,manifest = self._read()
            if (operation_id != self.operation_id or self._descriptor(op,manifest) != self.pin
                    or type(grant) is not dict or grant.get('owner_id') != op['scope']['owner_id']
                    or grant.get('base_commit') != manifest['base_sha']
                    or type(request) is not dict or request.get('operation_id') != operation_id
                    or request.get('grant') != grant or request.get('scope') != manifest['scope']
                    or request.get('authority') != self.pin): fail()
            self.worker._local_control(operation_id)
            return True
        except DevelopmentError: raise
        except Exception: fail('development_git_authority_unavailable')
