"""Local threads and real private journals only; no model, Board or network."""
import json
import threading
import time
from unittest.mock import patch

import pytest

from blackoutburst_agents.pool_journal import PoolError
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.worker_pool import WorkerPool, PoolDriverBinding, PoolOutcome
from blackoutburst_agents.work_journal import WorkJournal
from blackoutburst_agents.queue_journal import QueueJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal


def eventually(predicate, timeout=3):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(0.002)
    assert predicate()


class Harness:
    def __init__(self):
        self.contexts=[];self.identities=set();self.lock=threading.Lock()
        self.run_calls=[];self.closed=set();self.reconcile_calls=[];self.verify_calls=[]
        self.behavior=lambda context:PoolOutcome(context.operation_id,context.session_id,'complete',True)
        self.verified=True;self.factory_hook=None

    def factory(self, context):
        with self.lock:
            self.contexts.append(context);self.identities.add(threading.get_ident())
        # The actual worker thread owns every SQLite connection and closes it.
        journals=[WorkJournal(context.journals['work']),QueueJournal(context.journals['queue']),
                  RunStore(context.journals['runs']),RuntimeJournal(context.journals['runtime'])]
        if self.factory_hook:self.factory_hook(context)
        harness=self
        class Driver:
            def run(self):
                harness.run_calls.append(context.operation_id)
                scope={'owner_id':'synthetic-owner','agent_id':context.worker_id,'project_id':'synthetic-project',
                       'ticket_id':'synthetic-ticket','board_session_id':context.session_id,
                       'board_endpoint':'https://synthetic.invalid/mcp'}
                with journals[0].lock(scope):
                    journals[0].begin(scope,context.operation_id,{'kind':'synthetic_pool_test'},
                                      operation_id=context.operation_id)
                return harness.behavior(context)
            def close(self):
                for journal in journals:journal.close()
                harness.closed.add(context.operation_id)
        return Driver()

    def reconcile(self, context):
        self.reconcile_calls.append(context.operation_id)
        return PoolOutcome(context.operation_id,context.session_id,'cancelled',True)

    def verify(self, context, outcome):
        self.verify_calls.append(context.operation_id)
        return self.verified

    def binding(self, revision='v1'):
        return PoolDriverBinding(revision,self.factory,self.reconcile,self.verify)


@pytest.fixture
def harness():return Harness()


@pytest.fixture
def pools(tmp_path):
    opened=[]
    def create(harness=None, **kwargs):
        pool=WorkerPool(tmp_path/'pool',drivers={'synthetic':harness.binding()} if harness else {},**kwargs)
        opened.append(pool);return pool
    yield create
    for pool in reversed(opened):
        if not pool.closed:
            pool.shutdown(3);pool.close()


def submit(pool, number):
    return pool.submit(request_id='request-'+str(number),driver_id='synthetic',payload={'ticket':'synthetic-'+str(number)})


def test_no_approved_driver_is_a_refusal_not_a_cloud_execution(pools):
    pool=pools()
    with pytest.raises(PoolError,match='driver_unavailable'):submit(pool,1)
    assert pool.dispatch_pending()==[]
    status=pool.status()
    assert (status['configured'],status['live_threads'],status['driver_calls'],status['executing_reported'],
            status['capacity_held'],status['retained'])==(1,0,0,0,0,0)


def test_four_real_threads_have_distinct_sessions_journals_and_fast_cooperative_stop(pools,harness):
    entered,executing=threading.Barrier(5),threading.Barrier(5)
    report=threading.Event()
    def run(context):
        entered.wait(3);assert report.wait(3)
        context.report_execution(True);executing.wait(3)
        while not context.should_stop():time.sleep(0.002)
        return PoolOutcome(context.operation_id,context.session_id,'cancelled',True)
    harness.behavior=run
    pool=pools(harness,concurrency=4,ceiling=4)
    for index in range(5):submit(pool,index)
    assert len(pool.dispatch_pending())==4
    entered.wait(3)
    status=pool.status()
    assert (status['configured'],status['live_threads'],status['driver_calls'],status['executing_reported'],
            status['capacity_held'],status['queued'])==(4,4,4,0,4,1)
    assert len(harness.identities)==4
    identities={value for context in harness.contexts for value in
                (context.operation_id,context.worker_id,context.session_id)}
    paths={str(path) for context in harness.contexts for path in context.journals.values()}
    assert len(identities)==12 and len(paths)==16
    report.set();executing.wait(3)
    assert pool.status()['executing_reported']==4
    started=time.monotonic();pool.stop();assert time.monotonic()-started<0.3
    eventually(lambda:pool.status()['live_threads']==0)
    status=pool.status()
    assert status['capacity_held']==status['retained']==status['executing_reported']==0
    cancelled=[job for job in status['jobs'] if job['state']=='cancelled']
    assert len(cancelled)==1 and cancelled[0]['cleanup_confirmed'] is None
    assert len(harness.run_calls)==4 and len(harness.closed)==4


