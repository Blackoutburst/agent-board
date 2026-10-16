"""Explicit confirmed-feed and reply channel; no task, queue or server action."""
from __future__ import annotations

import copy
import json
import re

from .board_reader import BoardReader, BoardReadError, MAX_CONNECTION_BYTES, _endpoint, _text
from .worker_client import _arguments as read_arguments

MESSAGE_TOOLS = frozenset({'get_ticket', 'get_thread', 'get_message', 'open_message_feed',
    'read_message_feed', 'checkpoint_message_feed', 'post_message'})
MAX_REPLY_BYTES = 8000


def _identifier(value):
    if not _text(value, 256):raise BoardReadError('invalid_arguments')


def _hex(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{32}', value):
        raise BoardReadError('invalid_arguments')


def arguments(name, args):
    if name not in MESSAGE_TOOLS:raise BoardReadError('tool_not_allowed')
    if not isinstance(args, dict):raise BoardReadError('invalid_arguments')
    values = copy.deepcopy(args)
    if name in ('get_ticket', 'get_thread', 'get_message'):
        return read_arguments(name, values)
    if name == 'open_message_feed':
        if set(values) != {'idempotency_key', 'start_at', 'limit'}:raise BoardReadError('invalid_arguments')
        _identifier(values['idempotency_key'])
        if values['start_at'] not in ('beginning', 'now') or type(values['limit']) is not int or not 1 <= values['limit'] <= 200:
            raise BoardReadError('invalid_arguments')
    elif name == 'read_message_feed':
        if set(values) != {'feed_id'}:raise BoardReadError('invalid_arguments')
        _hex(values['feed_id'])
    elif name == 'checkpoint_message_feed':
        if set(values) != {'feed_id', 'delivery_id', 'expected_checkpoint'}:raise BoardReadError('invalid_arguments')
        _hex(values['feed_id']);_hex(values['delivery_id'])
        if type(values['expected_checkpoint']) is not int or not 0 <= values['expected_checkpoint'] < 2**63:
            raise BoardReadError('invalid_arguments')
    elif name == 'post_message':
        if set(values) != {'thread_id', 'body', 'reply_to', 'idempotency_key'}:raise BoardReadError('invalid_arguments')
        for key in ('thread_id', 'reply_to', 'idempotency_key'):_identifier(values[key])
        body = values['body']
        try:
            if not isinstance(body, str) or not body.strip() or '\0' in body or len(body.encode('utf-8')) > MAX_REPLY_BYTES:
                raise ValueError
        except (ValueError, UnicodeError):raise BoardReadError('invalid_arguments') from None
    try:
        if len(json.dumps(values, allow_nan=False).encode()) > MAX_CONNECTION_BYTES-1024:raise ValueError
    except (TypeError, ValueError, UnicodeError):raise BoardReadError('invalid_arguments') from None
    return values


class MessageBoardClient(BoardReader):
    @property
    def endpoint(self):return _endpoint(self._config['mcp_url'])

    def call(self, name, args):
        values = arguments(name, args)  # Validate before any registration/network.
        if not self._registered:self.register()
        return self._tool(name, {**values, 'session_id': self.session_id})
