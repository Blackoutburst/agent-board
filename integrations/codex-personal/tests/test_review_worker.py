"""Real temporary Board/Runtime, private journals and synthetic tool-free turns."""
from copy import deepcopy
import hashlib
import json
import sys
import time
import uuid

import pytest

from blackoutburst_agents.context import ContextScope, build_context
from blackoutburst_agents.review_worker import DeliveryReviewWorker
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.work_journal import WorkJournal
from test_coordination import space, new  # noqa: F401
from test_review_queue import reviewer, deliver


class Client:
    endpoint = 'https://board.test/mcp'
    def __init__(self, board, actor):
        self.board, self.actor, self.session_id = board, actor, actor['session_id']
        self.calls, self.arguments, self.lost, self.before = [], [], set(), None
    def register(self):
        # A real already-bound reviewer profile/session, never another identity.
        return self.board.register_agent(self.actor, self.session_id, 'Reviewer', ['python'], 'claude-code', True, 'reviewer')
    def call(self, name, arguments):
        self.calls.append(name)
        self.arguments.append((name, deepcopy(arguments)))
        if self.before:
            self.before(name, arguments)
        value = getattr(self.board, name)(self.actor, **arguments)
        if name in self.lost:
            self.lost.remove(name)
            raise TimeoutError('PRIVATE_DIAGNOSTIC')
        return value


@pytest.fixture
def review_case(space, tmp_path):
    board, human, _, project, author, *_ = space
    actor = reviewer(space)
    task = deliver(board, author, new(space))
    board.claim_ticket(actor, task['id'], task['version'])
    client = Client(board, actor)
    registration = client.register()
    author_id = board.register_agent(author, author['session_id'], 'Author', ['python'], 'codex', True, 'installation')['identity']['id']
    scope = {'owner_id': human['owner_id'], 'agent_id': registration['identity']['id'], 'project_id': project['id'],
             'ticket_id': task['id'], 'board_session_id': client.session_id, 'board_endpoint': client.endpoint}
    context = build_context(client.call, scope=ContextScope(**{key: scope[key] for key in
        ('owner_id', 'agent_id', 'project_id', 'ticket_id')}), registration=registration)
    material = 'Synthetic exact patch supplied by an authorized delivery controller.'
    selection = {'schema': 'delivery-review-selection-v1', 'author_agent_id': author_id,
                 'delivery_sha': 'a' * 40, 'context_sha256': context['fingerprint'],
                 'material_sha256': hashlib.sha256(material.encode()).hexdigest()}
    journal, store, runtime = WorkJournal(tmp_path/'work'), RunStore(tmp_path/'runs'), RuntimeJournal(tmp_path/'runtime')
    case = {'board': board, 'human': human, 'actor': actor, 'author': author, 'client': client, 'scope': scope,
            'journal': journal, 'store': store, 'runtime': runtime, 'calls': [], 'guards': [], 'during': None,
            'guard_result': True, 'opinion': {'verdict': 'corrections', 'text': 'Add the missing boundary test.'}}
    def guard(**arguments):
        case['guards'].append(deepcopy(arguments))
        return case['guard_result']
    def transport(settings, prompt, workspace, **callbacks):
        callbacks['before_prompt']()
        case['calls'].append(prompt)
        callbacks['on_started'](999999999)
        session = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        if case['during']:
            case['during'](callbacks)
        return {'status': 'completed', 'text': json.dumps(case['opinion']), 'session_id': session,
                'verification': {'cleanup_confirmed': True, 'tool_free': True}}
    def worker(delivery_guard=guard):
        return DeliveryReviewWorker(case['journal'], store, client, runtime, client, delivery_guard=delivery_guard,
            runner_factory=lambda state, **options: PersonalRunner(state, transport, **options))
    case.update(worker=worker, args={'scope': scope, 'request_id': 'review-request', 'selection': selection,
        'instruction': 'Review the supplied patch against the stated acceptance criteria.', 'material': material,
        'workspace': tmp_path, 'executable': sys.executable, 'model': 'fixture-model', 'timeout': 10})
    yield case
    case['journal'].close(); store.close(); runtime.close()


def run(case, **changes):
    return case['worker']().run(**{**case['args'], **changes})


def ticket(case):
    return case['board'].get_ticket(case['human'], case['scope']['ticket_id'])['ticket']


