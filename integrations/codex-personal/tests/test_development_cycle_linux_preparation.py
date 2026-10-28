"""Real Linux-profile checkpoint/retirement/preparation, synthetic host/model.

Only Linux host pins and native qualification are substituted. The concrete
backend/policy constructors, private plans, Board, Worker, Planner, Runtime,
Git checkpoints and LinuxPoolJobPreparer all execute their ordinary paths.
No native sandbox, CLI, network or real model is launched by this fixture.
"""
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
import uuid

import pytest

from blackoutburst_agents import linux_planner_policy as policy_module
from blackoutburst_agents import linux_pool_preparation as preparation
from blackoutburst_agents import linux_test_backend as backend_module
from blackoutburst_agents.continuation import ContinuationChecks, ContinuationTask
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.dev_runtime import development_work_id
from blackoutburst_agents.development_cycle_driver import DevelopmentCycleDriver
from blackoutburst_agents.development_pool import DevelopmentPoolDriver
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from blackoutburst_agents.linux_input_plan import pin_linux_input_plan
from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
from blackoutburst_agents.private_development_checkpoint import PrivateDevelopmentCheckpointController
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.worker_pool import WorkerPool
from test_development_worker import DevelopmentBoardWorker, FixtureClient
from test_linux_planner_policy import environment
from test_linux_pool_preparation import Fixture
from test_runtime_integration import ControlClient
from linux_lock_fixture import LockObservationFixture


@pytest.fixture
def linux_lock_observation():
    """Portable Linux fdinfo model; every actual flock/file operation runs."""
    case = unittest.TestCase()
    LockObservationFixture(case)
    try:
        yield
    finally:
        case.doCleanups()


def synthetic_linux_boundaries(fixture, monkeypatch):
    """Keep concrete handles and file checks; simulate unavailable Linux host."""
    identity = environment()
    sources = {'synthetic_test_only': 'fixed source pins'}
    monkeypatch.setattr(preparation, 'SupervisedLinuxNamedPythonBackend', SupervisedLinuxNamedPythonBackend)
    monkeypatch.setattr(preparation, 'LinuxPlannerPolicy', LinuxPlannerPolicy)
    monkeypatch.setattr(preparation, 'prepare_linux_planner_policy', policy_module.prepare_linux_planner_policy)
    monkeypatch.setattr(policy_module, 'linux_execution_identity', lambda: deepcopy(identity))
    monkeypatch.setattr(policy_module, 'linux_source_pins', lambda: deepcopy(sources))
    monkeypatch.setattr(preparation, 'linux_source_pins', lambda: deepcopy(sources))

    def plan(*, root, scripts_root, scripts, interpreter, unshare):
        fixture.events.append('plan')
        inputs = pin_linux_input_plan(root=root, scripts_root=scripts_root, scripts=scripts).document()
        return {'schema': backend_module.PLAN_SCHEMA, 'root': root,
            'root_identity': inputs['root_chain'][-1]['identity'],
            'input_plan': inputs, 'input_plan_sha256': digest(canonical(inputs)),
            'environment': deepcopy(identity), 'sources': deepcopy(sources),
            'interpreter': interpreter, 'interpreter_pin': preparation._executable_pin(interpreter),
            'unshare': unshare, 'unshare_pin': preparation._executable_pin(unshare),
            'runtime': {'synthetic': 'no native runtime launched'},
            'bootstrap_sha256': digest(b'synthetic bootstrap'),
            'seccomp_sha256': digest(b'synthetic seccomp'),
            'imports': ['json', 'pathlib'], 'limits': {'synthetic_test_only': True}}

    def validate_plan(value):
        # Real private source/script/tool pins remain checked on every read.
        original = value['input_plan']
        scripts = {name: {key: item[key] for key in ('path', 'timeout_seconds', 'max_output_bytes')}
                   for name, item in original['scripts'].items()}
        assert pin_linux_input_plan(root=original['root'], scripts_root=original['scripts_root'],
            scripts=scripts).document() == original
        assert value['root'] == original['root']
        assert value['root_identity'] == original['root_chain'][-1]['identity']
        assert value['input_plan_sha256'] == digest(canonical(original))
        assert value['environment'] == identity and value['sources'] == sources
        for tool in ('interpreter', 'unshare'):
            assert preparation._executable_pin(value[tool]) == value[tool + '_pin']
        return value

    monkeypatch.setattr(preparation, 'pin_linux_execution_plan', plan)
    monkeypatch.setattr(backend_module, 'validate_current_plan', validate_plan)

    def qualify_backend(self):
        self._pins()
        assert self._authorize() is True
        fixture.events.append('backend')
        receipt = {'schema': 'blackoutburst-linux-execution-qualification-v1',
            'plan_sha256': self.document['plan_hash'],
            'environment_sha256': digest(canonical(self.environment)),
            'checks': {key: True for key in backend_module.QUALIFICATION_CASES},
            'cleanup_confirmed': True,
            'native_receipts': {key: digest(('synthetic-' + key).encode())
                                for key in backend_module.QUALIFICATION_CASES}}
        self.private.replace('qualification.json', canonical(receipt))
        return receipt

    def qualify_policy(self):
        self._pins()
        fixture.events.append('policy')
        marker = {'schema': policy_module.PLAN_SCHEMA, 'nonce': str(uuid.uuid4()),
            'fingerprint': self.fingerprint, 'environment_sha256': self.environment_sha256,
            'source_pins_sha256': digest(canonical(self._plan['source_pins']))}
        self.private.create('planner-attempt.json', canonical(marker))
        receipt = {key: marker[key] for key in ('schema', 'fingerprint', 'environment_sha256', 'source_pins_sha256')}
        receipt.update(attempt_sha256=digest(canonical(marker)),
            checks={key: True for key in policy_module.CHECKS}, cleanup_confirmed=True)
        self.private.replace('planner-qualification.json', canonical(receipt))
        return receipt

    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', qualify_backend)
    monkeypatch.setattr(LinuxPlannerPolicy, 'qualify', qualify_policy)
    monkeypatch.setattr('blackoutburst_agents.linux_execution_qualification.validate_qualification_evidence',
        lambda concrete, receipt: receipt['plan_sha256'] == concrete.document['plan_hash'])
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'run',
        lambda *_: pytest.fail('checkpoint preparation must not execute a named test'))


