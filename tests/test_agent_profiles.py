"""Profile lifecycle, versioned instructions, canonical identity and schema upgrade."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

import profiles
from board import Board, BoardError
from coordination import migrate_coordination


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path / 'board')
    board.ensure_owner('owner', 'Owner')
    board.ensure_owner('other', 'Other')
    human = {'kind': 'human', 'owner_id': 'owner'}
    board.set_policy(human, True, True, True, True, ['inspect'], ['repo:test'])
    project = board.create_project(human, 'Project', ['owner', 'other'], idempotency_key='project')
    return board, human, project


def granted():
    return {'allow_read': True, 'allow_messages': True, 'allow_requests': True,
            'actions': ['inspect'], 'resources': ['repo:test'], 'autonomy': 'requested'}


def running(home):
    board, human, project = home
    profile = board.create_agent_profile(human, 'Helper', instructions='Original instructions',
                                         policy=granted(), project_ids=[project['id']])
    device = board.create_device(human, 'Mac', [])
    profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'helper', 'codex')
    raw = board.authenticate(device['token'])
    registration = board.register_agent(raw, 'chat', 'Work', [], 'codex', False, 'helper')
    actor = board.actor_session(raw, 'chat')
    return profile, device, raw, registration, actor


def test_conversation_uses_current_avatar_without_rewriting_author_provenance(home):
    board, human, project = home
    profile, _, _, _, actor = running(home)
    thread_id = board.create_ticket(human, project['id'], 'Conversation')['thread_id']
    message = board.post_message(actor, thread_id, 'Existing message', idempotency_key='avatar-message')
    original_author = message['author'].copy()
    with board._connection() as db:
        original_stored = db.execute('SELECT author FROM messages WHERE id=?', (message['id'],)).fetchone()[0]
    board.update_agent_profile(human, profile['id'], profile['version'], avatar='🔆',
                               instructions='Private revised instructions')
    # A project peer can see the chosen public icon, without access to private profile fields.
    peer = {'kind': 'human', 'owner_id': 'other'}
    visible = board.get_thread(peer, thread_id)['messages'][0]
    assert visible['author_avatar'] == '🔆'
    assert visible['author'] == original_author
    assert 'instructions' not in visible['author']
    assert 'Private revised instructions' not in json.dumps(visible)
    assert board.get_message(peer, thread_id, message['id'])['message']['author'] == visible['author']
    with board._connection() as db:
        assert db.execute('SELECT author FROM messages WHERE id=?', (message['id'],)).fetchone()[0] == original_stored


def test_legacy_message_avatar_resolves_only_the_original_owner_device_session(home):
    board, human, project = home
    profile, _, _, _, actor = running(home)
    thread_id = board.create_ticket(human, project['id'], 'Legacy conversation')['thread_id']
    message = board.post_message(actor, thread_id, 'Legacy stamp', idempotency_key='legacy-avatar')
    board.update_agent_profile(human, profile['id'], profile['version'], avatar='🔎')
    legacy = {key: value for key, value in message['author'].items() if key != 'agent_id'}
    with board._tx() as db:
        db.execute('UPDATE messages SET author=? WHERE id=?', (json.dumps(legacy), message['id']))
    assert board.get_message(human, thread_id, message['id'])['message']['author_avatar'] == '🔎'
    legacy['device_id'] = 'not-the-original-device'
    with board._tx() as db:
        db.execute('UPDATE messages SET author=? WHERE id=?', (json.dumps(legacy), message['id']))
    assert not board.get_message(human, thread_id, message['id'])['message']['author_avatar']


def test_new_profile_is_unbound_restricted_and_creation_retry_does_not_duplicate(home):
    board, human, _ = home
    first = board.create_agent_profile(human, 'A helper', avatar='🌱', idempotency_key='profile')
    again = board.create_agent_profile(human, 'A helper', avatar='🌱', idempotency_key='profile')
    assert again == first
    assert first['owner_id'] == 'owner' and first['version'] == 1 and first['status'] == 'active'
    assert first['bindings'] == first['active_work'] == first['project_ids'] == []
    assert first['policy'] == profiles.DEFAULT_POLICY and first['legacy'] is False
    assert len(board.list_agent_profiles(human)) == 1
    with pytest.raises(BoardError):
        board.create_agent_profile(human, 'Changed payload', idempotency_key='profile')


def test_profile_versions_remain_private_and_immutable_after_lifecycle_changes(home):
    board, human, _ = home
    first = board.create_agent_profile(human, 'Helper', instructions='Private original', role='Reviewer')
    second = board.update_agent_profile(human, first['id'], 1, instructions='Private revision', status='paused')
    third = board.update_agent_profile(human, first['id'], 2, status='archived')
    assert third['version'] == 3 and board.list_agent_profiles(human) == []
    assert board.list_agent_profiles(human, True)[0]['status'] == 'archived'
    snapshot = board.get_agent_profile_version(human, first['id'], 1)
    assert snapshot['instructions'] == 'Private original' and snapshot['status'] == 'active'
    assert not set(snapshot) & {'bindings', 'active_work', 'fields'}
    assert board.get_agent_profile_version(human, first['id'], 2)['instructions'] == 'Private revision'
    with pytest.raises(BoardError):
        board.get_agent_profile_version({'kind': 'human', 'owner_id': 'other'}, first['id'], 1)
    with pytest.raises(BoardError):
        board.get_agent_profile_version(human, first['id'], 4)
    assert board.update_agent_profile(human, first['id'], 3, status='active')['version'] == 4
    assert board.get_agent_profile_version(human, first['id'], 1) == snapshot


def test_profile_can_bind_two_devices_without_changing_canonical_identity(home):
    board, human, project = home
    first = board.create_agent_profile(human, 'Portable helper', policy=granted(), project_ids=[project['id']])
    devices = [board.create_device(human, name, []) for name in ('Mac', 'Linux')]
    profile = first
    for device in devices:
        profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'portable', 'codex')
    registrations = [board.register_agent(board.authenticate(device['token']), 'chat-' + device['id'], 'Work', [], 'codex', False, 'portable') for device in devices]
    assert {item['identity']['id'] for item in registrations} == {first['id']}
    assert {item['identity']['device_id'] for item in registrations} == {item['id'] for item in devices}
    assert all(item['profile_version'] == profile['version'] for item in registrations)
    assert len(board.list_agents(human)[0]['sessions']) == 2
    assert len(board.get_agent_profile(human, first['id'])['bindings']) == 2


def test_instruction_change_pins_active_work_until_human_reconciles(home):
    board, human, project = home
    profile, _, raw, registration, actor = running(home)
    ticket = board.create_ticket(human, project['id'], 'Work', idempotency_key='work')
    claim = board.claim_ticket(actor, ticket['id'], ticket['version'])
    updated = board.update_agent_profile(human, profile['id'], profile['version'], instructions='New instructions')
    assert updated['active_work'][0]['profile_version'] == registration['profile_version']
    assert updated['active_work'][0]['id'] == ticket['id']
    with pytest.raises(BoardError, match='changed'):
        board.get_ticket(actor, ticket['id'])
    with pytest.raises(BoardError, match='active work'):
        board.register_agent(raw, 'chat', 'Resume', [], 'codex', False, 'helper')
    assert board.get_ticket(human, ticket['id'])['ticket']['claimant'] == claim['claimant']
    assert board.get_agent_profile_version(human, profile['id'], registration['profile_version'])['instructions'] == 'Original instructions'
    board.update_ticket(human, ticket['id'], claim['version'], release_claim=True)
    resumed = board.register_agent(raw, 'chat', 'Resume', [], 'codex', False, 'helper')
    assert resumed['profile_version'] == updated['version']
    assert resumed['profile']['instructions'] == 'New instructions'


def test_duplicate_keeps_customization_but_never_bindings_project_scope_or_permissions(home):
    board, human, _ = home
    profile, *_ = running(home)
    clone = board.duplicate_agent_profile(human, profile['id'], profile['version'], 'Copy', 'duplicate')
    assert clone['instructions'] == profile['instructions']
    assert clone['id'] != profile['id'] and clone['name'] == 'Copy' and clone['status'] == 'paused'
    assert clone['policy'] == profiles.DEFAULT_POLICY
    assert clone['bindings'] == clone['project_ids'] == [] and clone['legacy'] is False
    assert board.duplicate_agent_profile(human, profile['id'], profile['version'], 'Copy', 'duplicate')['id'] == clone['id']


@pytest.mark.parametrize('changes', [
    {'name': ' '}, {'avatar': 'https://example.test/p.png'}, {'avatar': '<svg>'}, {'avatar': 'x' * 33},
    {'language': 'unknown'}, {'instructions': 'x' * 12001}, {'project_ids': None}, {'status': None},
    {'policy': {}}, {'policy': {**granted(), 'allow_read': 1}}, {'owner_id': 'other'}, {'legacy': True},
    {'bindings': []}, {'preferred_model': None}, {'role': '\x00'},
])
def test_profile_fields_are_bounded_and_reject_ownership_or_permission_bypasses(home, changes):
    board, human, _ = home
    profile = board.create_agent_profile(human, 'Helper')
    with pytest.raises(BoardError):
        board.update_agent_profile(human, profile['id'], profile['version'], **changes)
    assert board.get_agent_profile(human, profile['id']) == profile


def make_schema_three(tmp_path):
    directory = tmp_path / 'legacy'
    directory.mkdir()
    db = sqlite3.connect(directory / 'board.sqlite3')
    db.row_factory = sqlite3.Row
    db.executescript((Path(__file__).parent / 'fixtures/schema_v2.sql').read_text())
    migrate_coordination(db)
    now = time.time()
    policy = json.dumps(dict(enabled=True, allow_read=True, allow_reply=True, allow_requests=True, actions=[], resources=[]))
    db.execute('INSERT INTO owners VALUES(?,?,NULL,?)', ('owner', 'Owner', policy))
    db.execute('INSERT INTO devices VALUES(?,?,?,?,?,?,NULL)', ('device', 'owner', 'Old Mac', '[]', hashlib.sha256(b'old-token').hexdigest(), now))
    db.execute('INSERT INTO agent_identities VALUES(?,?,?,?,?,?,?)', ('identity', 'owner', 'device', 'installation', 'Old Codex', 'codex', now))
    for identifier, agent_id, kind in (('old-chat', 'identity', 'codex'), ('legacy-inbox', None, 'agent')):
        db.execute('''INSERT INTO sessions(id,owner_id,device_id,label,capabilities,created_at,last_seen_at,agent_id,agent_kind)
            VALUES(?,?,?,?,?,?,?,?,?)''', (identifier, 'owner', 'device', identifier, '[]', now, now, agent_id, kind))
    db.execute("INSERT INTO projects(id,name,description,context,repository_url,environment,created_at,updated_at) VALUES('project','Project','','','','',?,?)", (now, now))
    db.execute("INSERT INTO project_members VALUES('project','owner')")
    db.execute("INSERT INTO threads VALUES('thread','Ticket thread',?,?)", (now, now))
    db.execute("INSERT INTO thread_members VALUES('thread','owner')")
    claimant = json.dumps({'kind': 'agent', 'owner_id': 'owner', 'device_id': 'device', 'session_id': 'old-chat', 'agent_id': 'identity'})
    db.execute('''INSERT INTO tickets(id,project_id,number,thread_id,title,description,acceptance_criteria,priority,status,
        claimant,claim_started_version,version,created_at,updated_at,lease_expires_at,assignee_agent_id)
        VALUES('ticket','project',1,'thread','Old work','','','normal','in_progress',?,2,2,?,?,?,'identity')''', (claimant, now, now, now + 900))
    db.execute('PRAGMA user_version=3')
    db.commit()
    db.close()
    return directory, claimant


def test_migration_preserves_ids_sessions_ticket_claims_and_old_raw_inbox(tmp_path):
    directory, claimant = make_schema_three(tmp_path)
    board = Board(directory)
    human = {'kind': 'human', 'owner_id': 'owner'}
    raw = board.authenticate('old-token')
    old = board.register_agent(raw, 'old-chat', 'Same work', [], 'codex', False, 'installation')
    assert old['identity']['id'] == 'identity' and old['identity']['device_id'] == 'device'
    assert old['profile']['legacy'] is True and old['profile']['project_ids'] is None
    assert old['policy']['allow_requests'] is True
    assert board.get_ticket(human, 'ticket')['ticket']['claimant'] == json.loads(claimant)
    assert board.get_ticket(human, 'ticket')['ticket']['version'] == 2
    inbox_actor = board.register_session(raw, 'legacy-inbox', 'Old reader', [])
    assert board.read_inbox(inbox_actor)['session']['agent_id'] is not None
    with pytest.raises(BoardError):
        board.register_agent(raw, 'escape', 'Escape', [], 'codex', False, 'new-key')
    snapshot = board.get_agent_profile_version(human, 'identity', 1)
    assert snapshot['name'] == 'Old Codex'
    with board._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 16
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert Board(directory).get_agent_profile(human, 'identity')['version'] == 1


def test_schema_migration_failure_rolls_back_all_profile_tables(tmp_path, monkeypatch):
    directory, _ = make_schema_three(tmp_path)
    original = profiles._record_version
    def fail(*args, **kwargs):
        raise RuntimeError('Simulated disk failure')
    monkeypatch.setattr(profiles, '_record_version', fail)
    with pytest.raises(RuntimeError):
        Board(directory)
    with sqlite3.connect(directory / 'board.sqlite3') as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute("SELECT name FROM sqlite_master WHERE name='agent_profiles'").fetchone() is None
        assert 'profile_version' not in {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
    monkeypatch.setattr(profiles, '_record_version', original)
    assert Board(directory).get_agent_profile({'kind': 'human', 'owner_id': 'owner'}, 'identity')['version'] == 1


def test_binding_race_one_tuple_cannot_be_given_to_two_profiles(home):
    board, human, _ = home
    device = board.create_device(human, 'Shared runtime', [])
    items = [board.create_agent_profile(human, name) for name in ('A', 'B')]
    def bind(item):
        try:
            return board.bind_agent_profile(human, item['id'], item['version'], device['id'], 'same', 'codex')['id']
        except BoardError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bind, items))
    assert sum(isinstance(result, str) for result in results) == 1
    assert results.count(409) == 1


@pytest.mark.parametrize('restriction', [{'status': 'paused'}, {'status': 'archived'}, {'project_ids': []}])
def test_human_task_edit_preserves_unchanged_ineligible_assignment(home, restriction):
    board, human, project = home
    profile, _, raw, _, actor = running(home)
    ticket = board.create_ticket(human, project['id'], 'Original title', assignee_agent_id=profile['id'])
    board.update_agent_profile(human, profile['id'], profile['version'], **restriction)
    edited = board.update_ticket(human, ticket['id'], ticket['version'], title='Edited title',
                                  assignee_agent_id=profile['id'], assignee_owner_id=None)
    assert edited['title'] == 'Edited title' and edited['assignee_agent_id'] == profile['id']
    assert edited['claimant'] is None
    with pytest.raises(BoardError):
        board.create_ticket(human, project['id'], 'New assignment', assignee_agent_id=profile['id'])
    other = board.create_ticket(human, project['id'], 'Existing unassigned task')
    with pytest.raises(BoardError):
        board.update_ticket(human, other['id'], other['version'], assignee_agent_id=profile['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(actor, ticket['id'], edited['version'])
    # Manual reconciliation remains possible without reactivating an executor.
    unassigned = board.update_ticket(human, ticket['id'], edited['version'], assignee_agent_id=None)
    assert unassigned['assignee_agent_id'] is None


def test_inbox_registration_cannot_silently_refresh_changed_profile_version(home):
    board, human, project = home
    profile, _, raw, registration, actor = running(home)
    ticket = board.create_ticket(human, project['id'], 'Still authorized')
    updated = board.update_agent_profile(human, profile['id'], profile['version'], instructions='Reviewed new configuration')
    with pytest.raises(BoardError, match='explicitly call register_agent'):
        board.register_session(raw, 'chat', 'Automatic inbox poll', [])
    with board._connection() as db:
        assert db.execute("SELECT profile_version FROM sessions WHERE id='chat'").fetchone()[0] == registration['profile_version']
    with pytest.raises(BoardError):
        board.read_inbox(actor)
    with pytest.raises(BoardError):
        board.claim_ticket(actor, ticket['id'], ticket['version'])
    refreshed = board.register_agent(raw, 'chat', 'Explicit configuration refresh', [], 'codex', False, 'helper')
    assert refreshed['profile_version'] == updated['version']
    assert refreshed['profile']['instructions'] == 'Reviewed new configuration'
    polled_actor = board.register_session(raw, 'chat', 'Subsequent inbox poll', [])
    assert board.read_inbox(polled_actor)['session']['profile_version'] == updated['version']
    assert board.get_ticket(polled_actor, ticket['id'])['ticket']['id'] == ticket['id']
