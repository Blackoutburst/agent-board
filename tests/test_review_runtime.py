"""Ordinary review execution on real temporary Board/Runtime records only."""
import json
import time

import pytest

from board import BoardError
from test_coordination import space, new  # noqa: F401
from test_review_queue import reviewer, deliver
from test_delegation_families import family  # noqa: F401


@pytest.fixture
def review(space):
    board, human, _, project, author, *_ = space
    actor = reviewer(space)
    delivered = deliver(board, author, new(space))
    claim = board.claim_ticket(actor, delivered['id'], delivered['version'])
    return board, human, project, author, actor, claim


def reserve(fixture, key='review-run'):
    board, _, project, _, actor, task = fixture
    return board.reserve_execution(actor, key, project['id'], task['id'], key+'-work',
                                   key+'-turn', 'codex_chatgpt', 'fixture-model')


def test_review_runtime_roundtrip_keeps_exact_scope_through_renew_and_progress(review):
    board, human, _, _, actor, task = review
    result = reserve(review)
    row = result['execution']
    assert not row['should_stop'] and task['status'] == 'review'
    with board._connection() as db:
        payload = json.loads(db.execute('SELECT payload FROM runtime_executions WHERE id=?',(row['id'],)).fetchone()[0])
        assert set(payload['review_scope']) == {'review_event_id','claim_started_version','delivery_event_id','owner_policy_event_id','source_sha256','grants_sha256','prerequisites_sha256'}
        # The family-only path still refuses review, even though ordinary Runtime admits it.
        with pytest.raises(BoardError, match='active_ticket_claim_required'):
            board._runtime_authorize(db, actor, task['project_id'], task['id'])
    renewed = board.renew_ticket(actor,task['id'],task['version'],progress='Reading the diff')
    assert renewed['version'] > task['version']
    board.update_ticket(actor,task['id'],renewed['version'],progress='Still reviewing')
    assert reserve(review)['retried'] is True
    running = board.start_execution(actor,row['id'],row['revision'])
    assert running['state'] == 'running' and not running['should_stop']
    heartbeat = board.heartbeat_execution(actor,row['id'],running['revision'])
    assert not heartbeat['should_stop']
    assert not board.lookup_execution(actor,'review-run')['execution']['should_stop']
    done = board.finish_execution(actor,row['id'],heartbeat['revision'],'completed',True)
    assert done['state'] == 'finished' and done['cleanup_confirmed']
    assert board.get_ticket(human,task['id'])['ticket']['status'] == 'review'


@pytest.mark.parametrize('field,value',[
    ('title','A changed delivery'),('description','New instructions'),('acceptance_criteria','New criteria'),
    ('outcome','Changed result'),('evidence','Changed evidence'),('required_skills',['python','rust']),
    ('resource','another:repository'),
])
def test_edits_to_delivered_review_stop_old_runtime_and_refuse_start(review,field,value):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    board.update_ticket(human,task['id'],task['version'],**{field:value})
    assert board.get_execution_control(actor,row['id'])['control_reason']=='authorization_changed'
    assert board.lookup_execution(actor,'review-run')['execution']['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
    # Losing scope never denies the exact executor's cleanup channel.
    clean=board.finish_execution(actor,row['id'],row['revision'],'cancelled',True)
    assert clean['cleanup_confirmed']


@pytest.mark.parametrize('status',['todo','in_progress','blocked','done'])
def test_losing_review_status_cannot_promote_old_runtime_to_another_scope(review,status):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    board.update_ticket(human,task['id'],task['version'],status=status)
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])


def test_same_session_reclaim_and_new_review_cycle_cannot_adopt_old_runtime(review):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    released=board.release_ticket(actor,task['id'],task['version'],'release-review','Restart review',status='review')
    claimed=board.claim_ticket(actor,task['id'],released['version'])
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):reserve(review)
    board.finish_execution(actor,row['id'],row['revision'],'cancelled',True)
    next_row=reserve(review,'new-review')['execution']
    changed=board.update_ticket(human,task['id'],claimed['version'],status='todo')
    board.update_ticket(human,task['id'],changed['version'],status='review')
    assert board.get_execution_control(actor,next_row['id'])['should_stop']


def test_expired_lease_and_changed_effective_grants_stop_review_runtime(review):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    board.set_policy(human,True,True,True,True,['additional-action'],[])
    # Reviewer has no such action, so its effective grants did not change.
    assert not board.get_execution_control(actor,row['id'])['should_stop']
    assert reserve(review)['retried'] is True
    board.set_policy(human,True,True,False,True,[],[])
    assert board.get_execution_control(actor,row['id'])['should_stop']
    board.set_policy(human,True,True,True,True,[],[])
    with board._tx() as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',(time.time()-1,task['id']))
    assert board.get_execution_control(actor,row['id'])['should_stop']


