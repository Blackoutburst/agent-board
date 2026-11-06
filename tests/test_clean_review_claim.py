"""Whole-ticket cleanup and the claim CAS are one real Board transaction."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from board import BoardError
from test_profile_management import lead
from test_managed_claim_release import coder, claim


def released_review(s):
    b=s['b'];task,target,old,execution=claim(s)
    task=b.update_ticket(s['human'],task['id'],task['version'],status='review',release_claim=True,assignee_agent_id=None)
    return task,old,execution


def test_clean_claim_receipt_retry_and_stale_version(lead):
    s=lead;b=s['b'];task,_,_=released_review(s)
    _,actor,_=coder(s,key='reviewer')
    result=b.claim_clean_review_ticket(actor,task['id'],task['version'])
    assert result['schema']=='clean-review-claim-v1'
    assert result['prior_version']==task['version'] and result['session_id']==actor['session_id']
    assert result['ticket']['version']==task['version']+1
    assert result['ticket']['claimant']['session_id']==actor['session_id']
    assert b.claim_clean_review_ticket(actor,task['id'],task['version'])==result
    with pytest.raises(BoardError):b.claim_clean_review_ticket(actor,task['id'],task['version']-1)


@pytest.mark.parametrize('state,cleanup',[('reserved',0),('running',0),('needs_review',0),('finished',0)])
def test_another_runtime_key_blocks_without_ticket_mutation(lead,state,cleanup):
    s=lead;b=s['b'];task,_,execution=released_review(s)
    # A second real record has a different work/turn/key from the sealed one.
    # Fixture-only state injection covers uncertain terminal cleanup too.
    with b._tx() as db:
        row=dict(db.execute('SELECT * FROM runtime_executions WHERE id=?',(execution['id'],)).fetchone())
        row.update(id='another-execution',work_id='another-work',turn_id='another-turn',idempotency_key='another-key',state=state,cleanup_confirmed=cleanup)
        db.execute('INSERT INTO runtime_executions ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
    _,actor,_=coder(s,key='reviewer')
    with pytest.raises(BoardError,match='unresolved execution'):
        b.claim_clean_review_ticket(actor,task['id'],task['version'])
    assert b.get_ticket(s['human'],task['id'])['ticket']==task


def test_two_distinct_sessions_compete_for_one_version(lead):
    s=lead;b=s['b'];task,_,_=released_review(s)
    actors=[coder(s,key='reviewer'+str(i))[1] for i in range(2)];barrier=Barrier(2)
    def take(actor):
        barrier.wait()
        try:return b.claim_clean_review_ticket(actor,task['id'],task['version'])
        except BoardError:return None
    with ThreadPoolExecutor(max_workers=2) as executor:results=list(executor.map(take,actors))
    assert sum(r is not None for r in results)==1
    assert b.get_ticket(s['human'],task['id'])['ticket']['version']==task['version']+1


def test_human_and_non_review_never_use_guarded_contract(lead):
    s=lead;b=s['b'];task,_,_=released_review(s)
    with pytest.raises(BoardError):b.claim_clean_review_ticket(s['human'],task['id'],task['version'])
    task=b.update_ticket(s['human'],task['id'],task['version'],status='todo')
    _,actor,_=coder(s,key='reviewer')
    with pytest.raises(BoardError):b.claim_clean_review_ticket(actor,task['id'],task['version'])
