"""Protected operator assembly with real Git/Board journals and fake inference.

Archived author receipts are deliberately synthetic Linux observations. These
tests never qualify an owner host, connect an account or publish to GitHub.
"""
from copy import deepcopy
from pathlib import Path
import unittest
import uuid
from unittest.mock import patch
import json
import threading

import pytest

import test_development_pool_delivery as archived
from blackoutburst_agents import cloud_workflow as module
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.finite_pool_batch import FLEXIBLE_SCHEMA
from test_finite_pool_batch import batch_case  # noqa: F401
from deploy.worker_pool import entry


class CloudWorkflowTests(unittest.TestCase):
    def fixture(self):
        helper=archived.DevelopmentPoolDeliveryTests();self.addCleanup(helper.doCleanups)
        f=helper.fixture();f.helper=helper
        for name in ('config','connections'):(f.base/name).mkdir(mode=0o700)
        connection=f.base/'connections'/'reviewer.json'
        with PrivateDirectory(connection.parent) as private:private.create(connection.name,canonical(dict(
            mcp_url=f.client.endpoint,token='SYNTHETIC_REVIEW_TOKEN',client='codex',identity_key='reviewer',name='Reviewer')))
        batch_id=str(uuid.uuid4())
        f.batch=dict(schema=FLEXIBLE_SCHEMA,batch_id=batch_id,owner_id='owner',board_endpoint=f.client.endpoint,
            timeout_seconds=60,jobs=[dict(name='development',agent_id=f.scope['agent_id'],connection_file=str(f.driver.selections.connection_file),
                repository=deepcopy(f.workspaces.grant),project_id=f.project['id'],ticket_id=f.ticket['id'],paths=['main.py','untouched.txt'])])
        f.config=dict(schema=module.SCHEMA,batch_id=batch_id,jobs=[dict(name='development',paths=['main.py'],
            commit_message='Apply the tested explicit change',pr_title='Deliver selected ticket',pr_body='Tested change for the selected ticket.',
            reviewer=dict(agent_id='distinct-reviewer',connection_file=str(connection),instruction='Review only the exact tested diff.',
                model='synthetic-review-model',timeout=10,skills=['review','python']))])
        f.path=f.base/'config'/'workflow.json'
        with PrivateDirectory(f.path.parent) as private:private.create(f.path.name,canonical(f.config))
        f.workflow=module.CloudLinuxWorkflow(f.path,root=f.base,batch=f.batch,owner_id='owner',board_endpoint=f.client.endpoint,
            gh_executable=f.gh,python_executable=f.python)
        return f

    def test_operator_paths_capture_actual_postimages_and_tested_sealed_diff(self):
        f=self.fixture()
        changes=f.workflow._postimages(f.pool,f.driver,f.job,['main.py','untouched.txt'])
        self.assertEqual(changes,[dict(path='main.py',sha256=digest(b'value = 2\n'),mode='100644')])
        f.helper.deliver(f)
        material=f.workflow._material(f.pipeline,f.operation,changes)
        self.assertIn(f.pipeline.status(f.operation)['delivery_sha'],material)
        self.assertIn('-value = 1',material);self.assertIn('+value = 2',material)
        self.assertEqual((f.pushes,f.posts),(1,1))

    def test_workflow_rejects_model_like_destination_and_paths_outside_operator_manifest(self):
        f=self.fixture()
        with self.assertRaises(DevelopmentError):f.workflow._postimages(f.pool,f.driver,f.job,['outside.txt'])
        changed=deepcopy(f.config);changed['jobs'][0]['destination']='https://example.invalid/private'
        with PrivateDirectory(f.path.parent) as private:private.replace(f.path.name,canonical(changed))
        with self.assertRaises(DevelopmentError):module.load_workflow(f.path,root=f.base,batch=f.batch)
        self.assertEqual((f.pushes,f.posts),(0,0))

    def test_stop_marks_real_settled_author_before_publication(self):
        f=self.fixture()
        with f.workflow.mutex:f.workflow.active_delivery=(f.pipeline,f.operation)
        f.workflow.stop()
        self.assertTrue(f.workflow.stopped.is_set())
        with self.assertRaises(DevelopmentError):f.helper.deliver(f)
        self.assertEqual((f.pushes,f.posts),(0,0))

    def test_configuration_and_connection_replacement_refuse_without_effect(self):
        f=self.fixture();connection=Path(f.config['jobs'][0]['reviewer']['connection_file'])
        with PrivateDirectory(connection.parent) as private:private.replace(connection.name,b'{}')
        with self.assertRaises(DevelopmentError):f.workflow.check()
        self.assertEqual((f.pushes,f.posts),(0,0))

    def test_original_v1_batch_cannot_implicitly_turn_on_workflow(self):
        f=self.fixture();batch={**f.batch,'schema':'linux-finite-pool-batch-v1'}
        with self.assertRaises(DevelopmentError):module.load_workflow(f.path,root=f.base,batch=batch)
        self.assertEqual((f.pushes,f.posts),(0,0))

    def workflow_fixture(self):
        from test_review_lifecycle import ReviewLifecycleTests
        from blackoutburst_agents.worker_pool import WorkerPool
        helper=ReviewLifecycleTests();self.addCleanup(helper.doCleanups)
        f=helper.fixture(publish_author=False,same_pool=True,submit_review=False)
        for name in ('config','connections'):(f.base/name).mkdir(mode=0o700)
        connection=f.base/'connections'/'reviewer.json'
        with PrivateDirectory(connection.parent) as private:
            private.create(connection.name,f.review_connection.read_bytes())
        batch_id=str(uuid.uuid4())
        f.batch=dict(schema=FLEXIBLE_SCHEMA,batch_id=batch_id,owner_id='owner',board_endpoint=f.client.endpoint,
            timeout_seconds=60,jobs=[dict(name='development',agent_id=f.scope['agent_id'],connection_file=str(f.driver.selections.connection_file),
                repository=deepcopy(f.workspaces.grant),project_id=f.project['id'],ticket_id=f.ticket['id'],paths=['main.py','untouched.txt'])])
        config=dict(schema=module.SCHEMA,batch_id=batch_id,jobs=[dict(name='development',paths=['main.py'],
            commit_message='Deliver the tested explicit fix',pr_title='Deliver selected ticket',pr_body='Explicit tested fix and independent review.',
            reviewer=dict(agent_id=f.review_driver.fixed['agent_id'],connection_file=str(connection),
                instruction='Review the exact tested diff.',model='synthetic-review-model',timeout=10,skills=['review','python']))])
        path=f.base/'config'/'workflow.json'
        with PrivateDirectory(path.parent) as private:private.create(path.name,canonical(config))
        f.workflow=module.CloudLinuxWorkflow(path,root=f.base,batch=f.batch,owner_id='owner',board_endpoint=f.client.endpoint,
            gh_executable=f.gh,python_executable=f.python)
        f.workflow_batch=f.base
        bindings=f.workflow.bindings(f.workflow_batch)
        f.pool.close();f.pool=WorkerPool(f.base/'pool',drivers={'development':f.driver.binding(),**bindings});self.addCleanup(f.pool.close)
        def operator_batch(state):
            state['ceiling']=1
            state['jobs'][0]['request_id']=batch_id+':development'
        f.pool.journal.change(operator_batch)
        # The archived author fixture owns a genuine Git controller whose path
        # layout predates FiniteLinuxPoolBatch. Only that protected layout lookup
        # is mapped here; all proof/Git/Board/Runtime/worker objects remain real.
        def existing_controller(state,*,grant):
            self.assertEqual(Path(state),f.base/'git-state'/batch_id/'development')
            self.assertEqual(grant.document(),f.workspaces.grant)
            return f.workspaces
        item=patch.object(module,'LocalGitWorkspaceController',side_effect=existing_controller)
        item.start();self.addCleanup(item.stop)
        f.workflow_tools=dict(executable={'path':str(f.python)},interpreter={'path':str(f.python)})
        return f

    @staticmethod
    def execute(f,*,should_stop=lambda:False):
        return f.workflow.deliver(pool=f.pool,drivers={'development':f.driver},batch=f.batch,submitted=[f.job],
            tools=f.workflow_tools,cli_version='codex-cli 0.163.0',should_stop=should_stop)

    def test_complete_workflow_publishes_one_pr_and_independent_opinion_on_same_ticket(self):
        f=self.workflow_fixture();before=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        result=self.execute(f);ticket=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        self.assertEqual(result['status'],'complete');self.assertTrue(result['ticket_published'])
        self.assertEqual((f.pushes,f.posts,len(f.review_turns)),(1,1,1))
        self.assertEqual(ticket['status'],'review');self.assertIsNone(ticket['claimant']);self.assertIsNone(ticket['assignee_agent_id'])
        item=result['jobs'][0];self.assertIn(item['delivery']['delivery_sha'],ticket['evidence'])
        self.assertIn('/pull/17',ticket['evidence']);self.assertIn(f.opinion_text,ticket['evidence'])
        self.assertNotEqual(f.workflow.review_drivers['development'].fixed['agent_id'],f.scope['agent_id'])
        self.assertEqual(ticket['version'],before['version']+4)
        self.assertEqual((f.native_prepares,f.native_qualifications),(1,1))
        self.assertEqual((f.pool.status()['capacity_held'],f.pool.status()['live_threads']),(0,0))
        self.assertEqual(len(f.pool.status()['jobs']),2)
        self.assertTrue(all(row['state']=='finished' and row['cleanup_confirmed'] for row in f.board.get_runtime(f.human)['executions']))

    def test_historical_workflow_checks_receipts_without_rpc_and_never_recreates_missing_pool_state(self):
        f=self.workflow_fixture();result=self.execute(f);f.pool.close()
        before=(len(f.lifecycle_calls),len(f.review_calls),len(f.review_turns),f.pushes,f.posts)
        (f.base/'selection-development').mkdir(mode=0o700)
        # Resolve only the archival layout's original concrete coder registry.
        with patch('blackoutburst_agents.development_pool.DevelopmentPoolDriver',return_value=f.driver):
            f.workflow.verify_completed(f.base,f.batch,result,tools=f.workflow_tools,cli_version='codex-cli 0.163.0')
        self.assertEqual((len(f.lifecycle_calls),len(f.review_calls),len(f.review_turns),f.pushes,f.posts),before)
        missing=f.base/'pool'/'pool-state.json';missing.unlink()
        with self.assertRaises((DevelopmentError,FileNotFoundError)):
            f.workflow.verify_completed(f.base,f.batch,result,tools=f.workflow_tools,cli_version='codex-cli 0.163.0')
        self.assertFalse(missing.exists())
        self.assertEqual((len(f.lifecycle_calls),len(f.review_calls),len(f.review_turns),f.pushes,f.posts),before)

    def test_change_after_test_cannot_be_published_by_full_workflow(self):
        f=self.workflow_fixture();(f.root/'main.py').write_text('value = 3\n')
        with self.assertRaises(DevelopmentError):self.execute(f)
        self.assertEqual((f.pushes,f.posts,len(f.review_turns)),(0,0,0))
        self.assertFalse(f.lifecycle_calls)

    def test_workflow_stop_at_final_transport_prevents_push_and_any_review(self):
        f=self.workflow_fixture()
        from blackoutburst_agents.git_publication import GitHubDraftPublisher
        original=GitHubDraftPublisher._transport
        def stop(publisher,*args,**options):
            value=original(publisher,*args,**options)
            if args[1]['phase']=='pushing':f.workflow.stop()
            return value
        with patch.object(GitHubDraftPublisher,'_transport',new=stop):
            with self.assertRaises(DevelopmentError):self.execute(f)
        self.assertEqual((f.pushes,f.posts,len(f.review_turns)),(0,0,0))


