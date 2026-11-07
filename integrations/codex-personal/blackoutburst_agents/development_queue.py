"""Bounded queue admission and explicit Core passes, without a daemon.

Discovery/admit never execute. Tick owns one durable active admission and calls
the existing continuation service; interrupted finite batches stay untouched.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import uuid

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import ACTIONS, Budgets, DevelopmentError, NamedTest, canonical, check_content, digest, identity, opaque, relative_path, sha256, signature
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


def read_queue_catalog(root,path):
    """Validate operator data without parsing connection credentials or RPC."""
    root=Path(root)
    path = child(str(Path(path).absolute()), root / 'config')
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
    if type(value['connection_file']) is not str: fail()
    connection = child(value['connection_file'], root / 'connections')
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
        child(entry['verifier_path'], root / 'verifiers'); sha256(entry['verifier_sha256'])
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
        child(repository['repository'], root / 'repositories')
        LocalRepositoryGrant(**repository, owner_id=value['owner_id'],
            workspace_root=str(root / 'checkouts' / 'preview' / entry['entry_id'])).document()
    return value, pin

def _standing_plan(root,catalog,catalog_pin,workflow_file,checkpoint_after_actions,provider_backoff_seconds):
    """Pin standing operator scope, independent of volatile ticket selection."""
    from .cloud_workflow import load_workflow
    from .continuation_pool_configuration import cycle_limits
    raw,_=file_document(child(workflow_file,root/'config'),MAX_BATCH)
    template=decode(raw)
    if type(template) is not dict or 'batch_id' not in template:fail('development_queue_workflow_invalid')
    jobs=[dict(name=e['entry_id'],agent_id=catalog['agent_id'],connection_file=catalog['connection_file'],
        paths=e['paths']) for e in catalog['entries']]
    workflow,workflow_pin=load_workflow(workflow_file,root=root,
        batch=dict(schema=QUEUE_SCHEMA,batch_id=template['batch_id'],jobs=jobs))
    if (type(checkpoint_after_actions) is not int or type(provider_backoff_seconds) is not int
            or not 30<=provider_backoff_seconds<=3600):fail('development_queue_tick_invalid')
    for entry in catalog['entries']:
        cap=cycle_limits(entry)
        # Keep the fixed test and final action together in a complete workflow.
        if (not 2<=checkpoint_after_actions<=min(cap['max_actions'],cap['max_turns'])
                or cap['max_wall_seconds']>catalog['timeout_seconds']):fail('development_queue_tick_invalid')
    binding=dict(catalog=catalog_pin,workflow=workflow_pin,checkpoint_after_actions=checkpoint_after_actions,
        provider_backoff_seconds=provider_backoff_seconds)
    return dict(schema='development-queue-tick-plan-v1',approval_sha256=digest(canonical(binding)),
        binding=binding,catalog=catalog,workflow=workflow,activated=False,launchable=False)

def queue_tick_plan(root,catalog_file,workflow_file,*,checkpoint_after_actions=3,provider_backoff_seconds=60):
    catalog,pin=read_queue_catalog(root,catalog_file)
    return _standing_plan(Path(root),catalog,pin,workflow_file,checkpoint_after_actions,provider_backoff_seconds)


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
        value,pin=read_queue_catalog(self.root,path)
        connection=child(value['connection_file'],self.root/'connections')
        if (value['board_endpoint']!=self.client.endpoint or connection!=self.client.connection_path
                or file_document(connection)[1]!=self.client.connection_pin):
            fail('development_queue_connection_changed')
        return value,pin

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

    def _preview(self, catalog_file, *, autonomous, assigned_only=False):
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
            if assigned_only and type(ticket) is dict and ticket.get('assignee_agent_id') is None:
                continue
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

    def admit(self, catalog_file, *, request_id, approved_sha256, _assigned_only=False):
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
            preview = (self._preview(catalog_file,autonomous=False,assigned_only=True)
                       if _assigned_only else self.preview(catalog_file))
            if preview['approval_sha256'] != approved_sha256:
                fail('development_queue_approval_changed')
            if not preview['execution_authorized']:
                fail('development_queue_not_authorized')
            intent = dict(schema='development-queue-intent-v1', request_id=request_id, preview=preview)
            private.create('intent.json', canonical(intent))
            fresh = self._preview(catalog_file, autonomous=True,assigned_only=_assigned_only)
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

    def tick_plan(self,catalog_file,workflow_file,*,checkpoint_after_actions=3,provider_backoff_seconds=60):
        catalog,pin=self._catalog(catalog_file)
        return _standing_plan(self.root,catalog,pin,workflow_file,checkpoint_after_actions,provider_backoff_seconds)

    @contextmanager
    def _tick_locked(self,catalog):
        self._local()
        key=digest(canonical({k:catalog[k] for k in ('owner_id','agent_id')}))
        with PrivateDirectory(self.root/'admissions',create=True) as parent:
            try:os.mkdir('queue-'+key,0o700,dir_fd=parent.fd);os.fsync(parent.fd)
            except FileExistsError:pass
        with PrivateDirectory(self.root/'admissions'/('queue-'+key)) as private:
            fd=os.open('tick.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600,dir_fd=private.fd)
            try:
                private.validate_file(fd);pin=identity(os.fstat(fd))
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:fail('development_queue_tick_busy')
                def held():
                    self._local();private.verify();private.validate_file(fd)
                    if identity(os.fstat(fd))!=pin or identity(os.stat('tick.lock',dir_fd=private.fd,
                            follow_symlinks=False))!=pin:fail('development_queue_tick_changed')
                held();yield private,held;held()
            finally:os.close(fd)

    def tick(self,catalog_file,workflow_file,*,approved_sha256,executable,cli_version,interpreter,
             unshare,gh_executable,max_seconds=7200,checkpoint_after_actions=3,provider_backoff_seconds=60,
             should_stop=None,turn_event=None):
        """Run at most one existing Core pass, never replay a finite batch.

        A completed independent review is archived before later discovery. A
        pending admission or Core remains attached to its original identities.
        """
        from .continuation_pool_service import ContinuationPoolService
        sha256(approved_sha256)
        if type(max_seconds) is not int or not 1<=max_seconds<=7200:fail('development_queue_tick_invalid')
        if should_stop is not None and not callable(should_stop):fail('development_queue_tick_invalid')
        def stopping():
            try:return bool(should_stop and should_stop())
            except BaseException:return True
        plan=self.tick_plan(catalog_file,workflow_file,checkpoint_after_actions=checkpoint_after_actions,
            provider_backoff_seconds=provider_backoff_seconds)
        if plan['approval_sha256']!=approved_sha256:fail('development_queue_approval_changed')
        catalog=plan['catalog']
        standing={}
        for pin in (plan['binding']['catalog'],plan['binding']['workflow']):
            before=signature(os.stat(pin['path'],follow_symlinks=False))
            observed=file_document(pin['path'],MAX_BATCH)[1]
            after=signature(os.stat(pin['path'],follow_symlinks=False))
            if observed!=pin or before!=after:fail('development_queue_approval_changed')
            standing[pin['path']]=after
        def checked_stop():
            # Cheap local revocation during native authorization windows;
            # full protected reads bracket the pass, never cache authority.
            self._local()
            for path,pin in standing.items():
                if signature(os.stat(path,follow_symlinks=False))!=pin:fail('development_queue_approval_changed')
            return stopping()
        def response(phase,active=None,service=None):
            return dict(schema='development-queue-tick-v1',phase=phase,
                request_id=None if active is None else active['request_id'],
                logical_task_id=None if active is None else active['logical_task_id'],
                service=service,launchable=False,pass_complete_is_project_completion=False)
        if stopping():return response('stopped')
        with self._tick_locked(catalog) as (private,held):
            try:os.stat('tick.json',dir_fd=private.fd,follow_symlinks=False)
            except FileNotFoundError:
                state=dict(schema='development-queue-tick-state-v1',approval_sha256=approved_sha256,
                    session_id=self.client.session_id,phase='idle',active=None,last_receipt=None)
                private.create('tick.json',canonical(state))
            else:state=decode(private.read('tick.json',MAX_BATCH))
            fields={'schema','approval_sha256','session_id','phase','active','last_receipt'}
            if (type(state) is not dict or set(state)!=fields or state['schema']!='development-queue-tick-state-v1'
                    or state['phase'] not in ('idle','admitting','active')
                    or state['session_id']!=self.client.session_id):fail('development_queue_tick_changed')
            def save(value):
                held()
                if decode(private.read('tick.json',MAX_BATCH))!=state:fail('development_queue_tick_changed')
                private.replace('tick.json',canonical(value))
                return value
            if state['phase']=='idle':
                if state['active'] is not None:fail('development_queue_tick_changed')
                if state['last_receipt'] is not None:
                    last=state['last_receipt']
                    if (type(last) is not dict or set(last)!={'file','sha256'}
                            or type(last['file']) is not str):fail('development_queue_tick_changed')
                    previous=uuid_value(last['file'].removeprefix('finished-').removesuffix('.json'))
                    if last['file']!='finished-'+previous+'.json':fail('development_queue_tick_changed')
                    if digest(private.read(last['file'],MAX_BATCH))!=last['sha256']:fail('development_queue_tick_changed')
                if stopping():return response('stopped')
                preview=self._preview(catalog_file,autonomous=False,assigned_only=True)
                if not preview['execution_authorized']:fail('development_queue_not_authorized')
                if preview['selected'] is None:return response('empty')
                active=dict(request_id=str(uuid.uuid4()),logical_task_id=str(uuid.uuid4()),
                    preview_sha256=preview['approval_sha256'],configuration_pin=None,admission_sha256=None)
                state=save({**state,'approval_sha256':approved_sha256,'phase':'admitting','active':active})
            if state['approval_sha256']!=approved_sha256:fail('development_queue_approval_changed')
            active=state['active']
            if (type(active) is not dict or set(active)!={'request_id','logical_task_id','preview_sha256',
                    'configuration_pin','admission_sha256'}):fail('development_queue_tick_changed')
            uuid_value(active['request_id']);uuid_value(active['logical_task_id']);sha256(active['preview_sha256'])
            request=active['request_id'];configuration=self.root/'config'/('queue-continuation-'+request+'.json')
            if state['phase']=='admitting':
                if stopping():return response('stopped',active)
                admission=self.admit(catalog_file,request_id=request,approved_sha256=active['preview_sha256'],_assigned_only=True)
                if admission['status']!='admitted':fail('development_queue_tick_changed')
                batch,_=load_batch(admission['batch_file'],root=self.root,owner_id=catalog['owner_id'],
                    board_endpoint=catalog['board_endpoint'],max_seconds=max_seconds)
                job=batch['jobs'][0];spec=next(j for j in plan['workflow']['jobs'] if j['name']==job['name'])
                workflow=dict(schema=plan['workflow']['schema'],batch_id=request,jobs=[spec])
                workflow_path=self.root/'config'/('queue-workflow-'+request+'.json')
                config=dict(schema='linux-cloud-continuation-v1',batch_file=admission['batch_file'],
                    workflow_file=str(workflow_path),concurrency=1,provider_backoff_seconds=provider_backoff_seconds,
                    tasks=[dict(name=job['name'],logical_task_id=active['logical_task_id'],
                        checkpoint_after_actions=checkpoint_after_actions)])
                with PrivateDirectory(self.root/'config') as documents:
                    for path,value in ((workflow_path,workflow),(configuration,config)):
                        held();expected=canonical(value)
                        try:os.stat(path.name,dir_fd=documents.fd,follow_symlinks=False)
                        except FileNotFoundError:documents.create(path.name,expected)
                        else:
                            if documents.read(path.name,MAX_BATCH)!=expected:fail('development_queue_tick_changed')
                active={**active,'configuration_pin':file_document(configuration,MAX_BATCH)[1],
                    'admission_sha256':digest(file_document(self.root/'admissions'/request/'complete.json',MAX_BATCH)[0])}
                state=save({**state,'phase':'active','active':active})
            if (file_document(configuration,MAX_BATCH)[1]!=active['configuration_pin']
                    or digest(file_document(self.root/'admissions'/request/'complete.json',MAX_BATCH)[0])!=active['admission_sha256']):
                fail('development_queue_tick_changed')
            held()
            if checked_stop():return response('stopped',active)
            service=ContinuationPoolService(self.root,configuration_path=configuration,owner_id=catalog['owner_id'],
                board_endpoint=catalog['board_endpoint'],executable=executable,cli_version=cli_version,
                interpreter=interpreter,unshare=unshare,gh_executable=gh_executable,max_seconds=max_seconds)
            service.turn_event=turn_event
            result=service.run_once(should_stop=checked_stop);held();checked_stop()
            for pin in (plan['binding']['catalog'],plan['binding']['workflow']):
                if file_document(pin['path'],MAX_BATCH)[1]!=pin:fail('development_queue_approval_changed')
            tasks=result.get('tasks') if type(result) is dict else None
            if type(tasks) is not list or len(tasks)!=1 or tasks[0].get('logical_task_id')!=active['logical_task_id']:
                fail('development_queue_tick_changed')
            phase=tasks[0]['phase']
            if result['status']=='pass_complete' and phase=='independent_review_published':
                review={key:tasks[0][key] for key in ('name','logical_task_id','phase','operation_id','state_sha256',
                    'review_operation_id','review_receipt_sha256','accepted','launchable')}
                archive=dict(schema='development-queue-finished-v1',active=active,
                    approval_sha256=approved_sha256,review=review,accepted=False)
                name='finished-'+request+'.json';raw=canonical(archive)
                try:os.stat(name,dir_fd=private.fd,follow_symlinks=False)
                except FileNotFoundError:private.create(name,raw)
                else:
                    if private.read(name,MAX_BATCH)!=raw:fail('development_queue_tick_changed')
                state=save({**state,'phase':'idle','active':None,'last_receipt':dict(file=name,sha256=digest(raw))})
            return response(phase,active,result)
