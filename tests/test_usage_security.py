"""Independent adversarial tests of owner-private, non-authoritative usage receipts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import math
import sqlite3
from threading import Barrier
import time

import pytest

from board import Board, BoardError
import board as board_module
from usage_observations import TTL_SECONDS, connection_identifier


def snapshot(*, used=12):
    return {
        'schema_version': 1, 'provider': 'codex_chatgpt',
        'source': 'codex_app_server_account_rate_limits', 'cli_version': '0.162.0-alpha.17.2',
        'data_status': 'observed', 'unavailable_reason': None, 'ordinary_usage_allowed': True,
        'buckets': [{'limit_id': 'codex', 'source_view': 'multi_bucket',
                     'primary': {'used_percent': used, 'window_minutes': 300, 'resets_at': 2000000000},
                     'secondary': None, 'reached_reason': None, 'spend_control_reached': None,
                     'normal_model_slug': None}],
    }


def unavailable():
    return {**snapshot(), 'data_status': 'unavailable', 'unavailable_reason': 'timeout',
            'ordinary_usage_allowed': None, 'buckets': []}


def rows(board, table='usage_observations'):
    assert table in ('usage_observations', 'runtime_executions', 'owner_execution_limits')
    with sqlite3.connect(board.db_path) as db:
        return db.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()


def reject(call, *args, statuses=(400, 401, 403, 409), **kwargs):
    with pytest.raises(BoardError) as error:
        call(*args, **kwargs)
    assert error.value.status in statuses
    return str(error.value)


@pytest.fixture
def usage(tmp_path):
    board = Board(tmp_path / 'board', base_url='https://board.test')
    humans = {owner: {'kind': 'human', 'owner_id': owner} for owner in ('alice', 'bob')}
    for owner, human in humans.items():
        board.ensure_owner(owner, owner.title())
        board.set_policy(human, True, True, True, True, [], [])
    project = board.create_project(humans['alice'], 'Shared', list(humans), idempotency_key='shared')
    counter = 0

    def agent(owner='alice'):
        nonlocal counter
        counter += 1
        device = board.create_device(humans[owner], f'Private {owner} device {counter}', [])
        raw = board.authenticate(device['token'])
        session_id = f'usage-{counter}'
        registration = board.register_agent(raw, session_id, f'Private {owner} agent {counter}', [], 'codex', False, 'fixture')
        actor = board.actor_session(raw, session_id)
        profile_id = registration['identity']['id']
        connection = connection_identifier(board.base_url + '/mcp', owner, profile_id, device['id'])
        return dict(actor=actor, raw=raw, session=session_id, profile_id=profile_id, device=device, connection=connection)

    return dict(board=board, humans=humans, project=project, agent=agent)


def test_connection_is_bound_to_actual_owner_profile_device_and_endpoint(usage):
    b = usage['board']
    one, two = usage['agent'](), usage['agent']('bob')
    stamp = time.time() - 1
    for connection in (
        two['connection'],
        connection_identifier('https://other-board.test/mcp', 'alice', one['profile_id'], one['device']['id']),
        connection_identifier(b.base_url + '/mcp', 'bob', one['profile_id'], one['device']['id']),
        connection_identifier(b.base_url + '/mcp', 'alice', two['profile_id'], one['device']['id']),
        connection_identifier(b.base_url + '/mcp', 'alice', one['profile_id'], two['device']['id']),
        True, None, {'connection': one['connection']},
    ):
        assert reject(b.report_usage_observation, one['actor'], connection, stamp, snapshot()) == 'usage_connection_mismatch'
    assert rows(b) == []
    own = b.report_usage_observation(one['actor'], one['connection'], stamp, snapshot())
    assert (own['owner_id'], own['agent_id'], own['device_id']) == ('alice', one['profile_id'], one['device']['id'])


def test_shared_project_does_not_share_private_usage_or_device_labels(usage):
    b = usage['board']
    one, two = usage['agent'](), usage['agent']('bob')
    for item in (one, two):
        b.report_usage_observation(item['actor'], item['connection'], time.time() - 1, snapshot())
    alice = b.get_runtime(usage['humans']['alice'])
    bob = b.get_runtime(usage['humans']['bob'])
    assert [v['connection_id'] for v in alice['usage_observations']] == [one['connection']]
    assert [v['connection_id'] for v in bob['usage_observations']] == [two['connection']]
    assert 'Private bob' not in json.dumps(alice) and two['device']['id'] not in json.dumps(alice)
    assert 'Private alice' not in json.dumps(bob) and one['profile_id'] not in json.dumps(bob)
    reject(b.get_runtime, one['actor'])
    reject(b.report_usage_observation, usage['humans']['alice'], one['connection'], time.time(), snapshot())


@pytest.mark.parametrize('change', ['owner_disabled', 'owner_requests', 'profile_paused', 'profile_archived',
                                     'profile_version', 'profile_requests', 'device_revoked', 'raw_actor', 'wrong_owner'])
def test_reporting_is_not_a_cleanup_bypass_after_authorization_change(usage, change):
    b, human = usage['board'], usage['humans']['alice']
    item = usage['agent']()
    b.report_usage_observation(item['actor'], item['connection'], time.time() - 2, snapshot())
    actor = item['actor']
    if change == 'owner_disabled':
        b.set_policy(human, False, False, False, False, [], [])
    elif change == 'owner_requests':
        b.set_policy(human, True, True, True, False, [], [])
    elif change == 'device_revoked':
        b.revoke_device(human, item['device']['id'])
    elif change == 'raw_actor':
        actor = item['raw']
    elif change == 'wrong_owner':
        actor = {**actor, 'owner_id': 'bob'}
    else:
        p = b.get_agent_profile(human, item['profile_id'])
        if change in ('profile_paused', 'profile_archived'):
            update = {'status': change.removeprefix('profile_')}
        elif change == 'profile_requests':
            update = {'policy': {**p['policy'], 'allow_requests': False}}
        else:
            update = {'instructions': 'New instruction version'}
        b.update_agent_profile(human, p['id'], p['version'], **update)
    before = rows(b)
    reject(b.report_usage_observation, actor, item['connection'], time.time() - 1, snapshot(used=34))
    assert rows(b) == before


def test_old_profile_receipt_remains_historical_until_new_dated_observation(usage):
    b, human = usage['board'], usage['humans']['alice']
    item = usage['agent']()
    stamp = time.time() - 3
    original = b.report_usage_observation(item['actor'], item['connection'], stamp, snapshot())
    profile = b.get_agent_profile(human, item['profile_id'])
    changed = b.update_agent_profile(human, profile['id'], profile['version'], instructions='Changed')
    reject(b.report_usage_observation, item['actor'], item['connection'], stamp, snapshot())
    b.register_agent(item['raw'], item['session'], 'Refresh', [], 'codex', False, 'fixture')
    same = b.report_usage_observation(item['actor'], item['connection'], stamp, snapshot())
    assert same['profile_version'] == original['profile_version']
    assert same['received_at'] == original['received_at']
    new = b.report_usage_observation(item['actor'], item['connection'], stamp + 1, snapshot())
    assert new['profile_version'] == changed['version']


def test_exact_retry_and_read_cannot_extend_ttl_or_write_database(usage, monkeypatch):
    b, item = usage['board'], usage['agent']()
    stamp = time.time() - 2
    first = b.report_usage_observation(item['actor'], item['connection'], stamp, snapshot())
    before = rows(b)
    monkeypatch.setattr(time, 'time', lambda: stamp + TTL_SECONDS + 1)
    retry = b.report_usage_observation(item['actor'], item['connection'], stamp, snapshot())
    assert retry['received_at'] == first['received_at'] and retry['expires_at'] == first['expires_at']
    assert retry['data_status'] == 'stale'
    runtime = b.get_runtime(usage['humans']['alice'])
    assert runtime['quota']['status'] == 'stale'
    assert runtime['usage_observations'][0]['snapshot']['data_status'] == 'observed'
    assert rows(b) == before


@pytest.mark.parametrize('stamp', [True, False, None, '1', 0, -1, math.nan, math.inf, -math.inf, 2**53])
def test_invalid_timestamps_do_not_write(usage, stamp):
    b, item = usage['board'], usage['agent']()
    assert reject(b.report_usage_observation, item['actor'], item['connection'], stamp, snapshot()) == 'invalid_usage_observation'
    assert rows(b) == []


def test_future_older_and_conflicting_equal_timestamp_cannot_replace_receipt(usage):
    b, item = usage['board'], usage['agent']()
    stamp = time.time() - 2
    b.report_usage_observation(item['actor'], item['connection'], stamp, snapshot())
    before = rows(b)
    assert reject(b.report_usage_observation, item['actor'], item['connection'], time.time() + 60, snapshot()) == 'usage_observation_in_future'
    for observed, value in ((stamp - 1, snapshot()), (stamp, snapshot(used=99)), (stamp, unavailable())):
        assert reject(b.report_usage_observation, item['actor'], item['connection'], observed, value) == 'usage_observation_out_of_order'
    assert rows(b) == before


def bad_snapshots():
    cases = []
    def mutate(name, edit):
        value = snapshot()
        edit(value)
        cases.append(pytest.param(value, id=name))
    mutate('top_secret', lambda v: v.update(access_token='synthetic forbidden marker'))
    mutate('unknown_schema', lambda v: v.update(schema_version=2))
    mutate('bool_schema', lambda v: v.update(schema_version=True))
    mutate('other_provider', lambda v: v.update(provider='api_key'))
    mutate('cli_log', lambda v: v.update(cli_version='version\nsynthetic secret'))
    mutate('unknown_source', lambda v: v.update(source='browser_scrape'))
    mutate('bool_disguised', lambda v: v.update(ordinary_usage_allowed=1))
    mutate('missing_windows', lambda v: v['buckets'][0].update(primary=None))
    mutate('duplicate_bucket', lambda v: v['buckets'].append(deepcopy(v['buckets'][0])))
    mutate('too_many_buckets', lambda v: v.update(buckets=[{**v['buckets'][0], 'limit_id': 'b' + str(i)} for i in range(33)]))
    mutate('bucket_secret', lambda v: v['buckets'][0].update(raw_body='synthetic forbidden marker'))
    mutate('window_secret', lambda v: v['buckets'][0]['primary'].update(email='private@example.invalid'))
    mutate('usage_above100', lambda v: v['buckets'][0]['primary'].update(used_percent=101))
    mutate('usage_negative', lambda v: v['buckets'][0]['primary'].update(used_percent=-1))
    mutate('usage_bool', lambda v: v['buckets'][0]['primary'].update(used_percent=True))
    mutate('usage_float', lambda v: v['buckets'][0]['primary'].update(used_percent=1.5))
    mutate('window_bool', lambda v: v['buckets'][0]['primary'].update(window_minutes=True))
    mutate('reset_infinite', lambda v: v['buckets'][0]['primary'].update(resets_at=math.inf))
    mutate('bucket_html', lambda v: v['buckets'][0].update(limit_id='<script>bad</script>'))
    mutate('model_control_character', lambda v: v['buckets'][0].update(normal_model_slug='model\n'))
    mutate('unknown_reason', lambda v: v['buckets'][0].update(reached_reason='raw error contains secret'))
    mutate('unhashable_reason', lambda v: v['buckets'][0].update(reached_reason=[]))
    mutate('legacy_mix', lambda v: v['buckets'].append({**v['buckets'][0], 'limit_id': None, 'source_view': 'legacy'}))
    mutate('null_multibucket', lambda v: v['buckets'][0].update(limit_id=None))
    mutate('unavailable_with_windows', lambda v: v.update(data_status='unavailable', unavailable_reason='timeout'))
    value = unavailable(); value['ordinary_usage_allowed'] = True
    cases.append(pytest.param(value, id='unavailable_claims_access'))
    return cases


@pytest.mark.parametrize('value', bad_snapshots())
def test_snapshot_is_bounded_and_rejects_private_or_ambiguous_fields(usage, value):
    b, item = usage['board'], usage['agent']()
    assert reject(b.report_usage_observation, item['actor'], item['connection'], time.time() - 1, value) == 'invalid_usage_observation'
    assert rows(b) == []


def test_unavailable_is_unknown_and_cannot_be_presented_as_zero_usage(usage):
    b, item = usage['board'], usage['agent']()
    value = b.report_usage_observation(item['actor'], item['connection'], time.time() - 1, unavailable())
    assert value['data_status'] == 'unavailable'
    assert value['snapshot']['ordinary_usage_allowed'] is None
    assert value['snapshot']['buckets'] == []
    assert b.get_runtime(usage['humans']['alice'])['quota']['status'] == 'unavailable'


def test_observations_cannot_clear_runtime_pause_failure_or_reserve_slots(usage):
    b, item, human = usage['board'], usage['agent'](), usage['humans']['alice']
    ticket = b.create_ticket(human, usage['project']['id'], 'Runtime fixture', idempotency_key='ticket')
    ticket = b.claim_ticket(item['actor'], ticket['id'], ticket['version'])
    execution = b.reserve_execution(item['actor'], 'runtime', usage['project']['id'], ticket['id'],
                                    'work', 'turn', 'codex_chatgpt', 'fixture-model')['execution']
    running = b.start_execution(item['actor'], execution['id'], execution['revision'])
    b.finish_execution(item['actor'], execution['id'], running['revision'], 'blocked', True, 'quota_or_rate_limit')
    limits = b.get_runtime(human)['limits']
    b.update_runtime_limits(human, limits['version'], 1, True)
    before_exec, before_limits = rows(b, 'runtime_executions'), rows(b, 'owner_execution_limits')
    b.report_usage_observation(item['actor'], item['connection'], time.time() - 1, snapshot(used=0))
    view = b.get_runtime(human)
    assert view['quota']['status'] == 'blocked_observed'
    assert view['quota']['reason'] == 'quota_or_rate_limit'
    assert view['limits']['paused'] and view['active_count'] == 0
    assert rows(b, 'runtime_executions') == before_exec and rows(b, 'owner_execution_limits') == before_limits


def test_two_sessions_of_binding_share_latest_observation_without_rewinding(usage):
    b, item = usage['board'], usage['agent']()
    b.register_agent(item['raw'], 'second-session', 'Same binding', [], 'codex', False, 'fixture')
    other = b.actor_session(item['raw'], 'second-session')
    stamp = time.time() - 2
    barrier = Barrier(2)
    def report(args):
        actor, date, used = args
        barrier.wait()
        try:
            return b.report_usage_observation(actor, item['connection'], date, snapshot(used=used))
        except BoardError as error:
            assert str(error) == 'usage_observation_out_of_order'
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(report, ((item['actor'], stamp, 11), (other, stamp + 1, 22))))
    value = b.get_runtime(usage['humans']['alice'])['usage_observations']
    assert len(value) == 1 and value[0]['observed_at'] == stamp + 1
    assert value[0]['snapshot']['buckets'][0]['primary']['used_percent'] == 22


def test_separate_devices_same_owner_have_distinct_records_and_restart_preserves(usage):
    b, one, two = usage['board'], usage['agent'](), usage['agent']()
    for item, used in ((one, 1), (two, 2)):
        b.report_usage_observation(item['actor'], item['connection'], time.time() - 1, snapshot(used=used))
    before = rows(b)
    restored = Board(b.data_dir, base_url=b.base_url)
    view = restored.get_runtime(usage['humans']['alice'])
    assert {r['connection_id'] for r in view['usage_observations']} == {one['connection'], two['connection']}
    assert rows(restored) == before


def test_report_does_not_publish_usage_to_shared_activity_or_agent_directories(usage):
    b, one, two = usage['board'], usage['agent'](), usage['agent']('bob')
    ticket = b.create_ticket(usage['humans']['alice'], usage['project']['id'], 'Shared ticket', idempotency_key='ticket')
    before = b.list_activity(two['actor'], project_id=usage['project']['id'])
    value = snapshot()
    value['buckets'][0]['normal_model_slug'] = 'private-usage-marker'
    b.report_usage_observation(one['actor'], one['connection'], time.time() - 1, value)
    public_views = [b.list_activity(two['actor'], project_id=usage['project']['id']),
                    b.list_agents(two['actor']), b.list_agents(usage['humans']['bob']),
                    b.list_projects(two['actor']), b.get_thread(two['actor'], ticket['thread_id']),
                    b.get_work(two['actor']), b.read_inbox(two['actor']),
                    b.inbox_snapshot(usage['humans']['bob'])]
    assert public_views[0] == before
    encoded = json.dumps(public_views)
    assert 'private-usage-marker' not in encoded and one['connection'] not in encoded
    assert 'usage_observations' not in encoded


def test_v6_migration_preserves_claims_execution_and_idempotency(usage):
    b, item, human = usage['board'], usage['agent'](), usage['humans']['alice']
    ticket = b.create_ticket(human, usage['project']['id'], 'Existing ticket', idempotency_key='old-ticket')
    claimed = b.claim_ticket(item['actor'], ticket['id'], ticket['version'])
    execution = b.reserve_execution(item['actor'], 'old-runtime', usage['project']['id'], ticket['id'],
                                    'work', 'turn', 'codex_chatgpt', 'fixture-model')['execution']
    cursor = b.read_inbox(item['actor'])['cursor']
    with sqlite3.connect(b.db_path) as db:
        db.execute('DROP TABLE message_feeds')
        db.execute('DROP TABLE ticket_attachments')
        db.execute('DROP TABLE usage_observations')
        db.execute('PRAGMA user_version=6')
        before_keys = db.execute('SELECT * FROM idempotency ORDER BY 1,2').fetchall()
        before_owners = db.execute('SELECT * FROM owners ORDER BY id').fetchall()
    reopened = Board(b.data_dir, base_url=b.base_url)
    with sqlite3.connect(b.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert db.execute('SELECT * FROM idempotency ORDER BY 1,2').fetchall() == before_keys
        assert db.execute('SELECT * FROM owners ORDER BY id').fetchall() == before_owners
    assert reopened.get_ticket(item['actor'], ticket['id'])['ticket']['claimant'] == claimed['claimant']
    assert reopened.lookup_execution(item['actor'], 'old-runtime')['execution']['id'] == execution['id']
    assert reopened.read_inbox(item['actor'], cursor=cursor)['messages'] == []
    assert reopened.get_runtime(human)['usage_observations'] == []


def test_v7_migration_failure_rolls_back_schema_and_data(usage, monkeypatch):
    b = usage['board']
    with sqlite3.connect(b.db_path) as db:
        db.execute('DROP TABLE message_feeds')
        db.execute('DROP TABLE ticket_attachments')
        db.execute('DROP TABLE usage_observations')
        db.execute('PRAGMA user_version=6')
    def database_dump():
        with sqlite3.connect(b.db_path) as db:
            return tuple(db.iterdump())
    before = database_dump()
    migrate = board_module.migrate_usage_observations
    def broken(db):
        migrate(db)
        raise RuntimeError('synthetic migration failure')
    monkeypatch.setattr(board_module, 'migrate_usage_observations', broken)
    with pytest.raises(RuntimeError, match='synthetic migration failure'):
        Board(b.data_dir, base_url=b.base_url)
    assert database_dump() == before
    with sqlite3.connect(b.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 6


def test_future_schema_refuses_without_modifying_existing_usage(usage):
    b, item = usage['board'], usage['agent']()
    b.report_usage_observation(item['actor'], item['connection'], time.time() - 1, snapshot())
    before = rows(b)
    with sqlite3.connect(b.db_path) as db:
        db.execute('PRAGMA user_version=10')
    with pytest.raises(RuntimeError, match='newer'):
        Board(b.data_dir, base_url=b.base_url)
    assert rows(b) == before
