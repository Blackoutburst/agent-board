"""RECETTE-51 §7.6 rollback preflight, on a fake owner root only.

The script runs as a separate `python -I -B` process, exactly as the runbook
invokes it (without sudo/flock), so it cannot import the package.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'recette51' / 'preflight_76.py'
KEY = 'a' * 64
OTHER_KEY = 'b' * 64
REQUEST = '11111111-2222-4333-8444-555555555555'
SECRET = 'PRIVATE_CATALOG_VALUE'


def directory(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True); path.chmod(0o700)
    return path


def document(path, value):
    path.write_bytes(value if type(value) is bytes else json.dumps(value).encode()); path.chmod(0o600)
    return path


def tick(base, key, phase='idle', active=None):
    queue = directory(directory(base / 'admissions') / ('queue-' + key))
    document(queue / 'tick.lock', b'')
    document(queue / 'tick.json', {'schema': 'development-queue-tick-state-v1', 'phase': phase, 'active': active,
                                   'approval_sha256': SECRET, 'last_receipt': {'file': SECRET}})
    return queue


@pytest.fixture
def root(tmp_path):
    owner = directory(tmp_path / 'owner')
    config = directory(owner / 'config')
    document(config / 'continuation.json', b'{"schema":"slots-in-use"}')
    document(config / 'continuation.pre51.json', b'{"schema":"single-standing"}')
    document(config / 'slots.json', b'{"schema":"linux-cloud-queue-slots-v1"}')
    slots = directory(owner / 'slots')
    for name in ('dev-1', 'dev-2'):
        slot = directory(slots / name)
        tick(slot, KEY)
        directory(slot / 'admissions' / REQUEST)
        directory(slot / 'checkouts')
    tick(owner, OTHER_KEY)
    directory(owner / 'continuations')
    return owner


def run(root, *extra):
    return subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), '--root', str(root), *extra],
                          capture_output=True, text=True, timeout=60, env={'PATH': os.environ.get('PATH', '')})


def snapshot(root):
    result = {}
    for path in sorted(root.rglob('*')):
        info = os.lstat(path)
        result[str(path.relative_to(root))] = (info.st_mode, info.st_ino, info.st_mtime_ns,
                                               path.read_bytes() if path.is_file() and not path.is_symlink() else None)
    return result


def test_all_idle_passes_read_only_and_prints_only_path_phase_and_active(root):
    before = snapshot(root)
    result = run(root)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines == [
        'admissions/queue-%s/tick.json phase=idle active_is_none=True' % OTHER_KEY,
        'slots/dev-1/admissions/queue-%s/tick.json phase=idle active_is_none=True' % KEY,
        'slots/dev-2/admissions/queue-%s/tick.json phase=idle active_is_none=True' % KEY,
        'pointers=3 all_idle=True']
    assert SECRET not in result.stdout + result.stderr
    assert snapshot(root) == before  # The dry run never writes.


@pytest.mark.parametrize('phase, active', [('active', {'request_id': REQUEST}), ('admitting', None),
                                           ('idle', {'request_id': REQUEST})])
def test_one_busy_pointer_refuses_and_apply_writes_nothing(root, phase, active):
    tick(root / 'slots' / 'dev-2', KEY, phase, active)
    before = snapshot(root)
    for extra in ((), ('--apply',)):
        result = run(root, *extra)
        assert result.returncode == 1
        assert 'slots/dev-2/admissions/queue-%s/tick.json phase=%s active_is_none=%s' % (
            KEY, phase, active is None) in result.stdout
        assert 'all_idle=False' in result.stdout and 'old_sha256' not in result.stdout
        assert SECRET not in result.stdout and REQUEST not in result.stdout
    assert snapshot(root) == before


@pytest.mark.parametrize('where', ['slot', 'admissions', 'queue', 'tick', 'slots'])
def test_a_symlink_anywhere_on_the_pointer_paths_refuses(root, tmp_path, where):
    outside = directory(tmp_path / 'outside')
    slot = root / 'slots' / 'dev-1'
    if where == 'slot':
        os.symlink(outside, root / 'slots' / 'dev-3')
    elif where == 'admissions':
        directory(root / 'slots' / 'dev-3'); os.symlink(outside, root / 'slots' / 'dev-3' / 'admissions')
    elif where == 'queue':
        os.symlink(outside, slot / 'admissions' / ('queue-' + OTHER_KEY))
    elif where == 'tick':
        target = document(outside / 'tick.json', {'phase': 'idle', 'active': None})
        (slot / 'admissions' / ('queue-' + KEY) / 'tick.json').unlink()
        os.symlink(target, slot / 'admissions' / ('queue-' + KEY) / 'tick.json')
    else:
        moved = root / 'slots-real'; os.rename(root / 'slots', moved); os.symlink(moved, root / 'slots')
    result = run(root, '--apply')
    assert result.returncode == 2 and 'refused' in result.stderr
    assert (root / 'config' / 'continuation.json').read_bytes() == b'{"schema":"slots-in-use"}'


@pytest.mark.parametrize('target, mode', [('slots', 0o750), ('slots/dev-1', 0o755), ('slots/dev-1/admissions', 0o701),
    ('admissions/queue-' + OTHER_KEY, 0o770), ('slots/dev-1/admissions/queue-' + KEY + '/tick.json', 0o640),
    ('slots/dev-1/admissions/queue-' + KEY + '/tick.lock', 0o644), ('', 0o711)])
def test_a_bad_mode_refuses(root, target, mode):
    (root / target).chmod(mode)
    result = run(root)
    assert result.returncode == 2 and 'mode is not' in result.stderr


@pytest.mark.parametrize('case', ['unexpected_queue_entry', 'unexpected_admission_entry', 'unexpected_slot_name',
                                  'hard_link', 'not_regular', 'missing_tick', 'invalid_json', 'duplicate_key',
                                  'not_a_pointer'])
def test_unexpected_entries_and_documents_refuse(root, case):
    queue = root / 'slots' / 'dev-1' / 'admissions' / ('queue-' + KEY)
    if case == 'unexpected_queue_entry':
        document(queue / ('write-' + 'c' * 32), b'{}')
    elif case == 'unexpected_admission_entry':
        document(root / 'admissions' / 'notes.txt', b'x')
    elif case == 'unexpected_slot_name':
        directory(root / 'slots' / 'bad name')
    elif case == 'hard_link':
        os.link(queue / 'tick.json', root / 'config' / 'linked')
    elif case == 'not_regular':
        (queue / 'tick.json').unlink(); os.mkfifo(queue / 'tick.json', 0o600)
    elif case == 'missing_tick':
        (queue / 'tick.json').unlink()
    elif case == 'invalid_json':
        document(queue / 'tick.json', b'{"phase": "idle", "active": nul')
    elif case == 'duplicate_key':
        document(queue / 'tick.json', b'{"phase":"active","phase":"idle","active":null}')
    else:
        document(queue / 'tick.json', b'["idle"]')
    result = run(root)
    assert result.returncode == 2 and 'refused' in result.stderr
    assert SECRET not in result.stderr


def test_apply_requires_a_private_single_link_pre51(root):
    pre51 = root / 'config' / 'continuation.pre51.json'
    for prepare in (lambda: pre51.unlink(), lambda: pre51.chmod(0o644),
                    lambda: os.link(pre51, root / 'config' / 'second-link')):
        prepare()
        result = run(root, '--apply')
        assert result.returncode == 2 and 'continuation.pre51.json' in result.stderr
        assert (root / 'config' / 'continuation.json').read_bytes() == b'{"schema":"slots-in-use"}'
        # Restore a clean pre51 for the next variant.
        for extra in (root / 'config' / 'second-link',):
            if extra.exists(): extra.unlink()
        if pre51.exists(): pre51.unlink()
        document(pre51, b'{"schema":"single-standing"}')


def test_apply_replaces_atomically_and_keeps_every_other_file(root):
    config = root / 'config'
    old_inode = os.stat(config / 'continuation.json').st_ino
    before = snapshot(root)
    result = run(root, '--apply')
    assert result.returncode == 0, result.stderr
    old = hashlib.sha256(b'{"schema":"slots-in-use"}').hexdigest()
    new = hashlib.sha256(b'{"schema":"single-standing"}').hexdigest()
    assert result.stdout.splitlines()[-1] == 'continuation.json old_sha256=%s new_sha256=%s' % (old, new)
    info = os.stat(config / 'continuation.json')
    assert (config / 'continuation.json').read_bytes() == b'{"schema":"single-standing"}'
    assert oct(info.st_mode & 0o777) == '0o600' and info.st_nlink == 1 and info.st_ino != old_inode
    after = snapshot(root)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    # Only the replaced file (and its directory's own mtime) changed: the slots
    # document, slot roots, pre51 and every history are kept.
    assert changed == {'config', 'config/continuation.json'}
    assert not [name for name in os.listdir(config) if name.startswith('.continuation.json.preflight')]


def test_missing_root_or_relative_root_refuses(tmp_path):
    assert run(tmp_path / 'absent').returncode == 2
    result = subprocess.run([sys.executable, '-I', '-B', str(SCRIPT), '--root', 'relative'],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2


def test_script_is_standard_library_only():
    source = SCRIPT.read_text()
    assert 'blackoutburst_agents' not in source
    imports = {line.split()[1] for line in source.splitlines() if line.startswith(('import ', 'from '))}
    assert imports <= {'argparse', 'hashlib', 'json', 'os', 're', 'stat', 'sys'}
