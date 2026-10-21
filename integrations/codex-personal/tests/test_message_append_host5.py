"""A private finite append transaction, not a live Board or launch authority."""
import copy
import json
import sqlite3
import threading
import uuid
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from blackoutburst_agents.host_config import HostError,digest
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_append_children import locked_message_append_children
from blackoutburst_agents.message_append_journal import effective_message_catalog,REMOTE,PIN_KEYS
from blackoutburst_agents.message_append_journal import _hash,MAX_PLAN,validate_append_proof
from blackoutburst_agents.message_journal import MessageJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.server_controller import ServerController,launch_catalog
from test_message_append_children import roster


def candidate(f,identifier='aaa-new'):
    value=copy.deepcopy(f.host.control()['configuration'])
    value['targets'].append({**value['targets'][0],'id':identifier,'agent_id':'profile-'+identifier,
        'board_session_id':'session-'+identifier,'workspace':str(f.root/('work-'+identifier)),
        'connection_file':str(f.root/('connection-'+identifier+'.json'))})
    return value


def proposal(f,children,value=None):
    kwargs=dict(operation_id=str(uuid.uuid4()),expected_host_revision=f.host.control()['revision'],
        expected_configuration_sha256=f.host.control()['hash'],expected_catalog_sha256=f.host.managed_settings()['catalog_sha256'],
        expected_release_sha256='b'*64,children=children)
    if f.host._messages_append_version()!=5:kwargs['anchor_operation_id']=str(uuid.uuid4())
    return f.host.propose_messages_append(candidate(f) if value is None else value,**kwargs)


def proof(p,phase):
    return dict(schema='server-messages-append-proof-v1',operation_id=p['operation_id'],request_id='c'*32,
        request_sha256='d'*64,owner_id=p['owner_id'],host_id=p['host_id'],device_id='fixture-device',
        grant_version=1,request_revision=REMOTE[phase],expected_host_version=1,expected_catalog_version=1,
        expected_instances_sha256='e'*64,**{k:p[k] for k in PIN_KEYS},phase=phase,
        instance_id='new-instance' if REMOTE[phase]>=4 else None,
        catalog_version=2 if REMOTE[phase]>=4 else None,instances_sha256='f'*64 if REMOTE[phase]>=4 else None)


def finish(f,p,children,proof_factory=proof):
    f.host.begin_messages_append(p,proof_factory(p,'prepared'),children=children,recheck=lambda:None)
    f.host.commit_messages_append(p['operation_id'],1,proof_factory(p,'precommit'),children=children,recheck=lambda:None)
    f.host.bind_messages_append_instance(p['operation_id'],2,proof_factory(p,'committed'),children=children,recheck=lambda:None)
    return f.host.complete_messages_append(p['operation_id'],3,proof_factory(p,'complete'),children=children,recheck=lambda:None)


def test_three_real_children_commit_and_catalogue_key_before_previous(roster):
    f=roster;before=f.snapshot();original=f.host.control();old=f.host.managed_target('primary')
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children)
        assert sorted(p['new_catalog'],key=lambda e:e['catalog_key'])[0]['catalog_key']=='aaa-new'
        done=finish(f,p,children)
        assert done['phase']=='complete' and done['revision']==4
        assert f.host.control()['configuration']==p['new_configuration']
        assert f.host.control()['hash']==digest(p['new_configuration'])
        for k in ('remaining','activation','next_target','instance_id','enabled'):
            assert f.host.control()[k]==original[k]
        assert f.host.managed_target('primary')==old
        new=f.host.managed_target('aaa-new')
        assert new['launchspec_sha256']==next(e['launchspec_sha256'] for e in p['new_catalog'] if e['catalog_key']=='aaa-new')
        assert new['instance_id']=='new-instance' and not new['enabled'] and new['remaining']==new['generation']==0
        assert effective_message_catalog(f.host,'b'*64)==p['new_catalog']
        assert children.recheck()==p['children_roster']
    # Every child DB is byte-for-byte unchanged; only the Host write-set moved.
    assert f.snapshot()[1:]==before[1:]
    with HostJournal(f.host.directory) as reopened:
        assert effective_message_catalog(reopened,'b'*64)==p['new_catalog']


