"""Queued continuation against real Board/Git/Worker/Core/Runtime journals.

Only Linux host qualification, the provider CLI and the connection transport are
the existing fixture seams. Every selected claim uses the real Board API.
"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from blackoutburst_agents import continuation_pool_service as service_module
from blackoutburst_agents.continuation import ContinuationChecks, ContinuationTask
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import canonical, digest
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.development_pool_selection import file_document
from test_continuation_pool_service_integration import actual_service, linux_lock_observation
from test_continuation_pool_service_integration import (
    test_actual_final_delivery_uses_original_base_and_releases_for_independent_review as _actual_final_delivery)
from test_development_worker import FixtureClient


@pytest.fixture
def queued_service(actual_service, monkeypatch):
    f, root, task_id, ticket, construct, calls, sequence = actual_service
    assert hasattr(f.board, 'claim_selected_queue_ticket'), 'Requires the actual combined Board queue API'
    profile = f.board.get_agent_profile(f.human, f.agent)
    if profile['policy']['autonomy'] != 'queue':
        profile = f.board.update_agent_profile(f.human, profile['id'], profile['version'],
            policy={**profile['policy'], 'autonomy': 'queue'})
    path = root / 'config/batch.json'; batch = decode(path.read_bytes())
    batch['schema'] = 'linux-finite-pool-batch-v3'
    admission = dict(schema='development-queue-admission-v1', ticket_version=ticket['version'],
        profile_version=profile['version'], catalog_sha256=digest(b'explicit synthetic queue catalog'))
    batch['jobs'][0]['queue_admission'] = admission
    path.write_bytes(canonical(batch))
    f.queue_clients = []; f.queue_claims = []; f.queue_lose_once = False

    def connected(path, *, session_id, timeout, queue_admission=False):
        client = FixtureClient(f.board, f.raw, session_id)
        client._queue_admission = queue_admission
        client.connection_identity = tuple(file_document(path)[1]['identity'])
        client.close = lambda: None

        def register():
            client.register_count += 1
            value = f.board.register_agent(f.raw, session_id, 'Queue continuation fixture', [],
                'codex', queue_admission, 'worker')
            client.actor = f.board.actor_session(f.raw, session_id)
            client.registration = value
            return value

        client.register_initial = register
        client.adopt_registration = lambda: setattr(client, 'actor', f.board.actor_session(f.raw, session_id))
        client.release_checkpoint_claim = lambda **values: client.call('release_ticket',
            {**values, 'status': 'todo', 'unassign_self': False})
        original = client.call

        def call(name, values):
            assert name != 'claim_ticket', 'Queue continuation must never fall back to manual claim'
            if name == 'claim_selected_queue_ticket':
                assert queue_admission is True
                f.queue_claims.append(dict(session_id=session_id, **deepcopy(values)))
                if f.queue_lose_once:
                    f.queue_lose_once = False
                    client.lost.add(name)  # Lose the ACK only after the real Board transaction.
            return original(name, values)

        client.call = call; f.queue_clients.append(client)
        return client

    monkeypatch.setattr(DevelopmentBoardClient, 'from_connection_file', connected)
    yield f, root, task_id, ticket, construct, calls, sequence, admission


def target(root, task_id):
    return root / 'continuations' / digest(canonical({'owner_id': 'owner', 'logical_task_id': task_id}))


def metadata(directory, operation_id):
    matches = [(path, decode((path / 'generation.json').read_bytes()))
        for path in (directory / 'generations').iterdir() if (path / 'generation.json').is_file()]
    return next((path, value) for path, value in matches if value['submitted']['id'] == operation_id)


def scheduled(directory):
    # Scheduling is never authority. The actual provider observation, retirement,
    # fresh Board state and selected claim still run.
    with PrivateDirectory(directory) as private:
        value = decode(private.read('cursor.json', 512 * 1024))
        private.replace('cursor.json', canonical({**value, 'not_before': 1.}))


def test_two_queued_generations_use_fresh_selected_cas_and_preserve_old_history(queued_service):
    f, root, task_id, ticket, construct, calls, sequence, original = queued_service
    batch_before = (root / 'config/batch.json').read_bytes()
    f.queue_lose_once = True
    first = construct().run_once()
    assert first['tasks'][0]['phase'] == 'paused_cycle', first
    first_op = first['tasks'][0]['operation_id']; directory = target(root, task_id)
    first_root, first_metadata = metadata(directory, first_op)
    assert first_metadata['queue_admission'] == original
    after_first = f.board.get_ticket(f.human, ticket['id'])['ticket']
    assert after_first['status'] == 'todo' and after_first['claimant'] is None
    assert len(f.queue_claims) == 1  # The lost ACK is confirmed by exact fresh claim, no new mutation.
    old = {str(path): path.read_bytes() for path in first_root.rglob('*') if path.is_file()}
    sequence.write_text(json.dumps([
        dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 2\n'), content='value = 3\n'),
        dict(schema=1, kind='read', path='one.py')]))
    second = construct().run_once()
    assert second['tasks'][0]['phase'] == 'paused_cycle', second
    second_op = second['tasks'][0]['operation_id']; second_root, next_metadata = metadata(directory, second_op)
    assert second_op != first_op and second_root != first_root
    assert next_metadata['queue_admission'] == {**original, 'ticket_version': after_first['version']}
    assert next_metadata['queue_admission']['ticket_version'] > original['ticket_version']
    assert first_metadata['submitted']['session_id'] != next_metadata['submitted']['session_id']
    assert [row['expected_version'] for row in f.queue_claims] == [original['ticket_version'], after_first['version']]
    assert [row['idempotency_key'] for row in f.queue_claims] == [
        'development-queue-claim-' + first_op, 'development-queue-claim-' + second_op]
    assert len(calls.read_text().splitlines()) == 4 and json.loads(sequence.read_text()) == []
    assert all(Path(path).read_bytes() == value for path, value in old.items())
    with ContinuationJournal.open(directory / 'logical') as journal:
        state = journal.snapshot()['state']
        assert state['phase'] == 'paused_cycle' and state['cycle_seq'] == 2
        assert state['used_total']['turns'] == 4 and state['used_total']['actions'] == 4
    with f.board._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM idempotency WHERE operation='claim_selected_queue_ticket'").fetchone()[0] == 2
    assert f.board.get_runtime(f.human)['active_count'] == 0
    assert (root / 'config/batch.json').read_bytes() == batch_before
    assert decode((root / 'config/batch.json').read_bytes())['jobs'][0]['queue_admission'] == original


def test_queued_provider_wake_reuses_exact_prepared_candidate_without_reclaim(queued_service, monkeypatch):
    from blackoutburst_agents import provider_disposition
    from test_provider_disposition import usage
    f, root, task_id, ticket, construct, calls, sequence, original = queued_service
    sequence.write_text(json.dumps([None]))
    first = construct().run_once()
    assert first['tasks'][0]['phase'] == 'paused_provider', first
    directory = target(root, task_id); first_op = first['tasks'][0]['operation_id']
    released = f.board.get_ticket(f.human, ticket['id'])['ticket']
    observations = []
    def available_except_after_preparation(*args, **kwargs):
        observations.append(len(observations) + 1)
        return usage(len(observations) != 3)
    monkeypatch.setattr(provider_disposition, 'read_usage', available_except_after_preparation)
    scheduled(directory); second = construct().run_once()
    assert second['tasks'][0]['phase'] == 'paused_provider', second
    with PrivateDirectory(directory) as private:
        waiting = decode(private.read('cursor.json', 512 * 1024))
    assert waiting['generation'] != waiting['wake_generation']
    waiting_root = directory / 'generations' / waiting['wake_generation']
    candidate_before = decode((waiting_root / 'generation.json').read_bytes())
    assert candidate_before['queue_admission'] == {**original, 'ticket_version': released['version']}
    assert len(f.queue_claims) == 1 and len(calls.read_text().splitlines()) == 1
    assert f.board.get_ticket(f.human, ticket['id'])['ticket']['claimant'] is None
    sequence.write_text(json.dumps([
        dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 1\n'), content='value = 2\n'),
        dict(schema=1, kind='read', path='one.py')]))
    scheduled(directory); third = construct().run_once()
    assert third['tasks'][0]['phase'] == 'paused_cycle', third
    assert third['tasks'][0]['operation_id'] == candidate_before['submitted']['id'] != first_op
    assert decode((waiting_root / 'generation.json').read_bytes()) == candidate_before
    assert len(list((directory / 'generations').iterdir())) == 2
    assert len(f.queue_claims) == 2 and f.queue_claims[-1]['expected_version'] == released['version']
    assert len(calls.read_text().splitlines()) == 3 and f.board.get_runtime(f.human)['active_count'] == 0


@pytest.mark.parametrize('change', ['logical_stop', 'profile', 'source'])
def test_queued_continuation_refuses_drift_before_another_claim_or_model(queued_service, monkeypatch, change):
    f, root, task_id, ticket, construct, calls, sequence, _ = queued_service
    first = construct().run_once(); assert first['tasks'][0]['phase'] == 'paused_cycle', first
    directory = target(root, task_id); released = f.board.get_ticket(f.human, ticket['id'])['ticket']
    sequence.write_text(json.dumps([dict(schema=1, kind='read', path='one.py')]))
    if change == 'logical_stop':
        def no_grant(*args): raise AssertionError('Logical Stop never needs an admission grant')
        with ContinuationJournal.open(directory / 'logical') as journal:
            task = ContinuationTask(journal, ContinuationChecks(**{
                key: no_grant for key in ('scope', 'claim', 'source', 'fence', 'runtime', 'resume')}))
            task.stop(expected_revision=task.snapshot()['state']['revision'], request_id='human-queue-stop')
    elif change == 'profile':
        profile = f.board.get_agent_profile(f.human, f.agent)
        f.board.update_agent_profile(f.human, profile['id'], profile['version'],
            policy={**profile['policy'], 'resources': []})
    else:
        monkeypatch.setattr(service_module, 'linux_source_pins', lambda: {'synthetic_test_only': 'changed source release'})
    second = construct().run_once()
    assert second['tasks'][0]['phase'] == ('stopped' if change == 'logical_stop' else 'needs_you'), second
    assert len(f.queue_claims) == 1 and len(calls.read_text().splitlines()) == 2
    current = f.board.get_ticket(f.human, ticket['id'])['ticket']
    assert current['version'] == released['version'] and current['claimant'] is None and current['status'] == 'todo'
    assert f.board.get_runtime(f.human)['active_count'] == 0


def test_queued_generations_reach_real_aggregate_publication_and_independent_review(queued_service, monkeypatch):
    # Reuse the existing full proof/Git/Board lifecycle scenario. Its only
    # substitutions are the same local CLI/native/remote seams; queue admission
    # remains the actual Board transaction from this fixture.
    _actual_final_delivery(queued_service[:-1], monkeypatch, generations=2)
    f, root, task_id, _, _, _, _, initial = queued_service
    assert len(f.queue_claims) == 2
    assert f.queue_claims[0]['expected_version'] == initial['ticket_version']
    assert f.queue_claims[1]['expected_version'] > f.queue_claims[0]['expected_version']
    assert len({row['session_id'] for row in f.queue_claims}) == 2
    assert len({row['idempotency_key'] for row in f.queue_claims}) == 2
    assert len(f.review_turns) == 1
    with ContinuationJournal.open(target(root, task_id) / 'logical') as journal:
        state = journal.snapshot()['state']
        assert state['phase'] == 'complete' and state['used_total']['turns'] == 6
