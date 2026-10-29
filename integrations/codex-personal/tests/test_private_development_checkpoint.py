"""Real tiny Git repositories; no network, account, real model or publication."""
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import uuid

import pytest

from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import canonical,digest
from blackoutburst_agents.private_development_checkpoint import PrivateDevelopmentCheckpointController, validate_configuration, checkpoint_id_for
from test_git_workspace import setup, planned, git


@pytest.fixture
def checkpoint_case(setup):
    f=setup;f['op'],_=planned(f);f['checkout']=Path(f['controller'].materialize(f['op'])['checkout'])
    root=f['state'].parent/'private-checkpoints';root.mkdir(mode=0o700)
    config={'schema':'private-development-checkpoint-config-v1','task_id':'logical-task',
        'owner_id':f['scope']['owner_id'],'project_id':f['scope']['project_id'],'ticket_id':f['scope']['ticket_id'],
        'repository_id':f['grant'].repository_id,'resource':'repo:fixture','original_public_base':f['grant'].base_commit,
        'global_paths':['example.py'],'global_test_caps_sha256':'a'*64,'static_caps_sha256':'b'*64,'source_release_sha256':'c'*64}
    f.update(checkpoint_root=root,configuration=config,
        checkpoints=PrivateDevelopmentCheckpointController(f['controller'],root,configuration=config))
    return f


@pytest.mark.parametrize('change',[{'schema':'local-git-delivery-v1'},{'global_paths':['.git/config']},
    {'global_paths':['example.py','EXAMPLE.py']},{'global_paths':[]},{'global_test_caps_sha256':'bad'},
    {'original_public_base':'main'},{'allow_publish':True}])
def test_closed_configuration_refuses_untyped_or_expanded_caps(checkpoint_case,change):
    f=checkpoint_case;config={**deepcopy(f['configuration']),**change}
    with pytest.raises(DevelopmentError):validate_configuration(config)
    assert list(f['checkpoint_root'].iterdir())==[]


def test_no_serialized_proof_can_create_objects_or_ref(checkpoint_case):
    f=checkpoint_case;controller=f['checkpoints'];before=git(f['checkout'],'count-objects','-v')
    with pytest.raises(DevelopmentError,match='private_checkpoint_(proof_required|producer_unavailable)'):
        controller.capture(checkpoint_id=str(uuid.uuid4()),proof={'cleanup_confirmed':True,'ready':True})
    assert git(f['checkout'],'count-objects','-v')==before
    assert git(f['checkout'],'for-each-ref','--format=%(refname)','refs/blackoutburst/private-checkpoints/')==''
    assert list(f['checkpoint_root'].iterdir())==[]


def test_checkpoint_component_has_no_finalize_or_publication_entry_point(checkpoint_case):
    from blackoutburst_agents.git_publication import GitHubDraftPublisher
    controller=checkpoint_case['checkpoints']
    assert not hasattr(controller,'finalize') and not hasattr(controller,'publish')
    assert not hasattr(controller,'prepare_review') and not hasattr(controller,'push')
    with pytest.raises(DevelopmentError,match='git_publication_controller_required'):
        GitHubDraftPublisher(controller,checkpoint_case['state'].parent/'publication',grant=None)


def test_workspace_head_and_index_are_untouched_by_historical_missing_read(checkpoint_case):
    f=checkpoint_case;head=git(f['checkout'],'rev-parse','HEAD');index=(f['checkout']/'.git/index').read_bytes()
    with pytest.raises(DevelopmentError):f['checkpoints'].verify(str(uuid.uuid4()))
    assert git(f['checkout'],'rev-parse','HEAD')==head
    assert (f['checkout']/'.git/index').read_bytes()==index


