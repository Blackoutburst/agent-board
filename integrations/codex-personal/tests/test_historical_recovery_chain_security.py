"""Host metadata correlation counter-tests; no constructor/RPC/model is invoked. Parser handles never enter a worker or acquire authority."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import pytest
from blackoutburst_agents.historical_review_source import HistoricalAggregateReviewSource
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.dev_manifest import canonical, digest, DevelopmentError

def write(path, value, *, config=False):
    path = Path(path)
    path.parent.mkdir(mode=448, parents=True, exist_ok=True)
    path.parent.chmod(448)
    path.write_bytes((json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode() if config else canonical(value))
    path.chmod(384)
    return file_document(path)[1]

@pytest.fixture
def pending(tmp_path):
    source = HistoricalAggregateReviewSource.__new__(HistoricalAggregateReviewSource)
    source.prior_recovery = str(uuid.uuid4())
    source.prior = tmp_path / 'review-recoveries' / source.prior_recovery
    source.operation_id, source.prior_job = (str(uuid.uuid4()), str(uuid.uuid4()))
    job = dict(id=source.prior_job, worker_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()))
    source.old_scope = dict(owner_id='owner', project_id='project', ticket_id='ticket', agent_id='review-profile', board_endpoint='https://board.test/mcp', board_session_id=job['session_id'])
    source.receipt = {'delivery_sha': 'd' * 40}
    source.original_frame_sha256 = 'f' * 64
    source_path = tmp_path / 'source' / 'source.json'
    source.source_pin = write(source_path, {'receipt': source.receipt, 'frame_sha256': source.original_frame_sha256})
    transitive = write(tmp_path / 'earlier' / 'immutable-history.json', {'old': 'unchanged'})
    request = dict(schema='linux-cloud-review-recovery-v1', recovery_id=source.prior_recovery, source_operation_id=source.operation_id, expected_source_sha256=source.source_pin['sha256'], delivery_sha=source.receipt['delivery_sha'], reviewer_agent_id=source.old_scope['agent_id'], owner_id='owner', project_id='project', ticket_id='ticket', connection_file=str(tmp_path / 'connections' / 'reviewer.json'), instruction='Une revue complète, avec accents — sans nouvelle autorité')
    config_pin = write(tmp_path / 'config' / 'old.json', request, config=True)
    source.prior_configuration_sha = config_pin['sha256']
    admission = dict(schema='historical-aggregate-review-admission-v1', job=job, scope=source.old_scope, source_sha256=source.source_pin['sha256'], original_frame_sha256=source.original_frame_sha256, delivery_sha=source.receipt['delivery_sha'], pins={'source': source.source_pin}, previous={'kind': 'retired_before_transport', 'files': [transitive]})
    admission_pin = write(source.prior / 'historical-source' / 'admission.json', admission)
    source.old = {'selection': {'operation_id': job['id'], 'worker_id': job['worker_id'], 'session_id': job['session_id']}, 'connection': {'path': request['connection_file']}, 'source': {'admission': deepcopy(admission), 'admission_pin': admission_pin, 'pins': admission['pins']}}
    registry = dict(binding={'request': request, 'configuration': {'configuration': config_pin}}, phase='pending', job=job, result=None, proof=None, queue_ack_sha256=None)
    write(source.prior / 'registry.json', registry)
    source.pins = {}
    return (source, registry, admission, transitive)

def test_pending_dto_preserves_configuration_and_all_transitive_history(pending):
    source, _, _, transitive = pending
    before = {p: p.read_bytes() for p in source.prior.rglob('*') if p.is_file()}
    source._pending_recovery()
    assert source.pins['prior_previous_0'] == transitive
    assert source.pins['prior_source'] == source.source_pin
    assert {p: p.read_bytes() for p in source.prior.rglob('*') if p.is_file()} == before
    assert not (source.prior / 'queue-ack.json').exists()

@pytest.mark.parametrize('fault', ['complete', 'result', 'proof', 'ack_hash', 'extra_registry', 'ack_file', 'different_job', 'different_session', 'different_worker', 'changed_request', 'wrong_config_hash', 'wrong_source', 'wrong_frame', 'wrong_delivery', 'wrong_scope', 'ineligible_previous_kind'])
def test_mismatched_or_adopted_prior_dto_is_rejected(pending, fault):
    source, registry, admission, _ = pending
    if fault == 'complete':
        registry['phase'] = 'complete'
    elif fault in ('result', 'proof'):
        registry[fault] = {'untrusted': True}
    elif fault == 'ack_hash':
        registry['queue_ack_sha256'] = 'a' * 64
    elif fault == 'extra_registry':
        registry['adopted'] = True
    elif fault == 'ack_file':
        write(source.prior / 'queue-ack.json', {'untrusted': True})
    elif fault in ('different_job', 'different_session', 'different_worker'):
        registry['job'][{'different_job': 'id', 'different_session': 'session_id', 'different_worker': 'worker_id'}[fault]] = str(uuid.uuid4())
    elif fault == 'changed_request':
        registry['binding']['request']['instruction'] += ' changed'
    elif fault == 'wrong_config_hash':
        source.prior_configuration_sha = '0' * 64
    else:
        if fault == 'wrong_source':
            admission['source_sha256'] = '0' * 64
        elif fault == 'wrong_frame':
            admission['original_frame_sha256'] = '0' * 64
        elif fault == 'wrong_delivery':
            admission['delivery_sha'] = '0' * 40
        elif fault == 'wrong_scope':
            admission['scope'] = {**admission['scope'], 'ticket_id': 'another-ticket'}
        elif fault == 'ineligible_previous_kind':
            admission['previous']['kind'] = 'finished'
        pin = write(source.prior / 'historical-source/admission.json', admission)
        source.old['source']['admission'] = deepcopy(admission)
        source.old['source']['admission_pin'] = pin
    write(source.prior / 'registry.json', registry)
    with pytest.raises(DevelopmentError):
        source._pending_recovery()

@pytest.fixture
def gate(pending):
    source, _, _, _ = pending
    source.previous = {'kind': 'retired_pending_recovery_before_transport'}
    source.previous_runtime = {'only': 'exact original runtime record'}
    source.scope = dict(source.old_scope, board_session_id=str(uuid.uuid4()))
    source.expected_ticket_version, source.revocation_event_id = (8, 500)
    source.old['selection']['registration'] = {'profile': {'version': 5}}
    write(source.prior / 'lifecycle' / source.prior_job / 'claimed.json', {'version': 7, 'claimant': {'session_id': source.old_scope['board_session_id']}})
    ticket = {'version': 8, 'claimant': None}
    event = dict(id=500, event='managed_agent_claim_released', ticket_id='ticket', project_id='project', actor={'kind': 'agent', 'owner_id': 'owner', 'agent_id': 'manager-profile'}, detail={'source_session_id': source.old_scope['board_session_id'], 'target_profile_id': 'review-profile', 'source_profile_version': 5, 'expected_version': 7, 'version': 8, 'manager_profile_id': 'manager-profile'})
    calls = []

    def call(name, args):
        calls.append((name, deepcopy(args)))
        assert name == 'list_activity'
        return {'items': [deepcopy(event)]}

    def control(record):
        assert record is source.previous_runtime
        calls.append(('lookup_exact_old', None))
        return {'found': False}
    source._old_control = control
    return (source, SimpleNamespace(call=call), ticket, event, calls)

def test_gate_proves_release_then_reads_exact_old_runtime_without_any_replay(gate):
    source, client, ticket, _, calls = gate
    result = source._gate(client, ticket)
    assert result['event_id'] == 500 and result['ticket_version'] == 8
    assert calls == [('list_activity', {'ticket_id': 'ticket', 'after': 499, 'limit': 1, 'order': 'asc'}), ('lookup_exact_old', None)]

@pytest.mark.parametrize('fault', ['stale_event', 'wrong_event', 'wrong_ticket', 'wrong_project', 'wrong_manager', 'human_actor', 'other_owner', 'wrong_old_session', 'wrong_old_profile', 'wrong_profile_version', 'wrong_from_version', 'wrong_to_version', 'reclaimed_ticket', 'runtime_found'])
def test_gate_refuses_unrelated_release_or_nonnegative_old_lookup(gate, fault):
    source, client, ticket, event, calls = gate
    if fault == 'stale_event':
        event['id'] -= 1
    elif fault == 'wrong_event':
        event['event'] = 'ticket_claim_released'
    elif fault == 'wrong_ticket':
        event['ticket_id'] = 'other'
    elif fault == 'wrong_project':
        event['project_id'] = 'other'
    elif fault == 'wrong_manager':
        event['detail']['manager_profile_id'] = 'other'
    elif fault == 'human_actor':
        event['actor']['kind'] = 'human'
    elif fault == 'other_owner':
        event['actor']['owner_id'] = 'other'
    elif fault == 'wrong_old_session':
        event['detail']['source_session_id'] = str(uuid.uuid4())
    elif fault == 'wrong_old_profile':
        event['detail']['target_profile_id'] = 'other'
    elif fault == 'wrong_profile_version':
        event['detail']['source_profile_version'] = 6
    elif fault == 'wrong_from_version':
        event['detail']['expected_version'] = 6
    elif fault == 'wrong_to_version':
        event['detail']['version'] = 9
    elif fault == 'reclaimed_ticket':
        ticket['claimant'] = {'session_id': 'unrelated'}
    else:
        source._old_control = lambda _record: {'found': True, 'execution': {'id': 'untrusted'}}
    with pytest.raises(DevelopmentError):
        source._gate(client, ticket)
    if fault != 'runtime_found':
        assert not any((call[0] == 'lookup_exact_old' for call in calls))
