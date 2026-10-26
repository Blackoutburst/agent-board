"""Closed in-memory observations through real control and qualification paths.

Only synthetic pipes, Board/Git fixtures and process doubles; no native job,
model, authentication lookup, owner state or service operation.
"""
from copy import deepcopy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents import linux_execution_qualification as q
from blackoutburst_agents import linux_test_backend as b
from blackoutburst_agents import linux_test_supervisor as supervisor
from blackoutburst_agents import linux_pool_preparation as preparation
from blackoutburst_agents.board_reader import BoardReadError
import test_linux_execution_qualification as matrix_fixture
from test_linux_pool_preparation import Fixture


def observation():
    return {'case':'eof_running','stage':'native','phase':'complete','controller':{
        'operation':'heartbeat_window','bucket':'development','code':'permission_revoked'},
        'native':{'reason':'linux_authorization_expired','launcher_attempted':True,
            'source_release_intent':False,'source_released':False,'checks':{},'cleanup':{
                'init_identity_received':False,'init_terminal':False,'launcher_reaped':True,
                'cleanup_confirmed':False,'numeric_pid_signalled':False}}}


def test_projection_copies_false_null_and_complete_kernel_checks():
    value=observation();value['native']['checks']={name:False for name in b.CHECKS}
    value['controller']=None
    projected=q.qualification_diagnostic(value)
    assert projected==value and projected['controller'] is None
    value['native']['checks']['inputs_write']='/private/secret'
    value['native']['cleanup']['cleanup_confirmed']='/private/secret'
    assert projected['native']['checks']['inputs_write'] is False
    assert projected['native']['cleanup']['cleanup_confirmed'] is False
    assert '/private/' not in str(projected)


@pytest.mark.parametrize('mutate',[
    lambda p:p.update(path='/private/secret'),lambda p:p.update(case='private case'),
    lambda p:p.update(stage='private stage'),lambda p:p.update(phase='private phase'),
    lambda p:p['controller'].update(code='private error'),lambda p:p['controller'].update(bucket='private bucket'),
    lambda p:p['controller'].update(operation='private operation'),lambda p:p['controller'].update(stderr='private'),
    lambda p:p['native'].update(reason='private reason'),lambda p:p['native'].update(pid=123),
    lambda p:p['native'].update(checks={'inputs_write':True}),lambda p:p['native'].update(checks=[]),
    lambda p:p['native'].update(launcher_attempted=1),lambda p:p['native']['cleanup'].update(cleanup_confirmed=1),
    lambda p:p['native']['cleanup'].update(extra=True),lambda p:p.update(scope_check={
        'stage':'fresh','allowed':False,'bucket':'development','code':'private error','elapsed_ms':1}),
    lambda p:p.update(scope_check={'stage':'fresh','allowed':False,'bucket':None,'code':None,'elapsed_ms':True}),
    lambda p:p.update(scope_check={'stage':'fresh','allowed':False,'bucket':None,'code':None,'elapsed_ms':600001}),
])
def test_projection_refuses_foreign_or_ambiguous_fields_as_a_whole(mutate):
    value=observation();mutate(value)
    assert q.qualification_diagnostic(value) is None


