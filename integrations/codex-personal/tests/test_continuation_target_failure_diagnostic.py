"""Closed transient error telemetry; no actual model, RPC or native execution."""
import ast
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

import pytest

from blackoutburst_agents import continuation_pool_service as module
import test_continuation_pool_service as legacy
import test_continuation_pool_service_integration as integration

case=legacy.case
service_case=legacy.service_case
actual_service=integration.actual_service
linux_lock_observation=integration.linux_lock_observation


def observation(stage='next_preparation'):
    value=module._TargetObservation(str(uuid.uuid4()))
    value.stage=stage;value.generation_id=str(uuid.uuid4());value.operation_id=str(uuid.uuid4())
    return value


@pytest.mark.parametrize('cls,category',module._CODE_ERROR_TYPES)
def test_known_sdk_types_only_export_literal_public_codes(cls,category):
    value=observation();result=module._target_failure(cls('private_file_exists'),value)
    assert result['category']==category and result['code']=='private_file_exists'
    assert result['stage']=='next_preparation' and result['operation_id']==value.operation_id
    private='private_file_exists_PRIVATE_SUFFIX/model-output/secret'
    result=module._target_failure(cls(private),value)
    assert result['code']=='unrecognized_code' and 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('error,code',[(PermissionError('PRIVATE/PATH'),'permission_denied'),
    (FileNotFoundError('PRIVATE/PATH'),'file_not_found'),
    (sqlite3.OperationalError('SELECT PRIVATE_QUERY'),'sqlite_error'),
    (KeyError('PRIVATE_KEY'),'missing_key'),(TypeError('PRIVATE_TYPE'),'invalid_type')])
def test_standard_error_details_are_never_read_or_exported(error,code):
    result=module._target_failure(error,observation())
    assert result['code']==code and 'PRIVATE' not in json.dumps(result)


class HostileType(type):
    def __eq__(self,other):raise AssertionError('Unknown class equality was inspected')
    def __hash__(self):raise AssertionError('Unknown class hash was inspected')


class UnknownError99(Exception,metaclass=HostileType):
    def __str__(self):raise AssertionError('Exception text was inspected')
    def __repr__(self):raise AssertionError('Exception repr was inspected')
    def __getattribute__(self,key):
        if key in ('code','args','__dict__'):raise AssertionError('Unknown exception data was inspected')
        return super().__getattribute__(key)


def test_unknown_type99_is_generic_despite_a_forged_public_code():
    error=UnknownError99('PRIVATE_ARGS')
    object.__setattr__(error,'code','continuation_pool_binding_changed')
    result=module._target_failure(error,observation())
    assert result['category']=='unknown_error' and result['code']=='unrecognized_code'
    assert 'PRIVATE' not in json.dumps(result)


def test_unknown_subclass_is_not_a_known_sdk_type():
    class NewDevelopmentError(module.DevelopmentError):
        def __str__(self):raise AssertionError('Do not stringify')
    result=module._target_failure(NewDevelopmentError('private_file_exists'),observation())
    assert result['category']=='unknown_error' and result['code']=='unrecognized_code'


def test_code_and_identity_objects_are_never_coerced_or_hashed():
    class Poison(str):
        def __str__(self):raise AssertionError('No coercion')
        def __hash__(self):raise AssertionError('No subclass hashing')
    value=observation();value.stage=value.operation_id=Poison('PRIVATE_PATH')
    result=module._target_failure(module.DevelopmentError(Poison('private_file_exists')),value)
    assert result['stage']=='unknown' and result['operation_id'] is None
    assert result['code']=='unrecognized_code' and 'PRIVATE' not in json.dumps(result)


