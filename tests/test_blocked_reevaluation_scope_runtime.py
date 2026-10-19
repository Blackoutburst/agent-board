"""Scoped automatic maintenance, with temporary Board/Runtime metadata only."""
import json
import time

import pytest

from board import Board, BoardError
from test_runtime import runtime, reserve
from test_delegation_families import family, open_family, member
from test_delegation_runtime_integration import claimed, execution


def expired_block(board, ticket_id):
    with board._tx() as db:
        db.execute("UPDATE tickets SET status='blocked',claimant=NULL,lease_expires_at=NULL,"
                   "blocked_reason='Uncertain cleanup',recheck_at=? WHERE id=?",
                   (time.time()-1, ticket_id))


def snapshot(board):
    with board._connection() as db:
        return tuple(db.iterdump())


def readonly_scope(tmp_path):
    board = Board(tmp_path/'board')
    human = {'kind': 'human', 'owner_id': 'alice'}
    board.ensure_owner('alice', 'Alice')
    board.set_policy(human, True, True, True, True, [], [])
    allowed = board.create_project(human, 'Allowed', ['alice'], 'allowed')
    hidden = board.create_project(human, 'Hidden', ['alice'], 'hidden')
    profile = board.create_agent_profile(human, 'Read-only fixture', status='active',
        project_ids=[allowed['id']], policy=dict(allow_read=True, allow_messages=False,
        allow_requests=False, actions=[], resources=[], autonomy='responses'), idempotency_key='profile')
    device = board.create_device(human, 'Fixture', [])
    board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], 'fixture', 'codex')
    raw = board.authenticate(device['token'])
    board.register_agent(raw, 'reader', 'Fixture', [], 'codex', False, 'fixture')
    board.set_policy(human, True, True, False, False, [], [])
    return board, human, allowed, hidden, board.actor_session(raw, 'reader')


@pytest.mark.parametrize('explicit_hidden', [False, True])
def test_restricted_read_never_mutates_or_announces_a_hidden_project(tmp_path, explicit_hidden):
    board, human, _, hidden, actor = readonly_scope(tmp_path)
    ticket = board.create_ticket(human, hidden['id'], 'Hidden task', idempotency_key='task')
    expired_block(board, ticket['id'])
    before = snapshot(board)
    if explicit_hidden:
        with pytest.raises(BoardError):
            board.get_work(actor, hidden['id'])
    else:
        response = board.get_work(actor)
        assert ticket['id'] not in [item['id'] for item in response['ready']]
    assert snapshot(board) == before
    with pytest.raises(BoardError): board.get_ticket(actor, ticket['id'])


def test_automatic_maintenance_keeps_authorized_readonly_projects_working(tmp_path):
    board, human, allowed, hidden, actor = readonly_scope(tmp_path)
    tickets = [board.create_ticket(human, project['id'], project['name'],
                                  idempotency_key=project['id']) for project in (allowed, hidden)]
    for ticket in tickets: expired_block(board, ticket['id'])
    response = board.get_work(actor)
    assert [item['id'] for item in response['ready']] == [tickets[0]['id']]
    assert board.get_ticket(human, tickets[0]['id'])['ticket']['status'] == 'todo'
    assert board.get_ticket(human, tickets[1]['id'])['ticket']['status'] == 'blocked'
    assert board.get_thread(human, tickets[1]['thread_id'])['messages'] == []
    assert response['policy']['allow_requests'] is False


def test_scope_and_membership_changes_apply_before_any_sweep(tmp_path):
    board, human, allowed, _, actor = readonly_scope(tmp_path)
    ticket = board.create_ticket(human, allowed['id'], 'Visible then removed', idempotency_key='task')
    expired_block(board, ticket['id'])
    with board._tx() as db:
        db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',
                   (allowed['id'], human['owner_id']))
    before = snapshot(board)
    with pytest.raises(BoardError): board.get_work(actor, allowed['id'])
    assert snapshot(board) == before
    board.get_work(actor)
    assert snapshot(board) == before


