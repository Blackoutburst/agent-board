"""Correlate real GitDelivery, reviewer Work/RunStore/Runtime historical data.

Only model transports and Linux observations are synthetic. Historical source
reads never call the Board, reserve Runtime or replay the review.
"""
from contextlib import ExitStack, closing
from functools import partial
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import uuid

import pytest

import test_development_delivery as author_fixture
from test_board_worker import Client
from test_runtime_integration import ControlClient
from blackoutburst_agents.context import ContextScope, build_context
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.development_delivery import DevelopmentDeliveryProof
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from blackoutburst_agents.git_delivery import LocalGitDeliveryController
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant
from blackoutburst_agents.review_worker import DeliveryReviewWorker
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.team_writer_source import TeamWriterSource
from blackoutburst_agents import team_role_pool as role_module
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.worker_pool import WorkerPool
from blackoutburst_agents.work_journal import WorkJournal
from test_team_role_pool import write, finished, operation


@pytest.fixture
def historical(tmp_path):
    f=author_fixture.DevelopmentDeliveryTests('test_exact_all_manifest_map_reconstructs_distinct_git_snapshot_and_seals')
    f.setUp()
    try:
        f.ready();evidence=f.proof.evidence(f.snapshot)
        f.seal=f.deliveries.finalize(operation_id=f.operation,snapshot_sha256=evidence['snapshot_sha256'],
            test_evidence=evidence,message='Exact synthetic tested delivery')
        assert f.worker.publish(f.operation)['stage']=='complete'
        device=f.board.create_device(f.human,'Independent synthetic reviewer',[])
        raw=f.board.authenticate(device['token']);client=Client(f.board,raw,'independent-review-session')
        registration=client.register();ticket=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        f.board.claim_ticket(client.actor,ticket['id'],ticket['version'])
        scope={**f.scope,'agent_id':registration['identity']['id'],'board_session_id':client.session_id}
        context=build_context(client.call,scope=ContextScope(**{key:scope[key] for key in
            ('owner_id','agent_id','project_id','ticket_id')}),registration=registration)
        material='main.py: value changes from 1 to 2; exact delivery has a passing named unit test.'
        export_id=str(uuid.uuid4())
        export=f.deliveries.prepare_review(operation_id=f.operation,review_operation_id=export_id,
            reviewer_scope=scope,context_sha256=context['fingerprint'],material=material)
        with ExitStack() as resources:
            journal=resources.enter_context(WorkJournal(f.base/'review-work'))
            store=resources.enter_context(closing(RunStore(f.base/'review-runs')))
            runtime=resources.enter_context(RuntimeJournal(f.base/'review-runtime'))
            control=ControlClient(f.board,raw,client.session_id)
            turns=[]
            def transport(settings,prompt,workspace,**callbacks):
                callbacks['before_prompt']();callbacks['on_started'](999999999)
                session=str(uuid.uuid4());callbacks['on_session'](session);turns.append(prompt)
                return {'status':'completed','session_id':session,'text':json.dumps({'verdict':'corrections',
                    'text':'The code is tested. Add documentation before acceptance.'}),
                    'verification':{'cleanup_confirmed':True,'tool_free':True}}
            reviewer=DeliveryReviewWorker(journal,store,client,runtime,control,
                delivery_guard=partial(f.deliveries.review_guard,f.operation,export_id),
                runner_factory=lambda state,**options:PersonalRunner(state,transport,**options))
            result=reviewer.run(scope=scope,request_id='independent-review',selection=export['request']['selection'],
                instruction='Inspect only this exact tested delivery.',material=material,workspace=export['workspace'],
                executable=sys.executable,model='explicit-review-model',timeout=10)
            assert result['status']=='complete',result
            review_id=result['operation_id']
        f.reviewer_raw=raw;f.reviewer_scope=scope;f.export_id=export_id;f.review_id=review_id
        f.review_turns=turns;f.source_calls=[]
        def source_factory():
            resources=ExitStack()
            try:
                # Reopen every SQLite handle on the caller's actual Pool thread.
                author_journal=resources.enter_context(WorkJournal(f.journal.directory))
                author_store=resources.enter_context(closing(RunStore(f.store.state_dir,existing_only=True)))
                author_runtime=resources.enter_context(RuntimeJournal(f.runtime.directory))
                def no_rpc(*_):
                    f.source_calls.append('rpc');pytest.fail('Historical writer source invoked Board/Runtime RPC')
                author_client=Client(f.board,f.raw,f.scope['board_session_id']);author_client.call=no_rpc
                author_control=ControlClient(f.board,f.raw,f.scope['board_session_id']);author_control.call=no_rpc
                author=DevelopmentBoardWorker(author_journal,author_store,author_runtime,author_client,author_control,
                    _test_anchor=f.base/'anchor')
                workspaces=LocalGitWorkspaceController(f.workspaces.state_root,
                    grant=LocalRepositoryGrant(**f.workspaces.grant))
                proof=DevelopmentDeliveryProof(author,workspaces,f.operation)
                deliveries=LocalGitDeliveryController(workspaces,f.deliveries.delivery_root,
                    f.deliveries.review_root,test_guard=proof)
                journal=resources.enter_context(WorkJournal(f.base/'review-work'))
                store=resources.enter_context(closing(RunStore(f.base/'review-runs',existing_only=True)))
                runtime=resources.enter_context(RuntimeJournal(f.base/'review-runtime'))
                client=Client(f.board,raw,scope['board_session_id']);client.call=no_rpc
                control=ControlClient(f.board,raw,scope['board_session_id']);control.call=no_rpc
                worker=DeliveryReviewWorker(journal,store,client,runtime,control,
                    runner_factory=lambda *_a,**_k:pytest.fail('Historical source replayed model'))
                return TeamWriterSource(resources,deliveries,worker,delivery_operation_id=f.operation,
                    review_export_id=export_id,review_operation_id=review_id)
            except BaseException:resources.close();raise
        f.source_factory=source_factory
        yield f
    finally:f.doCleanups()