def test_installed_operator_workflow_plan_names_exact_selection_without_execution(batch_case,monkeypatch,capsys):
    f=batch_case
    batch={**f.config,'schema':FLEXIBLE_SCHEMA,'jobs':f.jobs[:1]}
    with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical(batch))
    selected=dict(schema=module.SCHEMA,batch_id=batch['batch_id'],jobs=[dict(name='coder0',paths=['one.txt'],
        commit_message='Fix selected behavior',pr_title='Selected ticket',pr_body='Explicit tested change.',
        reviewer=dict(agent_id='independent-reviewer',connection_file=str(f.batch_root/'connections'/'reviewer.json'),
            instruction='Review the exact change.',model='explicit-review-model',timeout=30,skills=['review']))])
    path=f.config_path.parent/'workflow.json'
    with PrivateDirectory(path.parent) as private:private.create(path.name,canonical(selected))
    manifest=dict(board_owner='owner',board_endpoint=batch['board_endpoint'],unit_limits={'lifetime_seconds':3600})
    monkeypatch.setattr(entry.p,'verify_release',lambda *_:manifest)
    monkeypatch.setattr(entry,'owner_context',lambda *_:object())
    monkeypatch.setattr(entry.p,'owner_root',lambda *_:f.batch_root)
    monkeypatch.setattr(entry,'in_installed_unit',lambda *_:pytest.fail('plan demanded execution unit'))
    result=entry.main(['--release',str(f.protected),'--owner','owner','workflow-plan',
        '--batch',str(f.config_path),'--workflow',str(path)])
    assert result==0
    value=json.loads(capsys.readouterr().out)
    assert value['jobs'][0]['ticket_id']==f.jobs[0]['ticket_id']
    assert value['workflow']==[dict(name='coder0',paths=['one.txt'],reviewer_agent_id='independent-reviewer',reviewer_model='explicit-review-model')]
    assert value['destination']=='https://github.com/Blackoutburst/agent-board'
    assert value['activated'] is False and value['execution_qualified'] is False
    assert f.clients==[] and f.native_calls==[] and f.calls==[]


