"""Live file locks on controlled fixtures only; no process, fork, model or RPC."""
import copy
import fcntl
import json
import os
import pickle
import threading
from unittest import TestCase
from unittest.mock import patch

import test_linux_input_snapshot as fixture
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from blackoutburst_agents.linux_input_snapshot import capture_input_snapshot,capture_input_snapshot_locked


class LinuxLockLeaseTests(TestCase):
    def setUp(self):
        fixture.LinuxInputSnapshotTests.setUp(self)
        self.journal_object=self.broker.journal

    def capture(self,lease,**changes):
        args=dict(lock_lease=lease,fence=self.fence,binding=self.binding,
            operation_id=self.operation,planner_snapshot=self.planner)
        args.update(changes)
        return capture_input_snapshot_locked(self.broker,**args)

    def refuse(self,call):
        with self.assertRaises(DevelopmentError):call()

    def other_broker(self):
        other=DevelopmentBroker(self.journal,authorize_scope=lambda *args:True,enabled=True)
        self.addCleanup(other.close)
        return other

    def test_lock_yield_unchanged_and_capture_reuses_actual_acquisition(self):
        other=self.other_broker()
        with self.journal_object.lock() as state:
            self.assertIs(type(state),dict)
            self.journal_object.validate_state(state)
            lease=self.journal_object.held_lock_lease()
            self.assertIs(lease,self.journal_object.held_lock_lease())
            with self.capture(lease) as snapshot:
                self.assertEqual(snapshot.read('right.txt'),b'22\n')
                self.assertTrue(snapshot.revalidate())
                self.refuse(lambda:other.journal.lock().__enter__())
            self.assertTrue(self.journal_object.require_lock_lease(lease))
            with self.capture(lease) as another:self.assertEqual(another.document()['planner_snapshot'],self.planner)
            self.refuse(lambda:capture_input_snapshot(self.broker,fence=self.fence,binding=self.binding,
                operation_id=self.operation,planner_snapshot=self.planner).__enter__())
        with other.journal.lock():pass
        self.authorize.assert_not_called()

    def test_no_lease_is_issued_outside_real_lock_or_by_asserted_flag(self):
        self.refuse(self.journal_object.held_lock_lease)
        for value in (True,False,None,object(),'held',lambda:True):
            self.refuse(lambda v=value:self.capture(v).__enter__())
        self.assertIsNone(self.journal_object._lease)

    def test_expired_lease_cannot_be_replayed_in_another_acquisition(self):
        with self.journal_object.lock():old=self.journal_object.held_lock_lease()
        self.refuse(lambda:self.journal_object.require_lock_lease(old))
        with self.journal_object.lock():
            fresh=self.journal_object.held_lock_lease()
            self.assertIsNot(fresh,old)
            self.refuse(lambda:self.capture(old).__enter__())
            self.assertTrue(self.journal_object.require_lock_lease(fresh))

    def test_lease_is_not_serializable_or_copyable(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            for call in (lambda:pickle.dumps(lease),lambda:copy.copy(lease),
                         lambda:copy.deepcopy(lease),lambda:json.dumps(lease)):
                with self.assertRaises(TypeError):call()
            self.assertTrue(self.journal_object.require_lock_lease(lease))

    def test_foreign_journal_instance_cannot_consume_lease(self):
        other=self.other_broker()
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            self.refuse(lambda:other.journal.require_lock_lease(lease))

    def test_other_thread_cannot_issue_or_use_lease(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();result=[]
            def foreign():
                for call in (self.journal_object.held_lock_lease,lambda:self.journal_object.require_lock_lease(lease)):
                    try:call()
                    except DevelopmentError:result.append('refused')
            thread=threading.Thread(target=foreign);thread.start();thread.join(2)
            self.assertFalse(thread.is_alive());self.assertEqual(result,['refused','refused'])

    def test_observed_pid_rejects_inherited_lease_without_real_fork(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();pid=os.getpid()
            with patch('os.getpid',return_value=pid+1):
                self.refuse(lambda:self.journal_object.require_lock_lease(lease))
                with patch('fcntl.flock',side_effect=AssertionError('child must not unlock parent')):
                    lease._revoke()

    def test_inherited_journal_cannot_issue_a_fresh_lease(self):
        pid=os.getpid()
        with patch('os.getpid',return_value=pid+1):
            with self.journal_object.lock():self.refuse(self.journal_object.held_lock_lease)

    def test_lease_invalidated_before_any_unlock(self):
        original=fcntl.flock;lease=None
        def flock(descriptor,operation):
            if operation==fcntl.LOCK_UN:
                self.assertFalse(lease._active)
                self.assertFalse(self.journal_object._in_lock)
                self.assertIsNone(self.journal_object._lease)
            return original(descriptor,operation)
        with patch('fcntl.flock',side_effect=flock):
            with self.journal_object.lock():lease=self.journal_object.held_lock_lease()

    def test_closed_and_reopened_same_inode_fd_is_refused_and_left_untouched(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();descriptor=self.journal_object._lock_fd
            os.close(descriptor)
            replacement=os.open(self.journal/'broker.lock',os.O_RDWR|os.O_NOFOLLOW)
            self.assertEqual(replacement,descriptor)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))
        self.assertIsNone(self.journal_object._lock_fd)
        self.assertTrue(os.fstat(replacement))
        os.close(replacement)

    def test_reused_fd_to_foreign_file_is_not_closed_or_unlocked(self):
        foreign=self.base/'foreign';foreign.write_text('untouched');foreign.chmod(0o600)
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();descriptor=self.journal_object._lock_fd
            os.close(descriptor);replacement=os.open(foreign,os.O_RDWR)
            self.assertEqual(replacement,descriptor)
            self.refuse(lambda:self.capture(lease).__enter__())
        self.assertTrue(os.fstat(replacement));os.close(replacement)
        self.assertEqual(foreign.read_text(),'untouched')

    def test_reused_guard_descriptor_is_not_touched_and_original_lock_releases(self):
        other=self.other_broker();foreign=self.base/'foreign';foreign.write_text('untouched');foreign.chmod(0o600)
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();descriptor=lease._guard
            os.close(descriptor);replacement=os.open(foreign,os.O_RDWR)
            self.assertEqual(replacement,descriptor)
            fcntl.flock(replacement,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))
        self.assertTrue(os.fstat(replacement))
        probe=os.open(foreign,os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            os.close(probe);fcntl.flock(replacement,fcntl.LOCK_UN);os.close(replacement)
        with other.journal.lock():pass

    def test_replaced_lock_path_permissions_and_links_are_refused(self):
        for change in ('replacement','symlink','hardlink','permissions'):
            with self.subTest(change=change),self.journal_object.lock():
                lease=self.journal_object.held_lock_lease();path=self.journal/'broker.lock'
                if change=='permissions':path.chmod(0o644)
                elif change=='hardlink':os.link(path,self.journal/'alias.lock')
                else:
                    path.rename(self.journal/'old.lock')
                    if change=='replacement':path.write_bytes(b'');path.chmod(0o600)
                    else:path.symlink_to(self.journal/'old.lock')
                try:self.refuse(lambda:self.journal_object.require_lock_lease(lease))
                finally:
                    if change=='permissions':path.chmod(0o600)
                    elif change=='hardlink':(self.journal/'alias.lock').unlink()
                    else:path.unlink();(self.journal/'old.lock').rename(path)

    def test_changed_ancestor_directory_identity_is_refused(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();old=self.base.with_name(self.base.name+'-old')
            self.base.rename(old);self.base.mkdir(mode=0o700)
            # Preserve the same journal inode through its new ancestor.
            (old/'broker').rename(self.base/'broker')
            try:self.refuse(lambda:self.journal_object.require_lock_lease(lease))
            finally:
                (self.base/'broker').rename(old/'broker');self.base.rmdir();old.rename(self.base)

    def test_manifest_fence_content_and_root_checks_remain_in_force(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            with self.assertRaises(DevelopmentError):
                with self.capture(lease) as snapshot:
                    (self.root/'right.txt').write_text('changed\n');snapshot.revalidate()
        (self.root/'right.txt').write_text('22\n')
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            self.refuse(lambda:self.capture(lease,binding={**self.binding,'configuration_hash':'0'*64}).__enter__())
        original=(self.journal/'manifest.json').read_bytes()
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            value=json.loads(original);value['resource']='other'
            (self.journal/'manifest.json').write_bytes(canonical(value))
            try:self.refuse(lambda:self.capture(lease).__enter__())
            finally:(self.journal/'manifest.json').write_bytes(original)

    def test_snapshot_cannot_survive_its_outer_lock_or_revive(self):
        outer=self.journal_object.lock();outer.__enter__();lease=self.journal_object.held_lock_lease()
        capture=self.capture(lease);snapshot=capture.__enter__()
        outer.__exit__(None,None,None)
        self.refuse(snapshot.document)
        with self.journal_object.lock():self.refuse(snapshot.revalidate)
        self.refuse(lambda:capture.__exit__(None,None,None))

    def test_plain_journal_lock_never_creates_a_lease_or_dup(self):
        with patch('os.dup',side_effect=AssertionError('ordinary lock must not duplicate')):
            with self.journal_object.lock() as state:
                self.assertIs(type(state),dict);self.assertIsNone(self.journal_object._lease)

    def test_lock_lost_then_reacquired_does_not_revive_the_lease(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            fcntl.flock(self.journal_object._lock_fd,fcntl.LOCK_UN)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))
            fcntl.flock(self.journal_object._lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))

    def test_shared_downgrade_is_refused_without_reacquiring_exclusive(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            fcntl.flock(self.journal_object._lock_fd,fcntl.LOCK_SH)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))
            probe=os.open(self.journal/'broker.lock',os.O_RDONLY)
            try:fcntl.flock(probe,fcntl.LOCK_SH|fcntl.LOCK_NB)
            finally:fcntl.flock(probe,fcntl.LOCK_UN);os.close(probe)
            # The fixture restores its own lock only for known-fd cleanup.
            fcntl.flock(self.journal_object._lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))

    def test_closed_journal_invalidates_lease_without_second_unlock(self):
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease()
            self.journal_object.close()
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))

    def test_lock_descriptor_pin_precedes_lease_issuance(self):
        foreign=self.base/'foreign';foreign.write_text('private fixture');foreign.chmod(0o600)
        with self.journal_object.lock():
            descriptor=self.journal_object._lock_fd
            os.close(descriptor);replacement=os.open(foreign,os.O_RDWR)
            self.assertEqual(descriptor,replacement)
            fcntl.flock(replacement,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.refuse(self.journal_object.held_lock_lease)
        # Cleanup did not close or unlock this now-foreign descriptor.
        self.assertTrue(os.fstat(replacement))
        probe=os.open(foreign,os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):fcntl.flock(probe,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:
            os.close(probe);fcntl.flock(replacement,fcntl.LOCK_UN);os.close(replacement)

    def test_ancestor_permissions_are_part_of_lease_identity(self):
        mode=self.base.stat().st_mode&0o7777
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();self.base.chmod(0o755)
            try:self.refuse(lambda:self.journal_object.require_lock_lease(lease))
            finally:self.base.chmod(mode)

    def test_reused_private_directory_descriptor_is_not_closed(self):
        foreign=self.base/'foreign-directory';foreign.mkdir(mode=0o700)
        with self.journal_object.lock():
            lease=self.journal_object.held_lock_lease();descriptor=self.journal_object.private.fd
            os.close(descriptor);replacement=os.open(foreign,os.O_RDONLY|os.O_DIRECTORY)
            self.assertEqual(replacement,descriptor)
            self.refuse(lambda:self.journal_object.require_lock_lease(lease))
        self.assertIsNone(self.journal_object.private.fd)
        self.journal_object.close()
        self.assertTrue(os.fstat(replacement));os.close(replacement)

    def test_competitor_release_cannot_make_old_lease_reacquire(self):
        journal = self.journal_object
        observed = self.lock_observation.observe
        competitor = None
        with journal.lock():
            lease = journal.held_lock_lease()
            fcntl.flock(journal._lock_fd, fcntl.LOCK_UN)
            competitor = os.open(self.journal/'broker.lock', os.O_RDWR | os.O_NOFOLLOW)
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            released = []
            def observation(descriptor, **arguments):
                if descriptor == lease._guard and not released:
                    # A different OFD held EX in the gap and now releases.
                    self.lock_observation.flock(competitor, fcntl.LOCK_UN)
                    self.lock_observation.locks.pop(self.lock_observation.handles[competitor])
                    released.append(True)
                return observed(descriptor, **arguments)
            with patch('blackoutburst_agents.dev_journal._linux_lock_observation', side_effect=observation), \
                 patch('fcntl.flock', side_effect=AssertionError('validation must never acquire')):
                self.refuse(lambda: journal.require_lock_lease(lease))
                self.refuse(lambda: self.capture(lease).__enter__())
                self.assertFalse(lease._active)
        self.assertEqual(released, [True])
        os.close(competitor)

    def test_observation_and_cleanup_never_acquire_or_probe(self):
        journal = self.journal_object
        original = fcntl.flock
        with journal.lock():
            lease = journal.held_lock_lease()
            def only_unlock(descriptor, operation):
                self.assertEqual(operation, fcntl.LOCK_UN)
                return original(descriptor, operation)
            with patch('fcntl.flock', side_effect=only_unlock):
                self.assertTrue(journal.require_lock_lease(lease))
                lease._revoke()
            self.refuse(lambda: journal.require_lock_lease(lease))

    def test_missing_observation_invalidates_permanently_without_unknown_cleanup(self):
        journal = self.journal_object
        with journal.lock():
            lease = journal.held_lock_lease()
            with patch('blackoutburst_agents.dev_journal._linux_lock_observation', side_effect=OSError('unavailable')), \
                 patch('fcntl.flock', side_effect=AssertionError('no ownership, no unlock')):
                self.refuse(lambda: journal.require_lock_lease(lease))
                lease._revoke()
            self.refuse(lambda: journal.require_lock_lease(lease))

    def test_darwin_ordinary_journal_needs_no_observation_or_lease(self):
        journal = self.journal_object
        with patch('blackoutburst_agents.dev_journal._linux_lock_supported', return_value=False), \
             patch('blackoutburst_agents.dev_journal._linux_lock_observation', side_effect=AssertionError('unsupported')):
            with journal.lock() as state:
                self.assertIs(type(state), dict)
                self.refuse(journal.held_lock_lease)
                self.assertIsNone(journal._lease)
            with journal.lock():pass

    def test_observed_mount_or_flags_change_permanently_invalidates_lease(self):
        for changed in [(2, int('0100002',8)), (1, int('0100002',8) | os.O_CLOEXEC)]:
            with self.subTest(changed=changed), self.journal_object.lock():
                lease = self.journal_object.held_lock_lease()
                with patch('blackoutburst_agents.dev_journal._linux_lock_observation', return_value=changed):
                    self.refuse(lambda: self.journal_object.require_lock_lease(lease))
                self.refuse(lambda: self.journal_object.require_lock_lease(lease))

    def test_original_reopened_same_inode_before_issuance_cannot_borrow_guard(self):
        with self.journal_object.lock():
            descriptor = self.journal_object._lock_fd
            os.close(descriptor)
            replacement = os.open(self.journal/'broker.lock', os.O_RDWR | os.O_NOFOLLOW)
            self.assertEqual(descriptor, replacement)
            self.refuse(self.journal_object.held_lock_lease)
        self.assertTrue(os.fstat(replacement))
        os.close(replacement)
