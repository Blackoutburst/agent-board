"""Actual installed entry -> standing queue facade -> Core -> selected claims.

The existing final-lifecycle helper's third call explicitly reads the generated
Core configuration through the same entry, because it asserts task history.
The next call uses the standing facade again and discovers a new assigned task.
No result, completion receipt or claim is substituted.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

from deploy.worker_pool import entry
from blackoutburst_agents import continuation_pool_service, development_queue
from blackoutburst_agents.continuation_journal import ContinuationJournal
from blackoutburst_agents.dev_journal import decode
from blackoutburst_agents.dev_manifest import canonical, digest,DevelopmentError
from blackoutburst_agents.development_queue import DevelopmentQueueAdmission
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.development_queue import queue_tick_plan
from blackoutburst_agents.development_queue_client import DevelopmentQueueDiscoveryClient
from test_queued_continuation_service import queued_service, actual_service, linux_lock_observation
from test_continuation_pool_service_integration import (
    test_actual_final_delivery_uses_original_base_and_releases_for_independent_review as _actual_final_delivery)


@pytest.fixture
def queued_entry(queued_service, monkeypatch):
    f, root, task_id, ticket, _, calls, sequence, _ = queued_service
    batch_path = root / 'config/batch.json'; batch = decode(batch_path.read_bytes()); job = batch['jobs'][0]
    # Exactly the fixed verifier later written by the existing final helper.
    verifier = Path(job['verifier_path'])
    verifier.write_bytes(b'from pathlib import Path\nassert Path("one.py").read_bytes() == b"value = 3\\n"\n')
    job['verifier_sha256'] = digest(verifier.read_bytes()); batch_path.write_bytes(canonical(batch))
    omit = {'name', 'agent_id', 'connection_file', 'project_id', 'ticket_id', 'repository', 'queue_admission'}
    selected = {key: deepcopy(value) for key, value in job.items() if key not in omit}
    selected.update(entry_id=job['name'], repository={key: job['repository'][key]
        for key in ('repository_id', 'repository', 'base_commit')})
    profile = f.board.get_agent_profile(f.human, f.agent)
    catalog = dict(schema='development-queue-catalog-v1', owner_id='owner', agent_id=f.agent,
        profile_version=profile['version'], board_endpoint=batch['board_endpoint'], project_id=f.project['id'],
        connection_file=job['connection_file'], timeout_seconds=batch['timeout_seconds'], entries=[selected])
    catalog_path = root / 'config/standing-catalog.json'; catalog_path.write_bytes(canonical(catalog)); catalog_path.chmod(0o600)
    workflow_path = root / 'config/workflow.json'
    plan = queue_tick_plan(root, str(catalog_path), str(workflow_path), checkpoint_after_actions=3)
    wrapper = root / 'config/standing-continuation.json'
    wrapper.write_bytes(canonical(dict(schema='linux-cloud-queue-continuation-v1', catalog_file=str(catalog_path),
        workflow_file=str(workflow_path), approved_sha256=plan['approval_sha256'],
        checkpoint_after_actions=3, provider_backoff_seconds=60))); wrapper.chmod(0o600)

    release = f.base / 'installed-release'; (release / 'tools').mkdir(parents=True, mode=0o700)
    (release / 'source').mkdir(mode=0o700)
    for name, source in (('codex', f.tools['codex']), ('gh', f.tools['python'])):
        path = release / 'tools' / name; path.write_bytes(Path(source).read_bytes()); path.chmod(0o700)
    manifest = dict(adapter_version='0.48.0', unix_owner='fixture-worker', board_owner='owner',
        board_endpoint=batch['board_endpoint'], codex_version='1.2.3', python={'path': f.tools['python']},
        unit_limits={'lifetime_seconds': 990})
    account = SimpleNamespace(pw_dir=str(f.base), pw_uid=os.getuid(), pw_gid=os.getgid())
    unit_checks = []
    monkeypatch.setattr(entry.p, 'owner_root', lambda owner: root)
    monkeypatch.setattr(entry, 'in_continuation_unit', lambda *args: unit_checks.append(True))
    monkeypatch.setattr(sys, 'path', list(sys.path))  # entry inserts its protected release source path.
    executable_pin = continuation_pool_service._executable_pin
    # The installed entry's fixed Linux path is absent on the Mac test host.
    # Map that one public tool identity only; all returned pins are real files.
    monkeypatch.setattr(continuation_pool_service, '_executable_pin', lambda given:
        executable_pin(f.tools['unshare'] if os.fspath(given) == '/usr/bin/unshare' else given))

    f.discovery_calls = []; f.discovery_sessions = []
    class Discovery:
        endpoint = batch['board_endpoint']
        def __init__(self, path, session_id):
            self.session_id = session_id; self.connection_path = str(path)
            self.connection_pin = file_document(path)[1]
            f.discovery_sessions.append(session_id)
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def registration(self, *, autonomous=False):
            f.discovery_calls.append(('register', self.session_id))
            return f.board.register_agent(f.raw, self.session_id, 'Queue discovery', [], 'codex', autonomous, 'worker')
        def call(self, name, values):
            assert name in ('get_work', 'get_project', 'get_ticket')
            f.discovery_calls.append((name, deepcopy(values)))
            return getattr(f.board, name)(f.board.actor_session(f.raw, self.session_id), **values)
    monkeypatch.setattr(DevelopmentQueueDiscoveryClient, 'from_connection_file',
        lambda path, *, session_id, timeout=20: Discovery(path, session_id))

    request = uuid.uuid4(); next_request = uuid.uuid4(); next_logical = uuid.uuid4()
    identifiers = iter((request, uuid.UUID(task_id), next_request, next_logical))
    # Replace only this module binding, never the process-wide uuid.uuid4.
    monkeypatch.setattr(development_queue, 'uuid', SimpleNamespace(uuid4=lambda: next(identifiers)))
    def invoke(action='run-continuation-once', *, core=False):
        path = root / 'config' / ('queue-continuation-' + str(request) + '.json') if core else wrapper
        args = SimpleNamespace(action=action, owner='fixture-worker', continuation=str(path))
        return entry.continuation_entry(args, manifest, release, account)
    return SimpleNamespace(f=f, root=root, task_id=task_id, ticket=ticket, calls=calls, sequence=sequence,
        catalog=catalog_path, workflow=workflow_path, wrapper=wrapper, invoke=invoke, unit_checks=unit_checks,
        request=request, next_request=next_request, next_logical=next_logical,
        release=release,manifest=manifest,account=account)


def test_installed_queue_plan_is_pure_and_never_reads_a_connection_or_constructs_client(queued_entry, monkeypatch):
    case = queued_entry; before = {str(path): digest(path.read_bytes())
        for path in case.root.rglob('*') if path.is_file()}
    original = os.open
    def opened(path, *args, **kwargs):
        assert Path(os.fsdecode(path)).name not in ('coder.json', 'reviewer.json'), 'Planning must not read a connection'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', opened)
    monkeypatch.setattr(DevelopmentQueueDiscoveryClient, 'from_connection_file',
        lambda *args, **kwargs: pytest.fail('Planning must not construct a client'))
    result = case.invoke('continuation-plan')
    assert result['schema'] == 'linux-cloud-queue-continuation-v1' and result['status'] == 'plan'
    assert result['activated'] is False and result['execution_qualified'] is False and result['launchable'] is False
    assert not case.unit_checks and not case.f.discovery_calls and not case.f.queue_clients
    assert before == {str(path): digest(path.read_bytes()) for path in case.root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('known',[True,False])
def test_installed_queue_runtime_refusal_is_closed_without_inventing_cleanup(queued_entry,monkeypatch,capsys,known):
    class PrivateError(Exception):
        def __str__(self):raise AssertionError('Do not stringify private exceptions')
        def __repr__(self):raise AssertionError('Do not repr private exceptions')
        def __getattribute__(self,key):
            if key in ('code','args','__dict__'):raise AssertionError('Do not inspect private exception fields')
            return super().__getattribute__(key)
    error=DevelopmentError('development_queue_approval_changed') if known else PrivateError('PRIVATE_BODY')
    attempts=[]
    def refused(*args,**kwargs):
        attempts.append(True);raise error
    monkeypatch.setattr(DevelopmentQueueAdmission,'tick',refused)
    monkeypatch.setattr(entry.p,'verify_release',lambda *_:queued_entry.manifest)
    monkeypatch.setattr(entry,'owner_context',lambda *_:queued_entry.account)
    assert entry.main(['--release',str(queued_entry.release),'--owner','fixture-worker',
        'run-continuation-once','--continuation',str(queued_entry.wrapper)])==3
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='needs_you' and result['activated'] is True and result['tasks']==[]
    assert result['pass_complete_is_project_completion'] is False
    failure=result['failure']
    assert failure['stage']=='queue_tick' and failure['code']==('development_queue_approval_changed' if known else 'unrecognized_code')
    assert failure['category']==('development_error' if known else 'unknown_error')
    assert failure['logical_task_id'] is failure['operation_id'] is failure['generation_id'] is None
    assert 'cleanup' not in json.dumps(result) and 'model_calls' not in result and 'PRIVATE' not in json.dumps(result)
    assert attempts==[True] and not queued_entry.calls.exists()


def test_installed_entry_completes_two_generations_then_discovers_next_assigned_ticket(queued_entry, monkeypatch):
    case = queued_entry; f = case.f; helper_calls = []
    def construct():
        class Pass:
            def run_once(self):
                helper_calls.append(True)
                assert len(helper_calls) <= 3
                # The helper's own replay assertion targets the completed Core;
                # every call still traverses the true installed entry and SDK.
                return case.invoke(core=len(helper_calls) == 3)
        return Pass()
    _actual_final_delivery((f, case.root, case.task_id, case.ticket, construct, case.calls, case.sequence),
        monkeypatch, generations=2)
    assert len(helper_calls) == 3 and len(f.queue_claims) == 2 and len(f.review_turns) == 1
    assert len(set(f.discovery_sessions)) == 1
    queue_reads = sum(name == 'get_work' for name, _ in f.discovery_calls)
    assert queue_reads == 3  # discovery + pre-intent and fresh admission confirmation, never generation two
    old = case.root / 'continuations' / digest(canonical({'owner_id': 'owner', 'logical_task_id': case.task_id}))
    old_bytes = {str(path): path.read_bytes() for path in old.rglob('*') if path.is_file()}
    current = f.board.get_ticket(f.human, case.ticket['id'])['ticket']
    assert current['status'] == 'review' and current['claimant'] is None
    ticket = f.board.create_ticket(f.human, f.project['id'], 'Next assigned queue task',
        resource='repo:fixture', idempotency_key='next-queue-task')
    ticket = f.board.update_ticket(f.human, ticket['id'], ticket['version'], assignee_agent_id=f.agent)
    case.sequence.write_text(json.dumps([
        dict(schema=1, kind='patch', path='one.py', expected_sha256=digest(b'value = 1\n'), content='value = 2\n'),
        dict(schema=1, kind='read', path='one.py'), dict(schema=1, kind='read', path='one.py')]))
    result = case.invoke()
    assert result['status'] == 'pass_complete' and result['tasks'][0]['phase'] == 'paused_cycle', result
    assert result['queue']['request_id'] == str(case.next_request)
    assert result['queue']['logical_task_id'] == str(case.next_logical)
    assert f.queue_claims[-1]['ticket_id'] == ticket['id'] and f.queue_claims[-1]['expected_version'] == ticket['version']
    assert len(f.queue_claims) == 3 and len({row['session_id'] for row in f.queue_claims}) == 3
    assert len(set(f.discovery_sessions)) == 1 and sum(name == 'get_work' for name, _ in f.discovery_calls) == queue_reads + 3
    assert len(case.calls.read_text().splitlines()) == 9 and len(f.review_turns) == 1
    assert all(Path(path).read_bytes() == raw for path, raw in old_bytes.items())
    with ContinuationJournal.open(old / 'logical') as journal:
        assert journal.snapshot()['state']['phase'] == 'complete'
    assert f.board.get_runtime(f.human)['active_count'] == 0
    assert len(case.unit_checks) == 4
