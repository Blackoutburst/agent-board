"""Real SQLite ownership, replay, ordering, migration and failure boundaries."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import time

import pytest

from board import Board, BoardError

SHA = 'a' * 64


@pytest.fixture
def env(tmp_path):
    board = Board(tmp_path / 'board')
    board.ensure_owner('one', 'One')
    board.ensure_owner('two', 'Two')
    human = {'kind': 'human', 'owner_id': 'one'}
    board.set_policy(human, True, True, True, True, [], [])
    device = board.create_device(human, 'Owner controller', [])
    raw = board.authenticate(device['token'])
    profile = board.create_agent_profile(human, 'Responder', policy={
        'allow_read': True, 'allow_messages': True, 'allow_requests': True,
        'actions': [], 'resources': [], 'autonomy': 'requested'})
    profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'responder', 'codex')
    host = board.create_server_host(human, device['id'], 'Server', 'grant')
    entry = dict(catalog_key='target-a', profile_id=profile['id'], profile_version=profile['version'], launchspec_sha256=SHA, mode='messages')
    catalog = board.publish_server_catalog(raw, host['id'], 0, [entry], 'publish')
    return board, human, device, raw, profile, host, entry, catalog['instances'][0]


def report(env, state='stopped', cleanup=True, *, instance=None, key=None, **changes):
    b, _, _, raw, _, host, _, initial = env
    i = instance or b.poll_instance_commands(raw, host['id'])['instances'][0]
    args = dict(host_id=host['id'], instance_id=i['id'], expected_observation_revision=i['observation_revision'],
        generation=i['generation'], launchspec_sha256=i['launchspec_sha256'], command_id=i['command_id'],
        state=state, cleanup_confirmed=cleanup, reason='', idempotency_key=key or f"report-{i['observation_revision']}")
    args.update(changes)
    return b.report_instance_state(raw, **args)


def command(env, action='start', key='start', *, instance=None, **changes):
    b, human, _, raw, _, host, _, _ = env
    i = instance or b.poll_instance_commands(raw, host['id'])['instances'][0]
    args = dict(action=action, expected_version=i['version'], expected_generation=i['generation'],
        launchspec_sha256=i['launchspec_sha256'], budget=0 if action == 'stop' else 1, idempotency_key=key)
    args.update(changes)
    return b.command_server_instance(human, i['id'], **args)


def ack(env, cmd, key=None):
    b, _, _, raw, _, host, *_ = env
    return b.ack_instance_command(raw, host['id'], cmd['id'], cmd['revision'], key or 'ack-' + cmd['id'])


def test_explicit_human_device_role_and_owner_isolation(env):
    b, human, _, raw, _, host, *_ = env
    with pytest.raises(BoardError):
        b.create_server_host(raw, raw['device_id'], 'Self promotion', 'bad')
    other = {'kind': 'human', 'owner_id': 'two'}
    foreign = b.create_device(other, 'Foreign', [])
    with pytest.raises(BoardError, match='not_found'):
        b.get_server_host(other, host['id'])
    assert b.list_server_hosts(other) == {'hosts': []}
    with pytest.raises(BoardError):
        b.poll_instance_commands(b.authenticate(foreign['token']), host['id'])
    with pytest.raises(BoardError, match='exists'):
        b.create_server_host(human, raw['device_id'], 'Second', 'second')
    # Another device of the same owner is not implicitly a manager.
    unbound = b.create_device(human, 'Ordinary agent', [])
    with pytest.raises(BoardError, match='not_found'):
        b.poll_instance_commands(b.authenticate(unbound['token']), host['id'])


def test_initial_unknown_cannot_start_then_full_lifecycle(env):
    b, _, _, raw, _, host, *_ = env
    with pytest.raises(BoardError, match='not_settled'):
        command(env)
    clean = report(env)['instance']
    start = command(env, instance=clean)
    poll = b.poll_instance_commands(raw, host['id'])
    assert poll['commands'] == [start] and poll['instances'][0]['generation'] == 1
    accepted = ack(env, start)
    assert accepted['command']['status'] == 'accepted'
    # ACK stays redeliverable until actual observation.
    assert b.poll_instance_commands(raw, host['id'])['commands'][0]['status'] == 'accepted'
    with pytest.raises(BoardError, match='not_settled'):
        command(env, key='double')
    ready = report(env, 'ready', False)
    assert ready['command']['status'] == 'complete'
    assert b.poll_instance_commands(raw, host['id'])['commands'] == []
    stop = command(env, 'stop', 'stop')
    ack(env, stop)
    done = report(env)
    assert done['instance']['desired_state'] == 'stopped' and done['command']['status'] == 'complete'
    assert done['instance']['generation'] == 1


def test_command_and_report_replay_preserve_receipts_not_budget_or_presence(env):
    b, _, _, raw, _, host, *_ = env
    r = report(env, key='initial')
    seen = b.get_server_host(env[1], host['id'])['instances'][0]['last_seen_at']
    time.sleep(.001)
    assert report(env, instance=env[-1], key='initial') == r
    assert b.get_server_host(env[1], host['id'])['instances'][0]['last_seen_at'] == seen
    cmd = command(env, instance=r['instance'])
    assert command(env, instance=r['instance']) == cmd
    assert ack(env, cmd) == ack(env, cmd)
    with pytest.raises(BoardError, match='idempotency'):
        command(env, instance=r['instance'], budget=2)
    with b._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM server_instance_commands').fetchone()[0] == 1


def test_stop_preempts_unstarted_and_acknowledged_start_even_on_ack_retry(env):
    b, _, _, raw, _, host, *_ = env
    report(env)
    start = command(env)
    ack(env, start, 'first-ack')
    stop = command(env, 'stop', 'stop')
    with pytest.raises(BoardError, match='not_current'):
        ack(env, start, 'first-ack')
    assert b.poll_instance_commands(raw, host['id'])['commands'] == [stop]
    with b._connection() as db:
        assert db.execute('SELECT status FROM server_instance_commands WHERE id=?', (start['id'],)).fetchone()[0] == 'superseded'
    ack(env, stop)
    report(env)


def test_report_cannot_claim_success_before_ack_or_wrong_identity(env):
    report(env)
    start = command(env)
    with pytest.raises(BoardError, match='not_accepted'):
        report(env, 'ready', False)
    ack(env, start)
    for changes in ({'generation': 0}, {'launchspec_sha256': 'b' * 64}, {'command_id': None}, {'expected_observation_revision': 0}):
        with pytest.raises(BoardError, match='changed'):
            report(env, 'ready', False, **changes)
    with pytest.raises(BoardError, match='cleanup'):
        report(env, 'ready', True)


def test_restart_requires_stop_observation_and_never_resets_generation(env):
    report(env); start = command(env); ack(env, start); report(env, 'ready', False)
    restart = command(env, 'restart', 'restart')
    assert restart['generation'] == 2
    ack(env, restart)
    with pytest.raises(BoardError, match='cleanup_required'):
        report(env, 'ready', False)
    intermediate = report(env)
    assert intermediate['command']['status'] == 'accepted' and intermediate['command']['stop_confirmed']
    assert report(env, 'starting', False)['command']['status'] == 'accepted'
    assert report(env, 'ready', False)['command']['status'] == 'complete'


def test_clean_process_but_unknown_effect_stays_review_and_blocks_restart(env):
    report(env); cmd = command(env); ack(env, cmd)
    observation = report(env, 'needs_review', True, reason='cleanup_unconfirmed')
    assert observation['instance']['cleanup_confirmed']
    for action in ('start', 'restart'):
        with pytest.raises(BoardError, match='not_settled'):
            command(env, action, 'bad-' + action)
    stop = command(env, 'stop', 'human-stop'); ack(env, stop)
    # A process stop observation without settled effects cannot finish the stop.
    assert report(env, 'needs_review', True)['command']['status'] == 'accepted'


def test_work_revocation_allows_poll_stop_cleanup_not_publish_or_launch(env):
    b, human, _, raw, profile, host, entry, _ = env
    report(env); cmd = command(env); ack(env, cmd)
    b.update_agent_profile(human, profile['id'], profile['version'], status='paused')
    b.set_policy(human, False, False, False, False, [], [])
    poll = b.poll_instance_commands(raw, host['id'])
    assert poll['enabled'] is False and not poll['instances'][0]['can_start']
    with pytest.raises(BoardError, match='not_authorized'):
        ack(env, cmd)  # Cached ACK must not revive launch authority.
    with pytest.raises(BoardError, match='not_authorized'):
        report(env, 'ready', False)
    with pytest.raises(BoardError, match='not_authorized'):
        b.publish_server_catalog(raw, host['id'], 1, [entry], 'new')
    stop = command(env, 'stop', 'stop-after-revoke'); ack(env, stop)
    assert report(env)['command']['status'] == 'complete'


def test_host_revocation_is_durable_stop_and_device_revocation_absolute(env):
    b, human, device, raw, _, host, *_ = env
    report(env); cmd = command(env)
    revoked = b.revoke_server_host(human, host['id'], 1, 'revoke')
    assert not revoked['enabled'] and revoked['version'] == 2
    assert b.revoke_server_host(human, host['id'], 1, 'revoke') == revoked
    poll = b.poll_instance_commands(raw, host['id'])
    assert len(poll['commands']) == 1 and poll['commands'][0]['action'] == 'stop'
    ack(env, poll['commands'][0]); report(env)
    with b._tx() as db:
        db.execute('UPDATE devices SET revoked_at=? WHERE id=?', (time.time(), device['id']))
    with pytest.raises(BoardError, match='unavailable'):
        b.poll_instance_commands(raw, host['id'])


def test_two_simultaneous_starts_only_one_commits(env):
    report(env)
    i = env[0].poll_instance_commands(env[3], env[5]['id'])['instances'][0]
    def attempt(key):
        try: return command(env, instance=i, key=key)
        except BoardError as e: return e
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, ['left', 'right']))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, BoardError) for r in results) == 1


def test_reopen_replays_accepted_command_without_relaunch_or_reset(env):
    b, _, _, raw, _, host, *_ = env
    report(env); cmd = command(env); accepted = ack(env, cmd)
    reopened = Board(b.data_dir)
    poll = reopened.poll_instance_commands(raw, host['id'])
    assert poll['commands'] == [accepted['command']]
    assert poll['instances'][0]['generation'] == 1
    with reopened._connection() as db:
        assert db.execute('SELECT COUNT(*) FROM runtime_executions').fetchone()[0] == 0


def test_catalog_changes_require_stopped_and_reset_old_observation(env):
    b, _, _, raw, _, host, entry, _ = env
    with pytest.raises(BoardError, match='not_settled'):
        b.publish_server_catalog(raw, host['id'], 1, [], 'remove-unknown')
    report(env)
    updated = b.publish_server_catalog(raw, host['id'], 1, [{**entry, 'launchspec_sha256': 'b' * 64}], 'new-source')
    assert updated['catalog_version'] == 2
    assert updated['instances'][0]['observed_state'] == 'unknown'
    assert not updated['instances'][0]['cleanup_confirmed']
    with pytest.raises(BoardError, match='changed'):
        b.publish_server_catalog(raw, host['id'], 1, [entry], 'stale')


@pytest.mark.parametrize('entries', [None, {}, '[]', [None], [{}], [dict(catalog_key='a', profile_id='b',profile_version=True,launchspec_sha256=SHA,mode='messages')]])
def test_strict_catalog_shapes(env, entries):
    with pytest.raises(BoardError):
        env[0].publish_server_catalog(env[3], env[5]['id'], 1, entries, 'bad')


@pytest.mark.parametrize('value', [True, 1.0, '1', None, -1, 2**63])
def test_strict_numeric_fields(env, value):
    with pytest.raises(BoardError):
        command(env, expected_generation=value)


def test_no_secret_or_paths_in_catalog_payload_and_fixed_errors(env):
    b, _, _, raw, _, host, entry, _ = env
    sentinel = 'SENTINEL_PRIVATE_TOKEN_AND_PATH'
    with pytest.raises(BoardError) as error:
        b.publish_server_catalog(raw, host['id'], 1, [{**entry, 'connection_file': sentinel}], 'bad')
    assert sentinel not in str(error.value)
    assert sentinel not in json.dumps(b.get_server_host(env[1], host['id']))


def test_schema13_to14_preserves_accounts_profiles_and_old_tables(env):
    b, human, device, raw, profile, host, *_ = env
    before = b.get_agent_profile(human, profile['id'])
    # Genuine schema13 shape: remove only the new tables/indexes and rewind tag.
    with b._tx() as db:
        for name in ('server_control_receipts','server_instance_commands','server_instances','server_hosts'):
            db.execute('DROP TABLE ' + name)
        db.execute('PRAGMA user_version=13')
    reopened = Board(b.data_dir)
    assert reopened.get_agent_profile(human, profile['id']) == before
    assert reopened.authenticate(device['token']) == raw
    assert reopened.list_server_hosts(human) == {'hosts': []}
    with reopened._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 14
    assert Board(b.data_dir).list_server_hosts(human) == {'hosts': []}


def test_catalog_profile_owner_binding_version_and_authority_are_current(env):
    b, human, device, raw, profile, host, entry, _ = env
    report(env)
    for index, bad in enumerate(({**entry, 'profile_id': 'absent'}, {**entry, 'profile_version': profile['version'] + 1})):
        with pytest.raises(BoardError):
            b.publish_server_catalog(raw, host['id'], 1, [bad], 'bad-' + str(index))
    current = b.get_agent_profile(human, profile['id'])
    b.update_agent_profile(human, profile['id'], current['version'], instructions='Edited instructions')
    # Merely returning profile status to active does not restore an old version.
    with pytest.raises(BoardError, match='not_authorized'):
        command(env)
    poll = b.poll_instance_commands(raw, host['id'])
    assert poll['instances'][0]['control_reason'] == 'configuration_changed'
    with b._tx() as db:
        db.execute('DELETE FROM agent_profile_bindings WHERE profile_id=?', (profile['id'],))
    newer = b.get_agent_profile(human, profile['id'])
    with pytest.raises(BoardError, match='not_authorized'):
        b.publish_server_catalog(raw, host['id'], 1, [{**entry, 'profile_version': newer['version']}], 'unbound')


def test_revoked_device_also_prevents_human_start_and_marks_host_unavailable(env):
    b, human, device, raw, _, host, *_ = env
    clean = report(env)['instance']
    with b._tx() as db:
        db.execute('UPDATE devices SET revoked_at=? WHERE id=?', (time.time(), device['id']))
    with pytest.raises(BoardError, match='not_authorized'):
        command(env, instance=clean)
    assert not b.get_server_host(human, host['id'])['enabled']
    assert not b.get_server_host(human, host['id'])['instances'][0]['can_start']
    # Human stop remains possible even when no authenticated daemon can ACK it.
    assert command(env, 'stop', 'stop-revoked-device', instance=clean)['action'] == 'stop'


def test_report_cas_is_independent_of_control_cas_and_does_not_erase_new_command(env):
    b, _, _, raw, _, host, *_ = env
    initial = report(env)['instance']
    report(env, instance=initial, key='heartbeat')
    # The heartbeat increments observation_revision, not control.version.
    cmd = command(env, instance=initial)
    old_observation = b.poll_instance_commands(raw, host['id'])['instances'][0]
    ack(env, cmd)
    stop = command(env, 'stop', 'newer-stop')
    with pytest.raises(BoardError, match='changed'):
        report(env, 'ready', False, instance=old_observation)
    assert b.poll_instance_commands(raw, host['id'])['commands'][0]['id'] == stop['id']


def test_catalog_duplicate_keys_profiles_overflow_and_no_partial_write(env):
    b, human, _, raw, _, host, entry, _ = env
    before = b.get_server_host(human, host['id'])
    for entries in ([entry, entry], [entry, {**entry, 'catalog_key': 'other'}], [entry] * 17,
                    [{**entry, 'catalog_key': '\ud800'}], [{**entry, 'catalog_key': 'x' * 10000}]):
        with pytest.raises(BoardError):
            b.publish_server_catalog(raw, host['id'], 1, entries, 'invalid')
        assert b.get_server_host(human, host['id']) == before


def test_catalog_remove_reactivate_requires_new_clean_observation(env):
    b, human, _, raw, _, host, entry, _ = env
    clean = report(env)['instance']
    removed = b.publish_server_catalog(raw, host['id'], 1, [], 'remove')
    assert removed['instances'] == []
    with pytest.raises(BoardError):
        command(env, instance=clean)
    readded = b.publish_server_catalog(raw, host['id'], 2, [entry], 'readd')['instances'][0]
    assert readded['id'] == clean['id'] and readded['observed_state'] == 'unknown'
    with pytest.raises(BoardError, match='not_settled'):
        command(env, instance=readded, key='start-readded')


def test_sixteen_distinct_catalog_instances_poll_stays_bounded_and_complete(env):
    b, human, device, raw, profile, host, entry, _ = env
    entries = [entry]
    for index in range(1, 16):
        p = b.create_agent_profile(human, 'Responder ' + str(index), policy={
            'allow_read': True, 'allow_messages': True, 'allow_requests': False,
            'actions': [], 'resources': [], 'autonomy': 'responses'})
        p = b.bind_agent_profile(human, p['id'], p['version'], device['id'], 'id-' + str(index), 'codex')
        entries.append({**entry, 'catalog_key': 'a\U0001f600' * 30 + str(index), 'profile_id': p['id'], 'profile_version': p['version']})
    b.publish_server_catalog(raw, host['id'], 1, entries, 'sixteen')
    value = b.poll_instance_commands(raw, host['id'])
    assert len(value['instances']) == 16
    assert len(json.dumps(value, ensure_ascii=True).encode('ascii')) < 65536
    assert value['commands'] == []


def test_transaction_failure_does_not_leave_half_command_or_new_generation(env, monkeypatch):
    b, human, _, raw, _, host, *_ = env
    clean = report(env)['instance']
    original = b._server_remember
    def fail_after_write(*args, **kwargs):
        raise RuntimeError('simulated lost durable write')
    monkeypatch.setattr(b, '_server_remember', fail_after_write)
    with pytest.raises(RuntimeError):
        command(env, instance=clean)
    monkeypatch.setattr(b, '_server_remember', original)
    poll = b.poll_instance_commands(raw, host['id'])
    assert poll['commands'] == [] and poll['instances'][0]['generation'] == 0
    assert command(env, instance=clean)['generation'] == 1


def test_reports_never_clear_existing_runtime_capacity(env):
    b, human, device, raw, profile, host, *_ = env
    # Reserve an actual message Runtime from the same owner/profile/device.
    project = b.create_project(human, 'Project', ['one'], idempotency_key='runtime-project')
    ticket = b.create_ticket(human, project['id'], 'Source', idempotency_key='runtime-source')
    b.update_agent_profile(human, profile['id'], profile['version'], project_ids=[project['id']])
    b.register_agent(raw, 'message-session', 'Responder', [], 'codex', False, 'responder')
    actor = {**raw, 'session_id': 'message-session'}
    thread = {'id': ticket['thread_id']}
    message = b.post_message(human, thread['id'], 'Addressed source', mentions=['agent:' + profile['id']], idempotency_key='source')
    source = b.get_message(actor, thread['id'], message['id'])
    result = b.reserve_message_execution(actor, 'reserve', thread['id'], message['id'], source['source_sha256'], 'work', 'turn', 'codex_chatgpt', 'fixture')
    assert result['reserved'] and b.get_runtime(human)['active_count'] == 1
    report(env)
    assert b.get_runtime(human)['active_count'] == 1
    b.revoke_server_host(human, host['id'], 1, 'revoke')
    assert b.get_runtime(human)['active_count'] == 1


def test_new_host_cannot_forget_unsettled_old_instance(env):
    b, human, _, raw, _, host, *_ = env
    b.revoke_server_host(human, host['id'], 1, 'revoke')
    new_device = b.create_device(human, 'Replacement', [])
    with pytest.raises(BoardError, match='previous_host_not_settled'):
        b.create_server_host(human, new_device['id'], 'Replacement', 'new-grant')
    stop = b.poll_instance_commands(raw, host['id'])['commands'][0]
    ack(env, stop); report(env)
    replacement = b.create_server_host(human, new_device['id'], 'Replacement', 'new-grant')
    assert replacement['id'] != host['id'] and replacement['catalog_version'] == 0


def test_runtime_pause_is_authority_not_state_and_can_start_survives_busy(env):
    b, human, _, raw, _, host, *_ = env
    report(env); cmd = command(env); ack(env, cmd); busy = report(env, 'busy', False)
    assert busy['instance']['can_start'] is True
    limits = b.get_runtime(human)['limits']
    b.update_runtime_limits(human, limits['version'], limits['max_concurrent'], True)
    assert b.poll_instance_commands(raw, host['id'])['instances'][0]['can_start'] is False
    stop = command(env, 'stop', 'pause-stop'); ack(env, stop); report(env)


def test_old_completed_command_cannot_become_new_catalog_launch_authority(env):
    b, human, _, raw, profile, host, entry, _ = env
    report(env); cmd = command(env); ack(env, cmd); report(env, 'ready', False)
    # The old activation is now stopped and all effects settled.
    report(env, key='old-activation-stopped')
    changed = b.update_agent_profile(human, profile['id'], profile['version'], description='New version')
    catalog = b.publish_server_catalog(raw, host['id'], 1, [{**entry, 'profile_version': changed['version']}], 'new-version')
    assert catalog['instances'][0]['current_command']['instance_version'] < catalog['instances'][0]['version']
    with pytest.raises(BoardError, match='not_authorized'):
        report(env, 'ready', False, key='stale-success')
    report(env, key='new-catalog-clean')
    new = command(env, key='new-activation'); ack(env, new)
    assert report(env, 'ready', False, key='new-success')['command']['status'] == 'complete'


def test_refused_start_settled_report_closes_only_that_command(env):
    b, _, _, raw, _, host, *_ = env
    report(env); cmd = command(env); ack(env, cmd)
    refused = report(env, reason='launch_refused')
    assert refused['command']['status'] == 'rejected'
    assert b.poll_instance_commands(raw, host['id'])['commands'] == []
    assert command(env, key='explicit-new-start')['generation'] == 2


@pytest.mark.parametrize('terminal', ['stopped', 'needs_review', 'unknown'])
def test_observation_cannot_resurrect_completed_or_uncertain_activation(env, terminal):
    report(env); cmd = command(env); ack(env, cmd); report(env, 'ready', False)
    report(env, terminal, terminal != 'unknown', key='terminal-observation')
    for state in ('starting', 'ready', 'busy'):
        with pytest.raises(BoardError, match='not_authorized'):
            report(env, state, False, key='resurrect-' + state)
    stop = command(env, 'stop', 'settle'); ack(env, stop); report(env, key='settled')
    new = command(env, key='explicit-start'); ack(env, new)
    assert report(env, 'ready', False, key='explicit-ready')['command']['status'] == 'complete'


def test_schema14_migration_failure_is_atomic_with_no_partial_role(env, monkeypatch):
    import board as board_module
    b, *_ = env
    with b._tx() as db:
        for name in ('server_control_receipts','server_instance_commands','server_instances','server_hosts'):
            db.execute('DROP TABLE ' + name)
        db.execute('PRAGMA user_version=13')
    original = board_module.migrate_server_instances
    def fail(db):
        original(db)
        raise RuntimeError('synthetic schema14 write failure')
    monkeypatch.setattr(board_module, 'migrate_server_instances', fail)
    with pytest.raises(RuntimeError):
        Board(b.data_dir)
    with b._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 13
        assert db.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'server_%'").fetchone()[0] == 0
    monkeypatch.setattr(board_module, 'migrate_server_instances', original)
    assert Board(b.data_dir).list_server_hosts(env[1]) == {'hosts': []}
