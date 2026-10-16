"""Independent adversarial checks for persistent profile authorization boundaries."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Barrier
import time

import pytest

from backup import create_backup, restore_backup
from board import Board, BoardError


def denied(call, *args, **kwargs):
    with pytest.raises(BoardError) as error:
        call(*args, **kwargs)
    assert error.value.status in (401, 403, 404, 409)


def policy(**changes):
    return dict(allow_read=True, allow_messages=True, allow_requests=True,
                actions=['inspect'], resources=['fixture:read'], autonomy='requested') | changes


@pytest.fixture
def guarded(tmp_path):
    board = Board(tmp_path / 'board', base_url='https://board.test')
    for owner in ('alice', 'bob'):
        board.ensure_owner(owner, owner.title())
    alice = {'kind': 'human', 'owner_id': 'alice'}
    bob = {'kind': 'human', 'owner_id': 'bob'}
    for human in (alice, bob):
        board.set_policy(human, True, True, True, True,
                         ['inspect', 'deploy'], ['fixture:read', 'fixture:write'])
    allowed = board.create_project(alice, 'Allowed', ['alice', 'bob'], idempotency_key='allowed')
    hidden = board.create_project(alice, 'Hidden project marker', ['alice', 'bob'], idempotency_key='hidden')
    ticket = board.create_ticket(alice, allowed['id'], 'Allowed ticket', idempotency_key='allowed-ticket')
    secret = board.create_ticket(alice, hidden['id'], 'Hidden ticket marker', idempotency_key='hidden-ticket')
    profile = board.create_agent_profile(alice, 'Restricted fixture',
        project_ids=[allowed['id']], policy=policy(), idempotency_key='profile')
    device = board.create_device(alice, 'Fixture Mac', ['read'])
    profile = board.bind_agent_profile(alice, profile['id'], profile['version'],
        device['id'], 'fixture', agent_kind='codex')
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'restricted-session', 'Fixture', [], 'codex', False, 'fixture')
    actor = board.actor_session(raw, 'restricted-session')
    return dict(board=board, alice=alice, bob=bob, allowed=allowed, hidden=hidden,
                ticket=ticket, secret=secret, profile=profile, device=device,
                raw=raw, actor=actor, registration=registration)


def revise(s, **changes):
    current = s['board'].get_agent_profile(s['alice'], s['profile']['id'])
    result = s['board'].update_agent_profile(s['alice'], current['id'], current['version'], **changes)
    s['profile'] = result
    return result


def test_known_foreign_ids_do_not_bypass_project_scope(guarded):
    s = guarded
    b, a, hidden, secret = s['board'], s['actor'], s['hidden'], s['secret']
    attachment = b.add_attachment(s['alice'], secret['thread_id'], 'private.txt', 'text/plain', b'private marker')
    for method, args in (
        (b.get_project, (hidden['id'],)),
        (b.list_tickets, (hidden['id'],)),
        (b.get_ticket, (secret['id'],)),
        (b.get_thread, (secret['thread_id'],)),
        (b.get_attachment, (attachment['id'],)),
        (b.mark_read, (secret['thread_id'],)),
        (b.post_message, (secret['thread_id'], 'Do not publish')),
        (b.add_attachment, (secret['thread_id'], 'blocked.txt', 'text/plain', b'blocked')),
        (b.claim_ticket, (secret['id'], secret['version'])),
        (b.list_human_requests, (hidden['id'],)),
    ):
        denied(method, a, *args)
    denied(b.list_activity, a, project_id=hidden['id'])
    denied(b.get_work, a, project_id=hidden['id'])
    assert len(list((b.data_dir / 'attachments').iterdir())) == 1
    assert b.get_ticket(a, s['ticket']['id'])['ticket']['id'] == s['ticket']['id']


def test_scope_filters_previews_search_inbox_pagination_and_legacy_tasks(guarded):
    s = guarded
    b, human, actor = s['board'], s['alice'], s['actor']
    for n in range(5):
        b.post_message(human, s['secret']['thread_id'], f'Hidden body marker {n}', idempotency_key=f'hidden-{n}')
    visible = b.post_message(human, s['ticket']['thread_id'], 'Visible body', mentions=['agent:'+s['profile']['id']], idempotency_key='visible')
    b.create_task(human, s['secret']['thread_id'], 'Hidden legacy task marker', 'Hidden instructions marker', '',
                  'group', 'alice', action='inspect', resource='fixture:read')
    standalone = b.create_thread(human, 'Standalone private marker', ['alice'], 'standalone')
    b.post_message(human, standalone['id'], 'Standalone body marker')
    inbox = b.read_inbox(actor, limit=1, include_work=True)
    assert [m['id'] for m in inbox['messages']] == [visible['id']]
    assert inbox['tasks'] == []
    assert b.read_inbox(actor, cursor=inbox['cursor'], limit=1)['messages'] == []
    assert b.list_threads(actor, query='Hidden') == []
    assert [p['id'] for p in b.list_projects(actor)] == [s['allowed']['id']]
    outputs = (b.list_threads(actor), b.get_work(actor), b.list_activity(actor), inbox)
    rendered = json.dumps(outputs)
    assert 'Hidden' not in rendered and 'Standalone' not in rendered
    denied(b.get_thread, actor, standalone['id'])
    denied(b.create_thread, actor, 'Escaped thread', ['alice'], 'cannot-escape')
    denied(b.create_project, actor, 'Escaped project', ['alice'], idempotency_key='cannot-create-scope')


def test_live_scope_changes_apply_to_old_actor_and_previously_issued_cursor(guarded):
    s = guarded
    b = s['board']
    inbox = b.read_inbox(s['actor'])
    revise(s, project_ids=[])
    b.post_message(s['alice'], s['ticket']['thread_id'], 'Newly hidden marker')
    denied(b.read_inbox, s['actor'], cursor=inbox['cursor'])
    b.register_agent(s['raw'], 'restricted-session', 'Refresh profile', [], 'codex', False, 'fixture')
    assert b.read_inbox(s['actor'], cursor=inbox['cursor'])['messages'] == []
    assert b.list_projects(s['actor']) == []
    assert b.list_threads(s['actor']) == []
    denied(b.get_ticket, s['actor'], s['ticket']['id'])


def test_managed_device_cannot_mint_or_forge_an_unrestricted_identity(guarded):
    s = guarded
    b = s['board']
    for kind, key in [('codex', 'new-installation'), ('agent', 'fixture'), ('claude-code', 'fixture')]:
        denied(b.register_agent, s['raw'], f'forged-{kind}-{key}', 'Forged', [], kind, True, key)
    denied(b.register_session, s['raw'], 'raw-session', 'No profile', [])
    forged = {**s['actor'], 'agent_id': 'unrestricted', 'profile_id': 'unrestricted', 'policy': {'enabled': True}}
    denied(b.get_ticket, forged, s['secret']['id'])
    denied(b.get_ticket, s['raw'], s['secret']['id'])
    retry = b.register_agent(s['raw'], 'second-session', 'Same profile', [], 'codex', False, 'fixture')
    assert retry['identity']['id'] == s['profile']['id']


def test_session_cannot_change_to_another_even_human_authorized_profile(guarded):
    s = guarded
    b = s['board']
    other = b.create_agent_profile(s['alice'], 'Other profile', policy=policy(), project_ids=[s['hidden']['id']])
    b.bind_agent_profile(s['alice'], other['id'], other['version'], s['device']['id'], 'other', agent_kind='codex')
    denied(b.register_agent, s['raw'], 'restricted-session', 'Swap identity', [], 'codex', False, 'other')
    still = b.register_agent(s['raw'], 'restricted-session', 'Same identity', [], 'codex', False, 'fixture')
    assert still['identity']['id'] == s['profile']['id']
    denied(b.get_ticket, b.actor_session(s['raw'], 'restricted-session'), s['secret']['id'])


def test_management_and_binding_require_same_human_owner(guarded):
    s = guarded
    b, p = s['board'], s['profile']
    for a in (s['actor'], s['bob']):
        denied(b.get_agent_profile, a, p['id'])
        denied(b.update_agent_profile, a, p['id'], p['version'], name='Stolen')
        denied(b.duplicate_agent_profile, a, p['id'], p['version'], name='Copy')
        denied(b.bind_agent_profile, a, p['id'], p['version'], s['device']['id'], 'stolen')
    denied(b.create_agent_profile, s['actor'], 'Escalation', policy=policy())
    foreign = b.create_device(s['bob'], 'Other person device', [])
    denied(b.bind_agent_profile, s['alice'], p['id'], p['version'], foreign['id'], 'foreign')
    assert b.list_agent_profiles(s['bob']) == []


def test_duplicate_cannot_inherit_grants_scope_or_device_authority(guarded):
    s = guarded
    b, p = s['board'], s['profile']
    duplicate = b.duplicate_agent_profile(s['alice'], p['id'], p['version'], name='Safe copy', idempotency_key='clone')
    assert duplicate['id'] != p['id']
    assert duplicate['project_ids'] == []
    assert not duplicate['policy']['allow_read']
    assert not duplicate['policy']['allow_messages']
    assert not duplicate['policy']['allow_requests']
    assert duplicate['policy']['actions'] == [] and duplicate['policy']['resources'] == []
    assert duplicate.get('bindings', []) == []
    denied(b.register_agent, s['raw'], 'copy-session', 'Copy', [], 'codex', True, duplicate['id'])
    repeated = b.duplicate_agent_profile(s['alice'], p['id'], p['version'], name='Safe copy', idempotency_key='clone')
    assert repeated['id'] == duplicate['id']


def test_profile_permissions_and_owner_permissions_are_intersected_live(guarded):
    s = guarded
    b, a, thread_id = s['board'], s['actor'], s['ticket']['thread_id']
    b.post_message(a, thread_id, 'Initial allowed message')
    b.set_policy(s['alice'], True, True, False, True, ['inspect'], ['fixture:read'])
    denied(b.post_message, a, thread_id, 'Owner removed messages')
    b.set_policy(s['alice'], True, True, True, True, ['inspect'], ['fixture:read'])
    p = policy()
    p.update(allow_messages=False, allow_requests=False)
    revise(s, policy=p)
    denied(b.post_message, a, thread_id, 'Profile removed messages')
    denied(b.claim_ticket, a, s['ticket']['id'], s['ticket']['version'])
    b.register_agent(s['raw'], 'restricted-session', 'Refresh profile', [], 'codex', False, 'fixture')
    denied(b.post_message, a, thread_id, 'Profile still forbids messages')
    denied(b.claim_ticket, a, s['ticket']['id'], s['ticket']['version'])
    assert b.get_ticket(a, s['ticket']['id'])['ticket']['id'] == s['ticket']['id']
    p['allow_read'] = False
    revise(s, policy=p)
    denied(b.read_inbox, a)
    denied(b.register_agent, s['raw'], 'still-blocked', 'Same restricted profile', [], 'codex', False, 'fixture')


def test_old_task_actions_resources_and_session_capabilities_do_not_escape_profile(guarded):
    s = guarded
    b, human, actor, thread_id = s['board'], s['alice'], s['actor'], s['ticket']['thread_id']
    tasks = []
    for action, resource, capabilities in [('deploy', 'fixture:read', []), ('inspect', 'fixture:write', []),
                                           ('inspect', 'fixture:read', ['admin'])]:
        tasks.append(b.create_task(human, thread_id, 'Restricted legacy action', 'Do not run', '', 'group', 'alice',
                                  action=action, resource=resource, required_capabilities=capabilities))
    for task in tasks:
        denied(b.claim_task, actor, task['id'])
    denied(b.register_session, s['raw'], 'restricted-session', 'Capability escalation', ['admin'])
    allowed = b.create_task(human, thread_id, 'Permitted legacy action', 'Read fixture', '', 'group', 'alice',
                            action='inspect', resource='fixture:read')
    assert b.claim_task(actor, allowed['id'])['claimed']
    b.set_policy(human, True, True, True, True, [], [])
    claimed = b.get_thread(human, thread_id)['tasks'][-1]
    denied(b.update_task, actor, allowed['id'], claimed['claim_version'], progress='No longer permitted')


@pytest.mark.parametrize('status', ['paused', 'archived'])
def test_pause_and_archive_revoke_existing_sessions_and_claim_writes(guarded, status):
    s = guarded
    b = s['board']
    claim = b.claim_ticket(s['actor'], s['ticket']['id'], s['ticket']['version'])
    revise(s, status=status)
    denied(b.get_work, s['actor'])
    denied(b.read_inbox, s['actor'])
    denied(b.update_ticket, s['actor'], claim['id'], claim['version'], progress='Stale writer')
    denied(b.register_agent, s['raw'], 'new-after-stop', 'Stopped profile', [], 'codex', True, 'fixture')
    # Human access remains available for recovery of existing work.
    assert b.get_ticket(s['alice'], claim['id'])['ticket']['id'] == claim['id']


def test_device_revocation_cannot_be_bypassed_by_cached_actor_or_profile_binding(guarded):
    s = guarded
    b = s['board']
    b.revoke_device(s['alice'], s['device']['id'])
    denied(b.read_inbox, s['actor'])
    denied(b.register_agent, s['raw'], 'revoked-session', 'Revoked', [], 'codex', False, 'fixture')
    denied(b.bind_agent_profile, s['alice'], s['profile']['id'], s['profile']['version'], s['device']['id'], 'replacement')


def test_same_profile_on_two_devices_keeps_device_authentication_boundaries(guarded):
    s = guarded
    b = s['board']
    second = b.create_device(s['alice'], 'Second authorized device', [])
    profile = b.bind_agent_profile(s['alice'], s['profile']['id'], s['profile']['version'], second['id'], 'second', agent_kind='codex')
    raw = b.authenticate(second['token'])
    registration = b.register_agent(raw, 'second-device-session', 'Second device', [], 'codex', False, 'second')
    assert registration['identity']['id'] == profile['id']
    assert registration['identity']['device_id'] == second['id']
    actor = b.actor_session(raw, 'second-device-session')
    denied(b.get_ticket, {**s['raw'], 'session_id': 'second-device-session'}, s['ticket']['id'])
    denied(b.get_ticket, {**raw, 'session_id': 'restricted-session'}, s['ticket']['id'])
    b.revoke_device(s['alice'], s['device']['id'])
    denied(b.read_inbox, s['actor'])
    assert b.get_ticket(actor, s['ticket']['id'])['ticket']['id'] == s['ticket']['id']


def test_concurrent_human_edits_have_one_winner_and_preserve_denied_grants(guarded):
    s = guarded
    p = s['profile']
    barrier = Barrier(2)

    def update(name):
        barrier.wait()
        try:
            return s['board'].update_agent_profile(s['alice'], p['id'], p['version'], name=name)
        except BoardError as error:
            assert error.status == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ['First edit', 'Second edit']))
    assert sum(result is not None for result in results) == 1
    current = s['board'].get_agent_profile(s['alice'], p['id'])
    assert current['version'] == p['version'] + 1
    assert current['project_ids'] == [s['allowed']['id']]
    denied(s['board'].get_ticket, s['actor'], s['secret']['id'])


def test_first_registration_race_cannot_create_two_legacy_privilege_roots(guarded):
    s = guarded
    b = s['board']
    device = b.create_device(s['alice'], 'New installation', [])
    raw = b.authenticate(device['token'])
    barrier = Barrier(2)

    def register(key):
        barrier.wait()
        try:
            return b.register_agent(raw, f'race-{key}', 'Bootstrap race', [], 'codex', False, key)
        except BoardError as error:
            assert error.status in (403, 409)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register, ['one', 'two']))
    assert sum(result is not None for result in results) == 1


def test_backup_restore_keeps_scope_pause_bindings_and_non_escalation(guarded, tmp_path):
    s = guarded
    revise(s, status='paused')
    archive = tmp_path / 'profiles.zip'
    manifest = create_backup(s['board'].data_dir, archive)
    assert manifest['schema']['user_version'] == 9
    restore_backup(archive, tmp_path / 'restored')
    restored = Board(tmp_path / 'restored')
    profile = restored.get_agent_profile(s['alice'], s['profile']['id'])
    assert profile['status'] == 'paused' and profile['project_ids'] == [s['allowed']['id']]
    denied(restored.read_inbox, s['actor'])
    denied(restored.register_agent, s['raw'], 'restored-new-id', 'Escape after restore', [], 'codex', True, 'new-key')
    restored.update_agent_profile(s['alice'], profile['id'], profile['version'], status='active')
    denied(restored.get_ticket, s['actor'], s['ticket']['id'])
    restored.register_agent(s['raw'], 'restricted-session', 'Refresh after restore', [], 'codex', False, 'fixture')
    assert restored.get_ticket(s['actor'], s['ticket']['id'])['ticket']['id'] == s['ticket']['id']
    denied(restored.get_ticket, s['actor'], s['secret']['id'])


def test_agent_directory_uses_real_shared_profile_scope_and_public_projection(guarded):
    s = guarded
    b = s['board']
    hidden = b.create_agent_profile(s['bob'], 'Hidden agent marker',
        project_ids=[s['hidden']['id']], policy=policy(), instructions='Hidden private instructions marker')
    public = b.create_agent_profile(s['bob'], 'Shared colleague',
        project_ids=[s['allowed']['id']], policy=policy(), instructions='Private shared instructions marker',
        preferred_model='Private model marker', preferred_provider='Private provider marker')
    device = b.create_device(s['bob'], 'Private device marker', [])
    b.bind_agent_profile(s['bob'], public['id'], public['version'], device['id'], 'private-identity-key', agent_kind='codex')
    b.register_agent(b.authenticate(device['token']), 'private-session-id', 'Private session label marker', [], 'codex', False, 'private-identity-key')
    directory = b.list_agents(s['actor'])
    assert hidden['id'] not in {item['id'] for item in directory}
    shared = next(item for item in directory if item['id'] == public['id'])
    assert set(shared) == {'id', 'owner_id', 'name', 'avatar', 'status'}
    assert 'Private' not in json.dumps(directory)
    human_shared = next(item for item in b.list_agents(s['alice']) if item['id'] == public['id'])
    assert set(human_shared) == {'id', 'owner_id', 'name', 'avatar', 'status'}
    assert b.get_agent_profile(s['bob'], public['id'])['instructions'] == 'Private shared instructions marker'


def test_profile_edit_during_claim_requires_human_reconciliation(guarded):
    s = guarded
    b = s['board']
    claimed = b.claim_ticket(s['actor'], s['ticket']['id'], s['ticket']['version'])
    revise(s, instructions='New configuration while old work is running')
    denied(b.renew_ticket, s['actor'], claimed['id'], claimed['version'])
    denied(b.register_agent, s['raw'], 'restricted-session', 'Cannot acknowledge during work', [], 'codex', False, 'fixture')
    denied(b.register_session, s['raw'], 'restricted-session', 'Legacy cannot acknowledge during work', [])
    preserved = b.get_ticket(s['alice'], claimed['id'])['ticket']
    assert preserved['claimant'] == claimed['claimant'] and preserved['version'] == claimed['version']
    b.update_ticket(s['alice'], claimed['id'], claimed['version'], release_claim=True)
    b.register_agent(s['raw'], 'restricted-session', 'Human reconciled existing work', [], 'codex', False, 'fixture')
    assert b.get_ticket(s['actor'], claimed['id'])['ticket']['claimant'] is None


def test_registration_after_scope_reduction_does_not_expose_old_private_work(guarded):
    s = guarded
    b = s['board']
    revise(s, project_ids=[s['allowed']['id'], s['hidden']['id']])
    b.register_agent(s['raw'], 'restricted-session', 'Explicit broader profile', [], 'codex', False, 'fixture')
    b.claim_ticket(s['actor'], s['secret']['id'], s['secret']['version'])
    revise(s, project_ids=[s['allowed']['id']])
    denied(b.register_agent, s['raw'], 'restricted-session', 'Old claim must be reconciled', [], 'codex', False, 'fixture')
    registration = b.register_agent(s['raw'], 'new-narrow-session', 'New scope', [], 'codex', False, 'fixture')
    assert s['secret']['id'] not in json.dumps(registration)
    assert 'Hidden ticket marker' not in json.dumps(registration)
    # Recovery details are still available to the human who can release the claim.
    owner_view = b.get_agent_profile(s['alice'], s['profile']['id'])
    assert owner_view['active_work'][0]['id'] == s['secret']['id']


def schema_three_fixture(path):
    """Frozen v2 fixture plus the existing v3 migration, before any profile tables."""
    from coordination import migrate_coordination
    path.mkdir()
    serialize = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    actor = {'kind': 'agent', 'owner_id': 'alice', 'device_id': 'old-device', 'session_id': 'old-session'}
    human = {'kind': 'human', 'owner_id': 'alice'}
    payload = dict(thread_id='old-thread', body='Existing idempotent message', reply_to=None, mentions=[], attachment_ids=[])
    grant = dict(enabled=True, allow_read=True, allow_reply=True, allow_requests=True, actions=[], resources=[])
    with sqlite3.connect(path / 'board.sqlite3') as db:
        db.row_factory = sqlite3.Row
        db.executescript((Path(__file__).parent / 'fixtures' / 'schema_v2.sql').read_text())
        migrate_coordination(db)
        db.execute('PRAGMA user_version=3')
        db.execute('INSERT INTO owners VALUES(?,?,NULL,?)', ('alice', 'Alice', serialize(grant)))
        db.execute('INSERT INTO devices VALUES(?,?,?,?,?,?,NULL)',
            ('old-device', 'alice', 'Existing Mac', '[]', hashlib.sha256(b'fixture-token').hexdigest(), 1))
        db.execute('INSERT INTO agent_identities VALUES(?,?,?,?,?,?,?)',
            ('old-agent', 'alice', 'old-device', 'old-key', 'Existing Codex', 'codex', 1))
        db.execute('''INSERT INTO sessions(id,owner_id,device_id,label,capabilities,created_at,last_seen_at,
            skills,agent_kind,autonomous,agent_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
            ('old-session', 'alice', 'old-device', 'Existing chat', '[]', 1, 2, '[]', 'codex', 0, 'old-agent'))
        db.execute('INSERT INTO projects(id,name,description,context,repository_url,environment,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            ('old-project', 'Existing project', '', '', '', '', 1, 1))
        db.execute('INSERT INTO project_members VALUES(?,?)', ('old-project', 'alice'))
        db.execute('INSERT INTO threads VALUES(?,?,?,?)', ('old-thread', 'Existing discussion', 1, 1))
        db.execute('INSERT INTO thread_members VALUES(?,?)', ('old-thread', 'alice'))
        db.execute('''INSERT INTO tickets(id,project_id,number,thread_id,title,description,acceptance_criteria,
            priority,status,claimant,version,lease_expires_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('old-ticket', 'old-project', 1, 'old-thread', 'Existing active work', '', '', 'normal',
             'in_progress', serialize(actor), 8, time.time() + 3600, 1, 1))
        db.execute("INSERT INTO messages(id,thread_id,body,author,created_at,mentions,attachments) VALUES(?,?,?,?,?,'[]','[]')",
            ('old-message', 'old-thread', payload['body'], serialize(actor), 1))
        db.execute('INSERT INTO idempotency VALUES(?,?,?,?,?)',
            (serialize(actor), 'post_message', 'stable-before-upgrade', hashlib.sha256(serialize(payload).encode()).hexdigest(), 'old-message'))
        db.execute('INSERT INTO reader_state VALUES(?,?)', (serialize(actor), 1))
        db.execute('INSERT INTO cursors VALUES(?,?,?)',
            (hashlib.sha256(b'pre-migration-cursor').hexdigest(), serialize(actor), 1))
    return actor, human


def test_schema_three_migration_keeps_claim_identity_cursor_and_effect_deduplication(tmp_path):
    path = tmp_path / 'schema3'
    actor, human = schema_three_fixture(path)
    b = Board(path)
    with sqlite3.connect(b.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 9
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    old = b.get_ticket(actor, 'old-ticket')['ticket']
    assert old['version'] == 8 and old['claimant'] == actor
    assert old['lease_expires_at'] > time.time()
    profile = b.get_agent_profile(human, 'old-agent')
    assert profile['legacy'] and profile['project_ids'] is None
    assert profile['bindings'][0]['identity_key'] == 'old-key'
    assert b.read_inbox(actor, cursor='pre-migration-cursor')['messages'] == []
    result = b.post_message(actor, 'old-thread', 'Existing idempotent message', idempotency_key='stable-before-upgrade')
    assert result['id'] == 'old-message'
    assert len(b.get_thread(human, 'old-thread')['messages']) == 1
    raw = b.authenticate('fixture-token')
    denied(b.register_agent, raw, 'new-unbound', 'Cannot escape migration', [], 'codex', True, 'new-key')
    denied(b.register_session, raw, 'new-raw', 'Cannot bypass migration', [])
    reopened = Board(path)
    assert reopened.get_agent_profile(human, 'old-agent')['id'] == 'old-agent'


def test_schema_upgrade_failure_rolls_back_ddl_and_data_together(tmp_path, monkeypatch):
    import board as module
    path = tmp_path / 'rollback-schema3'
    schema_three_fixture(path)
    original = module.migrate_profiles

    def fail_after_migration(db):
        original(db)
        raise RuntimeError('Injected migration failure')

    monkeypatch.setattr(module, 'migrate_profiles', fail_after_migration)
    with pytest.raises(RuntimeError, match='Injected migration failure'):
        Board(path)
    with sqlite3.connect(path / 'board.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_profiles'").fetchone() is None
        assert 'profiles_managed' not in [row[1] for row in db.execute('PRAGMA table_info(devices)')]
        assert db.execute('SELECT agent_id FROM sessions').fetchone()[0] == 'old-agent'
        assert db.execute('SELECT version FROM tickets').fetchone()[0] == 8
    monkeypatch.setattr(module, 'migrate_profiles', original)
    assert Board(path).get_agent_profile({'kind': 'human', 'owner_id': 'alice'}, 'old-agent')['legacy']


def test_future_database_version_is_refused_without_rewriting_profiles(guarded):
    s = guarded
    with sqlite3.connect(s['board'].db_path) as db:
        before = db.execute('SELECT * FROM agent_profiles ORDER BY id').fetchall()
        db.execute('PRAGMA user_version=99')
    with pytest.raises(RuntimeError):
        Board(s['board'].data_dir)
    with sqlite3.connect(s['board'].db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 99
        assert db.execute('SELECT * FROM agent_profiles ORDER BY id').fetchall() == before
