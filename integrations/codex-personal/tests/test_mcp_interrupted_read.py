"""One application read retry, real loopback sockets and synthetic identities."""
import http.client
import http.server
import json
import socket
import ssl
import threading
import time
import urllib.error

import pytest

from blackoutburst_agents.board_reader import BoardReader, BoardReadError
from test_board_reader import Response, TOKEN, URL
import test_persistent_transport as tls_fixture


READS = ('get_ticket', 'get_thread', 'get_message', 'get_work',
    'get_development_ticket_frame', 'lookup_execution')


def read_rpc(reader, name='get_ticket'):
    return reader._rpc('tools/call', {'name': name, 'arguments': {'session_id': 'actor', 'ticket_id': 'ticket'}})


def assert_error(code, call):
    with pytest.raises(BoardReadError) as caught:
        call()
    assert type(caught.value) is BoardReadError
    assert caught.value.code == str(caught.value) == code
    assert TOKEN not in repr(caught.value)


class WireServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, *_):pass


class WireHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_):pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append((request, dict(self.headers)))
        behavior = self.server.behaviors.pop(0) if self.server.behaviors else 'normal'
        if behavior == 'lost_slow':time.sleep(.07)
        if behavior in ('lost', 'lost_slow'):
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            return
        if behavior == 'slow':time.sleep(.15)
        body = json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {'fresh': True}}).encode()
        if behavior == 'malformed':body = b'{bad json'
        if behavior == 'wrong_id':body = json.dumps({'jsonrpc': '2.0', 'id': request['id']+1, 'result': {}}).encode()
        self.send_response(403 if behavior == 'forbidden' else 200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)+(10 if behavior == 'partial' else 0)))
        self.send_header('Mcp-Session-Id', 'wire-session')
        self.end_headers()
        try:self.wfile.write(body); self.wfile.flush()
        except OSError:pass
        if behavior in ('idle_close', 'partial'):
            # Deliberately no Connection: close header. The next request must
            # encounter the server's closed persistent socket, not a new hint.
            self.connection.shutdown(socket.SHUT_RDWR)
            self.close_connection = True
            self.server.idle_closed.set()


@pytest.fixture
def wire():
    server = WireServer(('127.0.0.1', 0), WireHandler)
    server.requests, server.behaviors, server.idle_closed = [], [], threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = 'http://127.0.0.1:'+str(server.server_port)+'/mcp'
    readers = []
    def create(timeout=1):
        reader = BoardReader({'mcp_url': endpoint, 'token': TOKEN, 'client': 'codex',
            'identity_key': 'fixture', 'name': 'Fixture'}, 'actor', timeout)
        readers.append(reader)
        attempts = []
        original = reader._opener.open
        def record(request, timeout):
            attempts.append((json.loads(request.data), dict(request.header_items()), timeout))
            return original(request, timeout)
        reader._opener.open = record
        return reader, attempts
    yield server, create
    for reader in readers:reader.close()
    server.shutdown();server.server_close();thread.join(timeout=2)


def test_silently_closed_http11_connection_reconnects_for_one_fresh_read(wire):
    server, create = wire
    reader, attempts = create()
    server.behaviors = ['idle_close', 'normal']
    assert read_rpc(reader) == {'fresh': True}
    assert server.idle_closed.wait(1)
    assert read_rpc(reader) == {'fresh': True}
    assert len(attempts) == 3 and len(server.requests) == 2
    first, second = attempts[1:]
    assert first[0]['id'] != second[0]['id']
    assert first[0]['params'] == second[0]['params']
    assert first[1] == second[1] and first[1]['Mcp-session-id'] == 'wire-session'
    assert first[1]['Authorization'] == 'Bearer '+TOKEN
    assert second[2] <= first[2] <= reader.timeout == 1


@pytest.mark.parametrize('behavior', ['lost', 'partial'])
def test_lost_or_incomplete_read_has_only_two_distinct_requests(wire, behavior):
    server, create = wire
    reader, attempts = create()
    server.behaviors = [behavior, 'normal']
    assert read_rpc(reader) == {'fresh': True}
    assert len(attempts) == len(server.requests) == 2
    assert [row[0]['id'] for row in attempts] == [1, 2]
    assert attempts[0][0]['params'] == attempts[1][0]['params']


def test_lost_mutation_response_is_never_replayed(wire):
    server, create = wire
    reader, attempts = create()
    server.behaviors = ['lost', 'normal']
    assert_error('connection_failed', lambda: reader._rpc('tools/call',
        {'name': 'update_ticket', 'arguments': {'session_id': 'actor', 'changes': {'status': 'review'}}}))
    assert len(attempts) == len(server.requests) == 1
    assert server.requests[0][0]['params']['name'] == 'update_ticket'


def test_fresh_permission_denial_after_lost_read_is_not_hidden_or_retried(wire):
    server, create = wire
    reader, attempts = create()
    server.behaviors = ['lost', 'forbidden', 'normal']
    assert_error('permission_denied', lambda: read_rpc(reader, 'get_development_ticket_frame'))
    assert len(attempts) == len(server.requests) == 2


@pytest.mark.parametrize('behavior,code', [('forbidden', 'permission_denied'),
    ('malformed', 'invalid_response'), ('wrong_id', 'invalid_response'), ('slow', 'timeout')])
def test_http_protocol_and_timeout_failures_do_not_retry(wire, behavior, code):
    server, create = wire
    reader, attempts = create(timeout=.08)
    server.behaviors = [behavior, 'normal']
    assert_error(code, lambda: read_rpc(reader))
    assert len(attempts) == len(server.requests) == 1


