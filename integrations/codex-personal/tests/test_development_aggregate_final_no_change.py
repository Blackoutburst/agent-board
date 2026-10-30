"""Actual two-cycle finalization; final cycle only reads/retests existing work.

Native/CLI/HTTPS are synthetic. Real private source, aggregate, public seal,
Git push to a local bare repository, Board handoff and review Lifecycle run.
"""
import json
from pathlib import Path
import pytest
from blackoutburst_agents import continuation_pool_service as module
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import canonical,digest
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
from test_continuation_pool_service_integration import actual_service,linux_lock_observation,_REAL_LINUX_TEST_RUN


def test_partial_private_capture_is_not_repaired_or_adopted(tmp_path):
    from blackoutburst_agents.development_aggregate_delivery import AggregateDevelopmentDeliveryController
    from blackoutburst_agents.dev_manifest import DevelopmentError
    controller=object.__new__(AggregateDevelopmentDeliveryController)
    controller.operation_id='8b9c14a8-78a3-4968-8507-d8b5d76745d1'
    controller.paths={'source-deliveries':tmp_path/'source'}
    path=controller.paths['source-deliveries']/controller.operation_id;path.mkdir(parents=True,mode=0o700)
    marker=path/'private-source.json';marker.write_bytes(b'partial receipt retained\n')
    before=marker.read_bytes()
    with pytest.raises(DevelopmentError,match='development_aggregate_source_capture_uncertain'):
        controller._capture_source({},[])
    assert marker.read_bytes()==before and not (path/'delivery.lock').exists()


def test_public_delivery_still_refuses_empty_capture_before_any_journal_io():
    from blackoutburst_agents.git_delivery import LocalGitDeliveryController
    from blackoutburst_agents.dev_manifest import DevelopmentError
    controller=object.__new__(LocalGitDeliveryController)
    with pytest.raises(DevelopmentError,match='git_delivery_changes_refused'):
        controller.capture(operation_id='8b9c14a8-78a3-4968-8507-d8b5d76745d1',scope={},changes=[])


