"""Independent host adversarial checks: local fixtures, no network/model/service."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

from blackoutburst_agents.board_worker import BoardWorker
from blackoutburst_agents.event_drain import EventDrainer
from blackoutburst_agents.event_journal import EventJournal
from blackoutburst_agents.host_config import HostError, scope_for, validate, worker_configuration
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.host_supervisor import HostSupervisor, PinnedClient
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.work_journal import WorkJournal
from test_event_drain import Client, event


def test_canonical_lock_root_uses_os_identity_not_shell_home_resolution(monkeypatch):
    from blackoutburst_agents import host_journal
    monkeypatch.setattr(host_journal.pwd, 'getpwuid', lambda _uid: SimpleNamespace(pw_dir='/fixture-os-home'))
    def forbidden():
        raise AssertionError('environment-dependent home lookup must not select the lock root')
    monkeypatch.setattr(Path, 'home', forbidden)
    assert host_journal.canonical_directory() == Path('/fixture-os-home/.local/state/blackoutburst-agents/hosts')


class FakeClient(Client):
    def __init__(self):
        super().__init__()
        self.registration['profile'] = {'id': 'agent', 'owner_id': 'owner', 'version': 1,
                                       'status': 'active', 'instructions': 'PRIVATE_PROFILE'}
        self.registration['profile_version'] = 1
        self.registration['session']['profile_version'] = 1
        self.lost_publication = False
        self.publications = 0

    def call(self, name, args):
        result = super().call(name, args)
        if name == 'update_ticket':
            self.publications += 1
            if self.lost_publication:
                self.lost_publication = False
                raise RuntimeError('synthetic lost acknowledgement')
        return result


class Harness:
    def __init__(self, root, monkeypatch):
        self.root, self.client, self.inferences = root, FakeClient(), []
        self.connection = root / 'connection.json'
        self.connection.write_text(json.dumps({'mcp_url': self.client.endpoint, 'token': 'fixture-only',
            'client': 'codex', 'identity_key': 'fixture', 'name': 'Fixture'}))
        self.connection.chmod(0o600)
        self.workspace = root / 'workspace'
        self.workspace.mkdir()
        self.config = validate({'schema_version': 1, 'owner_id': 'owner', 'board_endpoint': self.client.endpoint,
            'interval_seconds': 5, 'max_pages': 2, 'page_limit': 50, 'dispatch_budget': 2,
            'targets': [{'id': 'fixed-target', 'agent_id': 'agent', 'project_id': 'project', 'ticket_id': 'ticket',
                         'board_session_id': 'session', 'profile_version': 1, 'connection_file': str(self.connection),
                         'instruction': 'PRIVATE_FIXED_INSTRUCTION', 'workspace': str(self.workspace),
                         'executable': str(Path(sys.executable).resolve()), 'model': 'fixture-model', 'timeout': 10,
                         'after': 0}]}, bind_connections=True)
        monkeypatch.setattr('blackoutburst_agents.host_journal.canonical_directory', lambda: root / 'canonical')
        self.journal = HostJournal(root / 'host')
        self.journal.configure(self.config)
        self.before_inference = None
        self.after_preview = None
        self.supervisor = HostSupervisor(self.journal, resource_factory=self.resources)

    @contextmanager
    def resources(self, host, target, scope, cancelled):
        with EventJournal(host.directory / 'events') as events, WorkJournal(host.directory / 'work') as work:
            runs = RunStore(host.directory / 'runs')
            try:
                client = PinnedClient(self.client, host, target, scope, cancelled)
                worker = BoardWorker(work, runs, client,
                    runner_factory=lambda store, **_kwargs: PersonalRunner(store, self.transport),
                    runtime_guard_factory=lambda _scope: SimpleNamespace())
                drainer = EventDrainer(events, worker, client)
                original = drainer.drain
                def drain(**kwargs):
                    result = original(**kwargs)
                    if not kwargs['execute'] and self.after_preview:
                        self.after_preview(drainer, result)
                    return result
                drainer.drain = drain
                yield drainer
            finally:
                runs.close()

    def transport(self, settings, prompt, workspace, **callbacks):
        # Record the durable parent/child intent before this fake model effect.
        dispatches = self.journal.dispatches()
        assert len([d for d in dispatches if d['state'] != 'complete']) == 1
        assert dispatches[-1]['worker_id']
        self.inferences.append((deepcopy(settings), prompt))
        if self.before_inference:
            self.before_inference()
        callbacks['on_started'](999999999)
        sid = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](sid)
        return {'status': 'completed', 'reason': 'verified', 'session_id': sid,
                'text': 'PRIVATE_RESULT', 'verification': {'cleanup_confirmed': True}}

    def enable(self):
        with self.journal.owner_lock():
            return self.journal.enable(self.journal.control()['revision'])

    def tick(self, **kwargs):
        with self.journal.owner_lock():
            return self.supervisor.tick(**kwargs)

    def reopen(self):
        self.journal.close()
        self.journal = HostJournal(self.root / 'host')
        self.supervisor = HostSupervisor(self.journal, resource_factory=self.resources)

    def close(self):
        self.journal.close()


@pytest.fixture
def host(tmp_path, monkeypatch):
    result = Harness(tmp_path, monkeypatch)
    try:
        yield result
    finally:
        result.close()


def test_disabled_and_empty_stream_never_admit_work(host):
    assert host.tick()['status'] == 'disabled'
    assert host.client.registrations == 0
    host.enable()
    assert host.tick()['status'] == 'idle'
    assert host.tick()['status'] == 'idle'
    assert host.inferences == []
    assert host.journal.dispatches() == []
    assert host.journal.status()['remaining_dispatches'] == 2
    assert not any(name == 'claim_ticket' for name, _ in host.client.calls)


def test_one_dispatch_per_tick_has_durable_parent_and_child_intent(host):
    host.client.events = [event(1), event(2)]
    host.enable()
    result = host.tick()
    assert result['status'] == 'complete'
    assert len(host.inferences) == 1
    assert len(host.journal.dispatches()) == 1
    assert host.journal.status()['remaining_dispatches'] == 1
    assert 'PRIVATE_' not in json.dumps(result)
    assert host.tick()['status'] == 'idle'
    assert len(host.inferences) == 1


def test_fixed_budget_is_not_replenished_by_restart_or_idle_tick(host):
    host.enable()
    for identifier in (1, 2):
        host.client.events.append(event(identifier))
        host.client.ticket['status'] = 'todo'
        assert host.tick()['status'] == 'complete'
        host.reopen()
    host.client.events.append(event(3))
    host.client.ticket['status'] = 'todo'
    assert host.tick()['reason'] == 'budget_exhausted'
    assert len(host.inferences) == 2
    assert host.journal.status()['status'] == 'disabled'


def test_lost_publication_is_reconciled_without_another_inference(host):
    host.client.events = [event(1)]
    host.client.lost_publication = True
    host.enable()
    assert host.tick()['status'] == 'pending'
    assert len(host.inferences) == 1
    original = deepcopy(host.journal.dispatches()[0])
    host.reopen()
    assert host.tick()['status'] == 'complete'
    assert len(host.inferences) == 1 and host.client.publications == 1
    recovered = host.journal.dispatches()[0]
    for key in ('batch_id', 'request_id', 'worker_id', 'activation'):
        assert recovered[key] == original[key]
    assert host.journal.status()['remaining_dispatches'] == 1


def test_child_completion_survives_parent_receipt_failure(host, monkeypatch):
    host.client.events = [event(1)]
    host.enable()
    def crash(*args, **kwargs):
        raise RuntimeError('simulated parent receipt failure')
    monkeypatch.setattr(host.journal, 'finish_dispatch', crash)
    assert host.tick()['status'] == 'needs_review'
    assert len(host.inferences) == 1
    host.reopen()
    with pytest.raises(HostError):
        host.enable()
    batch_id = host.journal.dispatches()[0]['batch_id']
    with host.journal.owner_lock():
        assert host.supervisor.reconcile(batch_id)['reason'] == 'human_reconciled'
    host.enable()  # Explicit local authorization after offline reconciliation.
    assert host.tick()['status'] == 'idle'
    assert len(host.inferences) == 1 and host.client.publications == 1


def test_actual_interruption_after_child_publication_reconciles_without_a_third_effect(host, monkeypatch):
    host.client.events = [event(1)]
    host.enable()
    def crash(*args, **kwargs):
        raise SystemExit('simulated process interruption')
    monkeypatch.setattr(host.journal, 'finish_dispatch', crash)
    with pytest.raises(SystemExit):
        host.tick()
    assert len(host.inferences) == 1
    host.reopen()
    assert host.tick()['reason'] == 'child_reconciled'
    assert len(host.inferences) == 1 and host.client.publications == 1


def test_preallocated_empty_child_crash_recovers_only_after_parent_intent(host, monkeypatch):
    host.client.events = [event(1)]
    host.enable()
    def crash(*args, **kwargs):
        raise SystemExit('simulated interruption between preallocation and link')
    monkeypatch.setattr(host.journal, 'link_worker', crash)
    with pytest.raises(SystemExit):
        host.tick()
    dispatch = host.journal.dispatches()[0]
    assert str(uuid.UUID(dispatch['worker_id'])) == dispatch['worker_id']
    with WorkJournal(host.journal.directory/'work') as work:
        assert work.get(dispatch['worker_id'])['stage'] == 'new'
    assert host.inferences == []
    host.reopen()
    assert host.tick()['status'] == 'complete'
    assert host.journal.dispatches()[0]['worker_id'] == dispatch['worker_id']
    assert len(host.inferences) == 1


@pytest.mark.parametrize('with_payload', [False, True])
def test_host_refuses_an_independently_preexisting_child(host, with_payload):
    host.client.events = [event(1)]
    target = host.config['targets'][0]
    scope = scope_for(host.config, target)
    def preexist(drainer, result):
        host.after_preview = None
        batch = drainer.journal.get_batch(result['batch_id'])
        with drainer.worker.journal.lock(scope):
            operation = drainer.worker.journal.begin(scope, batch['request_id'], worker_configuration(target))
            if with_payload:
                drainer.worker.journal.update(operation['id'], operation['revision'], stage='claiming', payload={'independent': True})
    host.after_preview = preexist
    host.enable()
    assert host.tick()['status'] == 'needs_review'
    assert host.inferences == []
    assert not any(name == 'claim_ticket' for name, _ in host.client.calls)


def test_reenable_cannot_adopt_a_preexisting_child_already_refused(host):
    host.client.events = [event(1)]
    target = host.config['targets'][0]
    scope = scope_for(host.config, target)
    def preexist(drainer, result):
        host.after_preview = None
        batch = drainer.journal.get_batch(result['batch_id'])
        with drainer.worker.journal.lock(scope):
            drainer.worker.journal.begin(scope, batch['request_id'], worker_configuration(target))
    host.after_preview = preexist
    host.enable()
    assert host.tick()['status'] == 'needs_review'
    host.reopen()
    try:
        host.enable()
    except HostError as error:
        assert error.code == 'host_needs_review'
    else:
        assert host.tick()['status'] in ('disabled', 'needs_review', 'blocked')
    assert host.inferences == []


def test_stop_cannot_erase_review_fence_when_dispatch_receipt_was_unavailable(host, monkeypatch):
    host.client.events = [event(1)]
    target = host.config['targets'][0]
    scope = scope_for(host.config, target)
    def preexist(drainer, result):
        host.after_preview = None
        batch = drainer.journal.get_batch(result['batch_id'])
        with drainer.worker.journal.lock(scope):
            drainer.worker.journal.begin(scope, batch['request_id'], worker_configuration(target))
    def unavailable(*args, **kwargs):
        raise RuntimeError('synthetic dispatch receipt unavailable')
    host.after_preview = preexist
    monkeypatch.setattr(host.journal, 'finish_dispatch', unavailable)
    host.enable()
    assert host.tick()['status'] == 'needs_review'
    host.reopen()
    host.journal.stop()
    with pytest.raises(HostError) as error:
        host.enable()
    assert error.value.code == 'host_needs_review'
    assert host.inferences == []


@pytest.mark.parametrize('change', ['version', 'instructions', 'policy', 'device', 'connection'])
def test_pinned_profile_policy_device_and_connection_changes_disable_before_effect(host, change):
    host.enable()
    assert host.tick()['status'] == 'idle'  # Persist the original profile pin.
    if change == 'version':
        host.client.registration['profile']['version'] = 2
        host.client.registration['profile_version'] = 2
        host.client.registration['session']['profile_version'] = 2
    elif change == 'instructions':
        host.client.registration['profile']['instructions'] = 'Changed without version fixture'
    elif change == 'policy':
        host.client.registration['policy']['allow_requests'] = False
    elif change == 'device':
        host.client.registration['identity']['device_id'] = 'other-device'
        host.client.registration['session']['device_id'] = 'other-device'
    else:
        host.connection.write_text(host.connection.read_text().replace('fixture-only', 'changed-fixture-only'))
    host.client.events = [event(1)]
    assert host.tick()['status'] == 'blocked'
    assert host.journal.status()['status'] == 'disabled'
    assert host.inferences == []
    assert not any(name == 'claim_ticket' for name, _ in host.client.calls)


def test_stop_is_durable_and_cancel_before_admission_leaves_no_child(host):
    host.client.events = [event(1)]
    host.enable()
    assert host.tick(should_cancel=lambda: True)['status'] == 'disabled'
    host.reopen()
    assert host.tick()['status'] == 'disabled'
    assert host.inferences == [] and host.journal.dispatches() == []


def test_same_owner_has_one_canonical_lock_and_cannot_use_new_journal_after_stop(host):
    other = HostJournal(host.root / 'other')
    try:
        other.configure(host.config)
        with host.journal.owner_lock():
            with pytest.raises(HostError) as error, other.owner_lock():
                pass
            assert error.value.code == 'host_busy'
        host.journal.stop()
        with pytest.raises(HostError) as error, other.owner_lock():
            pass
        assert error.value.code == 'host_directory_conflict'
    finally:
        other.close()


def test_recreated_journal_at_same_path_cannot_forget_an_uncertain_dispatch(host):
    host.client.events = [event(1)]
    host.client.lost_publication = True
    host.enable()
    assert host.tick()['status'] == 'pending'
    host.journal.close()
    old_path = host.root / 'host'
    old_path.rename(host.root / 'preserved-host-state')
    host.journal = HostJournal(old_path)
    host.journal.configure(host.config)
    with pytest.raises(HostError), host.journal.owner_lock():
        pass


@pytest.mark.parametrize('alias', ['https://BOARD.test/mcp', 'https://board.test:443/mcp', 'https://board.test./mcp'])
def test_equivalent_endpoints_do_not_bypass_canonical_owner_lock(host, alias):
    other = HostJournal(host.root / 'other')
    config = deepcopy(host.config)
    config['board_endpoint'] = alias
    other.configure(config)
    try:
        with host.journal.owner_lock():
            with pytest.raises(HostError), other.owner_lock():
                pass
        with pytest.raises(HostError), other.owner_lock():
            pass
    finally:
        other.close()


def test_model_or_instruction_configuration_cannot_change_after_pinning(host):
    for field, value in [('model', 'other-model'), ('instruction', 'Other instruction')]:
        config = deepcopy(host.config)
        config['targets'][0][field] = value
        with pytest.raises(HostError):
            host.journal.configure(config)
    assert host.inferences == []


def test_foreign_empty_child_is_never_adopted_after_both_refusal_writes_fail(host, monkeypatch):
    host.client.events = [event(1)]
    target = host.config['targets'][0]
    scope = scope_for(host.config, target)
    independent_id = []
    def preexist(drainer, result):
        host.after_preview = None
        batch = drainer.journal.get_batch(result['batch_id'])
        with drainer.worker.journal.lock(scope):
            operation = drainer.worker.journal.begin(scope,batch['request_id'],worker_configuration(target))
            independent_id.append(operation['id'])
    def unavailable(*args, **kwargs):
        raise OSError('synthetic storage full')
    host.after_preview = preexist
    host.enable()
    monkeypatch.setattr(host.journal,'finish_dispatch',unavailable)
    monkeypatch.setattr(host.journal,'stop',unavailable)
    with pytest.raises(OSError):
        host.tick()
    intent = host.journal.dispatches()[0]
    assert intent['worker_id'] != independent_id[0]
    assert host.journal.status()['status'] == 'enabled'
    host.reopen()  # Writes work again; no refusal fence was persisted.
    assert host.tick()['status'] == 'needs_review'
    assert host.inferences == []
    assert not any(name=='claim_ticket' for name,_ in host.client.calls)
    with WorkJournal(host.journal.directory/'work') as work:
        independent = work.get(independent_id[0])
        assert independent['stage']=='new' and independent['payload']=={}
        assert work.find(scope,intent['request_id'])['id']==independent_id[0]