def test_idempotency_and_persisted_distinct_identity_precede_driver_dispatch(pools,harness):
    pool=pools(harness)
    first=submit(pool,1);assert submit(pool,1)==first
    with pytest.raises(PoolError,match='pool_request_conflict'):
        pool.submit(request_id='request-1',driver_id='synthetic',payload={'changed':True})
    def inspect(context):
        status=pool.status();job=status['jobs'][0]
        assert job['id']==context.operation_id and job['session_id']==context.session_id
        assert job['state']=='running' and status['capacity_held']==1
    harness.factory_hook=inspect
    pool.dispatch_pending();eventually(lambda:pool.status()['live_threads']==0)
    assert pool.status()['jobs'][0]['state']=='settled'
    assert pool.dispatch_pending()==[] and len(harness.run_calls)==1


def test_cleanup_false_and_dead_thread_keep_capacity_across_reopen(pools,harness):
    harness.behavior=lambda context:PoolOutcome(context.operation_id,context.session_id,'needs_review',False)
    pool=pools(harness,concurrency=1,ceiling=2)
    first=submit(pool,1);submit(pool,2)
    pool.dispatch_pending();eventually(lambda:pool.status()['live_threads']==0)
    status=pool.status()
    assert (status['capacity_held'],status['retained'],status['execution_unknown'],status['queued'])==(1,1,1,1)
    assert status['jobs'][0]['cleanup_confirmed'] is False
    assert pool.dispatch_pending()==[]
    pool.close();reopened=pools(harness)
    assert reopened.status()['jobs'][0]['session_id']==first['session_id']
    assert reopened.dispatch_pending()==[] and len(harness.run_calls)==1
    harness.verified=False
    assert reopened.reconcile(first['id'])['state']=='retained'
    assert reopened.status()['capacity_held']==1
    harness.verified=True
    assert reopened.reconcile(first['id'])['state']=='settled'
    assert len(harness.run_calls)==1 and len(harness.reconcile_calls)==2


def test_exception_is_ambiguous_and_never_replayed(pools,harness):
    def fail(context):
        context.report_execution(True)
        raise TimeoutError('PRIVATE_DIAGNOSTIC')
    harness.behavior=fail
    pool=pools(harness)
    first=submit(pool,1);pool.dispatch_pending()
    eventually(lambda:pool.status()['live_threads']==0)
    assert pool.status()['retained']==pool.status()['capacity_held']==1
    assert 'PRIVATE_DIAGNOSTIC' not in json.dumps(pool.status())
    pool.close();reopened=pools(harness)
    assert reopened.dispatch_pending()==[] and len(harness.run_calls)==1
    reopened.reconcile(first['id'])
    assert reopened.status()['capacity_held']==0 and len(harness.run_calls)==1


def test_thread_start_failure_retains_intent_without_claiming_cleanup(pools,harness):
    pool=pools(harness)
    first=submit(pool,1)
    with patch.object(threading.Thread,'start',side_effect=RuntimeError('synthetic')):
        assert pool.dispatch_pending()==[]
    status=pool.status()
    assert status['live_threads']==0 and status['capacity_held']==status['retained']==1
    assert status['jobs'][0]['cleanup_confirmed'] is None and not harness.contexts
    assert pool.reconcile(first['id'])['state']=='settled'
    assert not harness.contexts and harness.reconcile_calls==[first['id']]


def test_recovery_of_durable_dispatch_intent_is_retained_not_relaunched(pools,harness):
    pool=pools(harness)
    first=submit(pool,1)
    def interrupted(state):state['jobs'][0].update(state='dispatching',revision=2)
    pool.journal.change(interrupted);pool.close()
    reopened=pools(harness)
    status=reopened.status();job=status['jobs'][0]
    assert (job['state'],job['reason'],job['cleanup_confirmed'])==('retained','owner_restarted',None)
    assert job['id']==first['id'] and job['session_id']==first['session_id']
    assert reopened.dispatch_pending()==[] and not harness.contexts


