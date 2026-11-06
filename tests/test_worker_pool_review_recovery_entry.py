"""Installed entry routes the explicit recovery schema only after its unit gate."""
import json
import uuid
from types import SimpleNamespace

import pytest

from deploy.worker_pool import entry
from blackoutburst_agents import review_recovery_service as module
from blackoutburst_agents.dev_manifest import DevelopmentError
from test_worker_pool_continuation_entry import entry_fixture
from test_review_recovery_service import recovery,case


def test_recovery_plan_is_inert_and_does_not_construct_a_service(recovery,monkeypatch,capsys):
    f=recovery;_,manifest,argv=entry_fixture(monkeypatch,f.root,'0.49.0')
    manifest['board_endpoint']='http://localhost/mcp'
    def forbidden(*_args,**_kwargs):pytest.fail('planning constructed service/unit')
    monkeypatch.setattr(module.ReviewRecoveryService,'__init__',forbidden)
    monkeypatch.setattr(entry,'in_continuation_unit',forbidden)
    assert entry.main(argv('continuation-plan','--continuation',str(f.path)))==0
    result=json.loads(capsys.readouterr().out)
    assert result['schema']==module.SCHEMA and result['status']=='plan'
    assert result['activated'] is result['execution_qualified'] is result['launchable'] is False
    assert not (f.root/'review-recoveries').exists() and 'PRIVATE' not in json.dumps(result)


def test_recovery_requires_exact_unit_before_config_or_service_read(recovery,monkeypatch,capsys):
    f=recovery;_,_,argv=entry_fixture(monkeypatch,f.root,'0.49.0')
    def denied(*_):entry.p.fail('pool_continuation_unit_required')
    monkeypatch.setattr(entry,'in_continuation_unit',denied)
    monkeypatch.setattr(module,'ReviewRecoveryService',lambda *_a,**_k:pytest.fail('unit bypass'))
    assert entry.main(argv('run-continuation-once','--continuation',str(f.path)))==3
    assert json.loads(capsys.readouterr().out)['reason']=='pool_continuation_unit_required'


@pytest.mark.parametrize('action',['continuation-plan','run-continuation-once'])
def test_older_release_cannot_route_review_recovery(recovery,monkeypatch,capsys,action):
    f=recovery;_,_,argv=entry_fixture(monkeypatch,f.root,'0.48.3')
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:None)
    assert entry.main(argv(action,'--continuation',str(f.path)))==3
    assert json.loads(capsys.readouterr().out)['reason']=='pool_review_recovery_version_required'


def test_new_service_receives_only_the_qualified_manifest_tools(recovery,monkeypatch,capsys):
    f=recovery;release,manifest,argv=entry_fixture(monkeypatch,f.root,'0.49.0');calls=[]
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:calls.append('unit'))
    def constructor(root,**kwargs):
        assert calls==['unit'];calls.append('service')
        assert root==f.root and kwargs['configuration_path']==f.path
        assert kwargs['executable']==release/'tools/codex' and kwargs['gh_executable']==release/'tools/gh'
        assert kwargs['interpreter']==manifest['python']['path'] and kwargs['max_seconds']==3510
        return SimpleNamespace(run_once=lambda *,should_stop:dict(status='pass_complete',queue=dict(phase='empty')))
    monkeypatch.setattr(module,'ReviewRecoveryService',constructor)
    assert entry.main(argv('run-continuation-once','--continuation',str(f.path)))==0
    assert calls==['unit','service'] and json.loads(capsys.readouterr().out)['queue']['phase']=='empty'


def test_entry_prints_closed_runtime_failure_without_turning_it_into_success(recovery,monkeypatch,capsys):
    f=recovery;_,manifest,argv=entry_fixture(monkeypatch,f.root,'0.49.1')
    manifest['board_endpoint']='http://localhost/mcp'
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:None)
    result=dict(status='needs_review',reason='review_recovery_cleanup_unconfirmed',activated=False,
        runtime_failure=dict(stage='reserve',code='capacity_exhausted'))
    monkeypatch.setattr(module,'ReviewRecoveryService',lambda *_a,**_k:
        SimpleNamespace(run_once=lambda **_:result))
    assert entry.main(argv('run-continuation-once','--continuation',str(f.path)))==3
    assert json.loads(capsys.readouterr().out)==result


@pytest.mark.parametrize('known',[True,False])
def test_early_service_failure_has_a_closed_code_without_exception_prose(recovery,monkeypatch,capsys,known):
    f=recovery;_,_,argv=entry_fixture(monkeypatch,f.root,'0.49.0')
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:None)
    class PrivateError(Exception):
        def __str__(self):raise AssertionError('private exception stringified')
        def __getattribute__(self,key):
            if key in ('code','args','__dict__'):raise AssertionError('private exception inspected')
            return super().__getattribute__(key)
    def refused(*_args,**_kwargs):
        if known:raise DevelopmentError('historical_review_cleanup_unconfirmed')
        raise PrivateError('PRIVATE_RESPONSE')
    monkeypatch.setattr(module,'ReviewRecoveryService',refused)
    assert entry.main(argv('run-continuation-once','--continuation',str(f.path)))==3
    result=json.loads(capsys.readouterr().out)
    assert result['reason']==('historical_review_cleanup_unconfirmed' if known else 'finite_pool_entry_refused')
    assert 'PRIVATE' not in json.dumps(result)



