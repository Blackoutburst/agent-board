"""Narrow MCP client for one human-selected ticket operation.

The existing reader retains its read-only contract. This separate client adds
only explicit claims, lease renewal and review/blocked results. It cannot take
queue work, reassign a ticket, mark work done or publish discussion messages.
"""
from __future__ import annotations

import copy
import json

from .board_reader import BoardReader, BoardReadError, MAX_CONNECTION_BYTES, _endpoint, _text


WORKER_TOOLS = frozenset({'get_ticket', 'get_thread', 'get_message', 'claim_ticket', 'renew_ticket', 'update_ticket'})


def _identifier(value):
    if not _text(value, 256):
        raise BoardReadError('invalid_arguments')


def _prose(value):
    if not isinstance(value, str) or len(value) > 20000 or '\x00' in value:
        raise BoardReadError('invalid_arguments')


def _arguments(name, args):
    if not isinstance(args, dict):
        raise BoardReadError('invalid_arguments')
    try:
        values = copy.deepcopy(args)
        json.dumps(values, ensure_ascii=False, allow_nan=False).encode('utf-8')
        serialized = json.dumps(values, allow_nan=False).encode('utf-8')
        # Reserve space for the JSON-RPC envelope and injected session, before
        # registration can contact the board with an otherwise invalid call.
        if len(serialized) > MAX_CONNECTION_BYTES - 1024:
            raise ValueError
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BoardReadError('invalid_arguments') from None
    if name == 'get_message':
        if set(values) != {'thread_id','message_id'}:
            raise BoardReadError('invalid_arguments')
        _identifier(values['thread_id']);_identifier(values['message_id'])
        return values
    if name == 'get_thread':
        if set(values) - {'thread_id', 'before', 'limit'} or 'thread_id' not in values:
            raise BoardReadError('invalid_arguments')
        _identifier(values['thread_id'])
        if values.get('before') is not None:
            _identifier(values['before'])
        if 'limit' in values and (type(values['limit']) is not int or not 1 <= values['limit'] <= 200):
            raise BoardReadError('invalid_arguments')
        return values
    allowed = {'ticket_id'}
    required = {'ticket_id'}
    if name != 'get_ticket':
        allowed.add('expected_version')
        required.add('expected_version')
    if name == 'renew_ticket':
        allowed.add('progress')
    if name == 'update_ticket':
        allowed.add('changes')
        required.add('changes')
    if set(values) - allowed or not required.issubset(values):
        raise BoardReadError('invalid_arguments')
    _identifier(values['ticket_id'])
    if 'expected_version' in values and (type(values['expected_version']) is not int
                                        or not 1 <= values['expected_version'] < 2 ** 63):
        raise BoardReadError('invalid_arguments')
    if 'progress' in values:
        _prose(values['progress'])
    if name == 'update_ticket':
        changes = values['changes']
        if (not isinstance(changes, dict) or not changes
                or set(changes) - {'status', 'outcome', 'evidence', 'release_claim',
                                   'blocked_reason', 'recheck_after_seconds'}):
            raise BoardReadError('invalid_arguments')
        if ('blocked_reason' in changes or 'recheck_after_seconds' in changes) and changes.get('status') != 'blocked':
            raise BoardReadError('invalid_arguments')
        if 'blocked_reason' in changes:
            _prose(changes['blocked_reason'])
        if 'recheck_after_seconds' in changes and (type(changes['recheck_after_seconds']) is not int
                                                   or not 300 <= changes['recheck_after_seconds'] <= 1209600):
            raise BoardReadError('invalid_arguments')
        if 'status' in changes and changes['status'] not in ('review', 'blocked'):
            raise BoardReadError('invalid_arguments')
        if 'release_claim' in changes and type(changes['release_claim']) is not bool:
            raise BoardReadError('invalid_arguments')
        for field in ('outcome', 'evidence'):
            if field in changes:
                _prose(changes[field])
    return values


class WorkerBoardClient(BoardReader):
    """Pinned existing connection; registration remains non-autonomous."""

    def __init__(self, config, session_id, timeout):
        pinned = copy.deepcopy(config)
        self._pinned_endpoint = _endpoint(pinned.get('mcp_url'))
        super().__init__(pinned, session_id, timeout)

    @property
    def endpoint(self):
        return self._pinned_endpoint

    def call(self, name, args):
        if not isinstance(name, str) or name not in WORKER_TOOLS:
            raise BoardReadError('tool_not_allowed')
        values = _arguments(name, args)
        if not self._registered:
            self.register()
        values['session_id'] = self.session_id
        return self._tool(name, values)
