"""Exact queued Stop observations using real owner locks/SQLite, no service/RPC."""
import json
from unittest.mock import patch

import pytest

from blackoutburst_agents.host_config import HostError, digest
from blackoutburst_agents.message_stopped_controls import stopped_control_rows
import blackoutburst_agents.message_stopped_controls as module
from test_managed_host import host, prepared, command, locked


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def snapshot(host):
    return tuple(host._db.iterdump())


def stop(host, **changes):
    value = command(action='stop', identity='stop-1', gen=1)
    value.update(changes)
    host.queue_managed_command(value)
    return value


def ack(host, stop, identity='ack-stop'):
    args = {'host_id': 'host-identity', 'command_id': stop['id'],
        'expected_revision': 1, 'idempotency_key': identity}
    host.begin_managed_effect(identity, 'ack_instance_command', args)
    return args


def report(host, stop, identity='report-stop'):
    args = {'host_id': 'host-identity', 'instance_id': stop['instance_id'],
        'expected_observation_revision': 3, 'generation': stop['generation'],
        'launchspec_sha256': stop['launchspec_sha256'], 'command_id': stop['id'],
        'state': 'stopped', 'cleanup_confirmed': True, 'reason': 'stopped', 'idempotency_key': identity}
    host.begin_managed_effect(identity, 'report_instance_state', args)
    return args


def replace_json(host, table, column, identity, value):
    host._db.execute('UPDATE '+table+' SET '+column+'=?,payload_sha256=? WHERE id=?',
        (canonical(value), digest(value), identity))


def assert_refused_unchanged(host):
    before = snapshot(host)
    with pytest.raises(HostError, match='^stopped_controls_'):
        stopped_control_rows(host)
    assert snapshot(host) == before


def test_requires_actual_owner_lock(host):
    with locked(host): prepared(host)
    with pytest.raises(HostError, match='stopped_controls_lock'):
        stopped_control_rows(host)


def test_empty_and_exact_stops_are_readonly_raw_snapshots_inside_caller_transaction(host):
    with locked(host):
        prepared(host)
        assert stopped_control_rows(host) == {'commands': [], 'effects': []}
        value = stop(host); ack(host, value); report(host, value)
        before = snapshot(host)
        host._db.execute('BEGIN IMMEDIATE')
        try:
            with patch.object(host, 'control', side_effect=AssertionError('No config path reads')), \
                    patch('subprocess.Popen', side_effect=AssertionError('No process')), \
                    patch('socket.socket', side_effect=AssertionError('No network')):
                result = stopped_control_rows(host)
            assert host._db.in_transaction
            assert result['commands'] == [dict(row) for row in host._db.execute("SELECT * FROM managed_commands WHERE state='queued' ORDER BY id")]
            assert result['effects'] == [dict(row) for row in host._db.execute("SELECT * FROM managed_effects WHERE state='pending' ORDER BY id")]
            assert snapshot(host) == before
        finally: host._db.execute('ROLLBACK')
        assert snapshot(host) == before


def test_historical_terminal_rows_are_preserved_and_no_unbound_target_is_authority(host):
    with locked(host):
        prepared(host)
        old = stop(host); host.apply_managed_command(old['id'])
        ack(host, old); host.supersede_managed_effect('ack-stop')
        before = snapshot(host)
        assert stopped_control_rows(host) == {'commands': [], 'effects': []}
        assert snapshot(host) == before
        host._db.execute("UPDATE managed_targets SET instance_id='' WHERE target_id='primary'")
        assert stopped_control_rows(host) == {'commands': [], 'effects': []}


@pytest.mark.parametrize(('key','value'), [('action','start'), ('action','restart'), ('budget',True),
    ('budget',1), ('generation',True), ('generation',1.0), ('instance_id','foreign'),
    ('launchspec_sha256','f'*64), ('target_id','missing'), ('id','foreign')])
def test_wrong_command_cannot_be_rehashed_into_a_stop(host,key,value):
    with locked(host):
        prepared(host); payload=stop(host); payload[key]=value
        replace_json(host,'managed_commands','payload','stop-1',payload)
        assert_refused_unchanged(host)


