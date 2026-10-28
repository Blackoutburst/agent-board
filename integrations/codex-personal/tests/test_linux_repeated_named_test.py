"""Two named tests through real planner/broker journals; fake model/native only."""
from copy import deepcopy

import pytest

from blackoutburst_agents import linux_test_backend as backend_module
from blackoutburst_agents.dev_manifest import Budgets,digest
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
import test_linux_connected_execution as fixture


@pytest.fixture
def repeated_case(monkeypatch,request):
    original=backend_module.prepare_manifest
    def prepared(*args,**kwargs):
        kwargs['budgets']=Budgets(max_operations=request.param,max_tests=2,max_read_bytes=256*1024,
            max_file_bytes=8192,max_patch_bytes=64*1024)
        return original(*args,**kwargs)
    monkeypatch.setattr(backend_module,'prepare_manifest',prepared)
    case=fixture.PlannerConnectedExecutionTests();case.setUp()
    case.operation_budget=request.param
    try:yield case
    finally:case.doCleanups()


@pytest.mark.parametrize('repeated_case',[8,10,12,24],indirect=True)
@pytest.mark.parametrize('first_outcome',['failed','cancelled'])
def test_read_patch_failed_test_read_second_test_get_distinct_durable_attempts(repeated_case,first_outcome):
    case=repeated_case
    case.actions=[{'schema':1,'kind':'read','path':'one.txt'},
        {'schema':1,'kind':'patch','path':'one.txt','expected_sha256':digest(b'one\n'),'content':'two\n'},
        {'schema':1,'kind':'test','name':'unit'},
        {'schema':1,'kind':'read','path':'one.txt'},
        {'schema':1,'kind':'test','name':'unit'},
        {'schema':1,'kind':'final','summary':'Verified second attempt.'}]
    calls=[]
    def native(attempt):
        calls.append(deepcopy(attempt.request));attempt.before_source()
        value=case.fixture.native(attempt.request)
        if len(calls)==1:value.update(reason='script_failed' if first_outcome=='failed' else 'linux_authorization_expired',
            exit_code=1 if first_outcome=='failed' else None)
        return value
    case.fixture.backend.run.side_effect=native
    planner=DevelopmentPlanner(broker=case.broker,store=case.store,runtime_journal=case.runtime,
        control_client=case.control,board_scope=case.scope,planner_policy=case.policy,
        authorization=lambda:deepcopy(case.grant),claim_id='claim',instruction='Run both named attempts.',
        model='fixture-model',budgets=PlannerBudgets(max_turns=8,max_actions=8,max_wall_seconds=600,turn_timeout_seconds=240),
        enabled=True,_test_anchor=case.fixture.base/'planner-anchor',_transport=case.transport,
        _allow_fixture_backend=True,_clock=lambda:100.0)
    case.addCleanup(planner.close)
    result=planner.run()
    state=planner.journal.read();broker_state=case.fixture.state()
    if case.operation_budget==8:
        assert result['status']=='needs_review' and result['reason']=='operations_budget_exhausted',result
        assert [item['kind'] for item in state['history']]==['read','patch','test','read']
        assert result['used']['actions']==result['used']['turns']==5
        assert state['step']['action']['kind']=='test' and state['step']['model_turn_id'] is not None
        assert state['step']['tool_finished'] is False and not state['tests']
        assert broker_state['used']['operations']==8
        # The second native test can already be admitted, while the planner's
        # subsequent diff still fails its operation charge. Cleanup inspection
        # may finish this tool as blocked; it cannot promote a final result.
        assert broker_state['operations'][calls[-1]['operation_id']]['receipt']['result']=='passed'
        from blackoutburst_agents.development_cleanup import _effect_receipt
        assert _effect_receipt(state['step'],case.broker.manifest,broker_state,
            configuration={'execution_profile':'linux-python-execution-v1',
                'linux_execution_plan_sha256':case.fixture.plan_hash},workflow_id=planner.operation_id)=='blocked'
        assert planner.run()==result and len(calls)==2  # No implicit replay/reset.
    else:
        assert result['status']=='review_ready',result
        assert broker_state['used']['operations']==10 and len(state['tests'])==1
        assert [item['kind'] for item in state['history']]==['read','patch','test','read','test']
    assert len(calls)==2 and calls[0]['operation_id']!=calls[1]['operation_id']
    assert broker_state['used']['tests']==2
    assert broker_state['used']['read_bytes']<=256*1024
    assert result['used']['actions']<=8 and result['used']['turns']<=8
    if case.operation_budget>8:assert all(value['cleanup_confirmed'] for value in case.control.rows.values())
