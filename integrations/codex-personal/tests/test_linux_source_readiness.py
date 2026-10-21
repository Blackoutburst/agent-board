"""Real local control descriptors; no process, namespace, filter or model."""
import errno
import fcntl
import os
import socket
import threading
from unittest.mock import patch

import pytest

from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents import linux_test_namespace as namespace


@pytest.fixture
def channels():
    # Darwin lacks AF_UNIX SOCK_SEQPACKET. Both kinds expose the same fd
    # O_NONBLOCK behavior after settimeout; Linux uses its real packet channel.
    kind = socket.SOCK_SEQPACKET if os.uname().sysname == 'Linux' else socket.SOCK_STREAM
    left, right = socket.socketpair(socket.AF_UNIX, kind)
    left.settimeout(namespace.STARTUP_SECONDS)
    try:
        yield left, right
    finally:
        left.close()
        right.close()


def frame():
    return canonical({'schema': 'linux-source-release-v1', 'nonce': 'a' * 32,
        'control': {'schema': 1, 'nonce': 'a' * 32, 'sequence': 2,
                    'started_at': 100.0, 'validated_at': 100.1, 'deadline': 101.5}})


def test_settimeout_fd_waits_for_delayed_source_ack_instead_of_eagain(channels):
    left, right = channels
    assert fcntl.fcntl(left.fileno(), fcntl.F_GETFL) & os.O_NONBLOCK
    with pytest.raises(BlockingIOError):
        os.read(left.fileno(), 1025)  # The previous inner path fails here.
    waiting, finished = threading.Event(), threading.Event()
    actual_select = namespace.select.select
    result, errors = [], []
    def observed_select(*args):
        waiting.set()
        return actual_select(*args)
    def reader():
        try:
            result.append(namespace.read_source_release(left))
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()
    with patch.object(namespace.select, 'select', side_effect=observed_select):
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            assert waiting.wait(2) and not finished.is_set()
            right.sendall(frame())
            assert finished.wait(2)
        finally:
            right.close()
            thread.join(2)
    assert not thread.is_alive() and not errors and result == [frame()]
    assert namespace.validate_source_release(result[0], 'a' * 32, _clock=lambda: 100.2)['deadline'] == 101.5


@pytest.mark.parametrize('failure', ['eof', 'oversize', 'timeout'])
def test_source_ack_read_has_closed_size_and_wait_bounds(channels, failure, monkeypatch):
    left, right = channels
    if failure == 'eof':
        right.close()
    elif failure == 'oversize':
        right.sendall(b'x' * 1025)
    else:
        monkeypatch.setattr(namespace, 'STARTUP_SECONDS', 0.01)
    with pytest.raises(DevelopmentError, match='linux_source_ack_missing'):
        namespace.read_source_release(left)


@pytest.mark.parametrize('failure', ['wrong_nonce', 'expired'])
def test_readiness_does_not_authorize_mutated_or_expired_source_frame(channels, failure):
    left, right = channels
    raw = frame().replace(b'a' * 32, b'b' * 32) if failure == 'wrong_nonce' else frame()
    right.sendall(raw)
    received = namespace.read_source_release(left)
    with pytest.raises(DevelopmentError):
        namespace.validate_source_release(received, 'a' * 32, _clock=lambda: 101.5 if failure == 'expired' else 100.2)


def test_source_ack_real_descriptor_error_is_not_reported_as_timeout(channels):
    left, right = channels
    right.sendall(frame())
    with patch.object(namespace.os, 'read', side_effect=OSError(errno.EIO, 'synthetic descriptor error')):
        with pytest.raises(OSError) as observed:
            namespace.read_source_release(left)
    assert observed.value.errno == errno.EIO


def test_closed_source_descriptor_error_is_not_reported_as_timeout(channels):
    left, _right = channels
    left.close()
    with pytest.raises((OSError, ValueError)):
        namespace.read_source_release(left)
