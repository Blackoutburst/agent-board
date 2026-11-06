"""The dedicated MCP name and receipt are an explicit wire contract."""
from test_api import application
from test_profile_management_api import lead_browser,call


def test_mcp_clean_review_claim_returns_an_exact_bound_receipt(lead_browser):
    s=lead_browser;b=s[1]
    ticket=b.create_ticket(s[2],s[4]['id'],'Exact review',
        assignee_owner_id=s[2]['owner_id'],assignee_agent_id=s[5]['id'],resource='repo:fixture')
    ticket=b.update_ticket(s[2],ticket['id'],ticket['version'],status='review')
    args=dict(ticket_id=ticket['id'],expected_version=ticket['version'])
    receipt=call(s,'claim_clean_review_ticket',args)
    assert receipt['schema']=='clean-review-claim-v1'
    assert receipt['prior_version']==ticket['version'] and receipt['session_id']=='lead-api'
    assert receipt['ticket']['version']==ticket['version']+1
    assert receipt['ticket']['claimant']['session_id']=='lead-api'
    assert call(s,'claim_clean_review_ticket',args)==receipt


def test_mcp_clean_review_claim_refuses_non_review_without_change(lead_browser):
    s=lead_browser;b=s[1]
    ticket=b.create_ticket(s[2],s[4]['id'],'Not yet a review',resource='repo:fixture')
    call(s,'claim_clean_review_ticket',dict(ticket_id=ticket['id'],expected_version=ticket['version']),error=True)
    assert b.get_ticket(s[2],ticket['id'])['ticket']==ticket
