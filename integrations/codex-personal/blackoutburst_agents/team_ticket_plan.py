"""Publish an approved, bounded DAG as ordinary tickets through a scoped Lead.

No model, task execution, profile management, claim or ticket update occurs.
The private intent precedes creation. Exact Board idempotency keys reconcile a
lost creation response; confirmed receipts are read without rewriting work.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import DevelopmentError, canonical, check_content, digest, identity, opaque, sha256


MAX_DOCUMENT_BYTES = 128 * 1024
PLAN_FIELDS = {'schema', 'owner_id', 'project_id', 'parent_ticket_id',
               'project_version', 'parent_version', 'steps'}
STEP_FIELDS = {'step_id', 'slot_id', 'title', 'description',
               'acceptance_criteria', 'depends_on', 'milestone'}
SLOT_FIELDS = {'slot_id', 'agent_id', 'profile_version', 'resource'}
RECEIPT_FIELDS = {'step_id', 'ticket_id', 'number', 'thread_id',
                  'created_version', 'payload_sha256'}
PARENT_FIELDS = ('id', 'project_id', 'version', 'title', 'description', 'acceptance_criteria',
                 'status', 'assignee_owner_id', 'assignee_agent_id', 'resource', 'depends_on',
                 'labels', 'required_skills', 'outcome', 'evidence', 'attachments')
PROFILE_FIELDS = ('id', 'owner_id', 'status', 'version', 'project_ids', 'policy', 'legacy',
                  'name', 'role', 'description', 'instructions', 'language', 'response_style',
                  'preferred_model', 'preferred_provider', 'avatar')


def fail(code='team_ticket_plan_invalid'):
    raise DevelopmentError(code)


def version(value):
    if type(value) is not int or not 1 <= value < 2 ** 63:
        fail()
    return value


def prose(value, maximum, *, empty=False):
    if (type(value) is not str or len(value.encode('utf-8', 'surrogatepass')) > maximum
            or not empty and not value.strip()
            or value != value.strip()
            or any(ord(char) < 32 and char not in '\n\t' or ord(char) == 127 for char in value)):
        fail()
    check_content(value.encode('utf-8', 'strict'))
    return value


def documents(plan, catalog):
    """Closed data only. Reusing a slot/profile/resource grants no execution."""
    if (len(canonical(plan)) > MAX_DOCUMENT_BYTES or len(canonical(catalog)) > MAX_DOCUMENT_BYTES
            or type(plan) is not dict or set(plan) != PLAN_FIELDS
            or plan['schema'] != 'team-ticket-plan-v1'
            or type(catalog) is not dict or set(catalog) != {'schema', 'owner_id', 'slots'}
            or catalog['schema'] != 'team-ticket-catalog-v1'
            or plan['owner_id'] != catalog['owner_id']):
        fail()
    for field in ('owner_id', 'project_id', 'parent_ticket_id'):
        opaque(plan[field])
    if len('team-parent:' + plan['parent_ticket_id']) > 200:
        fail()
    version(plan['project_version']); version(plan['parent_version'])
    if type(catalog['slots']) is not list or not 1 <= len(catalog['slots']) <= 16:
        fail()
    slots = {}
    for slot in catalog['slots']:
        if type(slot) is not dict or set(slot) != SLOT_FIELDS:
            fail()
        for field in ('slot_id', 'agent_id', 'resource'):
            opaque(slot[field])
        version(slot['profile_version'])
        if (slot['slot_id'] in slots or '*' in slot['resource']
                or slot['resource'].startswith(('agent-profiles:', 'runtime:', 'server:',
                                              'provider:', 'device:', 'credential:', 'policy:'))):
            fail()
        slots[slot['slot_id']] = slot
    if type(plan['steps']) is not list or not 1 <= len(plan['steps']) <= 16:
        fail()
    steps = {}
    for step in plan['steps']:
        if type(step) is not dict or set(step) != STEP_FIELDS:
            fail()
        opaque(step['step_id']); opaque(step['slot_id'])
        if (step['step_id'] in steps or step['slot_id'] not in slots
                or len('team-step:' + step['step_id']) > 200):
            fail()
        prose(step['title'], 200); prose(step['description'], 4000, empty=True)
        prose(step['acceptance_criteria'], 4000); prose(step['milestone'], 80, empty=True)
        if '\n' in step['title'] or '\t' in step['title'] or any(c in step['milestone'] for c in '\n\t'):
            fail()
        dependencies = step['depends_on']
        if (type(dependencies) is not list or len(dependencies) > 15
                or any(type(item) is not str for item in dependencies)
                or len(set(dependencies)) != len(dependencies)):
            fail()
        for dependency in dependencies:
            opaque(dependency)
        steps[step['step_id']] = step
    pending = {key: set(step['depends_on']) for key, step in steps.items()}
    if any(not deps <= steps.keys() for deps in pending.values()):
        fail()
    order = []
    while pending:
        ready = sorted(key for key, deps in pending.items() if not deps)
        if not ready:
            fail('team_ticket_plan_cycle')
        order.extend(ready)
        pending = {key: deps - set(ready) for key, deps in pending.items() if key not in ready}
    normalized_plan = deepcopy(plan)
    normalized_plan['steps'] = [{**steps[key], 'depends_on': sorted(steps[key]['depends_on'])}
                                for key in sorted(steps)]
    normalized_catalog = {**deepcopy(catalog), 'slots': [deepcopy(slots[key]) for key in sorted(slots)]}
    return normalized_plan, normalized_catalog, order


def _project(value, plan):
    fields = ('id', 'version', 'name', 'description', 'context', 'repository_url',
              'environment', 'participants', 'archived')
    if (type(value) is not dict or any(key not in value for key in fields)
            or value['id'] != plan['project_id'] or value['version'] != plan['project_version']
            or value['archived'] is not False or type(value['participants']) is not list
            or plan['owner_id'] not in value['participants']):
        fail('team_ticket_project_changed')
    version(value['version'])
    return {key: deepcopy(value[key]) for key in fields}


def _profile_context(value):
    return {key: deepcopy(value[key]) for key in PROFILE_FIELDS if key in value}


class TeamTicketPlanController:
    def __init__(self, client, journal_directory=None):
        self.client = client
        self.directory = None if journal_directory is None else Path(journal_directory).absolute()

    def _facts(self, plan, catalog):
        registration = self.client.registration()
        if type(registration) is not dict:
            fail('team_ticket_lead_unavailable')
        actor, session, profile, policy = (registration.get(key) for key in
                                          ('identity', 'session', 'profile', 'policy'))
        if not all(type(value) is dict for value in (actor, session, profile, policy)):
            fail('team_ticket_lead_unavailable')
        if (actor.get('owner_id') != plan['owner_id'] or actor.get('id') != profile.get('id')
                or session.get('id') != self.client.session_id
                or session.get('agent_id') != actor.get('id')
                or session.get('owner_id') != plan['owner_id']
                or session.get('device_id') != actor.get('device_id')
                or session.get('profile_version') != profile.get('version')
                or actor.get('profile_version') != profile.get('version')
                or profile.get('status') != 'active' or profile.get('legacy') is not False
                or type(profile.get('project_ids')) is not list
                or plan['project_id'] not in profile['project_ids']
                or any(policy.get(key) is not True for key in ('enabled', 'allow_read', 'allow_requests'))
                or policy.get('autonomy') not in ('requested', 'queue')
                or type(policy.get('actions')) is not list or 'agent.profile.read' not in policy['actions']
                or type(policy.get('resources')) is not list
                or 'agent-profiles:' + plan['owner_id'] not in policy['resources']):
            fail('team_ticket_lead_unavailable')
        version(profile.get('version'))
        project = self.client.call('get_project', {'project_id': plan['project_id']})
        project = _project(project, plan)
        parent_result = self.client.call('get_ticket', {'ticket_id': plan['parent_ticket_id']})
        if type(parent_result) is not dict or type(parent_result.get('ticket')) is not dict:
            fail('team_ticket_parent_changed')
        parent = parent_result['ticket']
        if (parent.get('id') != plan['parent_ticket_id'] or parent.get('project_id') != plan['project_id']
                or parent.get('version') != plan['parent_version'] or parent.get('status') == 'done'
                or _project(parent_result.get('project'), plan) != project):
            fail('team_ticket_parent_changed')
        version(parent.get('version'))
        parent = {key: deepcopy(parent[key]) for key in PARENT_FIELDS if key in parent}
        profiles = {}
        for slot in catalog['slots']:
            if slot['agent_id'] not in profiles:
                # A Lead may assign work to itself using this exact registered
                # profile. Other managers remain outside the ordinary-profile
                # endpoint's delegation; never fall back to this self context.
                profiles[slot['agent_id']] = (profile if slot['agent_id'] == actor['id'] else
                    self.client.call('get_managed_agent_profile', {'profile_id': slot['agent_id']}))
            target = profiles[slot['agent_id']]
            if type(target) is not dict:
                fail('team_ticket_profile_changed')
            version(target.get('version'))
            target_policy = target.get('policy')
            if (target.get('id') != slot['agent_id'] or target.get('owner_id') != plan['owner_id']
                    or target.get('version') != slot['profile_version'] or target.get('status') != 'active'
                    or type(target.get('project_ids')) is not list or plan['project_id'] not in target['project_ids']
                    or type(target_policy) is not dict
                    or any(target_policy.get(key) is not True for key in ('allow_read', 'allow_requests'))
                    or target_policy.get('autonomy') not in ('requested', 'queue')
                    or type(target_policy.get('resources')) is not list
                    or slot['resource'] not in target_policy['resources']
                    or slot['resource'] not in policy['resources']):
                fail('team_ticket_profile_changed')
        # Session timestamps and presence are deliberately excluded; grants
        # and identity remain bound and are reread before every creation.
        return {'endpoint': self.client.endpoint,
            'identity': {key: actor[key] for key in ('id', 'owner_id', 'device_id', 'profile_version')},
            'session_id': self.client.session_id, 'lead_profile_sha256': digest(canonical(_profile_context(profile))),
            'policy': {key: deepcopy(policy[key]) for key in
                       ('enabled', 'allow_read', 'allow_requests', 'actions', 'resources', 'autonomy')},
            'project_sha256': digest(canonical(project)), 'parent_sha256': digest(canonical(parent)),
            'profiles': {key: digest(canonical(_profile_context(value))) for key, value in sorted(profiles.items())}}

    def preview(self, plan, catalog):
        plan, catalog, order = documents(plan, catalog)
        facts = self._facts(plan, catalog)
        binding = {'schema': 'team-ticket-approval-v1', 'plan': plan, 'catalog': catalog, 'facts': facts}
        fingerprint = digest(canonical(binding))
        slots = {item['slot_id']: item for item in catalog['slots']}
        steps = {item['step_id']: item for item in plan['steps']}
        effects = [dict(step_id=key, title=steps[key]['title'], agent_id=slots[steps[key]['slot_id']]['agent_id'],
                        resource=slots[steps[key]['slot_id']]['resource'],
                        depends_on=steps[key]['depends_on'], milestone=steps[key]['milestone']) for key in order]
        return {'schema': 'team-ticket-preview-v1', 'approval_sha256': fingerprint,
                'order': order, 'effects': effects, 'binding': binding}

    @contextmanager
    def _locked(self):
        if self.directory is None:
            fail('team_ticket_journal_required')
        with PrivateDirectory(self.directory, create=True) as private:
            descriptor = None
            try:
                descriptor = os.open('plan.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=private.fd)
                private.validate_file(descriptor)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    fail('team_ticket_journal_busy')
                pin = identity(os.fstat(descriptor))
                def held():
                    private.verify(); private.validate_file(descriptor)
                    if identity(os.stat('plan.lock', dir_fd=private.fd, follow_symlinks=False)) != pin:
                        fail('team_ticket_journal_changed')
                held()
                yield private, held
                held()
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    @staticmethod
    def _read_optional(private, name):
        try:
            os.stat(name, dir_fd=private.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        return decode(private.read(name, 512 * 1024))

    @staticmethod
    def _payload(preview, step, receipts):
        binding = preview['binding']; plan = binding['plan']
        slot = next(item for item in binding['catalog']['slots'] if item['slot_id'] == step['slot_id'])
        key = 'team-ticket-' + digest(canonical({'approval_sha256': preview['approval_sha256'], 'step_id': step['step_id']}))
        labels = ['team-plan:' + preview['approval_sha256'], 'team-step:' + step['step_id'],
                  'team-parent:' + plan['parent_ticket_id']]
        if step['milestone']:
            labels.append('milestone:' + step['milestone'])
        return dict(project_id=plan['project_id'], title=step['title'], description=step['description'],
            acceptance_criteria=step['acceptance_criteria'], priority='normal',
            assignee_owner_id=plan['owner_id'], assignee_agent_id=slot['agent_id'], idempotency_key=key,
            depends_on=[receipts[key]['ticket_id'] for key in step['depends_on']], labels=labels,
            required_skills=[], resource=slot['resource'])

    def apply(self, plan, catalog, *, approved_sha256):
        sha256(approved_sha256)
        preview = self.preview(plan, catalog)
        if preview['approval_sha256'] != approved_sha256:
            fail('team_ticket_approval_changed')
        with self._locked() as (private, held):
            intent = self._read_optional(private, 'intent.json')
            if intent is None:
                private.create('intent.json', canonical(preview))
            elif intent != preview:
                fail('team_ticket_journal_conflict')
            state = self._read_optional(private, 'receipts.json')
            if state is None:
                state = {'schema': 'team-ticket-receipts-v1', 'approval_sha256': approved_sha256,
                         'tickets': {}, 'pending': None}
                private.create('receipts.json', canonical(state))
            if (type(state) is not dict or set(state) != {'schema', 'approval_sha256', 'tickets', 'pending'}
                    or state['schema'] != 'team-ticket-receipts-v1' or state['approval_sha256'] != approved_sha256
                    or type(state['tickets']) is not dict or not state['tickets'].keys() <= set(preview['order'])):
                fail('team_ticket_receipt_changed')
            completed = [key for key in preview['order'] if key in state['tickets']]
            if completed != preview['order'][:len(completed)]:
                fail('team_ticket_receipt_changed')
            identities = {key: set() for key in ('ticket_id', 'number', 'thread_id')}
            for step_id, receipt in state['tickets'].items():
                if (type(receipt) is not dict or set(receipt) != RECEIPT_FIELDS
                        or receipt['step_id'] != step_id or type(receipt['created_version']) is not int
                        or receipt['created_version'] != 1):
                    fail('team_ticket_receipt_changed')
                opaque(receipt['ticket_id']); opaque(receipt['thread_id'])
                version(receipt['number']); sha256(receipt['payload_sha256'])
                for key, seen in identities.items():
                    if receipt[key] in seen:
                        fail('team_ticket_receipt_changed')
                    seen.add(receipt[key])
            pending = state['pending']
            if pending is not None and (type(pending) is not dict or set(pending) != {'step_id', 'payload_sha256'}
                    or len(completed) == len(preview['order'])
                    or pending['step_id'] != preview['order'][len(completed)]):
                fail('team_ticket_receipt_changed')
            steps = {item['step_id']: item for item in preview['binding']['plan']['steps']}
            for step_id in preview['order']:
                held()
                step = steps[step_id]
                if not set(step['depends_on']) <= state['tickets'].keys():
                    fail('team_ticket_receipt_changed')
                payload = self._payload(preview, step, state['tickets'])
                payload_hash = digest(canonical(payload))
                existing = state['tickets'].get(step_id)
                if existing is not None:
                    if existing['payload_sha256'] != payload_hash:
                        fail('team_ticket_receipt_changed')
                    response = self.client.call('get_ticket', {'ticket_id': existing['ticket_id']})
                    ticket = response.get('ticket') if type(response) is dict else None
                    if (type(ticket) is not dict or ticket.get('id') != existing['ticket_id']
                            or ticket.get('project_id') != payload['project_id']
                            or ticket.get('number') != existing['number'] or ticket.get('thread_id') != existing['thread_id']
                            or type(ticket.get('version')) is not int or ticket['version'] < existing['created_version']):
                        fail('team_ticket_receipt_changed')
                    # A confirmed child may have progressed or been edited.
                    # Never restore its former prose, assignment or status.
                    continue
                held()
                fresh = self.preview(plan, catalog)
                held()
                if fresh != preview:
                    fail('team_ticket_approval_changed')
                recovering = state['pending'] is not None
                expected_pending = {'step_id': step_id, 'payload_sha256': payload_hash}
                if recovering:
                    if state['pending'] != expected_pending:
                        fail('team_ticket_receipt_changed')
                else:
                    state['pending'] = expected_pending
                    private.replace('receipts.json', canonical(state))
                ticket = self.client.call('create_ticket', payload)
                held()
                if (type(ticket) is not dict or type(ticket.get('version')) is not int or ticket['version'] < 1
                        or ticket.get('project_id') != payload['project_id']):
                    fail('team_ticket_creation_unconfirmed')
                # The server binds this exact key to the original payload hash.
                # Repeating a pending intent returns that ticket's current
                # state, which may already have changed after a lost ACK.
                # No search, label match or mutable prose is adoption proof.
                if (not recovering or ticket['version'] == 1) and (
                        ticket['version'] != 1 or ticket.get('status') != 'todo'
                        or ticket.get('claimant') is not None
                        or any(ticket.get(key) != value for key, value in payload.items()
                               if key not in {'idempotency_key', 'depends_on', 'labels', 'required_skills'})
                        or any(type(ticket.get(key)) is not list or sorted(ticket[key]) != sorted(payload[key])
                               for key in ('depends_on', 'labels', 'required_skills'))):
                    fail('team_ticket_creation_unconfirmed')
                for field in ('id', 'thread_id'):
                    opaque(ticket.get(field))
                version(ticket.get('number'))
                if any(any(row[field] == ticket[key] for field, key in
                           (('ticket_id', 'id'), ('number', 'number'), ('thread_id', 'thread_id')))
                       for row in state['tickets'].values()):
                    fail('team_ticket_creation_unconfirmed')
                state['tickets'][step_id] = dict(step_id=step_id, ticket_id=ticket['id'], number=ticket['number'],
                    thread_id=ticket['thread_id'], created_version=1, payload_sha256=payload_hash)
                state['pending'] = None
                private.replace('receipts.json', canonical(state))
            return {'schema': 'team-ticket-application-v1', 'status': 'complete',
                    'approval_sha256': approved_sha256,
                    'tickets': [deepcopy(state['tickets'][key]) for key in preview['order']]}