def test_projection_is_pure_and_malformed_correlation_is_not_exported(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Telemetry cannot perform IO')
    value=observation();value.logical_task_id='/PRIVATE/root';value.generation_id={'PRIVATE':True}
    with monkeypatch.context() as patch:
        patch.setattr(module.os,'open',forbidden)
        patch.setattr(Path,'read_bytes',forbidden)
        result=module._target_failure(RuntimeError('PRIVATE_EXCEPTION'),value)
    assert result['logical_task_id'] is result['generation_id'] is None
    assert 'PRIVATE' not in json.dumps(result)


def test_already_closed_backend_qualification_reaches_public_failure_without_io(monkeypatch):
    from blackoutburst_agents.linux_execution_qualification import _case_observation
    diagnostic=_case_observation('controller_loss')
    diagnostic['stage']='fault'
    diagnostic['scope_check']={'stage':'scope','allowed':True,'bucket':None,'code':None,'elapsed_ms':201}
    error=module.DevelopmentError('linux_qualification_unconfirmed')
    error._linux_backend_qualification_diagnostic=diagnostic
    def forbidden(*args,**kwargs):raise AssertionError('Failure projection cannot perform IO')
    with monkeypatch.context() as patch:
        patch.setattr(module.os,'open',forbidden)
        patch.setattr(Path,'read_bytes',forbidden)
        result=module._target_failure(error,observation())
    assert result['qualification']==diagnostic
    assert result['qualification'] is not diagnostic
    assert result['qualification']['scope_check'] is not diagnostic['scope_check']


@pytest.mark.parametrize('mutation',[
    lambda d:d.update(private_path='/PRIVATE/owner/auth'),
    lambda d:d.update(case='PRIVATE_CONTROLLER'),
    lambda d:d.update(controller={'operation':'heartbeat_window','bucket':'development','code':'PRIVATE_CODE'}),
    lambda d:d.update(scope_check={'stage':'scope','allowed':True,'bucket':None,'code':None,'elapsed_ms':'PRIVATE_MS'}),
])
def test_malformed_qualification_is_not_exported(mutation):
    from blackoutburst_agents.linux_execution_qualification import _case_observation
    diagnostic=_case_observation('controller_loss');mutation(diagnostic)
    error=module.DevelopmentError('linux_qualification_unconfirmed')
    error._linux_backend_qualification_diagnostic=diagnostic
    result=module._target_failure(error,observation())
    assert 'qualification' not in result and 'PRIVATE' not in json.dumps(result)


def test_qualification_attached_to_an_unknown_exception_is_not_inspected():
    error=UnknownError99('PRIVATE_ARGS')
    object.__setattr__(error,'_linux_backend_qualification_diagnostic',{'PRIVATE':True})
    result=module._target_failure(error,observation())
    assert 'qualification' not in result and 'PRIVATE' not in json.dumps(result)


def test_nested_and_two_thread_contexts_never_share_correlation():
    outer=observation('cursor_read');token=module._TARGET_OBSERVATION.set(outer)
    barrier=threading.Barrier(2);results={};errors=[]
    def run(name,stage):
        try:
            assert module._TARGET_OBSERVATION.get() is None
            own=observation();inner=module._TARGET_OBSERVATION.set(own)
            try:
                module._observe_stage(stage);module._observe_identity(own.generation_id,own.operation_id)
                barrier.wait(timeout=2)
                results[name]=module._target_failure(ValueError('PRIVATE'),own)
            finally:module._TARGET_OBSERVATION.reset(inner)
            assert module._TARGET_OBSERVATION.get() is None
        except BaseException:errors.append(True)
    try:
        threads=[threading.Thread(target=run,args=('a','worker_open')),
                 threading.Thread(target=run,args=('b','cycle_run'))]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3);assert not thread.is_alive()
        assert not errors and results['a']['stage']=='worker_open' and results['b']['stage']=='cycle_run'
        assert results['a']['operation_id']!=results['b']['operation_id']
        assert module._TARGET_OBSERVATION.get() is outer and outer.stage=='cursor_read'
    finally:module._TARGET_OBSERVATION.reset(token)
    assert module._TARGET_OBSERVATION.get() is None


@pytest.mark.parametrize('error_factory,category,code',[
    (lambda:module.DevelopmentError('private_file_exists'),'development_error','private_file_exists'),
    (lambda:UnknownError99('PRIVATE'),'unknown_error','unrecognized_code')])
def test_actual_advance_adds_failure_without_changing_reason_or_status(service_case,monkeypatch,error_factory,category,code):
    construct,(root,path,configuration,*_)=service_case;legacy.available_apis(monkeypatch)
    expected=configuration['tasks'][0]['logical_task_id'];seen=[]
    def target(self,*args):
        module._observe_stage('previous_generation_open');seen.append(module._TARGET_OBSERVATION.get())
        raise error_factory()
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    result=construct().run_once();row=result['tasks'][0]
    assert result['status']=='needs_you' and row['reason']=='continuation_pool_target_failed'
    assert row['operation_id'] is row['state_sha256'] is None
    failure=row['failure'];assert failure['stage']=='previous_generation_open'
    assert failure['category']==category and failure['code']==code
    assert failure['logical_task_id']==expected and failure['generation_id'] is failure['operation_id'] is None
    assert 'PRIVATE' not in json.dumps(result) and module._TARGET_OBSERVATION.get() is None
    assert len(seen)==1