def test_exact_historical_source_is_readonly_correlated_and_keeps_corrections(historical):
    f=historical;before=f.board.get_runtime(f.human)
    with f.source_factory() as source:
        value=source.read();assert source.read()==value
        assert value['historical_data'] is True
        assert value['selection']['delivery_sha']==f.seal['delivery_sha']
        assert value['reviewer_agent_id']!=value['selection']['author_agent_id']
        assert value['opinion']['verdict']=='corrections'
        assert value['review_operation_id']==f.review_id
    assert f.board.get_runtime(f.human)==before and not f.source_calls and len(f.review_turns)==1


@pytest.mark.parametrize('change',['opinion','result','runtime','export','delivery'])
def test_changed_actual_receipt_or_export_refuses_without_repair(historical,change):
    f=historical
    with f.source_factory() as source:
        worker=source.worker
        if change=='opinion':
            op=worker.journal.get(f.review_id);payload=dict(op['payload'])
            payload['opinion']={**payload['opinion'],'verdict':'accept'}
            worker.journal._db.execute('UPDATE operations SET payload_json=? WHERE id=?',
                (canonical(payload).decode(),f.review_id))
        elif change=='result':
            op=worker.journal.get(f.review_id)
            worker.run_store._db.execute("UPDATE turns SET status='blocked' WHERE id=?",(op['payload']['turn_id'],))
        elif change=='runtime':
            record=worker.runtime_journal.db.execute('SELECT id,receipt FROM executions').fetchone()
            receipt=json.loads(record['receipt']);receipt['cleanup_confirmed']=False
            worker.runtime_journal.db.execute('UPDATE executions SET receipt=? WHERE id=?',
                (canonical(receipt).decode(),record['id']))
        elif change=='export':
            path=Path(source.deliveries.review_root/f.export_id/'export/main.py')
            path.chmod(0o600);path.write_text('unreviewed bytes\n');path.chmod(0o400)
        else:(source.deliveries.delivery_root/f.operation/'delivery.json').unlink()
        with pytest.raises((DevelopmentError,FileNotFoundError)):source.read()
    assert not f.source_calls and len(f.review_turns)==1


def test_source_handles_are_thread_owned_and_factory_opens_on_pool_thread(historical):
    f=historical;values=[]
    def read():
        with f.source_factory() as source:values.append(source.read())
    thread=threading.Thread(target=read);thread.start();thread.join(timeout=15)
    assert not thread.is_alive() and len(values)==1
    with f.source_factory() as source:
        errors=[]
        def wrong_thread():
            try:source.read()
            except DevelopmentError:errors.append('refused')
        thread=threading.Thread(target=wrong_thread);thread.start();thread.join(timeout=5)
        assert errors==['refused']
    assert not f.source_calls


def test_metadata_cannot_replace_concrete_sdk_source():
    with pytest.raises(DevelopmentError,match='team_writer_concrete_source_required'):
        TeamWriterSource(ExitStack(),{'verified':True},object(),delivery_operation_id=str(uuid.uuid4()),
            review_export_id=str(uuid.uuid4()),review_operation_id=str(uuid.uuid4()))