@pytest.mark.parametrize('mode,operation,bucket,code',[
    ('denied','heartbeat_window','development','permission_revoked'),
    ('slow','heartbeat_window','development','supervisor_authorization_expired'),
    ('send','initial_send','development','supervisor_control_write_failed'),
])
def test_real_run_job_early_eof_case_refusal_closes_pipe_without_inventing_source(
        tmp_path,monkeypatch,mode,operation,bucket,code):
    root=tmp_path/'backend';root.mkdir(mode=0o700)
    request={'timeout_seconds':5,'max_output_bytes':4096}
    calls=[];pipes=[];finished=False;clock=[100.]
    native=observation()['native']
    producer=SimpleNamespace(private=SimpleNamespace(path=root),manifest={'scope':{},'resource':'fixture'},
        document={'plan_hash':'a'*64},plan={},_pins=Mock(),_job_directory=lambda _:root/'execution',
        _read_native=lambda _:native)
    def authorize():
        calls.append(True)
        if len(calls)==3:
            if mode=='slow':clock[0]+=2
            else:raise DevelopmentError('permission_revoked')
        return True
    producer._authorize=authorize
    context=b._QualificationContext(producer,'eof_running')
    def spawn(*_args,**_kwargs):
        read,write=os.pipe();pipes.append(read)
        stdin=os.fdopen(write,'wb',buffering=0)
        with PrivateDirectory(root/'execution') as private:
            state=decode(private.read('state.json',8192));state['phase']='namespace_launch_intent'
            private.replace('state.json',canonical(state))
        process=SimpleNamespace(stdin=stdin,wait=Mock(),poll=lambda:0 if finished else None)
        return process
    def advance(_seconds):
        nonlocal finished
        with PrivateDirectory(root/'execution') as private:
            state=decode(private.read('state.json',8192));state.update(phase='complete',native_receipt_sha256='b'*64)
            private.replace('state.json',canonical(state))
        finished=True
    monkeypatch.setattr(supervisor,'helper_command',lambda *_:['synthetic-never-executed'])
    monkeypatch.setattr(b.subprocess,'Popen',spawn)
    monkeypatch.setattr(b.time,'sleep',advance)
    from blackoutburst_agents import dev_test_supervisor as control
    assert control.CONTROL_TTL==1.5
    monkeypatch.setattr(control,'_control_clock',lambda:clock[0])
    monkeypatch.setattr(b,'_control_clock',lambda:clock[0])
    if mode=='send':monkeypatch.setattr(b,'_send_control_window',Mock(
        side_effect=DevelopmentError('supervisor_control_write_failed')))
    try:
        returned=b.SupervisedLinuxNamedPythonBackend._run_job(producer,request,b'inputs',b'pass',
            context,qualification=True,qualification_case='eof_running')
        assert returned is native
        assert context._diagnostic['controller']=={'operation':operation,'bucket':bucket,'code':code}
        assert context._diagnostic['phase']==('helper_launch_intent' if mode=='send' else 'namespace_launch_intent')
        assert context.stage=='complete' and context.fault_observed is False
        if mode!='send':assert os.read(pipes[0],4096)  # Original valid initial frame.
        assert os.read(pipes[0],1)==b''  # The same real control pipe is closed.
        with pytest.raises(DevelopmentError,match='^linux_qualification_source_unconfirmed$'):
            q._expected_native('eof_running',native)
    finally:
        for fd in pipes:os.close(fd)


def test_real_native_cleanup_refusal_adds_only_already_read_summary(tmp_path,monkeypatch):
    summary=observation()['native'];plan={'synthetic':True}
    job={'nonce':'a'*32,'request':{'synthetic':True},'max_output_bytes':4096}
    native={**summary,'schema':b.NATIVE_SCHEMA,'nonce':job['nonce'],
        'request_sha256':digest(canonical(job['request'])),'plan_sha256':digest(canonical(plan)),
        'exit_code':None,'output_bytes':0,'output_sha256':digest(b''),'identity':None,
        'broker_snapshot_admitted':False}
    assert b.validate_native_receipt(native,job,plan)==native
    tmp_path.chmod(0o700)
    with PrivateDirectory(tmp_path) as private:private.create('native-receipt.json',canonical(native))
    monkeypatch.setattr(q,'_job',lambda *_:(PrivateDirectory(tmp_path),job,
        {'phase':'complete','native_receipt_sha256':digest(canonical(native))}))
    monkeypatch.setattr(b,'read_native_observation',lambda _private,j,p:b.validate_native_receipt(native,j,p))
    producer=SimpleNamespace(plan=plan,_qualification_observation=q._case_observation('eof_running'))
    with pytest.raises(DevelopmentError,match='^linux_qualification_cleanup_unconfirmed$'):
        q._native(producer,'eof_running',job['request'])
    projected=q.qualification_diagnostic(producer._qualification_observation)
    assert projected['stage']=='native' and projected['phase']=='complete'
    assert projected['native']==summary
    assert 'exit_code' not in projected['native'] and 'identity' not in projected['native']


def test_failed_matrix_attaches_case_and_does_not_extend_persisted_proof_schema(monkeypatch):
    case=matrix_fixture.MatrixTests('runTest');case.setUp()
    try:
        original=case.proof
        def failed(backend,name):
            if name=='eof_running':
                backend._qualification_observation=observation()
                raise DevelopmentError('linux_qualification_cleanup_unconfirmed')
            return original(backend,name)
        monkeypatch.setattr(q,'_ordinary',failed)
        with pytest.raises(DevelopmentError) as stopped:case.qualify()
        assert stopped.value.code=='linux_qualification_cleanup_unconfirmed'
        assert stopped.value._linux_backend_qualification_diagnostic==observation()
        matrix=case.path/'qualification-matrix'
        assert (matrix/'eof_running-intent.json').exists() and not (matrix/'eof_running.json').exists()
        proof=decode((matrix/'normal.json').read_bytes())
        assert 'controller' not in proof and 'native' not in proof and 'scope_check' not in proof
    finally:case.doCleanups()