@pytest.mark.parametrize('state', ['reserved', 'running', 'needs_review'])
def test_unfinished_runtime_keeps_block_until_positive_cleanup(runtime, state):
    board, human, _, project, agent = runtime
    actor, ticket, *_ = agent()
    record = reserve(runtime, actor, ticket)
    if state == 'running':
        record = board.start_execution(actor, record['id'], record['revision'])
    elif state == 'needs_review':
        record = board.finish_execution(actor, record['id'], record['revision'],
                                        'cancelled', False, 'cleanup_unconfirmed')
    expired_block(board, ticket['id'])
    before = snapshot(board)
    response = board.get_work(actor, project['id'])
    assert snapshot(board) == before
    detail = board.get_ticket(human, ticket['id'])['ticket']
    assert detail['status'] == 'blocked' and detail['block_reasons'] == ['runtime_cleanup']
    assert ticket['id'] not in [item['id'] for item in response['ready']]
    assert record['id'] not in json.dumps(detail['block_reasons'])
    board.finish_execution(actor, record['id'], record['revision'], 'cancelled', True)
    runtime_after_cleanup = board.get_runtime(human)
    assert ticket['id'] in [item['id'] for item in board.get_work(actor, project['id'])['ready']]
    assert board.get_runtime(human) == runtime_after_cleanup


def test_finished_row_without_cleanup_is_still_a_verifiable_block(runtime):
    board, human, _, project, agent = runtime
    actor, ticket, *_ = agent()
    record = reserve(runtime, actor, ticket)
    with board._tx() as db:
        db.execute("UPDATE runtime_executions SET state='finished',cleanup_confirmed=0 WHERE id=?",
                   (record['id'],))
    expired_block(board, ticket['id'])
    before = snapshot(board)
    board.get_work(actor, project['id'])
    assert snapshot(board) == before
    assert board.get_ticket(human, ticket['id'])['ticket']['block_reasons'] == ['runtime_cleanup']


@pytest.mark.parametrize('target', ['same_child', 'other_child', 'parent'])
def test_uncertain_family_is_not_reopened_or_adopted(family, target):
    f=family; board=f['b']; opened=open_family(f)
    child=claimed(f,opened)
    record=execution(f,child)
    pending=board.finish_execution(f['actors'][1],record['id'],record['revision'],
                                   'cancelled',False,'cleanup_unconfirmed')
    board.stop_delegation_family(f['h'],opened['id'],opened['revision'])
    selected = (child if target=='same_child' else f['parent'] if target=='parent'
                else board.get_ticket(f['h'],member(f,opened,1)['ticket_id'])['ticket'])
    expired_block(board,selected['id'])
    before=snapshot(board)
    board.get_work(f['actors'][1],f['project']['id'])
    assert snapshot(board)==before
    detail=board.get_ticket(f['h'],selected['id'])['ticket']
    assert detail['status']=='blocked'
    assert {'runtime_cleanup','delegation'} <= set(detail['block_reasons'])
    assert record['id'] not in json.dumps(detail['block_reasons'])
    board.finish_execution(f['actors'][1],pending['id'],pending['revision'],'cancelled',True)
    after_cleanup=snapshot(board)
    board.get_work(f['actors'][1],f['project']['id'])
    assert snapshot(board)==after_cleanup
    assert board.get_ticket(f['h'],selected['id'])['ticket']['status']=='blocked'
    assert 'delegation' in board.get_ticket(f['h'],selected['id'])['ticket']['block_reasons']
    assert board.get_delegation_family(f['h'],opened['id'])['state']=='cancelled'


def stale_review(board, ticket_id):
    with board._tx() as db:
        db.execute("UPDATE tickets SET status='review',claimant=NULL,lease_expires_at=NULL,"
                   "updated_at=? WHERE id=?", (time.time()-25*3600,ticket_id))


@pytest.mark.parametrize('explicit_hidden', [False, True])
def test_review_reminders_never_mutate_a_hidden_project(tmp_path, explicit_hidden):
    board,human,_,hidden,actor=readonly_scope(tmp_path)
    ticket=board.create_ticket(human,hidden['id'],'Hidden review',idempotency_key='review')
    stale_review(board,ticket['id'])
    before=snapshot(board)
    if explicit_hidden:
        with pytest.raises(BoardError): board.get_work(actor,hidden['id'])
    else:
        response=board.get_work(actor)
        assert ticket['id'] not in [item['id'] for item in response['review']]
    assert snapshot(board)==before


def test_review_reminder_maintenance_remains_scoped_and_idempotent(tmp_path):
    board,human,allowed,hidden,actor=readonly_scope(tmp_path)
    tickets=[board.create_ticket(human,project['id'],project['name'],
        idempotency_key=project['id']) for project in (allowed,hidden)]
    for ticket in tickets: stale_review(board,ticket['id'])
    response=board.get_work(actor)
    assert [item['id'] for item in response['review']]==[tickets[0]['id']]
    assert response['policy']['allow_messages'] is False
    assert len(board.get_thread(human,tickets[0]['thread_id'])['messages'])==1
    assert board.get_thread(human,tickets[1]['thread_id'])['messages']==[]
    before=snapshot(board)
    board.get_work(actor)
    assert snapshot(board)==before
