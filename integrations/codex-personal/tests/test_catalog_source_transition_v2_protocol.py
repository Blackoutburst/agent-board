"""A new catalogue purpose keeps the two kinds of disposition separate."""
from copy import deepcopy

import pytest

from blackoutburst_agents.catalog_source_transition import proposal, SCHEMA, EXECUTED_SCHEMA
from blackoutburst_agents.host_config import HostError, digest


def value(*, executed=True):
    entry = {'catalog_key': 'primary', 'profile_id': 'profile-a', 'profile_version': 1,
             'mode': 'messages', 'launchspec_sha256': '1' * 64}
    body = {'schema': EXECUTED_SCHEMA if executed else SCHEMA, 'request_key': 'source-next',
            'host_id': 'host-a', 'host_instance': 'host-instance-a',
            'configuration_sha256': 'a' * 64, 'old_release_sha256': 'b' * 64,
            'new_release_sha256': 'c' * 64, 'old_catalog': [entry],
            'new_catalog': [{**entry, 'launchspec_sha256': '2' * 64}],
            'preserved_sha256': 'd' * 64,
            'closures': [{'intent_id': 'legacy-intent', 'request_key': 'legacy-request',
                          'revision': 6, 'proposal_sha256': 'e' * 64}]}
    if executed:
        body['executed_closures'] = [{'intent_id': 'executed-intent', 'request_key': 'executed-request',
                                     'revision': 5, 'proposal_sha256': 'f' * 64}]
    return hashed(body)


def hashed(body):
    body = {key: item for key, item in body.items() if key != 'proposal_sha256'}
    return {**body, 'proposal_sha256': digest(body)}


def test_historical_v1_is_read_without_relabeling_its_legacy_closures():
    original = value(executed=False)
    assert proposal(original) == original
    assert 'executed_closures' not in proposal(original)


def test_v2_retains_both_different_dispositions_with_distinct_revision_contracts():
    original = value()
    result = proposal(original)
    assert result == original and result is not original
    assert result['closures'][0]['revision'] == 6
    assert result['executed_closures'][0]['revision'] == 5


@pytest.mark.parametrize('revision', [True, 0, 4, 6, 5.0, '5'])
def test_executed_closure_requires_its_own_exact_integer_revision(revision):
    body = value()
    body['executed_closures'][0]['revision'] = revision
    with pytest.raises(HostError):
        proposal(hashed(body))


@pytest.mark.parametrize('change', ['duplicate', 'cross-kind', 'unknown-field', 'v1-with-v2-field', 'missing'])
def test_changed_roster_or_schema_is_rejected_even_with_recomputed_body_hash(change):
    body = deepcopy(value())
    if change == 'duplicate':
        body['executed_closures'] *= 2
    elif change == 'cross-kind':
        body['executed_closures'][0]['intent_id'] = body['closures'][0]['intent_id']
    elif change == 'unknown-field':
        body['executed_closures'][0]['cleanup_confirmed'] = True
    elif change == 'v1-with-v2-field':
        body['schema'] = SCHEMA
    else:
        del body['executed_closures']
    with pytest.raises(HostError):
        proposal(hashed(body))
