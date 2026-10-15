"""Bounded local app-server usage read; no model, login or quota mutation."""
from __future__ import annotations
import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time

from .board_reader import _json, BoardReadError
from .subscription_probe import validate_executable, sanitized_environment, _stop_process_group
from .usage_observation import normalize_response, unavailable, UsageError

MAX_LINE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024 * 1024


def read_usage(executable, *, timeout=30, max_output_bytes=MAX_TOTAL_BYTES):
    """Return only an allowlisted observation; stderr and raw RPC stay in memory."""
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60
            or type(max_output_bytes) is not int or not 1024 <= max_output_bytes <= MAX_TOTAL_BYTES):
        raise UsageError('invalid_probe_configuration')
    executable = validate_executable(executable)
    if os.name != 'posix':
        return {'observed_at': time.time(), 'snapshot': unavailable('unsupported_cli')}
    process, selector = None, None
    version, reason, snapshot = 'unknown', 'transport_error', None
    observed_at = time.time()
    cleaned = True
    with tempfile.TemporaryDirectory(prefix='codex-usage-') as cwd:
        try:
            process = subprocess.Popen([str(executable), 'app-server', '--stdio', '-c', 'analytics.enabled=false'],
                cwd=cwd, env=sanitized_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, shell=False, start_new_session=True)
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            buffer, total, waiting = b'', 0, 1
            deadline = time.monotonic() + timeout

            def send(value):
                process.stdin.write(json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8') + b'\n')
                process.stdin.flush()

            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'blackoutburst_usage_observer', 'version': '1.0.0'},
                'capabilities': {'experimentalApi': False, 'explicitGatewayOauth': True}}})
            done = False
            while not done and time.monotonic() < deadline:
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > max_output_bytes:
                        raise UsageError('invalid_response')
                    if key.data == 'stderr':
                        continue
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        if len(line) > MAX_LINE_BYTES:
                            raise UsageError('invalid_response')
                        message = _json(line, 'invalid_response')
                        if type(message) is not dict:
                            raise UsageError('invalid_response')
                        if 'method' in message:
                            # Sparse notifications are never snapshots; server RPCs
                            # (including external-token refresh) are not supported.
                            if 'id' in message:
                                raise UsageError('unsupported_cli')
                            continue
                        if type(message.get('id')) is not int or message['id'] != waiting:
                            raise UsageError('invalid_response')
                        if 'error' in message:
                            error = message['error']
                            code = error.get('code') if type(error) is dict else None
                            raise UsageError('unsupported_cli' if code == -32601 else 'transport_error')
                        result = message.get('result')
                        if type(result) is not dict:
                            raise UsageError('invalid_response')
                        if waiting == 1:
                            ua = result.get('userAgent')
                            match = re.search(r'codex(?:_cli_rs|-cli)?[/ ]([0-9][A-Za-z0-9._+-]{0,79})(?:\s|$)', ua) if type(ua) is str else None
                            version = match.group(1) if match else 'unknown'
                            send({'method': 'initialized'})
                            waiting = 2
                            send({'id': 2, 'method': 'account/read', 'params': {'refreshToken': False}})
                        elif waiting == 2:
                            account = result.get('account')
                            if account is None:
                                raise UsageError('auth_unavailable')
                            if type(account) is not dict or account.get('type') != 'chatgpt':
                                raise UsageError('unsupported_auth')
                            waiting = 3
                            send({'id': 3, 'method': 'account/rateLimits/read', 'params': {
                                'excludeResetCreditDetails': True, 'supportsLunaReserve': False}})
                        else:
                            snapshot = normalize_response(result, version)
                            observed_at = time.time()
                            done = True
                            break
                    if len(buffer) > MAX_LINE_BYTES:
                        raise UsageError('invalid_response')
                    if done:
                        break
                if process.poll() is not None and not selector.get_map() and not done:
                    raise UsageError('process_exited')
            if not done:
                reason = 'timeout'
        except (UsageError, BoardReadError) as error:
            reason = error.code
        except (OSError, ValueError, TypeError, RecursionError):
            reason = 'transport_error'
        finally:
            try:
                if process is not None:
                    cleaned = _stop_process_group(process)
                    try:
                        os.killpg(process.pid, 0)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        cleaned = False
                    else:
                        cleaned = False
            finally:
                try:
                    if selector is not None:
                        selector.close()
                finally:
                    if process is not None:
                        for pipe in (process.stdin, process.stdout, process.stderr):
                            if pipe is not None:
                                pipe.close()
    if not cleaned:
        snapshot = unavailable('cleanup_unconfirmed', version)
    elif snapshot is None:
        snapshot = unavailable(reason, version)
    return {'observed_at': observed_at if snapshot['data_status'] == 'observed' else time.time(), 'snapshot': snapshot}
