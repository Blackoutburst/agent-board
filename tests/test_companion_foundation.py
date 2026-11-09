"""Companion identity, migration and transport fences using real local Board data."""
import json
import sqlite3

import pytest

from backup import create_backup, restore_backup
from board import Board, BoardError
from owner_powers import OWNER_POWERS, has_owner_power
from profile_refresh import _compatible


@pytest.fixture
def setup(tmp_path):
    board = Board(tmp_path / 'board')
    for owner in ('alice', 'bob'):
        board.ensure_owner(owner, owner.title())
        board.set_policy({'kind': 'human', 'owner_id': owner}, True, True, True, True, ['inspect'], ['repo:test'])
    human = {'kind': 'human', 'owner_id': 'alice'}
    project = board.create_project(human, 'Alice project', ['alice'], idempotency_key='project')
    return board, human, project


def running(board, human, profile, session='companion-session'):
    device = board.create_device(human, session, [])
    profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], session, 'codex')
    raw = board.authenticate(device['token'])
    board.register_agent(raw, session, session, [], 'codex', False, session)
    return profile, device, raw, board.actor_session(raw, session)


def ordinary_policy():
    return dict(allow_read=True, allow_messages=True, allow_requests=True,
                actions=['inspect'], resources=['repo:test'], autonomy='queue')


def test_one_companion_per_owner_is_human_created_and_archival_keeps_identity(setup):
    board, human, _ = setup
    first = board.create_agent_profile(human, 'Companion', kind='companion', idempotency_key='create')
    assert board.create_agent_profile(human, 'Companion', kind='companion', idempotency_key='create') == first
    assert first['kind'] == 'companion' and first['acts_for'] == human['owner_id']
    assert first['project_ids'] is None and first['legacy'] is False
    assert first['policy'] == dict(allow_read=True, allow_messages=True, allow_requests=True,
        actions=[], resources=[], autonomy='requested', allow_create_projects=False)
    snapshot = board.get_agent_profile_version(human, first['id'], 1)
    assert snapshot['kind'] == 'companion' and snapshot['acts_for'] == human['owner_id']
    with board._connection() as db:
        saved = json.loads(db.execute('SELECT snapshot FROM agent_profile_versions WHERE profile_id=?', (first['id'],)).fetchone()[0])
    assert saved['kind'] == 'companion' and saved['acts_for'] == human['owner_id']
    archived = board.update_agent_profile(human, first['id'], first['version'], status='archived')
    with pytest.raises(BoardError):
        board.create_agent_profile(human, 'Replacement', kind='companion', idempotency_key='replacement')
    resumed = board.update_agent_profile(human, first['id'], archived['version'], status='active')
    assert resumed['id'] == first['id']
    bob = board.create_agent_profile({'kind': 'human', 'owner_id': 'bob'}, 'Bob companion', kind='companion')
    assert bob['acts_for'] == 'bob' and bob['id'] != first['id']
    ordinary = board.create_agent_profile(human, 'Ordinary')
    with pytest.raises(sqlite3.IntegrityError):
        with board._tx() as db:
            db.execute("UPDATE agent_profiles SET kind='companion' WHERE id=?", (ordinary['id'],))
    assert board.get_agent_profile(human, ordinary['id'])['kind'] == 'agent'


def test_companion_transport_marker_and_authorship_are_sql_derived(setup):
    board, human, project = setup
    profile = board.create_agent_profile(human, 'Personal companion', kind='companion', project_ids=[])
    profile, _, raw, actor = running(board, human, profile)
    assert actor['kind'] == 'agent' and actor['companion_for'] == human['owner_id']
    assert OWNER_POWERS == {'dispatch', 'answer_requests', 'accept_reviews', 'manage_agents'}
    for power in OWNER_POWERS:
        assert has_owner_power(actor, power) and has_owner_power(actor, power, 'alice')
        assert not has_owner_power(actor, power, 'bob')
    assert not has_owner_power(actor, 'create_agent')
    assert not has_owner_power(actor, 'dispatch', '')
    assert board.get_project(actor, project['id'])['id'] == project['id']
    foreign = board.create_project({'kind': 'human', 'owner_id': 'bob'}, 'Bob private', ['bob'])
    with pytest.raises(BoardError):
        board.get_project(actor, foreign['id'])
    ticket = board.create_ticket(human, project['id'], 'Authorship')
    message = board.post_message(actor, ticket['thread_id'], 'Companion reply', idempotency_key='reply')
    author = message['author']
    assert author['kind'] == 'agent' and author['profile_kind'] == 'companion'
    assert author['companion_for'] == author['acts_for'] == 'alice'
    assert author['agent_id'] == profile['id'] and author['agent_name'] == 'Personal companion'
    assert author['display_name'] == 'Alice'
    spoofed = {**raw, 'companion_for': 'bob', 'acts_for': 'bob', 'profile_kind': 'companion'}
    assert board.actor_session(spoofed, actor['session_id'])['companion_for'] == 'alice'


