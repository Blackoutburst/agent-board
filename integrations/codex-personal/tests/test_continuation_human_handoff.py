"""Actual failed cycle/Board question; transport/native/model seams synthetic."""
from copy import deepcopy
from contextlib import contextmanager
import http.client
import json
from pathlib import Path

import pytest

from board import BoardError
from blackoutburst_agents import continuation_pool_service as service
from blackoutburst_agents.continuation_human_handoff import ContinuationHumanHandoff
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import canonical
from test_board_reader import Response, Opener, URL
from test_continuation_pool_service_integration import actual_service
from test_development_cycle_linux_preparation import linux_lock_observation


@pytest.mark.parametrize('mode', ['question', 'lost_ack', 'cleanup_unknown', 'request_revoked', 'all_revoked', 'stopped', 'provider_pause', 'fence_drift', 'source_drift', 'late_stop'])
def test_failed_actual_cycle_only_confirms_real_human_handoff_or_closed_notice(actual_service, monkeypatch, mode):
    f, root, task_id, ticket, construct, calls, sequence = actual_service
    # Completed tool-free model output, rejected before any accepted action.
    sequence.write_text(json.dumps([None if mode == 'provider_pause' else dict(schema=1, kind='PRIVATE_MODEL_TEXT')]))
    observed, packets, publications = [], [], []
    original = service._Generation.open_task
    def open_task(generation, *args, **kwargs):
        original(generation, *args, **kwargs)
        run = generation.provider.run_checkpoint
        def closed(*args, **kwargs):
            result = run(*args, **kwargs)
            assert result['status'] == ('paused_provider' if mode == 'provider_pause' else 'needs_you'), result
            worker = generation.worker
            class Wire:
                def open(self, request, timeout):
                    packet = json.loads(request.data)
                    packets.append(packet)
                    assert packet['method'] == 'tools/call', 'No initialize/register during visibility'
                    name = packet['params']['name']
                    values = deepcopy(packet['params']['arguments']);values.pop('view', None)
                    sid = values.pop('session_id')
                    assert name not in ('register_agent', 'claim_ticket', 'renew_ticket', 'release_ticket', 'update_ticket')
                    actor = f.board.actor_session(f.raw, sid)
                    try:
                        value = getattr(f.board, name)(actor, **values)
                        response = {'structuredContent': value}
                    except BoardError:
                        response = {'isError': True, 'content': [{'type': 'text', 'text': 'FIXED_REFUSAL'}]}
                    if mode == 'lost_ack' and name == 'request_human_input':
                        publications.append(value['id'])
                        raise http.client.RemoteDisconnected('PRIVATE_LOST_ACK')
                    return Response(json.dumps({'jsonrpc': '2.0', 'id': packet['id'], 'result': response}).encode(),
                        url='https://board.test/mcp')
            client = DevelopmentBoardClient({'mcp_url': 'https://board.test/mcp', 'token': 'SYNTHETIC',
                'client': 'codex', 'identity_key': 'fixture', 'name': 'Fixture'}, worker.client.session_id, 3)
            client._registered = client._initialized = True;client._opener = Wire()
            client.register = lambda: pytest.fail('Visibility never registers')
            client.adopt_registration = lambda: pytest.fail('Visibility never adopts registration')
            worker.client = client
            monkeypatch.setattr(worker.control, 'close', lambda: None, raising=False)
            if mode == 'cleanup_unknown':
                call = worker.control.call
                def unknown(name, arguments):
                    value = call(name, arguments)
                    if name == 'lookup_execution':
                        value = deepcopy(value);value['execution'].update(cleanup_confirmed=False, state='needs_review')
                    return value
                worker.control.call = unknown
            if mode in ('request_revoked', 'all_revoked'):
                f.board.set_policy(f.human, True, True, mode != 'all_revoked', False, f.actions, ['repo:fixture'])
            if mode == 'fence_drift':
                create = PrivateDirectory.create
                def drift(private, name, raw):
                    value = create(private, name, raw)
                    if name.startswith('continuation-human-intent-'):
                        with PrivateDirectory(worker.test_anchor) as anchor:
                            document = decode(anchor.read('fence.json', 2*1024*1024))
                            keys = [key for key, row in document['records'].items() if row['operation_id'] == generation.operation]
                            assert len(keys) == 1
                            document['records'][keys[0]]['operation_id'] = '00000000-0000-0000-0000-000000000000'
                            anchor.replace('fence.json', canonical(document))
                    return value
                monkeypatch.setattr(PrivateDirectory, 'create', drift)
            visibility = ContinuationHumanHandoff(worker, generation.task, generation.operation)
            if mode == 'source_drift':
                op = worker.journal.get(generation.operation)
                with PrivateDirectory(op['configuration']['pins']['broker']['path']) as broker:
                    manifest = decode(broker.read('manifest.json', 128*1024))
                (Path(manifest['root'])/next(iter(manifest['files']))).write_text('UNRECORDED_SOURCE\n')
            if mode == 'late_stop':
                effects = visibility._effects;stopped = []
                @contextmanager
                def late(op):
                    with effects(op) as (manifest, sha, fresh):
                        def changed():
                            fresh()
                            if not stopped:
                                current = generation.task.snapshot()['state']
                                generation.task.stop(expected_revision=current['revision'], request_id='stop-during-last-source-check')
                                stopped.append(True)
                        yield manifest, sha, changed
                visibility._effects = late
            if mode == 'stopped':
                current = generation.task.snapshot()['state']
                generation.task.stop(expected_revision=current['revision'], request_id='human-stop-before-visibility')
            before = generation.task.snapshot()
            report = visibility.publish(question_code='permissions')
            observed.append(report)
            assert generation.task.snapshot() == before
            if mode in ('question', 'lost_ack'):
                assert visibility.publish(question_code='permissions') == report
            client.close()
            return result
        generation.provider.run_checkpoint = closed
    monkeypatch.setattr(service._Generation, 'open_task', open_task)
    result = construct().run_once()
    assert result['status'] == ('pass_complete' if mode == 'provider_pause' else 'needs_you')
    assert len(calls.read_text().splitlines()) == 1
    assert len(observed) == 1, result
    report = observed[0]
    names = [p['params']['name'] for p in packets]
    questions = f.board.list_human_requests(f.human)
    actual = f.board.get_ticket(f.human, ticket['id'])['ticket']
    if mode in ('question', 'lost_ack'):
        assert report['status'] == 'question_confirmed' and report['human_question_confirmed'] is True, report
        assert len(questions) == 1 and names.count('request_human_input') == 1
        assert questions[0]['id'] == report['request_id']
        assert questions[0]['kind'] == 'question' and questions[0].get('command', '') == ''
        assert 'PRIVATE_' not in questions[0]['question']
        assert actual['status'] == 'blocked' and actual['claimant'] is None
        assert names.count('post_message') == 0
        if mode == 'lost_ack':assert publications == [report['request_id']] and names.count('lookup_human_request') == 2
    elif mode in ('stopped', 'provider_pause'):
        assert report['status'] == 'not_applicable' and not questions and names == []
        assert (actual['claimant'] is None) == (mode == 'provider_pause')
    else:
        assert not questions and not report['human_question_confirmed']
        assert names.count('request_human_input') == 0
        assert actual['status'] == 'in_progress' and actual['claimant'] is not None
        assert report['status'] == ('pending' if mode in ('all_revoked', 'fence_drift', 'late_stop') else 'notice_confirmed'), report
        assert names.count('post_message') == (0 if mode in ('fence_drift', 'late_stop') else 1)
    assert f.board.get_runtime(f.human)['active_count'] == 0
    assert 'PRIVATE_' not in json.dumps(report)


