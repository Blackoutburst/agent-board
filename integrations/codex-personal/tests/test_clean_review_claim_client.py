"""A legacy or malformed reply cannot authorize the protected review path."""
from copy import deepcopy
import pytest
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.review_lifecycle_client import DeliveryReviewLifecycleClient
from blackoutburst_agents.historical_review_source import _claim_clean_review


def client(reply):
    c=DeliveryReviewLifecycleClient(dict(mcp_url='https://board.test/mcp',token='SYNTHETIC',client='codex',
        identity_key='reviewer',name='Reviewer'),'session',3)
    c._initialize=lambda:None;c.pin_registration({'session':{'id':'session'}});c.set_effect_guard(lambda:None)
    c._tool=lambda name,args:reply
    return c


def receipt():
    return dict(schema='clean-review-claim-v1',prior_version=5,session_id='session',
        ticket=dict(id='ticket',version=6,status='review',claimant=dict(kind='agent',session_id='session')))


def test_exact_new_contract_returns_the_claimed_ticket():
    r=receipt();assert _claim_clean_review(client(r),dict(ticket_id='ticket',expected_version=5))==r['ticket']


@pytest.mark.parametrize('attack',['legacy','schema','session','version','ticket','claimant','status','unknown','boolean_version'])
def test_contract_downgrade_or_substitution_is_refused(attack):
    r=deepcopy(receipt())
    if attack=='legacy':r=r['ticket']
    elif attack=='schema':r['schema']='old'
    elif attack=='session':r['session_id']='other'
    elif attack=='version':r['prior_version']=4
    elif attack=='ticket':r['ticket']['id']='other'
    elif attack=='claimant':r['ticket']['claimant']['session_id']='other'
    elif attack=='status':r['ticket']['status']='todo'
    elif attack=='unknown':r['unchecked']=True
    elif attack=='boolean_version':r['prior_version']=True
    with pytest.raises(BoardReadError,match='review_cleanup_claim_unconfirmed'):
        _claim_clean_review(client(r),dict(ticket_id='ticket',expected_version=5))


def test_native_controller_still_refuses_the_host_only_tool():
    with pytest.raises(BoardReadError,match='review_controller_tool_not_allowed'):
        client(receipt()).call('claim_clean_review_ticket',dict(ticket_id='ticket',expected_version=5))


def test_stop_during_initialize_refuses_the_host_mutation():
    c=client(receipt());stopped=[False];calls=[]
    c._initialize=lambda:stopped.__setitem__(0,True)
    def guard():
        if stopped[0]:raise BoardReadError('stopped')
    c.set_effect_guard(guard);c._tool=lambda *args:calls.append(args)
    with pytest.raises(BoardReadError,match='stopped'):
        _claim_clean_review(c,dict(ticket_id='ticket',expected_version=5))
    assert calls==[]
