"""Per-observation full-frame brackets with real readonly command plumbing."""
from contextlib import contextmanager
from contextvars import copy_context
from copy import deepcopy
import copy,json,os,pickle,subprocess,sys,threading,uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from blackoutburst_agents import development_aggregate_review_source as q
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.git_publication import GitHubDraftPublisher,API,REMOTE,REPOSITORY
from test_development_aggregate_delivery import aggregate_fixture,publisher_fixture,linux_lock_observation


def published_case(tmp_path,monkeypatch,lock,callback):
    def check(f,aggregate,worker,directory,turns):
        gh_state=f.base/'synthetic-gh-state.json'
        gh_program='#!'+str(Path(sys.executable).resolve())+' -I\n'+'''import json,sys
from pathlib import Path
args=sys.argv[1:]
if args==['auth','status','--hostname','github.com']:print('synthetic ordinary authentication');raise SystemExit(0)
assert args[:4]==['api','--hostname','github.com','--method'] and args[4]=='GET'
state=json.loads(Path(STATE).read_bytes());path=args[5]
if path=='user':value={'login':'synthetic-user'}
elif path==API:value={'full_name':REPO,'html_url':'https://github.com/'+REPO,'permissions':{'push':True},'archived':False,'disabled':False}
elif path.startswith(API+'/pulls?'):value=state['prs']
else:raise SystemExit(99)
print(json.dumps(value))
'''.replace('STATE',repr(str(gh_state))).replace('API',repr(API)).replace('REPO',repr(REPOSITORY))
        write=Path.write_text
        def approved_gh(path,data,*args,**kwargs):
            return write(path,gh_program if path.name=='approved-gh' and data=='#!/bin/sh\nexit 99\n' else data,*args,**kwargs)
        monkeypatch.setattr(Path,'write_text',approved_gh)
        seal=aggregate.seal(message='Final aggregate');aggregate.complete_local()
        publisher,public=publisher_fixture(f,aggregate,seal)
        aggregate.publish_and_handoff(publisher,title='Final aggregate',body='Final source and tests.')
        gh_state.write_bytes(canonical({'prs':public['prs']}));gh_state.chmod(0o600)
        source=q.PublishedAggregateReviewSource(aggregate,publisher)
        launches=[];popen=subprocess.Popen;run=q.PublishedAggregateReadView._run;state={'after':None}
        def local_popen(argv,*args,**kwargs):
            if str(publisher.guardian) in argv:
                command=argv[argv.index('--')+1:];launches.append(list(command))
                if command[0]==str(publisher.git):
                    assert command[1]=='ls-remote' and REMOTE in command
                    argv=[str(f.base/'remote.git') if part==REMOTE else part for part in argv]
                    env=dict(kwargs['env']);env['GIT_ALLOW_PROTOCOL']='file'
                    count=int(env['GIT_CONFIG_COUNT']);env.update(GIT_CONFIG_COUNT=str(count+1));env['GIT_CONFIG_KEY_'+str(count)]='protocol.file.allow';env['GIT_CONFIG_VALUE_'+str(count)]='always';kwargs={**kwargs,'env':env}
            return popen(argv,*args,**kwargs)
        monkeypatch.setattr(subprocess,'Popen',local_popen)
        def actual_run(reader,*args,**kwargs):
            result=run(reader,*args,**kwargs)
            if state['after'] is not None:state['after']()
            return result
        monkeypatch.setattr(q.PublishedAggregateReadView,'_run',actual_run)
        callback(f,source,public,turns,launches,state)
    aggregate_fixture(tmp_path,monkeypatch,lock,check)


def test_actual_readonly_command_path_has_two_full_checks_per_status_no_cross_status_cache(tmp_path,monkeypatch,linux_lock_observation):
    def check(f,source,public,turns,launches,state):
        full=Mock(wraps=source._verify_frame);controls=Mock(wraps=source._verify_observation_controls)
        monkeypatch.setattr(source,'_verify_frame',full);monkeypatch.setattr(source,'_verify_observation_controls',controls)
        one=source.status(source.operation_id)
        assert full.call_count==2 and controls.call_count==12 and len(launches)==5
        assert q._OBSERVATION.get() is None
        assert source.status(source.operation_id)==one
        assert full.call_count==4 and controls.call_count==24 and len(launches)==10
        assert public['posts']==public['pushes']==1 and len(turns)==3
        assert all(row[1]=='ls-remote' or row[1]=='auth' or row[1]=='api' and row[5]=='GET' for row in launches)
    published_case(tmp_path,monkeypatch,linux_lock_observation,check)


