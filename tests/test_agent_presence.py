"""Presence is aggregate check-in metadata, separate from lifecycle and work."""
import json

import pytest

import profiles
from board import Board
from mcp_views import MCPViews, compact

NOW = 1800000000
CONTROLS = {'presence', 'online', 'last_seen_at', 'idle_after_seconds'}
PUBLIC = {'id', 'owner_id', 'name', 'avatar', 'status'} | CONTROLS


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path / 'board')
    human, peer = ({'kind': 'human', 'owner_id': owner} for owner in ('owner', 'peer'))
    for actor in (human, peer):
        board.ensure_owner(actor['owner_id'], actor['owner_id'].title())
        board.set_policy(actor, True, True, True, True, [], [])
    project = board.create_project(human, 'Shared', ['owner', 'peer'], idempotency_key='shared')
    profile = board.create_agent_profile(human, 'Helper', instructions='PRIVATE instructions',
        project_ids=[project['id']], policy={'allow_read': True, 'allow_messages': True,
            'allow_requests': True, 'actions': [], 'resources': [], 'autonomy': 'queue'})
    return board, human, peer, project, profile


def connect(home, key='first'):
    board, human, _, _, profile = home
    current = board.get_agent_profile(human, profile['id'])
    device = board.create_device(human, 'PRIVATE device ' + key, [])
    board.bind_agent_profile(human, profile['id'], current['version'], device['id'], key, 'codex')
    raw = board.authenticate(device['token'])
    board.register_agent(raw, 'PRIVATE session ' + key, 'PRIVATE label ' + key, [], 'codex', False, key)
    return device, raw


def latest(home, viewer=None):
    board, human, _, _, profile = home
    return next(item for item in board.list_agents(viewer or human) if item['id'] == profile['id'])


def test_unconnected_and_unknown_checkin_are_offline_without_changing_lifecycle(home):
    board, human, _, _, profile = home
    original = board.get_agent_profile(human, profile['id'])
    assert {key: original[key] for key in CONTROLS} == {
        'presence': 'offline', 'online': False, 'last_seen_at': None, 'idle_after_seconds': 900}
    connect(home)
    with board._tx() as db:
        db.execute('UPDATE sessions SET last_seen_at=NULL')
    assert latest(home)['presence'] == 'offline'
    assert latest(home)['status'] == 'active'
    snapshot = board.get_agent_profile_version(human, profile['id'], original['version'])
    assert not CONTROLS.intersection(snapshot)


@pytest.mark.parametrize('age,expected', [(0, 'online'), (899, 'online'), (900, 'idle'), (901, 'idle')])
def test_presence_timeout_boundary_and_reads_do_not_refresh_it(home, monkeypatch, age, expected):
    board, human, peer, _, profile = home
    connect(home)
    last_seen = NOW - age
    with board._tx() as db:
        db.execute('UPDATE sessions SET last_seen_at=?', (last_seen,))
    monkeypatch.setattr(profiles.time, 'time', lambda: NOW)
    for viewer in (human, peer):
        item = latest(home, viewer)
        assert (item['presence'], item['online'], item['last_seen_at']) == (expected, expected == 'online', last_seen)
        assert item['idle_after_seconds'] == 900 and item['status'] == 'active'
    with board._connection() as db:
        assert db.execute('SELECT last_seen_at FROM sessions').fetchone()[0] == last_seen
    assert board.get_agent_profile(human, profile['id'])['presence'] == expected


def test_newest_nonrevoked_session_wins_and_revoked_devices_do_not_count(home, monkeypatch):
    board, human, _, _, profile = home
    first, _ = connect(home)
    second, _ = connect(home, 'second')
    with board._tx() as db:
        db.execute('UPDATE sessions SET last_seen_at=? WHERE device_id=?', (NOW - 901, first['id']))
        db.execute('UPDATE sessions SET last_seen_at=? WHERE device_id=?', (NOW, second['id']))
    monkeypatch.setattr(profiles.time, 'time', lambda: NOW)
    assert latest(home)['presence'] == 'online'
    board.revoke_device(human, second['id'])
    assert latest(home)['presence'] == 'idle'
    assert latest(home)['last_seen_at'] == NOW - 901
    board.revoke_device(human, first['id'])
    assert latest(home)['presence'] == 'offline'
    assert latest(home)['last_seen_at'] is None
    assert board.get_agent_profile(human, profile['id'])['presence'] == 'offline'


def test_shared_directory_mentions_and_mcp_keep_only_safe_presence_controls(home, monkeypatch):
    board, human, peer, project, profile = home
    _, raw = connect(home)
    with board._tx() as db:
        db.execute('UPDATE sessions SET last_seen_at=?', (NOW - 901,))
    monkeypatch.setattr(profiles.time, 'time', lambda: NOW)
    actor = board.actor_session(raw, 'PRIVATE session first')
    item = latest(home, peer)
    assert set(item) == PUBLIC and item['presence'] == 'idle'
    assert 'PRIVATE' not in json.dumps(item)
    own_agent = latest(home, actor)
    assert set(own_agent) == PUBLIC
    thread = board.create_ticket(human, project['id'], 'Presence')['thread_id']
    target = next(target for target in board.list_mention_targets(peer, thread) if target.get('agent_id') == profile['id'])
    assert set(target) == {'id', 'agent_id', 'name', 'kind', 'owner_id'} | CONTROLS
    assert target['presence'] == 'idle' and 'PRIVATE' not in json.dumps(target)
    views = MCPViews(board)
    for name, value in (('list_agents', [own_agent]), ('list_mention_targets', [target])):
        page = views.page(actor, name, value)
        assert {key: page['items'][0][key] for key in CONTROLS} == {key: value[0][key] for key in CONTROLS}
        assert compact(value)[0]['presence'] == 'idle'


def test_paused_lifecycle_does_not_masquerade_as_presence_or_renew_work(home, monkeypatch):
    board, human, _, _, profile = home
    connect(home)
    with board._tx() as db:
        db.execute('UPDATE sessions SET last_seen_at=?', (NOW,))
    current = board.get_agent_profile(human, profile['id'])
    board.update_agent_profile(human, profile['id'], current['version'], status='paused')
    monkeypatch.setattr(profiles.time, 'time', lambda: NOW)
    item = latest(home)
    assert item['status'] == 'paused' and item['presence'] == 'online'
