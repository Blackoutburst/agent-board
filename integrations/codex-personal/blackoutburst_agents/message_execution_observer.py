"""Read-only Runtime channel for a finite, explicit executed-message disposition.

This channel uses the already registered owner/device/session. It cannot open,
start, renew, finish or seal an execution, nor refresh a profile registration.
"""
from .board_reader import BoardReadError
from .runtime_client import RuntimeBoardClient


class MessageExecutionObservationClient(RuntimeBoardClient):
    def call(self, name, arguments):
        if name not in ('lookup_execution', 'get_execution_control'):
            raise BoardReadError('tool_not_allowed')
        return super().call(name, arguments)