def test_historical_partial_read_never_creates_a_missing_lock(checkpoint_case):
    f=checkpoint_case;checkpoint_id=str(uuid.uuid4());record=f['checkpoint_root']/checkpoint_id
    record.mkdir(mode=0o700)
    with pytest.raises(DevelopmentError,match='private_checkpoint_lock_missing_or_changed'):
        f['checkpoints'].verify(checkpoint_id)
    assert list(record.iterdir())==[]


def test_exposed_configuration_cannot_be_mutated_after_freeze(checkpoint_case):
    controller=checkpoint_case['checkpoints']
    controller.configuration['resource']='repo:other'
    with pytest.raises(DevelopmentError,match='private_checkpoint_configuration_changed'):
        controller.verify(str(uuid.uuid4()))


@pytest.fixture
def live_case(monkeypatch,request):
    peer=pytest.importorskip('test_development_checkpoint_proof')
    original=peer.git
    def git_with_unselected(root,*args):
        if args[-2:]==('add','main.py'):
            (root/'unselected.txt').write_text('untouched public fixture\n')
            return original(root,*args,'unselected.txt')
        return original(root,*args)
    monkeypatch.setattr(peer,'git',git_with_unselected)
    fixture=peer.checkpoint_case.__wrapped__(request)
    case=next(fixture)
    case.actions=peer.actions(case);case.capture_results=[]
    root=case.base/'private-checkpoints';root.mkdir(mode=0o700)
    case.checkpoint_root=root;case.checkpoint_id=checkpoint_id_for(case.operation)
    def capture(proof):
        observation=case.observations[-1]
        config={'schema':'private-development-checkpoint-config-v1','task_id':observation['task_id'],
            **{key:observation['scope'][key] for key in ('owner_id','project_id','ticket_id')},
            'repository_id':case.workspaces.grant['repository_id'],'resource':observation['resource'],
            'original_public_base':observation['original_public_base'],'global_paths':observation['selected_paths'],
            'global_test_caps_sha256':observation['test_caps_sha256'],'static_caps_sha256':observation['static_caps_sha256'],
            'source_release_sha256':observation['source_release_sha256']}
        case.producer=PrivateDevelopmentCheckpointController(case.workspaces,root,configuration=config)
        case.capture_results.append(case.producer.capture(checkpoint_id=case.checkpoint_id,proof=proof))
    case.on_checkpoint=capture
    try:yield case
    finally:
        try:next(fixture)
        except StopIteration:pass


def test_actual_ready_proof_captures_private_commit_without_delivery_or_head_index_effect(live_case):
    case=live_case;head=git(case.root,'rev-parse','HEAD');index=(case.root/'.git/index').read_bytes()
    result=case.worker.run(case.operation)
    assert case.capture_results, result
    seal=case.capture_results[0]
    assert seal['schema']=='private-development-checkpoint-v1' and seal['publishable'] is False
    assert seal['historical'] is False and seal['launchable'] is False
    assert git(case.root,'rev-parse','HEAD')==head==case.base_commit
    assert (case.root/'.git/index').read_bytes()==index
    assert git(case.root,'show',seal['commit_sha']+':main.py')=='value = 2'
    assert git(case.root,'show',seal['commit_sha']+':unselected.txt')=='untouched public fixture'
    assert git(case.root,'rev-parse',seal['commit_sha']+'^')==head
    assert not case.backend.calls and len(case.inferences)==2
    assert 'delivery_sha' not in seal and 'test_evidence' not in seal
    commit=git(case.root,'cat-file','commit',seal['commit_sha'])
    assert seal['configuration_sha256'] in commit and seal['proof_sha256'] in commit
    # Historical read remains possible after the normal worker closed the live
    # proof handles; it never asks an old claim or starts another operation.
    expected={**seal,'historical':True}
    assert case.producer.verify(case.checkpoint_id)==expected


@pytest.mark.parametrize('field,value',[('task_id','other-task'),('resource','repo:other'),
    ('test_caps_sha256','0'*64),('static_caps_sha256','0'*64),('source_release_sha256','0'*64),
    ('original_public_base','0'*40),('selected_paths',['unselected.txt'])])
