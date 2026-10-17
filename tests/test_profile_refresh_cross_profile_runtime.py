"""API-only regression from the independent PR 28 review.

All state lives below pytest's tmp_path. No app import, real credentials, network,
SQL writes or subprocesses are required. The original four cases reproduced the
cleanup gap at 51ccc0e3f7db54889b4ff6bd398f50b0fd598033.
"""
import json

import pytest

from board import Board, BoardError


POLICY = {
    'allow_read': True, 'allow_messages': True, 'allow_requests': True,
    'actions': ['inspect'], 'resources': ['repo:test'], 'autonomy': 'requested',
}
EVIDENCE = 'Stopped this session operation and inspected its local processes and effects; no pending write remains.'


def worker(board, human, project, name):
    profile = board.create_agent_profile(human, name, policy=POLICY, project_ids=[project['id']])
    device = board.create_device(human, name + ' device', [])
    profile = board.bind_agent_profile(human, profile['id'], profile['version'], device['id'], name, 'codex')
    raw = board.authenticate(device['token'])
    board.register_agent(raw, name, name, [], 'codex', False, name)
    return profile, raw, board.actor_session(raw, name)


@pytest.mark.parametrize('stage', ['inspect', 'refresh'])
@pytest.mark.parametrize('replacement_owner', ['owner', 'other'])
def test_other_profile_unresolved_execution_blocks_refresh_of_claimed_ticket(tmp_path, stage, replacement_owner):
    board = Board(tmp_path / 'isolated-board')
    for owner in ('owner', 'other'):
        board.ensure_owner(owner, 'Synthetic ' + owner)
        board.set_policy({'kind': 'human', 'owner_id': owner}, True, True, True, True, ['inspect'], ['repo:test'])
    human = {'kind': 'human', 'owner_id': 'owner'}
    replacement_human = {'kind': 'human', 'owner_id': replacement_owner}
    project = board.create_project(human, 'Synthetic project', ['owner', 'other'], idempotency_key='project')
    old_profile, _, old_actor = worker(board, human, project, 'old-worker')
    new_profile, new_raw, new_actor = worker(board, replacement_human, project, 'new-worker')
    assert old_profile['id'] != new_profile['id']

    ticket = board.create_ticket(human, project['id'], 'Synthetic task', resource='repo:test',
        assignee_owner_id=human['owner_id'], assignee_agent_id=old_profile['id'])
    claimed = board.claim_ticket(old_actor, ticket['id'], ticket['version'])
    execution = board.reserve_execution(old_actor, 'unresolved-old-execution', project['id'], ticket['id'],
        'old-work', 'old-turn', 'codex_chatgpt', 'fixture-model')['execution']

    # Supported human release and normal claim transfer; do not alter Runtime.
    released = board.update_ticket(human, ticket['id'], claimed['version'], release_claim=True,
        assignee_owner_id=None, assignee_agent_id=None)
    replacement = board.claim_ticket(new_actor, ticket['id'], released['version'])
    assert replacement['claimant']['session_id'] == 'new-worker'
    control = board.get_execution_control(old_actor, execution['id'])
    assert control['should_stop'] is True
    with board._connection() as db:
        retained = db.execute('SELECT state,cleanup_confirmed,agent_id,ticket_id FROM runtime_executions WHERE id=?',
            (execution['id'],)).fetchone()
        assert retained['agent_id'] == old_profile['id'] and retained['ticket_id'] == ticket['id']
        assert retained['state'] != 'finished' or retained['cleanup_confirmed'] != 1

    changed = board.update_agent_profile(replacement_human, new_profile['id'], new_profile['version'], description='Cosmetic edit')
    plan = board.inspect_profile_refresh(new_raw, 'new-worker')
    assert plan['mode'] == 'preserve_claims'
    assert plan['to_profile_version'] == changed['version']
    # Only a generic blocker is disclosed, never the prior worker's private
    # Runtime identity, execution identifier or operation metadata.
    assert old_profile['id'] not in json.dumps(plan)
    assert execution['id'] not in json.dumps(plan)
    assert 'old-worker' not in json.dumps(plan)
    if stage == 'inspect':
        assert 'runtime_cleanup_required' in plan['blocked_reasons']
    else:
        with pytest.raises(BoardError, match='runtime_cleanup_required'):
            board.refresh_agent_session(new_raw, session_id='new-worker',
                from_profile_version=plan['from_profile_version'],
                expected_profile_version=plan['to_profile_version'],
                configuration_sha256=plan['configuration_sha256'], expected_claims=plan['claims'],
                idempotency_key='checked-refresh', recovery_evidence=EVIDENCE)

    # The original executor can still record cleanup through its identity-only
    # channel after a transfer. Only that receipt makes the refresh available.
    finished = board.finish_execution(old_actor, execution['id'], execution['revision'],
        'cancelled', True, 'policy_changed')
    clean_plan = board.inspect_profile_refresh(new_raw, 'new-worker')
    assert 'runtime_cleanup_required' not in clean_plan['blocked_reasons']
    result = board.refresh_agent_session(new_raw, session_id='new-worker',
        from_profile_version=clean_plan['from_profile_version'],
        expected_profile_version=clean_plan['to_profile_version'],
        configuration_sha256=clean_plan['configuration_sha256'], expected_claims=clean_plan['claims'],
        idempotency_key='after-prior-cleanup', recovery_evidence=EVIDENCE)
    assert result['mode'] == 'preserve_claims'
    assert finished['profile_version'] == old_profile['version']
