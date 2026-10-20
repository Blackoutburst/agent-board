"""Real temporary message stack and SQLite; no provider or network transport."""
from copy import deepcopy
import json
import sqlite3
from unittest.mock import patch

import pytest

import test_message_host as host_fixture
from board import BoardError
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.message_runtime import MessageRuntimeClient
from blackoutburst_agents.runtime_admission import refusal_arguments,validate_refusal
from blackoutburst_agents.runtime_guard import RuntimeAdmissionRefused,RuntimeControlError
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.message_runtime import MessageRuntimeGuard
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.work_journal import JournalError


class MemoryControl(MessageRuntimeClient):
    """The real SDK argument/parser path; only _tool is in-process Board."""
    def __init__(self,fixture):
        self.fixture=fixture
        self.calls=[];self.arguments=[];self.fault=None;self.mutate=None
        super().__init__({'mcp_url':fixture.client.endpoint,'token':'SYNTHETIC_ONLY',
            'client':'codex','identity_key':'fixture','name':'Fixture'},fixture.client.session_id,3)

    def _initialize(self):pass

    def _tool(self,name,args):
        self.calls.append(name);self.arguments.append(deepcopy(args))
        if self.fault=='before' and name=='reserve_message_execution':
            self.fault=None;raise BoardReadError('timeout')
        values=dict(args);values.pop('session_id')
        if self.fault=='legacy' and name=='reserve_message_execution':
            values.pop('refusal_mode');self.fault=None
        try:result=getattr(self.fixture.board,name)(self.fixture.client.actor,**values)
        except BoardError:raise BoardReadError('tool_rejected') from None
        if self.fault=='after' and name=='reserve_message_execution':
            self.fault=None;raise BoardReadError('timeout')
        if self.mutate and name=='reserve_message_execution':result=self.mutate(deepcopy(result))
        return result


@pytest.fixture
def managed():
    value=host_fixture.MessageHostTests('test_general_reply_without_ticket_or_claim_is_managed_and_exact')
    value.setUp()
    def forbidden(*args,**kwargs):
        value.calls+=1
        raise AssertionError('NO_PROVIDER_TRANSPORT')
    value.transport=forbidden
    value.control=MemoryControl(value)
    try:yield value
    finally:value.doCleanups()


def occupy(value):
    value.board.set_policy(value.human,True,True,True,True,[],[])
    ticket=value.board.claim_ticket(value.client.actor,value.ticket['id'],value.ticket['version'])
    row=value.board.reserve_execution(value.client.actor,'old',value.project['id'],ticket['id'],
        'old-work','old-turn','codex_chatgpt','fixture-model')['execution']
    value.board.start_execution(value.client.actor,row['id'],row['revision'])
    value.board.set_policy(value.human,True,True,True,False,[],[])
    with value.board._connection() as db:
        value.old=tuple(db.execute('SELECT * FROM runtime_executions WHERE id=?',(row['id'],)).fetchone())
    value.old_id=row['id']


def increase(value,maximum=2):
    limits=value.board.get_runtime(value.human)['limits']
    value.board.update_runtime_limits(value.human,limits['version'],maximum,False)


def local(value):
    op=value.operation()
    turn=value.message_runs._db.execute('SELECT * FROM turns WHERE work_id=?',(op['work_id'],)).fetchone()
    runtime=value.message_runtime.decode(value.message_runtime.db.execute('SELECT * FROM executions').fetchone())
    return op,turn,runtime


def assert_untouched(value):
    assert value.calls==0 and value.replies()==[]
    with value.board._connection() as db:
        assert value.old==tuple(db.execute('SELECT * FROM runtime_executions WHERE id=?',(value.old_id,)).fetchone())


def test_confirmed_capacity_is_terminal_local_without_fake_finish_or_refund(managed):
    value=managed;occupy(value);value.post();value.enable()
    result=value.tick_host()
    assert (result['status'],result['reason'],result['remaining_dispatches'])==(
        'needs_review','message_runtime_capacity_exhausted',0)
    op,turn,runtime=local(value)
    assert op['state']=='needs_review' and turn['status']=='blocked' and not turn['requires_review']
    assert runtime['phase']=='not_admitted' and not runtime['start_confirmed']
    assert runtime['receipt']['reason']=='capacity_exhausted' and 'cleanup_confirmed' not in runtime['receipt']
    assert value.message_runtime.db.execute('PRAGMA user_version').fetchone()[0]==3
    result=value.message_runs.turn_result(turn['id'])
    assert result=={'status':'blocked','reason':'runtime_capacity_exhausted','text':'','uncertain':False,
        'verification':{'runtime_not_admitted':True}}
    assert value.control.calls==['reserve_message_execution']
    assert not value.host.control()['enabled']
    assert value.host.dispatches()[0]['state']=='needs_review'
    assert len(value.board.get_runtime(value.human)['executions'])==1
    increase(value)
    assert value.tick_host()['status']=='disabled'
    assert value.host.control()['remaining']==0 and value.control.calls==['reserve_message_execution']
    assert_untouched(value)