def test_second_read_uses_only_the_initial_remaining_timeout(wire):
    server, create = wire
    reader, attempts = create(timeout=.12)
    server.behaviors = ['lost_slow', 'slow', 'normal']
    started = time.monotonic()
    assert_error('timeout', lambda: read_rpc(reader))
    elapsed = time.monotonic()-started
    assert len(attempts) == len(server.requests) == 2
    assert attempts[1][2] < .08 and attempts[1][2] < attempts[0][2]
    assert elapsed < .24 and reader.timeout == .12


class ScriptedOpener:
    def __init__(self, first, *, callback=None):
        self.first, self.callback, self.requests, self.discards = first, callback, [], 0
    def discard(self):self.discards += 1
    def close(self):pass
    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.requests.append((payload, request.full_url, dict(request.header_items()), timeout))
        if len(self.requests) == 1:
            if self.callback is not None:self.callback()
            raise self.first
        return Response(json.dumps({'jsonrpc': '2.0', 'id': payload['id'], 'result': {'fresh': True}}).encode())


def synthetic(first, callback=None):
    reader = BoardReader({'mcp_url': URL, 'token': TOKEN, 'client': 'codex',
        'identity_key': 'fixture', 'name': 'Fixture'}, 'actor', 1)
    opener = ScriptedOpener(first, callback=callback)
    reader._opener = opener
    return reader, opener


@pytest.mark.parametrize('name', READS)
@pytest.mark.parametrize('error', [http.client.RemoteDisconnected('private'), ConnectionResetError('private'),
    BrokenPipeError('private'), ConnectionAbortedError('private'), http.client.IncompleteRead(b'private')])
def test_only_exact_local_interruption_types_allow_a_single_read_retry(name, error):
    reader, opener = synthetic(error)
    assert read_rpc(reader, name) == {'fresh': True}
    assert len(opener.requests) == 2 and opener.discards >= 1


@pytest.mark.parametrize('method,name', [('initialize', None), ('notifications/initialized', None),
    ('synthetic/read', None), ('tools/call', 'register_agent'), ('tools/call', 'claim_ticket'),
    ('tools/call', 'renew_ticket'), ('tools/call', 'update_ticket'), ('tools/call', 'release_ticket'),
    ('tools/call', 'post_message'), ('tools/call', 'reserve_execution'), ('tools/call', 'start_execution'),
    ('tools/call', 'heartbeat_execution'), ('tools/call', 'finish_execution'),
    ('tools/call', 'get_execution_control'), ('tools/call', 'get_policy'), ('tools/call', 'unknown')])
def test_unknown_methods_and_every_mutation_are_never_repeated(method, name):
    reader, opener = synthetic(http.client.RemoteDisconnected('private'))
    params = {} if name is None else {'name': name, 'arguments': {}}
    assert_error('connection_failed', lambda: reader._rpc(method, params))
    assert len(opener.requests) == 1


@pytest.mark.parametrize('error,code', [(BoardReadError('connection_failed'), 'connection_failed'),
    (urllib.error.URLError(ConnectionResetError('private')), 'connection_failed'),
    (ssl.SSLCertVerificationError('private'), 'connection_failed'), (ssl.SSLError('private'), 'connection_failed'),
    (OSError('private'), 'connection_failed'), (ValueError('private'), 'connection_failed'),
    (TimeoutError('private'), 'timeout'), (http.client.BadStatusLine('private'), 'connection_failed')])
def test_declared_errors_ssl_and_noninterruption_exceptions_are_not_retry_authority(error, code):
    reader, opener = synthetic(error)
    assert_error(code, lambda: read_rpc(reader))
    assert len(opener.requests) == 1


def test_tools_list_retry_freezes_semantics_headers_session_and_endpoint():
    params = {'cursor': 'original'}
    reader, opener = synthetic(ConnectionResetError('private'))
    reader._transport_session = 'original-session'
    def mutate():
        params['cursor'] = 'changed'
        reader._config.update(mcp_url='https://other.example.test/mcp', token='OTHER_ACTOR')
    opener.callback = mutate
    assert reader._rpc('tools/list', params) == {'fresh': True}
    left, right = opener.requests
    assert left[0]['id'] != right[0]['id']
    assert left[0]['params'] == right[0]['params'] == {'cursor': 'original'}
    assert left[1] == right[1] == URL
    assert left[2] == right[2] and left[2]['Authorization'] == 'Bearer '+TOKEN
    assert left[2]['Mcp-session-id'] == 'original-session'


def test_closed_client_never_reopens_even_after_interruption():
    reader, opener = synthetic(ConnectionResetError('private'))
    opener.callback = reader.close
    assert_error('connection_closed', lambda: read_rpc(reader))
    assert len(opener.requests) == 1
    assert_error('connection_closed', lambda: read_rpc(reader))
    assert len(opener.requests) == 1


def test_retry_exhaustion_is_exact_public_error_not_private_marker():
    reader, opener = synthetic(ConnectionResetError('private'))
    def always(request, timeout):
        opener.requests.append((json.loads(request.data), timeout))
        raise http.client.RemoteDisconnected('private')
    opener.open = always
    assert_error('connection_failed', lambda: read_rpc(reader))
    assert len(opener.requests) == 2


def test_read_never_retries_an_untrusted_real_tls_certificate():
    tls = tls_fixture.PersistentTransportTests
    tls.setUpClass()
    case = tls(methodName='runTest')
    try:
        case.setUp()
        reader = case.reader(trusted=False)
        attempts = []
        original = reader._opener.open
        def record(request, timeout):
            attempts.append(request)
            return original(request, timeout)
        reader._opener.open = record
        assert_error('connection_failed', lambda: read_rpc(reader))
        assert len(attempts) == 1 and case.server.requests == []
    finally:
        case.doCleanups()
        tls.tearDownClass()
