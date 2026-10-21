"""Actual temporary child graph, without a provider, process or external RPC."""
import copy
import pickle
from types import MethodType

import pytest

from blackoutburst_agents.host_config import HostError
from blackoutburst_agents.message_append_children import locked_message_append_children
import test_message_disposed_children as disposed_fixture
import test_message_host as host_fixture


@pytest.fixture
def roster():
    f=disposed_fixture.TwoDisposedChildrenTests('test_real_roster_has_two_distinct_proofs_and_stays_stable_after_normal_ack')
    f.setUp()
    try:
        f.transport=MethodType(host_fixture.MessageHostTests.transport,f)
        with f.locked():
            f.host.queue_managed_command({'id':'normal-start','target_id':'primary','instance_id':'fixture-instance',
                'generation':3,'action':'start','launchspec_sha256':f.catalog[0]['launchspec_sha256'],'budget':1})
            f.host.apply_managed_command('normal-start')
        assert f.tick_host()['status']=='idle'  # Normal ACK of the executed-disposed predecessor.
        f.post()
        assert f.tick_host()['status']=='complete'
        with f.locked():f.host.stop()
        yield f
    finally:f.doCleanups()


def test_all_three_kinds_are_reconciled_under_real_locks_readonly(roster):
    f=roster
    before=f.snapshot();calls=(f.calls,copy.deepcopy(f.seal.calls),copy.deepcopy(f.observer.calls))
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        proof=children.proof
        assert set(proof)=={'schema','host_instance_id','configuration_sha256','roster_sha256'}
        assert proof['schema']=='message-append-children-v1'
        assert children.recheck()==proof
        with f.host._tx():assert children.recheck()==proof
        with pytest.raises(TypeError):pickle.dumps(children)
        assert {r[0] for r in f.message_runs._db.execute('SELECT status FROM works')}=={'closed','open'}
    assert f.snapshot()==before
    assert calls==(f.calls,f.seal.calls,f.observer.calls)
    with pytest.raises(HostError):children.recheck()


@pytest.mark.parametrize('table,sql',[
    ('operations',"UPDATE operations SET state='needs_review' WHERE state='complete'"),
    ('deliveries',"UPDATE deliveries SET state='acking' WHERE id=(SELECT delivery_id FROM operations WHERE state='complete')"),
    ('turns',"UPDATE turns SET requires_review=1 WHERE status='completed'"),
    ('executions',"UPDATE executions SET phase='needs_review' WHERE phase='finished' AND configuration LIKE '%normal-impossible%'"),
])
def test_changed_child_is_not_a_new_proof(roster,table,sql):
    f=roster
    db=f.messages.db if table in ('operations','deliveries') else f.message_runs._db if table=='turns' else f.message_runtime.db
    if table=='executions':sql="UPDATE executions SET phase='needs_review' WHERE id=(SELECT id FROM executions WHERE phase='finished' ORDER BY rowid DESC LIMIT 1)"
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        db.execute(sql)
        with pytest.raises(HostError):children.recheck()


def test_no_owner_acquisition_or_replayed_acquisition(roster):
    f=roster
    with pytest.raises(HostError):
        with locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime):pass
    with f.locked():
        with locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
            proof=children.proof
    with f.locked():
        with pytest.raises(HostError):children.recheck()
        with locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as current:
            assert current.proof==proof


def test_unknown_work_cannot_be_filtered_from_roster(roster):
    f=roster
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        f.message_runs._db.execute("UPDATE works SET status='needs_review' WHERE status='open'")
        with pytest.raises(HostError):children.recheck()


@pytest.mark.parametrize('name,sql',[
    ('messages',"CREATE TABLE unrelated(id TEXT)"),
    ('messages',"CREATE INDEX unexpected ON readers(checkpoint)"),
    ('messages',"CREATE VIEW unexpected AS SELECT * FROM readers"),
    ('messages',"CREATE TEMP TABLE operations(id TEXT)"),
    ('runtime',"CREATE TRIGGER unexpected AFTER UPDATE ON executions BEGIN SELECT 1; END"),
    ('runtime',"CREATE TABLE unrelated(id TEXT)"),
    ('runs',"CREATE VIEW unexpected AS SELECT * FROM works"),
])
def test_unknown_child_schema_is_rejected(roster,name,sql):
    f=roster;db={'messages':f.messages.db,'runs':f.message_runs._db,'runtime':f.message_runtime.db}[name]
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        db.execute(sql);before=f.snapshot()
        with pytest.raises(HostError):children.recheck()
        assert f.snapshot()==before


@pytest.mark.parametrize('name,sql',[
    ('messages',"UPDATE deliveries SET ack_json='{}' WHERE rowid=(SELECT MIN(rowid) FROM deliveries)"),
    ('messages',"UPDATE operations SET payload_json=json_set(payload_json,'$.body','changed') WHERE state='complete'"),
    ('messages',"UPDATE deliveries SET payload_hash='0000000000000000000000000000000000000000000000000000000000000000' WHERE rowid=(SELECT MIN(rowid) FROM deliveries)"),
    ('messages',"UPDATE readers SET checkpoint=checkpoint+1"),
    ('runs',"UPDATE turns SET prompt_sha256='changed' WHERE status='completed'"),
    ('runs',"UPDATE works SET provider_session_id=NULL WHERE status='open'"),
    ('runtime',"PRAGMA application_id=0"),
    ('runtime',"PRAGMA user_version=1"),
])
def test_normal_publication_ack_and_actual_receipt_are_reconciled(roster,name,sql):
    f=roster;db={'messages':f.messages.db,'runs':f.message_runs._db,'runtime':f.message_runtime.db}[name]
    with f.locked(),locked_message_append_children(f.host,f.messages,f.message_runs,f.message_runtime) as children:
        db.execute(sql)
        with pytest.raises(HostError):children.recheck()
