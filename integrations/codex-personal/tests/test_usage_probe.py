import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.usage_probe import read_usage
from blackoutburst_agents.usage_observation import UsageError


class UsageProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fake(self, behavior='success'):
        path = self.directory / 'codex'
        self.transcript = self.directory / 'transcript'
        path.write_text('#!' + sys.executable + '\n' + '''import json, os, sys, time
behavior = ''' + repr(behavior) + '''
transcript = ''' + repr(str(self.transcript)) + '''
def send(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    with open(transcript, 'a') as stream: stream.write(json.dumps(message) + '\\n')
    method = message['method']
    if behavior == 'timeout': time.sleep(30)
    if behavior == 'oversize': print('PRIVATE' * 1000, file=sys.stderr, flush=True); time.sleep(30)
    if behavior == 'exit': raise SystemExit(2)
    if method == 'initialize':
        send({'id': 1, 'result': {'userAgent': 'codex_cli_rs/1.2.3 (PRIVATE_OS)', 'codexHome': '/PRIVATE/HOME'}})
    elif method == 'account/read':
        send({'id': 2, 'result': {'account': None if behavior == 'noauth' else {'type': 'apiKey' if behavior == 'apikey' else 'chatgpt', 'email': 'PRIVATE_EMAIL'}}})
    elif method == 'account/rateLimits/read':
        if behavior == 'serverrequest': send({'id': 99, 'method': 'account/chatgptAuthTokens/refresh', 'params': {'previousAccountId': 'PRIVATE'}})
        elif behavior == 'rpcerror': send({'id': 3, 'error': {'code': -32601, 'message': 'PRIVATE_SECRET'}})
        elif behavior == 'wrongid': send({'id': 99, 'result': {}})
        else:
            send({'method': 'account/rateLimits/updated', 'params': {'rateLimits': {'primary': None}}})
            send({'id': 3, 'result': {'accountId': 'PRIVATE_ID', 'rateLimits': {'primary': {'usedPercent': 42}}, 'ordinaryUsageAllowed': True}})
''')
        path.chmod(0o700); return path

    def test_success_only_sends_read_methods_and_drops_private_data(self):
        value = read_usage(self.fake())
        self.assertEqual(value['snapshot']['data_status'], 'observed')
        self.assertEqual(value['snapshot']['cli_version'], '1.2.3')
        self.assertNotIn('PRIVATE', json.dumps(value))
        calls = [json.loads(line) for line in self.transcript.read_text().splitlines()]
        self.assertEqual([c['method'] for c in calls], ['initialize', 'initialized', 'account/read', 'account/rateLimits/read'])
        self.assertEqual(calls[-1]['params'], {'excludeResetCreditDetails': True, 'supportsLunaReserve': False})
        self.assertEqual(calls[-2]['params'], {'refreshToken': False})

    def test_absent_or_api_key_auth_never_reads_limits_or_starts_login(self):
        for behavior, reason in [('noauth', 'auth_unavailable'), ('apikey', 'unsupported_auth')]:
            with self.subTest(behavior=behavior):
                self.transcript = self.directory / 'transcript'; self.transcript.unlink(missing_ok=True)
                value = read_usage(self.fake(behavior))
                self.assertEqual(value['snapshot']['unavailable_reason'], reason)
                self.assertNotIn('rateLimits/read', self.transcript.read_text())
                self.assertNotIn('login', self.transcript.read_text())

    def test_errors_timeouts_requests_and_oversize_are_bounded_and_redacted(self):
        for behavior, reason in [('timeout', 'timeout'), ('oversize', 'invalid_response'), ('exit', 'process_exited'),
                ('serverrequest', 'unsupported_cli'), ('rpcerror', 'unsupported_cli'), ('wrongid', 'invalid_response')]:
            with self.subTest(behavior=behavior):
                value = read_usage(self.fake(behavior), timeout=0.15 if behavior == 'timeout' else 2, max_output_bytes=1024)
                self.assertEqual(value['snapshot']['data_status'], 'unavailable')
                self.assertEqual(value['snapshot']['unavailable_reason'], reason)
                self.assertNotIn('PRIVATE', json.dumps(value))

    def test_selector_allocation_failure_stops_real_child_and_closes_pipes(self):
        original = subprocess.Popen; children = []
        def capture(*args, **kwargs):
            child = original(*args, **kwargs); children.append(child); return child
        with patch('blackoutburst_agents.usage_probe.subprocess.Popen', side_effect=capture), \
             patch('blackoutburst_agents.usage_probe.selectors.DefaultSelector', side_effect=OSError('PRIVATE')):
            value = read_usage(self.fake('timeout'))
        self.assertEqual(value['snapshot']['data_status'], 'unavailable')
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(all(pipe.closed for pipe in (children[0].stdin, children[0].stdout, children[0].stderr)))

    def test_unconfirmed_cleanup_discards_measurements(self):
        from blackoutburst_agents.usage_probe import _stop_process_group
        def cleaned_but_unproved(process): _stop_process_group(process); return False
        with patch('blackoutburst_agents.usage_probe._stop_process_group', side_effect=cleaned_but_unproved):
            value = read_usage(self.fake())
        self.assertEqual(value['snapshot']['unavailable_reason'], 'cleanup_unconfirmed')
        self.assertEqual(value['snapshot']['buckets'], [])

    def test_invalid_bounds_never_spawn(self):
        for kwargs in ({'timeout': True}, {'timeout': float('nan')}, {'timeout': 61}, {'max_output_bytes': True}, {'max_output_bytes': 1023}):
            with patch('blackoutburst_agents.usage_probe.subprocess.Popen') as spawn:
                with self.assertRaises(UsageError): read_usage('/not-used', **kwargs)
                spawn.assert_not_called()
