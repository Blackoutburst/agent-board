"""Real Worker/Board/Planner/Broker/Runtime; fake model and named test only."""
from copy import deepcopy
import copy
import pickle
from pathlib import Path
import subprocess
import uuid

import pytest

from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.dev_manifest import DevelopmentError,NamedTest,digest,prepare_manifest,canonical
from blackoutburst_agents.development_checkpoint_proof import DevelopmentCheckpointRequest,DevelopmentCheckpointReady
from blackoutburst_agents.git_workspace import LocalRepositoryGrant,LocalGitWorkspaceController
import test_development_worker as worker_helpers


def git(root,*args):
    return subprocess.check_output(['/usr/bin/git','-C',str(root),*args],env={'PATH':'/usr/bin:/bin',
        'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null'}).decode().strip()


@pytest.fixture
def checkpoint_case(request):
    if worker_helpers.Board is None:pytest.skip('Board sources not bundled')
    case=worker_helpers.DevelopmentWorkerTests(methodName='runTest');case.setUp()
    # Real independent Git workspace, so the peer's private producer can use
    # this same fixture without inventing a source grant or parent identity.
    source=case.root
    git(source,'init','--quiet','--initial-branch=main')
    git(source,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','add','main.py')
    git(source,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--quiet','-m','Initial')
    base=git(source,'rev-parse','HEAD');state=case.base/'git-state';work=case.base/'checkouts'
    state.mkdir(mode=0o700);work.mkdir(mode=0o700)
    grant=LocalRepositoryGrant('owner','fixture-repository',str(source),str(work),base)
    workspaces=LocalGitWorkspaceController(state,grant=grant)
    operation=str(uuid.uuid4());workspaces.plan(operation_id=operation,scope=case.manifest_scope)
    workspace=workspaces.materialize(operation)
    case.root=Path(workspace['checkout']);case.broker_dir=case.base/'checkpoint-broker'
    prepare_manifest(case.broker_dir,root=case.root,base_sha=base,scope=case.manifest_scope,paths=['main.py'],resource='repo:fixture',
        authorize_scope=lambda *_:True,tests=[NamedTest('unit',backend_id=case.backend.backend_id)])
    case.checkpoint_request=DevelopmentCheckpointRequest(case.worker,'logical-task',str(uuid.uuid4()),str(uuid.uuid4()),
        str(uuid.uuid4()),base,'b'*64,after_actions=2)
    case.frames=[];case.observations=[];case.planners=[];case.on_checkpoint=None
    def factory(**options):
        planner=DevelopmentPlanner(**options);case.planners.append(planner)
        class Adapter:
            def run(self):
                frame=planner.run(checkpoint_request=case.checkpoint_request)
                case.frames.append(frame)
                if type(frame) is DevelopmentCheckpointReady:
                    with frame.proof.hold() as observation:
                        case.observations.append(deepcopy(observation));frame.proof.recheck(observation)
                    if case.on_checkpoint:case.on_checkpoint(frame.proof)
                    state=planner.journal.read()
                    return dict(operation_id=planner.operation_id,status='checkpoint_ready',reason='',used=state['used'],review=None,
                        stop_requested=False)
                return frame
            def close(self):planner.close()
        return Adapter()
    case.worker.planner_factory=factory
    options={} if getattr(request,'param',None) is None else {'reasoning_effort':request.param}
    case.prepared=case.prepare(operation_id=operation,**options)
    case.operation=operation;case.workspaces=workspaces;case.source=source;case.base_commit=base
    try:yield case
    finally:case.doCleanups()


def actions(case):
    return [case.action('patch',path='main.py',expected_sha256=digest(b'value = 1\n'),content='value = 2\n'),
        case.action('read',path='main.py'),case.action('test',name='unit'),case.action('final',summary='verified')]


@pytest.mark.parametrize('checkpoint_case',[None,'low'],indirect=True)
def test_patch_read_yield_is_ready_without_next_prompt_or_budget_charge(checkpoint_case):
    case=checkpoint_case;case.actions=actions(case)
    result=case.worker.run(case.operation)
    assert result['stage']=='running' and result['status']=='pending'
    frame=case.frames[0];assert type(frame) is DevelopmentCheckpointReady and frame.status=='checkpoint_ready'
    observation=case.observations[0]
    assert observation['selected_postimages']=={'main.py':digest(b'value = 2\n')}
    assert observation['used_planner']['turns']==observation['used_planner']['actions']==2
    assert observation['passed_tests_promoted'] is False
    assert len(case.inferences)==2 and len(case.backend.calls)==0 and len(case.actions)==2
    assert case.worker.journal.get(case.operation)['stage']=='running'
    # Explicit normal same-operation continuation does not replay patch/read;
    # there is still one absolute budget/deadline/fence, not a new generation.
    case.worker.planner_factory=DevelopmentPlanner
    final=case.worker.run(case.operation)
    assert final['status']=='review_ready' and len(case.inferences)==4 and len(case.backend.calls)==1
    assert final['planner']['used']['turns']==final['planner']['used']['actions']==4


def test_repeated_live_rechecks_never_charge_or_change_deadline_fence_and_tests(checkpoint_case):
    case=checkpoint_case;case.actions=actions(case)
    def capture(proof):
        planner=proof.planner;before=planner.journal.private.read('planner.json',2*1024*1024)
        broker=planner.broker.journal.private.read('state.json',2*1024*1024)
        fence=planner.fence.inspect_checkout(planner.broker.manifest['root_identity'])
        for _ in range(3):
            with proof.hold() as observation:
                assert proof.recheck(observation)==observation
                assert observation['passed_tests_promoted'] is False
        assert planner.journal.private.read('planner.json',2*1024*1024)==before
        assert planner.broker.journal.private.read('state.json',2*1024*1024)==broker
        assert planner.fence.inspect_checkout(planner.broker.manifest['root_identity'])==fence
    case.on_checkpoint=capture;case.worker.run(case.operation)
    assert len(case.inferences)==2


def test_a_late_source_change_during_fresh_authorization_is_refused(checkpoint_case):
    case=checkpoint_case;case.actions=actions(case)
    def capture(proof):
        original=proof.planner._fresh
        def changed(*args,**kwargs):
            result=original(*args,**kwargs);(case.root/'main.py').write_text('value = 99\n');return result
        proof.planner._fresh=changed
        with pytest.raises(DevelopmentError):
            with proof.hold():pass
    case.on_checkpoint=capture;case.worker.run(case.operation)
    assert len(case.inferences)==2


def test_pending_extra_runtime_cannot_hide_behind_finished_history(checkpoint_case):
    case=checkpoint_case;case.actions=actions(case)
    def capture(proof):
        from blackoutburst_agents.runtime_journal import RuntimeJournal
        planner=proof.planner
        config=RuntimeJournal.configuration(planner.scope,planner.work['id'],str(uuid.uuid4()),'extra',provider='local_broker')
        planner.runtime_journal.begin(config)
        with pytest.raises(DevelopmentError):
            with proof.hold():pass
    case.on_checkpoint=capture;case.worker.run(case.operation)
    assert len(case.inferences)==2


def test_ready_snapshot_and_receipt_never_export_private_model_or_source_body(checkpoint_case):
    case=checkpoint_case;case.actions=actions(case);case.worker.run(case.operation)
    raw=canonical(case.observations[0])
    for value in [b'value = 2',b'Change the fixture',b'last_result',b'model_summary',b'instruction']:
        assert value not in raw


@pytest.mark.parametrize('mutation',['source','claim','scope','runtime','worker_stage','stop'])
def test_live_hold_rejects_drift_or_pending_runtime_without_a_new_effect(checkpoint_case,mutation):
    case=checkpoint_case;case.actions=actions(case);observed=[]
    def capture(proof):
        planner=proof.planner
        before=planner.journal.read();broker_before=planner.broker.journal.private.read('state.json',2*1024*1024)
        if mutation=='source':(case.root/'main.py').write_text('value = 99\n')
        elif mutation=='claim':
            ticket=case.read_ticket();case.board.release_ticket(case.client.actor,ticket['id'],ticket['version'],'fixture-release','Clean test release')
        elif mutation=='scope':case.board.set_policy(case.human,True,False,True,True,[],[])
        elif mutation=='runtime':
            original=case.control.call
            def pending(name,args):
                value=original(name,args)
                if name=='lookup_execution':value['execution']['cleanup_confirmed']=False
                return value
            case.control.call=pending
        elif mutation=='worker_stage':
            op=case.worker.journal.get(case.operation);case.worker._save(op,'needs_review',reason='fixture review')
        else:planner.request_stop()
        with pytest.raises(DevelopmentError):
            with proof.hold():pass
        assert planner.journal.read()==before
        assert planner.broker.journal.private.read('state.json',2*1024*1024)==broker_before
        observed.append(True)
    case.on_checkpoint=capture
    case.worker.run(case.operation)
    assert observed==[True] and len(case.inferences)==2 and not case.backend.calls


def test_proof_cannot_be_copied_serialized_or_rechecked_outside_its_hold(checkpoint_case):
    case=checkpoint_case;case.actions=actions(case)
    def capture(proof):
        for copier in (copy.copy,copy.deepcopy,pickle.dumps):
            with pytest.raises(TypeError):copier(proof)
        with pytest.raises(DevelopmentError):proof.recheck(case.observations[0])
        with proof.hold() as observation:
            wrong=deepcopy(observation);wrong['selected_postimages']['main.py']='0'*64
            with pytest.raises(DevelopmentError):proof.recheck(wrong)
            assert proof.recheck(observation)==observation
    case.on_checkpoint=capture;case.worker.run(case.operation)
    with pytest.raises(Exception):
        with case.frames[0].proof.hold():pass  # Normal worker already closed its resources.
