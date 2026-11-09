"""Exercise actual Board boundaries; no live migration or model execution."""
import json

import pytest

from board import BoardError
from test_hierarchy_store import setup, enabled, new_project, bind_created, leaf_policy


def test_created_profile_reads_claims_and_loses_access_on_root_revocation(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    actor = bind_created(s, result); b = s['b']
    assert b.get_project(actor, result['project_id'])['id'] == result['project_id']
    ticket = b.create_ticket(s['human'], result['project_id'], 'Work', assignee_owner_id='alice',
        assignee_agent_id=result['profile_id'], resource='repo:work', idempotency_key='work')
    claimed = b.claim_ticket(actor, ticket['id'], ticket['version'])
    s['store'].change(s['human'], root['id'], 1, 'revoke-root', revoke=True)
    for call in (lambda: b.get_project(actor, result['project_id']),
                 lambda: b.get_ticket(actor, ticket['id']),
                 lambda: b.renew_ticket(actor, ticket['id'], claimed['version']),
                 lambda: b.reserve_execution(actor, idempotency_key='reserve', project_id=result['project_id'],
                    ticket_id=ticket['id'], work_id='work', turn_id='turn', provider='codex_chatgpt', model='fixture')):
        with pytest.raises(BoardError, match='hierarchy_revoked'): call()
    # Revocation does not fabricate release or erase the claimed work.
    current = b.get_ticket(s['human'], ticket['id'])['ticket']
    assert current['claimant'] is not None


def test_created_scope_reduction_blocks_real_project_read_without_profile_rewrite(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    actor = bind_created(s, result)
    s['store'].configure(s['human'], 1, s['ceiling'](allow_new_projects=False), 'reduce')
    with pytest.raises(BoardError, match='Project not found'):
        s['b'].get_project(actor, result['project_id'])
    with s['b']._connection() as db:
        profile = db.execute('SELECT * FROM agent_profiles WHERE id=?', (result['profile_id'],)).fetchone()
        assert json.loads(profile['project_ids']) == [result['project_id']]


def test_root_created_project_read_uses_binding_and_revocation_removes_it(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    assert s['b'].get_project(s['parent_actor'], result['project_id'])['id'] == result['project_id']
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    with pytest.raises(BoardError, match='Project not found'):
        s['b'].get_project(s['parent_actor'], result['project_id'])
    # Root's preexisting ordinary scope is not an implicit hierarchy grant.
    original = s['ceiling']()['project_ids'][0]
    assert s['b'].get_project(s['parent_actor'], original)['id'] == original


def test_ancestor_pause_blocks_existing_descendant_session(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    actor = bind_created(s, result)
    s['b'].update_agent_profile(s['human'], s['parent']['id'], s['parent']['version'], status='paused')
    with pytest.raises(BoardError, match='hierarchy_ancestor_profile_unavailable'):
        s['b'].get_project(actor, result['project_id'])


def test_current_ancestor_actions_intersect_registration_and_effective_policy(setup):
    s = setup; root = enabled(s); result = new_project(s, root)
    actor = bind_created(s, result)
    with s['b']._connection() as db:
        parent = s['b']._profile_owned(db, s['human'], s['parent']['id'])
        policy = json.loads(parent['policy'])
    policy['actions'] = ['repo.read_file']
    s['b'].update_agent_profile(s['human'], s['parent']['id'], s['parent']['version'], policy=policy)
    with s['b']._connection() as db:
        profile = s['b']._profile_for_actor(db, actor)
        effective = s['b']._effective_profile_policy(db, actor, profile)
        assert effective['actions'] == ['repo.read_file']
