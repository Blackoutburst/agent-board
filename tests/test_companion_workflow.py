"""Companions manage owner work without becoming workers or human actors."""
import json

import pytest

from board import Board, BoardError


def policy():
    return dict(allow_read=True, allow_messages=True, allow_requests=True,
                actions=[], resources=[], autonomy='queue')


def running_profile(board, human, label, projects, *, kind='agent'):
    profile = board.create_agent_profile(human, label, kind=kind,
                                         policy=policy(), project_ids=projects)
    device = board.create_device(human, label + ' device', [])
    profile = board.bind_agent_profile(human, profile['id'], profile['version'],
                                       device['id'], label, 'codex')
    raw = board.authenticate(device['token'])
    board.register_agent(raw, label + '-session', label, ['python'],
                         'codex', True, label)
    return dict(profile=profile, device=device, raw=raw,
                actor=board.actor_session(raw, label + '-session'))


@pytest.fixture
def home(tmp_path):
    board = Board(tmp_path / 'board', base_url='https://board.test')
    humans = {owner: {'kind': 'human', 'owner_id': owner}
              for owner in ('alice', 'bob', 'outsider')}
    for owner, human in humans.items():
        board.ensure_owner(owner, owner.title())
        board.set_policy(human, True, True, True, True, [], [])
    shared = board.create_project(humans['alice'], 'Shared', ['alice', 'bob'])
    private = board.create_project(humans['alice'], 'Alice private', ['alice'])
    outside = board.create_project(humans['outsider'], 'Outside', ['outsider'])
    companion = running_profile(board, humans['alice'], 'Companion', [], kind='companion')
    worker = running_profile(board, humans['alice'], 'Coder', [shared['id']])
    second = running_profile(board, humans['alice'], 'Second', [shared['id']])
    peer = running_profile(board, humans['bob'], 'Peer', [shared['id']])
    return dict(board=board, humans=humans, shared=shared, private=private,
                outside=outside, companion=companion, worker=worker,
                second=second, peer=peer)


def denied(call, *args, statuses=(403,), **kwargs):
    with pytest.raises(BoardError) as error:
        call(*args, **kwargs)
    assert error.value.status in statuses


def task(home, title='Work', **changes):
    return home['board'].create_ticket(home['humans']['alice'], home['shared']['id'],
                                       title, **changes)


def delivered(home, title='Delivered', *, peer=False):
    board = home['board']
    worker = home['peer'] if peer else home['worker']
    ticket = task(home, title, assignee_agent_id=worker['profile']['id'])
    claim = board.claim_ticket(worker['actor'], ticket['id'], ticket['version'])
    reviewed = board.update_ticket(worker['actor'], ticket['id'], claim['version'],
                                   status='review', outcome='Implemented result',
                                   evidence='Synthetic check passed')
    return board.release_ticket(worker['actor'], ticket['id'], reviewed['version'],
                                 title + '-release', 'Ready for review', status='review')


def last_audit(board, event, ticket_id=None):
    with board._connection() as db:
        rows = db.execute('SELECT * FROM audit WHERE event=? ORDER BY id DESC', (event,))
        row = next(row for row in rows
                   if ticket_id is None or json.loads(row['detail']).get('ticket_id') == ticket_id)
        return json.loads(row['actor']), json.loads(row['detail'])


def assert_companion_author(author, home):
    assert author['kind'] == 'agent'
    assert author['owner_id'] == author['companion_for'] == author['acts_for'] == 'alice'
    assert author['agent_id'] == home['companion']['profile']['id']
    assert author['profile_kind'] == 'companion'
    assert author['agent_name'] == 'Companion'
    assert author['display_name'] == 'Alice'


