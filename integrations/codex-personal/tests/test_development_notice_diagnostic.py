"""Closed notification diagnostics on real local Board/Pool failure paths.

Existing fixtures use synthetic model/native transports; no external account,
network, CLI, service or prior owner batch is used.
"""
import errno
import json

import pytest

from blackoutburst_agents import development_pool as module
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.development_client import failure_notice
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from test_development_failure_notice import pool_case

FIELDS={'schema','status','kind','phase','category','code','errno','delivery_state','notice_code'}


def projection(phase,error):
    value=module.failure_notice_diagnostic(phase,error,failure_notice('development_context_changed'))
    assert set(value)==FIELDS and b'PRIVATE' not in canonical(value)
    return value


def diagnostic_lines(capsys):
    return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith('{')]


def queue(s,job):return s.f.base/'pool/workers'/job['worker_id']/'queue'


@pytest.mark.parametrize('error,category,code,number',[
    (BoardReadError('tool_rejected'),'board_read_error','tool_rejected',None),
    (BoardReadError('PRIVATE_CODE'),'board_read_error','unknown',None),
    (DevelopmentError('private_write_failed'),'storage_error','private_write_failed',None),
    (DevelopmentError('PRIVATE_CODE'),'storage_error','unknown',None),
    (TimeoutError('PRIVATE_LOST_ACK'),'timeout_error','timeout',None),
    (OSError(errno.ENOSPC,'PRIVATE_PATH'),'os_error','os_error',errno.ENOSPC),
    (OSError(99999,'PRIVATE_PATH'),'os_error','os_error',None),
    (RuntimeError('PRIVATE_ERROR'),'unknown_error','unknown',None),
])
def test_error_projection_is_closed_without_exception_text(error,category,code,number):
    value=projection('post_rpc',error)
    assert (value['category'],value['code'],value['errno'],value['delivery_state'])==(category,code,number,'unknown')
    assert projection('receipt_write',error)['delivery_state']=='confirmed'


def test_unknown_exception_subclasses_and_values_never_execute_hooks():
    class Hostile:
        def __str__(self):raise AssertionError('must not stringify')
        def __repr__(self):raise AssertionError('must not repr')
        def __eq__(self,other):raise AssertionError('must not compare')
    class HostileBoard(BoardReadError):
        def __getattribute__(self,name):raise AssertionError('must not inspect subclass')
        def __str__(self):raise AssertionError('must not stringify')
    class HostileOS(OSError):
        @property
        def errno(self):raise AssertionError('must not read subclass property')
    for error in (Hostile(),HostileBoard('PRIVATE'),HostileOS(1,'PRIVATE')):
        assert projection('post_rpc',error)['category']=='unknown_error'
    error=BoardReadError('safe');error.code=Hostile()
    assert projection('post_rpc',error)['code']=='unknown'
    error=DevelopmentError('safe');error.code=Hostile()
    assert projection('receipt_write',error)['code']=='unknown'
    for phase in (None,Hostile(),'before_intent'):
        assert module.failure_notice_diagnostic(phase,error,{}) is None


def test_real_pool_denied_post_has_durable_closed_reason_without_fallback(pool_case,monkeypatch,capsys):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False
    original=module.DevelopmentBoardClient.post_failure_notice
    def denied(client,**kwargs):
        f.board.set_policy(f.human,True,True,False,True,
            ['repo.read_file','repo.patch_file','repo.run_test'],f.resources)
        return original(client,**kwargs)
    monkeypatch.setattr(module.DevelopmentBoardClient,'post_failure_notice',denied)
    f.pool.dispatch_pending();result=f.wait()[0]
    path=queue(s,job);value=decode((path/'development-failure-notice-diagnostic.json').read_bytes())
    assert value['phase']=='post_rpc' and value['category']=='board_read_error'
    assert value['code']=='tool_rejected' and value['delivery_state']=='unknown'
    assert diagnostic_lines(capsys)==[value]
    assert result['state']=='retained' and result['cleanup_confirmed'] is False
    assert (path/'development-failure-notice-intent.json').is_file()
    assert not (path/'development-failure-notice-receipt.json').exists()
    assert not f.board.get_thread(f.human,data['ticket']['thread_id'])['messages']


