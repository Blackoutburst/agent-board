"""Temporary SQLite/files only: proposals never grant config/Board authority."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
import pickle
import threading

import pytest

from blackoutburst_agents.host_config import HostError, digest, validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_append_stage import _StageAttempt
from blackoutburst_agents.work_journal import _encode
from blackoutburst_agents.server_controller import launch_catalog
from blackoutburst_agents.message_journal import MessageJournal
from test_host_journal import configuration


@pytest.fixture
def host(tmp_path):
    cfg = configuration(tmp_path)
    cfg['schema_version'] = 2; cfg['dispatch_budget'] = 4
    target = cfg['targets'][0]
    for key in ('project_id','ticket_id','after'):
        del target[key]
    target.update(thread_ids=['thread-one'], start_at='now')
    cfg = validate(cfg, bind_connections=True)
    with HostJournal(tmp_path/'host') as journal:
        journal.configure(cfg)
        journal.test_locks = tmp_path/'locks'
        with locked(journal):
            journal.prepare_managed('owner-host', launch_catalog(cfg, 'd'*64))
            journal.bind_managed_instances({'primary':'instance-one'})
        yield journal


def locked(host):
    return host.owner_lock(lock_directory=host.test_locks)


def candidate(host):
    value = host.control()['configuration']
    target = {**value['targets'][0], 'id':'second', 'agent_id':'agent-two',
        'board_session_id':'session-two', 'workspace':str(host.directory.parent/'new-workspace'),
        'connection_file':str(host.directory.parent/'prepaired-slot-two.json')}
    value['targets'].append(target)
    return value


def stage(host, value=None, key='append-one', **changes):
    args = {'expected_configuration_sha256':host.control()['hash'],
        'expected_catalog_sha256':host.managed_settings()['catalog_sha256'], 'idempotency_key':key}
    return host.stage_messages_append(candidate(host) if value is None else value, **{**args, **changes})


def live(host):
    tables = ('control','targets','dispatches','managed_settings','managed_targets','managed_commands','managed_effects')
    return {name:tuple(tuple(row) for row in host._db.execute('SELECT * FROM '+name+' ORDER BY rowid')) for name in tables}


def command(host, *, action='start', identifier='start-one', generation=1):
    target = host.managed_target('primary')
    return {'id':identifier,'target_id':'primary','instance_id':'instance-one','generation':generation,
        'action':action,'launchspec_sha256':target['launchspec_sha256'],'budget':2 if action=='start' else 0}


def test_stage_only_preserves_live_state_and_replays_metadata_across_reopen(host):
    before = live(host); value = candidate(host)
    with locked(host):
        result = stage(host, value)
        assert stage(host, copy.deepcopy(value)) == result
    assert live(host) == before
    assert result['status'] == 'staged' and result['work_budget'] == 0
    assert result['target_enabled'] is False and result['activation_allowed'] is False
    assert result['board_fence_verified'] is False and result['live_configuration_changed'] is False
    assert result['candidate_configuration_sha256'] == digest(value)
    assert all(word not in json.dumps(result) for word in ('PRIVATE_INSTRUCTION','connection.json','test-secret'))
    assert host._db.execute('PRAGMA user_version').fetchone()[0] == 2
    assert host._db.execute('SELECT COUNT(*) FROM host_append_stages').fetchone()[0] == 1
    with HostJournal(host.directory) as reopened:
        reopened.test_locks = host.test_locks
        with locked(reopened):
            assert stage(reopened, value) == result
    assert live(host) == before


def test_real_owner_acquisition_required_and_competing_journal_cannot_enter(host):
    with pytest.raises(HostError, match='host_lock_required'):
        stage(host)
    host._held = (os.getpid(), threading.get_ident())
    try:
        with pytest.raises(HostError, match='host_lock_required'):
            stage(host)
    finally:
        host._held = False
    with locked(host), HostJournal(host.directory) as other:
        with pytest.raises(HostError, match='host_busy'):
            with other.owner_lock(lock_directory=host.test_locks):
                stage(other)


@pytest.mark.parametrize('case', ['global','old_target','reorder','refresh','remove','no_append','two_appends',
    'duplicate_profile','duplicate_id','duplicate_session','same_workspace','same_connection','history_start',
    'numeric_type','bad_hash','extra_field','bad_schema','too_large'])
def test_only_one_normalized_append_is_accepted_without_side_effects(host, case):
    value = candidate(host)
    if case=='global': value['dispatch_budget'] += 1
    elif case=='old_target': value['targets'][0]['model'] = 'different'
    elif case=='reorder': value['targets'].reverse()
    elif case=='refresh': value['targets'][0]['profile_version'] += 1
    elif case=='remove': value['targets'] = value['targets'][1:]
    elif case=='no_append': value['targets'].pop()
    elif case=='two_appends': value['targets'].append({**value['targets'][-1], 'id':'third'})
    elif case=='duplicate_profile': value['targets'][-1]['agent_id'] = value['targets'][0]['agent_id']
    elif case=='duplicate_id': value['targets'][-1]['id'] = 'primary'
    elif case=='duplicate_session': value['targets'][-1]['board_session_id'] = 'session'
    elif case=='same_workspace': value['targets'][-1]['workspace'] = value['targets'][0]['workspace']
    elif case=='same_connection': value['targets'][-1]['connection_file'] = value['targets'][0]['connection_file']
    elif case=='history_start': value['targets'][-1]['start_at'] = 'beginning'
    elif case=='numeric_type': value['targets'][0]['timeout'] = float(value['targets'][0]['timeout'])
    elif case=='bad_hash': value['targets'][-1]['connection_sha256'] = 'bad'
    elif case=='extra_field': value['provider_token'] = 'SENTINEL_NEVER_ECHO'
    elif case=='bad_schema': value['schema_version'] = True
    elif case=='too_large': value['targets'][-1]['instruction'] = 'x'*300000
    before = live(host)
    with locked(host), pytest.raises(HostError) as error:
        stage(host, value)
    assert 'SENTINEL_NEVER_ECHO' not in str(error.value)
    assert live(host) == before
    assert not host._db.execute("SELECT 1 FROM sqlite_master WHERE name='host_append_stages'").fetchone()


@pytest.mark.parametrize('field,value', [('expected_configuration_sha256','a'*64),
    ('expected_catalog_sha256','b'*64),('expected_catalog_sha256',False),('idempotency_key','\ud800')])
def test_exact_cas_and_strict_identifiers(host, field, value):
    before = live(host)
    with locked(host), pytest.raises(HostError):
        stage(host, **{field:value})
    assert live(host) == before


def test_control_and_every_target_must_be_disabled(host):
    with locked(host):
        c = command(host); host.queue_managed_command(c); host.apply_managed_command(c['id'])
        with pytest.raises(HostError, match='host_append_requires_stopped'):
            stage(host)
        # Refuse inconsistent metadata as well: global disabled is insufficient.
        host._db.execute('UPDATE control SET enabled=0')
        with pytest.raises(HostError, match='host_append_requires_stopped'):
            stage(host)


@pytest.mark.parametrize('managed', [False, True])
def test_unmanaged_or_ticket_configuration_never_creates_stage_schema(tmp_path, managed):
    config = validate(configuration(tmp_path), bind_connections=True)
    with HostJournal(tmp_path/'other') as journal:
        journal.configure(config); journal.test_locks = tmp_path/'locks'
        with locked(journal):
            if managed:
                journal.prepare_managed('host', launch_catalog(config,'d'*64))
            with pytest.raises(HostError, match='host_append_(managed_required|messages_required)'):
                journal.stage_messages_append(config, expected_configuration_sha256=journal.control()['hash'],
                    expected_catalog_sha256='a'*64, idempotency_key='never-staged')
            assert not journal._db.execute("SELECT 1 FROM sqlite_master WHERE name='host_append_stages'").fetchone()


@pytest.mark.parametrize('count', [15, 16])
def test_sixteen_target_limit_in_candidate_is_not_a_live_target_install(host, count):
    config = host.control()['configuration']; config['owner_id'] = 'capacity-owner'
    template = config['targets'][0]
    for i in range(1,count):
        config['targets'].append({**template,'id':'old-'+str(i),'agent_id':'agent-'+str(i),
            'board_session_id':'session-'+str(i)})
    with HostJournal(host.directory.parent/'capacity') as capacity:
        capacity.configure(config); capacity.test_locks = host.test_locks
        with locked(capacity):
            capacity.prepare_managed('capacity-host', launch_catalog(config,'d'*64))
            if count==15:
                assert stage(capacity)['status']=='staged'
            else:
                with pytest.raises(HostError): stage(capacity)
            assert len(capacity.control()['configuration']['targets'])==count


@pytest.mark.parametrize('kind', ['dispatch','command','effect'])
def test_no_unsettled_child_command_or_effect(host, kind):
    with locked(host):
        c = command(host); host.queue_managed_command(c)
        if kind!='command':
            host.apply_managed_command(c['id'])
            if kind=='dispatch':
                host.reserve_dispatch('primary', {'id':'batch','request_id':'req','subscription_id':'sub'}, host.control()['activation'])
            host.stop()
        if kind=='effect':
            host.begin_managed_effect('pending-effect','ack_instance_command',{'host_id':'owner-host',
                'command_id':'remote-command','expected_revision':1,'idempotency_key':'effect-key'})
        with pytest.raises(HostError, match='host_append_unsettled'):
            stage(host)


def test_consumed_budgets_history_and_real_message_reader_are_untouched(host):
    with locked(host):
        c = command(host); host.queue_managed_command(c); host.apply_managed_command(c['id'])
        dispatch = host.reserve_dispatch('primary', {'id':'batch','request_id':'req','subscription_id':'sub'}, host.control()['activation'])
        host.finish_dispatch('batch','complete',{'worker_operation_id':dispatch['worker_id'],'fixture_only':True})
        host.pin_profile('primary','e'*64)
        host.save_target('primary',{'subscription_id':'old-subscription','checkpoint':17})
        host.stop()
        with MessageJournal(host.directory/'messages') as messages:
            reader = messages.reader('primary','f'*64,'now',20)
            messages.bind_feed(reader['id'],{'feed_id':'1'*32,'start_at':'now','limit':20,'human_only':True,'checkpoint':0})
        # Compare all child files without opening them through the primitive.
        child_files = {str(p):p.read_bytes() for p in (host.directory/'messages').iterdir() if p.is_file()}
        before = live(host)
        stage(host)
        assert live(host) == before
        assert host.control()['remaining'] == 3 and host.managed_target('primary')['remaining'] == 1
        assert {str(p):p.read_bytes() for p in (host.directory/'messages').iterdir() if p.is_file()} == child_files


def test_ordinary_control_stays_independent_and_old_proposal_cannot_reset_later_work(host):
    with locked(host):
        result = stage(host)
        assert not host.control()['enabled'] and len(host.control()['configuration']['targets'])==1
        c = command(host); host.queue_managed_command(c); host.apply_managed_command(c['id'])
        dispatch = host.reserve_dispatch('primary', {'id':'later-batch','request_id':'req','subscription_id':'sub'}, host.control()['activation'])
        host.finish_dispatch('later-batch','complete',{'worker_operation_id':dispatch['worker_id'],'fixture_only':True})
        host.stop()
        before = live(host)
        with pytest.raises(HostError, match='host_append_predecessor_changed'):
            stage(host)
        assert live(host)==before and host.control()['remaining']==3
        assert host.managed_target('primary')['remaining']==1
        stored = json.loads(host._db.execute('SELECT result_json FROM host_append_stages').fetchone()[0])
        assert stored==result and stored['activation_allowed'] is False


def test_cold_ambiguous_intent_is_never_resumed_or_reconstructed(host, monkeypatch):
    captured = []
    def lose(attempt):
        captured.append(attempt)
        with pytest.raises(TypeError):
            pickle.dumps(attempt)
        raise RuntimeError('synthetic loss after durable intent')
    monkeypatch.setattr(host, '_finish_messages_append_stage', lose)
    with locked(host), pytest.raises(RuntimeError):
        stage(host)
    assert captured[0].active is False and host._append_active_attempt is None
    row = dict(host._db.execute('SELECT * FROM host_append_stages').fetchone())
    assert row['state'] == 'intent' and row['result_json'] == '{}'
    with HostJournal(host.directory) as reopened:
        reopened.test_locks = host.test_locks
        with locked(reopened):
            for key in ('append-one','a-new-key'):
                with pytest.raises(HostError, match='host_append_needs_review'):
                    stage(reopened, key=key)
            forged = _StageAttempt(reopened, row['request_key'], row['id'], row['request_sha256'])
            for value in (captured[0], forged, row, row['request_key'], True):
                with pytest.raises(HostError, match='host_append_attempt_expired'):
                    reopened._finish_messages_append_stage(value)
    assert host._db.execute('SELECT state FROM host_append_stages').fetchone()[0] == 'intent'


@pytest.mark.parametrize('race', ['stop','queued_start','effect_roundtrip'])
def test_revalidation_catches_cross_connection_change_after_intent(host, monkeypatch, race):
    original = host._finish_messages_append_stage
    def changed(attempt):
        with HostJournal(host.directory) as control:
            if race=='stop': control.stop('configuration_changed')
            elif race=='queued_start': control.queue_managed_command(command(host))
            else:
                control.begin_managed_effect('racing-effect','ack_instance_command',{'host_id':'owner-host',
                    'command_id':'remote','expected_revision':1,'idempotency_key':'racing-key'})
                control.supersede_managed_effect('racing-effect')
        return original(attempt)
    monkeypatch.setattr(host, '_finish_messages_append_stage', changed)
    with locked(host), pytest.raises(HostError, match='host_append_(predecessor_changed|unsettled)'):
        stage(host)
    assert host._db.execute('SELECT state FROM host_append_stages').fetchone()[0] == 'intent'
    assert len(host.control()['configuration']['targets']) == 1


def test_completed_but_lost_return_replays_without_new_intent(host, monkeypatch):
    original = host._finish_messages_append_stage
    results = []
    def lost(attempt):
        results.append(original(attempt))
        raise RuntimeError('synthetic lost return after result commit')
    monkeypatch.setattr(host, '_finish_messages_append_stage', lost)
    with locked(host):
        with pytest.raises(RuntimeError): stage(host)
        assert stage(host) == results[0]
        assert host._db.execute('SELECT COUNT(*) FROM host_append_stages').fetchone()[0] == 1
        changed = candidate(host); changed['targets'][-1]['model'] = 'other-request'
        with pytest.raises(HostError, match='host_append_idempotency_conflict'):
            stage(host, changed)


@pytest.mark.parametrize('committed', [False, True])
def test_lost_intent_transaction_return_invalidates_attempt_even_before_finish(host, monkeypatch, committed):
    original = host._tx
    captured = []; calls = 0
    @contextmanager
    def interrupted():
        nonlocal calls
        calls += 1
        current = calls
        with original() as db:
            yield db
            if current==1:
                captured.append(host._append_active_attempt)
                if not committed:
                    raise RuntimeError('synthetic rollback before commit')
        if current==1:
            raise RuntimeError('synthetic lost commit return')
    monkeypatch.setattr(host, '_tx', interrupted)
    with locked(host):
        with pytest.raises(RuntimeError): stage(host)
        assert not captured[0].active and host._append_active_attempt is None
        with pytest.raises(HostError, match='host_append_attempt_expired'):
            host._finish_messages_append_stage(captured[0])
        if committed:
            assert host._db.execute('SELECT state FROM host_append_stages').fetchone()[0]=='intent'
            with pytest.raises(HostError, match='host_append_needs_review'):
                stage(host)
        else:
            assert not host._db.execute("SELECT 1 FROM sqlite_master WHERE name='host_append_stages'").fetchone()
            assert stage(host)['status']=='staged'


def test_attempt_is_thread_pid_acquisition_bound_and_not_reentrant(host, monkeypatch):
    original = host._finish_messages_append_stage
    def checked(attempt):
        failures = []
        def other_thread():
            try: original(attempt)
            except HostError as error: failures.append(error.code)
        thread = threading.Thread(target=other_thread); thread.start(); thread.join()
        assert failures == ['host_lock_required']
        with pytest.raises(HostError, match='host_append_busy'):
            stage(host, key='nested-call')
        old_pid = os.getpid()
        with monkeypatch.context() as m:
            m.setattr(os, 'getpid', lambda:old_pid+1)
            with pytest.raises(HostError, match='host_lock_required'):
                original(attempt)
        acquisition = host._owner_lock_token
        host._owner_lock_token = object()
        with pytest.raises(HostError, match='host_append_attempt_expired'):
            original(attempt)
        host._owner_lock_token = acquisition
        return original(attempt)
    monkeypatch.setattr(host, '_finish_messages_append_stage', checked)
    with locked(host):
        assert stage(host)['status']=='staged'
    assert host._append_active_attempt is None


@pytest.mark.parametrize('corruption', ['version','shape','partial_schema','json','result_type','oversize','state'])
def test_optional_schema_and_records_fail_closed_without_adoption(host, corruption):
    with locked(host):
        stage(host)
        if corruption=='version': host._db.execute('UPDATE host_append_stage_meta SET version=2')
        elif corruption=='shape': host._db.execute('ALTER TABLE host_append_stages ADD COLUMN unexpected TEXT')
        elif corruption=='partial_schema': host._db.execute('DROP TABLE host_append_stage_meta')
        elif corruption=='json': host._db.execute("UPDATE host_append_stages SET request_json='{' ")
        elif corruption=='result_type':
            result = json.loads(host._db.execute('SELECT result_json FROM host_append_stages').fetchone()[0])
            result['activation_allowed'] = 0
            host._db.execute('UPDATE host_append_stages SET result_json=?', (json.dumps(result,sort_keys=True,separators=(',',':')),))
        elif corruption=='oversize': host._db.execute('UPDATE host_append_stages SET request_json=?', ('x'*300000,))
        else: host._db.execute("UPDATE host_append_stages SET state='applied'")
        before = tuple(host._db.iterdump())
        with pytest.raises(HostError, match='host_append_(schema_invalid|corrupt)'):
            stage(host)
        with pytest.raises(HostError, match='host_append_(schema_invalid|corrupt)'):
            stage(host, key='another-key')
        assert tuple(host._db.iterdump()) == before


@pytest.mark.parametrize('corruption', ['configuration_hash','catalog_hash','candidate_schema','candidate_not_normalized'])
def test_rehashed_stored_proposal_still_requires_full_canonical_schema(host, corruption):
    with locked(host):
        stage(host)
        row = dict(host._db.execute('SELECT * FROM host_append_stages').fetchone())
        request = json.loads(row['request_json'])
        if corruption=='configuration_hash': request['configuration_sha256'] = 'g'*64
        elif corruption=='catalog_hash': request['catalog_sha256'] = False
        elif corruption=='candidate_schema': del request['candidate']['interval_seconds']
        else: request['candidate']['targets'][-1]['workspace'] += '/.'
        # A coherent checksum and inert receipt must not legitimize malformed data.
        encoded = _encode(request, 264192, 'fixture_invalid')
        result = _encode(host._append_result(row, request), 4096, 'fixture_invalid')
        host._db.execute('UPDATE host_append_stages SET request_json=?,request_sha256=?,result_json=?',
            (encoded, hashlib.sha256(encoded.encode()).hexdigest(), result))
        before = tuple(host._db.iterdump())
        with pytest.raises(HostError, match='host_append_corrupt'):
            host._append_schema(host._db)
        for key in ('append-one','another-key'):
            with pytest.raises(HostError, match='host_append_corrupt'):
                stage(host, key=key)
        assert tuple(host._db.iterdump()) == before


def test_historical_schema_validation_is_independent_of_current_configuration(host, monkeypatch):
    with locked(host):
        stage(host)
        row = dict(host._db.execute('SELECT * FROM host_append_stages').fetchone())
        request = json.loads(row['request_json'])
        # These valid historical pins need not match today's live predecessor.
        request['configuration_sha256'] = 'a'*64
        request['catalog_sha256'] = 'b'*64
        encoded = _encode(request, 264192, 'fixture_invalid')
        result = _encode(host._append_result(row, request), 4096, 'fixture_invalid')
        host._db.execute('UPDATE host_append_stages SET request_json=?,request_sha256=?,result_json=?',
            (encoded, hashlib.sha256(encoded.encode()).hexdigest(), result))
        before = tuple(host._db.iterdump())
        def forbidden(*args, **kwargs): raise AssertionError('No live configuration or credential reads')
        from blackoutburst_agents import host_config, message_config
        monkeypatch.setattr(host, 'control', forbidden)
        monkeypatch.setattr(host_config, 'connection_digest', forbidden)
        monkeypatch.setattr(message_config, 'connection_digest', forbidden)
        monkeypatch.setattr(message_config, '_connection_file', forbidden)
        host._append_schema(host._db)
        assert tuple(host._db.iterdump()) == before


def test_no_credentials_provider_or_process_reads_and_capacity_is_bounded(host, monkeypatch):
    import socket, subprocess
    from blackoutburst_agents import host_config, message_config
    def forbidden(*args, **kwargs): raise AssertionError('No credential/process/network access')
    monkeypatch.setattr(host_config, 'connection_digest', forbidden)
    monkeypatch.setattr(message_config, 'connection_digest', forbidden)
    monkeypatch.setattr(message_config, '_connection_file', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'socket', forbidden)
    with locked(host):
        for i in range(32): stage(host, key='bounded-'+str(i))
        with pytest.raises(HostError, match='host_append_full'):
            stage(host, key='one-too-many')
        assert len(host.control()['configuration']['targets']) == 1
