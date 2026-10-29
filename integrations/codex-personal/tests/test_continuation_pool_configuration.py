"""Real owned operator files; no Board, provider, credential or process access."""
from copy import deepcopy
import json
import uuid

import pytest

from blackoutburst_agents import continuation_pool_configuration as module
from blackoutburst_agents.dev_manifest import Budgets,DevelopmentError,canonical
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.finite_pool_batch import FLEXIBLE_SCHEMA,SCHEMA as LEGACY_SCHEMA
from blackoutburst_agents.cloud_workflow import SCHEMA as WORKFLOW_SCHEMA


@pytest.fixture
def case(tmp_path,monkeypatch):
    root=tmp_path.resolve()/'owner';root.mkdir(mode=0o700)
    (root/'config').mkdir(mode=0o700)
    batch_id=str(uuid.uuid4())
    job=dict(name='coder',agent_id='selected-coder',connection_file=str(root/'connections/coder.json'),
        repository=dict(owner_id='owner',repository_id='repo',repository=str(root/'repositories/source'),
            workspace_root=str(root/'checkouts'/batch_id/'coder'),base_commit='a'*40),
        project_id='project',ticket_id='ticket',paths=['src/main.py'],resource='repo:approved',
        actions=['repo.read_file','repo.patch_file','repo.run_test'],
        verifier_path=str(root/'verifiers/fixed.py'),verifier_sha256='b'*64,test_name='unit',
        test_timeout_seconds=2,test_max_output_bytes=1024,instruction='PRIVATE_CODER_INSTRUCTION',
        model='selected-model',reasoning_effort='low',
        planner_budgets=PlannerBudgets(max_turns=8,max_actions=8,max_wall_seconds=30).document(),
        broker_budgets=Budgets(max_operations=24,max_tests=2).document())
    batch=dict(schema=FLEXIBLE_SCHEMA,batch_id=batch_id,owner_id='owner',
        board_endpoint='https://board.invalid/mcp',timeout_seconds=60,jobs=[job])
    workflow=dict(schema=WORKFLOW_SCHEMA,batch_id=batch_id,jobs=[dict(name='coder',paths=['src/main.py'],
        commit_message='PRIVATE_COMMIT_MESSAGE',pr_title='PRIVATE_PR_TITLE',pr_body='PRIVATE_PR_BODY',
        reviewer=dict(agent_id='selected-reviewer',connection_file=str(root/'connections/reviewer.json'),
            instruction='PRIVATE_REVIEW_INSTRUCTION',model='selected-review-model',timeout=30,skills=['review']))])
    value=dict(schema=module.SCHEMA,batch_file=str(root/'config/batch.json'),
        workflow_file=str(root/'config/workflow.json'),concurrency=1,provider_backoff_seconds=30,
        tasks=[dict(name='coder',logical_task_id=str(uuid.uuid4()),checkpoint_after_actions=2)])
    path=root/'config/continuation.json'
    def write():
        for p,v in [(path,value),(root/'config/batch.json',batch),(root/'config/workflow.json',workflow)]:
            p.write_bytes(canonical(v));p.chmod(0o600)
    write()
    def load(**overrides):
        return module.load_continuation(path,root=root,owner_id='owner',
            board_endpoint='https://board.invalid/mcp',max_seconds=90,**overrides)
    return root,path,value,batch,workflow,write,load