@pytest.mark.parametrize('forged_verdict',[False,True])
def test_writer_pool_runs_one_pinned_turn_and_preserves_actual_review_verdict(historical,monkeypatch,forged_verdict):
    f=historical;root=f.base/'writer';root.mkdir(mode=0o700);resource='team:writer'
    f.board.set_policy(f.human,True,True,True,True,
        ['repo.read_file','repo.patch_file','repo.run_test'],['repo:fixture',resource])
    device=f.board.create_device(f.human,'Independent synthetic writer',[])
    raw=f.board.authenticate(device['token']);registration=Client(f.board,raw,'writer-seed').register()
    task=f.board.create_ticket(f.human,f.project['id'],'Summarize tested historical delivery',
        resource=resource,idempotency_key='writer')
    connection=root/'connection.json';write(connection,{'synthetic':'never usable credentials'})
    with f.source_factory() as source:value=source.read()
    inputs=root/'inputs.json';write(inputs,{'schema':'team-writer-inputs-v1',
        'sources':[{'source_id':'selected-review','sha256':digest(canonical(value))}]})
    policy_dir=root/'policy';policy_dir.mkdir(mode=0o700);workspace=root/'empty';workspace.mkdir(mode=0o700)
    for name in ('planner-plan.json','planner-attempt.json','planner-qualification.json'):write(policy_dir/name,{'synthetic':name})
    clients=[];controls=[];calls=[];source_threads=[]
    def client_factory(path,*,session_id,timeout):
        assert str(path)==str(connection)
        client=Client(f.board,raw,session_id);client.connection_identity=tuple(file_document(connection)[1]['identity'])
        client.register_initial=client.register;client.close=lambda:None;clients.append(client);return client
    def control_factory(client):
        control=ControlClient(f.board,raw,client.session_id);control.close=lambda:None;controls.append(control);return control
    class Policy:
        fingerprint='b'*64
        executable=str(Path(sys.executable).resolve())
        def __init__(self,directory):assert str(directory)==str(policy_dir);self.workspace=str(workspace)
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def validate_for(self,executable,root):assert executable==self.executable and root==self.workspace
    def source_factory():source_threads.append(threading.get_ident());return f.source_factory()
    output={'schema':'team-writer-summary-v1','summary':'A tested delivery requires documentation corrections.',
        'items':[{'source_id':'selected-review','review_operation_id':f.review_id,
            'verdict':'accept' if forged_verdict else 'corrections','text':'Add documentation before acceptance.'}]}
    def transport(settings,prompt,root,*,planner_policy,**callbacks):
        assert settings.model=='explicit-writer-model'
        callbacks['before_prompt']();callbacks['on_started'](999999999)
        session=str(uuid.uuid4());callbacks['on_session'](session);calls.append(prompt)
        assert value['selection']['delivery_sha'] in prompt and 'historical_data' in prompt
        return {'status':'completed','session_id':session,'text':json.dumps(output),
            'verification':{'cleanup_confirmed':True,'tool_free':True}}
    monkeypatch.setattr(role_module.DevelopmentBoardClient,'from_connection_file',client_factory)
    monkeypatch.setattr(role_module.RuntimeBoardClient,'from_worker',control_factory)
    monkeypatch.setattr(role_module,'LinuxPlannerPolicy',Policy)
    monkeypatch.setattr(role_module,'execute_turn',transport)
    driver=role_module.TeamRolePoolDriver(root/'selections',role='writer',connection_file=connection,
        owner_id='owner',agent_id=registration['identity']['id'],board_endpoint=Client.endpoint,
        writer_sources={'selected-review':source_factory})
    pool=WorkerPool(root/'pool',concurrency=1,ceiling=4,drivers={'writer':driver.binding()})
    try:
        job=pool.submit(request_id='writer',driver_id='writer',payload={'project_id':f.project['id'],'ticket_id':task['id']})
        selected=root/'selection.json';write(selected,{'schema':1,'role':'writer','operation_id':job['id'],
            'worker_id':job['worker_id'],'session_id':job['session_id'],
            'scope':{**driver.fixed,'project_id':f.project['id'],'ticket_id':task['id'],'board_session_id':job['session_id']},
            'resource':resource,'model':'explicit-writer-model','instruction':'Summarize only these exact historical inputs.',
            'timeout':10,'planner_policy_directory':str(policy_dir),'input_file':str(inputs)})
        driver.bind_selection(job,selected);case=SimpleNamespace(pool=pool)
        result=finished(case);op=operation(case)
        assert result['jobs'][0]['outcome']==('needs_review' if forged_verdict else 'complete'),(result,op)
        if forged_verdict:assert 'private_result' not in op['payload']
        else:assert op['payload']['private_result']==output
        assert len(calls)==1 and result['capacity_held']==0
        assert any(value!=threading.get_ident() for value in source_threads)
        assert not f.source_calls and len(f.review_turns)==1
        assert not {'update_ticket','release_ticket','post_message','create_ticket'} & {
            name for client in clients for name,_ in client.calls}
        ticket=f.board.get_ticket(f.human,task['id'])['ticket']
        assert ticket['status']=='in_progress' and not ticket['outcome'] and not ticket['evidence']
    finally:pool.shutdown(timeout=5);pool.close()
