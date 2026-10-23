"""Explicit reviewer registration, then a pinned read-only worker channel."""
from copy import deepcopy

from .board_reader import BoardReadError, _text
from .development_client import DevelopmentBoardClient
from .worker_client import _arguments


class DeliveryReviewBoardClient(DevelopmentBoardClient):
    """The controller registers before claiming; dispatch never registers.

    A protected registration snapshot fixes the session/profile used to build
    context. Every read and Runtime action still uses server authorization.
    No ticket mutation is available on this review channel.
    """
    def register_initial(self, *, skills):
        if (self._registered or type(skills) is not list or not 1 <= len(skills) <= 32
                or any(not _text(item, 100) for item in skills)
                or 'review' not in skills or len(set(skills)) != len(skills)):
            raise BoardReadError('invalid_reviewer_registration')
        self._initialize()
        result = self._tool('register_agent', dict(session_id=self.session_id,
            agent_kind=self._config['client'], identity_key=self._config['identity_key'],
            name=self._config['name'], label=self._config['name'][:192]+' review',
            skills=deepcopy(skills), autonomous=False))
        self.pin_registration(result)
        return deepcopy(result)

    def pin_registration(self, value):
        if (type(value) is not dict or type(value.get('session')) is not dict
                or value['session'].get('id') != self.session_id):
            raise BoardReadError('invalid_reviewer_registration')
        self._review_registration = deepcopy(value)
        self._registered = True

    def register(self):
        if not self._registered or not hasattr(self, '_review_registration'):
            raise BoardReadError('reviewer_registration_required')
        return deepcopy(self._review_registration)

    def call(self, name, args):
        if not self._registered:
            raise BoardReadError('reviewer_registration_required')
        if name not in ('get_ticket', 'get_thread', 'get_message'):
            raise BoardReadError('reviewer_tool_not_allowed')
        values = _arguments(name, args)
        self._initialize()
        return self._tool(name, {**values, 'session_id': self.session_id})
