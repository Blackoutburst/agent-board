"""Actual entry exception boundary; no real unit, runtime, credentials or RPC."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from deploy.worker_pool import entry
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.planner_policy import PlannerPolicyError
from blackoutburst_agents import finite_pool_batch


CHECKS = {'inside_read':True,'inside_write_denied':True,'outside_read_denied':True,
          'outside_write_denied':False,'network_loopback_denied':True}
DIAGNOSTIC = {'checks':CHECKS,'cleanup_confirmed':True,'phase':'canary_result'}


@pytest.fixture
def run_entry(tmp_path,monkeypatch,capsys):
    release = tmp_path/'public-release';root = tmp_path/'owner'
    manifest = {'unix_owner':'bb-agent-synthetic','board_owner':'synthetic-owner',
        'board_endpoint':'https://board.invalid/mcp','python':{'path':'/synthetic/python'},
        'codex_version':'0.163.0','unit_limits':{'lifetime_seconds':3600}}
    monkeypatch.setattr(entry.p,'verify_release',lambda *_:manifest)
    monkeypatch.setattr(entry,'owner_context',lambda *_:SimpleNamespace())
    monkeypatch.setattr(entry.p,'owner_root',lambda *_:root)
    monkeypatch.setattr(entry,'in_installed_unit',lambda *_:None)
    monkeypatch.setattr(entry.sys,'path',list(entry.sys.path))
    def run(error):
        class FailingBatch:
            def __init__(self,*_args,**_kwargs): pass
            def run(self,*_args,**_kwargs): raise error
        monkeypatch.setattr(finite_pool_batch,'FiniteLinuxPoolBatch',FailingBatch)
        code = entry.main(['--release',str(release),'--owner',manifest['unix_owner'],
                          'run-batch','--batch',str(root/'config/batch.json')])
        captured = capsys.readouterr()
        assert code == 3 and captured.err == ''
        return [json.loads(line) for line in captured.out.splitlines()]
    return run


def error_with(payload=DIAGNOSTIC,code='linux_planner_native_policy_unqualified',kind=PlannerPolicyError):
    error = kind(code)
    error._linux_planner_canary_diagnostic = deepcopy(payload)
    return error


def test_entry_preserves_original_line_and_emits_only_closed_facts(run_entry):
    lines = run_entry(error_with())
    assert lines == [{'status':'needs_review','reason':'linux_planner_native_policy_unqualified'},
        {'status':'qualification_diagnostic','kind':'linux_planner_canary',**DIAGNOSTIC}]


def test_entry_accepts_empty_checks_and_bounded_host_errno(run_entry):
    facts = {'checks':{},'cleanup_confirmed':True,'phase':'host_fixtures','errno':99}
    assert run_entry(error_with(facts))[1] == {'status':'qualification_diagnostic',
        'kind':'linux_planner_canary',**facts}


def test_projection_is_a_snapshot_not_an_alias_to_exception_payload():
    error = error_with()
    projected = entry._planner_qualification_diagnostic(error)
    error._linux_planner_canary_diagnostic['checks']['inside_read'] = '/private/secret'
    error._linux_planner_canary_diagnostic['phase'] = '/private/workspace'
    assert projected == {'status':'qualification_diagnostic','kind':'linux_planner_canary',**DIAGNOSTIC}


def test_real_qualification_refusal_reaches_actual_entry_unchanged(run_entry):
    from blackoutburst_agents.dev_manifest import canonical
    from blackoutburst_agents.linux_planner_policy import LinuxPlannerPolicy
    import test_linux_planner_policy as fixture_module
    case = fixture_module.LinuxPolicyTests('runTest')
    case.setUp()
    try:
        case.prepare()
        def refused(argv,*args):
            result = case.fake_run(argv,*args)
            if 'sandbox' in argv:
                result.stdout = canonical({'schema':1,'checks':CHECKS})
                result.returncode = 78
            return result
        case.run.side_effect = refused
        with LinuxPlannerPolicy(case.state) as policy:
            with pytest.raises(PlannerPolicyError) as stopped:
                policy.qualify()
        lines = run_entry(stopped.value)
        assert lines == [{'status':'needs_review','reason':stopped.value.code},
            {'status':'qualification_diagnostic','kind':'linux_planner_canary',**DIAGNOSTIC}]
    finally:
        case.doCleanups()


@pytest.mark.parametrize('change',[
    lambda p:p.update(path='/private/secret'),
    lambda p:p.update(stdout='private output'),
    lambda p:p.update(fingerprint='a'*64),
    lambda p:p.update(checks={'inside_read':True}),
    lambda p:p.update(checks={**CHECKS,'inside_read':1}),
    lambda p:p.update(checks=[]),
    lambda p:p.update(cleanup_confirmed=1),
    lambda p:p.update(phase='/private/workspace'),
    lambda p:p.update(phase=1),
    lambda p:p.update(errno=True),
    lambda p:p.update(errno=0),
    lambda p:p.update(errno=-1),
    lambda p:p.update(errno=4096),
    lambda p:p.update(errno=None),
    lambda p:p.update(checks=type('CustomChecks',(dict,),{})(CHECKS)),
])
def test_malicious_or_ambiguous_projection_is_not_partially_exported(run_entry,change):
    payload = deepcopy(DIAGNOSTIC);change(payload)
    lines = run_entry(error_with(payload))
    assert lines == [{'status':'needs_review','reason':'linux_planner_native_policy_unqualified'}]


@pytest.mark.parametrize('error',[
    PlannerPolicyError('linux_planner_native_policy_unqualified'),
    error_with(code='linux_planner_attempt_not_reusable'),
    error_with(kind=DevelopmentError),
    error_with(kind=type('CustomPlannerError',(PlannerPolicyError,),{})),
])
def test_absent_wrong_type_or_unrelated_error_emits_no_diagnostic(run_entry,error):
    assert len(run_entry(error)) == 1