def test_dispatch_uses_owner_membership_and_keeps_companion_attribution(home):
    board = home['board']; actor = home['companion']['actor']
    # A companion's owner scope is not narrowed to the profile's empty project list.
    created = board.create_ticket(actor, home['private']['id'], 'Private task')
    assert created['project_id'] == home['private']['id']
    assigned = board.create_ticket(actor, home['shared']['id'], 'Dispatched',
                                    assignee_agent_id=home['peer']['profile']['id'])
    assert assigned['assignee_agent_id'] == home['peer']['profile']['id']
    assert assigned['claimant'] is None and assigned['status'] == 'todo'
    message = board.post_message(actor, assigned['thread_id'], 'Please inspect the result.')
    assert_companion_author(message['author'], home)
    audit, _ = last_audit(board, 'ticket_created', assigned['id'])
    assert audit['kind'] == 'agent' and audit['companion_for'] == 'alice'
    assert audit['owner_id'] == 'alice'
    denied(board.create_ticket, actor, home['outside']['id'], 'Outside scope', statuses=(404,))


def test_reassignment_clears_another_members_live_claim_without_claiming_for_companion(home):
    board = home['board']; actor = home['companion']['actor']
    ticket = task(home, assignee_agent_id=home['peer']['profile']['id'])
    claim = board.claim_ticket(home['peer']['actor'], ticket['id'], ticket['version'])
    changed = board.update_ticket(actor, ticket['id'], claim['version'],
                                  assignee_agent_id=home['worker']['profile']['id'])
    assert changed['assignee_agent_id'] == home['worker']['profile']['id']
    assert changed['claimant'] is None and changed['lease_expires_at'] is None
    assert changed['outcome'] == changed['evidence'] == ''
    denied(board.update_ticket, actor, ticket['id'], claim['version'],
           assignee_agent_id=home['second']['profile']['id'], statuses=(409,))
    assert board.get_ticket(home['humans']['alice'], ticket['id'])['ticket']['assignee_agent_id'] == home['worker']['profile']['id']


def test_companion_can_release_a_project_claim_without_assigning_work_to_itself(home):
    board = home['board']; actor = home['companion']['actor']
    ticket = task(home, assignee_agent_id=home['peer']['profile']['id'])
    claim = board.claim_ticket(home['peer']['actor'], ticket['id'], ticket['version'])
    released = board.release_ticket(actor, ticket['id'], claim['version'],
                                    'companion-release', 'Replan this task')
    assert released['claimant'] is None and released['lease_expires_at'] is None
    assert released['assignee_agent_id'] == home['peer']['profile']['id']
    assert released['assignee_agent_id'] != home['companion']['profile']['id']
    assert board.release_ticket(actor, ticket['id'], claim['version'],
                                 'companion-release', 'Replan this task') == released


@pytest.mark.parametrize('human_dispatch', [False, True])
def test_dispatch_cannot_assign_a_companion_as_a_worker(home, human_dispatch):
    board = home['board']
    actor = home['humans']['alice'] if human_dispatch else home['companion']['actor']
    companion_id = home['companion']['profile']['id']
    denied(board.create_ticket, actor, home['shared']['id'], 'Companion cannot work',
           assignee_agent_id=companion_id, statuses=(400, 403))
    ticket = task(home)
    denied(board.update_ticket, actor, ticket['id'], ticket['version'],
           assignee_agent_id=companion_id, statuses=(400, 403))
    assert board.get_ticket(home['humans']['alice'], ticket['id'])['ticket']['assignee_agent_id'] is None


@pytest.mark.parametrize('spoof', [False, True])
def test_ordinary_agent_cannot_assign_specific_identities_even_with_companion_fields(home, spoof):
    board = home['board']; actor = dict(home['worker']['actor'])
    if spoof:
        actor.update(companion_for='alice', acts_for='alice', profile_kind='companion')
    denied(board.create_ticket, actor, home['shared']['id'], 'Unauthorized dispatch',
           assignee_agent_id=home['second']['profile']['id'])
    ticket = task(home, 'Assigned worker', assignee_agent_id=home['worker']['profile']['id'])
    claim = board.claim_ticket(home['worker']['actor'], ticket['id'], ticket['version'])
    denied(board.update_ticket, actor, ticket['id'], claim['version'],
           assignee_agent_id=home['second']['profile']['id'])
    assert board.get_ticket(home['humans']['alice'], ticket['id'])['ticket']['claimant']['agent_id'] == home['worker']['profile']['id']