@pytest.mark.parametrize('fault',[None,'source','mode','unselected','stop','payload','empty_original'])
def test_retest_only_final_cycle_delivers_original_delta_or_stops_before_public_effect(actual_service,monkeypatch,fault):
    generations=2
    failures=[]
    target=module.ContinuationPoolService._target
    def traced(self,*args,**kwargs):
        try:return target(self,*args,**kwargs)
        except Exception as error:
            import traceback
            failures.append((type(error).__name__,getattr(error,'code',None),[(row.name,row.lineno) for row in traceback.extract_tb(error.__traceback__)]));raise
    monkeypatch.setattr(module.ContinuationPoolService,'_target',traced)
    import subprocess
    from blackoutburst_agents.git_publication import GitHubDraftPublisher,REPOSITORY,REMOTE,API
    from test_development_aggregate_delivery import native_receipt
    from test_git_workspace import git
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    batch_path=root/'config/batch.json';batch=json.loads(batch_path.read_text());job=batch['jobs'][0]
    verifier=Path(job['verifier_path'])
    verifier.write_bytes(b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n' if fault!='empty_original' else b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 1\\n"\n')
    job['verifier_sha256']=digest(verifier.read_bytes());batch_path.write_bytes(canonical(batch))
    config_path=root/'config/continuation.json';config=json.loads(config_path.read_text())
    config['tasks'][0]['checkpoint_after_actions']=3 if generations==2 else 8
    config_path.write_bytes(canonical(config))
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'run',_REAL_LINUX_TEST_RUN)
    if fault=='empty_original':
        import inspect,test_development_aggregate_delivery as native_helpers
        namespace=dict(vars(native_helpers));exec(inspect.getsource(native_receipt).replace('value = 3','value = 1'),namespace);native_receipt=namespace['native_receipt']
    monkeypatch.setattr(SupervisedLinuxNamedPythonBackend,'_run_job',native_receipt)
    remote=f.base/'remote.git';git(f.base,'clone','--bare','--no-hardlinks',str(f.source),str(remote))
    state={'pushes':0,'posts':0,'prs':[]};captured=[];api_checks=[]
    from blackoutburst_agents.development_aggregate_delivery import AggregateDevelopmentDeliveryController,PRIVATE_SOURCE_SCHEMA
    from blackoutburst_agents.dev_manifest import DevelopmentError
    capture=AggregateDevelopmentDeliveryController._capture_source
    def observe(controller,manifest,changes):
        snapshot=capture(controller,manifest,changes);captured.append((controller,snapshot))
        try:controller.final_proof.evidence(snapshot)
        except DevelopmentError:public_rejected=True
        else:public_rejected=False
        try:controller.final_proof._aggregate_source_evidence(snapshot,controller={})
        except DevelopmentError:declared_rejected=True
        else:declared_rejected=False
        api_checks.append((public_rejected,declared_rejected))
        rootpath=Path(manifest['root'])
        if fault=='source':(rootpath/'one.py').write_bytes(b'value = 999\n')
        elif fault=='mode':(rootpath/'one.py').chmod(0o700)
        elif fault=='unselected':(rootpath/'outside.py').write_bytes(b'outside\n')
        elif fault=='stop':
            state=controller.task.snapshot()['state'];controller.task.stop(expected_revision=state['revision'],request_id='stop-private-final-source')
        elif fault=='payload':
            path=controller.paths['source-deliveries']/controller.operation_id/snapshot['files'][0]['payload'];path.write_bytes(b'corrupt\n')
        return snapshot
    monkeypatch.setattr(AggregateDevelopmentDeliveryController,'_capture_source',observe)
    def command(publisher,argv,*,cwd,git=False):
        publisher._confirm_launch(argv,cwd=cwd,git=git)
        if git:
            assert REMOTE in argv and argv[1] in ('push','ls-remote')
            if argv[1]=='push':state['pushes']+=1
            result=subprocess.run([str(remote) if part==REMOTE else part for part in argv],cwd=cwd,
                env={'PATH':'/usr/bin:/bin','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_SYSTEM':'/dev/null','GIT_ALLOW_PROTOCOL':'file'},
                stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            return result.returncode,result.stdout,result.stderr
        if argv[1:]==['auth','status','--hostname','github.com']:return 0,b'synthetic official authentication\n',b''
        assert argv[1:5]==['api','--hostname','github.com','--method']
        method,path=argv[5:7]
        if path=='user':value={'login':'synthetic-user'}
        elif path==API:value=dict(full_name=REPOSITORY,html_url='https://github.com/'+REPOSITORY,permissions={'push':True},archived=False,disabled=False)
        elif method=='GET' and path.startswith(API+'/pulls?'):value=state['prs']
        elif method=='POST' and path==API+'/pulls':
            state['posts']+=1;payload=json.loads(Path(argv[8]).read_bytes());record=json.loads((Path(cwd)/'publication.json').read_bytes())
            value=dict(number=17,html_url='https://github.com/'+REPOSITORY+'/pull/17',state='open',draft=True,
                user={'login':'synthetic-user'},title=payload['title'],body=payload['body'],
                head=dict(ref=payload['head'],sha=record['request']['delivery_sha'],repo={'full_name':REPOSITORY}),
                base=dict(ref='main',sha=f.commit,repo={'full_name':REPOSITORY}))
            state['prs'].append(value)
        else:raise AssertionError('No unexpected network command')
        return 0,canonical(value),b''
    monkeypatch.setattr(GitHubDraftPublisher,'_command',command)
    if generations==2:
        first_actions=json.loads(sequence.read_text())
        if fault=='empty_original':first_actions=[dict(schema=1,kind='read',path='one.py'),dict(schema=1,kind='read',path='one.py')]
        else:first_actions[0]['content']='value = 3\n'
        sequence.write_text(json.dumps(first_actions+[dict(schema=1,kind='read',path='one.py')]))
        first=construct().run_once();assert first['tasks'][0]['phase']=='paused_cycle',first
        before={str(p):p.read_bytes() for p in root.rglob('planner.json')}
        expected=b'value = 2\n'
    else:before={};expected=b'value = 1\n'
    sequence.write_text(json.dumps([dict(schema=1,kind='read',path='one.py'),
        dict(schema=1,kind='test',name='unit'),dict(schema=1,kind='final',summary='Verified final aggregate')]))
    result=construct().run_once()
    if fault is None and failures:print(json.dumps(failures))
    assert len(captured)==1
    controller,snapshot=captured[0]
    assert snapshot['schema']==PRIVATE_SOURCE_SCHEMA and snapshot['request']['changes']==[]
    private=controller.paths['source-deliveries']/controller.operation_id
    assert (private/'private-source.json').exists() and not (private/'delivery.json').exists()
    assert api_checks==[(True,True)]
    if fault is not None:
        assert result['status'] in ('needs_you','stopped'),result
        assert (state['pushes'],state['posts'])==(0,0) and not f.review_turns
        key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
        with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:assert journal.snapshot()['state']['phase']!='complete'
        assert len(calls.read_text().splitlines())==6
        return
    assert result['status']=='pass_complete' and result['tasks'][0]['phase']=='independent_review_published',(result,failures)
    assert result['tasks'][0]['accepted'] is False and result['tasks'][0]['historical'] is False
    assert (state['pushes'],state['posts'])==(1,1)
    assert git(remote,'show',state['prs'][0]['head']['sha']+':one.py')=='value = 3'
    assert git(remote,'rev-parse',state['prs'][0]['head']['sha']+'^')==f.commit
    assert all(Path(path).read_bytes()==raw for path,raw in before.items())
    current=f.board.get_ticket(f.human,ticket['id'])['ticket']
    assert current['status']=='review' and current['claimant'] is None and current['assignee_agent_id'] is None
    assert len(f.review_turns)==1 and 'Add a boundary test' in current['outcome']
    assert state['prs'][0]['head']['sha'] in current['evidence']
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
    with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
        logical=journal.snapshot()['state']
        assert logical['phase']=='complete' and logical['used_total']['turns']==3*generations
    assert f.board.get_runtime(f.human)['active_count']==0
    replay=construct().run_once()
    assert replay['tasks'][0]['phase']=='independent_review_published' and replay['tasks'][0]['historical'] is True
    assert (state['pushes'],state['posts'])==(1,1) and len(f.review_turns)==1
