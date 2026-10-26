"""Real qualification/refusal paths with existing synthetic kernel/CLI fixtures.

No native process, auth, owner service or RPC. In-memory diagnostics must never
change the persisted refusal schema, acceptance checks or actual host guards.
"""
import errno
from types import SimpleNamespace

import pytest

from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import canonical
from blackoutburst_agents import linux_planner_policy as policy_module
from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy, PlannerPolicyError
import test_linux_planner_policy as policy_fixture


@pytest.fixture
def fixture():
    case = policy_fixture.LinuxPolicyTests('runTest')
    case.setUp()
    try:
        case.prepare()
        yield case
    finally:
        case.doCleanups()


def failed_qualification(case):
    with LinuxPlannerPolicy(case.state) as policy:
        with pytest.raises(PlannerPolicyError) as stopped:
            policy.qualify()
    record = decode((case.state/'planner-qualification.json').read_bytes())
    assert set(record) == {'schema','fingerprint','attempt_sha256','checks','cleanup_confirmed',
                           'reason','environment_sha256','result'}
    assert record['schema'] == 'blackoutburst-linux-planner-refusal-v1'
    assert record['reason'] == stopped.value.code
    diagnostic = stopped.value._linux_planner_canary_diagnostic
    assert diagnostic['checks'] == record['checks']
    assert diagnostic['cleanup_confirmed'] is record['cleanup_confirmed']
    assert 'phase' not in record and 'errno' not in record
    return stopped.value, record


def test_real_false_check_attaches_only_closed_in_memory_facts(fixture):
    case = fixture
    def fail(argv, *args):
        result = case.fake_run(argv, *args)
        if 'sandbox' in argv:
            checks = {name: name != 'outside_write_denied' for name in policy_module.CHECKS}
            result.stdout = canonical({'schema':1,'checks':checks})
            result.returncode = 78
        return result
    case.run.side_effect = fail
    error, record = failed_qualification(case)
    assert error.code == 'linux_planner_native_policy_unqualified'
    assert error._linux_planner_canary_diagnostic == {
        'checks':record['checks'],'cleanup_confirmed':True,'phase':'canary_result'}
    assert case.commands[-1][0][-2] == policy_module.LINUX_CANARY
    assert case.commands[-1][2:4] == (15,8192)


def test_wrong_version_keeps_original_reason_and_does_not_launch_canary(fixture):
    case = fixture
    case.run.side_effect = lambda *_: SimpleNamespace(failure=None,returncode=0,
        stdout=b'private version text is never exported',stderr=b'private path /secret')
    error, _ = failed_qualification(case)
    assert error.code == 'linux_planner_cli_version_changed'
    assert error._linux_planner_canary_diagnostic == {
        'checks':{},'cleanup_confirmed':True,'phase':'cli_version'}
    assert case.run.call_count == 1


def test_host_oserror_exports_only_bounded_number_not_exception_text(fixture,monkeypatch):
    case = fixture
    def unavailable(*_):
        raise OSError(errno.EADDRNOTAVAIL, 'private listener text /secret')
    listener = SimpleNamespace(bind=unavailable,close=lambda:None)
    monkeypatch.setattr(policy_module.socket,'socket',lambda *_:listener)
    error, _ = failed_qualification(case)
    assert error.code == 'linux_planner_native_policy_unqualified'
    assert error._linux_planner_canary_diagnostic == {'checks':{},'cleanup_confirmed':True,
        'phase':'host_fixtures','errno':errno.EADDRNOTAVAIL}
    assert 'private' not in str(error._linux_planner_canary_diagnostic)


def test_host_created_file_guard_and_uncertain_cleanup_still_refuse(fixture):
    case = fixture
    created = []
    def falsify(argv, *args):
        result = case.fake_run(argv, *args)
        if 'sandbox' in argv:
            from pathlib import Path
            payload = decode(argv[-1].encode())
            path = Path(payload['outside_write'])
            path.write_bytes(b'synthetic foreign file retained')
            created.append(path)
        return result
    case.run.side_effect = falsify
    error, record = failed_qualification(case)
    assert error.code == 'linux_planner_qualification_cleanup_unconfirmed'
    assert record['checks']['outside_write_denied'] is False
    assert error._linux_planner_canary_diagnostic == {
        'checks':record['checks'],'cleanup_confirmed':False,'phase':'cleanup'}
    assert created[0].read_bytes() == b'synthetic foreign file retained'


def test_success_and_nonreusable_attempt_do_not_gain_a_diagnostic(fixture):
    case = fixture
    with LinuxPlannerPolicy(case.state) as policy:
        result = policy.qualify()
        assert result['checks'] == {name:True for name in policy_module.CHECKS}
        assert result['cleanup_confirmed'] is True
        with pytest.raises(PlannerPolicyError) as stopped:
            policy.qualify()
    assert stopped.value.code == 'linux_planner_attempt_not_reusable'
    assert not hasattr(stopped.value,'_linux_planner_canary_diagnostic')
