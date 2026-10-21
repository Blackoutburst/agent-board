"""Actual Board, Host and three-child journals; synthetic transport only.

Public fixture IDs/generation are seeded to the existing historical private
fixture before the actual human Stop. All append boundaries use real APIs and
the production ServerControlClient validators, without HTTP or an OS process.
"""
from copy import deepcopy

import pytest
from board import BoardError

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_config import HostError, digest
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_append_coordinator import MessagesAppendCoordinator
from blackoutburst_agents.server_control_client import ServerControlClient
from blackoutburst_agents.server_controller import ServerController
from test_message_append_children import roster


class PublicClient(ServerControlClient):
    def __init__(self,f):
        self.fixture=f;self.calls=[];self.lose=None
    def _initialize(self):pass
    def _tool(self,name,args):
        f=self.fixture
        assert f.host._held and not any(db.in_transaction for db in
            (f.host._db,f.messages.db,f.message_runs._db,f.message_runtime.db))
        self.calls.append((name,deepcopy(args)))
        try:result=getattr(f.board,name)(f.raw,**args)
        except BoardError:raise BoardReadError('tool_rejected') from None
        if self.lose==name:
            self.lose=None
            raise BoardReadError('timeout')
        return result


@pytest.fixture
def coupled(roster):
    f=roster;b,h,raw=f.board,f.human,f.raw
    host=b.create_server_host(h,raw['device_id'],'Fixture owner','fixture-grant')
    with b._tx() as db:db.execute('UPDATE server_hosts SET id=? WHERE id=?',('fixture-host',host['id']))
    published=b.publish_server_catalog(raw,'fixture-host',0,f.catalog,'fixture-catalog')
    old=published['instances'][0]
    with b._tx() as db:
        db.execute('UPDATE server_instances SET id=?,generation=? WHERE id=?',
            ('fixture-instance',f.host.managed_target('primary')['generation'],old['id']))
    old=b.get_server_host(h,'fixture-host')['instances'][0]
    stop=b.command_server_instance(h,old['id'],'stop',old['version'],old['generation'],old['launchspec_sha256'],0,'real-human-stop')
    with f.locked():f.host.queue_managed_command(ServerController._normalized(old,stop))
    b.ack_instance_command(raw,'fixture-host',stop['id'],stop['revision'],'real-stop-ack')
    with f.locked():f.host.apply_managed_command(stop['id'])
    old=b.get_server_host(h,'fixture-host')['instances'][0]
    b.report_instance_state(raw,'fixture-host',old['id'],old['observation_revision'],old['generation'],
        old['launchspec_sha256'],stop['id'],'stopped',True,'stopped','real-stop-cleanup')
    model=f.config['targets'][0]['model']
    p=b.create_agent_profile(h,'Additional responder',preferred_provider='codex_chatgpt',preferred_model=model,
        policy={'allow_read':True,'allow_messages':True,'allow_requests':False,'actions':[],'resources':[],'autonomy':'responses'})
    p=b.bind_agent_profile(h,p['id'],p['version'],raw['device_id'],'additional-slot','codex')
    session='additional-'+p['id'];b.register_agent(raw,session,'Prepared slot',agent_kind='codex',identity_key='additional-slot')
    b.set_server_messages_append_grant(h,'fixture-host',schema='server-messages-append-grant-v1',
        expected_host_version=1,expected_grant_version=0,enabled=True,idempotency_key='explicit-B-grant')
    context=b.get_server_messages_append_context(h,'fixture-host',p['id'])
    fields='expected_host_version expected_catalog_version expected_instances_sha256 grant_version profile_id profile_version profile_sha256 provider_id requested_model'.split()
    body=dict(schema='server-messages-append-request-v1',**{k:context[k] for k in fields},
        slot_id=context['slots'][0]['slot_id'],idempotency_key='one-explicit-B-request')
    request=b.create_server_messages_append(h,'fixture-host',**body)
    target=deepcopy(f.config['targets'][0]);target.update(id='aaa-added',agent_id=p['id'],profile_version=p['version'],
        board_session_id=session,workspace=str(f.root/'additional-workspace'),connection_file=str(f.root/'additional-connection.json'))
    client=PublicClient(f)
    def candidate(record,current):
        value=deepcopy(current);value['targets'].append(deepcopy(target));return value
    coordinator=MessagesAppendCoordinator(f.host,client,source_release_sha256='b'*64,
        candidate_factory=candidate,source_guard=lambda:None)
    return f,client,coordinator,request


def current_record(f,client,coordinator):
    queue=client.call('poll_server_messages_appends',{'host_id':'fixture-host','limit':1})['requests']
    return queue[0] if queue else coordinator.pending_hint()


@pytest.mark.parametrize('lost',[None,'prepare_server_messages_append','precommit_server_messages_append',
    'commit_server_messages_append','complete_server_messages_append'])
