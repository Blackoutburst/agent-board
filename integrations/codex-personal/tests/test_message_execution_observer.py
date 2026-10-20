"""The administrative observer has no execution or registration authority."""
from unittest.mock import patch

import pytest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.message_execution_observer import MessageExecutionObservationClient
from blackoutburst_agents.runtime_client import RuntimeBoardClient


@pytest.mark.parametrize('method,arguments', [
    ('lookup_execution', {'idempotency_key': 'existing-key'}),
    ('get_execution_control', {'execution_id': 'existing-runtime'}),
])
def test_observation_delegates_exact_existing_read_only_channel(method, arguments):
    client = object.__new__(MessageExecutionObservationClient)
    with patch.object(RuntimeBoardClient, 'call', return_value={'read': True}) as call:
        assert client.call(method, arguments) == {'read': True}
    call.assert_called_once_with(method, arguments)


@pytest.mark.parametrize('method', [
    'register_agent', 'reserve_execution', 'reserve_message_execution', 'start_execution',
    'heartbeat_execution', 'finish_execution', 'seal_message_execution_admission',
    'post_message', 'get_thread', 'read_inbox', 'get_message_execution_context',
])
def test_every_mutation_and_normal_work_channel_is_rejected_before_transport(method):
    client = object.__new__(MessageExecutionObservationClient)
    with patch.object(RuntimeBoardClient, 'call') as transport:
        with pytest.raises(BoardReadError, match='tool_not_allowed'):
            client.call(method, {})
    transport.assert_not_called()


def test_registration_cannot_refresh_saved_authorization():
    client = object.__new__(MessageExecutionObservationClient)
    with pytest.raises(BoardReadError, match='runtime_registration_forbidden'):
        client.register()
