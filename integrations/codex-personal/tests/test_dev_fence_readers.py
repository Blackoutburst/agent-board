"""Stable standalone readers share metadata, never execution authority."""
import fcntl
import os
from pathlib import Path
import threading
import time
import traceback
import uuid

import pytest

from blackoutburst_agents import dev_fence as fences
from blackoutburst_agents.dev_manifest import DevelopmentError
from test_dev_fence_wait import binding, holding, delayed_release


def seeded(tmp_path):
    request = binding(tmp_path)
    operation = str(uuid.uuid4())
    fence = fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    fence.bind(request, operation)
    return fence, request, operation


def test_four_readers_include_existing_initialization_and_never_wait(tmp_path, monkeypatch):
    seed, request, operation = seeded(tmp_path)
    raw = os.open(tmp_path/'anchor/fence.lock', os.O_RDWR)
    fcntl.flock(raw, fcntl.LOCK_SH | fcntl.LOCK_NB)
    barrier = threading.Barrier(4)
    original = fences.DevelopmentFence._read
    reads, results, errors = [], [], []
    def concurrent_read(self):
        reads.append(threading.get_ident())
        barrier.wait(2)  # Every read must coexist, including constructor reads.
        return original(self)
    monkeypatch.setattr(fences.DevelopmentFence, '_read', concurrent_read)
    def run():
        current = None
        try:
            before = fences.fence_wait_revision()
            with fences.standalone_fence_wait():
                current = fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
                current.verify(request, operation)
                assert current.inspect_binding(request['scope'], request['root_identity'])['operation_id'] == operation
                assert current.inspect_checkout(request['root_identity'])['operation_id'] == operation
            results.append((before, fences.fence_wait_revision()))
        except BaseException:
            errors.append(traceback.format_exc())
        finally:
            if current is not None: current.close()
    threads = [threading.Thread(target=run) for _ in range(4)]
    try:
        for thread in threads: thread.start()
        for thread in threads: thread.join(4)
        assert not errors, errors
        assert all(not thread.is_alive() for thread in threads)
        assert len(reads) == 16 and len(set(reads)) == 4
        assert len(results) == 4 and all(before == after for before, after in results)
    finally:
        os.close(raw); seed.close()


def test_reader_waits_only_for_real_local_writer_and_advances_revision(tmp_path):
    owner, request, operation = seeded(tmp_path)
    reader = fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    try:
        with holding(owner) as release:
            timer = delayed_release(release)
            before = fences.fence_wait_revision()
            with fences.standalone_fence_wait(): reader.verify(request, operation)
            timer.join()
            assert fences.fence_wait_revision() == before + 1
    finally: reader.close(); owner.close()


def test_queued_writer_precedes_new_reader_without_holding_network_or_worker(tmp_path):
    seed, _, _ = seeded(tmp_path)
    handles = [fences.DevelopmentFence(_test_anchor=tmp_path/'anchor') for _ in range(3)]
    first_entered, release_first = threading.Event(), threading.Event()
    writer_entered, release_writer = threading.Event(), threading.Event()
    last_entered = threading.Event()
    order, errors = [], []
    def run(index, readonly, entered, release):
        try:
            with fences.standalone_fence_wait(), handles[index].lock(readonly=readonly):
                order.append(index); entered.set()
                if release is not None: assert release.wait(2)
        except BaseException: errors.append(traceback.format_exc())
    first = threading.Thread(target=run, args=(0, True, first_entered, release_first))
    writer = threading.Thread(target=run, args=(1, False, writer_entered, release_writer))
    last = threading.Thread(target=run, args=(2, True, last_entered, None))
    try:
        first.start(); assert first_entered.wait(1)
        writer.start()
        deadline = time.monotonic() + 1
        gate = seed._metadata_lock
        while True:
            with gate.condition: queued = gate.writers_waiting == 1
            if queued: break
            assert time.monotonic() < deadline
            time.sleep(.001)
        last.start(); assert not last_entered.wait(.02)
        release_first.set(); assert writer_entered.wait(1)
        assert not last_entered.wait(.02)
        release_writer.set()
        for thread in (first, writer, last): thread.join(2)
        assert not errors, errors
        assert order == [0, 1, 2] and last_entered.is_set()
    finally:
        release_first.set(); release_writer.set()
        for thread in (first, writer, last):
            if thread.ident is not None: thread.join(2)
        for handle in handles: handle.close()
        seed.close()