def test_live_proof_cannot_be_relabelled_with_other_global_caps(live_case,field,value):
    case=live_case;capture=case.on_checkpoint
    def changed(proof):
        case.observations[-1][field]=value
        with pytest.raises(DevelopmentError):capture(proof)
    case.on_checkpoint=changed;case.worker.run(case.operation)
    assert not case.capture_results
    assert git(case.root,'for-each-ref','--format=%(refname)','refs/blackoutburst/private-checkpoints/')==''


@pytest.mark.parametrize('mutation',['unselected','mode','ref','head','index','source'])
def test_actual_capture_or_history_refuses_source_ref_and_index_drift(live_case,mutation):
    case=live_case
    if mutation in ('unselected','mode'):
        capture=case.on_checkpoint
        def changed(proof):
            if mutation=='unselected':(case.root/'unselected.txt').write_text('changed\n')
            else:(case.root/'main.py').chmod(0o700)
            with pytest.raises(DevelopmentError):capture(proof)
        case.on_checkpoint=changed
        case.worker.run(case.operation)
        assert not case.capture_results
        assert git(case.root,'for-each-ref','--format=%(refname)','refs/blackoutburst/private-checkpoints/')==''
        return
    case.worker.run(case.operation);assert case.capture_results
    seal=case.capture_results[0]
    if mutation=='ref':git(case.root,'update-ref',seal['ref'],case.base_commit)
    elif mutation=='head':git(case.root,'update-ref','HEAD',seal['commit_sha'])
    elif mutation=='index':git(case.root,'add','main.py')
    else:(case.root/'main.py').write_text('new postimage\n')
    with pytest.raises(DevelopmentError):case.producer.verify(case.checkpoint_id)


@pytest.mark.parametrize('when',['intent','objects','ref','sealed'])
def test_interrupted_checkpoint_never_repeats_git_effects(live_case,monkeypatch,when):
    case=live_case;create=PrivateDirectory.create;replace=PrivateDirectory.replace;captured=[]
    original=case.on_checkpoint
    def capture(proof):
        try:original(proof)
        except Exception:
            captured.append(True)
            with pytest.raises(DevelopmentError,match='private_checkpoint_alternative_key_refused'):
                case.producer.capture(checkpoint_id=str(uuid.uuid4()),proof=proof)
    case.on_checkpoint=capture
    def lost_create(private,name,raw):
        create(private,name,raw)
        if when=='intent' and name=='checkpoint.json':raise OSError('synthetic intent ACK lost')
        if when=='objects' and name.startswith('object-'):raise OSError('synthetic object ACK lost')
    def lost_replace(private,name,raw):
        replace(private,name,raw)
        if when=='sealed' and name=='checkpoint.json' and b'"phase":"sealed"' in raw:
            raise OSError('synthetic sealed ACK lost')
    monkeypatch.setattr(PrivateDirectory,'create',lost_create);monkeypatch.setattr(PrivateDirectory,'replace',lost_replace)
    command=case.workspaces._git
    def lost_ref(args,**kwargs):
        result=command(args,**kwargs)
        if when=='ref' and args[0]=='update-ref':raise OSError('synthetic ref ACK lost')
        return result
    monkeypatch.setattr(case.workspaces,'_git',lost_ref)
    case.worker.run(case.operation);assert captured==[True]
    before=git(case.root,'count-objects','-v');refs=git(case.root,'for-each-ref','--format=%(refname):%(objectname)')
    if when=='sealed':
        assert case.producer.verify(case.checkpoint_id)['historical']
    else:
        with pytest.raises(DevelopmentError):case.producer.verify(case.checkpoint_id)
    assert git(case.root,'count-objects','-v')==before
    assert git(case.root,'for-each-ref','--format=%(refname):%(objectname)')==refs