def test_lost_ack_retains_unknown_then_explicit_same_key_retry_never_repeats_model(pool_case,monkeypatch,capsys):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False;s.lost[0]=True
    original=module._DevelopmentRun._failure_notice;first=[]
    def twice(run,code,cleaned=False):
        original(run,code,cleaned)
        first.append((queue(s,job)/'development-failure-notice-diagnostic.json').read_bytes())
        original(run,code,cleaned)
    monkeypatch.setattr(module._DevelopmentRun,'_failure_notice',twice)
    f.pool.dispatch_pending();result=f.wait()[0]
    path=queue(s,job);value=decode(first[0])
    assert value['code']=='timeout' and value['delivery_state']=='unknown'
    assert (path/'development-failure-notice-diagnostic.json').read_bytes()==first[0]
    assert (path/'development-failure-notice-receipt.json').is_file()
    assert len(s.posts)==2 and s.posts[0]==s.posts[1]
    assert len(f.calls)==1 and len(f.board.get_thread(f.human,data['ticket']['thread_id'])['messages'])==1
    assert diagnostic_lines(capsys)==[value]
    assert result['state']=='retained' and result['cleanup_confirmed'] is False


def test_conflicting_receipt_is_preserved_and_reports_only_confirmed_post(pool_case,monkeypatch,capsys):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False
    original=module.PrivateDirectory.create;raw=canonical({'private':'PRIVATE_EXISTING_RECEIPT'})
    def conflict(private,name,value):
        if name=='development-failure-notice-receipt.json':
            original(private,name,raw)
            raise DevelopmentError('private_file_exists')
        return original(private,name,value)
    monkeypatch.setattr(module.PrivateDirectory,'create',conflict)
    f.pool.dispatch_pending();result=f.wait()[0]
    path=queue(s,job);value=decode((path/'development-failure-notice-diagnostic.json').read_bytes())
    assert value['phase']=='receipt_write' and value['category']=='storage_error'
    assert value['code']=='development_notice_receipt_conflict' and value['delivery_state']=='confirmed'
    assert (path/'development-failure-notice-receipt.json').read_bytes()==raw
    assert len(s.posts)==1 and result['cleanup_confirmed'] is False
    assert diagnostic_lines(capsys)==[value]


@pytest.mark.parametrize('observer_failure',['none','storage','stdout','observer'])
def test_receipt_write_failure_never_masks_original_exception(pool_case,monkeypatch,capsys,observer_failure):
    s=pool_case;f=s.f;job,data=f.selected();original_error=DevelopmentError('development_context_changed')
    real_run=DevelopmentBoardWorker.run
    def failed(worker,operation):real_run(worker,operation);raise original_error
    monkeypatch.setattr(DevelopmentBoardWorker,'run',failed)
    create=module.PrivateDirectory.create
    def failing_create(private,name,value):
        if name=='development-failure-notice-receipt.json':raise OSError(errno.ENOSPC,'PRIVATE_PATH')
        if observer_failure=='storage' and name=='development-failure-notice-diagnostic.json':raise SystemExit('PRIVATE_OBSERVER')
        return create(private,name,value)
    monkeypatch.setattr(module.PrivateDirectory,'create',failing_create)
    if observer_failure=='stdout':
        def broken_print(*args,**kwargs):raise SystemExit('PRIVATE_STDOUT')
        monkeypatch.setattr(module,'print',broken_print,raising=False)
    if observer_failure=='observer':
        def broken_observer(*args,**kwargs):raise SystemExit('PRIVATE_OBSERVER')
        monkeypatch.setattr(module._DevelopmentRun,'_notice_diagnostic',broken_observer)
    actual_run=module._DevelopmentRun.run;seen=[]
    def observed(run):
        try:return actual_run(run)
        except BaseException as error:seen.append(error);raise
    monkeypatch.setattr(module._DevelopmentRun,'run',observed)
    f.pool.dispatch_pending();result=f.wait()[0]
    assert seen==[original_error] and seen[0] is original_error
    assert len(s.posts)==1 and result['state']=='retained'
    lines=diagnostic_lines(capsys)
    if observer_failure in ('none','storage'):
        assert len(lines)==1 and lines[0]['phase']=='receipt_write'
        assert lines[0]['code']=='os_error' and lines[0]['errno']==errno.ENOSPC and lines[0]['delivery_state']=='confirmed'
    else:assert not lines


def test_successful_notice_does_not_create_error_diagnostic(pool_case,capsys):
    s=pool_case;f=s.f;job,data=f.selected();f.cleanup=False
    f.pool.dispatch_pending();f.wait()
    assert len(s.posts)==1 and (queue(s,job)/'development-failure-notice-receipt.json').is_file()
    assert not (queue(s,job)/'development-failure-notice-diagnostic.json').exists()
    assert not diagnostic_lines(capsys)
