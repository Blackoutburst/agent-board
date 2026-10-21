"""Real local flock contention; no CLI, native namespace, network or model."""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import os
from pathlib import Path
import threading
import time
import traceback
from types import SimpleNamespace
import uuid

import pytest

from blackoutburst_agents import dev_fence as fences
from blackoutburst_agents.dev_manifest import DevelopmentError


def binding(root,index=0):
    directory=root/('checkout-'+str(index));directory.mkdir(mode=0o700)
    info=directory.stat();pin={'path':str(directory),'identity':[info.st_dev,info.st_ino]}
    return {'scope':dict(owner_id='owner',agent_id='agent-'+str(index),project_id='project',
            ticket_id='ticket-'+str(index),board_session_id='session-'+str(index),board_endpoint='https://board.test/mcp'),
        'root_identity':pin['identity'],'journal':pin,'configuration_hash':'a'*64,
        'child_scope':{'work_id':str(uuid.uuid4()),'runs':pin,'runtime':pin}}


@contextmanager
def holding(fence,after=lambda:None):
    ready,release=threading.Event(),threading.Event();errors=[]
    def hold():
        try:
            with fence.lock():
                ready.set()
                assert release.wait(3)
                after()
        except BaseException as error:errors.append(error);ready.set()
    thread=threading.Thread(target=hold);thread.start();assert ready.wait(2)
    try:yield release
    finally:
        release.set();thread.join(3)
        assert not thread.is_alive() and not errors,errors


def delayed_release(release):
    timer=threading.Timer(.03,release.set);timer.start();return timer


def test_four_distinct_checkouts_share_metadata_without_losing_bindings(tmp_path,monkeypatch):
    requests=[(binding(tmp_path,i),str(uuid.uuid4())) for i in range(4)]
    barrier=threading.Barrier(4);results=[];errors=[]
    read=fences.DevelopmentFence._read
    def slow_read(self):
        time.sleep(.002)
        return read(self)
    monkeypatch.setattr(fences.DevelopmentFence,'_read',slow_read)
    def run(request):
        fence=None
        try:
            with fences.standalone_fence_wait():
                fence=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
                fence.bind(*request);barrier.wait(2)
                for _ in range(10):fence.verify(*request)
                results.append(fences.fence_wait_revision())
        except BaseException:errors.append(traceback.format_exc())
        finally:
            if fence:fence.close()
    threads=[threading.Thread(target=run,args=(request,)) for request in requests]
    for thread in threads:thread.start()
    for thread in threads:thread.join(5)
    if errors:pytest.fail('\n'.join(errors))
    assert all(not thread.is_alive() for thread in threads)
    assert len(results)==4 and any(results)
    fence=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    try:
        for request in requests:fence.verify(*request)
    finally:fence.close()


def test_opt_in_is_thread_local_and_default_still_refuses(tmp_path):
    owner=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    other=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    try:
        with holding(owner):
            with pytest.raises(DevelopmentError,match='fence_busy'):
                with other.lock():pass
            errors=[]
            with fences.standalone_fence_wait():
                def child():
                    try:
                        with other.lock():pass
                    except DevelopmentError as error:errors.append(error.code)
                thread=threading.Thread(target=child);thread.start();thread.join(1)
                assert errors==['fence_busy'] and not thread.is_alive()
                with pytest.raises(DevelopmentError,match='fence_wait_context_invalid'):
                    with fences.standalone_fence_wait():pass
        with other.lock():pass
    finally:owner.close();other.close()


def test_unknown_raw_flock_remains_nonblocking_even_with_opt_in(tmp_path):
    fence=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    raw=os.open(tmp_path/'anchor/fence.lock',os.O_RDWR)
    try:
        fcntl.flock(raw,fcntl.LOCK_EX|fcntl.LOCK_NB)
        revision=fences.fence_wait_revision()
        with fences.standalone_fence_wait(),pytest.raises(DevelopmentError,match='fence_busy'):
            with fence.lock():pass
        assert fences.fence_wait_revision()==revision
    finally:os.close(raw);fence.close()


def test_timeout_and_changed_lock_never_adopt_or_retry(tmp_path,monkeypatch):
    owner=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    other=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    try:
        monkeypatch.setattr(fences,'FENCE_WAIT_SECONDS',.02)
        with holding(owner),fences.standalone_fence_wait(),pytest.raises(DevelopmentError,match='fence_busy'):
            with other.lock():pass
        with holding(owner) as release:
            path=tmp_path/'anchor/fence.lock';path.rename(path.with_name('held-old-lock'))
            path.write_bytes(b'');path.chmod(0o600)
            timer=delayed_release(release)
            monkeypatch.setattr(fences,'FENCE_WAIT_SECONDS',.2)
            with fences.standalone_fence_wait(),pytest.raises(DevelopmentError,match='fence_lock_changed'):
                with other.lock():pass
            timer.join()
        assert path.exists() and path.with_name('held-old-lock').exists()
    finally:owner.close();other.close()


def test_opt_in_never_releases_same_checkout_or_enters_family_api(tmp_path):
    fence=fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    request=binding(tmp_path);operation=str(uuid.uuid4())
    try:
        fence.bind(request,operation)
        other=deepcopy(request);other['scope']['ticket_id']='different'
        with fences.standalone_fence_wait():
            with pytest.raises(DevelopmentError,match='canonical_checkout_busy'):fence.bind(other,str(uuid.uuid4()))
            for call in (lambda:fence.reserve_family({},{}),lambda:fence.verify_family({},{},{}),lambda:fence.transfer_family_slot({})):
                with pytest.raises(DevelopmentError,match='fence_wait_family_forbidden'):call()
        assert fence.inspect_checkout(request['root_identity'])['operation_id']==operation
    finally:fence.close()