@pytest.mark.parametrize('kind',['db','native','tree','fence','git_object'])
def test_database_tree_git_object_and_fence_drift_during_get_refuse_before_result(tmp_path,monkeypatch,linux_lock_observation,kind):
    def check(f,source,public,turns,launches,state):
        final=Path(source.frame['final_root']);original=Path(source.frame['workspace']['request']['checkout'])
        objects=[p for p in (original/'.git/objects').glob('*/*') if p.is_file()];assert objects
        target={'db':Path(source.frame['databases'][0]),
            'native':next(Path(p) for p in source.frame['files'] if p.endswith('native-receipt.json')),
            'tree':final/'one.py','fence':Path(source.frame['fence_root'])/'fence.json','git_object':objects[0]}[kind]
        raw=target.read_bytes();mode=target.stat().st_mode&0o777;done=[]
        full=Mock(wraps=source._verify_frame);monkeypatch.setattr(source,'_verify_frame',full)
        def mutate():
            if not done:
                assert len(launches)==1  # Real first GET completed, not a preflight refusal.
                target.chmod(0o600);target.write_bytes(b'CORRUPTED'+raw if kind=='git_object' else raw+b'UNTRUSTED');done.append(True)
        state['after']=mutate
        try:
            with pytest.raises(DevelopmentError):source.status(source.operation_id)
            assert done and len(launches)==5 and full.call_count==2
            assert q._OBSERVATION.get() is None
        finally:state['after']=None;target.write_bytes(raw);target.chmod(mode)
        assert public['posts']==public['pushes']==1 and len(turns)==3
    # Each parameter owns a fresh real published fixture. Git ctime is never
    # "restored" or reused as authority by a later mutation scenario.
    published_case(tmp_path,monkeypatch,linux_lock_observation,check)


def test_fresh_stop_and_policy_between_gets_and_stop_during_policy_block_next_launch(tmp_path,monkeypatch,linux_lock_observation):
    def check(f,source,public,turns,launches,state):
        stop=Path(source.frame['stop_paths'][0]);original=q.DevelopmentAuthorization._policy
        for cause in ('stop_between','revoke_between','stop_during_policy'):
            launches.clear();done=[]
            def change():
                if done:return
                done.append(True)
                if cause=='revoke_between':f.board.set_policy(f.human,True,True,True,False,f.actions,['repo:fixture'])
                else:stop.write_bytes(b'{}');stop.chmod(0o600)
            state['after']=change if cause!='stop_during_policy' else None
            def policy(auth):
                result=original(auth)
                if cause=='stop_during_policy' and q._current_observation(source) is not None:change()
                return result
            with monkeypatch.context() as local:
                local.setattr(q.DevelopmentAuthorization,'_policy',policy)
                try:
                    with pytest.raises(DevelopmentError):source.status(source.operation_id)
                    assert done and len(launches)==(0 if cause=='stop_during_policy' else 1)
                    assert q._OBSERVATION.get() is None
                finally:
                    state['after']=None
                    if stop.exists():stop.unlink()
                    f.board.set_policy(f.human,True,True,True,True,f.actions,['repo:fixture'])
        assert public['posts']==public['pushes']==1 and len(turns)==3
    published_case(tmp_path,monkeypatch,linux_lock_observation,check)


def test_policy_callback_frame_reader_operation_and_context_drift_block_first_query(tmp_path,monkeypatch,linux_lock_observation):
    def check(f,source,public,turns,launches,state):
        policy=q.DevelopmentAuthorization._policy
        for cause in ('frame_and_sha','operation','reader','context'):
            launches.clear();done=[];original=(source.frame,source.frame_sha256,source.operation_id,source.reader)
            def changed(auth):
                result=policy(auth)
                if q._current_observation(source) is not None and not done:
                    done.append(True)
                    if cause=='frame_and_sha':
                        source.frame={**source.frame,'synthetic_change':True};source.frame_sha256=digest(canonical(source.frame))
                    elif cause=='operation':source.operation_id=str(uuid.uuid4())
                    elif cause=='reader':
                        other=object.__new__(q.PublishedAggregateReadView);other.__dict__.update(source.reader.__dict__);source.reader=other
                    else:
                        other=q._ReadObservation(source,q._OBSERVATION_ISSUER);done.append(other);q._OBSERVATION.set(other)
                return result
            with monkeypatch.context() as local:
                local.setattr(q.DevelopmentAuthorization,'_policy',changed)
                try:
                    with pytest.raises(DevelopmentError):source.status(source.operation_id)
                    assert done and not launches and q._OBSERVATION.get() is None
                finally:
                    source.frame,source.frame_sha256,source.operation_id,source.reader=original
                    for value in done:
                        if type(value) is q._ReadObservation:value.close()
        assert public['posts']==public['pushes']==1 and len(turns)==3
    published_case(tmp_path,monkeypatch,linux_lock_observation,check)


