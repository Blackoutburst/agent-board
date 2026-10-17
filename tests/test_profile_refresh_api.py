"""Refresh through the real authenticated MCP boundary, without a model process."""
import pytest

from test_runtime_api import browser, rpc
from test_profile_refresh import EVIDENCE, snapshot


def call(browser, name, values, *, error=False):
    return rpc(browser, name, values | {'view': 'full'}, error=error)


@pytest.fixture
def pending(browser):
    _, b, human, _, project, _, profile_id = browser
    profile = b.get_agent_profile(human, profile_id)
    b.update_agent_profile(human, profile_id, profile['version'], description='Updated descriptive field')
    plan = call(browser, 'inspect_profile_refresh', {})
    values = dict(from_profile_version=plan['from_profile_version'], expected_profile_version=plan['to_profile_version'],
        configuration_sha256=plan['configuration_sha256'], expected_claims=plan['claims'],
        idempotency_key='api-refresh', recovery_evidence=EVIDENCE)
    return browser, plan, values


def test_native_mcp_refresh_retains_claim_with_exact_versions_and_durable_retry(pending):
    browser, plan, values = pending
    _, b, human, _, _, ticket, _ = browser
    assert plan['mode'] == 'preserve_claims'
    rpc(browser, 'get_ticket', {'ticket_id': ticket['id']}, error=True)
    rpc(browser, 'register_agent', {'label': 'No silent acknowledgement', 'agent_kind': 'codex',
        'identity_key': 'fixture'}, error=True)
    result = call(browser, 'refresh_agent_session', values)
    retained = b.get_ticket(human, ticket['id'])['ticket']
    assert retained['claimant']['session_id'] == 'runtime-api'
    assert retained['claimant']['profile_version'] == result['profile_version']
    before = snapshot(b)
    assert call(browser, 'refresh_agent_session', values) == result
    assert snapshot(b) == before
    fresh = rpc(browser, 'get_ticket', {'ticket_id': ticket['id']})['ticket']
    assert fresh['version'] == retained['version']


@pytest.mark.parametrize('field,value', [
    ('from_profile_version', True), ('from_profile_version', 1.0), ('from_profile_version', '1'),
    ('expected_profile_version', 0), ('configuration_sha256', 'x' * 64),
    ('expected_claims', [{'kind': 'ticket', 'identifier': 'id', 'version': True}]),
    ('expected_claims', [{'kind': 'ticket', 'identifier': '../private', 'version': 1}]),
    ('expected_claims', [{'kind': 'ticket', 'identifier': 'id', 'version': 1, 'extra': True}]),
    ('expected_claims', None), ('idempotency_key', ''), ('idempotency_key', 'key\nsecret'),
    ('recovery_evidence', True), ('recovery_evidence', ''), ('recovery_evidence', None),
    ('session_id', True), ('session_id', '../private')])
def test_strict_mcp_payload_validation_leaves_database_unchanged(pending, field, value):
    browser, _, values = pending
    before = snapshot(browser[1])
    call(browser, 'refresh_agent_session', values | {field: value}, error=True)
    assert snapshot(browser[1]) == before


def test_mcp_old_session_can_cleanup_but_refresh_waits_for_runtime_receipt(browser):
    _, b, human, _, project, ticket, profile_id = browser
    execution = rpc(browser, 'reserve_execution', dict(idempotency_key='old', project_id=project['id'],
        ticket_id=ticket['id'], work_id='work', turn_id='turn', provider='codex_chatgpt', model='fixture'))['execution']
    profile = b.get_agent_profile(human, profile_id)
    b.update_agent_profile(human, profile_id, profile['version'], instructions='Updated instructions')
    plan = call(browser, 'inspect_profile_refresh', {})
    values = dict(from_profile_version=plan['from_profile_version'], expected_profile_version=plan['to_profile_version'],
        configuration_sha256=plan['configuration_sha256'], expected_claims=plan['claims'],
        idempotency_key='api-refresh', recovery_evidence=EVIDENCE)
    call(browser, 'refresh_agent_session', values, error=True)
    rpc(browser, 'finish_execution', dict(execution_id=execution['id'], expected_revision=execution['revision'],
        status='cancelled', cleanup_confirmed=True, reason='policy_changed'))
    result = call(browser, 'refresh_agent_session', values)
    assert result['mode'] == 'release_claims' and result['requires_new_model_operation']
    assert b.get_ticket(human, ticket['id'])['ticket']['claimant'] is None


def test_inspection_cannot_select_another_devices_session(pending):
    browser, _, _ = pending
    _, b, human, _, _, _, _ = browser
    device = b.create_device(human, 'Other device', [])
    b.register_agent(b.authenticate(device['token']), 'foreign-device-session', 'Other', [], 'codex', False, 'other')
    before = snapshot(b)
    call(browser, 'inspect_profile_refresh', {'session_id': 'foreign-device-session'}, error=True)
    assert snapshot(b) == before