def test_plan_copies_exact_caps_without_credentials_models_or_private_state(case,monkeypatch):
    root,path,config,batch,workflow,write,load=case
    before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    # Connections, repository, verifier and task roots deliberately do not exist.
    import socket,subprocess
    def forbidden(*a,**k):raise AssertionError('Planning cannot execute or connect')
    monkeypatch.setattr(socket,'socket',forbidden);monkeypatch.setattr(subprocess,'Popen',forbidden)
    plan=module.continuation_plan(path,root=root,owner_id='owner',board_endpoint='https://board.invalid/mcp',max_seconds=90)
    assert plan['activated'] is plan['execution_qualified'] is plan['launchable'] is False
    task=plan['tasks'][0];job=batch['jobs'][0]
    assert task['cycle_limits']==dict(max_turns=8,max_actions=8,max_wall_seconds=30,
        max_operations=24,max_tests=2,max_read_bytes=job['broker_budgets']['max_read_bytes'],
        max_patch_bytes=job['broker_budgets']['max_patch_bytes'])
    assert task['model']=='selected-model' and task['reasoning_effort']=='low'
    assert task['reviewer_agent_id']=='selected-reviewer' and task['original_public_base']=='a'*40
    assert 'PRIVATE_' not in json.dumps(plan) and 'connection_file' not in json.dumps(plan)
    assert before=={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert set(p.name for p in root.iterdir())=={'config'}


@pytest.mark.parametrize('key,value',[
    ('concurrency',True),('concurrency',0),('concurrency',8),
    ('provider_backoff_seconds',True),('provider_backoff_seconds',29),('provider_backoff_seconds',3601),
    ('schema','linux-finite-pool-batch-v2'),('tasks',[]),('tasks',None),
    ('batch_file','/outside/batch.json'),('workflow_file','/outside/workflow.json'),
])
def test_rejects_unapproved_selection_and_control_shapes(case,key,value):
    *_,config,batch,workflow,write,load=case
    config[key]=value;write()
    with pytest.raises(DevelopmentError):load()


@pytest.mark.parametrize('key,value',[
    ('name','other-job'),('name',[]),('logical_task_id','../new-task'),
    ('logical_task_id',None),('checkpoint_after_actions',True),
    ('checkpoint_after_actions',0),('checkpoint_after_actions',9),
])
def test_task_identity_and_yield_threshold_are_explicit_and_bounded(case,key,value):
    root,path,config,batch,workflow,write,load=case
    config['tasks'][0][key]=value;write()
    with pytest.raises(DevelopmentError):load()


@pytest.mark.parametrize('extra',[
    {'max_cycles':999999},{'launchable':True},{'cycle_limits':{'max_actions':99999}},
    {'provider':'different-account'},{'permission_granted':True},
])
def test_metadata_cannot_extend_lifetime_caps_or_supply_authority(case,extra):
    root,path,config,batch,workflow,write,load=case
    config.update(extra);write()
    with pytest.raises(DevelopmentError):load()


def test_zero_legacy_caps_are_refused_without_silent_allowance(case):
    root,path,config,batch,workflow,write,load=case
    for key in ('max_tests','max_patch_bytes'):
        original=batch['jobs'][0]['broker_budgets'][key]
        batch['jobs'][0]['broker_budgets'][key]=0;write()
        with pytest.raises(DevelopmentError,match='continuation_pool_cycle_limits_invalid'):load()
        batch['jobs'][0]['broker_budgets'][key]=original


def test_cycle_cannot_outlive_the_approved_batch_process_budget(case):
    root,path,config,batch,workflow,write,load=case
    batch['jobs'][0]['planner_budgets']['max_wall_seconds']=61;write()
    with pytest.raises(DevelopmentError,match='continuation_pool_cycle_exceeds_batch_timeout'):load()


def test_initial_four_job_schema_does_not_implicitly_enable_continuation(case):
    root,path,config,batch,workflow,write,load=case
    batch['schema']=LEGACY_SCHEMA;write()
    with pytest.raises(DevelopmentError):load()


def test_reviewer_must_remain_distinct_and_within_the_existing_workflow(case):
    root,path,config,batch,workflow,write,load=case
    workflow['jobs'][0]['reviewer']['agent_id']=batch['jobs'][0]['agent_id'];write()
    with pytest.raises(DevelopmentError):load()


def test_document_aliases_and_file_symlinks_cannot_replace_selection(case):
    root,path,config,batch,workflow,write,load=case
    config['workflow_file']=config['batch_file'];write()
    with pytest.raises(DevelopmentError):load()
    config['workflow_file']=str(root/'config/workflow.json');write()
    target=root/'config/saved.json';path.rename(target);path.symlink_to(target)
    with pytest.raises((DevelopmentError,OSError)):load()


def test_batch_drift_during_loading_is_detected_at_the_end(case,monkeypatch):
    root,path,config,batch,workflow,write,load=case
    original=module.load_workflow
    def drift(*args,**kwargs):
        result=original(*args,**kwargs);batch['jobs'][0]['instruction']='CHANGED_PRIVATE';write();return result
    monkeypatch.setattr(module,'load_workflow',drift)
    with pytest.raises(DevelopmentError,match='continuation_pool_configuration_changed'):load()


def test_returned_documents_are_copies_and_never_a_live_permit(case):
    root,path,config,batch,workflow,write,load=case
    first,pins=load();first['configuration']['concurrency']=99;first['batch']['jobs'][0]['model']='other'
    second,new_pins=load()
    assert second['configuration']['concurrency']==1 and second['batch']['jobs'][0]['model']=='selected-model'
    assert new_pins==pins


def second_target(case):
    root,path,config,batch,workflow,write,load=case
    other=deepcopy(batch['jobs'][0]);other.update(name='coder2',agent_id='coder2',ticket_id='ticket2',
        resource='repo:second',connection_file=str(root/'connections/coder2.json'))
    other['repository']['workspace_root']=str(root/'checkouts'/batch['batch_id']/'coder2')
    batch['jobs'].append(other)
    review=deepcopy(workflow['jobs'][0]);review['name']='coder2';workflow['jobs'].append(review)
    config['tasks'].append(dict(name='coder2',logical_task_id=str(uuid.uuid4()),checkpoint_after_actions=1))
    config['concurrency']=2


def test_two_explicit_targets_keep_distinct_task_ids_models_and_resources(case):
    root,path,config,batch,workflow,write,load=case
    second_target(case);write();loaded,pins=load()
    assert loaded['configuration']['concurrency']==2
    assert [task['name'] for task in loaded['configuration']['tasks']]==['coder','coder2']
    assert len(loaded['cycle_limits'])==2


@pytest.mark.parametrize('field',['name','logical_task_id'])
def test_distinct_targets_cannot_alias_one_logical_task_or_job(case,field):
    root,path,config,batch,workflow,write,load=case
    second_target(case);config['tasks'][1][field]=config['tasks'][0][field];write()
    with pytest.raises(DevelopmentError):load()
