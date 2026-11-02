"""Owned catalog admission is inert; real execution remains the finite driver."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import json
import uuid

import pytest

from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import Budgets, DevelopmentError, canonical, digest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.development_pool_selection import file_document, queue_admission
from blackoutburst_agents.development_queue import DevelopmentQueueAdmission
from blackoutburst_agents.finite_pool_batch import load_batch, QUEUE_SCHEMA
from blackoutburst_agents import cli, development_queue_cli
from blackoutburst_agents.development_client import failure_notice
from blackoutburst_agents.linux_pool_preparation import LinuxPoolJobPreparer


class Discovery:
    endpoint = 'http://localhost/mcp'
    session_id = 'discovery'

    def __init__(self, root):
        self.connection_path = str(root / 'connections' / 'coder.json')
        _, self.connection_pin = file_document(self.connection_path)
        self.calls = []; self.ready = True; self.queue = True; self.companion = False
        self.project = dict(id='project', version=2, name='Project', description='', context='',
                            repository_url='', environment='', participants=['owner'], archived=False,
                            updated_at=1, ticket_counts={})
        self.ticket = dict(id='ticket', project_id='project', version=3, status='todo', claimant=None,
                           resource='repo:work', delegation=None, claim_expired=False, blocked_by=[],
                           waiting_on_human=False, assignee_owner_id='owner', assignee_agent_id='coder')

    def registration(self, *, autonomous=False):
        self.calls.append(('register', autonomous))
        policy = dict(enabled=True, allow_read=True, allow_requests=True,
                      autonomy='queue' if self.queue else 'requested',
                      actions=['repo.read_file', 'repo.run_test'], resources=['repo:work'])
        return dict(identity=dict(id='coder', owner_id='owner', device_id='device', profile_version=5), profile_version=5,
                    profile=dict(id='coder', owner_id='owner', version=5, status='active', policy=policy,
                                 companion_for='companion-id' if self.companion else None),
                    session=dict(id=self.session_id, agent_id='coder', owner_id='owner', device_id='device',
                                 profile_version=5, autonomous=autonomous and self.queue), policy=policy)

    def call(self, name, args):
        self.calls.append((name, deepcopy(args)))
        assert name in ('get_work', 'get_ticket', 'get_project'), 'No effects are permitted'
        if name == 'get_project': return deepcopy(self.project)
        if name == 'get_ticket': return deepcopy(dict(ticket=self.ticket, project=self.project))
        return dict(ready=[deepcopy(self.ticket)] if self.ready else [])


@pytest.fixture
def case(tmp_path):
    root = tmp_path.resolve() / 'pool'; root.mkdir(mode=0o700)
    for name in ('config', 'connections', 'repositories', 'checkouts', 'verifiers'):
        (root / name).mkdir(mode=0o700)
    def write(path, raw):
        with PrivateDirectory(path.parent) as private: private.create(path.name, raw)
    write(root / 'connections' / 'coder.json', canonical({'synthetic': 'no credentials'}))
    verifier = root / 'verifiers' / 'fixed.py'; write(verifier, b'assert True\n')
    catalog = dict(schema='development-queue-catalog-v1', owner_id='owner', agent_id='coder', profile_version=5,
                   board_endpoint=Discovery.endpoint, project_id='project', connection_file=str(root / 'connections' / 'coder.json'),
                   timeout_seconds=300, entries=[dict(entry_id='code', resource='repo:work',
        repository=dict(repository_id='approved', repository=str(root / 'repositories' / 'approved'), base_commit='a' * 40),
        paths=['main.py'], actions=['repo.read_file', 'repo.run_test'], verifier_path=str(verifier), verifier_sha256=digest(verifier.read_bytes()),
        test_name='fixed', test_timeout_seconds=2, test_max_output_bytes=100, instruction='Inspect the authorized file and run the fixed test.',
        model='explicit-model', planner_budgets=PlannerBudgets().document(), broker_budgets=Budgets().document())])
    path = root / 'config' / 'catalog.json'; write(path, canonical(catalog))
    client = Discovery(root); controller = DevelopmentQueueAdmission(root, client)
    return root, path, catalog, client, controller


def replace(path, value):
    # External JSON can encode invalid Unicode escapes; the reader must reject
    # them rather than relying on the controller's canonical writer here.
    raw = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('ascii')
    with PrivateDirectory(path.parent) as private: private.replace(path.name, raw)


def test_preview_is_inert_and_ignores_counts_presence(case):
    root, path, _, client, controller = case
    first = controller.preview(path)
    client.project.update(updated_at=999, ticket_counts={'todo': 10})
    assert first == controller.preview(path)
    assert first['selected']['ticket_id'] == 'ticket' and first['execution_authorized'] is True
    assert not (root / 'admissions').exists()
    assert all(name != 'register' or value is False for name, value in client.calls)


def test_admit_writes_one_v3_batch_with_pinned_versions_and_no_dispatch(case):
    root, path, _, client, controller = case
    preview = controller.preview(path); request = str(uuid.uuid4())
    result = controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    batch, _ = load_batch(result['batch_file'], root=root, owner_id='owner', board_endpoint=client.endpoint, max_seconds=300)
    assert batch['schema'] == QUEUE_SCHEMA and batch['batch_id'] == request and len(batch['jobs']) == 1
    job = batch['jobs'][0]
    assert job['queue_admission'] == dict(schema='development-queue-admission-v1', ticket_version=3,
                                         profile_version=5, catalog_sha256=digest(path.read_bytes()))
    assert job['repository']['workspace_root'] == str(root / 'checkouts' / request / 'code')
    assert result['model_calls'] == 0 and result['launchable'] is False
    assert ('register', True) in client.calls


def test_replay_after_queue_changes_returns_original_batch_without_discovery(case):
    root, path, _, client, controller = case
    preview = controller.preview(path); request = str(uuid.uuid4())
    first = controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    original_bytes = Path(first['batch_file']).read_bytes()
    client.ticket.update(id='another-ticket', version=8, status='todo')
    client.ready = False; client.calls.clear()
    repeated = controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    assert repeated == {**first, 'historical': True}
    assert not any(name in ('get_work', 'get_ticket') for name, _ in client.calls)
    assert Path(first['batch_file']).read_bytes() == original_bytes


def test_empty_admission_is_stable_when_new_work_arrives(case):
    root, path, _, client, controller = case
    client.ready = False; preview = controller.preview(path); request = str(uuid.uuid4())
    result = controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    assert result['status'] == 'empty' and result['batch_file'] is None
    client.ready = True; client.calls.clear()
    assert controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])['historical'] is True
    assert not any(name == 'get_work' for name, _ in client.calls)
    assert not list((root / 'config').glob('queue-admission-*'))


def test_requested_profile_preview_never_becomes_an_implicit_queue_grant(case):
    root, path, _, client, controller = case
    client.queue = False; preview = controller.preview(path)
    assert preview['execution_authorized'] is False
    with pytest.raises(DevelopmentError, match='development_queue_not_authorized'):
        controller.admit(path, request_id=str(uuid.uuid4()), approved_sha256=preview['approval_sha256'])
    assert ('register', True) not in client.calls


def test_companion_metadata_refuses_before_admission(case):
    root, path, _, client, controller = case
    client.companion = True
    with pytest.raises(DevelopmentError, match='development_queue_not_authorized'): controller.preview(path)
    assert not (root / 'admissions').exists()


def test_changed_catalog_or_request_collision_cannot_adopt_history(case):
    _, path, catalog, _, controller = case
    preview = controller.preview(path); request = str(uuid.uuid4())
    controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    with pytest.raises(DevelopmentError, match='development_queue_intent_conflict'):
        controller.admit(path, request_id=request, approved_sha256='0' * 64)
    catalog['entries'][0]['instruction'] = 'A changed operator request.'; replace(path, catalog)
    with pytest.raises(DevelopmentError, match='development_queue_intent_conflict'):
        controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])


def test_interrupted_admission_is_not_reselected_or_rewritten(case):
    root, path, _, _, controller = case
    preview = controller.preview(path); request = str(uuid.uuid4())
    controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    completion = root / 'admissions' / request / 'complete.json'; completion.unlink()
    with pytest.raises(DevelopmentError, match='development_queue_admission_pending'):
        controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    assert len(list((root / 'config').glob('queue-admission-*'))) == 1


def test_receipt_cannot_adopt_another_batch_even_with_its_updated_hash(case):
    root, path, _, _, controller = case
    preview = controller.preview(path); request = str(uuid.uuid4())
    result = controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])
    batch_path = Path(result['batch_file']); batch = decode(batch_path.read_bytes())
    batch['batch_id'] = str(uuid.uuid4()); replace(batch_path, batch)
    completion = root / 'admissions' / request / 'complete.json'
    receipt = decode(completion.read_bytes()); receipt['batch_pin'] = file_document(str(batch_path))[1]
    replace(completion, receipt)
    with pytest.raises(DevelopmentError, match='development_queue_receipt_changed'):
        controller.admit(path, request_id=request, approved_sha256=preview['approval_sha256'])


@pytest.mark.parametrize('mutate', [
    lambda c: c.update(unknown=True),
    lambda c: c.update(profile_version=True),
    lambda c: c['entries'][0].update(unknown=True),
    lambda c: c['entries'][0].update(resource='runtime:all'),
    lambda c: c['entries'][0].update(actions=['repo.read_file']),
    lambda c: c['entries'][0]['repository'].update(workspace_root='/tmp/model-selected'),
    lambda c: c['entries'][0].update(model='invalid model'),
    lambda c: c['entries'][0].update(verifier_sha256='0' * 64),
    lambda c: c['entries'][0]['planner_budgets'].update(unknown=1),
    lambda c: c['entries'][0].update(instruction='\udfff'),
    lambda c: c.update(connection_file=None),
    lambda c: c['entries'][0].update(verifier_path=[]),
    lambda c: c['entries'][0]['repository'].update(repository=None),
])
def test_closed_catalog_and_pinned_local_inputs(case, mutate):
    _, path, catalog, _, controller = case
    mutate(catalog); replace(path, catalog)
    with pytest.raises(DevelopmentError): controller.preview(path)


@pytest.mark.parametrize('key,value', [('profile_version', True), ('ticket_version', 0),
                                      ('catalog_sha256', 'a' * 63), ('unknown', 1)])
def test_queue_admission_metadata_is_closed(key, value):
    admission = dict(schema='development-queue-admission-v1', ticket_version=1, profile_version=2, catalog_sha256='a' * 64)
    admission[key] = value
    with pytest.raises(DevelopmentError): queue_admission(admission)


@pytest.mark.parametrize('change', ['ticket', 'profile', 'autonomy', 'session'])
def test_preparer_admission_bounds_are_checked_before_qualification(change):
    preparer = object.__new__(LinuxPoolJobPreparer); preparer._local = lambda _: None
    admission = dict(schema='development-queue-admission-v1', ticket_version=3, profile_version=5, catalog_sha256='a' * 64)
    observed = dict(ticket=dict(status='todo', claimant=None, delegation=None, claim_expired=False,
                               resource='repo:work', version=3), context=dict(fingerprint='b' * 64), policy=dict(autonomy='queue'))
    authority = SimpleNamespace(manifest=dict(scope=dict(profile_version=5)), registration=dict(session=dict(autonomous=True)),
                                snapshot=lambda: deepcopy(observed))
    request = dict(resource='repo:work', queue_admission=admission)
    assert preparer._fresh(authority, request)['context_sha256'] == 'b' * 64
    if change == 'ticket': observed['ticket']['version'] = 4
    elif change == 'profile': authority.manifest['scope']['profile_version'] = 6
    elif change == 'autonomy': observed['policy']['autonomy'] = 'requested'
    else: authority.registration['session']['autonomous'] = False
    with pytest.raises(DevelopmentError, match='development_queue_admission_changed'): preparer._fresh(authority, request)


def test_queue_failure_notices_keep_closed_actionable_codes_only():
    for code in ('development_queue_unfinished_work', 'development_queue_claim_intent_changed',
                 'development_queue_admission_changed', 'development_queue_not_authorized'):
        assert failure_notice(code)['code'] == code
    assert failure_notice('private arbitrary transport message')['code'] == 'development_run_requires_review'


def test_cli_preview_and_admit_are_explicit_and_inert(case, monkeypatch, capsys):
    root, path, _, client, _ = case
    class Open:
        def __enter__(self): return client
        def __exit__(self, *_): pass
    monkeypatch.setattr(development_queue_cli.DevelopmentQueueDiscoveryClient, 'from_connection_file', lambda *_a, **_k: Open())
    common = ['--root', str(root), '--catalog', str(path), '--session', client.session_id]
    assert cli.main(['development-queue', 'preview', *common]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert cli.main(['development-queue', 'admit', *common, '--request-id', str(uuid.uuid4()),
                     '--approve-sha256', preview['approval_sha256']]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'admitted' and result['model_calls'] == 0 and result['launchable'] is False
