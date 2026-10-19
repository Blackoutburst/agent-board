"""Public JSONL error-item contract; synthetic bytes only, no CLI/account/model."""
import json
from unittest.mock import patch

import pytest

from blackoutburst_agents.subscription_probe import _ProcessResult, _parse_probe


NONCE = 'PRIVATE_NONCE_NOT_RETAINED'
PRIVATE = 'PRIVATE_ERROR_CONTENT_NOT_RETAINED'


def parse(*events, code=0):
    data = b'\n'.join(json.dumps(e).encode() if type(e) is dict else e for e in events)
    with patch('subprocess.Popen', side_effect=AssertionError('no process')):
        result = _parse_probe(_ProcessResult(code, data, b''), NONCE)
    assert PRIVATE not in json.dumps(result)
    assert NONCE not in json.dumps(result)
    return result


def item(**changes):
    return {'type':'item.completed', 'item':{
        'id':'item_0', 'type':'error', 'message':PRIVATE, **changes}}


def success():
    return ({'type':'item.completed', 'item':{'type':'agent_message', 'text':NONCE}},
            {'type':'turn.completed'})


def test_completed_error_item_cannot_qualify_even_with_nonce_and_exit_zero():
    result = parse(item(), *success())
    assert result['ok'] is False and result['reason'] == 'cli_diagnostic_item'
    assert result['verification'] == {'exit_zero':True, 'turn_completed':True, 'nonce_verified':True}
    assert result['diagnostic']['schema_version'] == 2
    assert result['diagnostic']['item_events']['error'] == 1
    assert result['diagnostic']['item_events']['unknown'] == 0


def test_diagnostic_item_does_not_assert_a_fatal_auth_or_quota_error():
    for text in ('authentication failed '+PRIVATE, 'quota exceeded '+PRIVATE):
        result = parse(item(message=text), *success())
        assert result['reason'] == 'cli_diagnostic_item' and result['ok'] is False


@pytest.mark.parametrize('terminal,reason', [
    ({'type':'error','message':'authentication failed '+PRIVATE}, 'authentication_failed'),
    ({'type':'turn.failed','error':{'message':'usage limit '+PRIVATE}}, 'quota_or_rate_limit'),
    ({'type':'turn.failed','error':{'message':PRIVATE}}, 'execution_failed'),
])
def test_terminal_failure_keeps_its_category_without_warning_content(terminal, reason):
    result = parse(item(), terminal, code=1)
    assert result['reason'] == reason and result['ok'] is False


@pytest.mark.parametrize('kind', ['command_execution','mcp_tool_call','collab_tool_call',
                                 'file_change','web_search','todo_list','future_tool'])
def test_tool_or_unknown_item_is_still_the_highest_priority_refusal(kind):
    result = parse(item(), {'type':'item.started','item':{'type':kind}}, *success())
    assert result['ok'] is False and result['reason'] == 'unexpected_tool_event'
    key = kind if kind != 'future_tool' else 'unknown'
    assert result['diagnostic']['item_events'][key] == 1


@pytest.mark.parametrize('change', [
    {'id':None}, {'id':True}, {'id':''}, {'id':'x'*257},
    {'message':None}, {'message':True}, {'message':'x'*16385}, {'extra':PRIVATE},
])
def test_malformed_known_error_item_never_becomes_a_valid_warning(change):
    result = parse(item(**change), *success())
    assert result['ok'] is False and result['reason'] == 'protocol_error'


def test_error_item_wrong_lifecycle_and_duplicate_keys_remain_protocol_errors():
    for event in ({**item(), 'type':'item.started'}, {**item(),'type':'item.updated'},
                  b'{"type":"item.completed","item":{"id":"x","type":"error","message":"a","message":"b"}}'):
        result = parse(event, *success())
        assert result['ok'] is False and result['reason'] == 'protocol_error'


def test_error_item_after_a_terminal_is_not_ignored():
    result = parse(*success(), item())
    assert result['reason'] == 'protocol_error' and result['ok'] is False


def test_error_counter_saturates_and_never_turns_into_success():
    result = parse(*([item()]*1025), *success())
    assert result['reason'] == 'cli_diagnostic_item' and result['ok'] is False
    assert result['diagnostic']['item_events']['error'] == 1024
    assert result['diagnostic']['saturated'] is True


def test_transport_failure_is_not_reparsed_for_new_diagnostics():
    raw = json.dumps(item()).encode()
    result = _parse_probe(_ProcessResult(0,raw,b'', 'cleanup_failed'), NONCE)
    assert result['reason'] == 'cleanup_failed' and result['ok'] is False
    assert sum(result['diagnostic']['item_events'].values()) == 0