def test_pending_transition_rejects_old_mutations_without_blocking_stop(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)
        assert f.host.require_messages_append_recovery(p['operation_id'])['phase']=='intent'
        with pytest.raises(HostError):f.host.require_messages_append_recovery(str(uuid.uuid4()))
        command=dict(id='forbidden-start',target_id='primary',instance_id='fixture-instance',generation=4,
            action='start',launchspec_sha256=f.catalog[0]['launchspec_sha256'],budget=1)
        for operation in (lambda:f.host.configure(f.config),lambda:f.host.prepare_managed('fixture-host',f.catalog),
                lambda:f.host.bind_managed_instances({'primary':'fixture-instance'}),
                lambda:f.host.queue_managed_command(command),lambda:f.host.require_no_catalog_source_transition()):
            with pytest.raises(HostError):operation()
        f.host.queue_managed_command({**command,'id':'permitted-stop','action':'stop','budget':0})
        assert not f.host.managed_target('primary')['enabled']
        assert f.host.apply_managed_command('permitted-stop')['state']=='applied'
        with pytest.raises(HostError):
            f.host.commit_messages_append(p['operation_id'],1,proof(p,'precommit'),children=children,recheck=lambda:None)


@pytest.mark.parametrize('phase',['prepared','precommit','committed','complete'])
def test_proof_hash_or_type_tamper_never_advances(roster,phase):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children)
        methods=[f.host.begin_messages_append,f.host.commit_messages_append,
            f.host.bind_messages_append_instance,f.host.complete_messages_append]
        for i,name in enumerate(REMOTE):
            bad=proof(p,name)
            if name==phase:bad['grant_version']=True
            before=f.snapshot()
            args=(p,bad) if i==0 else (p['operation_id'],i,bad)
            if name==phase:
                with pytest.raises(HostError):methods[i](*args,children=children,recheck=lambda:None)
                assert f.snapshot()==before
                break
            methods[i](*args,children=children,recheck=lambda:None)


def test_same_plan_and_receipt_replays_exactly_but_not_changed_candidate(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);one=f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)
        assert f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)==one
        two=f.host.commit_messages_append(p['operation_id'],1,proof(p,'precommit'),children=children,recheck=lambda:None)
        assert f.host.commit_messages_append(p['operation_id'],1,proof(p,'precommit'),children=children,recheck=lambda:None)==two
        bad=proof(p,'committed');bad['bindings_sha256']='a'*64
        with pytest.raises(HostError):f.host.bind_messages_append_instance(p['operation_id'],2,bad,children=children,recheck=lambda:None)


def test_guard_writes_are_rolled_back_before_local_intent(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);before=f.snapshot()
        def corrupt():f.host._db.execute('UPDATE control SET remaining=remaining+1')
        with pytest.raises(HostError):f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=corrupt)
        assert f.snapshot()==before and f.host._messages_append_version()==4


def test_wrong_installed_release_refuses_even_self_consistent_state(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        finish(f,proposal(f,children),children)
        with pytest.raises(HostError):effective_message_catalog(f.host,'a'*64)


@pytest.mark.parametrize('mutation',[
    "UPDATE control SET remaining=1", "UPDATE control SET activation='changed'",
    "UPDATE managed_targets SET generation=generation+1", "UPDATE targets SET profile_hash='changed'",
    "UPDATE managed_settings SET host_id='changed-host'",
])
def test_first_begin_pins_actual_host_snapshot_before_migration(roster,mutation):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);f.host._db.execute(mutation);before=f.snapshot()
        with pytest.raises(HostError):f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)
        assert f.snapshot()==before and f.host._messages_append_version()==4


def test_rehashed_host_identity_cannot_adopt_a_receipt_for_another_host(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);p['host_id']='different-host'
        p['plan_sha256']=_hash({k:v for k,v in p.items() if k!='plan_sha256'})
        before=f.snapshot()
        with pytest.raises(HostError):f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)
        assert f.snapshot()==before


@pytest.mark.parametrize('name,statement',[
    ('host',"UPDATE control SET remaining=1"),
    ('messages',"UPDATE readers SET checkpoint=checkpoint+1"),
    ('runs',"UPDATE works SET status='needs_review'"),
    ('runtime',"UPDATE executions SET phase='needs_review'"),
])
def test_readonly_callback_cannot_write_any_journal_even_if_denial_is_caught(roster,name,statement):
    f=roster;db={'host':f.host._db,'messages':f.messages.db,'runs':f.message_runs._db,'runtime':f.message_runtime.db}[name]
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);before=f.snapshot()
        def write():
            try:db.execute(statement)
            except sqlite3.DatabaseError:pass
        with pytest.raises(HostError):f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=write)
        assert f.snapshot()==before


