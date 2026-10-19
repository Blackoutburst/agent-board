"""Human work polling must not masquerade as a retry decision."""
import time

import pytest

from board import BoardError
from test_coordination import space, new  # noqa: F401


def retry(space,task,attempt):
    board,human,_,project,agent,*_=space
    current=board.get_ticket(human,task['id'])['ticket']
    if current['status']=='blocked':
        with board._tx() as db:
            db.execute('UPDATE tickets SET recheck_at=? WHERE id=?',(time.time()-1,task['id']))
        board.get_work(human,project['id'])
        current=board.get_ticket(human,task['id'])['ticket']
    claimed=board.claim_ticket(agent,task['id'],current['version'])
    return board.update_ticket(agent,task['id'],claimed['version'],status='blocked',
        blocked_reason='Attempt '+str(attempt),recheck_after_seconds=300,release_claim=True)


def test_repeated_human_polls_do_not_reset_agent_block_escalation(space):
    board,human,_,project,_,*_=space
    task=new(space)
    for attempt in range(3):
        blocked=retry(space,task,attempt)
        for _ in range(3):board.get_work(human,project['id'])
    assert blocked['unblock_owner']=='human' and blocked['recheck_at'] is None
    events=board.list_activity(human,ticket_id=task['id'])
    assert len([e for e in events if e['event']=='block_escalated'])==1
    assert len([e for e in events if e['event']=='ticket_unblocked' and e['actor']['kind']=='human'])==2


@pytest.mark.parametrize('real_edit',[True,False])
def test_explicit_human_edit_resets_count_but_empty_edit_does_not(space,real_edit):
    board,human,_,_,_,*_=space
    task=new(space)
    retry(space,task,1);blocked=retry(space,task,2)
    changes={'description':'Human clarified the retry plan'} if real_edit else {}
    if real_edit:
        board.update_ticket(human,task['id'],blocked['version'],**changes)
    else:
        with pytest.raises(BoardError):board.update_ticket(human,task['id'],blocked['version'])
    blocked=retry(space,task,3)
    assert (blocked['recheck_at'] is not None) is real_edit
    events=board.list_activity(human,ticket_id=task['id'])
    assert len([e for e in events if e['event']=='block_escalated'])==(0 if real_edit else 1)


def test_real_human_answer_restarts_the_retry_count(space):
    board,human,other,project,agent,*_=space
    task=new(space)
    retry(space,task,1);blocked=retry(space,task,2)
    with board._tx() as db:
        db.execute('UPDATE tickets SET recheck_at=? WHERE id=?',(time.time()-1,task['id']))
    board.get_work(human,project['id'])
    current=board.get_ticket(human,task['id'])['ticket']
    board.claim_ticket(agent,task['id'],current['version'])
    question=board.request_human_input(agent,task['id'],other['owner_id'],'Which retry plan?','retry-question')
    board.resolve_human_request(other,question['id'],'Use the revised plan')
    blocked=retry(space,task,3)
    assert blocked['recheck_at'] is not None and blocked['unblock_owner']!='human'