@pytest.mark.parametrize('reasoning_effort', [None, 'low'])
def test_real_linux_checkpoint_retirement_and_next_preparation_are_distinct_and_read_only_on_retry(
        tmp_path, monkeypatch, reasoning_effort, linux_lock_observation):
    f = Fixture(tmp_path, monkeypatch)
    synthetic_linux_boundaries(f, monkeypatch)
    with ExitStack() as resources:
        resources.callback(f.pool.close)
        job, ticket, parameters = f.selected()
        parameters['planner_budgets'] = PlannerBudgets(max_wall_seconds=600)
        if reasoning_effort is not None:
            parameters['reasoning_effort'] = reasoning_effort
        first_prepared = f.prepare(job, parameters)
        assert first_prepared['status'] == 'bound'
        directory = f.base/'preparations'/job['id']
        manifest = decode((directory/'broker'/'manifest.json').read_bytes())
        plan = decode((directory/'backend'/'execution-plan.json').read_bytes())
        selection = decode((directory/'selection.json').read_bytes())
        client = FixtureClient(f.board, f.raw, job['session_id'])
        control = ControlClient(f.board, f.raw, client.session_id)
        journal = resources.enter_context(closing(WorkJournal(f.base/'first-work')))
        store = resources.enter_context(closing(RunStore(f.base/'first-runs')))
        runtime = resources.enter_context(closing(RuntimeJournal(f.base/'first-runtime')))
        calls = []
        actions = [dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 1\n'),
                        content='value = 2\n'), dict(schema=1, kind='read', path='one.py')]

        def transport(settings, prompt, workspace, **callbacks):
            callbacks['on_started'](999999999)
            callbacks['before_prompt']()
            sid = callbacks['resume_session_id'] or str(uuid.uuid4())
            callbacks['on_session'](sid)
            calls.append(json.loads(prompt))
            return {'status': 'completed', 'reason': 'verified', 'session_id': sid,
                'text': json.dumps(actions.pop(0)), 'verification': dict(tool_free=True,
                    cleanup_confirmed=True, exit_zero=True, turn_completed=True,
                    session_verified=True, authentication='chatgpt')}

        worker = DevelopmentBoardWorker(journal, store, runtime, client, control,
            backend_factory=SupervisedLinuxNamedPythonBackend, policy_factory=LinuxPlannerPolicy,
            _transport=transport, _test_anchor=f.base/'anchor')
        worker.prepare(scope=selection['scope'], request_id='first-linux-generation',
            instruction=parameters['instruction'], broker_directory=directory/'broker',
            backend_directory=directory/'backend', planner_policy_directory=directory/'policy',
            model=parameters['model'], budgets=parameters['planner_budgets'], operation_id=job['id'],
            **({} if reasoning_effort is None else {'reasoning_effort': reasoning_effort}))
        operation = journal.get(job['id'])
        assert operation['configuration']['execution_profile'] == 'linux-python-execution-v1'
        tests = {'tests': [{'name': parameters['test_name'], 'script_sha256': parameters['verifier_sha256'],
            'timeout_seconds': parameters['test_timeout_seconds'], 'max_output_bytes': parameters['test_max_output_bytes']}],
            'imports': plan['imports'], 'limits': plan['limits']}
        caps = {'paths': sorted(manifest['files']), 'tests': tests, 'actions': manifest['actions'],
            'resource': manifest['resource'], 'broker_limits': manifest['budgets'],
            'planner_limits': operation['configuration']['budgets']}
        configuration = {'schema': 'private-development-checkpoint-config-v1', 'task_id': 'linux-logical',
            'owner_id': 'owner', 'project_id': f.project['id'], 'ticket_id': ticket['id'],
            'repository_id': f.workspaces.grant['repository_id'], 'resource': manifest['resource'],
            'original_public_base': f.commit, 'global_paths': ['one.py'],
            'global_test_caps_sha256': digest(canonical(tests)), 'static_caps_sha256': digest(canonical(caps)),
            'source_release_sha256': 'b'*64}
        checkpoints_root = f.base/'private-checkpoints'
        checkpoints_root.mkdir(mode=0o700)
        checkpoints = PrivateDevelopmentCheckpointController(f.workspaces, checkpoints_root, configuration=configuration)
        registration = operation['payload']['registration']
        scope = {key: selection['scope'][key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id', 'board_endpoint')}
        scope.update(device_id=registration['identity']['device_id'], profile_version=registration['profile_version'],
            resource=manifest['resource'], source_release_sha256='b'*64)
        # Concrete driver/Worker/Proof perform the live checks; these logical
        # fixture callbacks add no authority and launch no work on their own.
        checks = ContinuationChecks(**{name: lambda _: None for name in ('scope', 'claim', 'source', 'fence', 'runtime', 'resume')})
        task = resources.enter_context(closing(ContinuationTask.create(f.base/'logical', logical_task_id='linux-logical',
            scope=scope, initial_source_sha256=digest(canonical({'one.py': digest(b'value = 1\n')})), checks=checks,
            cycle_limits={'max_turns':64, 'max_actions':64, 'max_operations':128, 'max_tests':10,
                'max_read_bytes':10000000, 'max_patch_bytes':1048576, 'max_wall_seconds':600})))
        driver = DevelopmentCycleDriver(task, worker, checkpoints)
        client.release_checkpoint_claim = lambda **values: client.call('release_ticket',
            {**values, 'status': 'todo', 'unassign_self': False})
        start = task.start_cycle(expected_revision=0, request_id='first-linux-cycle')
        result = driver.run_checkpoint(job['id'], start=start, after_actions=2)
        assert result['status'] == 'paused_cycle', result
        checkpoint = result['checkpoint']
        assert checkpoint['original_public_base'] == f.commit
        assert checkpoint['publishable'] is False and checkpoint['launchable'] is False
        assert len(calls) == 2 and not actions
        old_operation = journal.get(job['id'])
        old_documents = {name: (directory/'broker'/name).read_bytes() for name in ('state.json', 'planner.json')}
        old_work = store.status(development_work_id(job['id']))
        old_runtime = [runtime.get(row['id']) for row in runtime.list_status()]
        driver.retire_checkpoint(job['id'])
        assert worker._stopped(job['id'])
        retired_ticket = f.board.get_ticket(f.human, ticket['id'])['ticket']
        assert retired_ticket['status'] == 'todo' and retired_ticket['claimant'] is None

        next_state, next_work = f.base/'next-git-state', f.base/'next-checkouts'
        next_state.mkdir(mode=0o700)
        next_work.mkdir(mode=0o700)
        workspaces = LocalGitWorkspaceController(next_state, grant=LocalRepositoryGrant('owner',
            f.workspaces.grant['repository_id'], checkpoint['checkout'], str(next_work), checkpoint['commit_sha']))
        pool_driver = DevelopmentPoolDriver(f.base/'next-selections', connection_file=f.connection,
            owner_id='owner', agent_id=f.agent, board_endpoint=client.endpoint)
        pool = resources.enter_context(closing(WorkerPool(f.base/'next-pool', concurrency=4,
            drivers={'development': pool_driver.binding()})))
        preparer = preparation.LinuxPoolJobPreparer(f.base/'next-preparations', pool=pool,
            driver=pool_driver, workspaces=workspaces)
        next_job = pool.submit(request_id=str(uuid.uuid4()), driver_id='development',
            payload={'project_id': f.project['id'], 'ticket_id': ticket['id']})
        assert next_job['id'] != job['id'] and next_job['session_id'] != job['session_id']
        prepared = driver.prepare_next(job['id'], preparer=preparer, next_operation_id=next_job['id'], parameters=parameters)
        assert prepared['status'] == 'bound' and prepared['launchable'] is False
        assert prepared['parent_checkpoint_sha256'] == checkpoint['checkpoint_sha256']
        assert prepared['original_public_base'] == f.commit
        assert preparer.status(next_job['id'])['status'] == 'prepared'
        next_directory = f.base/'next-preparations'/next_job['id']
        next_manifest = decode((next_directory/'broker'/'manifest.json').read_bytes())
        next_selection = decode((next_directory/'selection.json').read_bytes())
        assert next_manifest['base_sha'] == checkpoint['commit_sha']
        assert (Path(next_manifest['root'])/'one.py').read_bytes() == b'value = 2\n'
        assert next_manifest['tests'][0]['backend_id'] != manifest['tests'][0]['backend_id']
        assert next_selection.get('reasoning_effort') == reasoning_effort
        assert ('reasoning_effort' in next_selection) is (reasoning_effort is not None)
        assert next_selection['scope']['board_session_id'] == next_job['session_id']
        before_retry = list(f.events)
        monkeypatch.setattr(preparer, 'prepare', lambda *_args, **_kwargs: pytest.fail('retry requalified or cloned'))
        replay = driver.prepare_next(job['id'], preparer=preparer, next_operation_id=next_job['id'], parameters=parameters)
        assert replay == {**prepared, 'historical': True}
        assert f.events == before_retry == ['plan', 'backend', 'policy'] * 2
        assert journal.get(job['id']) == old_operation
        assert {name: (directory/'broker'/name).read_bytes() for name in old_documents} == old_documents
        assert store.status(development_work_id(job['id'])) == old_work
        assert [runtime.get(row['id']) for row in runtime.list_status()] == old_runtime
        assert len(calls) == 2 and f.board.get_runtime(f.human)['active_count'] == 0
        assert pool.status()['capacity_held'] == 0 and pool.status()['driver_calls'] == 0
        current = f.board.get_ticket(f.human, ticket['id'])['ticket']
        # Fresh preparation registrations update agent presence, not the
        # ticket's version, assignment, claim or work state.
        assert {key: value for key, value in current.items() if key != 'assignee_presence'} == {
            key: value for key, value in retired_ticket.items() if key != 'assignee_presence'}
        assert current['claimant'] is None


class _NegativePreparationComplete(BaseException):
    """Leave the shared positive setup after checking the intended refusal."""


@pytest.mark.parametrize('scenario', [
    'stop_before', 'stop_during_qualification', 'verifier_changed',
    'model_changed', 'reasoning_added', 'budgets_increased',
    'scope_revoked', 'dependency_added', 'interrupted_alternative_uuid',
])
def test_next_linux_preparation_cannot_bypass_controls_or_uncertain_intent(
        tmp_path, monkeypatch, linux_lock_observation, scenario):
    """Reuse the genuine first generation; inject only at the next admission."""
    fixtures = []
    boundaries = synthetic_linux_boundaries
    prepare_next = DevelopmentCycleDriver.prepare_next

    def remember_fixture(value, patcher):
        boundaries(value, patcher)
        fixtures.append(value)

    def refuse_next(driver, operation_id, *, preparer, next_operation_id, parameters):
        f = fixtures[-1]
        worker = driver.worker
        operation = worker.journal.get(operation_id)
        old_directory = Path(operation['configuration']['pins']['broker']['path'])
        old_documents = {name: (old_directory/name).read_bytes() for name in ('state.json', 'planner.json')}
        old_work = worker.store.status(development_work_id(operation_id))
        assert len(old_work['turns']) == 2
        old_runtime = [worker.runtime.get(row['id']) for row in worker.runtime.list_status()]
        old_events = list(f.events)
        assert old_events == ['plan', 'backend', 'policy']
        options = dict(preparer=preparer, next_operation_id=next_operation_id, parameters=deepcopy(parameters))
        qualification_entries = []

        def stop_logical():
            state = driver.task.snapshot()['state']
            driver.task.stop(expected_revision=state['revision'], request_id='human-stop-next-linux')

        if scenario == 'stop_before':
            stop_logical()
        elif scenario == 'stop_during_qualification':
            qualify = SupervisedLinuxNamedPythonBackend.qualify

            def stopped_before_qualification(backend):
                qualification_entries.append(len(f.events))
                stop_logical()
                return qualify(backend)

            monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', stopped_before_qualification)
        elif scenario == 'verifier_changed':
            f.verifier.write_bytes(b'from pathlib import Path\nassert False\n')
        elif scenario == 'model_changed':
            options['parameters']['model'] = 'a-different-approved-looking-model'
        elif scenario == 'reasoning_added':
            options['parameters']['reasoning_effort'] = 'low'
        elif scenario == 'budgets_increased':
            budgets = options['parameters']['planner_budgets']
            options['parameters']['planner_budgets'] = replace(budgets, max_actions=budgets.max_actions + 1)
        elif scenario == 'scope_revoked':
            f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
        elif scenario == 'dependency_added':
            dependency = f.board.create_ticket(f.human, f.project['id'], 'Human prerequisite',
                idempotency_key='next-generation-prerequisite')
            current = f.board.get_ticket(f.human, operation['scope']['ticket_id'])['ticket']
            f.board.update_ticket(f.human, current['id'], current['version'], depends_on=[dependency['id']])
        elif scenario == 'interrupted_alternative_uuid':
            qualify = SupervisedLinuxNamedPythonBackend.qualify

            def interrupted(backend):
                backend._pins()
                assert backend._authorize() is True
                qualification_entries.append(len(f.events))
                raise DevelopmentError('synthetic_native_qualification_interrupted')

            monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', interrupted)

        with pytest.raises(DevelopmentError) as refused:
            prepare_next(driver, operation_id, **options)
        if scenario == 'interrupted_alternative_uuid':
            assert refused.value.code == 'synthetic_native_qualification_interrupted'
            assert preparer.status(next_operation_id)['status'] == 'needs_review'
            failure = preparer.status(next_operation_id)['failure']
            assert failure['stage'] == 'backend-qualification'
            assert len(qualification_entries) == 1
            # Neither a retry nor another queued UUID can escape this driver's
            # durable uncertain preparation intent or cause requalification.
            monkeypatch.setattr(SupervisedLinuxNamedPythonBackend, 'qualify', qualify)
            before_retries = list(f.events)
            with pytest.raises(DevelopmentError, match='development_cycle_preparation_uncertain'):
                prepare_next(driver, operation_id, **options)
            alternative = preparer.pool.submit(request_id=str(uuid.uuid4()), driver_id='development',
                payload={'project_id': operation['scope']['project_id'], 'ticket_id': operation['scope']['ticket_id']})
            with pytest.raises(DevelopmentError, match='development_cycle_preparation_conflict'):
                prepare_next(driver, operation_id, **{**options, 'next_operation_id': alternative['id']})
            assert f.events == before_retries
            assert not (Path(preparer.directory)/alternative['id']).exists()
        elif scenario == 'stop_during_qualification':
            assert len(qualification_entries) == 1
            assert f.events[qualification_entries[0]:] == []
            assert not (Path(preparer.directory)/next_operation_id/'complete.json').exists()
        else:
            assert f.events == old_events
            assert not list(Path(preparer.directory).iterdir())
            assert not any(preparer.workspaces.workspace_root.iterdir())

        assert worker.journal.get(operation_id) == operation
        assert {name: (old_directory/name).read_bytes() for name in old_documents} == old_documents
        assert worker.store.status(development_work_id(operation_id)) == old_work
        assert [worker.runtime.get(row['id']) for row in worker.runtime.list_status()] == old_runtime
        assert worker._stopped(operation_id)
        assert f.board.get_runtime(f.human)['active_count'] == 0
        assert preparer.pool.status()['capacity_held'] == 0 and preparer.pool.status()['driver_calls'] == 0
        current = f.board.get_ticket(f.human, operation['scope']['ticket_id'])['ticket']
        assert current['claimant'] is None and current['status'] == 'todo'
        assert not list(Path(preparer.driver.selections.directory).iterdir())
        raise _NegativePreparationComplete()

    monkeypatch.setattr(sys.modules[__name__], 'synthetic_linux_boundaries', remember_fixture)
    monkeypatch.setattr(DevelopmentCycleDriver, 'prepare_next', refuse_next)
    with pytest.raises(_NegativePreparationComplete):
        test_real_linux_checkpoint_retirement_and_next_preparation_are_distinct_and_read_only_on_retry(
            tmp_path, monkeypatch, None, linux_lock_observation)