def test_ordinary_actor_cannot_spoof_companion_or_change_profile_kind(setup):
    board, human, project = setup
    profile = board.create_agent_profile(human, 'Ordinary', policy=ordinary_policy(), project_ids=[project['id']])
    profile, _, raw, actor = running(board, human, profile, 'ordinary-session')
    spoofed = board.actor_session({**raw, 'companion_for': 'alice', 'acts_for': 'alice', 'profile_kind': 'companion'}, actor['session_id'])
    assert 'companion_for' not in spoofed and not has_owner_power(spoofed, 'dispatch')
    with pytest.raises(BoardError):
        board.update_agent_profile(human, profile['id'], profile['version'], kind='companion')
    with pytest.raises(BoardError):
        board.create_agent_profile(actor, 'Unauthorized companion', kind='companion')
    companion = board.create_agent_profile(human, 'Companion', kind='companion')
    _, _, _, companion_actor = running(board, human, companion)
    for action in (
        lambda: board.create_agent_profile(companion_actor, 'Another'),
        lambda: board.duplicate_agent_profile(companion_actor, profile['id'], profile['version']),
        lambda: board.create_managed_agent_profile(companion_actor, 'Another', 'no-create'),
        lambda: board.update_managed_agent_profile(companion_actor, profile['id'], profile['version'], 'no-edit', policy=ordinary_policy()),
        lambda: board.archive_managed_agent_profile(companion_actor, profile['id'], profile['version'], 'no-archive'),
    ):
        with pytest.raises(BoardError):
            action()


@pytest.mark.parametrize('permission,index', [('allow_read', 1), ('allow_reply', 2), ('allow_requests', 3)])
def test_owner_permission_revocation_still_fences_companions(setup, permission, index):
    board, human, _ = setup
    profile = board.create_agent_profile(human, 'Companion', kind='companion')
    _, _, _, actor = running(board, human, profile)
    switches = [True, True, True, True]
    switches[index] = False
    board.set_policy(human, *switches, ['inspect'], ['repo:test'])
    with pytest.raises(BoardError):
        with board._connection() as db:
            board._actor(db, actor, permission)


def test_binding_pause_and_version_fences_apply_before_owner_powers(setup):
    board, human, _ = setup
    profile = board.create_agent_profile(human, 'Companion', kind='companion')
    profile, device, _, actor = running(board, human, profile)
    changed = board.update_agent_profile(human, profile['id'], profile['version'], description='New version')
    with pytest.raises(BoardError):
        board.actor_session(actor, actor['session_id'])
    board.register_agent(actor, actor['session_id'], 'Current', [], 'codex', False, actor['session_id'])
    actor = board.actor_session(actor, actor['session_id'])
    with board._tx() as db:
        db.execute('DELETE FROM agent_profile_bindings WHERE profile_id=? AND device_id=?', (profile['id'], device['id']))
    with pytest.raises(BoardError):
        board.actor_session(actor, actor['session_id'])
    board.bind_agent_profile(human, profile['id'], changed['version'], device['id'], actor['session_id'], 'codex')
    current = board.get_agent_profile(human, profile['id'])
    board.update_agent_profile(human, profile['id'], current['version'], status='paused')
    with pytest.raises(BoardError):
        board.actor_session(actor, actor['session_id'])


def test_companion_binding_cannot_use_legacy_registration_to_create_another_profile(setup):
    board, human, _ = setup
    profile = board.create_agent_profile(human, 'Companion', kind='companion')
    profile, device, raw, _ = running(board, human, profile)
    # Even inconsistent legacy device metadata must not reopen the bootstrap path.
    with board._tx() as db:
        db.execute('UPDATE devices SET profiles_managed=0 WHERE id=?', (device['id'],))
    with pytest.raises(BoardError):
        board.register_agent(raw, 'different-session', 'Different key', [], 'codex', False, 'new-key')
    assert len(board.list_agent_profiles(human)) == 1


def test_companions_cannot_claim_legacy_agent_or_human_tasks(setup):
    board, human, project = setup
    companion = board.create_agent_profile(human, 'Companion', kind='companion')
    _, _, _, actor = running(board, human, companion)
    ticket = board.create_ticket(human, project['id'], 'Legacy tasks')
    for destination in ('group', 'human'):
        task = board.create_task(human, ticket['thread_id'], 'Legacy task', 'Inspect fixture only', '', destination, 'alice',
                                 action='inspect', resource='repo:test', idempotency_key=destination)
        with pytest.raises(BoardError):
            board.claim_task(actor, task['id'])
        with board._connection() as db:
            row = db.execute('SELECT status,claim_version,claimant FROM tasks WHERE id=?', (task['id'],)).fetchone()
        assert dict(row) == {'status': 'pending', 'claim_version': 0, 'claimant': None}