def test_cached_negative_never_contacts_runtime_or_provider(managed):
    value=managed;occupy(value);value.post();value.enable();value.tick_host()
    op,turn,runtime=local(value);calls=list(value.control.calls)
    guard=MessageRuntimeGuard(value.message_runtime,value.control,op['payload']['scope'])
    with pytest.raises(RuntimeAdmissionRefused):
        PersonalRunner(value.message_runs,value.transport,runtime_guard=guard).run(op['work_id'],
            'host-message-'+op['worker_id'],op['payload']['prompt'],timeout=10)
    assert value.control.calls==calls
    assert local(value)[2]==runtime
    assert_untouched(value)


@pytest.mark.parametrize('fault',['before','legacy'])
def test_timeout_or_legacy_tool_error_stays_uncertain_even_after_capacity_change(managed,fault):
    value=managed;occupy(value);value.control.fault=fault;value.post();value.enable()
    result=value.tick_host();assert result['reason']=='message_runtime_reconciliation'
    op,turn,runtime=local(value)
    assert runtime['phase']=='needs_review' and runtime['receipt'] is None
    assert value.message_runs.turn_result(turn['id'])['uncertain'] is True
    increase(value)
    result=value.tick_host();assert result['status']=='pending' and result['reason']=='message_runtime_reconciliation'
    assert value.control.calls==['reserve_message_execution','lookup_execution']
    assert local(value)[2]['phase']=='needs_review'
    assert value.message_runtime.db.execute('PRAGMA user_version').fetchone()[0]==1
    assert_untouched(value)


@pytest.mark.parametrize('room',[False,True])
def test_lost_reply_does_not_adopt_negative_and_positive_cleanup_targets_real_slot(managed,room):
    value=managed;occupy(value)
    if room:increase(value)
    value.control.fault='after';value.post();value.enable()
    assert value.tick_host()['status']=='pending'
    assert local(value)[2]['phase']=='needs_review'
    result=value.tick_host()
    if room:
        assert result['status']=='needs_review' and result['reason']=='message_result_requires_review'
        assert value.control.calls==['reserve_message_execution','lookup_execution','finish_execution']
        assert local(value)[2]['phase']=='finished'
    else:
        assert result['status']=='pending' and result['reason']=='message_runtime_reconciliation'
        assert value.control.calls==['reserve_message_execution','lookup_execution']
        assert local(value)[2]['receipt'] is None
        with value.board._connection() as db:
            assert db.execute('SELECT COUNT(*) FROM runtime_admission_refusals').fetchone()[0]==1
        increase(value)
        assert value.tick_host()['status']=='pending'  # Never retrofit its lost proof.
    assert_untouched(value)


@pytest.mark.parametrize('change',[
    lambda x:x.update(extra='PRIVATE_DIAGNOSTIC'),
    lambda x:x['refusal'].update(extra='PRIVATE_DIAGNOSTIC'),
    lambda x:x['refusal'].update(request_sha256='0'*64),
    lambda x:x['refusal'].update(owner_id='other'),
    lambda x:x['refusal'].update(agent_id='other'),
    lambda x:x['refusal'].update(device_id='other'),
    lambda x:x['refusal'].update(profile_version=2),
    lambda x:x['refusal'].update(profile_version=True),
    lambda x:x['refusal'].update(session_id='other'),
    lambda x:x['refusal'].update(reason='owner_paused'),
    lambda x:x.update(reserved=0),
])
def test_malformed_or_foreign_proof_remains_uncertain(managed,change):
    value=managed;occupy(value);value.post();value.enable()
    def mutate(result):change(result);return result
    value.control.mutate=mutate
    assert value.tick_host()['reason']=='message_runtime_reconciliation'
    assert local(value)[2]['phase']=='needs_review' and local(value)[2]['receipt'] is None
    assert_untouched(value)


