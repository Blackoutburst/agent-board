"""Explicit local usage inspection/reporting; no execution or permission change."""
import copy
import json
import os
from pathlib import Path
import re
import stat

from .board_reader import BoardReader, BoardReadError, _endpoint, _json
from .model_policy import connection_identifier
from .runner_cli import _write_private
from .usage_observation import UsageError, validate_snapshot, validate_time, public_receipt
from .usage_probe import read_usage


def configure_parser(parser):
    commands = parser.add_subparsers(dest='usage_command', required=True)
    read = commands.add_parser('read', help='Lire localement les limites sans lancer de modèle.')
    read.add_argument('--codex', required=True)
    read.add_argument('--output-private', '--outputprivate', dest='output_private', type=Path)
    read.add_argument('--show', action='store_true', help='Afficher explicitement les pourcentages du compte local.')
    read.add_argument('--timeout', type=float, default=30)
    report = commands.add_parser('report', help='Publier les seules mesures autorisées de cette connexion.')
    report.add_argument('--connection-file', required=True, type=Path)
    report.add_argument('--session', required=True)
    report.add_argument('--owner', required=True)
    report.add_argument('--agent', required=True)
    report.add_argument('--observation-file', required=True, type=Path)


def read_observation(path):
    descriptor = None
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 65536):
            raise UsageError('insecure_observation_file')
        content = bytearray()
        while len(content) <= 65536:
            part = os.read(descriptor, min(8192, 65537 - len(content)))
            if not part:
                break
            content.extend(part)
        if len(content) > 65536:
            raise UsageError('invalid_usage_observation')
        value = _json(bytes(content), 'invalid_usage_observation')
        if type(value) is not dict or set(value) != {'observed_at', 'snapshot'}:
            raise UsageError()
        return {'observed_at': validate_time(value['observed_at']), 'snapshot': validate_snapshot(value['snapshot'])}
    except OSError:
        raise UsageError('observation_file_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


class UsageBoardClient(BoardReader):
    def report(self, owner_id, agent_id, observation):
        observation = {'observed_at': validate_time(observation['observed_at']),
                       'snapshot': validate_snapshot(observation['snapshot'])}
        registration = self.register()
        identity = registration.get('identity')
        if (type(identity) is not dict or identity.get('owner_id') != owner_id
                or identity.get('id') != agent_id or type(identity.get('device_id')) is not str):
            raise BoardReadError('usage_connection_mismatch')
        scope = {'board_endpoint': _endpoint(self._config['mcp_url']), 'owner_id': owner_id, 'agent_id': agent_id}
        connection = connection_identifier(scope, registration)
        value = self._tool('report_usage_observation', {'session_id': self.session_id,
            'connection_id': connection, **copy.deepcopy(observation)})
        # Never forward a remote response unfiltered to terminal output.
        if (value.get('connection_id') != connection or value.get('owner_id') != owner_id
                or value.get('agent_id') != agent_id or value.get('device_id') != identity['device_id']
                or value.get('observed_at') != observation['observed_at']
                or value.get('snapshot') != observation['snapshot']
                or value.get('data_status') not in ('observed', 'stale', 'unavailable')):
            raise BoardReadError('invalid_response')
        return {'status': 'reported', 'connection_id': connection,
                'data_status': value['data_status'], 'observed_at': observation['observed_at']}

    def call(self, name, args):
        raise BoardReadError('tool_not_allowed')


def run(args):
    try:
        if args.usage_command == 'read':
            observation = read_usage(args.codex, timeout=args.timeout)
            if args.output_private is not None:
                _write_private(args.output_private, json.dumps(observation, ensure_ascii=False, allow_nan=False) + '\n')
            output = observation if args.show else public_receipt(observation)
        else:
            observation = read_observation(args.observation_file)
            client = UsageBoardClient.from_connection_file(args.connection_file, session_id=args.session)
            output = client.report(args.owner, args.agent, observation)
        print(json.dumps(output, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except KeyboardInterrupt:
        print(json.dumps({'status': 'unavailable', 'reason': 'interrupted'})); return 130
    except Exception as error:
        code = getattr(error, 'code', 'usage_unavailable')
        if type(code) is not str or not re.fullmatch('[a-z][a-z0-9_]{0,100}', code):
            code = 'usage_unavailable'
        print(json.dumps({'status': 'unavailable', 'reason': code})); return 2
