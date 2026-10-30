"""Closed fault refusal diagnostics through real orchestration, without native execution."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from blackoutburst_agents import linux_execution_qualification as q
from blackoutburst_agents import linux_test_namespace as namespace
from blackoutburst_agents import linux_pool_preparation as preparation
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_journal import decode
import test_linux_execution_qualification as matrix
from test_linux_pool_preparation import Fixture


@pytest.fixture
def flow():
    case=matrix.FaultFlowTests('test_controller_loss_requires_positive_kernel_observer_before_ack')
    case.setUp();case.backend._qualification_observation=q._case_observation('controller_loss')
    case.backend._qualification_observation['stage']='fault'
    try:yield case
    finally:case.doCleanups()


def fault(case):
    value=q.qualification_diagnostic(case.backend._qualification_observation)
    assert value is not None
    return value['fault']


def refused(case, failure=None):
    if failure is None:failure=q._fault_detail('driver_refused',DevelopmentError('linux_execution_environment_changed'),driver_stage='backend_open')
    return {'schema':q.MATRIX_SCHEMA,'kind':'driver_refused','case':case.case,'nonce':case.nonce,'failure':failure}


def test_observer_constructor_failure_is_distinct_and_never_acknowledged(flow,monkeypatch):
    error=DevelopmentError('linux_qualification_process_unconfirmed')
    monkeypatch.setattr(q,'_FaultObserver',Mock(side_effect=error))
    with pytest.raises(DevelopmentError) as stopped:q._fault(flow.backend,flow.case)
    assert stopped.value.code=='linux_qualification_observer_init_unconfirmed'
    assert fault(flow)=={'operation':'observer_init','driver_stage':None,'bucket':'development','code':error.code}
    flow.send.assert_not_called();flow.receive.assert_not_called()
    flow.driver.kill.assert_called_once();flow.driver.wait.assert_called_once()


def test_exact_child_refusal_preserves_closed_stage_code_and_requires_no_ack(flow):
    flow.receive.side_effect=None;flow.receive.return_value=refused(flow)
    with pytest.raises(DevelopmentError) as stopped:q._fault(flow.backend,flow.case)
    assert stopped.value.code=='linux_qualification_driver_refused'
    assert fault(flow)==flow.receive.return_value['failure']
    flow.send.assert_not_called();flow.observer.acquire.assert_not_called()
    flow.observer.emergency_cleanup.assert_called_once_with(flow.driver)


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(nonce='e'*32),lambda p:p.update(case='supervisor_loss'),
    lambda p:p.update(schema='PRIVATE'),lambda p:p.update(extra='PRIVATE'),
    lambda p:p['failure'].update(code='PRIVATE'),lambda p:p['failure'].update(driver_stage='PRIVATE'),
    lambda p:p['failure'].update(operation='observer_init'),lambda p:p['failure'].update(bucket='PRIVATE'),
    lambda p:p['failure'].update(bucket='os_error'),lambda p:p['failure'].update(extra='PRIVATE'),
    lambda p:p.update(failure=[]),lambda p:p.update(failure=None),
])
def test_malicious_or_foreign_child_refusal_never_crosses_parent_codec(flow,mutate):
    packet=refused(flow);mutate(packet);flow.receive.side_effect=None;flow.receive.return_value=packet
    with pytest.raises(DevelopmentError) as stopped:q._fault(flow.backend,flow.case)
    assert stopped.value.code=='linux_qualification_fault_message_unconfirmed'
    assert fault(flow)==q._fault_detail('first_message')
    assert 'PRIVATE' not in str(q.qualification_diagnostic(flow.backend._qualification_observation))
    flow.send.assert_not_called();flow.observer.acquire.assert_not_called()


def test_ready_state_change_is_distinct_before_observer_or_ack(flow):
    packet=flow.message();flow.receive.side_effect=None;flow.receive.return_value=packet
    flow.state={'phase':'complete'}
    with pytest.raises(DevelopmentError) as stopped:q._fault(flow.backend,flow.case)
    assert stopped.value.code=='linux_qualification_ready_state_changed'
    assert fault(flow)==q._fault_detail('ready_state')
    assert flow.backend._qualification_observation['phase']=='complete'
    flow.send.assert_not_called();flow.observer.acquire.assert_not_called()


@pytest.mark.parametrize('where', ['_base_proof','_job'])
def test_pre_ready_metadata_failures_retain_their_original_code_and_location(flow,monkeypatch,where):
    monkeypatch.setattr(q,where,Mock(side_effect=DevelopmentError('linux_qualification_origin_changed')))
    with pytest.raises(DevelopmentError) as stopped:q._fault(flow.backend,flow.case)
    assert stopped.value.code=='linux_qualification_origin_changed'
    assert fault(flow)['operation']==('base_proof' if where=='_base_proof' else 'job_state')
    flow.send.assert_not_called();flow.observer.acquire.assert_not_called()


def test_successful_fault_still_requires_original_kernel_and_outcome_guards(flow):
    result=q._fault(flow.backend,flow.case)
    assert result['observation']['driver_outcome']=='self_sigkill'
    flow.observer.acquire.assert_called_once_with(456)
    assert flow.send.call_args.args[1]['kind']=='fault_ack'
    flow.observer.finish.assert_called_once_with(flow.driver)
    assert 'fault' not in result


class PrivateError(ValueError):
    def __str__(self):raise AssertionError('exception text must never be requested')


@pytest.mark.parametrize('error,bucket,code',[
    (DevelopmentError('linux_execution_environment_changed'),'development','linux_execution_environment_changed'),
    (DevelopmentError('/PRIVATE/SECRET'),'development',None),
    (OSError('/PRIVATE/SECRET'),'os_error',None),(PrivateError(),'other',None),
])
def test_real_driver_entry_emits_only_closed_refusal_before_backend_open(monkeypatch,error,bucket,code):
    packet={'case':'controller_loss','backend_directory':'/not-opened','manifest':{},'plan_sha256':'a'*64,
        'environment_sha256':'b'*64,'channel_fd':17,'nonce':'c'*32}
    raw=canonical(packet);channel=SimpleNamespace(close=Mock());send=Mock()
    monkeypatch.setattr(namespace,'read_sealed',Mock(return_value=raw))
    monkeypatch.setattr(q.socket,'socket',Mock(return_value=channel))
    monkeypatch.setattr(q,'_backend_class',lambda:Mock(side_effect=error));monkeypatch.setattr(q,'_send',send)
    assert q._driver_entry(canonical({'fd':18,'sha256':digest(raw)}).decode())==2
    projected=send.call_args.args[1]
    assert projected=={'schema':q.MATRIX_SCHEMA,'kind':'driver_refused','case':'controller_loss',
        'nonce':'c'*32,'failure':{'operation':'driver_refused','driver_stage':'backend_open','bucket':bucket,'code':code}}
    assert 'PRIVATE' not in str(projected);channel.close.assert_called_once()


def test_driver_ack_timeout_is_closed_and_preserves_original_timeout(monkeypatch):
    request={'operation_id':'d'*64};state={'phase':'source_ack_written','request_sha256':'e'*64}
    packet={'case':'controller_loss','backend_directory':'/not-opened','manifest':{},'plan_sha256':'a'*64,
        'environment_sha256':digest(canonical({})),'channel_fd':17,'nonce':'c'*32};raw=canonical(packet)
    backend=SimpleNamespace(document={'plan_hash':'a'*64},environment={},close=Mock())
    def qualify(case,*,fault_authorizer):fault_authorizer(case,SimpleNamespace(pid=22,poll=lambda:None),state,request)
    backend._qualify_case=qualify;send=Mock();channel=SimpleNamespace(close=Mock())
    monkeypatch.setattr(namespace,'read_sealed',Mock(return_value=raw));monkeypatch.setattr(q.socket,'socket',Mock(return_value=channel))
    monkeypatch.setattr(q,'_backend_class',lambda:lambda *a,**kw:backend);monkeypatch.setattr(q,'_real_host',Mock())
    monkeypatch.setattr(q,'_request',lambda *a:'e'*64);monkeypatch.setattr(q,'_send',send)
    monkeypatch.setattr(q,'_receive',Mock(side_effect=DevelopmentError('linux_qualification_protocol_timeout')))
    assert q._driver_entry(canonical({'fd':18,'sha256':digest(raw)}).decode())==2
    value=send.call_args.args[1]['failure']
    assert value['driver_stage']=='ack_wait' and value['code']=='linux_qualification_protocol_timeout'
    assert q.ACK_SECONDS==0.8 and q.CASE_SECONDS==22
    backend.close.assert_called_once()


def test_fault_projection_is_copied_and_legacy_shape_remains_valid():
    old=q._case_observation('controller_loss');assert q.qualification_diagnostic(old)==old
    value={**old,'fault':q._fault_detail('driver_refused',DevelopmentError('linux_execution_environment_changed'),driver_stage='host')}
    result=q.qualification_diagnostic(value);assert result==value
    value['fault']['code']='PRIVATE';assert result['fault']['code']=='linux_execution_environment_changed'
    assert q.qualification_diagnostic(value) is None
    for bad in (True,1,[],None):
        assert q.qualification_diagnostic({**old,'fault':bad}) is None


def test_real_preparer_persists_extended_closed_diagnostic_and_status_without_execution(tmp_path,monkeypatch):
    fixture=Fixture(tmp_path,monkeypatch)
    try:
        job,_,args=fixture.selected()
        info={**q._case_observation('controller_loss'),'stage':'fault',
            'fault':q._fault_detail('driver_refused',DevelopmentError('linux_execution_environment_changed'),driver_stage='backend_open')}
        def fail(backend):
            error=DevelopmentError('linux_qualification_driver_refused');error._linux_backend_qualification_diagnostic=deepcopy(info);raise error
        monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',fail)
        with pytest.raises(DevelopmentError) as stopped:fixture.prepare(job,args)
        failed=decode((Path(fixture.preparer.directory)/job['id']/'failed.json').read_bytes())
        # Existing scope_check may be added by the normal preparer; no new file or codec is needed.
        assert failed['reason']=='linux_qualification_driver_refused'
        assert failed['qualification_diagnostic']['fault']==info['fault']
        assert stopped.value._linux_backend_qualification_diagnostic==failed['qualification_diagnostic']
        assert fixture.preparer.status(job['id'])['failure']==failed
    finally:fixture.pool.close()