def test_real_boundaries_and_every_lost_reply_finish_one_inactive_membership(coupled,lost):
    f,client,c,request=coupled;before=f.snapshot()[1:];model_calls=f.calls
    client.lose=lost;seen_loss=False
    with f.locked():
        for _ in range(7):
            record=current_record(f,client,c)
            assert record is not None
            try:result=c.advance(record)
            except BoardReadError:
                assert not seen_loss;seen_loss=True;continue
            if result['status']=='complete':break
        else:pytest.fail('finite append did not complete')
        assert c.pending_hint() is None
    assert seen_loss is (lost is not None)
    assert f.snapshot()[1:]==before and f.calls==model_calls
    view=f.board.get_server_messages_append(f.human,'fixture-host',request['request_id'])
    assert view['state']=='complete' and view['connection_status']=='not_attested'
    rows=f.board.get_server_host(f.human,'fixture-host')['instances']
    assert len(rows)==2
    added=next(row for row in rows if row['catalog_key']=='aaa-added')
    assert (added['desired_state'],added['observed_state'],added['cleanup_confirmed'])==('stopped','unknown',False)
    assert f.host.managed_target('aaa-added')['remaining']==0
    assert f.host.managed_target('aaa-added')['generation']==0
    assert not f.host.managed_target('aaa-added')['enabled']


def test_missing_original_outbox_after_prepare_is_not_replanned(coupled):
    f,client,c,request=coupled
    with f.locked():
        client.lose='prepare_server_messages_append'
        with pytest.raises(BoardReadError):c.advance(current_record(f,client,c))
        path=f.host.directory/'message-append-outbox'/(request['request_id']+'.json');path.unlink()
        before=f.snapshot()
        with pytest.raises(HostError,match='original_proposal_required'):c.advance(current_record(f,client,c))
        assert f.snapshot()==before


def test_changed_host_after_prepare_does_not_adopt_new_snapshot(coupled):
    f,client,c,request=coupled
    with f.locked():
        client.lose='prepare_server_messages_append'
        with pytest.raises(BoardReadError):c.advance(current_record(f,client,c))
        f.host._db.execute('UPDATE control SET remaining=remaining+1')
        before=f.snapshot()
        with pytest.raises(HostError):c.advance(current_record(f,client,c))
        assert f.snapshot()==before
        assert f.host._db.execute('PRAGMA user_version').fetchone()[0]==4


def test_public_stop_from_another_generation_never_creates_local_intent(coupled):
    f,client,c,request=coupled
    with f.board._tx() as db:db.execute("UPDATE server_instances SET generation=generation+1 WHERE id='fixture-instance'")
    with f.locked():
        before=f.snapshot()
        with pytest.raises(BoardReadError,match='tool_rejected'):c.advance(current_record(f,client,c))
        assert f.snapshot()==before
        assert f.host._db.execute('PRAGMA user_version').fetchone()[0]==4


@pytest.mark.parametrize('lost',['prepare_server_messages_append','precommit_server_messages_append',
    'commit_server_messages_append','complete_server_messages_append'])
def test_cold_resume_reads_the_same_original_proposal_and_intent(coupled,lost):
    f,client,c,request=coupled;client.lose=lost
    with f.locked():
        for _ in range(4):
            try:c.advance(current_record(f,client,c))
            except BoardReadError:break
        else:pytest.fail('lost reply not exercised')
    old=f.host;f.host=HostJournal(old.directory);f.addCleanup(f.host.close);old.close()
    resumed=MessagesAppendCoordinator(f.host,client,source_release_sha256='b'*64,
        candidate_factory=c.candidate_factory,source_guard=lambda:None)
    with f.locked():
        for _ in range(5):
            item=current_record(f,client,resumed)
            assert item is not None
            if resumed.advance(item)['status']=='complete':break
        else:pytest.fail('cold resume did not complete')
    assert f.board.get_server_messages_append(f.human,'fixture-host',request['request_id'])['state']=='complete'
    assert len(f.board.get_server_host(f.human,'fixture-host')['instances'])==2


def test_completion_reply_lost_then_human_stop_keeps_both_operations_distinct(coupled):
    f,client,c,request=coupled;client.lose='complete_server_messages_append'
    with f.locked():
        for _ in range(4):
            try:c.advance(current_record(f,client,c))
            except BoardReadError:break
        else:pytest.fail('lost completion reply not exercised')
        rows=f.board.get_server_host(f.human,'fixture-host')['instances']
        new=next(row for row in rows if row['catalog_key']=='aaa-added')
        stop=f.board.command_server_instance(f.human,new['id'],'stop',new['version'],new['generation'],
            new['launchspec_sha256'],0,'human-stop-after-membership')
        # Historical complete is exact delivery evidence; the fresh membership
        # poll permits the later Stop and never pretends it has been applied.
        result=c.advance(c.pending_hint());assert result['status']=='complete'
    new=next(row for row in f.board.get_server_host(f.human,'fixture-host')['instances'] if row['catalog_key']=='aaa-added')
    assert new['current_command']['id']==stop['id'] and new['current_command']['status']=='queued'
    assert new['observed_state']=='unknown' and new['cleanup_confirmed'] is False


@pytest.mark.parametrize('field,new',[('request_sha256','f'*64),('device_id','different-manager'),
    ('grant_version',2),('owner_id','different-owner'),('expected_host_version',2),
    ('expected_catalog_version',2),('expected_instances_sha256','e'*64)])
def test_prepare_receipt_is_bound_to_the_original_human_request(coupled,field,new):
    f,client,c,request=coupled;original=client.call
    def substitute(name,args):
        result=original(name,args)
        if name=='prepare_server_messages_append':result={**result,field:new}
        return result
    client.call=substitute
    with f.locked():
        before=f.snapshot()
        with pytest.raises(HostError,match='receipt_conflict'):c.advance(current_record(f,client,c))
        assert f.snapshot()==before
        assert f.host._db.execute('PRAGMA user_version').fetchone()[0]==4
