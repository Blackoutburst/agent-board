"""Paired coder discovery only; no claim, model, message or grant tools."""
from copy import deepcopy
import math
from pathlib import Path

from .board_reader import BoardReader, BoardReadError, _connection_data, _json, _text
from .development_pool_selection import file_document


class DevelopmentQueueDiscoveryClient(BoardReader):
    @classmethod
    def from_connection_file(cls, path, *, session_id, timeout=20):
        if (not _text(session_id, 200) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise BoardReadError('invalid_session_or_timeout')
        path = str(Path(path).expanduser().absolute())
        raw, pin = file_document(path)
        value = cls(_connection_data(_json(raw, 'invalid_connection')), session_id, float(timeout))
        value.connection_path, value.connection_pin = path, pin
        return value

    def registration(self, *, autonomous=False):
        if type(autonomous) is not bool:
            raise BoardReadError('invalid_arguments')
        self._registered = False
        self._initialize()
        result = self._tool('register_agent', dict(session_id=self.session_id, agent_kind=self._config['client'],
            identity_key=self._config['identity_key'], name=self._config['name'],
            label=self._config['name'][:180] + ' queue discovery', skills=[], autonomous=autonomous))
        if type(result.get('session')) is not dict or result['session'].get('id') != self.session_id:
            raise BoardReadError('session_mismatch')
        if autonomous and (result['session'].get('autonomous') is not True
                or type(result.get('policy')) is not dict or result['policy'].get('autonomy') != 'queue'):
            raise BoardReadError('development_queue_not_authorized')
        self._registered = True
        return result

    def call(self, name, args):
        if name not in ('get_work', 'get_ticket', 'get_project'):
            raise BoardReadError('tool_not_allowed')
        field = 'ticket_id' if name == 'get_ticket' else 'project_id'
        fields = {field, 'limit'} if name == 'get_work' else {field}
        if (type(args) is not dict or set(args) != fields or not _text(args[field], 200)
                or name == 'get_work' and (type(args['limit']) is not int or not 1 <= args['limit'] <= 200)):
            raise BoardReadError('invalid_arguments')
        if not self._registered:
            raise BoardReadError('development_queue_registration_required')
        return self._tool(name, {**deepcopy(args), 'session_id': self.session_id, 'view': 'full'})