@pytest.mark.parametrize('operation', ['initialize', 'verify', 'inspect'])
def test_external_writer_stays_nonblocking_and_never_counts_local_wait(tmp_path, operation):
    fence, request, operation_id = seeded(tmp_path)
    raw = os.open(tmp_path/'anchor/fence.lock', os.O_RDWR)
    try:
        fcntl.flock(raw, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = fences.fence_wait_revision()
        with fences.standalone_fence_wait(), pytest.raises(DevelopmentError, match='fence_busy'):
            if operation == 'initialize': fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
            elif operation == 'verify': fence.verify(request, operation_id)
            else: fence.inspect_checkout(request['root_identity'])
        assert fences.fence_wait_revision() == before
    finally: os.close(raw); fence.close()


def test_external_shared_reader_permits_opt_in_reads_but_not_mutations_or_old_callers(tmp_path):
    fence, request, operation = seeded(tmp_path)
    raw = os.open(tmp_path/'anchor/fence.lock', os.O_RDWR)
    try:
        fcntl.flock(raw, fcntl.LOCK_SH | fcntl.LOCK_NB)
        with fences.standalone_fence_wait():
            fence.verify(request, operation)
            current = fences.DevelopmentFence(_test_anchor=tmp_path/'anchor'); current.close()
            with pytest.raises(DevelopmentError, match='fence_busy'): fence.bind(request, operation)
        with pytest.raises(DevelopmentError, match='fence_busy'): fence.verify(request, operation)
        with pytest.raises(DevelopmentError, match='fence_busy'):
            fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    finally: os.close(raw); fence.close()


def test_concurrent_absent_anchor_creation_double_checks_under_exclusive_lock(tmp_path):
    barrier = threading.Barrier(4)
    errors, created = [], []
    def run():
        current = None
        try:
            barrier.wait(1)
            with fences.standalone_fence_wait():
                current = fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
                assert current._read() == {'schema': 1, 'records': {}}
                created.append(current.private.identity)
        except BaseException: errors.append(traceback.format_exc())
        finally:
            if current is not None: current.close()
    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(3)
    assert not errors, errors
    assert len(created) == 4 and len(set(created)) == 1
    assert sorted(path.name for path in (tmp_path/'anchor').iterdir()) == ['fence.json', 'fence.lock']


@pytest.mark.parametrize('bad', ['corrupt', 'symlink', 'mode', 'directory'])
def test_present_invalid_initial_document_is_never_recreated(tmp_path, bad):
    fence, _, _ = seeded(tmp_path); fence.close()
    document = tmp_path/'anchor/fence.json'
    document.unlink()
    if bad == 'corrupt': document.write_bytes(b'{'); document.chmod(0o600)
    elif bad == 'symlink': document.symlink_to(tmp_path/'absent-target')
    elif bad == 'mode': document.write_bytes(b'{}'); document.chmod(0o400)
    else: document.mkdir(mode=0o700)
    before = document.lstat()
    with fences.standalone_fence_wait(), pytest.raises((DevelopmentError, OSError)):
        fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    after = document.lstat()
    assert (before.st_ino, before.st_mode, before.st_size) == (after.st_ino, after.st_mode, after.st_size)


def test_absence_probe_does_not_reinterpret_later_read_failure_as_creation(tmp_path, monkeypatch):
    fence, _, _ = seeded(tmp_path); fence.close()
    original = fences.DevelopmentFence._read
    def changed(self):
        if self.private.path == tmp_path/'anchor':
            raise DevelopmentError('private_read_failed')
        return original(self)
    monkeypatch.setattr(fences.DevelopmentFence, '_read', changed)
    before = (tmp_path/'anchor/fence.json').read_bytes()
    with fences.standalone_fence_wait(), pytest.raises(DevelopmentError, match='private_read_failed'):
        fences.DevelopmentFence(_test_anchor=tmp_path/'anchor')
    assert (tmp_path/'anchor/fence.json').read_bytes() == before