def test_journal_migration_preserves_old_rows_and_never_converts_old_review(managed):
    value=managed;occupy(value)
    # Create existing legacy ticket operations in both accepted old schemas.
    config=deepcopy(value.scope)
    from uuid import uuid4
    old_config=RuntimeJournal.configuration(config,str(uuid4()),str(uuid4()),'fixture-model')
    old,_=value.message_runtime.begin(old_config)
    old=value.message_runtime.update(old['id'],old['revision'],'reserved',{'id':'synthetic-prior-slot','state':'reserved'})
    old=value.message_runtime.update(old['id'],old['revision'],'starting')
    old=value.message_runtime.update(old['id'],old['revision'],'active',
        {'id':'synthetic-prior-slot','state':'running','revision':2},start_confirmed=True)
    old=value.message_runtime.update(old['id'],old['revision'],'needs_review')
    with value.message_runtime.transaction():value.message_runtime.db.execute('PRAGMA user_version=2')
    before=value.message_runtime.get(old['id'])
    value.post();value.enable();assert value.tick_host()['status']=='needs_review'
    assert value.message_runtime.get(old['id'])==before
    ids=[row[0] for row in value.message_runtime.db.execute('SELECT id FROM executions')]
    new=next(value.message_runtime.get(i) for i in ids if i!=old['id'])
    with pytest.raises(JournalError):
        value.message_runtime.update(old['id'],old['revision'],'not_admitted',new['receipt'])
    assert value.message_runtime.get(old['id'])==before
    directory=value.message_runtime.directory;value.message_runtime.close()
    value.message_runtime=RuntimeJournal(directory);value.addCleanup(value.message_runtime.close)
    assert value.message_runtime.get(old['id'])==before
    assert value.message_runtime.get(new['id'])==new
    for phase in ('not_admitted','reserved','needs_review','finished'):
        with pytest.raises(JournalError):value.message_runtime.update(new['id'],new['revision'],phase,new['receipt'])


def test_terminal_tamper_or_missing_schema_marker_refused_on_reopen(managed):
    value=managed;occupy(value);value.post();value.enable();value.tick_host()
    op,turn,runtime=local(value);path=value.message_runtime.directory/'runtime.sqlite3'
    value.message_runtime.close()
    with sqlite3.connect(path) as db:db.execute('PRAGMA user_version=2')
    with pytest.raises(JournalError):RuntimeJournal(path.parent)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=3')
        proof=deepcopy(runtime['receipt']);proof['request_sha256']='0'*64
        db.execute('UPDATE executions SET receipt=?',(json.dumps(proof),))
    with pytest.raises(JournalError):RuntimeJournal(path.parent)


def test_receipt_io_failure_cannot_claim_a_confirmed_negative(managed):
    value=managed;occupy(value);value.post();value.enable()
    original=value.message_runtime.update
    def lost(identifier,revision,phase,*args,**kwargs):
        result=original(identifier,revision,phase,*args,**kwargs)
        if phase=='not_admitted':raise OSError('SYNTHETIC_PRIVATE_DISK')
        return result
    with patch.object(value.message_runtime,'update',side_effect=lost):
        assert value.tick_host()['status']=='pending'
    op,turn,runtime=local(value)
    assert runtime['phase']=='not_admitted'
    assert value.message_runs.turn_result(turn['id'])['uncertain'] is True
    # Durable denial + uncertain local result is a conflict, never retro-adopted.
    assert value.tick_host()['status']=='pending'
    assert value.control.calls==['reserve_message_execution']
    assert_untouched(value)


def test_private_marker_and_refusal_transition_rollback_together(managed):
    value=managed;occupy(value);value.post();value.enable()
    value.message_runtime.db.execute('''CREATE TRIGGER synthetic_private_abort BEFORE UPDATE ON executions
        WHEN NEW.phase='not_admitted' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END''')
    assert value.tick_host()['status']=='pending'
    assert value.message_runtime.db.execute('PRAGMA user_version').fetchone()[0]==1
    assert local(value)[2]['phase']=='needs_review' and local(value)[2]['receipt'] is None
    assert_untouched(value)


def test_negative_proof_cannot_replace_a_known_positive_or_old_review(managed):
    value=managed;occupy(value);value.post();value.enable();value.tick_host()
    op,turn,negative=local(value)
    from uuid import uuid4
    config=deepcopy(negative['configuration']);config['turn_id']=str(uuid4())
    journal=value.message_runtime
    fresh,_=journal.begin(config)
    # Even a fresh phase rejects a previous proof with a different turn/key.
    with pytest.raises(JournalError):journal.update(fresh['id'],fresh['revision'],'not_admitted',negative['receipt'])
    positive=journal.update(fresh['id'],fresh['revision'],'reserved',{'id':'positive','state':'reserved'})
    with pytest.raises(JournalError):journal.update(positive['id'],positive['revision'],'not_admitted',negative['receipt'])
    guard=MessageRuntimeGuard(journal,value.control,negative['configuration']['scope'])
    guard.operation=negative;calls=list(value.control.calls)
    work=value.message_runs.get_work(op['work_id'])
    with pytest.raises(RuntimeControlError):guard.finish(work,turn['id'],value.message_runs.turn_result(turn['id']))
    assert value.control.calls==calls
