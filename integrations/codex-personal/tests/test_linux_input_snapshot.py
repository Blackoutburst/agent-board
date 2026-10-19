"""Filesystem fixtures only: no process, model, network, or service launch."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.dev_manifest import (DevelopmentError, RepositoryFiles,
                                               canonical, digest, prepare_manifest)
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.linux_input_snapshot import (capture_input_snapshot,
    validate_input_snapshot, validate_planner_snapshot)


class LinuxInputSnapshotTests(unittest.TestCase):
    def setUp(self):
        from linux_lock_fixture import LockObservationFixture
        self.lock_observation = LockObservationFixture(self)
        temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.root = self.base / 'checkout'; self.root.mkdir(mode=0o700)
        (self.root / 'data').mkdir(mode=0o700)
        (self.root / 'data/left.txt').write_text('20\n')
        (self.root / 'right.txt').write_text('22\n')
        self.scope = dict(owner_id='owner', agent_id='agent', device_id='device', session_id='session',
                          project_id='project', ticket_id='ticket', profile_version=1)
        self.journal = self.base / 'broker'
        prepare_manifest(self.journal, root=str(self.root), base_sha='a' * 40,
                         scope=self.scope, paths=['data/left.txt', 'right.txt'],
                         resource='repo:fixture', authorize_scope=lambda *args: True)
        # A later remote-capable callback must never be used by this helper.
        self.authorize = unittest.mock.Mock(side_effect=AssertionError('authorization callback invoked'))
        self.broker = DevelopmentBroker(self.journal, authorize_scope=self.authorize, enabled=True)
        self.addCleanup(self.broker.close)
        self.fence = DevelopmentFence(_test_anchor=self.base / 'anchor')
        self.addCleanup(self.fence.close)
        self.operation = str(uuid.uuid4())
        pin = {'path': str(self.journal), 'identity': list(self.broker.journal.private.identity)}
        self.binding = {'scope': dict(owner_id='owner', agent_id='agent', project_id='project',
            ticket_id='ticket', board_session_id='session', board_endpoint='https://board.test/mcp'),
            'root_identity': self.broker.manifest['root_identity'], 'journal': pin,
            'configuration_hash': 'b' * 64,
            'child_scope': {'work_id': str(uuid.uuid4()), 'runs': pin, 'runtime': pin}}
        self.fence.bind(self.binding, self.operation)
        self.planner = DevelopmentPlanner._snapshot({'files': []}, self.broker.manifest)

    def capture(self, **changes):
        args = dict(fence=self.fence, binding=self.binding,
                    operation_id=self.operation, planner_snapshot=self.planner)
        args.update(changes)
        return capture_input_snapshot(self.broker, **args)

    def assertRefused(self, action, code=None):
        with self.assertRaises(DevelopmentError) as caught:
            action()
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_exact_planner_projection_and_private_copies(self):
        with patch('subprocess.Popen', side_effect=AssertionError('process launch')):
            with self.capture() as snapshot:
                self.assertEqual(snapshot.read('data/left.txt'), b'20\n')
                self.assertEqual(snapshot.read('right.txt'), b'22\n')
                document = snapshot.document()
                self.assertEqual(document['planner_snapshot'], self.planner)
                self.assertNotIn('contents', document)
                self.assertEqual(document['operation_id'], self.operation)
                self.assertTrue(snapshot.revalidate())
                document['files']['right.txt']['sha256'] = '0' * 64
                self.assertNotEqual(document, snapshot.document())
                self.assertRefused(lambda: snapshot.read('unapproved.txt'), 'linux_input_path_not_allowed')
            self.assertRefused(snapshot.document, 'linux_input_snapshot_not_held')
            self.assertRefused(lambda: snapshot.read('right.txt'), 'linux_input_snapshot_not_held')
        self.authorize.assert_not_called()
        self.assertFalse(self.broker.journal._in_lock)

    def test_actual_broker_lock_is_held_and_released_after_error(self):
        second = DevelopmentBroker(self.journal, authorize_scope=lambda *args: True, enabled=True)
        self.addCleanup(second.close)
        with self.capture():
            self.assertTrue(self.broker.journal._in_lock)
            self.assertRefused(lambda: second.journal.lock().__enter__(), 'broker_busy')
        with self.assertRaisesRegex(RuntimeError, 'fixture'):
            with self.capture():
                raise RuntimeError('fixture')
        self.assertFalse(self.broker.journal._in_lock)
        with self.capture():
            pass

    def test_fake_controller_and_fence_are_not_lock_proof(self):
        self.assertRefused(lambda: capture_input_snapshot(object(), fence=self.fence, binding=self.binding,
            operation_id=self.operation, planner_snapshot=self.planner).__enter__(), 'linux_input_controller_required')
        self.assertRefused(lambda: self.capture(fence=object()).__enter__(), 'linux_input_controller_required')
        self.broker.enabled = False
        self.assertRefused(lambda: self.capture().__enter__(), 'linux_input_controller_required')

    def test_fence_operation_binding_scope_and_journal_must_match(self):
        self.assertRefused(lambda: self.capture(operation_id=str(uuid.uuid4())).__enter__(), 'linux_input_fence_mismatch')
        for key, changed in [('scope', {**self.binding['scope'], 'owner_id': 'another'}),
                             ('journal', {**self.binding['journal'], 'identity': [0, 1]}),
                             ('configuration_hash', 'c' * 64)]:
            binding = {**self.binding, key: changed}
            self.assertRefused(lambda: self.capture(binding=binding).__enter__(), 'linux_input_fence_mismatch')

    def test_unreserved_checkout_is_refused(self):
        other = DevelopmentFence(_test_anchor=self.base / 'empty-anchor'); self.addCleanup(other.close)
        self.assertRefused(lambda: self.capture(fence=other).__enter__(), 'linux_input_fence_mismatch')

    def test_changed_content_during_context_cannot_be_accepted(self):
        with self.assertRaises(DevelopmentError):
            with self.capture() as snapshot:
                (self.root / 'right.txt').write_text('23\n')
                snapshot.revalidate()
        self.assertFalse(self.broker.journal._in_lock)

    def test_exit_revalidates_even_without_explicit_check(self):
        with self.assertRaises(DevelopmentError):
            with self.capture():
                (self.root / 'right.txt').write_text('23\n')

    def test_same_content_new_inode_is_refused(self):
        with self.assertRaises(DevelopmentError):
            with self.capture() as snapshot:
                source = self.root / 'right.txt'
                source.rename(self.root / 'old.txt')
                source.write_text('22\n')
                snapshot.revalidate()

    def test_same_content_rewrite_changes_signature(self):
        with self.assertRaises(DevelopmentError):
            with self.capture() as snapshot:
                (self.root / 'right.txt').write_text('22\n')
                snapshot.revalidate()

    def test_parent_and_root_substitution_are_refused(self):
        for root in (False, True):
            with self.subTest(root=root):
                with self.assertRaises(DevelopmentError):
                    with self.capture() as snapshot:
                        target = self.root if root else self.root / 'data'
                        old = target.with_name(target.name + '-old')
                        target.rename(old); target.mkdir(mode=0o700)
                        if root:
                            (target / 'data').mkdir(mode=0o700)
                            (target / 'right.txt').write_text('22\n')
                        (target / ('data/left.txt' if root else 'left.txt')).write_text('20\n')
                        try:
                            snapshot.revalidate()
                        finally:
                            import shutil
                            shutil.rmtree(target); old.rename(target)

    def test_symlink_and_hardlink_inputs_are_refused(self):
        target = self.root / 'right.txt'
        target.rename(self.root / 'real.txt')
        target.symlink_to(self.root / 'real.txt')
        self.assertRefused(lambda: self.capture().__enter__())
        target.unlink(); os.link(self.root / 'real.txt', target)
        self.assertRefused(lambda: self.capture().__enter__())

    def test_symlink_parent_is_refused(self):
        parent = self.root / 'data'; parent.rename(self.root / 'real-data')
        parent.symlink_to(self.root / 'real-data', target_is_directory=True)
        self.assertRefused(lambda: self.capture().__enter__())

    def test_ancestor_permissions_change_is_refused(self):
        with self.assertRaises(DevelopmentError):
            with self.capture() as snapshot:
                (self.root / 'data').chmod(0o777)
                snapshot.revalidate()

    def test_mutation_between_capture_passes_is_refused(self):
        original = RepositoryFiles.read_at
        calls = 0
        def read(parent, name, maximum):
            nonlocal calls
            data = original(parent, name, maximum)
            calls += 1
            if calls == 2:
                (self.root / 'data/left.txt').write_text('21\n')
            return data
        with patch.object(RepositoryFiles, 'read_at', side_effect=read):
            self.assertRefused(lambda: self.capture().__enter__())
        self.assertLessEqual(calls, 4)

    def test_planner_hash_must_be_recomputed_from_exact_allowed_bytes(self):
        for changes in ({'sha256': '0' * 64}, {'files': {'right.txt': digest(b'22\n')}},
                        {'files': {**self.planner['files'], 'foreign.txt': 'a' * 64}},
                        {'files': {**self.planner['files'], 'right.txt': True}}, {'extra': True}):
            value = {**self.planner, **changes}
            self.assertRefused(lambda: self.capture(planner_snapshot=value).__enter__())
        files = {**self.planner['files'], 'right.txt': digest(b'wrong\n')}
        value = {'files': files, 'sha256': digest(canonical(files))}
        self.assertRefused(lambda: self.capture(planner_snapshot=value).__enter__(), 'linux_input_planner_snapshot_mismatch')

    def test_current_patched_inputs_can_match_new_planner_snapshot(self):
        (self.root / 'right.txt').write_text('23\n')
        current = DevelopmentPlanner._snapshot({'files': [{'path': 'right.txt',
            'after_sha256': digest(b'23\n')}]}, self.broker.manifest)
        with self.capture(planner_snapshot=current) as snapshot:
            self.assertEqual(snapshot.read('right.txt'), b'23\n')
            self.assertEqual(snapshot.document()['planner_snapshot'], current)

    def test_strict_metadata_types_and_keys(self):
        with self.capture() as snapshot:
            original = snapshot.document()
        mutations = [lambda value: value.update(schema=True),
            lambda value: value.update(extra=True), lambda value: value.update(operation_id=17),
            lambda value: value['limits'].update(max_files=True),
            lambda value: value['files']['right.txt'].update(bytes=True),
            lambda value: value['files']['right.txt']['signature'].__setitem__(5, True),
            lambda value: value['files']['right.txt']['signature'].__setitem__(1, 2.0),
            lambda value: value['files']['right.txt']['parents'][0].update(uid=True),
            lambda value: value['root_chain'][0].update(mode=True),
            lambda value: value['files']['right.txt'].update(extra=True)]
        for mutation in mutations:
            value = copy.deepcopy(original); mutation(value)
            with self.subTest(mutation=mutation):
                self.assertRefused(lambda: validate_input_snapshot(value,
                    manifest=self.broker.manifest, planner_snapshot=self.planner))

    def test_file_and_total_size_bounds(self):
        (self.root / 'right.txt').write_bytes(b'x' * (65536 + 1))
        self.assertRefused(lambda: self.capture().__enter__(), 'file_too_large')
        (self.root / 'right.txt').write_text('22\n')
        with patch('blackoutburst_agents.linux_input_snapshot.MAX_TOTAL_BYTES', 5):
            self.assertRefused(lambda: self.capture().__enter__(), 'linux_input_snapshot_too_large')

    def test_broker_lock_replacement_and_manifest_change_are_refused(self):
        for kind in ('lock', 'manifest'):
            with self.subTest(kind=kind):
                with self.assertRaises(DevelopmentError):
                    with self.capture() as snapshot:
                        path = self.journal / ('broker.lock' if kind == 'lock' else 'manifest.json')
                        saved = path.read_bytes()
                        if kind == 'lock':
                            path.rename(self.journal / 'old-lock')
                            path.write_bytes(b''); path.chmod(0o600)
                        else:
                            changed = json.loads(saved); changed['resource'] = 'repo:changed'
                            path.write_bytes(canonical(changed))
                        try:
                            snapshot.revalidate()
                        finally:
                            if kind == 'lock':
                                path.unlink(); (self.journal / 'old-lock').rename(path)
                            else:
                                path.write_bytes(saved)

    def test_no_snapshot_or_file_content_is_written(self):
        before = {str(path.relative_to(self.base)): path.read_bytes()
                  for path in self.base.rglob('*') if path.is_file()}
        with self.capture() as snapshot:
            snapshot.document()
            snapshot.read('right.txt')
        after = {str(path.relative_to(self.base)): path.read_bytes()
                 for path in self.base.rglob('*') if path.is_file()}
        self.assertEqual(before, after)

    def test_fifo_and_world_writable_input_are_refused(self):
        source = self.root / 'right.txt'; source.unlink()
        os.mkfifo(source, mode=0o600)
        self.assertRefused(lambda: self.capture().__enter__(), 'file_type_not_allowed')
        source.unlink(); source.write_text('22\n'); source.chmod(0o666)
        self.assertRefused(lambda: self.capture().__enter__(), 'file_type_not_allowed')

    def test_cross_thread_use_cannot_borrow_the_lock(self):
        import threading
        errors = []
        with self.capture() as snapshot:
            def use():
                try:
                    snapshot.read('right.txt')
                except DevelopmentError as error:
                    errors.append(error.code)
            thread = threading.Thread(target=use); thread.start(); thread.join()
        self.assertEqual(errors, ['linux_input_snapshot_not_held'])

    def test_casefold_alias_and_depth_bounds_are_refused_before_read(self):
        manifest = self.broker.manifest
        entry = manifest['files'].pop('right.txt')
        manifest['files']['DATA/LEFT.TXT'] = entry
        self.assertRefused(lambda: validate_planner_snapshot(self.planner, manifest), 'linux_input_alias_refused')
        manifest = self.broker.manifest
        manifest['files']['/'.join(['d'] * 33) + '/file.txt'] = entry
        self.assertRefused(lambda: validate_planner_snapshot(self.planner, manifest), 'linux_input_depth_exceeded')

    def test_noncanonical_root_and_surrogate_paths_are_refused(self):
        for root in (str(self.root) + '/', str(self.root).replace('/private/', '//private/'),
                     str(self.root) + '/./child', str(self.root) + '/\ud800'):
            manifest = self.broker.manifest; manifest['root'] = root
            self.assertRefused(lambda: validate_planner_snapshot(self.planner, manifest))

    def test_changed_serialized_inventory_cannot_be_a_valid_snapshot(self):
        with self.capture() as snapshot:
            original = snapshot.document()
        for mutate in (lambda value: value['files']['right.txt'].update(bytes=4000000),
                       lambda value: value['files']['right.txt']['signature'].__setitem__(5, 2),
                       lambda value: value['files']['right.txt']['signature'].__setitem__(7, 0o120600),
                       lambda value: value['files']['right.txt']['signature'].__setitem__(6, os.geteuid() + 1),
                       lambda value: value['files']['right.txt']['parents'][0].update(path='/elsewhere'),
                       lambda value: value['root_chain'][0].update(identity=[-1, 2]),
                       lambda value: value['files']['right.txt'].update(sha256='0' * 64)):
            value = copy.deepcopy(original); mutate(value)
            self.assertRefused(lambda: validate_input_snapshot(value,
                manifest=self.broker.manifest, planner_snapshot=self.planner))

    def test_directory_aliases_and_inconsistent_shared_pins_are_refused(self):
        with self.capture() as snapshot:
            original = snapshot.document()
        value = copy.deepcopy(original)
        value['files']['data/left.txt']['parents'][1]['identity'] = value['root_chain'][0]['identity']
        self.assertRefused(lambda: validate_input_snapshot(value,
            manifest=self.broker.manifest, planner_snapshot=self.planner), 'linux_input_alias_refused')
        value = copy.deepcopy(original)
        value['files']['right.txt']['parents'][0]['identity'][1] += 1
        self.assertRefused(lambda: validate_input_snapshot(value,
            manifest=self.broker.manifest, planner_snapshot=self.planner), 'linux_input_alias_refused')

    def test_observed_change_permanently_invalidates_the_copy(self):
        with self.assertRaises(DevelopmentError):
            with self.capture() as snapshot:
                (self.root / 'right.txt').write_text('23\n')
                self.assertRefused(snapshot.revalidate)
                self.assertRefused(lambda: snapshot.read('right.txt'), 'linux_input_snapshot_not_held')


if __name__ == '__main__':
    unittest.main()
