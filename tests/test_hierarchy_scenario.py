"""Integrated real SQLite/Board authorization scenario; no model transport.

Human bindings below exist only in each test's temporary Board. This does not
qualify live cloud execution or authorize provisioning a production identity.
"""
import pytest
from board import BoardError
from test_hierarchy_store import setup, enabled, new_project, leaf_policy


def bind_profile(s, identifier, identity):
    b, human = s['b'], s['human']
    profile = b.get_agent_profile(human, identifier)
    device = b.create_device(human, identity, [])
    b.bind_agent_profile(human, identifier, profile['version'], device['id'], identity, 'codex')
    raw = b.authenticate(device['token'])
    b.register_agent(raw, identity, identity, [], 'codex', True, identity)
    return b.actor_session(raw, identity)


def test_project_lead_team_assignment_claims_and_revocation_share_one_chain(setup):
    s = setup; b = s['b']; store = s['store']; root = enabled(s)
    created = new_project(s, root)
    lead = bind_profile(s, created['profile_id'], 'scenario-lead')
    leaves = [store.create_team_profile(lead, created['id'], role.title(), role,
        leaf_policy(created['project_id']), 1, role) for role in ('developer','reviewer')]
    assert len({leaf['profile_id'] for leaf in leaves}) == 2
    actors = [bind_profile(s, leaf['profile_id'], role) for leaf, role in zip(leaves, ('developer','reviewer'))]
    tickets = [b.create_ticket(s['human'], created['project_id'], role+' work',
        assignee_owner_id='alice', assignee_agent_id=leaf['profile_id'], resource='repo:work',
        idempotency_key=role) for leaf, role in zip(leaves, ('developer','reviewer'))]
    # A project role never permits adopting a sibling's assignment.
    for actor, ticket in zip(actors, reversed(tickets)):
        with pytest.raises(BoardError): b.claim_ticket(actor, ticket['id'], ticket['version'])
    developer = b.claim_ticket(actors[0], tickets[0]['id'], tickets[0]['version'])
    with pytest.raises(BoardError, match='resource is already in use'):
        b.claim_ticket(actors[1], tickets[1]['id'], tickets[1]['version'])
    released = b.release_ticket(actors[0], developer['id'], developer['version'],
                                'handoff-review', 'Synthetic sequential handoff; no model executed')
    reviewer = b.claim_ticket(actors[1], tickets[1]['id'], tickets[1]['version'])
    assert developer['claimant']['session_id'] != reviewer['claimant']['session_id']
    claimed = [released, reviewer]
    for actor, leaf in zip(actors, leaves):
        page = store.list_for_agent(actor)
        assert [item['delegation']['id'] for item in page['items']] == [leaf['id']]
        with pytest.raises(BoardError): b.get_project(actor, s['ceiling']()['project_ids'][0])
    store.change(s['human'],root['id'],1,'revoke-chain',revoke=True)
    for actor,ticket in zip(actors,claimed):
        with pytest.raises(BoardError): b.renew_ticket(actor,ticket['id'],ticket['version'])
        with pytest.raises(BoardError): b.get_project(actor,created['project_id'])
    # Human history preserves the released developer and claimed reviewer states.
    for ticket in claimed:
        assert b.get_ticket(s['human'],ticket['id'])['ticket']['claimant'] == ticket['claimant']
    with b._connection() as db:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute('SELECT COUNT(*) FROM hierarchy_project_bindings').fetchone()[0] == 1