def test_one_controller_lease_and_immutable_driver_revision(pools,harness):
    pool=pools(harness);first=submit(pool,1)
    with pytest.raises(PoolError,match='pool_owner_unavailable'):pools(harness)
    assert pool.status()['jobs'][0]['id']==first['id']
    pool.close()
    binding=harness.binding
    harness.binding=lambda:harness.__class__.binding(harness,'v2')
    reopened=pools(harness)
    assert reopened.dispatch_pending()==[]
    assert reopened.status()['jobs'][0]['reason']=='driver_changed'
    harness.binding=binding


def test_concurrency_change_is_bounded_persistent_and_does_not_release_retained(pools,harness):
    harness.behavior=lambda context:PoolOutcome(context.operation_id,context.session_id,'blocked',False)
    pool=pools(harness,concurrency=2,ceiling=2)
    for index in range(3):submit(pool,index)
    pool.dispatch_pending();eventually(lambda:pool.status()['live_threads']==0)
    assert pool.status()['capacity_held']==2
    pool.set_concurrency(1)
    assert pool.dispatch_pending()==[] and pool.status()['capacity_held']==2
    for invalid in (0,3,True,1.5):
        with pytest.raises(PoolError,match='invalid_pool_concurrency'):pool.set_concurrency(invalid)
    pool.close();reopened=pools(harness)
    assert (reopened.status()['configured'],reopened.status()['ceiling'])==(1,2)


def test_stop_during_blocked_factory_does_not_call_run_and_keeps_uncertainty(pools,harness):
    entered,release=threading.Event(),threading.Event()
    def factory(context):entered.set();assert release.wait(3)
    harness.factory_hook=factory
    pool=pools(harness);first=submit(pool,1);pool.dispatch_pending();assert entered.wait(3)
    assert pool.status()['live_threads']==1 and pool.status()['driver_calls']==0
    started=time.monotonic();pool.stop(first['id']);assert time.monotonic()-started<0.3
    with pytest.raises(PoolError,match='pool_workers_alive'):pool.close()
    release.set();eventually(lambda:pool.status()['live_threads']==0)
    assert not harness.run_calls and pool.status()['retained']==1
    with pytest.raises(PoolError,match='pool_cleanup_pending'):pool.resume_admissions()
    pool.reconcile(first['id']);assert pool.status()['capacity_held']==0


@pytest.mark.parametrize('verified',[False,1,{'confirmed':True}])
def test_cleanup_boolean_is_insufficient_without_fresh_strict_driver_proof(pools,harness,verified):
    harness.verified=verified
    pool=pools(harness);submit(pool,1);pool.dispatch_pending()
    eventually(lambda:pool.status()['live_threads']==0)
    assert pool.status()['capacity_held']==pool.status()['retained']==1
    assert pool.status()['jobs'][0]['cleanup_confirmed'] is None


@pytest.mark.parametrize('field,value',[('session_id','wrong'),('operation_id','wrong'),('status','done')])
def test_driver_outcome_must_correlate_exact_operation_and_session(pools,harness,field,value):
    def outcome(context):
        values={'operation_id':context.operation_id,'session_id':context.session_id,
                'status':'complete','cleanup_confirmed':True};values[field]=value
        return PoolOutcome(**values)
    harness.behavior=outcome
    pool=pools(harness);submit(pool,1);pool.dispatch_pending()
    eventually(lambda:pool.status()['live_threads']==0)
    assert pool.status()['retained']==1 and not harness.verify_calls


def test_stop_is_durable_and_never_fabricates_cleanup_for_queued_work(pools,harness):
    pool=pools(harness);submit(pool,1);pool.stop()
    status=pool.status();assert status['stopped'] is True
    assert status['jobs'][0]['state']=='cancelled' and status['jobs'][0]['cleanup_confirmed'] is None
    pool.close();reopened=pools(harness)
    assert reopened.status()['stopped'] and reopened.dispatch_pending()==[]
    with pytest.raises(PoolError,match='pool_stopped'):submit(reopened,2)
    reopened.resume_admissions();submit(reopened,2)
    assert reopened.status()['queued']==1 and not harness.contexts


def test_pool_payload_is_bounded_private_data_and_journal_modes_are_private(pools,harness):
    pool=pools(harness)
    for payload in ({'token':'secret'},{'text':'x'*4097}):
        with pytest.raises(PoolError,match='invalid_pool_payload'):
            pool.submit(request_id='bad',driver_id='synthetic',payload=payload)
    assert pool.journal.private.path.stat().st_mode&0o777==0o700
    assert (pool.journal.private.path/'pool-state.json').stat().st_mode&0o777==0o600
    assert pool.status()['jobs']==[]