def thin_source():
    source=object.__new__(q.PublishedAggregateReviewSource);reader=object.__new__(q.PublishedAggregateReadView)
    source._issuer=q._ISSUER;source.reader=reader;source.operation_id=str(uuid.uuid4());source.directory=Path('/synthetic-source')
    source.frame={'authority_pin':{},'publication_request':{}};source.frame_sha256=digest(canonical(source.frame));source.receipt={'synthetic':True}
    source._verify_frame=Mock();source._verify_observation_controls=Mock()
    reader.source=source;reader.root=Path('/synthetic-reader');reader.pins={};reader.grant={};reader._pins=Mock(return_value={});reader._grant=SimpleNamespace(document=lambda:{})
    source.grant={};reader.authority_pin={};reader.request={}
    return source


def test_forged_stale_foreign_thread_and_foreign_source_never_skip_full_check():
    source=thin_source();reader=source.reader;foreign=thin_source()
    forged=object.__new__(q._ReadObservation)
    for value in ({'active':True},forged):
        token=q._OBSERVATION.set(value)
        try:reader._roots()
        finally:q._OBSERVATION.reset(token)
    observation=q._ReadObservation(source,q._OBSERVATION_ISSUER);token=q._OBSERVATION.set(observation)
    try:
        context=copy_context();thread=threading.Thread(target=lambda:context.run(reader._roots));thread.start();thread.join(timeout=3);assert not thread.is_alive()
        foreign.reader._roots()
        observation.close();reader._roots()
    finally:observation.close();q._OBSERVATION.reset(token)
    assert source._verify_frame.call_count==4 and foreign._verify_frame.call_count==1
    source._verify_observation_controls.assert_not_called();foreign._verify_observation_controls.assert_not_called()


class Abort(BaseException):pass


def test_exception_reset_expired_tokens_and_serialization_cannot_reuse_window(monkeypatch):
    source=thin_source();captured=[]
    @contextmanager
    def record(*a,**k):yield SimpleNamespace(read=lambda *a:canonical({'receipt':source.receipt,'frame_sha256':source.frame_sha256}))
    monkeypatch.setattr(q,'_existing_record',record)
    for error in (ValueError('synthetic'),Abort()):
        def interrupted():captured.append(q._OBSERVATION.get());raise error
        source._published_observation=interrupted;previous={'outer':'not-authority'};token=q._OBSERVATION.set(previous)
        try:
            with pytest.raises(type(error)):source.status(source.operation_id)
            assert q._OBSERVATION.get() is previous
        finally:q._OBSERVATION.reset(token)
        old=captured[-1];assert not old.active and old not in q._ISSUED_OBSERVATIONS
        for operation in (lambda:copy.copy(old),lambda:deepcopy(old),lambda:pickle.dumps(old)):
            with pytest.raises(TypeError):operation()
        token=q._OBSERVATION.set(old)
        try:source.reader._roots()
        finally:q._OBSERVATION.reset(token)
    assert source._verify_frame.call_count==4


def test_concurrent_contexts_are_local_and_original_binding_drift_refuses(monkeypatch):
    sources=[thin_source(),thin_source()];barrier=threading.Barrier(2);seen=[]
    def use(source):
        window=q._ReadObservation(source,q._OBSERVATION_ISSUER);token=q._OBSERVATION.set(window)
        try:barrier.wait(timeout=3);seen.append(q._current_observation(source) is window);seen.append(q._current_observation(sources[1-sources.index(source)]) is None)
        finally:window.close();q._OBSERVATION.reset(token)
    threads=[threading.Thread(target=use,args=(source,)) for source in sources]
    for thread in threads:thread.start()
    for thread in threads:thread.join(timeout=3);assert not thread.is_alive()
    assert seen==[True]*4 and q._OBSERVATION.get() is None
    source=thin_source();window=q._ReadObservation(source,q._OBSERVATION_ISSUER);token=q._OBSERVATION.set(window)
    try:
        # Invoke the real local control entry: original operation pins must
        # refuse before policy or private source reads.
        source.operation_id=str(uuid.uuid4())
        with pytest.raises(DevelopmentError):q.PublishedAggregateReviewSource._verify_observation_controls(source)
    finally:window.close();q._OBSERVATION.reset(token)
