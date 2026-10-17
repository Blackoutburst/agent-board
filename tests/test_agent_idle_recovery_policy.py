"""An intentional owner/profile stop is not an abandoned assignment."""
import pytest

from board import BoardError
from test_agent_idle_recovery import idle, age, eligible, worker
from test_coordination import space, new
from test_delegation_families import family, open_family


@pytest.mark.parametrize('claimed', [False, True])
@pytest.mark.parametrize('control', [
    'owner_disabled', 'owner_requests_disabled', 'profile_requests_disabled',
    'profile_responses_only', 'runtime_paused', 'source_scope_removed', 'source_membership_removed',
])
def test_source_pause_cannot_be_bypassed_by_another_owner(idle, control, claimed):
    space, source_id, _, _, _ = idle
    board, human, other, project, original, *_ = space
    successor, _, _ = worker(board, other, 'Other owner successor')
    # An agent assignment does not require a redundant assignee_owner_id.
    ticket = new(space, assignee_agent_id=source_id)
    if claimed:
        ticket = board.claim_ticket(original, ticket['id'], ticket['version'])
    age(board, source_id, ticket, expired=claimed)
    if control == 'owner_disabled':
        board.set_policy(human, False, True, True, True, [], [])
    elif control == 'owner_requests_disabled':
        board.set_policy(human, True, True, True, False, [], [])
    elif control == 'source_scope_removed':
        profile = board.get_agent_profile(human, source_id)
        board.update_agent_profile(human, source_id, profile['version'], project_ids=[])
    elif control == 'source_membership_removed':
        # Public membership is immutable in v2; simulate an administrative
        # revocation while retaining the other owner's project membership.
        with board._tx() as db:
            db.execute('DELETE FROM project_members WHERE project_id=? AND owner_id=?',
                       (project['id'], human['owner_id']))
    elif control.startswith('profile_'):
        profile = board.get_agent_profile(human, source_id)
        policy = dict(profile['policy'])
        if control == 'profile_requests_disabled':
            policy['allow_requests'] = False
        else:
            policy['autonomy'] = 'responses'
        board.update_agent_profile(human, source_id, profile['version'], policy=policy)
    else:
        limits = board.get_runtime(human)['limits']
        board.update_runtime_limits(human, limits['version'], limits['max_concurrent'], True)
    assert ticket['id'] not in eligible(board, successor, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'],
                           'Observed old lease and no runtime effects; source policy is intentionally stopped')
    current = board.get_ticket(other, ticket['id'])['ticket']
    assert current['assignee_agent_id'] == source_id and current['version'] == ticket['version']


@pytest.mark.parametrize('target', ['family_child', 'resource_peer'])
def test_idle_assignment_cannot_escape_family_identity_or_resource_fence(family, target):
    board, human, project = family['b'], family['h'], family['project']
    opened = open_family(family)
    successor, _, _ = worker(board, human, 'Independent profile', skills=())
    child = board.get_ticket(human, opened['children'][0]['ticket_id'])['ticket']
    source_id = child['assignee_agent_id']
    if target == 'family_child':
        ticket = child
    else:
        ticket = board.create_ticket(human, project['id'], 'Resource peer',
                                    assignee_agent_id=source_id, resource='fixture:repo', idempotency_key='peer')
        # Expiry of the family's coordinator lease must not release its lock.
        age(board, source_id, family['parent'], expired=True)
    age(board, source_id, ticket)
    assert ticket['id'] not in eligible(board, successor, project['id'])
    with pytest.raises(BoardError):
        board.claim_ticket(successor, ticket['id'], ticket['version'])
    assert board.get_ticket(human, ticket['id'])['ticket']['assignee_agent_id'] == source_id