def test_constructor_is_inert_and_refuses_declared_handles(tmp_path):
    with pytest.raises(DevelopmentError):ContinuationHumanHandoff({}, {}, '00000000-0000-0000-0000-000000000000')
    assert list(tmp_path.iterdir()) == []


def channel():
    client = DevelopmentBoardClient({'mcp_url': URL, 'token': 'SYNTHETIC',
        'client': 'codex', 'identity_key': 'fixture', 'name': 'Fixture'}, 'session', 1)
    client._registered = client._initialized = True
    client._opener = Opener()
    return client


def parameters():
    return dict(ticket_id='ticket', owner_id='owner', question_code='permissions',
        idempotency_key='continuation-human-00000000-0000-0000-0000-000000000000')


@pytest.mark.parametrize('method', ['request_continuation_human_input', 'lookup_continuation_human_input'])
def test_controller_question_methods_never_retry_a_mutation_or_register(method):
    client = channel();client._opener = Opener([http.client.RemoteDisconnected('PRIVATE')])
    client.register = lambda: pytest.fail('Must never register')
    client.adopt_registration = lambda: pytest.fail('Must never adopt')
    values = parameters()
    if method.startswith('request'):values['expected_version'] = 1
    with pytest.raises(BoardReadError) as caught:getattr(client, method)(**values)
    assert type(caught.value) is BoardReadError and caught.value.code == 'connection_failed'
    assert len(client._opener.requests) == 1


@pytest.mark.parametrize('state', ['unregistered', 'uninitialized', 'closed'])
def test_question_channel_never_repairs_missing_registration_or_initialization(state):
    client = channel()
    if state == 'unregistered':client._registered = False
    elif state == 'uninitialized':client._initialized = False
    else:client._closed = True
    client.register = client._initialize = lambda: pytest.fail('No implicit initialization')
    for name in ('request_continuation_human_input', 'lookup_continuation_human_input'):
        values = parameters()
        if name.startswith('request'):values['expected_version'] = 1
        with pytest.raises(BoardReadError):getattr(client, name)(**values)
    assert client._opener.requests == []


@pytest.mark.parametrize('bad', ['PRIVATE_MODEL_TEXT', 'approval', 'quota', 'stop'])
def test_only_closed_fixed_questions_are_accepted(bad):
    client = channel();values = parameters();values['question_code'] = bad
    with pytest.raises(BoardReadError):client.request_continuation_human_input(**values, expected_version=1)
    assert client._opener.requests == []


def test_human_methods_are_not_added_to_the_planner_tool_channel():
    client = channel()
    for name in ('request_human_input', 'lookup_human_request'):
        with pytest.raises(BoardReadError):client.call(name, {})
    assert client._opener.requests == []
