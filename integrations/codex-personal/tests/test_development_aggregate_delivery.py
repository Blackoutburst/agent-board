"""Two real development generations/Git/Board/Runtime; Linux/model simulated."""
from contextlib import ExitStack, closing
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from blackoutburst_agents.development_aggregate_delivery import AggregateDevelopmentDeliveryController
from blackoutburst_agents.development_cycle_driver import DevelopmentCycleDriver
from blackoutburst_agents.development_delivery import DevelopmentDeliveryProof
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from blackoutburst_agents.git_publication import GitHubDraftPublisher, GitHubPublicationGrant, REPOSITORY, REMOTE, API
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend, NATIVE_SCHEMA, CHECKS, KIND
from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
from blackoutburst_agents.private_development_checkpoint import PrivateDevelopmentCheckpointController
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal
from test_development_worker import FixtureClient
from test_runtime_integration import ControlClient
import test_development_cycle_linux_preparation as linux

linux_lock_observation = linux.linux_lock_observation


class FinishedFixture(BaseException):
    pass


def test_declared_metadata_cannot_construct_an_aggregate_or_publication_authority(tmp_path):
    from blackoutburst_agents.git_publication import _development_authority_pair
    assert _development_authority_pair({'declared': 'authority'}, {'declared': 'test proof'}) is False
    with pytest.raises(DevelopmentError, match='development_aggregate_concrete_handles_required'):
        AggregateDevelopmentDeliveryController(tmp_path/'rejected', task={}, final_proof={},
            checkpoints={}, parent_checkpoint_id=str(uuid.uuid4()), original_workspaces={})
    assert not (tmp_path/'rejected').exists()


def native_receipt(self, request, inputs, script, context, *, qualification, qualification_case=None):
    """Durable synthetic native receipt; real broker admission remains intact."""
    assert qualification is False
    context.before_source()
    assert script == b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n'
    assert (Path(self.plan['root'])/'one.py').read_bytes() == b'value = 3\n'
    nonce, challenge = uuid.uuid4().hex, uuid.uuid4().hex
    plan_sha = self.document['plan_hash']
    native = {'schema': NATIVE_SCHEMA, 'nonce': nonce, 'request_sha256': digest(canonical(request)),
        'plan_sha256': plan_sha, 'source_released': True, 'source_release_intent': True,
        'exit_code': 0, 'reason': 'completed', 'checks': {key: True for key in CHECKS},
        'launcher_attempted': True, 'output_bytes': 0, 'output_sha256': digest(b''),
        'cleanup': {'init_identity_received': True, 'init_terminal': True, 'launcher_reaped': True,
            'cleanup_confirmed': True, 'numeric_pid_signalled': False},
        'identity': {'namespace_identity': [111, 222], 'acquired_by': 'SCM_RIGHTS_validated', 'numeric_pid_signalled': False},
        'broker_snapshot_admitted': False}
    directory = self.private.path/('execution-'+request['operation_id'])
    directory.mkdir(mode=0o700)
    first = {'schema': 1, 'nonce': nonce, 'sequence': 1, 'started_at': 10.0, 'validated_at': 10.1, 'deadline': 11.5}
    window = {**first, 'sequence': 3, 'started_at': 10.2, 'validated_at': 10.3, 'deadline': 11.7}
    job = {'schema': KIND, 'nonce': nonce, 'request': request, 'backend_directory': str(self.private.path),
        'plan_sha256': plan_sha, 'script_sha256': digest(script), 'timeout_seconds': request['timeout_seconds'],
        'max_output_bytes': request['max_output_bytes'], 'control': first, 'qualification': False, 'qualification_case': None}
    with PrivateDirectory(directory) as private:
        private.create('job.json', canonical(job))
        private.create('native-receipt.json', canonical(native))
        private.create('state.json', canonical({'schema': 1, 'nonce': nonce,
            'request_sha256': digest(canonical(request)), 'phase': 'complete', 'boundary_sequence': 2,
            'source_challenge': challenge, 'native_receipt_sha256': digest(canonical(native))}))
        private.create('source-admission.json', canonical({'nonce': nonce,
            'request_sha256': digest(canonical(request)), 'identity': native['identity'],
            'control': window, 'phase': 'source_release_intent'}))
        private.create('source-window.json', canonical({'nonce': nonce, 'challenge': challenge,
            'request_sha256': digest(canonical(request)), 'window_sha256': digest(canonical(window))}))
        private.create('source-window.ready', b'')
    return native