@pytest.mark.parametrize('decision', ['done', 'in_progress', 'todo'])
def test_companion_accepts_or_returns_another_owners_review_without_claim(home, decision):
    board = home['board']; actor = home['companion']['actor']
    review = delivered(home, peer=True)
    # Review decisions also release a human reviewer's current claim.
    claimed = board.claim_ticket(home['humans']['alice'], review['id'], review['version'])
    result = board.update_ticket(actor, review['id'], claimed['version'], status=decision)
    assert result['status'] == decision
    assert result['claimant'] is None and result['lease_expires_at'] is None
    assert result['outcome'] == 'Implemented result'
    assert result['evidence'] == 'Synthetic check passed'
    assert result['assignee_agent_id'] == home['peer']['profile']['id']


@pytest.mark.parametrize('changes', [
    {'progress': 'I did the implementation'},
    {'outcome': 'Invented delivery'},
    {'evidence': 'Invented verification'},
    {'status': 'review'},
    {'status': 'in_progress'},
    {'status': 'done', 'outcome': 'Invented', 'evidence': 'Invented'},
])
def test_companion_cannot_work_or_fabricate_delivery(home, changes):
    board = home['board']; ticket = task(home)
    before = board.get_ticket(home['humans']['alice'], ticket['id'])['ticket']
    denied(board.update_ticket, home['companion']['actor'], ticket['id'], ticket['version'], **changes)
    assert board.get_ticket(home['humans']['alice'], ticket['id'])['ticket'] == before


@pytest.mark.parametrize('resolution', ['superseded', 'wont_do'])
@pytest.mark.parametrize('metadata', [False, True])
def test_companion_cannot_reclassify_unworked_closure_as_verified_completion(home, resolution, metadata):
    board = home['board']; human = home['humans']['alice']; actor = home['companion']['actor']
    ticket = task(home, 'Closed without implementation')
    closed = board.update_ticket(human, ticket['id'], ticket['version'],
                                 status='done', resolution=resolution,
                                 outcome='The owner chose another approach')
    assert closed['resolution'] == resolution
    before = board.get_ticket(human, ticket['id'])['ticket']
    changes = {'resolution': 'completed'}
    if metadata:
        changes['title'] = 'Misleading verified result'
    denied(board.update_ticket, actor, ticket['id'], closed['version'], **changes)
    # Failed management edits cannot relabel non-verification evidence as proof.
    assert board.get_ticket(human, ticket['id'])['ticket'] == before


@pytest.mark.parametrize('resolution', ['superseded', 'wont_do'])
def test_late_companion_answer_preserves_an_unworked_closed_ticket(home, resolution):
    board = home['board']; human = home['humans']['alice']; actor = home['companion']['actor']
    ticket = task(home, 'Question superseded by owner decision')
    request = board.request_human_input(human, ticket['id'], 'alice',
                                         'Which implementation should proceed?', 'late-owner-question')
    blocked = board.get_ticket(human, ticket['id'])['ticket']
    closed = board.update_ticket(human, ticket['id'], blocked['version'],
                                 status='done', resolution=resolution,
                                 outcome='The owner no longer wants this implementation')
    assert closed['status'] == 'done' and closed['waiting_on_human']
    answer = 'Retain the closed decision; no implementation is requested'
    resolved = board.resolve_human_request(actor, request['id'], answer)
    assert resolved['status'] == 'resolved' and resolved['answer'] == answer
    after = board.get_ticket(human, ticket['id'])['ticket']
    for field in ('status', 'resolution', 'outcome', 'evidence', 'claimant', 'lease_expires_at', 'version'):
        assert after[field] == closed[field]
    assert not after['waiting_on_human']
    messages = board.get_thread(human, ticket['thread_id'])['messages']
    assert_companion_author(next(message for message in messages if answer in message['body'])['author'], home)
    audit, _ = last_audit(board, 'human_input_resolved', ticket['id'])
    assert audit['kind'] == 'agent' and audit['companion_for'] == 'alice'


