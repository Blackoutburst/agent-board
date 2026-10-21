"""Real local Git export; policy qualification and transport are synthetic."""
from pathlib import Path
import pickle
import threading

import pytest

from test_git_workspace import setup  # noqa: F401
from test_git_delivery import delivery, seal, review  # noqa: F401
from blackoutburst_agents import linux_review_binding as module
from blackoutburst_agents.dev_manifest import DevelopmentError


@pytest.fixture
def bound(delivery, tmp_path, monkeypatch):
    f = delivery
    seal(f); review(f)
    planner = tmp_path / 'empty-planner'; planner.mkdir(mode=0o700)
    state = {'valid': True, 'checks': [], 'closed': 0}
    def initialize(policy, directory):
        policy._fingerprint = 'b' * 64
        policy._plan = {'workspace': str(planner), 'checkout': f['review']['workspace'],
                        'executable': '/explicit/protected/codex'}
        def validate(executable, workspace):
            state['checks'].append((executable, workspace))
            if not state['valid']:
                raise DevelopmentError('synthetic_policy_unqualified')
        policy.validate_for = validate
        policy.close = lambda: state.update(closed=state['closed'] + 1)
    monkeypatch.setattr(module.LinuxPlannerPolicy, '__init__', initialize)
    binding = module.LinuxDeliveryReviewBinding(f['delivery'], operation_id=f['op'],
        review_operation_id=f['review_op'], planner_policy_directory=tmp_path/'synthetic-policy')
    f.update(binding=binding, policy_state=state, planner=planner)
    try:
        yield f
    finally:
        binding.close()


def guarded(f, **changes):
    arguments = dict(scope=f['review_scope'], selection=f['review']['request']['selection'],
        context=f['context'], material=f['material'], workspace=str(f['planner']))
    arguments.update(changes)
    return f['binding'].guard(**arguments)


def test_exact_export_is_verified_while_model_workspace_stays_empty(bound):
    f = bound
    assert guarded(f) is True
    assert f['binding'].workspace == str(f['planner'])
    assert f['binding'].executable == '/explicit/protected/codex'
    assert list(f['planner'].iterdir()) == []
    assert (Path(f['review']['workspace'])/'example.py').read_text() == 'value = 2\n'


def test_using_nonempty_export_as_model_workspace_is_refused(bound):
    with pytest.raises(DevelopmentError):
        guarded(bound, workspace=bound['review']['workspace'])


@pytest.mark.parametrize('field', ['fingerprint', 'export', 'qualification'])
def test_changed_policy_is_never_adopted(bound, field):
    policy = bound['binding'].policy
    if field == 'fingerprint': policy._fingerprint = 'c' * 64
    elif field == 'export': policy._plan['checkout'] = str(bound['planner'])
    else: bound['policy_state']['valid'] = False
    with pytest.raises(DevelopmentError): guarded(bound)


def test_policy_changed_during_delivery_check_refuses(bound, monkeypatch):
    original = bound['delivery'].review_guard
    def changed(**arguments):
        result = original(**arguments)
        bound['policy_state']['valid'] = False
        return result
    monkeypatch.setattr(bound['delivery'], 'review_guard', changed)
    with pytest.raises(DevelopmentError): guarded(bound)


def test_modified_readonly_export_cannot_pass_guard(bound):
    path = Path(bound['review']['workspace'])/'example.py'
    path.chmod(0o600); path.write_text('value = 999\n'); path.chmod(0o400)
    with pytest.raises(DevelopmentError): guarded(bound)


def test_only_concrete_review_runtime_can_create_the_linux_runner(bound):
    with pytest.raises(DevelopmentError):
        bound['binding'].runner_factory(None, runtime_guard=object())
    guard = object.__new__(module.ReviewRuntimeGuard)
    runner = bound['binding'].runner_factory(None, runtime_guard=guard)
    assert type(runner) is module.PersonalRunner
    assert runner.runtime_guard is guard
    assert runner.transport.func is module.execute_turn
    assert runner.transport.keywords == {'planner_policy': bound['binding'].policy}


def test_binding_is_thread_owned_and_not_serializable(bound):
    errors = []
    def other_thread():
        try: guarded(bound)
        except DevelopmentError: errors.append('refused')
    thread = threading.Thread(target=other_thread); thread.start(); thread.join(timeout=1)
    assert not thread.is_alive() and errors == ['refused']
    with pytest.raises(TypeError): pickle.dumps(bound['binding'])


def test_failed_context_entry_closes_policy_and_cannot_be_reused(bound):
    bound['policy_state']['valid'] = False
    with pytest.raises(DevelopmentError):
        with bound['binding']:
            pytest.fail('unqualified binding entered')
    assert bound['policy_state']['closed'] == 1
    with pytest.raises(DevelopmentError): guarded(bound)


def test_arbitrary_controller_refuses_before_opening_a_policy(monkeypatch):
    monkeypatch.setattr(module.LinuxPlannerPolicy, '__init__',
                        lambda *_: pytest.fail('opened a policy for invalid controller'))
    with pytest.raises(DevelopmentError):
        module.LinuxDeliveryReviewBinding(object(), operation_id='not-an-id',
            review_operation_id='not-an-id', planner_policy_directory='/unused')
