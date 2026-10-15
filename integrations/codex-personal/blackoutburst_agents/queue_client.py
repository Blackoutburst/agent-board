"""Narrow, explicitly opted-in queue selection alongside the existing worker."""
from __future__ import annotations

import copy
import json

from .board_reader import BoardReadError, MAX_CONNECTION_BYTES, _text
from .human_client import HumanWorkerClient
from .worker_client import WorkerBoardClient


class QueueBoardClient(WorkerBoardClient):
    """Read-only registration by default; selection requires a separate opt-in."""

    def __init__(self, config, session_id, timeout):
        super().__init__(config, session_id, timeout)
        self._selection_enabled = False

    def register(self):
        self._selection_enabled = False
        return super().register()

    def register_for_selection(self):
        self._selection_enabled = self._registered = False
        self._initialize()
        result = self._tool('register_agent', {'session_id': self.session_id,
            'agent_kind': self._config['client'], 'identity_key': self._config['identity_key'],
            'name': self._config['name'], 'label': self._config['name'][:192] + ' queue',
            'skills': [], 'autonomous': True})
        identity, session, policy = (result.get(key) for key in ('identity', 'session', 'policy'))
        if (not all(isinstance(item, dict) for item in (identity, session, policy))
                or session.get('id') != self.session_id or session.get('autonomous') is not True
                or not identity.get('id') or identity['id'] != session.get('agent_id')
                or identity.get('owner_id') != session.get('owner_id')
                or identity.get('device_id') != session.get('device_id')
                or identity.get('agent_kind') != self._config['client']
                or any(policy.get(key) is not True for key in ('enabled', 'allow_read', 'allow_requests'))
                or policy.get('autonomy') != 'queue'):
            raise BoardReadError('selection_permission_denied')
        self._selection_enabled = self._registered = True
        return result

    def call(self, name, args):
        if name not in ('get_project', 'get_work', 'claim_next_ticket', 'lookup_ticket_selection'):
            return super().call(name, args)
        allowed = {'project_id'}
        if name == 'get_work':
            allowed.add('limit')
        if name in ('claim_next_ticket', 'lookup_ticket_selection'):
            allowed.add('idempotency_key')
        if (not isinstance(args, dict) or set(args) - allowed or 'project_id' not in args
                or not _text(args['project_id'], 256)
                or ('limit' in args and (type(args['limit']) is not int or not 1 <= args['limit'] <= 200))
                or ('idempotency_key' in allowed and not _text(args.get('idempotency_key'), 200))):
            raise BoardReadError('invalid_arguments')
        try:
            values = copy.deepcopy(args)
            encoded = json.dumps(values, ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')
            if len(encoded) > MAX_CONNECTION_BYTES - 1024:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BoardReadError('invalid_arguments') from None
        if name == 'claim_next_ticket' and not self._selection_enabled:
            raise BoardReadError('selection_not_authorized')
        if not self._registered:
            self.register()
        return self._tool(name, {**values, 'session_id': self.session_id})


class HumanQueueBoardClient(HumanWorkerClient, QueueBoardClient):
    """Optional bounded clarification; no discussion, actions or approvals."""