def test_schema18_upgrade_preserves_ordinary_claims_snapshots_and_ticket_resolution(setup):
    board, human, project = setup
    profile = board.create_agent_profile(human, 'Ordinary', policy=ordinary_policy(), project_ids=[project['id']],
                                         preferred_provider='codex_chatgpt', preferred_model='pinned-model')
    profile, device, _, actor = running(board, human, profile, 'ordinary-session')
    ticket = board.create_ticket(human, project['id'], 'Existing claim')
    claimed = board.claim_ticket(actor, ticket['id'], ticket['version'])
    with board._connection() as db:
        before = [tuple(row) for row in db.execute('SELECT profile_id,version,snapshot FROM agent_profile_versions ORDER BY profile_id,version')]
    with board._tx() as db:
        from legacy_schema_fixtures import remove_hierarchy_schema20_tables
        remove_hierarchy_schema20_tables(db)
        db.execute('DROP INDEX companion_per_owner')
        db.execute('ALTER TABLE agent_profiles DROP COLUMN kind')
        db.execute('PRAGMA user_version=18')
    migrated = Board(board.data_dir)
    with migrated._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 20
        assert db.execute('SELECT kind FROM agent_profiles WHERE id=?', (profile['id'],)).fetchone()[0] == 'agent'
        assert [tuple(row) for row in db.execute('SELECT profile_id,version,snapshot FROM agent_profile_versions ORDER BY profile_id,version')] == before
    assert migrated.get_ticket(human, ticket['id'])['ticket']['claimant'] == claimed['claimant']
    assert migrated.get_ticket(human, ticket['id'])['ticket']['resolution'] == ''
    assert 'companion_for' not in migrated.actor_session(actor, actor['session_id'])
    old_snapshot = json.loads(before[-1][2])
    from server_provisioning import _hash
    assert migrated.get_agent_profile_version(human, profile['id'], profile['version']) == old_snapshot
    with migrated._connection() as db:
        _, digest, provider, model = migrated._provisioning_profile(db,
            {'owner_id': human['owner_id'], 'device_id': device['id']}, profile['id'])
    assert digest == _hash(old_snapshot) and (provider, model) == ('codex_chatgpt', 'pinned-model')
    migrated.update_agent_profile(human, profile['id'], profile['version'], name='Cosmetic rename')
    with migrated._connection() as db:
        fresh_snapshot = json.loads(db.execute('SELECT snapshot FROM agent_profile_versions WHERE profile_id=? ORDER BY version DESC LIMIT 1', (profile['id'],)).fetchone()[0])
    assert 'kind' not in fresh_snapshot and _compatible(old_snapshot, fresh_snapshot)


def test_companion_is_refused_by_public_provisioning_and_append_contexts(setup):
    board, human, _ = setup
    companion = board.create_agent_profile(human, 'Companion', kind='companion',
        preferred_provider='codex_chatgpt', preferred_model='pinned-model')
    device = board.create_device(human, 'Manager', [])
    companion = board.bind_agent_profile(human, companion['id'], companion['version'], device['id'], 'companion', 'codex')
    host = board.create_server_host(human, device['id'], 'Owner host', 'host')
    with board._connection() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM agent_profile_versions ORDER BY profile_id,version')]
    for context in (board.get_server_provisioning_context, board.get_server_messages_append_context):
        with pytest.raises(BoardError, match='server_provisioning_profile_unavailable'):
            context(human, host['id'], companion['id'])
    with board._connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM agent_profile_versions ORDER BY profile_id,version')] == before
        assert db.execute('SELECT COUNT(*) FROM server_provisioning_requests').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM server_messages_append_requests').fetchone()[0] == 0