@pytest.mark.parametrize('boundary',range(1,5))
def test_cold_reopen_at_each_durable_boundary_uses_same_intent(roster,boundary):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children)
        methods=[f.host.begin_messages_append,f.host.commit_messages_append,f.host.bind_messages_append_instance,f.host.complete_messages_append]
        for index,phase in enumerate(REMOTE):
            if index>=boundary:break
            args=(p,proof(p,phase)) if index==0 else (p['operation_id'],index,proof(p,phase))
            durable=methods[index](*args,children=children,recheck=lambda:None)
    with HostJournal(f.host.directory) as reopened:
        with reopened.owner_lock(lock_directory=f.root/'locks'):
            with locked_message_append_children(reopened,f.messages,f.message_runs,f.message_runtime,operation_id=p['operation_id']) as children:
                assert reopened.messages_append_record(operation_id=p['operation_id'])==durable
                methods=[reopened.begin_messages_append,reopened.commit_messages_append,reopened.bind_messages_append_instance,reopened.complete_messages_append]
                # A lost response replays byte-exactly, then resumes one phase.
                index=boundary-1;phase=tuple(REMOTE)[index]
                args=(p,proof(p,phase)) if index==0 else (p['operation_id'],index,proof(p,phase))
                assert methods[index](*args,children=children,recheck=lambda:None)==durable
                for index,phase in enumerate(REMOTE):
                    if index<boundary:continue
                    methods[index](p['operation_id'],index,proof(p,phase),children=children,recheck=lambda:None)
                assert reopened.messages_append_record(operation_id=p['operation_id'])['phase']=='complete'


def test_appended_catalogue_does_not_regenerate_old_launchspecs_for_publication(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);finish(f,p,children)
        with pytest.raises(HostError):f.host.require_messages_append_recovery(p['operation_id'])
    class NoRPC:
        def call(self,*args):raise AssertionError('no RPC after a wrong catalogue')
    controller=ServerController(f.host,NoRPC())
    derived=launch_catalog(f.host.control()['configuration'],'b'*64)
    assert derived!=p['new_catalog']
    with pytest.raises(HostError):controller.publish(derived)
    # A self-consistent forged settings hash still cannot replace the bindings.
    f.host._db.execute('UPDATE managed_settings SET catalog_sha256=?',(digest(sorted(derived,key=lambda e:e['catalog_key'])),))
    with pytest.raises(HostError):controller.publish(derived)
    with pytest.raises(HostError):f.host.require_no_messages_append()


def test_legacy_history_readable_but_old_writers_and_two_child_guard_remain_closed(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);finish(f,p,children)
        old=f.host._abandon_record(intent_id=f.legacy_result['intent_id'])
        executed=f.host._executed_record(intent_id=f.executed_result['intent_id'])
        assert old['phase']==executed['phase']=='complete'
        before=f.snapshot()
        for operation in (lambda:f.host._abandon_advance(old,'complete'),lambda:f.host._executed_advance(executed,'complete'),
                lambda:f.host._catalog_source_clean(),lambda:f.host._append_guard(f.host._db,p['new_configuration_sha256'],p['new_catalog_sha256'])):
            with pytest.raises(HostError):operation()
        assert f.snapshot()==before
    with f.locked(),pytest.raises(HostError):f.verify()


@pytest.mark.parametrize('kind,sql',[
    ('trigger',"CREATE TRIGGER hidden AFTER INSERT ON targets BEGIN UPDATE control SET remaining=1; END"),
    ('temp',"CREATE TEMP TABLE targets(id TEXT)"),
    ('index',"CREATE INDEX hidden ON managed_targets(generation)"),
    ('view',"CREATE VIEW hidden AS SELECT * FROM control"),
])
def test_host_schema_side_effects_are_refused_without_migrating(roster,kind,sql):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);f.host._db.execute(sql);before=f.snapshot()
        with pytest.raises(HostError):f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)
        assert f.snapshot()==before and f.host._messages_append_version()==4


def test_boolean_or_serialized_children_and_wrong_thread_are_not_authority(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);before=f.snapshot()
        for fake in (True,children.proof):
            with pytest.raises(HostError):f.host.begin_messages_append(p,proof(p,'prepared'),children=fake,recheck=lambda:None)
        errors=[]
        def other_thread():
            try:children.recheck()
            except HostError:errors.append(True)
        thread=threading.Thread(target=other_thread);thread.start();thread.join()
        assert errors==[True] and f.snapshot()==before


def test_append_recovery_does_not_reconstruct_owner_acquisition(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children);f.host.begin_messages_append(p,proof(p,'prepared'),children=children,recheck=lambda:None)
    before=f.snapshot()
    with pytest.raises(HostError):f.host.require_messages_append_recovery(p['operation_id'])
    assert f.snapshot()==before


