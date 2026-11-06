"""Installed entry routes N-slot queue passes only from a 0.50 release."""
import json

import pytest

from deploy.worker_pool import entry
from test_worker_pool_continuation_entry import entry_fixture
from test_queue_slots import slots, ENDPOINT  # noqa: F401


def test_slots_plan_is_inert_through_the_installed_entry(slots, monkeypatch, capsys):
    for name in ('a', 'b'):
        slots.slot(name, ['repo:' + name])
    path = slots.configure(2, ['a', 'b'])
    _, manifest, argv = entry_fixture(monkeypatch, slots.root, '0.50.0'); manifest['board_endpoint'] = ENDPOINT
    monkeypatch.setattr(entry, 'in_continuation_unit', lambda *_: pytest.fail('planning needs no unit'))
    before = {str(p): p.read_bytes() for p in slots.root.rglob('*') if p.is_file()}
    assert entry.main(argv('continuation-plan', '--continuation', str(path))) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['schema'] == 'linux-cloud-queue-slots-v1' and result['concurrency'] == 2
    assert result['activated'] is result['execution_qualified'] is result['launchable'] is False
    assert before == {str(p): p.read_bytes() for p in slots.root.rglob('*') if p.is_file()}
    assert slots.board.calls == []


@pytest.mark.parametrize('action', ['continuation-plan', 'run-continuation-once'])
def test_older_release_cannot_route_queue_slots(slots, monkeypatch, capsys, action):
    slots.slot('a', ['repo:a']); path = slots.configure(1, ['a'])
    _, manifest, argv = entry_fixture(monkeypatch, slots.root, '0.49.1'); manifest['board_endpoint'] = ENDPOINT
    monkeypatch.setattr(entry, 'in_continuation_unit', lambda *_: None)
    assert entry.main(argv(action, '--continuation', str(path))) == 3
    assert json.loads(capsys.readouterr().out)['reason'] == 'pool_queue_slots_version_required'
    assert slots.board.calls == [] and not (slots.root / 'continuations').exists()


def test_slots_pass_runs_through_the_installed_entry_after_its_unit_gate(slots, monkeypatch, capsys):
    slots.slot('a', ['repo:a']); slots.board.add('t-a', 'coder-a', 'repo:a')
    path = slots.configure(1, ['a']); gates = []
    _, manifest, argv = entry_fixture(monkeypatch, slots.root, '0.50.0'); manifest['board_endpoint'] = ENDPOINT
    monkeypatch.setattr(entry, 'in_continuation_unit', lambda *_: gates.append(True))
    assert entry.main(argv('run-continuation-once', '--continuation', str(path))) == 0
    result = json.loads(capsys.readouterr().out)
    assert gates == [True] and result['status'] == 'pass_complete'
    assert result['slots'][0]['phase'] == 'paused_cycle' and slots.board.claims == {'t-a': 1}
