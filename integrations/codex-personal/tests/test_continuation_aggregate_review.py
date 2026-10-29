"""Real published aggregate/Board/Runtime/Lifecycle; external seams synthetic."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import time
import uuid
import pytest

from blackoutburst_agents.continuation_aggregate_review import ContinuationAggregateReviewer
from blackoutburst_agents.continuation import ContinuationTask
from blackoutburst_agents.private_development_checkpoint import PrivateDevelopmentCheckpointController
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest,Budgets
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.dev_journal import PrivateDirectory
from test_development_aggregate_review_source import actual_lifecycle
from test_development_aggregate_delivery import aggregate_fixture,publisher_fixture,linux_lock_observation


def reviewer_case(f,aggregate,publisher,monkeypatch,stack):
    source,_unused_lifecycle,_unused_pool,_unused_job,calls,turns,profile=actual_lifecycle(f,aggregate,publisher,monkeypatch,stack)
    root=f.base/'operator';root.mkdir(mode=0o700)
    for name in ('config','connections'):(root/name).mkdir(mode=0o700)
    connection=root/'connections/reviewer.json';connection.write_bytes((f.base/'review-connection.json').read_bytes());connection.chmod(0o600)
    scope=source.receipt['author_scope'];batch_id=str(uuid.uuid4())
    coder={'name':'coder','agent_id':scope['agent_id'],'connection_file':str(root/'connections/coder.json'),
        'repository':{'owner_id':'owner','repository_id':source.workspaces.grant['repository_id'],'repository':str(root/'repositories/source'),
            'workspace_root':str(root/'checkouts'/batch_id/'coder'),'base_commit':f.commit},
        'project_id':scope['project_id'],'ticket_id':scope['ticket_id'],'paths':['one.py'],'resource':'repo:fixture',
        'actions':['repo.read_file','repo.patch_file','repo.run_test'],'verifier_path':str(root/'verifiers/fixed.py'),
        'verifier_sha256':'b'*64,'test_name':'unit','test_timeout_seconds':2,'test_max_output_bytes':1024,
        'instruction':'Selected original coding task','model':source.aggregate.final_proof.configuration['model'],
        'planner_budgets':PlannerBudgets(max_wall_seconds=600).document(),'broker_budgets':Budgets().document()}
    if 'reasoning_effort' in source.aggregate.final_proof.configuration:
        coder['reasoning_effort']=source.aggregate.final_proof.configuration['reasoning_effort']
    batch={'schema':'linux-finite-pool-batch-v2','batch_id':batch_id,'owner_id':'owner','board_endpoint':f.seed.endpoint,
        'timeout_seconds':1200,'jobs':[coder]}
    workflow={'schema':'linux-cloud-workflow-v1','batch_id':batch_id,'jobs':[{'name':'coder','paths':['one.py'],
        'commit_message':'Exact aggregate','pr_title':'Exact aggregate','pr_body':'Exact aggregate',
        'reviewer':{'agent_id':profile['id'],'connection_file':str(connection),'instruction':'Independently review the real final tested aggregate.',
            'model':'explicit-independent-review-model','timeout':120,'skills':['review']}}]}
    config={'schema':'linux-cloud-continuation-v1','batch_file':str(root/'config/batch.json'),
        'workflow_file':str(root/'config/workflow.json'),'concurrency':1,'provider_backoff_seconds':30,
        'tasks':[{'name':'coder','logical_task_id':aggregate.task.configuration['logical_task_id'],'checkpoint_after_actions':2}]}
    for name,value in [('batch.json',batch),('workflow.json',workflow),('continuation.json',config)]:
        with PrivateDirectory(root/'config') as private:private.create(name,canonical(value))
    helper=ContinuationAggregateReviewer(f.base/'continuation-review-run',source=source,configuration_path=root/'config/continuation.json',
        root=root,owner_id='owner',board_endpoint=f.seed.endpoint,name='coder',executable=f.tools['codex'],
        cli_version='codex-cli 0.163.0',interpreter=f.tools['python'],deadline=time.monotonic()+300,max_seconds=1200)
    return helper,source,calls,turns,root


def run_fixture(tmp_path,monkeypatch,linux_lock_observation,check):
    # Select a valid operator UUID at the initial fixture input, before any
    # actual core/checkpoint/proof is produced. No proof/history is rewritten.
    task_id=str(uuid.uuid4());create=ContinuationTask.create;init=PrivateDevelopmentCheckpointController.__init__
    def task_create(cls,*args,**kwargs):
        if kwargs.get('logical_task_id')=='linux-logical':kwargs['logical_task_id']=task_id
        return create(*args,**kwargs)
    def cp_init(self,*args,**kwargs):
        if kwargs.get('configuration',{}).get('task_id')=='linux-logical':
            kwargs['configuration']={**kwargs['configuration'],'task_id':task_id}
        init(self,*args,**kwargs)
    monkeypatch.setattr(ContinuationTask,'create',classmethod(task_create));monkeypatch.setattr(PrivateDevelopmentCheckpointController,'__init__',cp_init)
    def published(f,aggregate,worker,directory,author_turns):
        seal=aggregate.seal(message='Final aggregate');aggregate.complete_local()
        publisher,public=publisher_fixture(f,aggregate,seal)
        aggregate.publish_and_handoff(publisher,title='Final aggregate',body='Final source and tests.')
        with ExitStack() as stack:
            helper,source,calls,turns,root=reviewer_case(f,aggregate,publisher,monkeypatch,stack)
            check(f,aggregate,helper,source,calls,turns,author_turns,public,root)
    aggregate_fixture(tmp_path,monkeypatch,linux_lock_observation,published)


def test_published_aggregate_runs_one_actual_reviewer_and_replay_is_historical(tmp_path,monkeypatch,linux_lock_observation):
    def check(f,aggregate,helper,source,calls,turns,author_turns,public,root):
        assert not helper.directory.exists()
        for key,replacement in [('resource','repo:unselected'),('model','unselected-model'),('reasoning_effort','high')]:
            present=key in helper.coder;old=helper.coder.get(key);helper.coder[key]=replacement
            with pytest.raises(DevelopmentError,match='continuation_review_selection_changed'):helper._selection()
            if present:helper.coder[key]=old
            else:helper.coder.pop(key)
        with pytest.raises(DevelopmentError,match='continuation_review_stopped'):helper.run(should_stop=lambda:True)
        assert not helper.directory.exists() and not turns
        original_model=helper.reviewer['model'];helper.reviewer['model']='unselected-model'
        with pytest.raises(DevelopmentError,match='continuation_review_configuration_changed'):helper.run()
        helper.reviewer['model']=original_model
        f.board.set_policy(f.human,True,False,True,True,f.actions,['repo:fixture'])
        with pytest.raises(DevelopmentError):helper.run()
        assert not helper.directory.exists() and not turns
        f.board.set_policy(f.human,True,True,True,True,f.actions,['repo:fixture'])
        before=source.status(source.operation_id);result=helper.run()
        assert result['status']=='complete' and result['verdict']=='corrections'
        assert len(turns)==1 and len(author_turns)==3 and public['posts']==public['pushes']==1
        ticket=f.board.get_ticket(f.human,aggregate.final_proof.scope['ticket_id'])['ticket']
        assert ticket['status']=='review' and ticket['claimant'] is None
        assert 'Add a boundary test' in ticket['outcome'] and source.status(source.operation_id)==before
        count=len(calls);assert helper.run()==result
        assert len(turns)==1 and len(author_turns)==3
        assert not any(name in ('register_agent','claim_ticket','update_ticket') for name,_ in calls[count:])
        receipt=helper.historical_receipt_pin()
        assert receipt['status']=='published' and receipt['accepted'] is receipt['launchable'] is False
        assert receipt['review']==result and receipt['review_operation_id']==result['operation_id']
    run_fixture(tmp_path,monkeypatch,linux_lock_observation,check)


@pytest.mark.parametrize('fault',['lost_ack'])
def test_stop_revocation_or_ambiguous_review_never_reexecutes(tmp_path,monkeypatch,linux_lock_observation,fault):
    def check(f,aggregate,helper,source,calls,turns,author_turns,public,root):
        if fault=='stop':
            with pytest.raises(DevelopmentError,match='continuation_review_stopped'):helper.run(should_stop=lambda:True)
            assert not helper.directory.exists() and not turns
        elif fault=='revocation':
            f.board.set_policy(f.human,True,False,True,True,f.actions,['repo:fixture'])
            with pytest.raises(DevelopmentError):helper.run()
            assert not helper.directory.exists() and not turns
        else:
            from blackoutburst_agents.review_lifecycle import DeliveryReviewLifecycle
            publish=DeliveryReviewLifecycle.publish
            def lost(self,*args,**kwargs):
                publish(self,*args,**kwargs);raise BaseException('synthetic lost opinion ACK')
            monkeypatch.setattr(DeliveryReviewLifecycle,'publish',lost)
            with pytest.raises(BaseException,match='synthetic lost opinion ACK'):helper.run()
            assert len(turns)==1
            with pytest.raises(DevelopmentError,match='continuation_review_pending'):helper.run()
            assert len(turns)==1
        assert len(author_turns)==3 and public['posts']==public['pushes']==1
    run_fixture(tmp_path,monkeypatch,linux_lock_observation,check)


def test_declared_publication_metadata_is_not_a_reviewer_source(tmp_path):
    with pytest.raises(DevelopmentError,match='continuation_review_source_required'):
        ContinuationAggregateReviewer(tmp_path/'new',source={'status':'complete'},configuration_path=tmp_path/'missing',
            root=tmp_path,owner_id='owner',board_endpoint='https://board.invalid/mcp',name='coder',
            executable='/missing/codex',cli_version='codex-cli 0.163.0',interpreter='/missing/python',
            deadline=time.monotonic()+100,max_seconds=100)
    assert not (tmp_path/'new').exists()


@pytest.mark.parametrize('field,value',[('resource','repo:other'),('model','other-model'),('reasoning_effort','high')])
def test_selection_rejects_mismatched_author_configuration_before_source_access(tmp_path,field,value):
    # Negative-only unit check of scalar selection; these unissued metadata
    # objects never enter constructor/run and cannot be publication authority.
    from types import SimpleNamespace
    helper=object.__new__(ContinuationAggregateReviewer)
    scope={'owner_id':'owner','board_endpoint':'https://board.invalid/mcp','agent_id':'author','project_id':'project','ticket_id':'ticket'}
    helper.task={'logical_task_id':'logical'};helper.owner_id='owner';helper.endpoint=scope['board_endpoint']
    helper.coder={'agent_id':'author','project_id':'project','ticket_id':'ticket','resource':'repo:ok','model':'expected-model',
        'connection_file':'author.json','repository':{'base_commit':'a'*40,'repository_id':'repo'}}
    helper.coder[field]=value;helper.reviewer={'agent_id':'reviewer','connection_file':'reviewer.json'}
    helper.source=SimpleNamespace(receipt={'status':'complete','accepted':False,'operation_id':'op','author_scope':scope,
        'original_public_base':'a'*40},operation_id='op',grant={'owner_id':'owner'},
        frame={'floor':{'resource':'repo:ok'}},workspaces=SimpleNamespace(grant={'repository_id':'repo'}),
        aggregate=SimpleNamespace(task=SimpleNamespace(configuration={'logical_task_id':'logical'}),
            final_proof=SimpleNamespace(configuration={'model':'expected-model'})))
    with pytest.raises(DevelopmentError,match='continuation_review_selection_changed'):helper._selection()
    assert not list(tmp_path.iterdir())