def test_proof_wire_is_exact_and_plan_hash_is_bounded_utf8():
    from blackoutburst_agents.work_journal import JournalError
    assert _hash({'é':'été'})!=''
    with pytest.raises(JournalError):_hash({'x':'é'*(MAX_PLAN//2)})
    with pytest.raises(HostError):validate_append_proof({'schema':'server-messages-append-proof-v1','phase':'prepared'})


def test_multitarget_host4_anchor_two_appends_and_immutable_predecessor_history(roster):
    f=roster
    with HostJournal(f.root/'multi-host') as host:
        host.configure(candidate(f,'prior-two'))
        own=SimpleNamespace(host=host,root=f.root)
        with host.owner_lock(lock_directory=f.root/'multi-locks'):
            entries=launch_catalog(host.control()['configuration'],'b'*64)
            host.prepare_managed('multi-host',entries)
            host.bind_managed_instances({e['catalog_key']:'instance-'+e['catalog_key'] for e in entries})
            with host._tx() as db:
                host._abandon_schema(db,create=True);host._executed_schema(db,create=True)
                db.execute('PRAGMA user_version=4')
                db.execute("UPDATE control SET remaining=1,next_target=1,activation='original-global'")
                db.execute("UPDATE managed_targets SET remaining=1,activation=?,generation=8",(str(uuid.uuid4()),))
        with MessageJournal(host.directory/'messages') as messages,closing(RunStore(host.directory/'message-runs')) as runs,RuntimeJournal(host.directory/'message-runtime') as runtime:
            with host.owner_lock(lock_directory=f.root/'multi-locks'):
                old_control=host.control();old_targets={e['catalog_key']:host.managed_target(e['catalog_key']) for e in entries}
                with locked_message_append_children(host,messages,runs,runtime) as children:
                    p1=proposal(own,children)
                    assert p1['anchor_operation_id']!=p1['operation_id']
                    assert len(p1['old_bindings']['bindings'])==2 and len(p1['old_bindings']['origins'])==1
                    first=finish(own,p1,children)
                def later(p,phase):
                    value=proof(p,phase)
                    value.update(request_id='a'*32,expected_catalog_version=2,expected_host_version=2)
                    if REMOTE[phase]>=4:value.update(instance_id='final-instance',catalog_version=3)
                    return value
                with locked_message_append_children(host,messages,runs,runtime) as children:
                    p2=proposal(own,children,candidate(own,'000-final'))
                    assert p2['anchor_operation_id'] is None
                    finish(own,p2,children,later)
                assert host.messages_append_record(operation_id=p1['operation_id'])==first
                assert host.control()['remaining']==old_control['remaining']==1
                assert host.control()['next_target']==1 and host.control()['activation']=='original-global'
                assert all(host.managed_target(key)==value for key,value in old_targets.items())
                assert host.managed_target('000-final')['remaining']==host.managed_target('aaa-new')['remaining']==0
                assert list(messages.db.execute('SELECT * FROM readers'))==[]
                assert effective_message_catalog(host,'b'*64)==p2['new_catalog']
                # A validly shaped predecessor mutation cannot become current
                # authority merely by preserving its own internal consistency.
                proofs=copy.deepcopy(first['proofs'])
                for item in proofs:item['request_sha256']='0'*64
                host._db.execute('UPDATE host_message_appends SET proofs_json=? WHERE operation_id=?',
                    (json.dumps(proofs,sort_keys=True,ensure_ascii=False,separators=(',',':')),p1['operation_id']))
                with pytest.raises(HostError):effective_message_catalog(host,'b'*64)


@pytest.mark.parametrize('boundary',range(4))
def test_failure_after_local_writes_rolls_back_entire_phase(roster,boundary):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        p=proposal(f,children)
        methods=[f.host.begin_messages_append,f.host.commit_messages_append,f.host.bind_messages_append_instance,f.host.complete_messages_append]
        for index,phase in enumerate(REMOTE):
            args=(p,proof(p,phase)) if index==0 else (p['operation_id'],index,proof(p,phase))
            if index<boundary:methods[index](*args,children=children,recheck=lambda:None);continue
            if index>boundary:break
            before=f.snapshot();marker=f.host._messages_append_version()
            original=f.host._messages_append_expected
            def fail_after_postcondition(record):
                original(record)
                if record['revision']==boundary+1:raise HostError('synthetic_crash_after_writes')
            with patch.object(f.host,'_messages_append_expected',side_effect=fail_after_postcondition):
                with pytest.raises(HostError):methods[index](*args,children=children,recheck=lambda:None)
            assert f.snapshot()==before and f.host._messages_append_version()==marker
            # Retrying the same phase after the rollback is still finite.
            assert methods[index](*args,children=children,recheck=lambda:None)['revision']==boundary+1