def test_completed_ticket_metadata_edit_preserves_verification_results(home):
    board = home['board']; human = home['humans']['alice']; actor = home['companion']['actor']
    ticket = task(home, 'Completed implementation')
    completed = board.update_ticket(human, ticket['id'], ticket['version'],
                                    status='done', outcome='Implementation delivered',
                                    evidence='Independent synthetic verification passed')
    changed = board.update_ticket(actor, ticket['id'], completed['version'],
                                   title='Clarified completed implementation')
    assert changed['title'] == 'Clarified completed implementation'
    assert changed['version'] == completed['version'] + 1
    for field in ('status', 'resolution', 'outcome', 'evidence', 'claimant', 'lease_expires_at'):
        assert changed[field] == completed[field]
    assert changed['resolution'] == 'completed'


def test_all_claim_surfaces_refuse_companions_without_effects(home):
    board = home['board']; actor = home['companion']['actor']; ticket = task(home)
    denied(board.claim_ticket, actor, ticket['id'], ticket['version'])
    denied(board.claim_next_ticket, actor, 'companion-queue')
    old_task = board.create_task(home['humans']['alice'], ticket['thread_id'],
                                 'Old task', 'Inspect source', [], 'group', 'alice')
    denied(board.claim_task, actor, old_task['id'])
    assert board.get_ticket(home['humans']['alice'], ticket['id'])['ticket']['claimant'] is None
    with board._connection() as db:
        assert db.execute('SELECT claimant FROM tasks WHERE id=?', (old_task['id'],)).fetchone()[0] is None
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event IN ('ticket_claimed','task_claimed')").fetchone()[0] == 0


def test_companion_answers_only_owner_requests_with_its_own_identity(home):
    board = home['board']; actor = home['companion']['actor']
    own_ticket = task(home, 'Own question')
    own = board.request_human_input(home['humans']['alice'], own_ticket['id'],
                                     'alice', 'Which owner choice?', 'own-question')
    peer_ticket = task(home, 'Peer question')
    peer = board.request_human_input(home['humans']['bob'], peer_ticket['id'],
                                      'bob', 'Which peer choice?', 'peer-question')
    denied(board.resolve_human_request, actor, peer['id'], 'Impersonate Bob')
    result = board.resolve_human_request(actor, own['id'], 'Use the owner preference')
    assert result['status'] == 'resolved' and result['answer'] == 'Use the owner preference'
    assert board.resolve_human_request(actor, own['id'], 'Use the owner preference') == result
    messages = board.get_thread(home['humans']['bob'], own_ticket['thread_id'])['messages']
    answer = next(message for message in messages if 'Use the owner preference' in message['body'])
    assert_companion_author(answer['author'], home)
    with board._connection() as db:
        assert db.execute('SELECT status FROM human_requests WHERE id=?', (peer['id'],)).fetchone()[0] == 'open'


def test_companion_work_view_includes_owner_requests_and_peer_reviews(home):
    board = home['board']; actor = home['companion']['actor']
    review = delivered(home, peer=True)
    ticket = task(home, 'Needs owner')
    request = board.request_human_input(home['humans']['alice'], ticket['id'],
                                         'alice', 'Owner decision?', 'view-question')
    work = board.get_work(actor)
    assert request['id'] in {row['id'] for row in work['human_requests']}
    assert review['id'] in {row['id'] for row in work['review']}
    assert work['assigned'] == []


