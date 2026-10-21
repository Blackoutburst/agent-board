"""Real local synthetic Git repositories; no network, credentials or model."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import uuid

import pytest

from blackoutburst_agents.dev_manifest import DevelopmentError, prepare_manifest
from blackoutburst_agents.git_workspace import LocalGitWorkspaceController, LocalRepositoryGrant


def git(path, *arguments):
    return subprocess.check_output(['/usr/bin/git', *arguments], cwd=path,
        env={'PATH': '/usr/bin:/bin', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_SYSTEM': '/dev/null',
             'LANG': 'C', 'LC_ALL': 'C'}, stderr=subprocess.DEVNULL).decode().strip()


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'source'; source.mkdir(mode=0o700)
    git(source, 'init', '--quiet', '--initial-branch=main')
    git(source, 'config', 'user.name', 'Synthetic author')
    git(source, 'config', 'user.email', 'synthetic@example.invalid')
    (source / 'example.py').write_text('value = 1\n')
    git(source, 'add', 'example.py')
    git(source, 'commit', '--quiet', '-m', 'Synthetic base')
    base = git(source, 'rev-parse', 'HEAD')
    state, work = tmp_path / 'state', tmp_path / 'work'
    state.mkdir(mode=0o700); work.mkdir(mode=0o700)
    grant = LocalRepositoryGrant('owner-one', 'authorized-repository', str(source), str(work), base)
    scope = {'owner_id': 'owner-one', 'agent_id': 'profile-one', 'profile_version': 1,
             'device_id': 'device-one', 'session_id': 'session-one',
             'project_id': 'project-one', 'ticket_id': 'ticket-one'}
    return {'source': source, 'state': state, 'work': work, 'grant': grant,
            'scope': scope, 'controller': LocalGitWorkspaceController(state, grant=grant)}


def planned(setup, *, scope=None, operation=None):
    operation = operation or str(uuid.uuid4())
    controller = setup['controller']
    value = controller.plan(operation_id=operation, scope=scope or setup['scope'])
    return operation, value


def test_two_operations_have_independent_branches_objects_and_working_files(setup):
    one, first = planned(setup)
    two, second = planned(setup, scope={**setup['scope'], 'ticket_id': 'ticket-two', 'session_id': 'session-two'})
    assert first['status'] == second['status'] == 'planned'
    assert not list(setup['work'].iterdir())
    first = setup['controller'].materialize(one)
    second = setup['controller'].materialize(two)
    a, b = Path(first['checkout']), Path(second['checkout'])
    assert first['branch'] != second['branch']
    assert git(a, 'rev-parse', 'HEAD') == git(b, 'rev-parse', 'HEAD') == setup['grant'].base_commit
    (a / 'example.py').write_text('value = 2\n')
    assert (b / 'example.py').read_text() == (setup['source'] / 'example.py').read_text() == 'value = 1\n'
    hashed = git(a, 'rev-parse', 'HEAD:example.py')
    relative = Path('objects') / hashed[:2] / hashed[2:]
    files = [path / '.git' / relative for path in (a, b, setup['source'])]
    assert len({(p.stat().st_dev, p.stat().st_ino) for p in files}) == 3
    assert all(p.stat().st_nlink == 1 for p in files)
    assert all(not (path / '.git/objects/info/alternates').exists() for path in (a, b))


def test_two_different_operations_can_copy_concurrently(setup, monkeypatch):
    one, _ = planned(setup)
    two, _ = planned(setup, scope={**setup['scope'], 'ticket_id': 'ticket-two', 'session_id': 'session-two'})
    original = setup['controller']._clone
    together = threading.Barrier(2)
    def synchronized_copy(private, value):
        together.wait(timeout=10)
        return original(private, value)
    monkeypatch.setattr(setup['controller'], '_clone', synchronized_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(setup['controller'].materialize, (one, two)))
    assert a['status'] == b['status'] == 'ready'
    assert a['checkout'] != b['checkout']


def test_same_operation_cannot_be_copied_by_another_holder(setup):
    operation, _ = planned(setup)
    other = LocalGitWorkspaceController(setup['state'], grant=setup['grant'])
    with setup['controller']._locked(operation):
        with pytest.raises(DevelopmentError, match='git_workspace_busy'):
            other.materialize(operation)
    assert not list(setup['work'].iterdir())


def test_reopen_exact_plan_and_ready_preserves_edits_without_copy(setup, monkeypatch):
    operation, initial = planned(setup)
    first = setup['controller'].materialize(operation)
    checkout = Path(first['checkout'])
    (checkout / 'example.py').write_text('unpublished worker edit\n')
    reopened = LocalGitWorkspaceController(setup['state'], grant=setup['grant'])
    monkeypatch.setattr(reopened, '_clone', lambda *_: pytest.fail('replayed copy'))
    assert reopened.plan(operation_id=operation, scope=setup['scope']) == first
    assert reopened.materialize(operation) == first
    assert (checkout / 'example.py').read_text() == 'unpublished worker edit\n'
    assert initial['request_sha256'] == first['request_sha256']


@pytest.mark.parametrize('field,value', [('ticket_id', 'another-ticket'), ('session_id', 'another-session'),
    ('agent_id', 'another-profile'), ('profile_version', 2), ('device_id', 'another-device'),
    ('project_id', 'another-project')])
def test_same_operation_rejects_changed_scope_without_effect(setup, field, value):
    operation, before = planned(setup)
    with pytest.raises(DevelopmentError, match='git_workspace_request_conflict'):
        setup['controller'].plan(operation_id=operation, scope={**setup['scope'], field: value})
    assert not list(setup['work'].iterdir())
    assert setup['controller'].status(operation) == before


def test_wrong_owner_refused_before_creating_operation(setup):
    with pytest.raises(DevelopmentError, match='git_workspace_owner_refused'):
        planned(setup, scope={**setup['scope'], 'owner_id': 'another-owner'})
    assert not list(setup['state'].iterdir())


@pytest.mark.parametrize('field', ['base_commit', 'repository_id'])
def test_changed_grant_refuses_existing_operation(setup, field):
    operation, _ = planned(setup)
    alternate = replace(setup['grant'], **{field: 'a' * 40 if field == 'base_commit' else 'another-resource'})
    changed = LocalGitWorkspaceController(setup['state'], grant=alternate)
    with pytest.raises(DevelopmentError, match='git_workspace_record_refused'):
        changed.materialize(operation)
    assert not list(setup['work'].iterdir())


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize('after_copy', [False, True])
def test_crash_after_durable_intent_is_not_adopted_or_replayed(setup, monkeypatch, after_copy):
    operation, _ = planned(setup)
    original = setup['controller']._clone
    def crash(private, value):
        if after_copy:
            original(private, value)
        raise SimulatedCrash()
    monkeypatch.setattr(setup['controller'], '_clone', crash)
    with pytest.raises(SimulatedCrash):
        setup['controller'].materialize(operation)
    reopened = LocalGitWorkspaceController(setup['state'], grant=setup['grant'])
    assert reopened.status(operation)['phase'] == 'preparing'
    monkeypatch.setattr(reopened, '_clone', lambda *_: pytest.fail('replayed ambiguous copy'))
    for _ in range(2):
        with pytest.raises(DevelopmentError, match='git_workspace_interrupted_requires_review'):
            reopened.materialize(operation)
    if after_copy:
        assert (setup['work'] / operation / 'checkout/example.py').read_text() == 'value = 1\n'


def test_lost_ready_response_reopens_same_workspace(setup, monkeypatch):
    operation, _ = planned(setup)
    original = setup['controller']._public
    def crash(value):
        if value['phase'] == 'ready':
            raise SimulatedCrash()
        return original(value)
    monkeypatch.setattr(setup['controller'], '_public', crash)
    with pytest.raises(SimulatedCrash):
        setup['controller'].materialize(operation)
    reopened = LocalGitWorkspaceController(setup['state'], grant=setup['grant'])
    assert reopened.materialize(operation)['status'] == 'ready'
    assert len(list(setup['work'].iterdir())) == 1


def test_preexisting_destination_is_preserved_and_requires_review(setup):
    operation, _ = planned(setup)
    path = setup['work'] / operation; path.mkdir(mode=0o700)
    sentinel = path / 'sentinel'; sentinel.write_text('do not replace')
    with pytest.raises(DevelopmentError, match='git_workspace_preparation_failed'):
        setup['controller'].materialize(operation)
    assert sentinel.read_text() == 'do not replace'
    assert setup['controller'].status(operation)['phase'] == 'needs_review'


def test_durability_failure_never_publishes_ready_or_retries_copy(setup, monkeypatch):
    operation, _ = planned(setup)
    def failure(_):
        raise OSError('synthetic fsync failure')
    monkeypatch.setattr(setup['controller'], '_fsync_tree', failure)
    with pytest.raises(DevelopmentError, match='git_workspace_preparation_failed'):
        setup['controller'].materialize(operation)
    assert setup['controller'].status(operation)['phase'] == 'needs_review'
    assert (setup['work'] / operation / 'checkout/example.py').read_text() == 'value = 1\n'
    monkeypatch.setattr(setup['controller'], '_clone', lambda *_: pytest.fail('recopy after failed durability'))
    with pytest.raises(DevelopmentError, match='git_workspace_interrupted_requires_review'):
        setup['controller'].materialize(operation)


def test_manifest_inputs_compose_with_existing_preparation_api(setup, tmp_path):
    operation, _ = planned(setup)
    setup['controller'].materialize(operation)
    inputs = setup['controller'].manifest_inputs(operation)
    observed = []
    def authorize(scope, action, resource):
        observed.append((scope, action, resource))
        return scope == setup['scope'] and resource == 'authorized-repository'
    hashed = prepare_manifest(tmp_path / 'broker', **inputs, paths=['example.py'],
        resource='authorized-repository', authorize_scope=authorize, actions=['repo.read_file'])
    assert len(hashed) == 64
    manifest = json.loads((tmp_path / 'broker/manifest.json').read_text())
    assert manifest['root'] == inputs['root'] and manifest['base_sha'] == setup['grant'].base_commit
    assert observed == [(setup['scope'], 'repo.read_file', 'authorized-repository')]


@pytest.mark.parametrize('mode', ['branch', 'config', 'head'])
def test_ready_workspace_change_refused_without_resetting_edits(setup, mode):
    operation, _ = planned(setup)
    value = setup['controller'].materialize(operation); checkout = Path(value['checkout'])
    (checkout / 'example.py').write_text('preserve this edit\n')
    if mode == 'branch':
        git(checkout, 'symbolic-ref', 'HEAD', 'refs/heads/other')
    elif mode == 'config':
        with (checkout / '.git/config').open('a') as target:
            target.write('\n[include]\n path = /outside/forbidden\n')
    else:
        git(checkout, 'update-ref', 'refs/heads/' + value['branch'], '0' * 40)
    with pytest.raises(DevelopmentError):
        setup['controller'].materialize(operation)
    assert (checkout / 'example.py').read_text() == 'preserve this edit\n'


def test_source_alternates_refused_before_git_or_copy(setup):
    alternate = setup['source'] / '.git/objects/info/alternates'
    alternate.write_text('/outside/object-store\n')
    with pytest.raises(DevelopmentError, match='git_workspace_source_layout_refused'):
        planned(setup)
    assert not list(setup['work'].iterdir())


@pytest.mark.parametrize('key,value', [('include.path', '/outside/private-config'),
    ('core.worktree', '/outside/worktree'), ('extensions.partialClone', 'outside'),
    ('remote.outside.promisor', 'true')])
def test_source_configuration_cannot_expand_local_resource(setup, key, value):
    git(setup['source'], 'config', key, value)
    with pytest.raises(DevelopmentError, match='git_workspace_source_layout_refused'):
        planned(setup)
    assert not list(setup['work'].iterdir())


def test_exact_base_must_be_commit_not_tree_or_symbolic_revision(setup):
    for base in (git(setup['source'], 'rev-parse', 'HEAD^{tree}'), 'HEAD', '--all'):
        grant = replace(setup['grant'], base_commit=base)
        with pytest.raises(DevelopmentError, match='git_workspace_base_refused'):
            controller = LocalGitWorkspaceController(setup['state'], grant=grant)
            controller.plan(operation_id=str(uuid.uuid4()), scope=setup['scope'])
    assert not list(setup['work'].iterdir())


def test_source_administrative_directory_replacement_refused(setup):
    operation, _ = planned(setup)
    source = setup['source']
    (source / '.git').rename(source / 'previous-git')
    shutil.copytree(source / 'previous-git', source / '.git')
    with pytest.raises(DevelopmentError, match='git_workspace_pin_changed'):
        setup['controller'].materialize(operation)
    assert not list(setup['work'].iterdir())


def test_selected_tree_size_bound_precedes_copy(setup, monkeypatch):
    monkeypatch.setattr('blackoutburst_agents.git_workspace.MAX_CHECKOUT_FILE_BYTES', 2)
    with pytest.raises(DevelopmentError, match='git_workspace_tree_too_large'):
        planned(setup)
    assert not list(setup['work'].iterdir())


@pytest.mark.parametrize('kind', ['symlink', 'gitlink'])
def test_base_symlink_and_submodule_tree_refused(setup, kind):
    source = setup['source']
    if kind == 'symlink':
        (source / 'escape').symlink_to('/outside/forbidden')
        git(source, 'add', 'escape')
    else:
        git(source, 'update-index', '--add', '--cacheinfo', '160000,' + setup['grant'].base_commit + ',submodule')
    git(source, 'commit', '--quiet', '-m', 'Refused tree')
    grant = replace(setup['grant'], base_commit=git(source, 'rev-parse', 'HEAD'))
    controller = LocalGitWorkspaceController(setup['state'], grant=grant)
    with pytest.raises(DevelopmentError, match='git_workspace_tree_refused'):
        controller.plan(operation_id=str(uuid.uuid4()), scope=setup['scope'])
    assert not list(setup['work'].iterdir())


def test_tampered_record_cannot_escape_workspace_root(setup, tmp_path):
    operation, _ = planned(setup)
    path = setup['state'] / operation / 'workspace.json'
    value = json.loads(path.read_text())
    value['request']['checkout'] = str(tmp_path / 'outside')
    raw = json.dumps(value['request'], sort_keys=True, separators=(',', ':')).encode()
    value['request_sha256'] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(value))
    with pytest.raises(DevelopmentError, match='git_workspace_record_refused'):
        setup['controller'].materialize(operation)
    assert not (tmp_path / 'outside').exists()


@pytest.mark.parametrize('which', ['repository', 'workspace_root', 'state'])
def test_symlink_roots_refused(setup, tmp_path, which):
    target = setup['state'] if which == 'state' else Path(getattr(setup['grant'], which))
    alias = tmp_path / 'alias'; alias.symlink_to(target, target_is_directory=True)
    with pytest.raises((DevelopmentError, OSError)):
        LocalGitWorkspaceController(alias if which == 'state' else setup['state'],
            grant=setup['grant'] if which == 'state' else replace(setup['grant'], **{which: str(alias)}))


def test_overlapping_roots_refused(setup):
    with pytest.raises(DevelopmentError, match='git_workspace_roots_overlap'):
        LocalGitWorkspaceController(setup['state'], grant=replace(setup['grant'],
            workspace_root=str(setup['source'])))


@pytest.mark.parametrize('bad', ['../escape', '/outside', 'https://example.invalid/repo', '-command'])
def test_operation_cannot_be_a_path_or_git_argument(setup, bad):
    with pytest.raises(DevelopmentError, match='git_workspace_operation_refused'):
        planned(setup, operation=bad)
    assert not list(setup['state'].iterdir())


def test_no_inherited_git_helpers_or_templates_execute(setup, monkeypatch, tmp_path):
    marker = tmp_path / 'executed'
    hook = tmp_path / 'evil-hook'; hook.write_text('#!/bin/sh\ntouch ' + str(marker) + '\n'); hook.chmod(0o700)
    templates = tmp_path / 'evil-template'; (templates / 'hooks').mkdir(parents=True)
    (templates / 'hooks/post-checkout').write_bytes(hook.read_bytes())
    (templates / 'hooks/post-checkout').chmod(0o700)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '2')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.hooksPath')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', str(templates / 'hooks'))
    monkeypatch.setenv('GIT_CONFIG_KEY_1', 'uploadpack.packObjectsHook')
    monkeypatch.setenv('GIT_CONFIG_VALUE_1', str(hook))
    monkeypatch.setenv('GIT_TEMPLATE_DIR', str(templates))
    monkeypatch.setenv('GIT_SSH_COMMAND', str(hook))
    git(setup['source'], 'config', 'uploadpack.packObjectsHook', str(hook))
    git(setup['source'], 'config', 'core.hooksPath', str(templates / 'hooks'))
    operation, _ = planned(setup)
    assert setup['controller'].materialize(operation)['status'] == 'ready'
    assert not marker.exists()
