"""Real temporary Board/Runtime/effect journals, simulated inference only."""
import pytest
import test_host_supervisor as host_fixture
from test_managed_host import catalog,command
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_config import HostError


@pytest.fixture
def case():
    value=host_fixture.HostSupervisorTests('test_disabled_is_offline_and_never_opens_child')
    value.setUp()
    try:yield value
    finally:value.doCleanups()


def prepare(case):
    case.host.prepare_managed('host-identity',catalog(case.host))
    case.host.bind_managed_instances({'primary':'instance-one'})
    c=command(budget=1)
    case.host.queue_managed_command(c);case.host.apply_managed_command(c['id'])


def test_real_host_board_pipeline_uses_managed_target_budget_and_no_replay(case):
    with case.host.owner_lock(lock_directory=case.root/'locks'):
        prepare(case)
        result=case.supervisor.tick()
        assert result['status']=='complete',result
        assert case.calls==1 and case.control.calls.count('reserve_execution')==1
        assert case.host.managed_target('primary')['remaining']==0
        case.host.settle_managed_budgets()
        case.host.queue_managed_command(command(budget=1));case.host.apply_managed_command('start-1')
        assert case.supervisor.tick()['status']=='disabled'
        assert case.calls==1 and not case.host.target_selected('primary')


def test_target_stop_reaches_existing_runner_cancellation_and_preserves_uncertainty(case):
    with case.host.owner_lock(lock_directory=case.root/'locks'):
        prepare(case)
        def stop(callbacks):
            with HostJournal(case.host.directory) as control:
                control.queue_managed_command(command(action='stop',identity='stop-current'))
            assert callbacks['should_cancel']() is True
        case.during=stop;case.result_status='cancelled'
        result=case.supervisor.tick()
        assert result['status']=='needs_review'
        assert case.calls==1 and case.host.dispatches()[0]['state']=='needs_review'
        restart=command(action='restart',identity='restart-later',gen=2,budget=1)
        case.host.queue_managed_command(restart)
        with pytest.raises(HostError,match='host_work_pending'):
            case.host.apply_managed_command(restart['id'])
        assert not case.host.control()['enabled'] and case.calls==1
