"""Independent review of closed Runtime telemetry, using real synthetic MCP HTTP."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sys
import uuid

import httpx
import pytest

from test_review_runtime_http import (
    browser, review_client, args, Response,  # noqa: F401
)
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.review_worker import ReviewRuntimeGuard, _agent_id
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_guard import RuntimeControlError
from blackoutburst_agents.runtime_journal import RuntimeJournal


@pytest.mark.parametrize('cause', ['owner_paused', 'capacity_exhausted', 'review_policy_history_unavailable', 'http400', 'http403'])
def test_real_mcp_admission_cause_reaches_real_blocked_run_without_model_retry(review_client, tmp_path, cause):
    client, board, human, actor, scope, _ = review_client
    if cause == 'owner_paused':
        board.update_runtime_limits(human, 1, 8, True)
    elif cause == 'capacity_exhausted':
        board.reserve_execution(actor, **args(scope, 'synthetic-occupied-slot'))
    elif cause == 'review_policy_history_unavailable':
        with board._tx() as db:
            db.execute("DELETE FROM audit WHERE event='policy_changed'")
    else:
        bridge, status = client._opener, int(cause[4:])
        original = bridge.open
        def http_refusal(request, timeout):
            payload = json.loads(request.data)
            if payload['method'] == 'tools/call' and payload['params']['name'] == 'reserve_execution':
                bridge.calls.append(payload)
                return Response(httpx.Response(status, content=b'PRIVATE_HTTP_BODY'), request.full_url)
            return original(request, timeout)
        bridge.open = http_refusal
    expected_code = {'http400': 'http_error', 'http403': 'permission_denied'}.get(cause, cause)
    selection, operation = {'fixture': 'independent-review'}, str(uuid.uuid4())
    work_id = str(uuid.uuid4())
    models = []

    def forbidden_model(*_a, **_kw):
        models.append(True)
        raise AssertionError('No model may run')

    with closing(RunStore(tmp_path / 'runs')) as store, RuntimeJournal(tmp_path / 'runtime') as journal:
        store.create_work(owner_id=scope['owner_id'], agent_id=_agent_id(scope, selection, operation),
            project_id=scope['project_id'], ticket_id=scope['ticket_id'], workspace=tmp_path,
            executable=Path(sys.executable).resolve(), model='gpt-6.1-sol', work_id=work_id)
        guard = ReviewRuntimeGuard(journal, client, scope, selection, operation)
        runner = PersonalRunner(store, forbidden_model, runtime_guard=guard)
        with pytest.raises(RuntimeControlError, match='^runtime_start_requires_review$') as error:
            runner.run(work_id, 'explicit-review', 'PRIVATE_PROMPT_NOT_DIAGNOSTIC', timeout=10)
        assert str(error.value) == 'runtime_start_requires_review'
        turns = store.status(work_id)['turns']
        assert len(turns) == 1 and turns[0]['status'] == 'blocked'
        result = store.turn_result(turns[0]['id'])
        assert result == {'status': 'blocked', 'reason': 'runtime_start_requires_review',
                          'text': '', 'uncertain': True, 'verification': {'cleanup_confirmed': True,
                          'runtime_failure': {'stage': 'reserve', 'code': expected_code}}}
        assert journal.list_status()[0]['phase'] == 'needs_review'
        assert not journal.get(guard.operation['id'])['start_confirmed']
        assert client.call('lookup_execution', {'idempotency_key': guard.operation['key']}) == {'found': False}
        with pytest.raises(RuntimeControlError):
            runner.run(work_id, 'explicit-review', 'PRIVATE_PROMPT_NOT_DIAGNOSTIC', timeout=10)
        assert not models and len(store.status(work_id)['turns']) == 1
        reserve_calls = [c for c in client._opener.calls if c['method'] == 'tools/call'
                         and c['params']['name'] == 'reserve_execution']
        assert len(reserve_calls) == 1
        assert 'PRIVATE' not in json.dumps(result)


def test_real_mcp_start_refusal_is_distinct_from_reserve_and_existing_cleanup_checks_hold(review_client, tmp_path):
    client, board, human, _, scope, _ = review_client
    bridge, models = client._opener, []
    original = bridge.open

    def pause_between_reserve_and_start(request, timeout):
        payload = json.loads(request.data)
        response = original(request, timeout)
        if payload['method'] == 'tools/call' and payload['params']['name'] == 'reserve_execution':
            board.update_runtime_limits(human, 1, 8, True)
        return response

    bridge.open = pause_between_reserve_and_start
    selection, operation, work_id = {'fixture': 'review'}, str(uuid.uuid4()), str(uuid.uuid4())
    with closing(RunStore(tmp_path / 'runs')) as store, RuntimeJournal(tmp_path / 'runtime') as journal:
        store.create_work(owner_id=scope['owner_id'], agent_id=_agent_id(scope, selection, operation),
            project_id=scope['project_id'], ticket_id=scope['ticket_id'], workspace=tmp_path,
            executable=Path(sys.executable).resolve(), model='gpt-6.1-sol', work_id=work_id)
        guard = ReviewRuntimeGuard(journal, client, scope, selection, operation)
        runner = PersonalRunner(store, lambda *_a, **_kw: models.append(True), runtime_guard=guard)
        with pytest.raises(RuntimeControlError, match='^runtime_start_requires_review$'):
            runner.run(work_id, 'selected-review', 'PRIVATE_PROMPT', timeout=10)
        turn = store.status(work_id)['turns'][0]
        result = store.turn_result(turn['id'])
        assert result['verification']['runtime_failure'] == {'stage': 'start', 'code': 'owner_paused'}
        assert result['status'] == 'blocked' and result['uncertain'] is True and not models
        row = journal.get(guard.operation['id'])
        # Actual existing fresh uninvoked cleanup proves this reservation stopped.
        assert row['phase'] == 'finished' and row['receipt']['cleanup_confirmed'] and not row['start_confirmed']
        assert board.get_runtime(human)['active_count'] == 0
        calls_before = len([c for c in bridge.calls if c['method'] == 'tools/call'
                            and c['params']['name'] == 'reserve_execution'])
        assert calls_before == 1


@pytest.mark.parametrize('case', ['structured_conflict', 'extra_text_key', 'unknown', 'trailing_newline',
                                  'cross_method', 'multiple_text', 'not_text', 'non_bool_error'])
def test_malformed_or_cross_method_error_cannot_become_telemetry(review_client, case):
    client, _, _, _, scope, _ = review_client
    result = {'isError': True, 'content': [{'type': 'text', 'text':
              'Error executing tool reserve_execution: capacity_exhausted'}]}
    if case == 'structured_conflict': result['structuredContent'] = {'reserved': True, 'PRIVATE': 'SECRET'}
    elif case == 'extra_text_key': result['content'][0]['PRIVATE'] = 'SECRET'
    elif case == 'unknown': result['content'][0]['text'] = 'Error executing tool reserve_execution: PRIVATE_SECRET'
    elif case == 'trailing_newline': result['content'][0]['text'] += '\n'
    elif case == 'cross_method': result['content'][0]['text'] = 'Error executing tool start_execution: capacity_exhausted'
    elif case == 'multiple_text': result['content'] *= 2
    elif case == 'not_text': result['content'][0]['type'] = 'resource'
    elif case == 'non_bool_error': result['isError'] = 1
    bridge = client._opener
    original = bridge.open

    def inject(request, timeout):
        payload = json.loads(request.data)
        if payload['method'] == 'tools/call' and payload['params']['name'] == 'reserve_execution':
            envelope = {'jsonrpc': '2.0', 'id': payload['id'], 'result': deepcopy(result)}
            return Response(httpx.Response(200, json=envelope), request.full_url)
        return original(request, timeout)

    bridge.open = inject
    with pytest.raises(BoardReadError) as caught:
        client.call('reserve_execution', args(scope))
    assert not hasattr(caught.value, '_runtime_tool_code')
    assert str(caught.value) in ('tool_rejected', 'invalid_response')
    assert 'PRIVATE' not in str(caught.value) and 'SECRET' not in repr(caught.value)




@pytest.mark.parametrize('where', ['client', 'guard'])
def test_unknown_exception_cannot_execute_a_diagnostic_dict_or_text_accessor(review_client, tmp_path, where):
    client, _, _, _, scope, _ = review_client
    observed, models = [], []

    class PrivateError(Exception):
        def __getattribute__(self, name):
            if name == '__dict__':
                observed.append('dict')
                raise AssertionError('PRIVATE_DICT_ACCESS')
            return super().__getattribute__(name)
        def __str__(self):
            observed.append('str')
            raise AssertionError('PRIVATE_STR_ACCESS')
        def __repr__(self):
            observed.append('repr')
            raise AssertionError('PRIVATE_REPR_ACCESS')

    selection, operation, work_id = {'fixture': 'review'}, str(uuid.uuid4()), str(uuid.uuid4())
    with closing(RunStore(tmp_path / 'runs')) as store, RuntimeJournal(tmp_path / 'runtime') as journal:
        store.create_work(owner_id=scope['owner_id'], agent_id=_agent_id(scope, selection, operation),
            project_id=scope['project_id'], ticket_id=scope['ticket_id'], workspace=tmp_path,
            executable=Path(sys.executable).resolve(), model='gpt-6.1-sol', work_id=work_id)
        guard = ReviewRuntimeGuard(journal, client, scope, selection, operation)
        if where == 'client':
            original = client.call
            def fail_reserve(name, values):
                if name == 'reserve_execution': raise PrivateError()
                return original(name, values)
            client.call = fail_reserve
        else:
            def fail_before(*_): raise PrivateError()
            guard.before = fail_before
        runner = PersonalRunner(store, lambda *_a, **_kw: models.append(True), runtime_guard=guard)
        with pytest.raises(RuntimeControlError if where == 'client' else PrivateError):
            runner.run(work_id, 'private-exception-test', 'PRIVATE_PROMPT', timeout=10)
        turn = store.status(work_id)['turns'][0]
        result = store.turn_result(turn['id'])
        assert result['status'] == 'blocked' and result['uncertain'] is True
        if where == 'client':
            assert result['verification']['runtime_failure'] == {'stage': 'reserve', 'code': 'unrecognized_code'}
        else:
            assert 'runtime_failure' not in result['verification']
        assert not observed and not models