@pytest.fixture
def planner_fixture():
    from test_dev_planner import PlannerTests
    from blackoutburst_agents.development_authorization import DevelopmentAuthorization
    from blackoutburst_agents.linux_test_backend import SupervisedLinuxNamedPythonBackend
    fixture=PlannerTests();fixture.setUp();planner=fixture.create()
    authority=object.__new__(DevelopmentAuthorization)
    authority.manifest=planner.broker.manifest;authority.claim_id=fixture.grant['claim_id']
    authority.claim_identity=lambda _:authority.claim_id
    calls=[]
    def snapshot(**kwargs):
        calls.append(None)
        return {'ticket':{},'policy':deepcopy(fixture.grant)}
    authority.snapshot=snapshot
    planner.authorization=authority;planner._base_authorize=authority.allowed
    planner.broker.authorize_scope=planner._broker_authorize
    backend=object.__new__(SupervisedLinuxNamedPythonBackend)
    backend.manifest=planner.broker.manifest;backend.authorize_scope=authority.allowed
    planner.broker.test_backend=backend
    owner=fences.DevelopmentFence(_test_anchor=fixture.base/'anchor')
    try:yield fixture,planner,authority,calls,owner
    finally:owner.close();fixture.doCleanups()


@pytest.mark.parametrize('failure',['revoke','stop','expiry'])
def test_wait_after_grant_rechecks_before_any_effect(planner_fixture,failure):
    fixture,planner,authority,calls,owner=planner_fixture
    original=authority.snapshot;holders=[];timers=[];deadline=planner.journal.read()['deadline']
    def change():
        if failure=='revoke':fixture.grant['actions']=[]
        elif failure=='stop':planner.journal.request_stop()
        else:fixture.clock=deadline
    def snapshot(**kwargs):
        result=original(**kwargs)
        if len(calls)==1:
            holder=holding(owner,change);release=holder.__enter__();holders.append(holder)
            timers.append(delayed_release(release))
        return result
    authority.snapshot=snapshot
    try:
        with fences.standalone_fence_wait(),pytest.raises(DevelopmentError):
            planner._fresh(planner.journal.read())
        assert len(calls)==(2 if failure=='revoke' else 1)
        assert fixture.control.calls==[] and fixture.transports==[]
    finally:
        for timer in timers:timer.join()
        for holder in holders:holder.__exit__(None,None,None)


def test_grant_refresh_is_bounded_without_repeating_effects(monkeypatch):
    from blackoutburst_agents.dev_planner import DevelopmentPlanner
    calls=[]
    def local():fences._WAIT_CONTEXT.revision=fences.fence_wait_revision()+1
    with pytest.raises(DevelopmentError,match='fence_busy'):
        DevelopmentPlanner._fresh_after_fence_wait(lambda:calls.append(None),local)
    assert len(calls)==3


def test_optimized_capture_completion_refreshes_after_real_wait(planner_fixture):
    fixture,planner,authority,calls,owner=planner_fixture
    state=planner.journal.read();token=planner._linux_capture_fresh(state)
    assert token[0] is True and len(calls)==1
    with holding(owner,lambda:fixture.grant.update(actions=[])) as release:
        timer=delayed_release(release)
        with fences.standalone_fence_wait(),pytest.raises(DevelopmentError,match='planner_authorization_changed'):
            planner._linux_capture_complete(state,token)
        timer.join()
    assert len(calls)==2 and fixture.control.calls==[]


@pytest.mark.parametrize('failure',['none','revoke','ttl'])
def test_late_snapshot_metadata_wait_refreshes_only_grants(planner_fixture,failure):
    from blackoutburst_agents.linux_input_snapshot import LinuxInputSnapshot
    from blackoutburst_agents.linux_test_backend import _ExecutionCapturePermit,LinuxExecutionContext
    from blackoutburst_agents.dev_test_supervisor import _fresh_control_window
    fixture,planner,authority,calls,owner=planner_fixture
    context=object.__new__(LinuxExecutionContext);context.planner=planner
    context.state=planner.journal.read();context._check=lambda:True
    permit=object.__new__(_ExecutionCapturePermit);permit.context=context;permit.broker=planner.broker;permit.count=5
    snapshot=object.__new__(LinuxInputSnapshot);snapshot._broker=planner.broker;snapshot._capture_permit=permit
    snapshot._held_metadata=lambda:planner.fence.verify(planner.binding,planner.operation_id)
    original=planner.broker.journal._linux_execution_profile
    planner.broker.journal._linux_execution_profile=True
    try:
        token=planner._linux_capture_fresh(context.state);assert token[0] is True
        def change():
            if failure=='revoke':fixture.grant['actions']=[]
            elif failure=='ttl':fixture.clock+=1.5
        with holding(owner,change) as release:
            timer=delayed_release(release)
            with fences.standalone_fence_wait():
                if failure=='none':
                    window=_fresh_control_window(lambda:snapshot._held(),nonce='a'*32,sequence=1,clock=lambda:fixture.clock)
                    assert window['started_at']==100. and window['deadline']==101.5
                else:
                    with pytest.raises(DevelopmentError):
                        _fresh_control_window(lambda:snapshot._held(),nonce='a'*32,sequence=1,clock=lambda:fixture.clock)
            timer.join()
        assert len(calls)==2 and permit.count==5
        assert fixture.control.calls==[] and fixture.transports==[]
    finally:planner.broker.journal._linux_execution_profile=original
