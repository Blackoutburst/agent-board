"""Prerequisite eligibility epochs on real temporary Board/Runtime records."""
import pytest

from board import BoardError
from test_coordination import space, new  # noqa: F401
from test_review_queue import deliver, reviewer
from test_review_runtime import reserve


def setup(space,initial):
    board,human,_,project,author,*_=space
    dep=new(space,'Dependency')
    if initial=='done':
        dep=board.update_ticket(human,dep['id'],dep['version'],status='done',outcome='Ready',evidence='Checked')
    else:dep=deliver(board,author,dep)
    actor=reviewer(space)
    delivered=deliver(board,author,new(space,'Dependent review',depends_on=[dep['id']]))
    task=board.claim_ticket(actor,delivered['id'],delivered['version'])
    fixture=(board,human,project,author,actor,task)
    row=reserve(fixture)['execution']
    return fixture,dep,row


@pytest.mark.parametrize('initial,middle',[('done','todo'),('review','done')])
def test_prerequisite_status_roundtrip_never_revives_old_review(space,initial,middle):
    fixture,dep,row=setup(space,initial)
    board,human,_,_,actor,_=fixture
    changed=board.update_ticket(human,dep['id'],dep['version'],status=middle)
    assert board.get_execution_control(actor,row['id'])['should_stop']
    board.update_ticket(human,dep['id'],changed['version'],status=initial,outcome='Ready',evidence='Checked')
    assert board.get_execution_control(actor,row['id'])['control_reason']=='authorization_changed'
    assert board.lookup_execution(actor,'review-run')['execution']['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
    with pytest.raises(BoardError):reserve(fixture)
    assert board.finish_execution(actor,row['id'],row['revision'],'cancelled',True)['cleanup_confirmed']


def test_running_review_stops_after_prerequisite_aba_and_can_finish_cleanup(space):
    fixture,dep,row=setup(space,'done')
    board,human,_,_,actor,_=fixture
    running=board.start_execution(actor,row['id'],row['revision'])
    changed=board.update_ticket(human,dep['id'],dep['version'],status='todo')
    board.update_ticket(human,dep['id'],changed['version'],status='done',outcome='Ready',evidence='Checked')
    heartbeat=board.heartbeat_execution(actor,row['id'],running['revision'])
    assert heartbeat['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
    assert board.finish_execution(actor,row['id'],heartbeat['revision'],'cancelled',True)['cleanup_confirmed']


@pytest.mark.parametrize('initial',['done','review'])
def test_dependency_notes_progress_and_content_are_not_implicit_review_source(space,initial):
    fixture,dep,row=setup(space,initial)
    board,human,_,author,actor,_=fixture
    if initial=='review':
        # A review claim and its renewals do not change prerequisite eligibility.
        dep=board.claim_ticket(author,dep['id'],dep['version'])
        dep=board.renew_ticket(author,dep['id'],dep['version'],progress='Dependency review progress')
    dep=board.update_ticket(human,dep['id'],dep['version'],progress='Independent progress',
        description='Changed dependency description',outcome='Edited dependency result',evidence='Edited evidence')
    board.post_message(human,dep['thread_id'],'A comment on the prerequisite',idempotency_key='dependency-comment')
    assert not board.get_execution_control(actor,row['id'])['should_stop']
    assert reserve(fixture)['retried']
    running=board.start_execution(actor,row['id'],row['revision'])
    assert not running['should_stop']


def test_missing_prerequisite_status_history_is_not_a_proof_of_stability(space):
    fixture,dep,row=setup(space,'done')
    board,_,_,_,actor,_=fixture
    with board._tx() as db:
        db.execute("DELETE FROM ticket_events WHERE ticket_id=? AND event='ticket_updated'",(dep['id'],))
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
