"""Separate, narrow runtime control channel; never refreshes profile bindings."""
import copy
import re

from .board_reader import BoardReader, BoardReadError, _endpoint


RUNTIME_TOOLS = frozenset({'reserve_execution', 'lookup_execution', 'start_execution',
    'heartbeat_execution', 'finish_execution', 'get_execution_control'})
REASONS = frozenset({'', 'completed', 'cancelled', 'quota_or_rate_limit', 'authentication_failed',
    'execution_failed', 'unavailable', 'timeout', 'cleanup_unconfirmed', 'cleanup_failed',
    'runner_error', 'protocol_error', 'policy_changed', 'claim_lost', 'stop_requested', 'owner_paused'})


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise BoardReadError('invalid_arguments')


class RuntimeBoardClient(BoardReader):
    """The worker registered this exact session before invoking this channel.

    Registration here would refresh a changed profile and defeat cancellation.
    All calls therefore use the original session without calling register().
    """
    @classmethod
    def from_worker(cls, client):
        return cls(copy.deepcopy(client._config), client.session_id, 3)

    @property
    def endpoint(self):
        return _endpoint(self._config['mcp_url'])

    def register(self):
        raise BoardReadError('runtime_registration_forbidden')

    def call(self, name, args):
        if name not in RUNTIME_TOOLS:
            raise BoardReadError('tool_not_allowed')
        if not isinstance(args, dict):
            raise BoardReadError('invalid_arguments')
        expected = {'execution_id'}
        if name == 'reserve_execution':
            expected = {'idempotency_key', 'project_id', 'ticket_id', 'work_id', 'turn_id', 'provider', 'model'}
        elif name == 'lookup_execution':
            expected = {'idempotency_key'}
        elif name in ('start_execution', 'heartbeat_execution', 'finish_execution'):
            expected.add('expected_revision')
        if name == 'finish_execution':
            expected.update({'status', 'cleanup_confirmed', 'reason'})
        if set(args) != expected:
            raise BoardReadError('invalid_arguments')
        for key, value in args.items():
            if key == 'expected_revision':
                if type(value) is not int or not 1 <= value < 2**63:
                    raise BoardReadError('invalid_arguments')
            elif key == 'cleanup_confirmed':
                if type(value) is not bool:
                    raise BoardReadError('invalid_arguments')
            elif key == 'status':
                if value not in ('completed', 'blocked', 'cancelled'):
                    raise BoardReadError('invalid_arguments')
            elif key == 'reason':
                if value not in REASONS:
                    raise BoardReadError('invalid_arguments')
            elif key == 'model':
                if (not isinstance(value, str) or not value.strip() or len(value) > 200
                        or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    raise BoardReadError('invalid_arguments')
            else:
                _identifier(value)
        self._initialize()
        return self._tool(name, {**copy.deepcopy(args), 'session_id': self.session_id})