def test_author_or_other_session_cannot_borrow_review_runtime(space):
    board,human,_,project,author,other_session,_=space
    delivered=deliver(board,author,new(space))
    board.claim_ticket(author,delivered['id'],delivered['version'])
    with pytest.raises(BoardError,match='distinct_live_review_claim_required'):
        board.reserve_execution(author,'self-review',project['id'],delivered['id'],'w','t','codex_chatgpt','fixture-model')
    with pytest.raises(BoardError,match='active_ticket_claim_required'):
        board.reserve_execution(other_session,'borrow-review',project['id'],delivered['id'],'w2','t2','codex_chatgpt','fixture-model')


def test_old_ticket_runtime_has_no_fallback_to_review_scope(space):
    board,human,_,project,author,*_=space
    task=new(space)
    claimed=board.claim_ticket(author,task['id'],task['version'])
    row=board.reserve_execution(author,'old-work',project['id'],task['id'],'w','t','codex_chatgpt','fixture-model')['execution']
    board.update_ticket(human,task['id'],claimed['version'],status='review')
    assert board.get_execution_control(author,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(author,row['id'],row['revision'])


@pytest.mark.parametrize('change',['delivery','profile','scope','rights','release'])
def test_running_review_heartbeat_and_resume_fail_closed_after_scope_loss(review,change):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    running=board.start_execution(actor,row['id'],row['revision'])
    if change=='delivery':board.update_ticket(human,task['id'],task['version'],evidence='Changed while running')
    elif change=='profile':
        profile=board.get_agent_profile(human,task['claimant']['agent_id'])
        board.update_agent_profile(human,profile['id'],profile['version'],status='paused')
    elif change=='scope':
        profile=board.get_agent_profile(human,task['claimant']['agent_id'])
        board.update_agent_profile(human,profile['id'],profile['version'],project_ids=[])
    elif change=='rights':board.set_policy(human,True,False,True,True,[],[])
    else:board.release_ticket(actor,task['id'],task['version'],'lost-review','Release responsibility')
    heartbeat=board.heartbeat_execution(actor,row['id'],running['revision'])
    assert heartbeat['should_stop'] and heartbeat['control_reason']=='authorization_changed'
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
    assert board.finish_execution(actor,row['id'],heartbeat['revision'],'cancelled',True)['cleanup_confirmed']


def test_family_review_cannot_enter_the_ordinary_review_runtime(family):
    from test_delegation_families import open_family
    board=family['b']; opened=open_family(family)
    # A changed parent cannot make the family-only parent guard accept review.
    with board._tx() as db:
        db.execute("UPDATE tickets SET status='review' WHERE id=?",(opened['parent_ticket_id'],))
        actor=family['actors'][0]
        with pytest.raises(BoardError,match='active_ticket_claim_required'):
            board._family_parent(db,actor,board._family_row(db,opened['id']))
    with pytest.raises(BoardError,match='ordinary_review_required'):
        board.reserve_execution(actor,'family-review',family['project']['id'],opened['parent_ticket_id'],
                                'family-work','family-turn','codex_chatgpt','fixture-model')


def test_edit_then_restore_delivery_never_revives_the_original_runtime(review):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    edited=board.update_ticket(human,task['id'],task['version'],description='Temporary delivery change')
    board.update_ticket(human,task['id'],edited['version'],description=task['description'])
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])


def test_revoke_then_restore_grants_never_revives_original_runtime(review):
    board,human,_,_,actor,task=review
    row=reserve(review)['execution']
    board.set_policy(human,True,True,False,True,[],[])
    board.set_policy(human,True,True,True,True,[],[])
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])



def test_unrelated_owner_policy_audits_do_not_break_review(review):
    board,human,_,_,actor,_=review
    row=reserve(review)['execution']
    other={'kind':'human','owner_id':'thescreamer'}
    board.set_policy(other,True,False,False,False,['unrelated-action'],['unrelated-resource'])
    assert not board.get_execution_control(actor,row['id'])['should_stop']
    assert reserve(review)['retried']


@pytest.mark.parametrize('damage',['missing','malformed','ambiguous','overflow'])
def test_unprovable_policy_audit_history_stops_review_and_preserves_cleanup(review,damage):
    board,human,_,_,actor,_=review
    row=reserve(review)['execution']
    if damage=='overflow':
        for _ in range(65):board.set_policy(human,True,True,True,True,[],[])
    else:
        with board._tx() as db:
            initial=json.loads(db.execute('SELECT payload FROM runtime_executions WHERE id=?',(row['id'],)).fetchone()[0])['review_scope']['owner_policy_event_id']
            if damage=='missing':db.execute('DELETE FROM audit WHERE id=?',(initial,))
            else:db.execute('UPDATE audit SET detail=? WHERE id=?',('{' if damage=='malformed' else '{}',initial))
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
    assert board.finish_execution(actor,row['id'],row['revision'],'cancelled',True)['cleanup_confirmed']