def test_run_workflow_refuses_wrong_unit_before_configuration_or_rpc(batch_case,monkeypatch,capsys):
    f=batch_case;manifest=dict(board_owner='owner',board_endpoint=f.config['board_endpoint'],unit_limits={'lifetime_seconds':3600})
    monkeypatch.setattr(entry.p,'verify_release',lambda *_:manifest)
    monkeypatch.setattr(entry,'owner_context',lambda *_:object())
    monkeypatch.setattr(entry.p,'owner_root',lambda *_:f.batch_root)
    monkeypatch.setattr(entry,'in_installed_unit',lambda *_:entry.p.fail('pool_installed_unit_required'))
    assert entry.main(['--release',str(f.protected),'--owner','owner','run-workflow',
        '--batch',str(f.config_path),'--workflow',str(f.config_path.parent/'missing-workflow.json')])==3
    assert json.loads(capsys.readouterr().out)['reason']=='pool_installed_unit_required'
    assert f.clients==[] and f.native_calls==[] and f.calls==[]


def test_finite_batch_stop_companion_remains_alive_during_extended_delivery_phase(batch_case,monkeypatch):
    f=batch_case;batch={**f.config,'schema':FLEXIBLE_SCHEMA,'jobs':f.jobs[:1]}
    with PrivateDirectory(f.config_path.parent) as private:private.replace(f.config_path.name,canonical(batch))
    config=dict(schema=module.SCHEMA,batch_id=batch['batch_id'],jobs=[dict(name='coder0',paths=['one.txt'],
        commit_message='Explicit change',pr_title='Explicit ticket',pr_body='Tested change.',
        reviewer=dict(agent_id=f.jobs[1]['agent_id'],connection_file=f.jobs[1]['connection_file'],
            instruction='Review exact change.',model='fixture-model',skills=['review'],timeout=10))])
    path=f.config_path.parent/'workflow.json'
    with PrivateDirectory(path.parent) as private:private.create(path.name,canonical(config))
    workflow=module.CloudLinuxWorkflow(path,root=f.batch_root,batch=batch,owner_id='owner',board_endpoint=batch['board_endpoint'],
        gh_executable=f.tools['codex'],python_executable=f.tools['python'])
    stop=threading.Event();observed=[]
    def phase(self,**options):
        observed.append(options['pool'].status()['live_threads'])
        stop.set()
        assert self.stopped.wait(timeout=3),'batch Stop companion ended before delivery'
        return dict(schema=module.SCHEMA,status='needs_review',jobs=[],ticket_published=False)
    monkeypatch.setattr(module.CloudLinuxWorkflow,'deliver',phase)
    result=f.batch.run(f.config_path,should_stop=stop.is_set,workflow=workflow)
    assert observed==[0] and result['status']=='needs_review'
    assert result['pool']['stopped'] is True and result['ticket_published'] is False
    assert len(f.calls)==2 and len(f.native_calls)==2
