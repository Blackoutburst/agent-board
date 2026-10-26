"""Immutable local selections for the explicit development pool adapter.

These owned files bind an approved local configuration to a Pool job. They are
consistency pins, never Board grants, qualification or execution authority.
"""
from pathlib import Path
import os
import threading

from .dev_journal import PrivateDirectory, decode
from .dev_manifest import (DevelopmentError, canonical, check_content, digest,
                           opaque, signature, validate_manifest)
from .dev_planner import _directory_pin, _reasoning_configuration
from .dev_planner_journal import PlannerBudgets
from .pool_journal import uuid_value
from .work_journal import _scope

REVISION = 'development-pool-linux-v1'
MAX_SELECTION = 64 * 1024
FIELDS = {'schema', 'operation_id', 'worker_id', 'session_id', 'scope',
          'broker_directory', 'backend_directory', 'planner_policy_directory',
          'instruction_file', 'model', 'budgets'}


def fail(code='development_pool_selection_mismatch'):
    raise DevelopmentError(code)


def file_document(path, maximum=MAX_SELECTION):
    path = Path(path)
    if not path.is_absolute(): fail('development_pool_absolute_path_required')
    with PrivateDirectory(path.parent) as private:
        before = os.stat(path.name, dir_fd=private.fd, follow_symlinks=False)
        raw = private.read(path.name, maximum)
        after = os.stat(path.name, dir_fd=private.fd, follow_symlinks=False)
        if signature(before) != signature(after): fail('development_pool_file_changed')
    return raw, {'path': str(path), 'identity': [before.st_dev, before.st_ino],
                 'sha256': digest(raw)}


def job_identity(job):
    if type(job) is not dict or not {'id', 'worker_id', 'session_id'} <= set(job): fail()
    result = {name: job[name] for name in ('id', 'worker_id', 'session_id')}
    for value in result.values(): uuid_value(value)
    if len(set(result.values())) != 3: fail()
    return result


class DevelopmentSelections:
    def __init__(self, directory, *, connection_file, owner_id, agent_id, board_endpoint):
        self.directory = str(Path(directory).absolute())
        with PrivateDirectory(self.directory, create=True): pass
        self.pin = _directory_pin(self.directory)
        self.fixed = {'owner_id': opaque(owner_id), 'agent_id': opaque(agent_id),
                      'board_endpoint': board_endpoint}
        self.connection_file = str(Path(connection_file))
        _, self.connection_pin = file_document(self.connection_file)
        self.mutex = threading.RLock()

    def private(self):
        if _directory_pin(self.directory) != self.pin: fail('development_pool_registry_changed')
        return PrivateDirectory(self.directory)

    def validate(self, selection, job, payload):
        identity = job_identity(job)
        if (type(selection) is not dict or set(selection) not in (FIELDS, FIELDS | {'reasoning_effort'})
                or type(selection['schema']) is not int or selection['schema'] != 1
                or type(payload) is not dict or set(payload) != {'project_id', 'ticket_id'}): fail()
        if any(selection[name] != identity[key] for name, key in
               (('operation_id', 'id'), ('worker_id', 'worker_id'), ('session_id', 'session_id'))): fail()
        expected = {**self.fixed, **payload, 'board_session_id': identity['session_id']}
        _scope(expected)
        if selection['scope'] != expected: fail()
        opaque(selection['model'], 'invalid_development_model', 128)
        if 'reasoning_effort' in selection:
            if selection['reasoning_effort'] is None: fail('invalid_planner_reasoning_effort')
            _reasoning_configuration(selection['reasoning_effort'])
        if (type(selection['budgets']) is not dict
                or set(selection['budgets']) != set(PlannerBudgets().document())): fail()
        PlannerBudgets(**selection['budgets']).document()
        if any(type(selection[name + '_directory']) is not str
               or not Path(selection[name + '_directory']).is_absolute()
               for name in ('broker', 'backend', 'planner_policy')): fail()
        pins = {name: _directory_pin(selection[name + '_directory']) for name in
                ('broker', 'backend', 'planner_policy')}
        documents = {}
        for name, path in {
            'manifest': Path(pins['broker']['path']) / 'manifest.json',
            'backend_plan': Path(pins['backend']['path']) / 'execution-plan.json',
            'backend_qualification': Path(pins['backend']['path']) / 'qualification.json',
            'policy_plan': Path(pins['planner_policy']['path']) / 'planner-plan.json',
            'policy_qualification': Path(pins['planner_policy']['path']) / 'planner-qualification.json',
        }.items():
            raw, documents[name] = file_document(path, 128 * 1024)
            if name == 'manifest': manifest = validate_manifest(decode(raw))
            elif name.endswith('qualification') and type(decode(raw)) is not dict:
                fail('development_pool_unqualified')
        wanted = {key: expected[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')}
        if (any(manifest['scope'][key] != value for key, value in wanted.items())
                or manifest['scope']['session_id'] != identity['session_id']): fail()
        # The real worker/backend still validate every closed plan, qualification,
        # environment and current permission. Merely present JSON grants nothing.
        with PrivateDirectory(pins['broker']['path']) as private:
            state = decode(private.read('state.json', 2 * 1024 * 1024))
        if (type(state) is not dict or type(state.get('schema')) is not int or state.get('schema') != 3 or state.get('manifest_hash') != documents['manifest']['sha256']
                or state.get('linux_execution_plan_sha256') != documents['backend_plan']['sha256']):
            fail('development_pool_linux_profile_required')
        instruction, instruction_pin = file_document(selection['instruction_file'], 16384)
        instruction = check_content(instruction)
        if not instruction.strip(): fail('invalid_planner_instruction')
        _, connection = file_document(self.connection_file)
        if connection != self.connection_pin: fail('development_pool_connection_changed')
        return {'schema': 1, 'revision': REVISION, 'selection': selection,
                'pins': pins, 'documents': documents, 'connection': connection,
                'instruction_pin': instruction_pin, 'instruction': instruction,
                'manifest_scope': manifest['scope']}

    def bind(self, job, approved_selection_file, payload):
        if job.get('state') != 'queued' or job.get('driver_revision') != REVISION: fail()
        raw, _ = file_document(approved_selection_file)
        value = self.validate(decode(raw), job, payload)
        encoded = canonical(value)
        with self.mutex, self.private() as private:
            try: private.create(job['id'] + '.json', encoded)
            except DevelopmentError as error:
                if error.code != 'private_file_exists': raise
                if private.read(job['id'] + '.json', MAX_SELECTION) != encoded:
                    fail('development_pool_selection_already_bound')
        return {'operation_id': job['id'], 'session_id': job['session_id'],
                'selection_sha256': digest(encoded), 'status': 'bound'}

    def load(self, context):
        job = {'id': context.operation_id, 'worker_id': context.worker_id, 'session_id': context.session_id}
        with self.mutex, self.private() as private:
            raw = private.read(context.operation_id + '.json', MAX_SELECTION)
        value = decode(raw)
        if (type(value) is not dict or 'selection' not in value
                or canonical(value) != raw or self.validate(value['selection'], job, context.payload) != value): fail()
        return value
