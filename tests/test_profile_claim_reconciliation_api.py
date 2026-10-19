"""Exercise stale-profile recovery through the authenticated MCP boundary."""
import json
import sqlite3

import pytest

from board import BoardError
from test_coordination import database_snapshot
from test_runtime_api import browser, rpc


EVIDENCE = 'Checked the old local process and remote effects; no write is pending.'


@pytest.fixture
def recovery(browser):
    _, board, human, token, _, ticket, profile_id = browser
    old = board.actor_session(board.authenticate(token), 'runtime-api')
    claimed = board.get_ticket(human, ticket['id'])['ticket']
    blocked = board.update_ticket(old, ticket['id'], claimed['version'], status='blocked',
        blocked_reason='Waiting for the revised profile instructions', progress='Implementation retained', outcome='Existing result', evidence='Existing verification')
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], instructions='Revised instructions')
    with sqlite3.connect(board.db_path) as db:
        db.execute('UPDATE tickets SET lease_expires_at=0 WHERE id=?', (ticket['id'],))
    return browser, blocked


def register_current(browser):
    return rpc(browser, 'register_agent', {'session_id': 'recovery-api', 'label': 'Recovery only',
        'skills': [], 'agent_kind': 'codex', 'autonomous': False, 'identity_key': 'fixture'})


def arguments(blocked, **changes):
    return {'session_id': 'recovery-api', 'ticket_id': blocked['id'],
        'expected_version': blocked['version'], 'idempotency_key': 'recover-profile',
        'recovery_evidence': EVIDENCE, **changes}


def test_new_session_can_release_old_blocked_claim_but_cannot_resume_implicitly(recovery):
    browser, blocked = recovery
    _, board, human, _, _, _, profile_id = browser
    # Existing authorization fences remain unchanged for the stale session.
    rpc(browser, 'get_ticket', {'ticket_id': blocked['id']}, error=True)
    rpc(browser, 'register_agent', {'label': 'Old session cannot refresh past its claim',
        'skills': [], 'agent_kind': 'codex', 'autonomous': False, 'identity_key': 'fixture'}, error=True)
    registration = register_current(browser)
    assert registration['identity']['id'] == profile_id
    assert registration['session']['autonomous'] is False
    rpc(browser, 'claim_ticket', {'session_id': 'recovery-api', 'ticket_id': blocked['id'],
        'expected_version': blocked['version'], 'recovery_evidence': EVIDENCE}, error=True)
    rpc(browser, 'release_ticket', {'session_id': 'recovery-api', 'ticket_id': blocked['id'],
        'expected_version': blocked['version'], 'idempotency_key': 'ordinary-release',
        'reason': 'Ordinary release must not acquire a cross-session exception'}, error=True)

    released = rpc(browser, 'reconcile_profile_claim', arguments(blocked))
    assert released['status'] == 'blocked' and released['claimant'] is None
    assert released['lease_expires_at'] is None
    assert released['version'] == blocked['version'] + 1
    assert released['assignee_agent_id'] == profile_id
    for key in ('progress', 'outcome', 'evidence', 'assignee_owner_id', 'depends_on', 'human_requests'):
        assert released[key] == blocked[key]
    assert board.get_runtime(human)['executions'] == []

    # The release has not executed, completed or made a manual block claimable.
    rpc(browser, 'claim_ticket', {'session_id': 'recovery-api', 'ticket_id': blocked['id'],
        'expected_version': released['version']}, error=True)
    ready = rpc(browser, 'release_ticket', {'session_id': 'recovery-api', 'ticket_id': released['id'],
        'expected_version': released['version'], 'idempotency_key': 'explicit-status-correction',
        'reason': 'The manual blocker has been resolved', 'status': 'todo'})
    claimed = rpc(browser, 'claim_ticket', {'session_id': 'recovery-api', 'ticket_id': ready['id'],
        'expected_version': ready['version']})
    before = database_snapshot(board)
    retried = rpc(browser, 'reconcile_profile_claim', arguments(blocked))
    assert retried == claimed
    assert retried['claimant']['session_id'] == 'recovery-api'
    assert database_snapshot(board) == before
    with sqlite3.connect(board.db_path) as db:
        detail = db.execute("SELECT detail FROM ticket_events WHERE ticket_id=? AND event='ticket_profile_claim_reconciled'",
                            (blocked['id'],)).fetchall()
    assert len(detail) == 1
    audit = json.loads(detail[0][0])
    assert audit['source_session_id'] == 'runtime-api'
    assert audit['source_profile_version'] < audit['profile_version']
    assert audit['recovery_evidence'] == EVIDENCE


@pytest.mark.parametrize('field,value', [
    ('expected_version', True), ('expected_version', 1.0), ('expected_version', '1'),
    ('expected_version', 0), ('expected_version', -1), ('expected_version', None),
    ('idempotency_key', True), ('idempotency_key', None), ('idempotency_key', ''),
    ('idempotency_key', 'x' * 201), ('idempotency_key', 'key\nsecret'),
    ('ticket_id', 1), ('ticket_id', '/tmp/private'), ('session_id', True),
    ('recovery_evidence', True), ('recovery_evidence', None), ('recovery_evidence', {}),
    ('recovery_evidence', ''), ('recovery_evidence', ' \n '), ('recovery_evidence', 'x' * 20001),
])
def test_mcp_recovery_has_strict_types_bounds_and_no_effect_on_invalid_payload(recovery, field, value):
    browser, blocked = recovery
    register_current(browser)
    before = database_snapshot(browser[1])
    rpc(browser, 'reconcile_profile_claim', arguments(blocked, **{field: value}), error=True)
    assert database_snapshot(browser[1]) == before


def test_recovery_rejects_unencodable_evidence_before_database_changes(recovery):
    # HTTPX refuses this invalid Unicode value before sending JSON, so exercise
    # the Board boundary directly rather than accidentally testing the client.
    browser, blocked = recovery
    register_current(browser)
    _, board, _, token, *_ = browser
    actor = board.actor_session(board.authenticate(token), 'recovery-api')
    before = database_snapshot(board)
    with pytest.raises(BoardError, match='reconcile_claim_invalid_evidence'):
        board.reconcile_profile_claim(actor, blocked['id'], blocked['version'], 'invalid-unicode', '\ud800')
    assert database_snapshot(board) == before


def test_mcp_recovery_requires_current_authorization_even_for_a_recorded_retry(recovery):
    browser, blocked = recovery
    register_current(browser)
    rpc(browser, 'reconcile_profile_claim', arguments(blocked))
    _, board, human, _, _, _, profile_id = browser
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], status='paused')
    before = database_snapshot(board)
    rpc(browser, 'reconcile_profile_claim', arguments(blocked), error=True)
    assert database_snapshot(board) == before


def test_mcp_recovery_tool_is_distinct_with_required_release_arguments(browser):
    client, _, _, token, *_ = browser
    response = client.post('/mcp', headers={'Authorization': 'Bearer ' + token,
        'Accept': 'application/json, text/event-stream'}, json={
        'jsonrpc': '2.0', 'id': 7, 'method': 'tools/list'})
    listed = {tool['name']: tool for tool in response.json()['result']['tools']}
    assert 'release_ticket' in listed and 'reconcile_profile_claim' in listed
    schema = listed['reconcile_profile_claim']['inputSchema']
    assert set(schema['required']) == {
        'session_id', 'ticket_id', 'expected_version', 'idempotency_key', 'recovery_evidence'}
    assert schema['properties']['recovery_evidence']['maxLength'] == 20000
    assert schema['properties']['expected_version']['minimum'] == 1
