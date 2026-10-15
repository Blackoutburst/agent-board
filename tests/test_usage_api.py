"""Strict MCP observation reporting and human-only runtime projection."""
import copy
import time
import pytest

from test_runtime_api import browser, rpc
from usage_observations import connection_identifier


def observation(browser):
    board = browser[1]
    with board._connection() as db:
        row = db.execute("SELECT * FROM sessions WHERE id='runtime-api'").fetchone()
    return {'connection_id': connection_identifier(board.base_url + '/mcp', row['owner_id'], row['agent_id'], row['device_id']),
        'observed_at': time.time(), 'snapshot': {
            'schema_version': 1, 'provider': 'codex_chatgpt', 'source': 'codex_app_server_account_rate_limits',
            'cli_version': '1.2.3', 'data_status': 'observed', 'unavailable_reason': None,
            'ordinary_usage_allowed': True, 'buckets': [{'limit_id': 'codex', 'source_view': 'multi_bucket',
                'primary': {'used_percent': 25, 'window_minutes': None, 'resets_at': None},
                'secondary': None, 'reached_reason': None, 'spend_control_reached': None, 'normal_model_slug': None}]}}


def test_mcp_report_and_private_rest_projection(browser):
    values = observation(browser)
    reported = rpc(browser, 'report_usage_observation', values)
    assert reported['data_status'] == 'observed'
    assert reported['snapshot'] == values['snapshot']
    assert reported['agent_name'] and reported['device_label'] == 'Fixture host'
    view = browser[0].get('/api/runtime').json()
    assert view['usage_observations'] == [reported]
    assert view['quota']['status'] == 'observed'
    browser[0].cookies.clear()
    assert browser[0].get('/api/runtime').status_code == 401


@pytest.mark.parametrize('value', [True, False, '1', None, [], {}])
def test_mcp_timestamp_is_strict_without_write(browser, value):
    values = observation(browser); values['observed_at'] = value
    rpc(browser, 'report_usage_observation', values, error=True)
    assert browser[1].get_runtime(browser[2])['usage_observations'] == []


@pytest.mark.parametrize('change', [
    {'schema_version': True}, {'ordinary_usage_allowed': 1}, {'cli_version': 1},
    {'accountId': 'PRIVATE'}, {'credits': {'balance': 'PRIVATE'}}, {'buckets': 'PRIVATE'},
])
def test_mcp_snapshot_rejects_coercion_and_private_fields(browser, change):
    values = observation(browser); values['snapshot'].update(change)
    rpc(browser, 'report_usage_observation', values, error=True)
    assert browser[1].get_runtime(browser[2])['usage_observations'] == []


@pytest.mark.parametrize('change', [{'used_percent': True}, {'used_percent': 25.0}, {'used_percent': '25'},
    {'window_minutes': False}, {'resets_at': '1800000000'}, {'raw': 'PRIVATE'}])
def test_mcp_nested_window_values_are_strict(browser, change):
    values = observation(browser); values['snapshot']['buckets'][0]['primary'].update(change)
    rpc(browser, 'report_usage_observation', values, error=True)
    assert browser[1].get_runtime(browser[2])['usage_observations'] == []


def test_report_is_not_available_through_cleanup_permission(browser):
    values = observation(browser)
    board, human, profile_id = browser[1], browser[2], browser[6]
    profile = board.get_agent_profile(human, profile_id)
    board.update_agent_profile(human, profile_id, profile['version'], status='paused')
    rpc(browser, 'report_usage_observation', values, error=True)
    assert board.get_runtime(human)['usage_observations'] == []
