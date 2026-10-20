"""Shared ErrorItem recognition, exercised with local fake processes only."""
import json
from unittest.mock import patch

import pytest

import test_codex_turn as fixture
from blackoutburst_agents.subscription_probe import _ProcessResult, _parse_probe


NOTICE = {'type': 'item.completed', 'item': {
    'id': 'notice_0', 'type': 'error', 'message': 'PRIVATE_NOTICE_NOT_RETAINED'}}


@pytest.fixture
def turn():
    case = fixture.CodexTurnTests('runTest')
    case.setUp()
    try:
        yield case
    finally:
        case.doCleanups()


def notice_line(event=NOTICE):
    return f'event({event!r})\n'


@pytest.mark.parametrize('prefix,suffix', [
    ('', 'success()\n'),
    (f"event({{'type':'thread.started','thread_id':{fixture.SESSION!r}}})\n",
     "event({'type':'turn.started'})\nmessage('Final private reply')\nevent({'type':'turn.completed'})\n"),
    ('started()\n', "message('Final private reply')\nevent({'type':'turn.completed'})\n"),
])
def test_known_notice_does_not_replace_any_success_gate(turn, prefix, suffix):
    called = []
    result = turn.run_turn(prefix + notice_line() + suffix, on_session=called.append)
    assert result['status'] == 'completed' and result['reason'] == 'verified'
    assert result['text'] == 'Final private reply'
    assert called == [fixture.SESSION]
    assert all(result['verification'][name] is True for name in (
        'exit_zero', 'turn_completed', 'session_verified', 'tool_free', 'cleanup_confirmed'))
    assert 'PRIVATE_NOTICE_NOT_RETAINED' not in json.dumps(result)
    assert 'notice_0' not in json.dumps(result)


@pytest.mark.parametrize('suffix,reason', [
    ('', 'turn_not_completed'),
    ("event({'type':'turn.completed'})\n", 'protocol_error'),
    ("started()\nmessage('partial')\n", 'turn_not_completed'),
    ("started()\nevent({'type':'turn.completed'})\n", 'missing_response'),
    ('success()\nraise SystemExit(1)\n', 'execution_failed'),
    ("event({'type':'thread.started','thread_id':'invalid'})\n", 'protocol_error'),
])
def test_notice_alone_never_establishes_positive_proof(turn, suffix, reason):
    result = turn.run_turn(notice_line() + suffix)
    assert result['status'] == 'blocked' and result['reason'] == reason
    assert result['text'] is None
    assert result['verification']['cleanup_confirmed'] is True


@pytest.mark.parametrize('event', [
    {'type': 'error', 'message': 'authentication failed PRIVATE_FATAL'},
    {'type': 'turn.failed', 'error': {'message': 'usage limit PRIVATE_FATAL'}},
])
@pytest.mark.parametrize('after_success', [False, True])
def test_fatal_events_keep_their_refusal_with_a_notice(turn, event, after_success):
    prefix = notice_line() + ('success()\n' if after_success else '')
    result = turn.run_turn(prefix + f'event({event!r})\n')
    assert result['status'] == 'blocked'
    assert result['reason'] == ('authentication_failed' if event['type'] == 'error' else 'quota_or_rate_limit')
    assert result['text'] is None
    assert 'PRIVATE_FATAL' not in json.dumps(result)


def test_fatal_event_before_notice_is_not_ignored(turn):
    result = turn.run_turn("event({'type':'error','message':'authentication failed PRIVATE_FATAL'})\n" +
        notice_line() + 'success()\n')
    assert result['status'] == 'blocked' and result['reason'] == 'authentication_failed'
    assert result['text'] is None and 'PRIVATE_FATAL' not in json.dumps(result)