@pytest.mark.parametrize('mode', ['messages', 'ticket_text'])
def test_companion_catalog_publish_replace_and_start_are_refused_but_stop_survives(setup, mode):
    from server_instances import catalog_fence

    board, human, _ = setup
    device = board.create_device(human, 'Manager', [])
    raw = board.authenticate(device['token'])
    ordinary = board.create_agent_profile(human, 'Worker', policy=ordinary_policy())
    ordinary = board.bind_agent_profile(human, ordinary['id'], ordinary['version'], device['id'], 'worker', 'codex')
    companion = board.create_agent_profile(human, 'Companion', kind='companion')
    companion = board.bind_agent_profile(human, companion['id'], companion['version'], device['id'], 'companion', 'codex')
    host = board.create_server_host(human, device['id'], 'Owner host', 'host')
    entry = dict(catalog_key='target', profile_id=companion['id'], profile_version=companion['version'],
                 launchspec_sha256='a' * 64, mode=mode)

    def snapshot():
        with board._connection() as db:
            return tuple(tuple(tuple(row) for row in db.execute('SELECT * FROM ' + table + ' ORDER BY rowid'))
                for table in ('server_hosts', 'server_instances', 'server_instance_commands',
                              'server_control_receipts', 'audit', 'runtime_executions'))

    before = snapshot()
    with pytest.raises(BoardError, match='server_catalog_not_authorized'):
        board.publish_server_catalog(raw, host['id'], 0, [entry], 'companion-publish')
    assert snapshot() == before

    worker_entry = {**entry, 'profile_id': ordinary['id'], 'profile_version': ordinary['version']}
    instance = board.publish_server_catalog(raw, host['id'], 0, [worker_entry], 'worker-publish')['instances'][0]
    stop = board.command_server_instance(human, instance['id'], 'stop', instance['version'],
        instance['generation'], instance['launchspec_sha256'], 0, 'worker-stop')
    board.ack_instance_command(raw, host['id'], stop['id'], stop['revision'], 'worker-stop-ack')
    instance = board.poll_instance_commands(raw, host['id'])['instances'][0]
    board.report_instance_state(raw, host['id'], instance['id'], instance['observation_revision'],
        instance['generation'], instance['launchspec_sha256'], instance['command_id'],
        'stopped', True, '', 'worker-stopped')
    # Model an invalid older/external catalogue row. Public creation cannot
    # produce it, but its existing Stop channel must remain usable.
    with board._tx() as db:
        db.execute('UPDATE server_instances SET profile_id=?,profile_version=? WHERE id=?',
                   (companion['id'], companion['version'], instance['id']))
    current = board.get_server_host(human, host['id'])['instances'][0]
    assert current['can_start'] is False and current['control_reason'] == 'authorization_changed'
    before = snapshot()
    with pytest.raises(BoardError, match='server_catalog_not_authorized'):
        board.replace_server_catalog(raw, host['id'], 1, host['version'], catalog_fence([current]),
            [{**entry, 'launchspec_sha256': 'b' * 64}], 'companion-replace')
    assert snapshot() == before
    for action in ('start', 'restart'):
        with pytest.raises(BoardError, match='server_start_not_authorized'):
            board.command_server_instance(human, current['id'], action, current['version'],
                current['generation'], current['launchspec_sha256'], 1, 'companion-' + action)
        assert snapshot() == before
    cleanup = board.command_server_instance(human, current['id'], 'stop', current['version'],
        current['generation'], current['launchspec_sha256'], 0, 'companion-stop')
    assert cleanup['action'] == 'stop' and cleanup['budget'] == 0


def test_schema19_backup_roundtrip_and_newer_schema_refusal(setup, tmp_path):
    board, human, _ = setup
    companion = board.create_agent_profile(human, 'Companion', kind='companion')
    archive = tmp_path / 'backup.zip'
    create_backup(board.data_dir, archive)
    restore_backup(archive, tmp_path / 'restored')
    restored = Board(tmp_path / 'restored')
    assert restored.get_agent_profile(human, companion['id'])['kind'] == 'companion'
    with restored._tx() as db:
        db.execute('PRAGMA user_version=21')
    with pytest.raises(RuntimeError, match='newer'):
        Board(tmp_path / 'restored')


def test_companion_inbox_includes_messages_that_mention_its_owner(setup):
    board, human, project = setup
    companion = board.create_agent_profile(human, 'Companion', kind='companion', idempotency_key='create')
    _, _, _, companion_actor = running(board, human, companion)
    worker = board.create_agent_profile(human, 'Worker', idempotency_key='worker')
    board.update_agent_profile(human, worker['id'], worker['version'], policy=ordinary_policy(), project_ids=[project['id']])
    _, _, _, worker_actor = running(board, human, board.get_agent_profile(human, worker['id']), session='worker-session')
    thread = {'id': board.create_ticket(human, project['id'], 'Release')['thread_id']}
    pinged = board.post_message(worker_actor, thread['id'], 'Alice, can I deploy?', mentions=['alice'], idempotency_key='ping')
    other = board.post_message(worker_actor, thread['id'], 'Just a note', idempotency_key='note')
    inbox = board.read_inbox(companion_actor)
    ids = [message['id'] for message in inbox['messages']]
    assert pinged['id'] in ids and other['id'] not in ids
    assert 'alice' in next(m for m in inbox['messages'] if m['id'] == pinged['id'])['mentions']
    # The owner's own inbox still has it too.
    assert pinged['id'] in [m['id'] for m in board.read_inbox(human)['messages']]
