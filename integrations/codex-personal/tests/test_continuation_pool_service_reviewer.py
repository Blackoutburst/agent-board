"""Service -> published aggregate -> selected independent reviewer, no replay."""
import json
import os
from pathlib import Path
import threading

import pytest

pytest.importorskip('blackoutburst_agents.continuation_aggregate_review')
from blackoutburst_agents import continuation_pool_service as module
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.continuation import ContinuationTask,ContinuationChecks
from blackoutburst_agents.continuation_aggregate_review import ContinuationAggregateReviewer
from blackoutburst_agents.dev_journal import PrivateDirectory,decode
from blackoutburst_agents.dev_manifest import canonical,digest
from test_continuation_pool_service_integration import (actual_service,linux_lock_observation,
    test_actual_final_delivery_uses_original_base_and_releases_for_independent_review as final_case)

_RETAINED_TEST_HANDLES=[]


def never_open_author(*args,**kwargs):
    pytest.fail('Historical published review constructed an author generation')


def test_confirmed_review_reopen_is_history_only_and_core_stop_dominates(actual_service,monkeypatch):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    run=ContinuationAggregateReviewer.run;helpers=[]
    def observe(helper,*args,**kwargs):
        result=run(helper,*args,**kwargs);helpers.append(helper)
        monkeypatch.setattr(module._Generation,'open',never_open_author)
        return result
    monkeypatch.setattr(ContinuationAggregateReviewer,'run',observe)
    final_case(actual_service,monkeypatch,1)
    assert len(helpers)==1 and not helpers[0].pending
    key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}));directory=root/'continuations'/key
    with PrivateDirectory(directory) as private:cursor=decode(private.read('cursor.json',module.MAX_RECORD))
    assert cursor['review_receipt']['review']['delivery_sha'] in f.board.get_ticket(f.human,ticket['id'])['ticket']['evidence']
    ledger=directory/'aggregate/delivery/review-source/continuation-review.json'
    held=ledger.with_name('held-original-review.json');ledger.rename(held)
    try:
        changed=decode(held.read_bytes());changed['result']['verdict']='approve'
        ledger.write_bytes(canonical(changed));ledger.chmod(0o600)
        before=(len(f.review_calls),len(f.review_turns),calls.read_bytes())
        rejected=construct().run_once()
        assert rejected['status']=='needs_you' and rejected['tasks'][0]['reason']=='continuation_pool_binding_changed'
        assert before==(len(f.review_calls),len(f.review_turns),calls.read_bytes())
    finally:ledger.unlink();held.rename(ledger)
    assert construct().run_once()['tasks'][0]['phase']=='independent_review_published'
    before=len(f.review_calls)
    with ContinuationJournal.open(directory/'logical') as journal:
        # Stop is an ordinary durable Core event, never cleared by history.
        checks=ContinuationChecks(**{name:lambda _:None for name in ('scope','claim','source','fence','runtime','resume')})
        task=ContinuationTask(journal,checks);state=task.snapshot()['state']
        task.stop(expected_revision=state['revision'],request_id='stop-published-review-history')
    stopped=construct().run_once()
    assert stopped['tasks'][0]['phase']=='stopped' and len(helpers)==1
    assert len(f.review_calls)==before and len(calls.read_text().splitlines())==3

@pytest.mark.parametrize('fault',['revocation','stop'])
def test_review_failure_retains_pending_and_owner_lease_without_adopting_work(actual_service,monkeypatch,fault):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    run=ContinuationAggregateReviewer.run;review_helpers=[];stop=threading.Event();services=[];results=[]
    def reviewer(helper,*args,**kwargs):
        review_helpers.append(helper)
        if fault=='revocation':f.board.set_policy(f.human,True,False,True,True,f.actions,['repo:fixture'])
        return run(helper,*args,**kwargs)
    monkeypatch.setattr(ContinuationAggregateReviewer,'run',reviewer)
    if fault=='stop':
        from blackoutburst_agents import linux_review_binding
        transport=linux_review_binding.execute_turn
        def cancelled(settings,prompt,workspace,**callbacks):
            stop.set()
            return transport(settings,prompt,workspace,**callbacks)
        monkeypatch.setattr(linux_review_binding,'execute_turn',cancelled)
    class ObservedPass(BaseException):pass
    def observed_construct():
        service=construct();services.append(service);once=service.run_once
        def result_once(**kwargs):
            value=once(should_stop=stop.is_set);results.append(value);raise ObservedPass()
        service.run_once=result_once
        return service
    attempt=(f,root,task_id,ticket,observed_construct,calls,sequence)
    try:
        with pytest.raises(ObservedPass):final_case(attempt,monkeypatch,1)
        result=results[0];service=services[0]
        assert result['status']=='needs_you' and result['tasks'][0]['phase']=='needs_you'
        assert review_helpers and service._retained_lease is not None and service._retained_resources
        key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
        with PrivateDirectory(root/'continuations'/key) as private:
            cursor=decode(private.read('cursor.json',module.MAX_RECORD))
        assert cursor['phase']=='pending' and 'review_receipt' not in cursor
        assert len(calls.read_text().splitlines())==3
        count=(len(f.review_turns),len(f.review_calls))
        with pytest.raises(module.DevelopmentError,match='continuation_pool_owner_busy'):construct().run_once()
        assert count==(len(f.review_turns),len(f.review_calls))
    finally:
        # Synthetic model seams have no live process; join real reviewer threads
        # before releasing test-owned handles, without mutating any SDK receipt.
        for helper in review_helpers:
            if helper.pool is not None:
                for thread in helper.pool.threads.values():thread.join(timeout=5)
                assert not helper.pool.status()['live_threads']
        for service in services:
            # Match the process-exit contract: author SQLite handles belong to
            # the generation thread and are not closed from this test thread.
            _RETAINED_TEST_HANDLES.extend(service._retained_resources)
            if isinstance(service._retained_lease,tuple):
                owned,fd=service._retained_lease;os.close(fd);owned.close()