@pytest.mark.parametrize('kind',['duplicate','bad-hash','unknown-state','noncanonical','duplicate-json-key','too-large','stale'])
def test_ambiguous_stale_or_malformed_command_rows_refuse(host,kind):
    with locked(host):
        prepared(host); payload=stop(host)
        if kind=='duplicate':
            other={**payload,'id':'stop-2'}
            host._db.execute("INSERT INTO managed_commands VALUES(?,?,?,?,?,?,'queued')",
                (other['id'],canonical(other),digest(other),other['target_id'],other['generation'],other['action']))
        elif kind=='bad-hash':host._db.execute("UPDATE managed_commands SET payload_sha256=?",('f'*64,))
        elif kind=='unknown-state':host._db.execute("UPDATE managed_commands SET state='needs_review'")
        elif kind=='noncanonical':host._db.execute('UPDATE managed_commands SET payload=?',(json.dumps(payload,indent=2),))
        elif kind=='duplicate-json-key':host._db.execute('UPDATE managed_commands SET payload=?',(canonical(payload)[:-1]+',"budget":0}',))
        elif kind=='too-large':host._db.execute('UPDATE managed_commands SET payload=?',('x'*4097,))
        else:host._db.execute('UPDATE managed_targets SET generation=2')
        assert_refused_unchanged(host)


@pytest.mark.parametrize(('key','value'), [('host_id','foreign'),('command_id','foreign'),
    ('instance_id','foreign'),('generation',2),('launchspec_sha256','f'*64),
    ('state','needs_review'),('state','ready'),('cleanup_confirmed',False),('cleanup_confirmed',1)])
def test_pending_report_is_only_positive_stopped_data_for_the_same_exact_stop(host,key,value):
    with locked(host):
        prepared(host); payload=stop(host); args=report(host,payload);args[key]=value
        replace_json(host,'managed_effects','arguments','report-stop',args)
        assert_refused_unchanged(host)


@pytest.mark.parametrize('kind',['orphan-ack','duplicate-ack','publish','result','unknown-state','bad-hash'])
def test_unrelated_or_ambiguous_effects_cannot_be_ignored(host,kind):
    with locked(host):
        prepared(host); payload=stop(host); args=ack(host,payload)
        if kind=='orphan-ack':
            args['command_id']='missing';replace_json(host,'managed_effects','arguments','ack-stop',args)
        elif kind=='duplicate-ack':ack(host,payload,'another-ack')
        elif kind=='publish':host._db.execute("UPDATE managed_effects SET method='publish_server_catalog'")
        elif kind=='result':host._db.execute("UPDATE managed_effects SET result='[]'")
        elif kind=='unknown-state':host._db.execute("UPDATE managed_effects SET state='needs_review'")
        else:host._db.execute('UPDATE managed_effects SET payload_sha256=?',('f'*64,))
        assert_refused_unchanged(host)


@pytest.mark.parametrize('kind',['trigger','temp-trigger','shadow','marker','bound'])
def test_schema_and_response_bounds_refuse_without_rolling_back_caller_transaction(host,kind):
    with locked(host):
        prepared(host); stop(host)
        if kind=='trigger':host._db.execute('CREATE TRIGGER unexpected AFTER UPDATE ON managed_commands BEGIN SELECT 1;END')
        elif kind=='temp-trigger':host._db.execute('CREATE TEMP TRIGGER unexpected AFTER UPDATE ON managed_effects BEGIN SELECT 1;END')
        elif kind=='shadow':host._db.execute('CREATE TEMP VIEW managed_commands AS SELECT * FROM main.managed_commands')
        elif kind=='marker':host._db.execute('PRAGMA user_version=1')
        host._db.execute('BEGIN IMMEDIATE')
        try:
            if kind=='bound':
                with patch.object(module,'MAX_OUTPUT',1):assert_refused_unchanged(host)
            else:assert_refused_unchanged(host)
            assert host._db.in_transaction
        finally:host._db.execute('ROLLBACK')