def test_exact_review_finishes_runtime_and_keeps_only_private_opinion(review_case):
    case = review_case
    before = ticket(case)
    result = run(case)
    assert result['status'] == 'complete' and result['verdict'] == 'corrections'
    op = case['journal'].get(result['operation_id'])
    assert op['configuration']['work_phase'] == 'review'
    assert op['payload']['opinion'] == {**case['opinion'], 'selection': case['args']['selection']}
    assert ticket(case) == before
    assert not {'claim_ticket', 'release_ticket', 'update_ticket', 'post_message'} & set(case['client'].calls)
    execution = case['board'].get_runtime(case['human'])['executions'][0]
    assert execution['state'] == 'finished' and execution['cleanup_confirmed'] is True
    with case['board']._connection() as db:
        payload = json.loads(db.execute('SELECT payload FROM runtime_executions WHERE id=?', (execution['id'],)).fetchone()[0])
    assert payload['review_scope']['source_sha256']
    assert 'Add the missing' not in json.dumps(result)
    assert 'board-review-' in case['store'].get_work(op['payload']['work_id'])['agent_id']
    assert len(case['calls']) == 1
    assert run(case)['cached'] is True
    assert len(case['calls']) == 1 and case['client'].calls.count('start_execution') == 1
    reservations = [args for name, args in case['client'].arguments if name == 'reserve_execution']
    assert len(reservations) >= 2 and all(args == reservations[0] for args in reservations)
    assert len(case['board'].get_runtime(case['human'])['executions']) == 1


@pytest.mark.parametrize('guard', [None, lambda **_: False, lambda **_: {'verified': True}])
def test_missing_or_unproved_delivery_never_reserves_or_runs(review_case, guard):
    case = review_case
    result = case['worker'](guard).run(**case['args'])
    assert result['status'] == 'needs_review'
    assert result['reason'] in {'delivery_guard_required', 'delivery_proof_unavailable'}
    assert not case['calls'] and 'reserve_execution' not in case['client'].calls


def test_author_cannot_review_by_declaring_a_different_author(review_case):
    case = review_case
    board, author, task = case['board'], case['author'], ticket(case)
    released = board.release_ticket(case['actor'], task['id'], task['version'], 'review-release', 'Author test', status='review', unassign_self=True)
    board.claim_ticket(author, task['id'], released['version'])
    author_client = Client(board, author)
    author_client.register = lambda: board.register_agent(author, author['session_id'], 'Author', ['python'], 'codex', True, 'installation')
    author_registration = author_client.register()
    scope = {**case['scope'], 'agent_id': author_registration['identity']['id'], 'board_session_id': author['session_id']}
    context = build_context(author_client.call, scope=ContextScope(**{key: scope[key] for key in
        ('owner_id', 'agent_id', 'project_id', 'ticket_id')}), registration=author_client.register())
    selection = {**case['args']['selection'], 'author_agent_id': 'forged-other-author', 'context_sha256': context['fingerprint']}
    worker = DeliveryReviewWorker(case['journal'], case['store'], author_client, case['runtime'], author_client,
        delivery_guard=lambda **_: True, runner_factory=lambda state, **options: PersonalRunner(state,
            lambda *_args, **_kwargs: pytest.fail('author model must never run'), **options))
    result = worker.run(**{**case['args'], 'scope': scope, 'selection': selection})
    assert result['status'] == 'needs_review'
    assert case['board'].get_runtime(case['human'])['executions'] == []


def test_declared_same_profile_and_tampered_material_fail_before_runtime(review_case):
    case = review_case
    same = {**case['args']['selection'], 'author_agent_id': case['scope']['agent_id']}
    assert run(case, selection=same)['reason'] == 'distinct_reviewer_required'
    assert run(case, material='Different patch')['reason'] == 'invalid_review_selection'
    assert not case['calls'] and 'reserve_execution' not in case['client'].calls