def test_actual_advance_records_only_known_new_operation_and_preserves_known_reason(service_case,monkeypatch):
    construct,_=service_case;legacy.available_apis(monkeypatch)
    generation,operation=str(uuid.uuid4()),str(uuid.uuid4())
    def target(*args):
        module._observe_identity(generation,operation);module._observe_stage('cycle_run')
        raise module.DevelopmentError('continuation_pool_stopped')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    service=construct();result=service.run_once();row=result['tasks'][0]
    assert row['reason']=='continuation_pool_stopped' and service._retained_lease is None
    assert row['failure']['generation_id']==generation and row['failure']['operation_id']==operation


def test_success_and_pending_results_gain_no_failure_field(service_case,monkeypatch):
    construct,_=service_case;legacy.available_apis(monkeypatch)
    def target(self,root,target,job,control):return self._projection(target,reason='continuation_pool_pending')
    monkeypatch.setattr(module.ContinuationPoolService,'_target',target)
    result=construct().run_once();assert 'failure' not in result['tasks'][0]


def test_real_checkpoint_then_preparation_refusal_is_visible_and_never_replayed(actual_service,monkeypatch):
    from blackoutburst_agents import development_cycle_driver
    f,root,task,ticket,construct,calls,sequence=actual_service
    first=construct().run_once();assert first['tasks'][0]['phase']=='paused_cycle'
    old_calls=calls.read_bytes()
    def refused(*args,**kwargs):raise module.PlannerPolicyError('linux_planner_policy_changed')
    monkeypatch.setattr(development_cycle_driver,'_validate_next_inputs',refused)
    second=construct().run_once();row=second['tasks'][0];failure=row['failure']
    assert second['status']=='needs_you' and row['reason']=='continuation_pool_target_failed'
    assert failure['stage']=='next_preparation' and failure['category']=='planner_policy_error'
    assert failure['code']=='linux_planner_policy_changed' and failure['logical_task_id']==task
    assert module._failure_uuid(failure['generation_id']) is not None
    assert module._failure_uuid(failure['operation_id']) is not None
    assert failure['operation_id']!=first['tasks'][0]['operation_id']
    assert calls.read_bytes()==old_calls
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert current['status']=='todo' and current['claimant'] is None
    third=construct().run_once()
    assert third['tasks'][0]['reason']=='continuation_pool_pending'
    assert 'failure' not in third['tasks'][0] and calls.read_bytes()==old_calls
    assert f.board.get_runtime(f.human)['active_count']==0


def test_frozen_catalogue_is_exactly_public_literal_error_producers():
    root=Path(module.__file__).parent;codes=set()
    classes={'DevelopmentError','ContinuationError','PlannerPolicyError','BoardReadError','JournalError',
        'StoreError','ContextError','RuntimeControlError','RuntimeAdmissionRefused'}
    helpers={'fail','_fail','require','_require'}
    for path in root.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_bytes(),feature_version=(3,10))):
            values=[]
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id in classes|helpers:
                index=1 if node.func.id in ('require','_require') else 0
                if len(node.args)>index:values.append(node.args[index])
                values.extend(k.value for k in node.keywords if k.arg=='code')
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in helpers:
                arguments=node.args.posonlyargs+node.args.args
                values.extend(value for arg,value in zip(arguments[-len(node.args.defaults):],node.args.defaults) if arg.arg=='code')
            codes.update(value.value for value in values if isinstance(value,ast.Constant)
                and type(value.value) is str and re.fullmatch('[a-z][a-z0-9_]{0,127}',value.value))
    assert module.TARGET_FAILURE_CODES==codes|module.FAILURE_NOTICE_CODES
    assert 'private_file_exists' in codes and 'PRIVATE' not in codes
    assert 'operations_budget_exhausted' in module.TARGET_FAILURE_CODES


def test_production_stage_sites_are_static_closed_literals():
    tree=ast.parse(Path(module.__file__).read_bytes(),feature_version=(3,10))
    sites=[node for node in ast.walk(tree) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='_observe_stage']
    assert sites
    for node in sites:
        assert len(node.args)==1 and isinstance(node.args[0],ast.Constant)
        assert node.args[0].value in module.TARGET_FAILURE_STAGES
