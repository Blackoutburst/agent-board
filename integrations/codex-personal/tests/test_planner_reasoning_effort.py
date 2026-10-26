"""Explicit controller effort; real journals/Board/Git and fake CLI/model only."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import test_codex_turn as cli_fixture
SESSION=cli_fixture.SESSION
import test_dev_planner as planner_fixture
import test_development_worker as worker_fixture
from test_finite_pool_batch import batch_case
from blackoutburst_agents.codex_turn import execute_turn, _arguments
from blackoutburst_agents.subscription_probe import ProbeSettings
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_planner import _reasoning_configuration
from blackoutburst_agents.finite_pool_batch import FLEXIBLE_SCHEMA, load_batch
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.work_journal import WorkJournal
from deploy.worker_pool import entry


@pytest.fixture
def cli():
    f=cli_fixture.CodexTurnTests(methodName='runTest');f.setUp()
    try:yield f
    finally:f.doCleanups()


@pytest.fixture
def planner():
    f=planner_fixture.PlannerTests(methodName='runTest');f.setUp()
    try:yield f
    finally:f.doCleanups()


@pytest.fixture
def worker():
    f=worker_fixture.DevelopmentWorkerTests(methodName='runTest');f.setUp()
    try:yield f
    finally:f.doCleanups()


@pytest.mark.parametrize('effort',['low','medium','high','xhigh'])
@pytest.mark.parametrize('resume',[None,SESSION])
def test_real_cli_receives_exact_effort_on_fresh_and_resume(cli,effort,resume):
    capture=cli.directory/'capture.json'
    executable=cli.fake_cli(f'open({str(capture)!r},"w").write(json.dumps(args))\nsuccess()\n')
    result=execute_turn(ProbeSettings(executable,model='same-model'), 'Private prompt',cli.workspace,
                        resume_session_id=resume,reasoning_effort=effort)
    assert result['status']=='completed' and result['verification']['tool_free'] is True
    args=json.loads(capture.read_text());flag='model_reasoning_effort="'+effort+'"'
    assert args.count(flag)==1 and args[args.index(flag)-1]=='-c'
    assert args[args.index('--model')+1]=='same-model' and args[-1]=='-'
    assert ('resume' in args)==(resume is not None)
    if resume is not None:assert args[-2]==resume


@pytest.mark.parametrize('value',[True,False,0,1,[],{},'','LOW','low\n','max','ultra','none','minimal',
                                'low"\nmodel_provider="other'])
def test_invalid_effort_is_refused_before_metadata_cli_or_model(cli,value):
    with patch('blackoutburst_agents.codex_turn.inspect_cli',side_effect=AssertionError('CLI metadata forbidden')):
        result=execute_turn(ProbeSettings(Path('/not/executed')), 'private',cli.workspace,reasoning_effort=value)
    assert result['reason']=='invalid_input' and result['status']=='blocked'
    with pytest.raises(DevelopmentError,match='invalid_planner_reasoning_effort'):
        _reasoning_configuration(value)


def test_default_argv_and_durable_planner_configuration_omit_effort(planner):
    f=planner;p=f.create(reasoning_effort=None)
    assert 'reasoning_effort' not in p.configuration
    assert p.binding['configuration_hash']==digest(canonical(p.configuration))
    settings=ProbeSettings(Path('/public/codex'),model='same-model')
    assert _arguments(settings,settings.executable,None)==_arguments(settings,settings.executable,None,reasoning_effort=None)
    assert not any('model_reasoning_effort' in value for value in _arguments(settings,settings.executable,SESSION))


def test_effort_is_pinned_before_planner_fence_and_received_by_every_turn(planner):
    f=planner;f.successful_actions();received=[];original=f.transport
    def transport(*args,**kwargs):
        received.append((kwargs.pop('reasoning_effort'),kwargs.get('resume_session_id')))
        return original(*args,**kwargs)
    p=f.create(reasoning_effort='low',_transport=transport)
    assert p.configuration['reasoning_effort']=='low'
    assert p.binding['configuration_hash']==digest(canonical(p.configuration))
    assert p.journal.read()['configuration']['reasoning_effort']=='low'
    result=p.run()
    assert result['status']=='review_ready' and len(received)==4
    assert [effort for effort,_ in received]==['low']*4
    assert received[0][1] is None and all(session is not None for _,session in received[1:])


def test_mutating_bound_effort_cannot_start_an_inference(planner):
    f=planner;f.successful_actions();p=f.create(reasoning_effort='low')
    p.configuration['reasoning_effort']='high'
    result=p.run()
    assert result['status']=='needs_review' and result['reason']=='planner_reasoning_effort_changed'
    assert not f.transports


def test_cli_rejecting_explicit_effort_never_falls_back(cli):
    capture=cli.directory/'attempts.txt'
    executable=cli.fake_cli(f'open({str(capture)!r},"a").write("attempt\\n")\nraise SystemExit(2)\n')
    result=execute_turn(ProbeSettings(executable), 'Private prompt',cli.workspace,reasoning_effort='low')
    assert result['status']=='blocked' and result['reason']=='execution_failed'
    assert capture.read_text()=='attempt\n' and result['text'] is None


def test_worker_effort_drift_on_same_operation_refuses_before_model(worker):
    f=worker;first=f.prepare(reasoning_effort='low')
    op=f.journal.get(first['operation_id'])
    assert op['configuration']['reasoning_effort']=='low' and not f.inferences
    with pytest.raises(DevelopmentError,match='development_prepare_conflict'):f.prepare(reasoning_effort='high')
    with pytest.raises(DevelopmentError,match='development_prepare_conflict'):f.prepare()
    assert f.journal.get(first['operation_id'])==op and not f.inferences
    received=[];original=f.worker.transport
    def transport(*args,**kwargs):
        received.append(kwargs.pop('reasoning_effort'));return original(*args,**kwargs)
    f.worker.transport=transport;f.successful_actions()
    assert f.worker.run(first['operation_id'])['status']=='review_ready'
    count=len(f.inferences)
    with pytest.raises(DevelopmentError,match='development_prepare_conflict'):f.prepare(reasoning_effort='medium')
    assert len(f.inferences)==count and received==['low']*3


def test_worker_default_none_is_same_legacy_configuration(worker):
    f=worker;first=f.prepare();op=f.journal.get(first['operation_id'])
    assert 'reasoning_effort' not in op['configuration']
    assert f.prepare(reasoning_effort=None)==first and f.journal.get(first['operation_id'])==op
    assert not f.inferences


def test_real_finite_batch_carries_effort_through_preparation_selection_worker_and_planner(batch_case):
    f=batch_case;job={**f.jobs[0],'reasoning_effort':'low'}
    config={**f.config,'schema':FLEXIBLE_SCHEMA,'jobs':[job]}
    with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical(config))
    received=[];original=f.transport
    def transport(*args,**kwargs):
        received.append(kwargs.pop('reasoning_effort'));return original(*args,**kwargs)
    f.transport=transport
    result=f.batch.run(f.config_path)
    assert result['status']=='complete' and received==['low','low']
    operation=result['pool']['jobs'][0]['id'];root=f.batch_root/'batches'/config['batch_id']
    with PrivateDirectory(root/('selection-'+job['name'])) as private:
        selected=decode(private.read(operation+'.json',65536))
    assert selected['selection']['reasoning_effort']=='low'
    preparation=root/('preparation-'+job['name'])/operation
    intent=decode(file_document(preparation/'intent.json',128*1024)[0])
    assert intent['request']['reasoning_effort']=='low'
    pool_job=result['pool']['jobs'][0]
    with WorkJournal(root/'pool'/'workers'/pool_job['worker_id']/'work') as journal:
        assert journal.get(operation)['configuration']['reasoning_effort']=='low'
    state=decode(file_document(preparation/'broker'/'planner.json',2*1024*1024)[0])
    assert state['configuration']['reasoning_effort']=='low'


@pytest.mark.parametrize('effort',[None,'low'])
def test_actual_entry_plan_discloses_opt_in_and_preserves_legacy_omission(batch_case,monkeypatch,capsys,effort):
    f=batch_case;job=dict(f.jobs[0])
    if effort is not None:job['reasoning_effort']=effort
    config={**f.config,'schema':FLEXIBLE_SCHEMA,'jobs':[job]}
    with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical(config))
    manifest={'unix_owner':'bb-agent-synthetic','board_owner':'owner','board_endpoint':f.batch.endpoint,
              'unit_limits':{'lifetime_seconds':150}}
    monkeypatch.setattr(entry.p,'verify_release',lambda *_:manifest)
    monkeypatch.setattr(entry,'owner_context',lambda *_:SimpleNamespace())
    monkeypatch.setattr(entry.p,'owner_root',lambda *_:f.batch_root)
    monkeypatch.setattr(entry,'in_installed_unit',lambda *_:pytest.fail('plan entered unit execution'))
    monkeypatch.setattr(entry.sys,'path',list(entry.sys.path))
    assert entry.main(['--release',str(f.base/'release'),'--owner','bb-agent-synthetic',
                       'batch-plan','--batch',str(f.config_path)])==0
    captured=capsys.readouterr();assert captured.err==''
    result=json.loads(captured.out)
    expected={key:job[key] for key in ('name','agent_id','project_id','ticket_id','resource','model')}
    if effort is not None:expected['reasoning_effort']=effort
    assert result['jobs']==[expected] and result['activated'] is False and result['execution_qualified'] is False
    assert f.calls==[] and f.clients==[] and not list((f.batch_root/'batches').iterdir())


@pytest.mark.parametrize('value',[None,True,42,'max','low\n'])
def test_batch_explicit_null_or_invalid_effort_is_not_silently_ignored(batch_case,value):
    f=batch_case;config={**f.config,'schema':FLEXIBLE_SCHEMA,'jobs':[{**f.jobs[0],'reasoning_effort':value}]}
    with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical(config))
    with pytest.raises(DevelopmentError,match='invalid_planner_reasoning_effort'):
        load_batch(f.config_path,root=f.batch_root,owner_id='owner',board_endpoint=f.batch.endpoint,max_seconds=60)
    assert f.calls==[] and f.clients==[] and not list((f.batch_root/'batches').iterdir())
