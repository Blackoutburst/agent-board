"""Sparse owner audit evidence must never broaden the release gate."""
from copy import deepcopy
import pytest
from blackoutburst_agents.historical_review_source import _owner_release


def evidence():
    scope=dict(owner_id='owner',ticket_id='ticket',project_id='project')
    old=dict(agent_id='reviewer',board_session_id='sealed-session')
    actor=dict(kind='agent',owner_id='owner',agent_id='reviewer',session_id='sealed-session',profile_version=5)
    claimed=dict(status='review',version=15,claimant=actor,lease_expires_at=200)
    ticket=dict(status='review',version=16,claimant=None)
    release=dict(id=20,event='ticket_updated',ticket_id='ticket',project_id='project',created_at=150,
        actor=dict(kind='human',owner_id='owner'),detail=dict(fields=['release_claim'],ticket_id='ticket',version=16))
    prior=dict(id=19,event='ticket_claimed',ticket_id='ticket',project_id='project',created_at=100,actor=actor.copy())
    return release,[release,prior],claimed,ticket,scope,old


def test_exact_same_owner_release_has_distinct_evidence():
    assert _owner_release(*evidence())


@pytest.mark.parametrize('attack',[
    'foreign_owner','companion','agent','session','mixed_fields','wrong_version','expired','unclaimed_before',
    'different_claim','intervening_event','missing_history','duplicate_history','wrong_project','later_claim',
    'claim_after_release','missing_lease',
])
def test_sparse_or_disconnected_evidence_is_refused(attack):
    release,chain,claimed,ticket,scope,old=deepcopy(evidence())
    if attack=='foreign_owner':release['actor']['owner_id']='other'
    elif attack=='companion':release['actor']['profile_kind']='companion'
    elif attack=='agent':release['actor']['kind']='agent'
    elif attack=='session':release['actor']['session_id']='managed-session'
    elif attack=='mixed_fields':release['detail']['fields'].append('status')
    elif attack=='wrong_version':ticket['version']=17
    elif attack=='expired':release['created_at']=200
    elif attack=='unclaimed_before':claimed['claimant']=None
    elif attack=='different_claim':chain[1]['actor']['session_id']='another-session'
    elif attack=='intervening_event':chain[1]['event']='ticket_updated'
    elif attack=='missing_history':chain.pop()
    elif attack=='duplicate_history':chain[1]=release
    elif attack=='wrong_project':chain[1]['project_id']='other'
    elif attack=='later_claim':chain[1]['id']=21
    elif attack=='claim_after_release':chain[1]['created_at']=151
    elif attack=='missing_lease':claimed.pop('lease_expires_at')
    assert not _owner_release(release,chain,claimed,ticket,scope,old)