def test_another_owners_private_request_is_neither_visible_nor_answerable(home):
    board = home['board']; actor = home['companion']['actor']
    human = home['humans']['outsider']
    ticket = board.create_ticket(human, home['outside']['id'], 'Private decision')
    request = board.request_human_input(human, ticket['id'], 'outsider',
                                         'Private owner question', 'private-question')
    denied(board.resolve_human_request, actor, request['id'], 'Unauthorized answer', statuses=(403, 404))
    work = board.get_work(actor)
    assert request['id'] not in {row['id'] for row in work['human_requests']}
    assert 'Private owner question' not in json.dumps(work)
    with board._connection() as db:
        assert db.execute('SELECT status FROM human_requests WHERE id=?', (request['id'],)).fetchone()[0] == 'open'


def test_companion_can_toggle_project_automation_but_ordinary_agent_cannot(home):
    board = home['board']; project = home['shared']; actor = home['companion']['actor']
    changed = board.update_project(actor, project['id'], project['version'], automation_enabled=False)
    assert changed['automation_enabled'] is False
    denied(board.update_project, home['worker']['actor'], project['id'], changed['version'],
           automation_enabled=True)
    restored = board.update_project(actor, project['id'], changed['version'], automation_enabled=True)
    assert restored['automation_enabled'] is True


def test_companion_profile_status_management_is_versioned_owner_only_and_limited(home):
    board = home['board']; actor = home['companion']['actor']; target = home['worker']['profile']
    changed = board.set_companion_agent_status(actor, target['id'], target['version'],
                                              'paused', 'pause-coder')
    assert changed['status'] == 'paused'
    assert board.set_companion_agent_status(actor, target['id'], target['version'],
                                             'paused', 'pause-coder') == changed
    denied(board.set_companion_agent_status, actor, target['id'], target['version'],
           'active', 'stale-coder', statuses=(409,))
    restored = board.set_companion_agent_status(actor, target['id'], changed['version'],
                                               'active', 'resume-coder')
    assert restored['status'] == 'active'
    assert not set(restored) & {'policy', 'instructions', 'bindings'}
    assert board.get_agent_profile(home['humans']['alice'], target['id'])['policy'] == target['policy']
    denied(board.set_companion_agent_status, actor, target['id'], restored['version'],
           'archived', 'archive-coder', statuses=(400, 403))
    denied(board.set_companion_agent_status, actor, home['peer']['profile']['id'],
           home['peer']['profile']['version'], 'paused', 'pause-peer', statuses=(403, 404))
    denied(board.set_companion_agent_status, actor, home['companion']['profile']['id'],
           home['companion']['profile']['version'], 'paused', 'pause-self')
    spoof = dict(home['second']['actor'], companion_for='alice', acts_for='alice', profile_kind='companion')
    denied(board.set_companion_agent_status, spoof, target['id'], restored['version'],
           'paused', 'spoof-pause')


@pytest.mark.parametrize('revocation', ['owner_disabled', 'requests_revoked', 'profile_paused', 'profile_changed'])
def test_current_owner_and_profile_controls_fence_companion_powers(home, revocation):
    board = home['board']; human = home['humans']['alice']; actor = home['companion']['actor']
    if revocation == 'owner_disabled':
        board.set_policy(human, False, True, True, True, [], [])
    elif revocation == 'requests_revoked':
        board.set_policy(human, True, True, True, False, [], [])
    else:
        profile = board.get_agent_profile(human, home['companion']['profile']['id'])
        changes = {'status': 'paused'} if revocation == 'profile_paused' else {'instructions': 'Updated owner instructions'}
        board.update_agent_profile(human, profile['id'], profile['version'], **changes)
    denied(board.create_ticket, actor, home['shared']['id'], 'Should remain absent', statuses=(403, 409))
    assert board.list_tickets(human, home['shared']['id']) == []
