"""Separate, narrow runtime control channel; never refreshes profile bindings."""
import copy
import re

from .board_reader import BoardReader, BoardReadError, _endpoint


RUNTIME_TOOLS = frozenset({'reserve_execution', 'lookup_execution', 'start_execution',
    'heartbeat_execution', 'finish_execution', 'get_execution_control'})
REASONS = frozenset({'', 'completed', 'cancelled', 'quota_or_rate_limit', 'authentication_failed',
    'execution_failed', 'unavailable', 'timeout', 'cleanup_unconfirmed', 'cleanup_failed',
    'runner_error', 'protocol_error', 'policy_changed', 'claim_lost', 'stop_requested', 'owner_paused'})

RESERVATION_FAILURES = frozenset({'owner_paused', 'capacity_exhausted', 'active_ticket_claim_required',
    'profile_changed', 'review_skills_changed', 'distinct_live_review_claim_required', 'ordinary_review_required',
    'review_prerequisites_changed', 'review_prerequisites_history_unavailable', 'review_policy_history_unavailable',
    'review_scope_changed', 'execution_admission_refused', 'execution_key_conflict', 'execution_turn_conflict',
    'project_archived', 'ticket_project_mismatch', 'agent_authentication_required'})
RUNTIME_FAILURE_CODES = RESERVATION_FAILURES | frozenset({'unrecognized_code', 'tool_rejected',
    'authentication_failed', 'permission_denied', 'http_error', 'rpc_rejected', 'invalid_response',
    'connection_failed', 'timeout', 'protocol_mismatch', 'runtime_receipt_invalid', 'runtime_scope_mismatch',
    'runtime_identity_changed', 'runtime_reservation_unconfirmed', 'runtime_reservation_requires_review',
    'runtime_start_unconfirmed'})


def runtime_failure(value):
    """Closed telemetry only; never admission, cleanup or retry evidence."""
    if (type(value) is dict and set(value)=={'stage','code'}
            and type(value['stage']) is str and value['stage'] in ('reserve','receipt','start')
            and type(value['code']) is str and value['code'] in RUNTIME_FAILURE_CODES):
        return dict(value)
    return None


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', value):
        raise BoardReadError('invalid_arguments')


class RuntimeBoardClient(BoardReader):
    """Use the original registered session; refreshing its profile would defeat Stop."""
    @classmethod
    def from_worker(cls, client):
        return cls(copy.deepcopy(client._config), client.session_id, 3)

    @property
    def endpoint(self):
        return _endpoint(self._config['mcp_url'])

    def register(self):
        raise BoardReadError('runtime_registration_forbidden')

    def _tool_failure(self, name, result):
        error=super()._tool_failure(name,result)
        content=result.get('content')
        if (name in ('reserve_execution','start_execution') and 'structuredContent' not in result
                and type(content) is list and len(content)==1):
            block=content[0]
            if type(block) is dict and set(block)=={'type','text'} and block['type']=='text' and type(block['text']) is str:
                prefix='Error executing tool '+name+': '
                text=block['text']
                code=text[len(prefix):] if text.startswith(prefix) else None
                if code in RESERVATION_FAILURES:error._runtime_tool_code=code
        return error

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