@pytest.mark.parametrize('error,expected,bucket',[
    (DevelopmentError('linux_pool_selection_changed'),'linux_pool_selection_changed','development'),
    (RuntimeError('private /secret'),None,'other'),
    (BoardReadError('private /secret'),None,'other'),
    (OSError('private /secret'),None,'os_error'),
])
def test_actual_preparer_callback_preserves_only_its_own_allowlisted_failure(tmp_path,monkeypatch,error,expected,bucket):
    fixture=Fixture(tmp_path,monkeypatch)
    try:
        job,ticket,args=fixture.selected()
        def qualify(backend):
            monkeypatch.setattr(fixture.preparer,'_fresh',Mock(side_effect=error))
            assert backend.authorize(backend.manifest['scope'],'repo.run_test',backend.manifest['resource']) is False
            refused=DevelopmentError('linux_qualification_cleanup_unconfirmed')
            refused._linux_backend_qualification_diagnostic=observation()
            raise refused
        monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',qualify)
        with pytest.raises(DevelopmentError) as stopped:fixture.prepare(job,args)
        value=stopped.value._linux_backend_qualification_diagnostic
        assert value['scope_check']['stage']=='fresh' and value['scope_check']['code']==expected
        assert value['scope_check']['allowed'] is False and value['scope_check']['bucket']==bucket
        assert 0<=value['scope_check']['elapsed_ms']<=600000
        assert '/secret' not in str(value)
        failed=decode((Path(fixture.preparer.directory)/job['id']/'failed.json').read_bytes())
        assert set(failed)=={'request_sha256','stage','reason'}
    finally:fixture.pool.close()


def test_successful_slow_scope_collection_remains_visible_when_window_expires(tmp_path,monkeypatch):
    from blackoutburst_agents.dev_test_supervisor import _fresh_control_window,CONTROL_TTL
    fixture=Fixture(tmp_path,monkeypatch)
    try:
        job,_,args=fixture.selected()
        def qualify(backend):
            original=fixture.preparer._fresh;clock=[100.];ticks=iter((0,2000000000))
            def slow(*args,**kwargs):
                value=original(*args,**kwargs);clock[0]+=2;return value
            monkeypatch.setattr(fixture.preparer,'_fresh',slow)
            monkeypatch.setattr(preparation.time,'monotonic_ns',lambda:next(ticks))
            try:_fresh_control_window(lambda:backend.authorize(backend.manifest['scope'],
                'repo.run_test',backend.manifest['resource']),nonce='a'*32,sequence=1,clock=lambda:clock[0])
            except DevelopmentError as error:
                error._linux_backend_qualification_diagnostic={**observation(),'native':None}
                raise
        monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',qualify)
        with pytest.raises(DevelopmentError,match='^supervisor_authorization_expired$') as stopped:
            fixture.prepare(job,args)
        assert CONTROL_TTL==1.5
        assert stopped.value._linux_backend_qualification_diagnostic['scope_check']=={
            'stage':'scope','allowed':True,'bucket':None,'code':None,'elapsed_ms':2000}
    finally:fixture.pool.close()


def test_scope_observer_failure_cannot_change_authorization_or_original_error(tmp_path,monkeypatch):
    fixture=Fixture(tmp_path,monkeypatch)
    try:
        job,_,args=fixture.selected()
        original=DevelopmentError('linux_qualification_cleanup_unconfirmed')
        def qualify(backend):
            monkeypatch.setattr(preparation,'_scope_check',Mock(side_effect=RuntimeError('PRIVATE')))
            assert backend.authorize(backend.manifest['scope'],'repo.run_test',backend.manifest['resource']) is True
            raise original
        monkeypatch.setattr(preparation.SupervisedLinuxNamedPythonBackend,'qualify',qualify)
        with pytest.raises(DevelopmentError) as stopped:fixture.prepare(job,args)
        assert stopped.value is original
    finally:fixture.pool.close()


def test_matrix_observer_failure_does_not_replace_qualification_error(monkeypatch):
    case=matrix_fixture.MatrixTests('runTest');case.setUp()
    try:
        original=DevelopmentError('linux_qualification_cleanup_unconfirmed')
        monkeypatch.setattr(q,'_ordinary',Mock(side_effect=original))
        monkeypatch.setattr(q,'qualification_diagnostic',Mock(side_effect=RuntimeError('PRIVATE')))
        with pytest.raises(DevelopmentError) as stopped:case.qualify()
        assert stopped.value is original
    finally:case.doCleanups()
