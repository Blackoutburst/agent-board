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


def reserved_hierarchy(s):
    root = enabled(s); result = new_project(s, root); actor = bind_created(s, result)
    b = s['b']
    ticket = b.create_ticket(s['human'], result['project_id'], 'Runtime work', assignee_owner_id='alice',
        assignee_agent_id=result['profile_id'], resource='repo:work', idempotency_key='runtime-work')
    b.claim_ticket(actor, ticket['id'], ticket['version'])
    execution = b.reserve_execution(actor, idempotency_key='reservation', project_id=result['project_id'],
        ticket_id=ticket['id'], work_id='work', turn_id='turn', provider='codex_chatgpt', model='fixture')['execution']
    return root, result, actor, execution


def test_revocation_between_reservation_and_start_prevents_start_but_allows_cleanup(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    control = s['b'].get_execution_control(actor, reserved['id'])
    assert control['should_stop'] and control['control_reason'] == 'authorization_changed'
    with pytest.raises(BoardError, match='hierarchy_revoked'):
        s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    cleaned = s['b'].finish_execution(actor, reserved['id'], reserved['revision'], 'cancelled', True, 'policy_changed')
    assert cleaned['state'] == 'finished' and cleaned['cleanup_confirmed']


def test_revocation_after_running_denies_completed_result_but_not_bounded_cleanup(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    running = s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    with pytest.raises(BoardError, match='hierarchy_revoked'):
        s['b'].finish_execution(actor, running['id'], running['revision'], 'completed', True, 'completed')
    pending = s['b'].finish_execution(actor, running['id'], running['revision'], 'cancelled', False, 'policy_changed')
    assert pending['state'] == 'needs_review' and not pending['cleanup_confirmed']
    with s['b']._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM runtime_executions WHERE state!='finished'").fetchone()[0] == 1
    cleaned = s['b'].finish_execution(actor, pending['id'], pending['revision'], 'cancelled', True, 'policy_changed')
    assert cleaned['state'] == 'finished'
    # Only exact harmless receipt replay survives terminal cleanup.
    assert s['b'].finish_execution(actor, pending['id'], pending['revision'], 'cancelled', True, 'policy_changed')['revision'] == cleaned['revision']
    with pytest.raises(BoardError):
        s['b'].finish_execution(actor, cleaned['id'], cleaned['revision'], 'blocked', True, 'runner_error')
    with pytest.raises(BoardError):
        s['b'].get_execution_control(s['target_actor'], cleaned['id'])


def test_revocation_still_allows_human_inspection_but_device_revocation_closes_cleanup_channel(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    assert s['b'].get_runtime(s['human'])
    s['b'].revoke_device(s['human'], actor['device_id'])
    with pytest.raises(BoardError, match='execution_identity_unavailable'):
        s['b'].get_execution_control(actor, reserved['id'])


def test_already_recorded_completion_can_finish_its_cleanup_after_revocation(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    running = s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    pending = s['b'].finish_execution(actor, running['id'], running['revision'], 'completed', False, 'completed')
    s['store'].change(s['human'], root['id'], 1, 'revoke', revoke=True)
    cleaned = s['b'].finish_execution(actor, pending['id'], pending['revision'], 'completed', True, 'completed')
    assert cleaned['state'] == 'finished' and cleaned['cleanup_confirmed']


def test_current_permission_reduction_blocks_reserved_start_and_new_completion(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    s['store'].configure(s['human'], 1, s['ceiling'](allow_new_projects=False), 'reduce-owner')
    with pytest.raises(BoardError): s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    assert s['b'].get_execution_control(actor, reserved['id'])['should_stop']


def test_resource_revocation_after_reserve_blocks_start_and_control_requests_stop(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    reduced = s['ceiling'](resources=[])
    s['store'].change(s['human'], root['id'], 1, 'reduce-resource', policy=reduced)
    with pytest.raises(BoardError, match='hierarchy_runtime_resource_denied'):
        s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    assert s['b'].get_execution_control(actor, reserved['id'])['should_stop']


def test_ancestor_policy_pin_blocks_changed_admission_and_success_result(setup):
    s = setup; root, result, actor, reserved = reserved_hierarchy(s)
    running = s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    s['store'].change(s['human'], root['id'], 1, 'reduce-actions', policy=s['ceiling'](actions=['repo.read_file']))
    with pytest.raises(BoardError, match='hierarchy_execution_scope_changed'):
        s['b'].finish_execution(actor, running['id'], running['revision'], 'completed', True, 'completed')
    assert s['b'].get_execution_control(actor, reserved['id'])['control_reason'] == 'authorization_changed'


def reserved_message_hierarchy(s):
    root = enabled(s); result = new_project(s, root); b = s['b']
    # Messaging remains a separate explicit profile grant in this fixture.
    profile = b.get_agent_profile(s['human'], result['profile_id'])
    policy = dict(profile['policy'], allow_messages=True)
    b.update_agent_profile(s['human'], result['profile_id'], profile['version'], policy=policy)
    device = b.create_device(s['human'], 'message-lead', [])
    b.bind_agent_profile(s['human'], result['profile_id'], 2, device['id'], 'message-lead', 'codex')
    raw = b.authenticate(device['token'])
    b.register_agent(raw, 'message-lead', 'message-lead', [], 'codex', True, 'message-lead')
    actor = b.actor_session(raw, 'message-lead')
    ticket = b.create_ticket(s['human'], result['project_id'], 'Conversation', resource='repo:work', idempotency_key='message-ticket')
    source = b.post_message(s['human'], ticket['thread_id'], 'Please reply',
        mentions=['agent:' + result['profile_id']], idempotency_key='source')
    sha = b.get_message(actor, ticket['thread_id'], source['id'])['source_sha256']
    execution = b.reserve_message_execution(actor, 'message-turn', ticket['thread_id'], source['id'], sha,
        'message-work', 'message-turn', 'codex_chatgpt', 'fixture')['execution']
    return root, result, actor, execution, ticket, source


def test_message_admission_pin_changes_after_ancestor_policy_reduction(setup):
    s = setup; root, result, actor, reserved, ticket, source = reserved_message_hierarchy(s)
    s['store'].change(s['human'], root['id'], 1, 'reduce-message', policy=s['ceiling'](actions=['repo.read_file']))
    with pytest.raises(BoardError, match='hierarchy_execution_scope_changed'):
        s['b'].start_execution(actor, reserved['id'], reserved['revision'])
    assert s['b'].get_execution_control(actor, reserved['id'])['should_stop']
    assert s['b'].finish_execution(actor, reserved['id'], reserved['revision'], 'cancelled', True, 'policy_changed')['state'] == 'finished'


def test_message_success_and_all_new_publications_are_refused_after_revoke(setup):
    s = setup; root, result, actor, reserved, ticket, source = reserved_message_hierarchy(s)
    b = s['b']; running = b.start_execution(actor, reserved['id'], reserved['revision'])
    s['store'].change(s['human'], root['id'], 1, 'revoke-message', revoke=True)
    for call in (lambda: b.post_message(actor, ticket['thread_id'], 'Success', reply_to=source['id'], idempotency_key='publish'),
                 lambda: b.update_ticket(actor, ticket['id'], ticket['version'], progress='Done'),
                 lambda: b.add_attachment(actor, ticket['thread_id'], 'result.txt', 'text/plain', b'new result', 'publish-file'),
                 lambda: b.finish_execution(actor, running['id'], running['revision'], 'completed', True, 'completed')):
        with pytest.raises(BoardError, match='hierarchy_revoked'): call()


def test_message_seal_and_reservation_use_the_same_hierarchy_payload(setup):
    s = setup; root, result, actor, reserved, ticket, source = reserved_message_hierarchy(s)
    b = s['b']; sha = b.get_message(actor, ticket['thread_id'], source['id'])['source_sha256']
    args = (actor, 'sealed-turn', ticket['thread_id'], source['id'], sha,
            'sealed-work', 'sealed-turn', 'codex_chatgpt', 'fixture')
    first = b.seal_message_execution_admission(*args)
    assert b.seal_message_execution_admission(*args) == first
    with pytest.raises(BoardError, match='execution_admission_refused'):
        b.reserve_message_execution(*args)


def test_archive_requires_normal_claim_release_and_confirmed_runtime_cleanup(setup):
    s = setup; root = enabled(s); b = s['b']
    project = s['ceiling']()['project_ids'][0]
    result = s['store'].create_team_profile(s['parent_actor'], root['id'], 'Developer', 'developer',
                                          leaf_policy(project), 1, 'archivable-team')
    actor = bind_created(s, result)
    ticket = b.create_ticket(s['human'], project, 'Work', assignee_owner_id='alice',
        assignee_agent_id=result['profile_id'], resource='repo:work', idempotency_key='archive-work')
    claimed = b.claim_ticket(actor, ticket['id'], ticket['version'])
    reserved = b.reserve_execution(actor, 'archive-reserve', project, ticket['id'],
        'archive-work', 'archive-turn', 'codex_chatgpt', 'fixture')['execution']
    profile_version = b.get_agent_profile(s['human'], result['profile_id'])['version']
    def archive():
        return s['store'].archive_team_profile(s['parent_actor'], root['id'], result['profile_id'], profile_version, 'archive')
    with pytest.raises(BoardError, match='hierarchy_archive_claim_present'): archive()
    b.update_ticket(s['human'], ticket['id'], claimed['version'], release_claim=True)
    with pytest.raises(BoardError, match='hierarchy_archive_cleanup_unconfirmed'): archive()
    pending = b.finish_execution(actor, reserved['id'], reserved['revision'], 'cancelled', False, 'claim_lost')
    with pytest.raises(BoardError, match='hierarchy_archive_cleanup_unconfirmed'): archive()
    b.finish_execution(actor, pending['id'], pending['revision'], 'cancelled', True, 'claim_lost')
    assert archive()['version'] == profile_version + 1
    assert b.get_ticket(s['human'], ticket['id'])['ticket']['claimant'] is None
    with b._connection() as db:
        row = db.execute('SELECT * FROM runtime_executions WHERE id=?', (reserved['id'],)).fetchone()
        assert row['state'] == 'finished' and row['cleanup_confirmed'] == 1


@pytest.mark.parametrize('reduction', ['root', 'owner'])
def test_effective_responses_autonomy_blocks_new_requests_and_runtime_admission(setup, reduction):
    s = setup; root, result, actor, execution = reserved_hierarchy(s)
    b = s['b']
    reduced = s['ceiling'](autonomy='responses')
    if reduction == 'root':
        s['store'].change(s['human'], root['id'], 1, 'responses-only', policy=reduced)
    else:
        s['store'].configure(s['human'], 1, reduced, 'responses-only')
    for call in (lambda: b.create_ticket(actor, result['project_id'], 'Forbidden', idempotency_key='forbidden'),
                 lambda: b.claim_ticket(actor, execution['ticket_id'], b.get_ticket(s['human'], execution['ticket_id'])['ticket']['version']),
                 lambda: b.renew_ticket(actor, execution['ticket_id'], b.get_ticket(s['human'], execution['ticket_id'])['ticket']['version']),
                 lambda: b.reserve_execution(actor, 'new-request', result['project_id'], execution['ticket_id'],
                    'new-work', 'new-turn', 'codex_chatgpt', 'fixture')):
        with pytest.raises(BoardError, match='hierarchy_ancestor_permission_denied'): call()
    assert b.get_execution_control(actor, execution['id'])['should_stop']
    assert b.finish_execution(actor, execution['id'], execution['revision'], 'cancelled', True, 'policy_changed')['state'] == 'finished'


def test_responses_only_still_allows_message_reply_admission(setup):
    s = setup; root, result, actor, execution, ticket, source = reserved_message_hierarchy(s)
    b = s['b']
    s['store'].change(s['human'], root['id'], 1, 'responses-only', policy=s['ceiling'](autonomy='responses'))
    b.finish_execution(actor, execution['id'], execution['revision'], 'cancelled', True, 'policy_changed')
    reply = b.post_message(actor, ticket['thread_id'], 'Reply only', reply_to=source['id'], idempotency_key='response')
    assert reply['reply_to'] == source['id']
    sha = b.get_message(actor, ticket['thread_id'], source['id'])['source_sha256']
    assert b.reserve_message_execution(actor, 'response-only-turn', ticket['thread_id'], source['id'], sha,
        'response-work', 'response-turn', 'codex_chatgpt', 'fixture')['reserved']
