import argparse
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.model_policy import connection_identifier
from blackoutburst_agents.usage_cli import UsageBoardClient, configure_parser, read_observation, run
from blackoutburst_agents.usage_observation import UsageError, normalize_response


def observation():
    return {'observed_at': 1800000000.0, 'snapshot': normalize_response({
        'accountId': 'PRIVATE_ACCOUNT', 'rateLimits': {'primary': {'usedPercent': 42}}}, '1.2.3')}


class UsageCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.parser = argparse.ArgumentParser(); configure_parser(self.parser)

    def call(self, args):
        output = io.StringIO()
        with redirect_stdout(output): status = run(self.parser.parse_args(args))
        return status, json.loads(output.getvalue())

    def test_read_defaults_to_metadata_and_explicit_show_reveals_measurement(self):
        with patch('blackoutburst_agents.usage_cli.read_usage', return_value=observation()) as probe:
            status, value = self.call(['read', '--codex', '/fixture/codex'])
            self.assertEqual(status, 0); self.assertNotIn('used_percent', json.dumps(value))
            self.assertNotIn('PRIVATE', json.dumps(value))
            status, value = self.call(['read', '--codex', '/fixture/codex', '--show'])
            self.assertEqual(value['snapshot']['buckets'][0]['primary']['used_percent'], 42)
            probe.assert_called_with('/fixture/codex', timeout=30)

    def test_output_is_private_validated_and_can_be_read(self):
        path = self.directory / 'usage.json'
        with patch('blackoutburst_agents.usage_cli.read_usage', return_value=observation()):
            status, value = self.call(['read', '--codex', '/fixture/codex', '--output-private', str(path)])
        self.assertEqual(status, 0); self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(read_observation(path), observation())
        self.assertNotIn('PRIVATE', path.read_text())

    def test_observation_file_rejects_private_extras_symlink_hardlink_and_permissions(self):
        path = self.directory / 'usage.json'; path.write_text(json.dumps(observation())); path.chmod(0o600)
        link = self.directory / 'link'; link.symlink_to(path)
        with self.assertRaises(UsageError): read_observation(link)
        link.unlink(); os.link(path, link)
        with self.assertRaises(UsageError): read_observation(path)
        link.unlink(); path.chmod(0o644)
        with self.assertRaises(UsageError): read_observation(path)
        path.chmod(0o600); path.write_text(json.dumps({**observation(), 'token': 'PRIVATE'}))
        with self.assertRaises(UsageError): read_observation(path)

    def client(self):
        client = UsageBoardClient({'mcp_url': 'https://board.test/mcp', 'token': 'PRIVATE_TOKEN',
            'client': 'codex', 'identity_key': 'binding', 'name': 'Agent'}, 'session', 3)
        registration = {'identity': {'id': 'agent', 'owner_id': 'owner', 'device_id': 'device'}}
        client.register = Mock(return_value=registration)
        connection = connection_identifier({'board_endpoint': 'https://board.test/mcp', 'owner_id': 'owner', 'agent_id': 'agent'}, registration)
        client._tool = Mock(return_value={'connection_id': connection, 'owner_id': 'owner', 'agent_id': 'agent',
            'device_id': 'device', **observation(), 'data_status': 'observed', 'unexpected': 'PRIVATE'})
        return client

    def test_report_verifies_identity_and_forwards_only_exact_observation(self):
        client = self.client(); value = client.report('owner', 'agent', observation())
        self.assertNotIn('PRIVATE', json.dumps(value)); self.assertNotIn('used_percent', json.dumps(value))
        name, args = client._tool.call_args.args
        self.assertEqual(name, 'report_usage_observation')
        self.assertEqual(set(args), {'session_id', 'connection_id', 'observed_at', 'snapshot'})
        self.assertEqual(args['session_id'], 'session')
        for name in ('reserve_execution', 'claim_ticket', 'post_message'):
            with self.assertRaises(BoardReadError): client.call(name, {})

    def test_wrong_owner_or_profile_prevents_publication(self):
        for owner, agent in [('other', 'agent'), ('owner', 'other')]:
            client = self.client()
            with self.assertRaises(BoardReadError): client.report(owner, agent, observation())
            client._tool.assert_not_called()

    def test_remote_mismatches_and_diagnostics_never_print_private_values(self):
        client = self.client(); client._tool.return_value['owner_id'] = 'other'
        with self.assertRaises(BoardReadError): client.report('owner', 'agent', observation())
        with patch('blackoutburst_agents.usage_cli.read_usage', side_effect=RuntimeError('PRIVATE_TOKEN')):
            status, value = self.call(['read', '--codex', '/fixture'])
        self.assertEqual(status, 2); self.assertNotIn('PRIVATE', json.dumps(value))
