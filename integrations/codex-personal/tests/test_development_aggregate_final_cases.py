"""Closed first-generation/provider-intent cases; actual final delivery proof.

Provider envelopes here are schema fixtures over a healthy ordinary final run.
The actual ProviderSession producer is tested separately in its own component;
this never presents an archived failed turn as a real DeliveryProof.
"""
import json
from pathlib import Path
import uuid

import pytest

from blackoutburst_agents.development_aggregate_delivery import AggregateDevelopmentDeliveryController
from blackoutburst_agents.development_cycle_driver import DevelopmentCycleDriver
from blackoutburst_agents.development_delivery import DevelopmentDeliveryProof
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
from test_development_aggregate_delivery import FinishedFixture, native_receipt, aggregate_fixture
import test_development_cycle_linux_preparation as linux

linux_lock_observation = linux.linux_lock_observation


@pytest.mark.parametrize('envelope,mode,allowed', [
    ('normal', 'normal', True),
    ('provider', 'normal', False),
    ('provider', 'provider_healthy_v1', True),
    ('unknown_provider', 'provider_healthy_v1', False),
    ('failed_turn', 'provider_healthy_v1', False),
])
def test_first_generation_final_and_explicit_healthy_provider_intent_are_closed(
        tmp_path, monkeypatch, linux_lock_observation, envelope, mode, allowed):
    original_run = SupervisedLinuxNamedPythonBackend.run
    boundaries = linux.synthetic_linux_boundaries
    run_checkpoint = DevelopmentCycleDriver.run_checkpoint
    fixtures = []

    def record(f, patcher):
        boundaries(f, patcher)
        f.verifier.write_bytes(b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n')
        patcher.setattr(SupervisedLinuxNamedPythonBackend, 'run', original_run)
        patcher.setattr(SupervisedLinuxNamedPythonBackend, '_run_job', native_receipt)
        fixtures.append(f)

    def first_final(driver, operation_id, *, start, **_options):
        f = fixtures[-1]
        turns = []
        actions = [dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 1\n'), content='value = 3\n'),
            dict(schema=1, kind='test', name='approved'), dict(schema=1, kind='final', summary='Final first generation')]
        def transport(settings, prompt, workspace, **callbacks):
            callbacks['on_started'](999999999)
            callbacks['before_prompt']()
            sid = callbacks['resume_session_id'] or str(uuid.uuid4())
            callbacks['on_session'](sid)
            turns.append(prompt)
            return {'status': 'completed', 'reason': 'verified', 'session_id': sid, 'text': json.dumps(actions.pop(0)),
                'verification': dict(tool_free=True, cleanup_confirmed=True, exit_zero=True,
                    turn_completed=True, session_verified=True, authentication='chatgpt')}
        driver.worker.transport = transport
        result = run_checkpoint(driver, operation_id, start=start, after_actions=64)
        assert result['status'] == 'review_ready' and driver.task.snapshot()['state']['checkpoint'] is None
        worker = driver.worker
        if envelope != 'normal':
            with PrivateDirectory(worker.journal.directory) as private:
                name = 'development-checkpoint-intent-'+operation_id+'.json'
                intent = decode(private.read(name, 128*1024))
                intent.update(schema='development-worker-provider-checkpoint-intent-v1',
                    provider_mode='unknown' if envelope == 'unknown_provider' else 'terminal-failure-v1')
                private.replace(name, canonical(intent))
        if envelope == 'failed_turn':
            # Corruption counter: even a caller-declared healthy intent cannot
            # overcome the real DeliveryProof's completed-turn requirements.
            worker.store._db.execute("UPDATE turns SET status='blocked',requires_review=1 WHERE id=(SELECT id FROM turns LIMIT 1)")
        proof = DevelopmentDeliveryProof(worker, f.workspaces, operation_id)
        state, work = f.base/'original-state', f.base/'original-work'
        state.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        original = LocalGitWorkspaceController(state, grant=LocalRepositoryGrant('owner',
            f.workspaces.grant['repository_id'], str(f.source), str(work), f.commit))
        def finish():
            aggregate = AggregateDevelopmentDeliveryController(f.base/'first-aggregate', task=driver.task,
                final_proof=proof, checkpoints=driver.checkpoints, parent_checkpoint_id=None,
                original_workspaces=original, final_intent_mode=mode)
            seal = aggregate.seal(message='Original first-generation delivery')
            assert seal['base_commit'] == f.commit
            aggregate.complete_local()
            assert driver.task.snapshot()['state']['phase'] == 'complete'
            assert driver.task.snapshot()['state']['used_total']['turns'] == 3
            assert driver.task.snapshot()['state']['current_cycle']['used']['tests'] == 1
        if allowed:
            finish()
        else:
            with pytest.raises(DevelopmentError):
                finish()
            assert driver.task.snapshot()['state']['phase'] != 'complete'
        assert len(turns) == 3
        assert f.board.get_ticket(f.human, proof.scope['ticket_id'])['ticket']['status'] == 'in_progress'
        raise FinishedFixture()

    monkeypatch.setattr(linux, 'synthetic_linux_boundaries', record)
    monkeypatch.setattr(DevelopmentCycleDriver, 'run_checkpoint', first_final)
    with pytest.raises(FinishedFixture):
        linux.test_real_linux_checkpoint_retirement_and_next_preparation_are_distinct_and_read_only_on_retry(
            tmp_path, monkeypatch, None, linux_lock_observation)


def test_existing_logical_parent_cannot_be_omitted_to_rebase_history(
        tmp_path, monkeypatch, linux_lock_observation):
    def check(f, aggregate, worker, directory, turns):
        with pytest.raises(DevelopmentError):
            AggregateDevelopmentDeliveryController(f.base/'wrong-parent', task=aggregate.task,
                final_proof=aggregate.final_proof, checkpoints=aggregate.checkpoints,
                parent_checkpoint_id=None, original_workspaces=aggregate.original_workspaces)
        assert len(turns) == 3 and aggregate.task.snapshot()['state']['phase'] == 'running'
    aggregate_fixture(tmp_path, monkeypatch, linux_lock_observation, check)