@pytest.mark.parametrize('kind', [
    'command_execution', 'file_change', 'mcp_tool_call', 'collab_tool_call',
    'web_search', 'todo_list', 'future_tool',
])
def test_tools_and_unknown_items_are_not_diagnostics(turn, kind):
    result = turn.run_turn(notice_line() + 'started()\n' +
        f"event({{'type':'item.completed','item':{{'type':{kind!r},'text':'PRIVATE_TOOL'}}}})\n" +
        'success()\n')
    assert result['status'] == 'blocked' and result['reason'] == 'unexpected_tool_event'
    assert result['verification']['tool_free'] is False
    assert result['text'] is None and 'PRIVATE_TOOL' not in json.dumps(result)


@pytest.mark.parametrize('change', [
    {'id': None}, {'id': True}, {'id': ''}, {'id': 'x' * 257},
    {'message': None}, {'message': True}, {'message': 'x' * 16385}, {'extra': 'PRIVATE'},
])
def test_malformed_notice_is_refused_by_both_parsers(turn, change):
    event = {**NOTICE, 'item': {**NOTICE['item'], **change}}
    result = turn.run_turn(notice_line(event) + 'success()\n')
    assert result['status'] == 'blocked' and result['reason'] == 'protocol_error'
    raw = '\n'.join(json.dumps(value) for value in (
        event, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'nonce'}},
        {'type': 'turn.completed'})).encode()
    with patch('subprocess.Popen', side_effect=AssertionError('parser only')):
        probe = _parse_probe(_ProcessResult(0, raw, b''), 'nonce')
    assert probe['ok'] is False and probe['reason'] == 'protocol_error'


@pytest.mark.parametrize('kind', ['item.started', 'item.updated'])
def test_error_item_wrong_lifecycle_is_not_ignored(turn, kind):
    result = turn.run_turn(notice_line({**NOTICE, 'type': kind}) + 'success()\n')
    assert result['status'] == 'blocked' and result['reason'] == 'protocol_error'


def test_notice_after_terminal_and_duplicate_fields_stay_protocol_errors(turn):
    for body in (
        'success()\n' + notice_line(),
        'os.write(1, b\'{"type":"item.completed","item":{"id":"x","type":"error",'
        '"message":"a","message":"b"}}\\n\')\nsuccess()\n',
    ):
        result = turn.run_turn(body)
        assert result['status'] == 'blocked' and result['reason'] == 'protocol_error'
        assert result['text'] is None


def test_notice_does_not_bypass_resume_identity(turn):
    called = []
    result = turn.run_turn(notice_line() + 'success()\n',
        resume_session_id=fixture.OTHER_SESSION, on_session=called.append)
    assert result['reason'] == 'session_mismatch' and called == []
    assert result['text'] is None


def test_notice_bytes_still_count_toward_transport_output_limit(turn):
    settings = fixture.ProbeSettings(turn.fake_cli(notice_line({**NOTICE, 'item': {
        **NOTICE['item'], 'message': 'x' * 2048}}) + 'success()\n'), max_output_bytes=1024)
    result = turn.run_turn(settings=settings)
    assert result['reason'] == 'output_limit' and result['text'] is None


def test_notice_shape_bounds_are_inclusive_and_private(turn):
    event = {**NOTICE, 'item': {**NOTICE['item'], 'id': 'i' * 256, 'message': 'x' * 16384}}
    result = turn.run_turn(notice_line(event) + 'success()\n')
    assert result['status'] == 'completed'
    assert 'i' * 256 not in json.dumps(result) and 'x' * 16384 not in json.dumps(result)


def test_notice_cannot_mask_failed_cleanup(turn):
    import blackoutburst_agents.codex_turn as transport

    original = transport._stop_group

    def uncertain(process):
        original(process)
        return False

    with patch.object(transport, '_stop_group', side_effect=uncertain):
        result = turn.run_turn(notice_line() + 'success()\n')
    assert result['status'] == 'blocked' and result['reason'] == 'cleanup_failed'
    assert result['verification']['cleanup_confirmed'] is False and result['text'] is None
