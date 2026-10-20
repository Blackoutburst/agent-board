"""Real Host4/children and catalogue intent transactions, without remote effects."""
from unittest.mock import patch

import pytest

from blackoutburst_agents.catalog_source_transition import CatalogSourceTransition, EXECUTED_SCHEMA
from blackoutburst_agents.host_config import HostError
from blackoutburst_agents.server_controller import ServerController
import test_message_executed_disposition as executed_fixture


@pytest.fixture
def executed():
    fixture = executed_fixture.ExecutedDispositionTests('test_five_phases_finish_without_replay_publication_ack_or_runtime_write')
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()


def transition(fixture, *, with_disposition=True):
    return CatalogSourceTransition(fixture.host, None, source_guard=lambda *args: True,
        stopped_guard=lambda: True, executed_disposition=fixture.admin if with_disposition else None)


def plan(coordinator):
    return coordinator.plan(old_release_sha256='b' * 64, new_release_sha256='c' * 64,
                            request_key='after-executed-blocked-message')


def test_executed_closure_consumed_under_real_host_transaction_without_observation(executed):
    fixture = executed
    with fixture.locked():
        closed = fixture.admin.apply(fixture.plan())
        coordinator = transition(fixture)
        proposal = plan(coordinator)
        assert proposal['schema'] == EXECUTED_SCHEMA
        assert proposal['closures'] == []
        assert proposal['executed_closures'][0]['intent_id'] == closed['intent_id']
        before = [tuple(row) for row in fixture.host._db.execute('SELECT * FROM host_message_dispositions')]
        calls = list(fixture.observer.calls)
        original = fixture.admin.status
        checked = []

        def status(**arguments):
            assert fixture.host._db.in_transaction
            checked.append(arguments)
            return original(**arguments)

        with patch.object(fixture.admin, 'status', side_effect=status):
            result = fixture.host.begin_catalog_source_transition(proposal,
                consume_closures=lambda: coordinator._consume(proposal))
        assert checked == [{'intent_id': closed['intent_id']}]
        assert result['state'] == 'pending'
        assert [tuple(row) for row in fixture.host._db.execute('SELECT * FROM host_message_dispositions')] == before
        assert fixture.host._db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert fixture.messages.db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert fixture.observer.calls == calls and fixture.calls == 1


def test_executed_closure_cannot_be_omitted_or_consumed_as_a_legacy_list(executed):
    fixture = executed
    with fixture.locked():
        fixture.admin.apply(fixture.plan())
        with pytest.raises(HostError, match='closure_required'):
            plan(transition(fixture, with_disposition=False))
        coordinator = transition(fixture)
        proposal = plan(coordinator)
        before = '\n'.join(fixture.host._db.iterdump())
        with pytest.raises(HostError, match='closure_invalid'):
            fixture.host.begin_catalog_source_transition(proposal, consume_closures=lambda: [])
        assert '\n'.join(fixture.host._db.iterdump()) == before
        assert fixture.host.pending_managed_effects() == []


def test_changed_child_after_plan_cannot_be_adopted_by_source_intent(executed):
    fixture = executed
    with fixture.locked():
        fixture.admin.apply(fixture.plan())
        coordinator = transition(fixture)
        proposal = plan(coordinator)
        fixture.message_runs._db.execute('UPDATE turns SET requires_review=0')
        before = '\n'.join(fixture.host._db.iterdump())
        with pytest.raises(HostError):
            fixture.host.begin_catalog_source_transition(proposal,
                consume_closures=lambda: coordinator._consume(proposal))
        assert '\n'.join(fixture.host._db.iterdump()) == before
        assert fixture.host.pending_managed_effects() == []


def test_pending_disposition_blocks_start_and_source_but_keeps_stop_available(executed):
    fixture = executed
    fixture.observer.failure = ('before', 'lookup_execution')
    with fixture.locked():
        with pytest.raises(HostError, match='observation_unconfirmed'):
            fixture.admin.apply(fixture.plan())
        with pytest.raises(HostError, match='unsettled'):
            plan(transition(fixture))
        controller = ServerController(fixture.host, None)
        with pytest.raises(HostError, match='unsettled'):
            controller.start_worker()
        assert controller._thread is None
        assert fixture.host.stop()['status'] == 'disabled'
        assert fixture.calls == 1
