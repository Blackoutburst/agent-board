"""One scoped activity feed in addition to the explicit worker's operations."""
from __future__ import annotations

from .board_reader import BoardReadError, _json, _text
from .worker_client import WorkerBoardClient
from .human_client import HumanWorkerClient


class EventBoardClient(WorkerBoardClient):
    """No inbox cursor, queue selection, message posting or autonomous registration."""

    def call(self, name, args):
        if name != 'list_activity':
            return super().call(name, args)
        if (not isinstance(args, dict) or set(args) != {'project_id', 'ticket_id', 'after', 'limit', 'order'}
                or not _text(args['project_id'], 256) or not _text(args['ticket_id'], 256)
                or type(args['after']) is not int or not 0 <= args['after'] < 2 ** 63
                or type(args['limit']) is not int or not 1 <= args['limit'] <= 200
                or args['order'] != 'asc'):
            raise BoardReadError('invalid_arguments')
        try:
            for key in ('project_id', 'ticket_id'):
                args[key].encode('utf-8', 'strict')
        except UnicodeError:
            raise BoardReadError('invalid_arguments') from None
        values = {**args, 'session_id': self.session_id}
        if not self._registered:
            self.register()
        result = self._rpc('tools/call', {'name': 'list_activity', 'arguments': values})
        if not isinstance(result, dict) or ('isError' in result and type(result['isError']) is not bool):
            raise BoardReadError('invalid_response')
        if result.get('isError'):
            raise BoardReadError('tool_rejected')
        if 'structuredContent' in result:
            value = result['structuredContent']
        else:
            content = result.get('content')
            if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                    or content[0].get('type') != 'text'):
                raise BoardReadError('invalid_response')
            value = _json(content[0].get('text'), 'invalid_response')
        if isinstance(value, dict) and set(value) == {'result'}:
            value = value['result']
        if (not isinstance(value, list) or len(value) > args['limit']
                or any(not isinstance(item, dict) for item in value)):
            raise BoardReadError('invalid_response')
        return value


class HumanEventBoardClient(HumanWorkerClient, EventBoardClient):
    """Explicit opt-in: the scoped event feed plus bounded question/lookup.

    Both parent clients retain their argument validation. Queue selection,
    approvals, actions, messages and autonomous registration remain unavailable.
    """