def select_v2(f):
    value=dict(f.value,schema=module.SCHEMA_V2,prior_recovery_id=str(uuid.uuid4()),
        expected_prior_configuration_sha256='b'*64)
    from test_review_recovery_service import write
    write(f.path,value)
    return value


def test_v2_entry_plan_uses_real_pure_loader_without_unit_or_service(recovery,monkeypatch,capsys):
    f=recovery;value=select_v2(f);_,manifest,argv=entry_fixture(monkeypatch,f.root,'0.49.1')
    manifest['board_endpoint']='http://localhost/mcp'
    def forbidden(*_a,**_k):pytest.fail('pure V2 plan constructed service or entered unit')
    monkeypatch.setattr(entry,'in_continuation_unit',forbidden)
    monkeypatch.setattr(module,'ReviewRecoveryService',forbidden)
    from blackoutburst_agents import continuation_pool_configuration as normal
    monkeypatch.setattr(normal,'continuation_plan',forbidden)
    assert entry.main(argv('continuation-plan','--continuation',str(f.path)))==0
    result=json.loads(capsys.readouterr().out)
    assert result['schema']==module.SCHEMA_V2 and result['prior_recovery_id']==value['prior_recovery_id']
    assert result['expected_prior_configuration_sha256']=='b'*64
    assert result['activated'] is result['execution_qualified'] is result['launchable'] is False
    assert not (f.root/'review-recoveries').exists() and 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('action',['continuation-plan','run-continuation-once'])
@pytest.mark.parametrize('version',['0.48.3','0.49.0'])
def test_v2_entry_refuses_sdk_before_0491_before_loading_a_controller(recovery,monkeypatch,capsys,action,version):
    f=recovery;select_v2(f);_,_,argv=entry_fixture(monkeypatch,f.root,version)
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:None)
    def forbidden(*_a,**_k):pytest.fail('old release selected a controller')
    monkeypatch.setattr(module,'ReviewRecoveryService',forbidden)
    monkeypatch.setattr(module,'review_recovery_plan',forbidden)
    from blackoutburst_agents import continuation_pool_service as normal
    monkeypatch.setattr(normal,'ContinuationPoolService',forbidden)
    assert entry.main(argv(action,'--continuation',str(f.path)))==3
    assert json.loads(capsys.readouterr().out)['reason']=='pool_review_recovery_version_required'


def test_v2_entry_retains_exact_unit_gate_before_configuration_or_service(recovery,monkeypatch,capsys):
    f=recovery;select_v2(f);_,_,argv=entry_fixture(monkeypatch,f.root,'0.49.1')
    def denied(*_):entry.p.fail('pool_continuation_unit_required')
    monkeypatch.setattr(entry,'in_continuation_unit',denied)
    monkeypatch.setattr(module,'ReviewRecoveryService',lambda *_a,**_k:pytest.fail('unit bypass'))
    from blackoutburst_agents import development_pool_selection as selections
    monkeypatch.setattr(selections,'file_document',lambda *_a,**_k:pytest.fail('config read before exact unit'))
    assert entry.main(argv('run-continuation-once','--continuation',str(f.path)))==3
    assert json.loads(capsys.readouterr().out)['reason']=='pool_continuation_unit_required'


def test_v2_entry_constructs_recovery_service_with_qualified_manifest_tools(recovery,monkeypatch,capsys):
    f=recovery;value=select_v2(f);release,manifest,argv=entry_fixture(monkeypatch,f.root,'0.49.1');calls=[]
    monkeypatch.setattr(entry,'in_continuation_unit',lambda *_:calls.append('unit'))
    from blackoutburst_agents import continuation_pool_service as normal
    monkeypatch.setattr(normal,'ContinuationPoolService',lambda *_a,**_k:pytest.fail('V2 used normal queue controller'))
    def constructor(root,**kwargs):
        assert calls==['unit'];calls.append('service')
        assert root==f.root and kwargs['configuration_path']==f.path
        assert kwargs['executable']==release/'tools/codex' and kwargs['gh_executable']==release/'tools/gh'
        assert kwargs['interpreter']==manifest['python']['path'] and kwargs['max_seconds']==3510
        assert json.loads(f.path.read_bytes())==value
        return SimpleNamespace(run_once=lambda *,should_stop:dict(status='needs_review',reason='review_recovery_pending',activated=False))
    monkeypatch.setattr(module,'ReviewRecoveryService',constructor)
    assert entry.main(argv('run-continuation-once','--continuation',str(f.path)))==3
    assert calls==['unit','service']
    assert json.loads(capsys.readouterr().out)==dict(status='needs_review',reason='review_recovery_pending',activated=False)