@pytest.mark.parametrize('change', ['delivery', 'claim', 'proof'])
def test_change_during_turn_never_seals_opinion_for_another_delivery(review_case, change):
    case = review_case
    def during(callbacks):
        task = ticket(case)
        if change == 'delivery':
            edited = case['board'].update_ticket(case['human'], task['id'], task['version'], evidence='Transient edit')
            case['board'].update_ticket(case['human'], task['id'], edited['version'], evidence=task['evidence'])
        elif change == 'claim':
            released = case['board'].release_ticket(case['actor'], task['id'], task['version'], 'reclaim', 'Reclaim', status='review')
            case['board'].claim_ticket(case['actor'], task['id'], released['version'])
        else:
            case['guard_result'] = False
        assert callbacks['should_cancel']() is True
    case['during'] = during
    result = run(case)
    assert result['status'] == 'needs_review'
    op = case['journal'].get(result['operation_id'])
    assert 'opinion' not in op['payload'] and len(case['calls']) == 1
    assert run(case)['cached'] is True and len(case['calls']) == 1


def test_lost_local_result_write_resumes_same_turn_and_runtime_without_model(review_case):
    case = review_case
    original, fail = case['journal'].update, [True]
    def update(*args, **kwargs):
        if kwargs.get('stage') == 'result_ready' and fail:
            fail.pop()
            raise OSError('PRIVATE_DISK_DETAIL')
        return original(*args, **kwargs)
    case['journal'].update = update
    first = run(case)
    assert (first['status'], first['stage']) == ('pending', 'running')
    with case['board']._tx() as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',
                   (time.time() + 5, case['scope']['ticket_id']))
    directory = case['journal'].directory
    case['journal'].close()
    case['journal'] = WorkJournal(directory)
    result = run(case)
    assert result['status'] == 'complete' and len(case['calls']) == 1
    reservations = [args for name, args in case['client'].arguments if name == 'reserve_execution']
    assert all(args == reservations[0] for args in reservations)
    assert case['client'].calls.count('start_execution') == 1


def test_lost_runtime_finish_never_reruns_and_requires_explicit_reconciliation(review_case):
    case = review_case
    case['client'].lost.add('finish_execution')
    assert run(case)['status'] == 'needs_review'
    assert run(case)['cached'] is True and len(case['calls']) == 1
    assert case['runtime'].list_status()[0]['phase'] == 'needs_review'
    assert ticket(case)['status'] == 'review'


def test_cached_opinion_requires_same_fresh_context_and_delivery_guard(review_case):
    case = review_case
    result = run(case)
    case['guard_result'] = False
    assert run(case)['reason'] == 'delivery_proof_unavailable'
    case['guard_result'] = True
    task = ticket(case)
    case['board'].update_ticket(case['human'], task['id'], task['version'], evidence='New delivery')
    assert run(case)['reason'] == 'review_context_changed'
    assert case['journal'].get(result['operation_id'])['stage'] == 'complete'
    assert len(case['calls']) == 1


def test_parent_operation_id_is_exact_and_cannot_adopt_another_review(review_case):
    case=review_case;operation_id=str(uuid.uuid4())
    result=run(case,operation_id=operation_id)
    assert result['status']=='complete' and result['operation_id']==operation_id
    assert run(case,operation_id=str(uuid.uuid4()))['reason']=='local_operation_conflict'
    assert len(case['calls'])==1


def test_stop_during_final_runtime_reauthorization_never_completes_opinion(review_case):
    case=review_case;state={'reservations':0,'stop':False}
    def before(name,_arguments):
        if name=='reserve_execution':
            state['reservations']+=1
            if state['reservations']==4:state['stop']=True
    case['client'].before=before
    result=run(case,should_cancel=lambda:state['stop'])
    assert state['reservations']==4
    assert result['status']=='needs_review' and result['reason']=='cancel_requested'
    assert case['journal'].get(result['operation_id'])['stage']=='needs_review'
    assert len(case['calls'])==1


@pytest.mark.parametrize('opinion', [
    {'verdict': 'done', 'text': 'Done'}, {'verdict': 'accept', 'text': 'Fine', 'merge': True},
    {'verdict': 'limited', 'text': ''},
])
def test_model_has_no_completion_or_publication_vocabulary(review_case, opinion):
    review_case['opinion'] = opinion
    result = run(review_case)
    assert result['reason'] == 'invalid_review_opinion'
    assert ticket(review_case)['status'] == 'review'


