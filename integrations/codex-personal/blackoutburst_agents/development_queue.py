"""One bounded admission from an operator catalog, never a queue daemon.

Discovery is unclaimed. Admission writes one new finite v3 configuration only;
the existing finite driver later owns the single atomic selected-queue claim.
This module never launches models, tests, Git, Runtime, jobs or services.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import ACTIONS, Budgets, DevelopmentError, NamedTest, canonical, check_content, digest, opaque, relative_path, sha256
from .dev_planner import _directory_pin, _reasoning_configuration
from .dev_planner_journal import PlannerBudgets
from .development_pool_selection import file_document
from .finite_pool_batch import JOB_FIELDS, MAX_BATCH, QUEUE_SCHEMA, child, load_batch
from .git_workspace import LocalRepositoryGrant
from .pool_journal import identifier, uuid_value


CATALOG_FIELDS = {'schema', 'owner_id', 'agent_id', 'profile_version', 'board_endpoint',
                  'project_id', 'connection_file', 'timeout_seconds', 'entries'}
ENTRY_FIELDS = JOB_FIELDS - {'name', 'agent_id', 'connection_file', 'project_id', 'ticket_id'} | {'entry_id'}
PROJECT_FIELDS = ('id', 'version', 'name', 'description', 'context', 'repository_url',
                  'environment', 'participants', 'archived')


def fail(code='development_queue_invalid'):
    raise DevelopmentError(code)


def positive(value):
    if type(value) is not int or not 1 <= value < 2 ** 63:
        fail()
    return value


class DevelopmentQueueAdmission:
    def __init__(self, root, client):
        self.root = Path(root).absolute()
        self.client = client
        self.roots = {}
        for name in ('', 'config', 'connections', 'repositories', 'checkouts', 'verifiers'):
            path = self.root / name
            with PrivateDirectory(path): pass
            self.roots[str(path)] = _directory_pin(path)

    def _local(self):
        for path, pin in self.roots.items():
            if _directory_pin(path) != pin:
                fail('development_queue_root_changed')

    def _catalog(self, path):
        self._local()
        path = child(str(Path(path).absolute()), self.root / 'config')
        raw, pin = file_document(path, MAX_BATCH)
        value = decode(raw)
        canonical(value)  # Reject escaped invalid Unicode before any read/RPC.
        if (type(value) is not dict or set(value) != CATALOG_FIELDS
                or value['schema'] != 'development-queue-catalog-v1'
                or type(value['entries']) is not list or not 1 <= len(value['entries']) <= 16):
            fail()
        for key in ('owner_id', 'agent_id', 'project_id'): opaque(value[key])
        positive(value['profile_version'])
        if type(value['timeout_seconds']) is not int or not 1 <= value['timeout_seconds'] <= 14400:
            fail()
        if value['board_endpoint'] != self.client.endpoint:
            fail('development_queue_connection_changed')
        if type(value['connection_file']) is not str: fail()
        connection = child(value['connection_file'], self.root / 'connections')
        if connection != self.client.connection_path or file_document(connection)[1] != self.client.connection_pin:
            fail('development_queue_connection_changed')
        names = set(); resources = set()
        for entry in value['entries']:
            if type(entry) is not dict or set(entry) not in (ENTRY_FIELDS, ENTRY_FIELDS | {'reasoning_effort'}):
                fail()
            identifier(entry['entry_id']); opaque(entry['resource']); opaque(entry['model'], maximum=128)
            if (entry['entry_id'] in names or entry['resource'] in resources or '*' in entry['resource']
                    or entry['resource'].startswith(('agent-profiles:', 'runtime:', 'server:', 'provider:',
                                                     'device:', 'credential:', 'policy:'))):
                fail()
            names.add(entry['entry_id']); resources.add(entry['resource'])
            if (type(entry['paths']) is not list or not 1 <= len(entry['paths']) <= 128
                    or any(type(item) is not str for item in entry['paths'])
                    or len(set(entry['paths'])) != len(entry['paths'])):
                fail()
            for path in entry['paths']: relative_path(path)
            if (type(entry['actions']) is not list or any(type(item) is not str for item in entry['actions'])
                    or not {'repo.read_file', 'repo.run_test'} <= set(entry['actions']) <= ACTIONS
                    or len(set(entry['actions'])) != len(entry['actions'])):
                fail()
            if type(entry['verifier_path']) is not str: fail()
            child(entry['verifier_path'], self.root / 'verifiers'); sha256(entry['verifier_sha256'])
            verifier, _ = file_document(entry['verifier_path'], 65536)
            if digest(verifier) != entry['verifier_sha256']: fail('development_queue_verifier_changed')
            check_content(verifier)
            NamedTest(entry['test_name'], 'queue-preview', entry['test_timeout_seconds'], entry['test_max_output_bytes']).document()
            if entry['test_timeout_seconds'] > 60: fail()
            if type(entry['instruction']) is not str or not entry['instruction'].strip() or len(entry['instruction'].encode()) > 16384:
                fail()
            check_content(entry['instruction'].encode())
            if (type(entry['planner_budgets']) is not dict or set(entry['planner_budgets']) != set(PlannerBudgets().document())
                    or type(entry['broker_budgets']) is not dict or set(entry['broker_budgets']) != set(Budgets().document())):
                fail()
            PlannerBudgets(**entry['planner_budgets']).document(); Budgets(**entry['broker_budgets']).document()
            if 'reasoning_effort' in entry:
                if entry['reasoning_effort'] is None: fail()
                _reasoning_configuration(entry['reasoning_effort'])
            repository = entry['repository']
            if type(repository) is not dict or set(repository) != {'repository_id', 'repository', 'base_commit'}:
                fail()
            if any(type(value) is not str for value in repository.values()): fail()
            child(repository['repository'], self.root / 'repositories')
            LocalRepositoryGrant(**repository, owner_id=value['owner_id'],
                workspace_root=str(self.root / 'checkouts' / 'preview' / entry['entry_id'])).document()
        return value, pin

    def _registration(self, catalog, *, autonomous):
        registration = self.client.registration(autonomous=autonomous)
        try:
            actor, profile, session, policy = (registration[key] for key in ('identity', 'profile', 'session', 'policy'))
            if not all(type(value) is dict for value in (actor, profile, session, policy)):
                raise ValueError
            if any(value.get('kind') == 'companion' or value.get('companion_for') is not None
                   for value in (registration, actor, profile, session)):
                raise ValueError
            if (actor['id'] != catalog['agent_id'] or actor['owner_id'] != catalog['owner_id']
                    or profile['id'] != actor['id'] or profile['owner_id'] != actor['owner_id']
                    or profile['status'] != 'active' or profile['version'] != catalog['profile_version']
                    or registration['profile_version'] != catalog['profile_version']
                    or session['profile_version'] != catalog['profile_version']
                    or actor['profile_version'] != catalog['profile_version']
                    or session['id'] != self.client.session_id or session['agent_id'] != actor['id']
                    or session['owner_id'] != actor['owner_id'] or session['device_id'] != actor['device_id']
                    or policy['enabled'] is not True or policy['allow_read'] is not True):
                raise ValueError
            for version in (profile['version'], actor['profile_version'], registration['profile_version'], session['profile_version']):
                positive(version)
            for entry in catalog['entries']:
                if not set(entry['actions']) <= set(policy['actions']) or entry['resource'] not in policy['resources']:
                    raise ValueError
            executable = (policy.get('allow_requests') is True and policy.get('autonomy') == 'queue'
                          and profile['policy'].get('autonomy') == 'queue')
            if autonomous and (not executable or session.get('autonomous') is not True):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            fail('development_queue_not_authorized')
        return actor, policy, executable

    def _preview(self, catalog_file, *, autonomous):
        catalog, pin = self._catalog(catalog_file)
        actor, policy, executable = self._registration(catalog, autonomous=autonomous)
        project = self.client.call('get_project', {'project_id': catalog['project_id']})
        if (type(project) is not dict or any(key not in project for key in PROJECT_FIELDS)
                or project['id'] != catalog['project_id'] or project['archived'] is not False
                or type(project['participants']) is not list or catalog['owner_id'] not in project['participants']):
            fail('development_queue_project_changed')
        positive(project['version'])
        work = self.client.call('get_work', {'project_id': catalog['project_id'], 'limit': 200})
        if type(work) is not dict or type(work.get('ready')) is not list or len(work['ready']) > 200:
            fail('development_queue_candidates_invalid')
        entries = {entry['resource']: entry for entry in catalog['entries']}
        selected = None
        for row in work['ready']:
            if type(row) is not dict: fail('development_queue_candidates_invalid')
            if row.get('delegation') is not None: continue
            if type(row.get('resource')) is not str: fail('development_queue_candidates_invalid')
            if row.get('resource') not in entries: continue
            response = self.client.call('get_ticket', {'ticket_id': row['id']})
            ticket = response.get('ticket') if type(response) is dict else None
            if (type(ticket) is not dict or ticket.get('id') != row.get('id')
                    or ticket.get('project_id') != catalog['project_id'] or ticket.get('version') != row.get('version')
                    or ticket.get('status') != 'todo' or ticket.get('claimant') is not None
                    or ticket.get('delegation') is not None or ticket.get('claim_expired') is True
                    or ticket.get('blocked_by') or ticket.get('waiting_on_human')
                    or ticket.get('resource') != row.get('resource')
                    or ticket.get('assignee_owner_id') not in (None, catalog['owner_id'])
                    or ticket.get('assignee_agent_id') not in (None, catalog['agent_id'])):
                fail('development_queue_candidate_changed')
            positive(ticket['version']); opaque(ticket['id'])
            selected = dict(ticket_id=ticket['id'], ticket_version=ticket['version'], resource=ticket['resource'],
                            entry_id=entries[ticket['resource']]['entry_id'])
            break
        policy_context = {key: policy[key] for key in ('enabled', 'allow_read', 'allow_requests', 'autonomy', 'actions', 'resources')}
        binding = dict(catalog_pin=pin, catalog=catalog, roots=self.roots, connection_pin=self.client.connection_pin,
            identity={key: actor[key] for key in ('id', 'owner_id', 'device_id')},
            session_id=self.client.session_id, policy=policy_context,
            project={key: project[key] for key in PROJECT_FIELDS}, selected=selected)
        return dict(schema='development-queue-preview-v1', approval_sha256=digest(canonical(binding)),
                    execution_authorized=executable, selected=selected, binding=binding)

    def preview(self, catalog_file):
        return self._preview(catalog_file, autonomous=False)

    def _batch(self, preview, request_id):
        catalog = preview['binding']['catalog']; selected = preview['selected']
        entry = next(item for item in catalog['entries'] if item['entry_id'] == selected['entry_id'])
        name = entry['entry_id']; repository = {**entry['repository'], 'owner_id': catalog['owner_id'],
            'workspace_root': str(self.root / 'checkouts' / request_id / name)}
        job = {key: deepcopy(value) for key, value in entry.items() if key not in ('entry_id', 'repository')}
        job.update(name=name, agent_id=catalog['agent_id'], connection_file=catalog['connection_file'],
            repository=repository, project_id=catalog['project_id'], ticket_id=selected['ticket_id'],
            queue_admission=dict(schema='development-queue-admission-v1', ticket_version=selected['ticket_version'],
                profile_version=catalog['profile_version'], catalog_sha256=preview['binding']['catalog_pin']['sha256']))
        return dict(schema=QUEUE_SCHEMA, batch_id=request_id, owner_id=catalog['owner_id'],
                    board_endpoint=catalog['board_endpoint'], timeout_seconds=catalog['timeout_seconds'], jobs=[job])

    @contextmanager
    def _locked(self, request_id):
        uuid_value(request_id); self._local()
        with PrivateDirectory(self.root / 'admissions', create=True) as parent:
            try: os.mkdir(request_id, 0o700, dir_fd=parent.fd); os.fsync(parent.fd)
            except FileExistsError: pass
        with PrivateDirectory(self.root / 'admissions' / request_id) as private:
            descriptor = os.open('admission.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=private.fd)
            try:
                private.validate_file(descriptor)
                try: fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: fail('development_queue_busy')
                yield private
                private.verify()
            finally: os.close(descriptor)

    def admit(self, catalog_file, *, request_id, approved_sha256):
        uuid_value(request_id); sha256(approved_sha256)
        catalog, catalog_pin = self._catalog(catalog_file)
        with self._locked(request_id) as private:
            names = set(os.listdir(private.fd))
            if 'intent.json' in names:
                intent = decode(private.read('intent.json', MAX_BATCH * 3))
                if (type(intent) is not dict or set(intent) != {'schema', 'request_id', 'preview'}
                        or intent.get('schema') != 'development-queue-intent-v1' or intent.get('request_id') != request_id):
                    fail('development_queue_intent_conflict')
                preview = intent['preview']
                if (type(preview) is not dict or set(preview) != {'schema', 'approval_sha256',
                            'execution_authorized', 'selected', 'binding'}
                        or preview.get('schema') != 'development-queue-preview-v1'
                        or preview.get('approval_sha256') != approved_sha256 or preview.get('execution_authorized') is not True
                        or type(preview.get('binding')) is not dict):
                    fail('development_queue_intent_conflict')
                binding = preview['binding']
                if (set(binding) != {'catalog_pin', 'catalog', 'roots', 'connection_pin', 'identity',
                                    'session_id', 'policy', 'project', 'selected'}
                        or digest(canonical(binding)) != approved_sha256 or binding['selected'] != preview['selected']
                        or binding['catalog_pin'] != catalog_pin or binding['catalog'] != catalog
                        or binding['roots'] != self.roots or binding['connection_pin'] != self.client.connection_pin
                        or binding['session_id'] != self.client.session_id):
                    fail('development_queue_intent_conflict')
                actor, policy, _ = self._registration(catalog, autonomous=False)
                policy_context = {key: policy[key] for key in
                                  ('enabled', 'allow_read', 'allow_requests', 'autonomy', 'actions', 'resources')}
                if (binding['identity'] != {key: actor[key] for key in ('id', 'owner_id', 'device_id')}
                        or binding['policy'] != policy_context):
                    fail('development_queue_intent_conflict')
                project = self.client.call('get_project', {'project_id': catalog['project_id']})
                if (type(project) is not dict or project.get('id') != catalog['project_id']
                        or project.get('archived') is not False
                        or catalog['owner_id'] not in project.get('participants', [])):
                    fail('development_queue_project_changed')
                if 'complete.json' not in names: fail('development_queue_admission_pending')
                completed = decode(private.read('complete.json', MAX_BATCH))
                if (type(completed) is not dict or set(completed) != {'schema', 'request_id', 'approval_sha256',
                            'status', 'batch_file', 'batch_pin', 'launchable', 'model_calls'}
                        or completed.get('schema') != 'development-queue-admission-receipt-v1'
                        or completed.get('request_id') != request_id or completed.get('approval_sha256') != approved_sha256
                        or completed.get('status') not in ('admitted', 'empty')
                        or completed.get('launchable') is not False or type(completed.get('model_calls')) is not int
                        or completed['model_calls'] != 0):
                    fail('development_queue_receipt_changed')
                if completed['status'] == 'admitted':
                    if completed['batch_file'] != str(self.root / 'config' / ('queue-admission-' + request_id + '.json')):
                        fail('development_queue_receipt_changed')
                    raw, pin = file_document(completed['batch_file'], MAX_BATCH)
                    if (pin != completed['batch_pin'] or preview['selected'] is None
                            or raw != canonical(self._batch(preview, request_id))):
                        fail('development_queue_receipt_changed')
                elif completed['batch_file'] is not None or completed['batch_pin'] is not None or preview['selected'] is not None:
                    fail('development_queue_receipt_changed')
                return {**completed, 'historical': True}
            preview = self.preview(catalog_file)
            if preview['approval_sha256'] != approved_sha256:
                fail('development_queue_approval_changed')
            if not preview['execution_authorized']:
                fail('development_queue_not_authorized')
            intent = dict(schema='development-queue-intent-v1', request_id=request_id, preview=preview)
            private.create('intent.json', canonical(intent))
            fresh = self._preview(catalog_file, autonomous=True)
            if fresh != preview: fail('development_queue_approval_changed')
            completed = dict(schema='development-queue-admission-receipt-v1', request_id=request_id,
                approval_sha256=approved_sha256, status='empty', batch_file=None, batch_pin=None,
                launchable=False, model_calls=0)
            if preview['selected'] is not None:
                catalog = preview['binding']['catalog']
                batch = self._batch(preview, request_id)
                filename = 'queue-admission-' + request_id + '.json'
                with PrivateDirectory(self.root / 'config') as config:
                    config.create(filename, canonical(batch))
                path = str(self.root / 'config' / filename)
                loaded, pin = load_batch(path, root=self.root, owner_id=catalog['owner_id'],
                    board_endpoint=catalog['board_endpoint'], max_seconds=14400)
                if loaded != batch: fail('development_queue_batch_changed')
                completed.update(status='admitted', batch_file=path, batch_pin=pin)
            private.create('complete.json', canonical(completed))
            return {**completed, 'historical': False}
