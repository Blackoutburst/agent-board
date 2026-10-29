"""Local closed transport tags describe a failure, never authorize a retry."""
import http.client
import json
import ssl
import urllib.error

import pytest

from blackoutburst_agents.board_reader import BoardReadError, connection_failure_diagnostic
from blackoutburst_agents.development_pool_delivery import _delivery_failure, delivery_boundary_diagnostic
from test_mcp_interrupted_read import synthetic, read_rpc


@pytest.mark.parametrize('error,kind', [
    (http.client.RemoteDisconnected('PRIVATE_MESSAGE'), 'remote_disconnected'),
    (ConnectionResetError('PRIVATE_PATH'), 'reset'), (BrokenPipeError('PRIVATE_ERRNO'), 'broken_pipe'),
    (ConnectionAbortedError('PRIVATE_MESSAGE'), 'aborted'),
    (http.client.IncompleteRead(b'PRIVATE_PARTIAL_BYTES'), 'incomplete_read'),
    (ssl.SSLCertVerificationError('PRIVATE_CERT_PATH'), 'ssl_error'),
    (ssl.SSLError('PRIVATE_TLS_MESSAGE'), 'ssl_error'), (OSError('PRIVATE_PATH'), 'other_os'),
    (http.client.BadStatusLine('PRIVATE_STATUS'), 'http_state'), (ValueError('PRIVATE_DATA'), 'value_error'),
    (urllib.error.URLError(ConnectionResetError('PRIVATE_WRAPPED')), 'reset')])
def test_local_types_emit_closed_tags_with_unchanged_public_error_and_no_mutation_retry(error, kind):
    reader, opener = synthetic(error)
    with pytest.raises(BoardReadError) as caught:
        reader._rpc('tools/call', {'name': 'update_ticket', 'arguments': {}})
    public = caught.value
    assert type(public) is BoardReadError and public.code == str(public) == 'connection_failed'
    assert len(opener.requests) == 1
    assert connection_failure_diagnostic(public) == kind
    failure = _delivery_failure(public, 'board_handoff')
    diagnostic = delivery_boundary_diagnostic(failure._development_delivery_diagnostic)
    assert diagnostic == {'status': 'delivery_diagnostic', 'kind': 'development_result_delivery',
        'phase': 'board_handoff', 'category': 'board_read_error', 'code': 'connection_failed_'+kind}
    assert 'PRIVATE_' not in json.dumps(diagnostic) and 'PRIVATE_' not in repr(public)


def test_exhausted_read_reports_the_last_local_failure_without_changing_retry_count():
    reader, opener = synthetic(ConnectionResetError('PRIVATE_FIRST'))
    initial = opener.open
    def final(request, timeout):
        if not opener.requests:return initial(request, timeout)
        opener.requests.append((json.loads(request.data), timeout))
        raise http.client.RemoteDisconnected('PRIVATE_SECOND')
    opener.open = final
    with pytest.raises(BoardReadError) as caught:read_rpc(reader)
    assert type(caught.value) is BoardReadError and caught.value.code == 'connection_failed'
    assert len(opener.requests) == 2
    assert connection_failure_diagnostic(caught.value) == 'remote_disconnected'


@pytest.mark.parametrize('error', [ssl.SSLError('PRIVATE'), http.client.BadStatusLine('PRIVATE'), ValueError('PRIVATE')])
def test_descriptive_ssl_protocol_or_value_tag_never_makes_a_read_repeatable(error):
    reader, opener = synthetic(error)
    with pytest.raises(BoardReadError) as caught:read_rpc(reader)
    assert type(caught.value) is BoardReadError and caught.value.code == 'connection_failed'
    assert connection_failure_diagnostic(caught.value) is not None
    assert len(opener.requests) == 1


@pytest.mark.parametrize('tag', [None, 'reset', ('reset',), (object(), 'reset'),
    (object(), 'PRIVATE_MESSAGE'), (object(), 'reset', 'extra'), {}])
def test_declared_or_unissued_tags_never_reclassify_arbitrary_board_errors(tag):
    error = BoardReadError('connection_failed')
    error._connection_failure_kind = tag
    assert connection_failure_diagnostic(error) is None
    failure = _delivery_failure(error, 'board_handoff')
    assert delivery_boundary_diagnostic(failure._development_delivery_diagnostic)['code'] == 'connection_failed'


def test_foreign_exception_properties_are_never_read_for_diagnostics():
    class Foreign(BoardReadError):
        def __getattribute__(self, name):
            if name in ('code', '__dict__', '_connection_failure_kind'):
                raise AssertionError('Do not inspect foreign exception data')
            return super().__getattribute__(name)
    error = Foreign('PRIVATE')
    assert connection_failure_diagnostic(error) is None
    failure = _delivery_failure(error, 'board_handoff')
    assert delivery_boundary_diagnostic(failure._development_delivery_diagnostic)['category'] == 'unknown_error'