def test_in_progress_is_never_implicitly_accepted_as_review(review_case):
    case = review_case
    task = ticket(case)
    case['board'].update_ticket(case['human'], task['id'], task['version'], status='in_progress')
    assert run(case)['reason'] == 'ticket_not_reviewable'
    assert not case['calls'] and 'reserve_execution' not in case['client'].calls


def test_runtime_capacity_pause_never_invokes_model_or_changes_ticket(review_case):
    case = review_case
    before = ticket(case)
    limits = case['board'].get_runtime(case['human'])['limits']
    case['board'].update_runtime_limits(case['human'], limits['version'], 1, True)
    assert run(case)['status'] == 'needs_review'
    assert not case['calls'] and ticket(case) == before
    assert case['board'].get_runtime(case['human'])['executions'] == []


@pytest.mark.parametrize('field,value', [
    ('delivery_sha', 'A' * 40), ('context_sha256', 'short'),
    ('author_agent_id', 'bad\nprofile'), ('extra_authority', True),
])
def test_selection_is_closed_data_without_autoauthority(review_case, field, value):
    case = review_case
    result = run(case, selection={**case['args']['selection'], field: value})
    assert result['reason'] == 'invalid_review_selection'
    assert not case['calls'] and 'reserve_execution' not in case['client'].calls


def test_invalid_scope_and_missing_runtime_are_safe_local_refusals(review_case):
    case = review_case
    assert run(case, scope={})['status'] == 'needs_review'
    worker = case['worker']()
    worker.runtime_client = None
    assert worker.run(**case['args'])['reason'] == 'review_runtime_required'
    assert not case['calls']


def test_proof_changed_after_reservation_blocks_first_prompt_without_inference(review_case):
    case = review_case
    def before(name, _arguments):
        if name == 'start_execution':
            case['guard_result'] = False
    case['client'].before = before
    assert run(case)['status'] == 'needs_review'
    assert not case['calls']
    assert 'start_execution' in case['client'].calls
    assert case['board'].get_runtime(case['human'])['active_count'] == 1


def test_live_lease_during_and_after_turn_needs_no_second_full_turn_budget(review_case):
    case = review_case
    def during(callbacks):
        with case['board']._tx() as db:
            db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',
                       (time.time() + 5, case['scope']['ticket_id']))
        assert callbacks['should_cancel']() is False
    case['during'] = during
    assert run(case)['status'] == 'complete'
    assert 'renew_ticket' not in case['client'].calls


def revoke_restore(case):
    board, human = case['board'], case['human']
    board.set_policy(human, True, True, False, True, [], [])
    board.set_policy(human, True, True, True, True, [], [])


def test_rights_roundtrip_between_polls_preserves_cleanup_and_refuses_opinion(review_case):
    case = review_case
    case['during'] = lambda _callbacks: revoke_restore(case)
    result = run(case)
    assert result['status'] == 'needs_review'
    assert 'opinion' not in case['journal'].get(result['operation_id'])['payload']
    runtime = case['board'].get_runtime(case['human'])
    assert runtime['active_count'] == 0 and len(runtime['executions']) == 1
    assert runtime['executions'][0]['cleanup_confirmed'] is True
    assert case['runtime'].list_status()[0]['phase'] == 'finished'
    assert len(case['calls']) == 1 and ticket(case)['status'] == 'review'


def test_cached_opinion_cannot_be_reused_after_hidden_rights_roundtrip(review_case):
    case = review_case
    first = run(case)
    assert first['status'] == 'complete'
    revoke_restore(case)
    assert run(case)['status'] == 'needs_review'
    assert len(case['calls']) == 1


def test_rights_roundtrip_after_cleanup_and_lost_result_write_cannot_be_adopted(review_case):
    case = review_case
    original, once = case['journal'].update, [True]
    def update(*args, **kwargs):
        if kwargs.get('stage') == 'result_ready' and once:
            once.pop()
            revoke_restore(case)
            raise OSError('PRIVATE_DISK_DETAIL')
        return original(*args, **kwargs)
    case['journal'].update = update
    assert run(case)['status'] == 'pending'
    directory = case['journal'].directory
    case['journal'].close()
    case['journal'] = WorkJournal(directory)
    result = run(case)
    assert result['status'] == 'needs_review'
    assert 'opinion' not in case['journal'].get(result['operation_id'])['payload']
    assert len(case['calls']) == 1 and case['client'].calls.count('start_execution') == 1
