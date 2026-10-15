"""Explicit questions to a locally selected human, with idempotent lookup."""
from __future__ import annotations

import json

from .board_reader import BoardReadError, MAX_CONNECTION_BYTES, _text
from .board_worker import MAX_HUMAN_QUESTION_BYTES
from .worker_client import WorkerBoardClient


class HumanWorkerClient(WorkerBoardClient):
    """Add questions only; no approval requests, actions or general messages."""

    def call(self, name, args):
        if name not in ('request_human_input', 'lookup_human_request'):
            return super().call(name, args)
        required = {'ticket_id', 'owner_id', 'question', 'idempotency_key', 'kind'}
        if name == 'request_human_input':
            required.add('expected_version')
        if (not isinstance(args, dict) or set(args) != required
                or not _text(args['ticket_id'], 256) or not _text(args['owner_id'], 256)
                or not _text(args['idempotency_key'], 200)
                or args['kind'] != 'question'
                or not isinstance(args['question'], str) or not args['question'].strip()
                or '\x00' in args['question']):
            raise BoardReadError('invalid_arguments')
        if name == 'request_human_input' and (type(args['expected_version']) is not int
                or not 1 <= args['expected_version'] < 2 ** 63):
            raise BoardReadError('invalid_arguments')
        try:
            values = dict(args)
            if len(values['question'].encode('utf-8', 'strict')) > MAX_HUMAN_QUESTION_BYTES:
                raise ValueError
            json.dumps(values, ensure_ascii=False, allow_nan=False).encode('utf-8', 'strict')
            if len(json.dumps(values, allow_nan=False).encode('utf-8')) > MAX_CONNECTION_BYTES - 1024:
                raise ValueError
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise BoardReadError('invalid_arguments') from None
        if not self._registered:
            self.register()
        values['session_id'] = self.session_id
        return self._tool(name, values)
