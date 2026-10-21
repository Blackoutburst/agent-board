"""Explicit author self-unassignment after an exact published delivery.

This does not select/assign a reviewer, publish a message or perform another
ticket publication. The existing Board release CAS remains the authority.
"""
from copy import deepcopy
import os

from .context import ContextScope, _hash, _select, _TICKET, _ticket_sources, build_context
from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, digest
from .dev_planner import _directory_pin
from .development_delivery import DevelopmentPublishedDelivery
from .development_worker import DevelopmentBoardWorker
from .git_workspace import _operation


def fail(code='development_review_handoff_changed'):
    raise DevelopmentError(code)


class DevelopmentReviewHandoff:
    """One explicit release operation, protected by the worker's job lock.

    The private intent precedes the only RPC. After a lost response a matching
    post-state permits only the SAME idempotent RPC, never local adoption.
    Completed receipts are historical; they do not inspect a later reviewer.
    """
    def __init__(self, worker, published_delivery):
        if (type(worker) is not DevelopmentBoardWorker
                or type(published_delivery) is not DevelopmentPublishedDelivery
                or published_delivery.proof.worker is not worker):
            fail('development_review_handoff_controller_required')
        self.worker, self.delivery = worker, published_delivery

    def _operation(self, operation_id):
        _operation(operation_id)
        op = self.worker.journal.get(operation_id); self.worker._pin_controller(op)
        publication = op['payload'].get('publication')
        if (operation_id != self.delivery.proof.operation_id or op['stage'] != 'complete'
                or op['scope'] != self.delivery.proof.scope or op['configuration'] != self.delivery.proof.configuration
                or _directory_pin(self.worker.journal.directory) != op['configuration']['pins']['journal']
                or op['payload'].get('publication_confirmed') is not True or type(publication) is not dict
                or publication.get('delivery') is None or type(publication.get('expected_version')) is not int
                or not 1 <= publication['expected_version'] < 2**63-2
                or set(publication.get('changes',{})) != {'status','outcome','evidence','release_claim'}
                or publication['changes']['status'] != 'review' or publication['changes']['release_claim'] is not True):
            fail('development_review_handoff_publication_required')
        return op

    @staticmethod
    def _name(operation_id):
        return 'review-handoff-'+operation_id+'.json'

    def _request(self, op):
        scope, payload = op['scope'], op['payload']; publication = payload['publication']
        expected = _select(payload['claimed_ticket'],_TICKET)
        if (expected.get('assignee_agent_id') != scope['agent_id']
                or expected.get('assignee_owner_id') not in (None,scope['owner_id'])):
            fail('development_review_handoff_author_assignment_required')
        expected.update(version=publication['expected_version']+1,
            **{key:publication['changes'][key] for key in ('status','outcome','evidence')})
        sources = deepcopy(payload['context']['sources'])
        sources['ticket'] = dict(id=scope['ticket_id'],version=expected['version'],sha256=_hash(expected))
        rpc = dict(ticket_id=scope['ticket_id'],expected_version=expected['version'],
            idempotency_key='development-review-handoff-'+op['id'],
            reason='Published delivery '+publication['delivery']['delivery_sha']+' ready for an independent reviewer.',
            status='review',unassign_self=True)
        return dict(operation_id=op['id'],scope=deepcopy(scope),operation_sha256=digest(canonical(op)),
            publication=deepcopy(publication['delivery']),context_sources=sources,
            expected_ticket_sha256=_hash(expected),rpc=rpc)

    def _read(self, private, op, request):
        name = self._name(op['id'])
        try: os.stat(name,dir_fd=private.fd,follow_symlinks=False)
        except FileNotFoundError: return None
        value = decode(private.read(name,128*1024))
        if (type(value) is not dict or set(value) != {'schema','phase','request','request_sha256','result'}
                or value['schema'] != 'development-review-handoff-v1'
                or value['phase'] not in ('releasing','complete') or value['request'] != request
                or value['request_sha256'] != digest(canonical(request))
                or value['result'] != (self._result(request) if value['phase']=='complete' else None)):
            fail('development_review_handoff_intent_changed')
        return value

    @staticmethod
    def _result(request):
        return dict(ticket_id=request['rpc']['ticket_id'],version=request['rpc']['expected_version']+1,
            status='review',assignee_agent_id=None)

    @staticmethod
    def _public(value):
        return dict(operation_id=value['request']['operation_id'],status=value['phase'],
            result=deepcopy(value['result']))

    def _local(self, op, request):
        if self._operation(op['id']) != op or self._request(op) != request: fail()
        self.worker._local_control(op['id'])
        registration = op['payload']['registration']; scope = op['scope']
        if (self.worker.client.endpoint != scope['board_endpoint']
                or self.worker.client.session_id != scope['board_session_id']
                or registration['session']['id'] != scope['board_session_id']): fail()

    def _policy(self, op):
        policy = self.worker.client.call('get_work',dict(project_id=op['scope']['project_id'],limit=1))['policy']
        # This is waiting-state assignment maintenance, not code execution.
        if any(policy.get(key) is not True for key in ('enabled','allow_read','allow_requests')):
            fail('development_review_handoff_permission_denied')

    def _ticket(self, response, op, request, *, allow_post):
        scope = ContextScope(**{key:op['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id')})
        project, selected = _ticket_sources(response,scope); ticket = response['ticket']
        if (project.get('archived') is not False or ticket['claimant'] is not None
                or ticket['lease_expires_at'] is not None or ticket.get('blocked_by')
                or ticket.get('waiting_on_human') is not False or selected.get('status') != 'review'
                or selected.get('assignee_owner_id') not in (None,scope.owner_id)):
            fail()
        post = selected.get('version') == request['rpc']['expected_version']+1 and selected.get('assignee_agent_id') is None
        if post:
            if not allow_post: fail()
            selected.update(version=request['rpc']['expected_version'],assignee_agent_id=scope.agent_id)
        elif (selected.get('version') != request['rpc']['expected_version']
                or selected.get('assignee_agent_id') != scope.agent_id): fail()
        if _hash(selected) != request['expected_ticket_sha256']: fail()
        if _hash(project) != request['context_sources']['project']['sha256']: fail()
        return post

    def _fresh(self, op, request, *, allow_post):
        self._local(op,request); self._policy(op)
        observed = []
        def call(name,args):
            value = self.worker.client.call(name,args)
            if name == 'get_ticket': observed.append(deepcopy(value))
            return value
        scope = ContextScope(**{key:op['scope'][key] for key in ('owner_id','agent_id','project_id','ticket_id')})
        context = build_context(call,scope=scope,registration=op['payload']['registration'])
        if len(observed) != 2: fail()
        states = [self._ticket(row,op,request,allow_post=allow_post) for row in observed]
        if states[0] != states[1]: fail()
        sources = deepcopy(context['sources'])
        # All sources must match the sealed worker context except its known
        # publication transition and, on replay only, this self-unassignment.
        sources['ticket'] = request['context_sources']['ticket']
        if sources != request['context_sources']: fail('development_review_handoff_context_changed')
        self._policy(op); self._local(op,request)
        return states[0]

    def release(self, operation_id):
        try: return self._release(operation_id)
        except DevelopmentError: raise
        except Exception: fail('development_review_handoff_unavailable')

    def _release(self, operation_id):
        op = self._operation(operation_id)
        with self.worker.journal.lock(op['scope']), PrivateDirectory(self.worker.journal.directory) as private:
            op = self._operation(operation_id); request = self._request(op)
            value = self._read(private,op,request)
            if value is not None and value['phase'] == 'complete': return self._public(value)
            self.worker.client.adopt_registration()
            if self.delivery.verify(self.worker,operation_id,op['scope']) != request['publication']: fail()
            # Authority/context observations follow every GitHub lookup.
            self._fresh(op,request,allow_post=value is not None)
            if value is None:
                value = dict(schema='development-review-handoff-v1',phase='releasing',request=request,
                    request_sha256=digest(canonical(request)),result=None)
                private.create(self._name(operation_id),canonical(value))
            if self._read(private,op,request) != value: fail('development_review_handoff_intent_changed')
            self._local(op,request)
            try:
                response = self.worker.client.call('release_ticket',deepcopy(request['rpc']))
            except Exception:
                fail('development_review_handoff_unconfirmed')
            # A matching GET alone is insufficient: the exact RPC must have
            # returned successfully, including after an earlier lost response.
            if not self._ticket(dict(project=self._project(op),ticket=response),op,request,allow_post=True): fail()
            if not self._fresh(op,request,allow_post=True): fail()
            if self._read(private,op,request) != value: fail('development_review_handoff_intent_changed')
            value = {**value,'phase':'complete','result':self._result(request)}
            private.replace(self._name(operation_id),canonical(value))
            return self._public(value)

    def _project(self, op):
        # Only the RPC response's ticket is reused; observe its project freshly.
        return self.worker.client.call('get_ticket',dict(ticket_id=op['scope']['ticket_id']))['project']

    def status(self, operation_id):
        op = self._operation(operation_id)
        with self.worker.journal.lock(op['scope']), PrivateDirectory(self.worker.journal.directory) as private:
            op = self._operation(operation_id); value = self._read(private,op,self._request(op))
            return dict(operation_id=operation_id,status='not_started',result=None) if value is None else self._public(value)
