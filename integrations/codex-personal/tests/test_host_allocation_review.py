"""Independent regression: durable child identity precedes child creation."""
import uuid
import pytest

from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.host_config import scope_for
from test_host_security import host, event


def test_interrupt_after_parent_reservation_before_child_creation_is_exact(host, monkeypatch):
    host.client.events = [event(1)]
    host.enable()
    original = WorkJournal.begin
    def crash(*args, **kwargs):
        raise SystemExit('synthetic interruption before child creation')
    monkeypatch.setattr(WorkJournal, 'begin', crash)
    with pytest.raises(SystemExit):
        host.tick()
    intent = host.journal.dispatches()[0]
    assert str(uuid.UUID(intent['worker_id'])) == intent['worker_id']
    assert host.journal.status()['remaining_dispatches'] == 1
    scope = scope_for(host.config, host.config['targets'][0])
    with WorkJournal(host.journal.directory/'work') as work:
        assert work.find(scope, intent['request_id']) is None
    assert host.inferences == []
    host.reopen()
    monkeypatch.setattr(WorkJournal, 'begin', original)
    result = host.tick()
    assert result['status'] == 'complete'
    assert host.journal.dispatches()[0]['worker_id'] == intent['worker_id']
    assert len(host.inferences) == 1 and host.client.publications == 1
    host.reopen()
    assert host.tick()['status'] == 'idle'
    assert len(host.inferences) == 1 and host.client.publications == 1
    assert host.journal.status()['remaining_dispatches'] == 1