def test_private_record_is_rejected_by_existing_delivery_reader_and_finalize(live_case):
    from blackoutburst_agents.git_delivery import LocalGitDeliveryController
    case=live_case;case.worker.run(case.operation);assert case.capture_results
    record=case.checkpoint_root/case.checkpoint_id
    with PrivateDirectory(record) as private:
        private.create('delivery.json',private.read('checkpoint.json',8*1024*1024))
        with pytest.raises(DevelopmentError,match='git_delivery_record_refused'):LocalGitDeliveryController._read(private)
    delivery=case.base/'ordinary-deliveries';review=case.base/'ordinary-review'
    delivery.mkdir(mode=0o700);review.mkdir(mode=0o700)
    controller=LocalGitDeliveryController(case.workspaces,delivery,review)
    # A checkpoint metadata dictionary is not test evidence or a delivery seal.
    snapshot=controller.capture(operation_id=case.operation,scope=case.manifest_scope,
        changes=[{'path':'main.py','sha256':digest(b'value = 2\n'),'mode':'100644'}])
    with pytest.raises(DevelopmentError,match='git_delivery_test_evidence_required'):
        controller.finalize(operation_id=case.operation,snapshot_sha256=digest(canonical(snapshot)),
            test_evidence=case.capture_results[0],message='must not publish checkpoint')
    assert git(case.root,'for-each-ref','--format=%(refname)','refs/heads/codex/delivery-')==''


