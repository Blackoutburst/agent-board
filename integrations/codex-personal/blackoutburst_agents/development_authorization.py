"""Live read-only grant/context checks for one explicitly selected operation."""
from copy import deepcopy
import math
import time

from .context import ContextScope, build_context, _ticket_sources
from .dev_manifest import DevelopmentError, canonical, digest


class DevelopmentAuthorization:
    def __init__(self, client, *, scope, manifest, registration, clock=time.time, stop_requested=None, delegation_guard=None):
        self.client, self.scope, self.manifest = client, dict(scope), deepcopy(manifest)
        self.registration, self.clock = deepcopy(registration), clock
        self.stop_requested = stop_requested or (lambda: False)
        if delegation_guard is not None and not callable(delegation_guard):
            raise DevelopmentError('invalid_development_controller')
        self.delegation_guard = delegation_guard
        self.expected_context = None
        self.claim_id = None
        self.claim_version = None
        self._registration()

    def _registration(self):
        if self.stop_requested():
            raise DevelopmentError('development_stop_requested')
        value, scope = self.registration, self.scope
        try:
            profile, session, identity = value['profile'], value['session'], value['identity']
            expected = self.manifest['scope']
            if (self.client.endpoint != scope['board_endpoint'] or self.client.session_id != scope['board_session_id']
                    or any(expected[key] != scope[key] for key in ('owner_id','agent_id','project_id','ticket_id'))
                    or expected['session_id'] != scope['board_session_id']
                    or identity['id'] != scope['agent_id'] or profile['id'] != scope['agent_id']
                    or identity['owner_id'] != scope['owner_id'] or profile['owner_id'] != scope['owner_id']
                    or session['agent_id'] != scope['agent_id'] or session['owner_id'] != scope['owner_id']
                    or session['id'] != scope['board_session_id']
                    or session['device_id'] != expected['device_id'] or identity['device_id'] != expected['device_id']
                    or profile['status'] != 'active'
                    or any(type(item) is not int or item != expected['profile_version'] for item in
                        (value['profile_version'], profile['version'], session['profile_version']))):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise DevelopmentError('development_registration_mismatch') from None

    def _policy(self):
        try:
            value = self.client.call('get_work', {'project_id': self.scope['project_id'], 'limit': 1})
            policy = value['policy']
            if (any(policy[key] is not True for key in ('enabled','allow_read','allow_requests'))
                    or type(policy['actions']) is not list or type(policy['resources']) is not list
                    or not set(self.manifest['actions']).issubset(policy['actions'])
                    or self.manifest['resource'] not in policy['resources']):
                raise ValueError
            return deepcopy(policy)
        except Exception:
            raise DevelopmentError('development_grants_unavailable') from None

    def ticket(self):
        try:
            response = self.client.call('get_ticket', {'ticket_id': self.scope['ticket_id']})
            project, ticket = response['project'], response['ticket']
            if ticket.get('delegation') is not None or self.delegation_guard is not None:
                if self.delegation_guard is None or self.delegation_guard(response) is not True:
                    raise ValueError
            if (project['id'] != self.scope['project_id'] or ticket['id'] != self.scope['ticket_id']
                    or ticket['project_id'] != self.scope['project_id']
                    or self.scope['owner_id'] not in project['participants'] or project['archived'] is True
                    or type(ticket['version']) is not int or ticket['version'] < 1
                    or ticket['blocked_by'] or ticket['waiting_on_human']
                    or ticket['status'] not in ('todo','in_progress')
                    or ticket['assignee_owner_id'] not in (None, self.scope['owner_id'])
                    or ticket['assignee_agent_id'] not in (None, self.scope['agent_id'])):
                raise ValueError
            return response
        except Exception:
            raise DevelopmentError('development_ticket_unavailable') from None

    def own_claim(self, ticket, *, remaining=0):
        try:
            claimant = ticket['claimant']
            expected = {'kind':'agent', 'owner_id':self.scope['owner_id'],
                'agent_id':self.scope['agent_id'], 'device_id':self.manifest['scope']['device_id'],
                'session_id':self.scope['board_session_id']}
            lease = ticket['lease_expires_at']
            return (type(claimant) is dict and all(claimant.get(k) == v for k,v in expected.items())
                and type(lease) in (int,float) and math.isfinite(lease)
                and lease > self.clock() + remaining and ticket.get('claim_expired') is not True
                and ticket['status'] == 'in_progress')
        except (KeyError, TypeError):
            return False

    def claim_identity(self, ticket):
        if not self.own_claim(ticket):
            raise DevelopmentError('development_claim_lost')
        value = ticket['version'] if self.claim_version is None else self.claim_version
        if type(value) is not int or not 1 <= value <= ticket['version']:
            raise DevelopmentError('development_claim_invalid')
        # The sealed context pins the post-claim ticket revision. A fresh claim
        # increments that revision and cannot adopt a prior operation merely
        # by reusing this session identity. Renew only before the final seal.
        return 'claim-' + digest(canonical({'ticket_id':ticket['id'],
            'sealed_claim_version':value, 'claimant':ticket['claimant']}))

    def snapshot(self, *, require_claim=False, remaining=0):
        self._registration()
        policy = self._policy()
        first = self.ticket()
        if require_claim and not self.own_claim(first['ticket'], remaining=remaining):
            raise DevelopmentError('development_claim_lost')
        context = build_context(self.client.call,
            scope=ContextScope(**{key:self.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}),
            registration=self.registration)
        last = self.ticket()
        if (last['ticket']['version'] != first['ticket']['version']
                or last['ticket']['version'] != context['sources']['ticket']['version']):
            raise DevelopmentError('development_context_changed')
        if require_claim and not self.own_claim(last['ticket'], remaining=remaining):
            raise DevelopmentError('development_claim_lost')
        if self.expected_context is not None and context['fingerprint'] != self.expected_context['fingerprint']:
            raise DevelopmentError('development_context_changed')
        selected_project, selected_ticket = _ticket_sources(last, ContextScope(**{key:self.scope[key] for key in
            ('owner_id','agent_id','project_id','ticket_id')}))
        return {'policy':policy, 'context':context, 'ticket':last['ticket'],
                'selected_ticket':selected_ticket, 'selected_project':selected_project}

    def seal(self, snapshot):
        context, ticket = snapshot['context'], snapshot['ticket']
        if context['sources']['ticket']['version'] != ticket['version']:
            raise DevelopmentError('development_context_changed')
        if self.expected_context is not None and self.expected_context != context:
            raise DevelopmentError('development_context_already_sealed')
        self.expected_context = deepcopy(context)
        self.claim_version = ticket['version']
        self.claim_id = self.claim_identity(ticket)
        return self.claim_id

    def __call__(self):
        snapshot = self.snapshot(require_claim=True, remaining=1)
        claim = self.claim_identity(snapshot['ticket'])
        if self.claim_id is None or claim != self.claim_id:
            raise DevelopmentError('development_claim_changed')
        return {'scope':deepcopy(self.manifest['scope']), 'claim_id':claim,
            'owner_enabled':True, 'profile_enabled':True, 'project_allowed':True, 'claim_current':True,
            'actions':snapshot['policy']['actions'], 'resources':snapshot['policy']['resources']}