def aggregate_fixture(tmp_path, monkeypatch, lock_observation, callback):
    prepare_next = DevelopmentCycleDriver.prepare_next
    boundaries = linux.synthetic_linux_boundaries
    fixtures = []

    def record(f, patcher):
        boundaries(f, patcher)
        f.verifier.write_bytes(b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n')
        # Both concrete Backend.run and all broker source admission steps run.
        patcher.setattr(SupervisedLinuxNamedPythonBackend, 'run', original_run)
        patcher.setattr(SupervisedLinuxNamedPythonBackend, '_run_job', native_receipt)
        fixtures.append(f)

    original_run = SupervisedLinuxNamedPythonBackend.run

    def second_generation(driver, operation_id, *, preparer, next_operation_id, parameters):
        prepared = prepare_next(driver, operation_id, preparer=preparer,
            next_operation_id=next_operation_id, parameters=parameters)
        f = fixtures[-1]
        parent = driver.checkpoints.verify(prepared['parent_checkpoint_id'])
        directory = Path(preparer.directory)/next_operation_id
        selected = decode((directory/'selection.json').read_bytes())
        with ExitStack() as stack:
            client = FixtureClient(f.board, f.raw, prepared['session_id'])
            control = ControlClient(f.board, f.raw, prepared['session_id'])
            journal = stack.enter_context(closing(WorkJournal(f.base/'final-work')))
            store = stack.enter_context(closing(RunStore(f.base/'final-runs')))
            runtime = stack.enter_context(closing(RuntimeJournal(f.base/'final-runtime')))
            turns = []
            actions = [dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 2\n'), content='value = 3\n'),
                dict(schema=1, kind='test', name=parameters['test_name']), dict(schema=1, kind='final', summary='Fresh final tests')]

            def transport(settings, prompt, workspace, **callbacks):
                callbacks['on_started'](999999999)
                callbacks['before_prompt']()
                sid = callbacks['resume_session_id'] or str(uuid.uuid4())
                callbacks['on_session'](sid)
                turns.append(json.loads(prompt))
                return {'status': 'completed', 'reason': 'verified', 'session_id': sid, 'text': json.dumps(actions.pop(0)),
                    'verification': dict(tool_free=True, cleanup_confirmed=True, exit_zero=True,
                        turn_completed=True, session_verified=True, authentication='chatgpt')}

            worker = DevelopmentBoardWorker(journal, store, runtime, client, control,
                backend_factory=SupervisedLinuxNamedPythonBackend, policy_factory=LinuxPlannerPolicy,
                _transport=transport, _test_anchor=f.base/'anchor')
            worker.prepare(scope=selected['scope'], request_id='final-generation', instruction=parameters['instruction'],
                broker_directory=directory/'broker', backend_directory=directory/'backend',
                planner_policy_directory=directory/'policy', model=parameters['model'],
                budgets=parameters['planner_budgets'], operation_id=next_operation_id)
            checkpoints = PrivateDevelopmentCheckpointController(preparer.workspaces, driver.checkpoints.root,
                configuration=driver.checkpoints.configuration)
            final_driver = DevelopmentCycleDriver(driver.task, worker, checkpoints)
            state = driver.task.snapshot()['state']
            start = driver.task.start_cycle(expected_revision=state['revision'], request_id='final-cycle')
            result = final_driver.run_checkpoint(next_operation_id, start=start, parent_checkpoint_id=parent['checkpoint_id'], after_actions=64)
            assert result['status'] == 'review_ready', result
            assert driver.task.snapshot()['state']['phase'] == 'running'
            proof = DevelopmentDeliveryProof(worker, preparer.workspaces, next_operation_id)
            state_root, work_root = f.base/'original-git-state', f.base/'original-checkouts'
            state_root.mkdir(mode=0o700)
            work_root.mkdir(mode=0o700)
            originals = LocalGitWorkspaceController(state_root, grant=LocalRepositoryGrant('owner',
                f.workspaces.grant['repository_id'], str(f.source), str(work_root), f.commit))
            aggregate = AggregateDevelopmentDeliveryController(f.base/'aggregate', task=driver.task,
                final_proof=proof, checkpoints=checkpoints, parent_checkpoint_id=parent['checkpoint_id'], original_workspaces=originals)
            callback(f, aggregate, worker, directory, turns)
        raise FinishedFixture()

    monkeypatch.setattr(linux, 'synthetic_linux_boundaries', record)
    monkeypatch.setattr(DevelopmentCycleDriver, 'prepare_next', second_generation)
    with pytest.raises(FinishedFixture):
        linux.test_real_linux_checkpoint_retirement_and_next_preparation_are_distinct_and_read_only_on_retry(
            tmp_path, monkeypatch, None, lock_observation)


def test_two_generations_final_native_test_original_base_seal_and_logical_completion(
        tmp_path, monkeypatch, linux_lock_observation):
    def check(f, aggregate, worker, directory, turns):
        old = worker.store.status(aggregate.final_proof.worker.journal.get(aggregate.operation_id)['payload']['parent_binding']['child_scope']['work_id'])
        task = aggregate.task
        assert task.snapshot()['state']['used_total']['turns'] == 2
        seal = aggregate.seal(message='Aggregate from two generations')
        assert seal['base_commit'] == f.commit
        assert aggregate.final_proof.workspaces.grant['base_commit'] != f.commit
        assert len(turns) == 3
        complete = aggregate.complete_local()
        assert complete['historical'] is False and complete['launchable'] is False
        assert task.snapshot()['state']['phase'] == 'complete'
        assert task.snapshot()['state']['used_total']['turns'] == 5
        assert task.snapshot()['state']['current_cycle']['used']['tests'] == 1
        assert aggregate.complete_local() == {**complete, 'historical': True}
        assert aggregate.seal(message='Aggregate from two generations') == {**seal, 'historical': True}
        assert worker.store.status(old['work']['id']) == old
        ticket = f.board.get_ticket(f.human, aggregate.final_proof.scope['ticket_id'])
        assert ticket['ticket']['status'] == 'in_progress' and ticket['ticket']['claimant'] is not None
        assert len(turns) == 3
    aggregate_fixture(tmp_path, monkeypatch, linux_lock_observation, check)


@pytest.mark.parametrize('mutation', ['stop', 'scope', 'selected_source', 'unselected_source', 'mode', 'pending_runtime',
    'fence_changed', 'scope_after_ref', 'lost_seal_ack', 'lost_complete_ack'])
def test_aggregate_refuses_drift_and_retains_ambiguous_effects(
        tmp_path, monkeypatch, linux_lock_observation, mutation):
    def check(f, aggregate, worker, directory, turns):
        operation = worker.journal.get(aggregate.operation_id)
        broker = {name: (directory/'broker'/name).read_bytes() for name in ('state.json', 'planner.json')}
        before_total = deepcopy(aggregate.task.snapshot()['state']['used_total'])
        if mutation == 'stop':
            state = aggregate.task.snapshot()['state']
            aggregate.task.stop(expected_revision=state['revision'], request_id='human-final-stop')
        elif mutation == 'scope':
            f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
        elif mutation == 'selected_source':
            (Path(aggregate.final_proof.workspaces.manifest_inputs(aggregate.operation_id)['root'])/'one.py').write_bytes(b'unverified\n')
        elif mutation == 'unselected_source':
            (Path(aggregate.final_proof.workspaces.manifest_inputs(aggregate.operation_id)['root'])/'outside.txt').write_bytes(b'not declared\n')
        elif mutation == 'mode':
            materialize = aggregate.original_workspaces.materialize
            def changed_mode(operation_id):
                value = materialize(operation_id)
                (Path(value['checkout'])/'one.py').chmod(0o700)
                return value
            monkeypatch.setattr(aggregate.original_workspaces, 'materialize', changed_mode)
        elif mutation == 'pending_runtime':
            config = RuntimeJournal.configuration(operation['scope'], operation['payload']['parent_binding']['child_scope']['work_id'],
                str(uuid.uuid4()), 'unknown_pending')
            worker.runtime.begin(config)
        elif mutation == 'fence_changed':
            with PrivateDirectory(f.base/'anchor') as private:
                fence = decode(private.read('fence.json', 2*1024*1024))
                for row in fence['records'].values():
                    if row['operation_id'] == aggregate.operation_id:
                        row['operation_id'] = str(uuid.uuid4())
                private.replace('fence.json', canonical(fence))
        elif mutation == 'scope_after_ref':
            command = aggregate.original_workspaces._git
            def revoked_after_ref(arguments, **kwargs):
                result = command(arguments, **kwargs)
                if arguments[0] == 'update-ref':
                    f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
                return result
            monkeypatch.setattr(aggregate.original_workspaces, '_git', revoked_after_ref)
        elif mutation == 'lost_seal_ack':
            finalize = aggregate.deliveries.finalize
            calls = []
            def lose(**kwargs):
                calls.append(True)
                finalize(**kwargs)
                raise BaseException('synthetic lost delivery seal ACK')
            monkeypatch.setattr(aggregate.deliveries, 'finalize', lose)
            with pytest.raises(BaseException, match='synthetic lost delivery seal ACK'):
                aggregate.seal(message='Final aggregate')
            with pytest.raises(DevelopmentError, match='development_aggregate_uncertain'):
                aggregate.seal(message='Final aggregate')
            with pytest.raises(DevelopmentError, match='development_aggregate_controller_conflict'):
                AggregateDevelopmentDeliveryController(f.base/'alternative-aggregate', task=aggregate.task,
                    final_proof=aggregate.final_proof, checkpoints=aggregate.checkpoints,
                    parent_checkpoint_id=aggregate.parent_checkpoint_id, original_workspaces=aggregate.original_workspaces)
            assert len(calls) == 1
            value = decode((aggregate.directory/'aggregate.json').read_bytes())
            assert value['phase'] == 'preparing' and value['seal'] is None
        elif mutation == 'lost_complete_ack':
            aggregate.seal(message='Final aggregate')
            checkpoint = aggregate.task.checkpoint
            calls = []
            def lose(**kwargs):
                calls.append(True)
                checkpoint(**kwargs)
                raise BaseException('synthetic lost logical completion ACK')
            monkeypatch.setattr(aggregate.task, 'checkpoint', lose)
            with pytest.raises(BaseException, match='synthetic lost logical completion ACK'):
                aggregate.complete_local()
            monkeypatch.setattr(aggregate.task, 'checkpoint', lambda **_: pytest.fail('completion replayed'))
            result = aggregate.complete_local()
            assert result['launchable'] is False and len(calls) == 1
            assert aggregate.task.snapshot()['state']['used_total']['turns'] == 5
        if mutation not in ('lost_seal_ack', 'lost_complete_ack'):
            with pytest.raises(DevelopmentError):
                aggregate.seal(message='Final aggregate')
        assert worker.journal.get(aggregate.operation_id) == operation
        assert {name: (directory/'broker'/name).read_bytes() for name in broker} == broker
        assert len(turns) == 3
        assert f.board.get_ticket(f.human, operation['scope']['ticket_id'])['ticket']['status'] == 'in_progress'
        if mutation != 'lost_complete_ack':
            assert aggregate.task.snapshot()['state']['used_total'] == before_total
            assert aggregate.task.snapshot()['state']['phase'] != 'complete'
    aggregate_fixture(tmp_path, monkeypatch, linux_lock_observation, check)


def publisher_fixture(f, aggregate, seal):
    root = f.base/'publications'
    root.mkdir(mode=0o700)
    gh = f.base/'approved-gh'
    gh.write_text('#!/bin/sh\nexit 99\n')
    gh.chmod(0o700)
    python = f.base/'approved-python'
    python.write_bytes(Path(sys.executable).resolve().read_bytes())
    python.chmod(0o700)
    remote = f.base/'remote.git'
    from test_git_workspace import git
    git(f.base, 'clone', '--bare', '--no-hardlinks', str(f.source), str(remote))
    grant = GitHubPublicationGrant('owner', REPOSITORY, 'main', f.commit)
    with pytest.raises(DevelopmentError, match='git_publication_authority_required'):
        GitHubDraftPublisher(aggregate.deliveries, root, grant=grant, gh_executable=gh, python_executable=python)
    with pytest.raises(DevelopmentError, match='git_publication_authority_required'):
        GitHubDraftPublisher(aggregate.deliveries, root, grant=grant, gh_executable=gh,
            python_executable=python, authority={'declared': 'not a live authority'})
    publisher = GitHubDraftPublisher(aggregate.deliveries, root, grant=grant, gh_executable=gh,
        python_executable=python, authority=aggregate.publication_authority(grant))
    state = {'pushes': 0, 'posts': 0, 'prs': [], 'during_lookup': None, 'during_repository': None}

    def command(argv, *, cwd, git=False):
        if git:
            assert argv[0] == '/usr/bin/git' and argv[1] in ('ls-remote', 'push') and REMOTE in argv
            if argv[1] == 'push':
                assert publisher._mutation_authority[0] == aggregate.operation_id
                state['pushes'] += 1
            result = subprocess.run([str(remote) if part == REMOTE else part for part in argv], cwd=cwd,
                env={'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
                    'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ALLOW_PROTOCOL': 'file', 'GIT_TERMINAL_PROMPT': '0'},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return result.returncode, result.stdout, result.stderr
        assert argv[0] == str(gh)
        if argv[1:] == ['auth', 'status', '--hostname', 'github.com']:
            return 0, b'synthetic ordinary authentication\n', b''
        assert argv[1:5] == ['api', '--hostname', 'github.com', '--method']
        method, path = argv[5:7]
        if path == 'user':
            value = {'login': 'synthetic-user'}
        elif path == API:
            if state['during_repository']:
                state['during_repository']()
            value = {'full_name': REPOSITORY, 'html_url': 'https://github.com/'+REPOSITORY,
                'permissions': {'push': True}, 'archived': False, 'disabled': False}
        elif method == 'GET' and path.startswith(API+'/pulls?'):
            if state['during_lookup']:
                state['during_lookup']()
            value = state['prs']
        elif method == 'POST' and path == API+'/pulls':
            assert publisher._mutation_authority[0] == aggregate.operation_id and argv[7] == '--input'
            state['posts'] += 1
            payload = json.loads(Path(argv[8]).read_bytes())
            value = {'number': 17, 'html_url': 'https://github.com/'+REPOSITORY+'/pull/17', 'state': 'open', 'draft': True,
                'user': {'login': 'synthetic-user'}, 'title': payload['title'], 'body': payload['body'],
                'head': {'ref': payload['head'], 'sha': seal['delivery_sha'], 'repo': {'full_name': REPOSITORY}},
                'base': {'ref': 'main', 'sha': f.commit, 'repo': {'full_name': REPOSITORY}}}
            state['prs'].append(value)
        else:
            pytest.fail('Unexpected command cannot reach any network')
        return 0, canonical(value), b''
    publisher._command = command
    return publisher, state


@pytest.mark.parametrize('mutation', [None, 'stop_after_complete', 'revoke_before_push', 'revoke_before_post'])
def test_aggregate_publication_uses_fresh_authority_and_distinct_review(
        tmp_path, monkeypatch, linux_lock_observation, mutation):
    def check(f, aggregate, worker, directory, turns):
        seal = aggregate.seal(message='Final aggregate')
        aggregate.complete_local()
        publisher, state = publisher_fixture(f, aggregate, seal)
        if mutation == 'stop_after_complete':
            snap = aggregate.task.snapshot()['state']
            aggregate.task.stop(expected_revision=snap['revision'], request_id='human-stop-before-publish')
        elif mutation == 'revoke_before_push':
            state['during_repository'] = lambda: f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
        elif mutation == 'revoke_before_post':
            state['during_lookup'] = lambda: f.board.set_policy(f.human, True, True, True, False, f.actions, ['repo:fixture'])
        if mutation is not None:
            with pytest.raises(DevelopmentError):
                aggregate.publish_and_handoff(publisher, title='Final aggregate', body='Final source and tests.')
            assert state['posts'] == 0 and state['pushes'] == (1 if mutation == 'revoke_before_post' else 0)
            assert f.board.get_ticket(f.human, aggregate.final_proof.scope['ticket_id'])['ticket']['status'] == 'in_progress'
            return
        result = aggregate.publish_and_handoff(publisher, title='Final aggregate', body='Final source and tests.')
        assert result['status'] == 'awaiting_independent_review' and result['accepted'] is False
        assert state['pushes'] == state['posts'] == 1
        ticket = f.board.get_ticket(f.human, aggregate.final_proof.scope['ticket_id'])['ticket']
        assert ticket['status'] == 'review' and ticket['claimant'] is None and ticket['assignee_agent_id'] is None
        assert f.commit == result['publication']['pull_request']['base_sha']
        author_scope = worker.journal.get(aggregate.operation_id)['scope']
        with pytest.raises(DevelopmentError, match='git_delivery_distinct_reviewer_required'):
            aggregate.deliveries.prepare_review(operation_id=aggregate.operation_id, review_operation_id=str(uuid.uuid4()),
                reviewer_scope=author_scope, context_sha256='d'*64, material='Actual tested aggregate')
        device = f.board.create_device(f.human, 'Distinct reviewer', [])
        raw = f.board.authenticate(device['token'])
        registration = f.board.register_agent(raw, 'aggregate-review-session', 'Reviewer', [], 'codex', False, 'reviewer')
        reviewer = f.board.actor_session(raw, 'aggregate-review-session')
        claimed = f.board.claim_ticket(reviewer, ticket['id'], ticket['version'])
        assert claimed['status'] == 'review' and registration['identity']['id'] != author_scope['agent_id']
        scope = {**author_scope, 'agent_id': registration['identity']['id'], 'board_session_id': 'aggregate-review-session'}
        review_id = str(uuid.uuid4())
        export = aggregate.deliveries.prepare_review(operation_id=aggregate.operation_id, review_operation_id=review_id,
            reviewer_scope=scope, context_sha256='d'*64, material='Actual tested aggregate')
        assert (Path(export['workspace'])/'one.py').read_bytes() == b'value = 3\n'
        assert aggregate.deliveries.review_guard(aggregate.operation_id, review_id, scope=scope,
            selection=export['request']['selection'], context={'fingerprint': 'd'*64},
            material='Actual tested aggregate', workspace=export['workspace']) is True
        assert f.board.get_ticket(f.human, ticket['id'])['ticket']['status'] == 'review'
        assert len(turns) == 3
    aggregate_fixture(tmp_path, monkeypatch, linux_lock_observation, check)
