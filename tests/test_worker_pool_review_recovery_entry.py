"""Installed entry routes the explicit recovery schema only after its unit gate."""
import json
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