def test_normal_explicit_second_generation_preserves_original_base_and_parent_chain(live_case):
    from blackoutburst_agents.git_workspace import LocalGitWorkspaceController,LocalRepositoryGrant
    from blackoutburst_agents.dev_manifest import prepare_manifest,NamedTest
    from test_development_worker import FixtureClient
    from test_runtime_integration import ControlClient
    from test_dev_planner import FakePolicy
    case=live_case;case.worker.run(case.operation);assert case.capture_results
    first=case.capture_results[0];old_root=case.root;old_operation=case.operation
    old_broker=case.broker_dir
    before={name:(old_broker/name).read_bytes() for name in ('state.json','planner.json')}
    # Test-only explicit handoff through the existing stop/release APIs. The
    # producer itself performs neither action and returns no next-run grant.
    case.worker.stop(old_operation)
    ticket=case.read_ticket()
    case.board.release_ticket(case.client.actor,ticket['id'],ticket['version'],'generation-handoff',
        'Clean explicit fixture handoff',status='')
    new_client=FixtureClient(case.board,case.raw,session='generation-next')
    registration=new_client.register();del new_client.registration;new_client.register_count=0
    case.client=new_client
    case.scope={**case.scope,'board_session_id':new_client.session_id,'agent_id':registration['identity']['id']}
    case.manifest_scope={key:case.scope[key] for key in ('owner_id','agent_id','project_id','ticket_id')}
    case.manifest_scope.update(device_id=registration['identity']['device_id'],session_id=new_client.session_id,
        profile_version=registration['profile_version'])
    case.control=ControlClient(case.board,case.raw,new_client.session_id)
    state=case.base/'git-state-next';work=case.base/'checkouts-next'
    state.mkdir(mode=0o700);work.mkdir(mode=0o700)
    grant=LocalRepositoryGrant('owner','fixture-repository',str(old_root),str(work),first['commit_sha'])
    case.workspaces=LocalGitWorkspaceController(state,grant=grant)
    case.operation=str(uuid.uuid4());case.workspaces.plan(operation_id=case.operation,scope=case.manifest_scope)
    case.root=Path(case.workspaces.materialize(case.operation)['checkout'])
    assert (case.root/'main.py').read_bytes()==b'value = 2\n'
    case.broker_dir=case.base/'broker-next';case.backend_dir=case.base/'backend-next';case.policy_dir=case.base/'policy-next'
    case.backend_dir.mkdir(mode=0o700);case.policy_dir.mkdir(mode=0o700)
    case.planner_root=case.base/'planner-next';case.planner_root.mkdir(mode=0o700)
    case.policy=FakePolicy(case.planner_root);case.policy.close=lambda:None
    prepare_manifest(case.broker_dir,root=case.root,base_sha=first['commit_sha'],scope=case.manifest_scope,
        paths=['main.py'],resource='repo:fixture',authorize_scope=lambda *_:True,
        tests=[NamedTest('unit',backend_id=case.backend.backend_id)])
    planner_factory=case.worker.planner_factory;case.worker=case.make_worker();case.worker.planner_factory=planner_factory
    case.checkpoint_request=replace(case.checkpoint_request,worker=case.worker,generation_id=str(uuid.uuid4()),
        cycle_id=str(uuid.uuid4()),attempt_id=str(uuid.uuid4()),parent_checkpoint_sha256=first['checkpoint_sha256'])
    case.actions=[case.action('patch',path='main.py',expected_sha256=digest(b'value = 2\n'),content='value = 3\n'),
        case.action('read',path='main.py')]
    case.checkpoint_id=checkpoint_id_for(case.operation)
    def capture_parent(proof):
        # Reuse the same immutable configuration; only the derived local
        # workspace and actual new proof/session differ from generation one.
        controller=PrivateDevelopmentCheckpointController(case.workspaces,case.checkpoint_root,
            configuration=case.producer.configuration)
        case.producer=controller
        case.capture_results.append(controller.capture(checkpoint_id=case.checkpoint_id,proof=proof,
            parent_checkpoint_id=first['checkpoint_id']))
    case.on_checkpoint=capture_parent
    case.prepare(operation_id=case.operation,request_id='generation-next')
    result=case.worker.run(case.operation)
    assert len(case.capture_results)==2,result
    second=case.capture_results[-1]
    assert second['original_public_base']==first['original_public_base']==case.base_commit
    assert second['base_commit']==first['commit_sha'] and second['parent_checkpoint_sha256']==first['checkpoint_sha256']
    assert second['configuration_sha256']==first['configuration_sha256']
    assert git(case.root,'show',second['commit_sha']+':main.py')=='value = 3'
    assert git(case.root,'rev-parse','HEAD')==first['commit_sha']
    assert git(old_root,'rev-parse','HEAD')==case.base_commit
    assert all((old_broker/name).read_bytes()==raw for name,raw in before.items())
    assert case.producer.verify(second['checkpoint_id'])['checkpoint_sha256']==second['checkpoint_sha256']
    git(old_root,'update-ref',first['ref'],case.base_commit)
    with pytest.raises(DevelopmentError):case.producer.verify(second['checkpoint_id'])


@pytest.mark.parametrize('when',['ref','projection'])
def test_scope_revocation_after_ref_effect_retains_unknown_intent_without_seal(live_case,monkeypatch,when):
    case=live_case;command=case.workspaces._git;original=case.on_checkpoint;revoked=[]
    def revoke(args,**kwargs):
        result=command(args,**kwargs)
        if not revoked and ((when=='ref' and args[0]=='update-ref') or
                (when=='projection' and args[:2]==['cat-file','commit'])):
            revoked.append(True)
            case.board.set_policy(case.human,True,False,True,True,[],[])
        return result
    monkeypatch.setattr(case.workspaces,'_git',revoke)
    captured=[]
    def capture(proof):
        with pytest.raises(DevelopmentError):original(proof)
        captured.append(True)
    case.on_checkpoint=capture;case.worker.run(case.operation)
    assert revoked==captured==[True] and not case.capture_results
    assert git(case.root,'for-each-ref','--format=%(refname)','refs/blackoutburst/private-checkpoints/')
    with pytest.raises(DevelopmentError,match='private_checkpoint_interrupted_requires_review'):
        case.producer.verify(case.checkpoint_id)
    assert git(case.root,'rev-parse','HEAD')==case.base_commit