def test_stop_does_not_wait_for_thread_start_and_close_cannot_transfer_live_intent(pools,harness):
    pool=pools(harness);submit(pool,1)
    pending,release=threading.Event(),threading.Event()
    original=threading.Thread.start
    def gated_start(thread):
        if thread.name.startswith('pool-'):
            pending.set();assert release.wait(3)
        return original(thread)
    with patch.object(threading.Thread,'start',gated_start):
        dispatcher=threading.Thread(target=pool.dispatch_pending)
        dispatcher.start();assert pending.wait(3)
        assert pool.status()['capacity_held']==1 and pool.status()['live_threads']==0
        started=time.monotonic();pool.stop();assert time.monotonic()-started<0.3
        with pytest.raises(PoolError,match='pool_workers_alive'):pool.close()
        release.set();dispatcher.join(3)
    eventually(lambda:pool.status()['live_threads']==0)
    assert pool.status()['capacity_held']==0 and not harness.contexts


def test_failed_stop_persistence_still_cancels_cooperatively_and_fences_admission(pools,harness):
    entered=threading.Event()
    def run(context):
        entered.set()
        while not context.should_stop():time.sleep(0.002)
        return PoolOutcome(context.operation_id,context.session_id,'cancelled',False)
    harness.behavior=run
    pool=pools(harness);submit(pool,1);pool.dispatch_pending();assert entered.wait(3)
    with patch.object(pool.journal,'change',side_effect=PoolError('synthetic_disk_failure')):
        with pytest.raises(PoolError,match='pool_stop_unconfirmed'):pool.stop()
    eventually(lambda:pool.status()['live_threads']==0)
    status=pool.status()
    assert status['stopped'] and status['stop_unconfirmed'] and not status['stop_persisted']
    assert status['capacity_held']==status['retained']==1
    assert pool.dispatch_pending()==[]
    with pytest.raises(PoolError,match='pool_stopped'):submit(pool,2)


def test_concurrent_scheduling_passes_never_exceed_capacity(pools,harness):
    barrier=threading.Barrier(5)
    def run(context):
        barrier.wait(3)
        while not context.should_stop():time.sleep(0.002)
        return PoolOutcome(context.operation_id,context.session_id,'cancelled',True)
    harness.behavior=run
    pool=pools(harness,concurrency=4)
    for index in range(8):submit(pool,index)
    schedule=threading.Barrier(3);results=[]
    def dispatch():schedule.wait(3);results.extend(pool.dispatch_pending())
    dispatchers=[threading.Thread(target=dispatch) for _ in range(2)]
    for dispatcher in dispatchers:dispatcher.start()
    schedule.wait(3)
    for dispatcher in dispatchers:dispatcher.join(3)
    barrier.wait(3)
    assert len(results)==len(set(results))==4
    assert pool.status()['capacity_held']==pool.status()['live_threads']==4
    pool.shutdown(3)


def test_corrupt_durable_state_is_not_reset_on_reopen(pools,harness):
    pool=pools(harness);submit(pool,1)
    path=pool.journal.private.path/'pool-state.json'
    pool.close();path.write_bytes(b'{')
    with pytest.raises(PoolError,match='pool_journal_unavailable'):pools(harness)
    assert path.read_bytes()==b'{'


@pytest.mark.parametrize('settings',[
    {'concurrency':True},{'concurrency':1.0},{'ceiling':True},{'ceiling':1.0},
])
def test_reopen_validates_supplied_types_before_numeric_equality(tmp_path,settings):
    root=tmp_path/'pool'
    pool=WorkerPool(root,concurrency=1,ceiling=1);pool.close()
    before=(root/'pool-state.json').read_bytes()
    with pytest.raises(PoolError,match='invalid_pool_concurrency'):
        WorkerPool(root,**settings)
    assert (root/'pool-state.json').read_bytes()==before
    reopened=WorkerPool(root)
    try:assert reopened.status()['configured']==reopened.status()['ceiling']==1
    finally:reopened.close()


def test_failed_workers_directory_initialization_releases_lease_and_keeps_original_error(tmp_path):
    root=tmp_path/'pool';root.mkdir(mode=0o700)
    foreign=tmp_path/'foreign';foreign.mkdir(mode=0o700)
    (root/'workers').symlink_to(foreign,target_is_directory=True)
    failed=WorkerPool.__new__(WorkerPool)
    with pytest.raises(DevelopmentError):failed.__init__(root)
    assert failed.closed and failed.journal.fd is None and failed.journal.private.fd is None
    (root/'workers').unlink()
    reopened=WorkerPool(root)
    try:assert reopened.status()['capacity_held']==0
    finally:reopened.close()
