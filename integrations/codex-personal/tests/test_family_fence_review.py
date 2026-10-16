"""Independent global-fence races and interrupted writes; metadata only."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.dev_fence import DevelopmentFence
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_planner import _directory_pin
from blackoutburst_agents.dev_runtime import development_work_id
import test_family_fence as transfer_fixture


SLOTS = ('child_0', 'child_1', 'parent')


class FamilyFenceReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.scope = dict(owner_id='owner', agent_id='agent', project_id='project', ticket_id='parent-ticket',
                          board_endpoint='https://board.test/mcp', board_session_id='root-session')
        roots, stores, scopes, pins = {}, {}, {}, {}
        for slot in SLOTS:
            path = self.base / slot
            path.mkdir(mode=0o700)
            roots[slot] = _directory_pin(path)
            stores[slot] = {}
            for kind in ('runs', 'runtime'):
                path = self.base / (slot + '-' + kind)
                path.mkdir(mode=0o700)
                stores[slot][kind] = _directory_pin(path)
            pins[slot] = {}
            for kind in ('journal', 'broker_directory'):
                path = self.base / (slot + '-' + kind)
                path.mkdir(mode=0o700)
                pins[slot][kind] = _directory_pin(path)
            scopes[slot] = {**self.scope, 'board_session_id': 'session-' + slot} if slot != 'parent' else dict(self.scope)
        configuration = {'scope': self.scope, 'roots': roots, 'child_stores': stores,
                         'provisional_scopes': scopes, 'pins': pins}
        operation = str(uuid.uuid4())
        workflows = {slot: str(uuid.uuid5(uuid.UUID(operation), slot)) for slot in SLOTS}
        directory = self.base / 'family-journal'
        directory.mkdir(mode=0o700)
        journal = _directory_pin(directory)
        fingerprint = digest(canonical(configuration))
        self.descriptor = {'schema': 1, 'operation_id': operation, 'configuration_sha256': fingerprint,
                           'journal': journal, 'roots': roots,
                           'sessions': {slot: scopes[slot]['board_session_id'] for slot in SLOTS}}
        self.reservations = {
            slot: {'scope': scopes[slot], 'root_identity': roots[slot]['identity'], 'journal': journal,
                   'configuration_hash': fingerprint,
                   'child_scope': {'work_id': development_work_id(workflows[slot]), **stores[slot]}}
            for slot in SLOTS}
        state = {'schema': 1, 'operation_id': operation, 'workflow_ids': workflows, 'configuration': configuration,
                 'configuration_sha256': fingerprint, 'revision': 1, 'started_at': 1234.0,
                 'phase': 'prepared', 'fence_ready': False,
                 'payload': {'fence_descriptor': self.descriptor, 'fence_reservations': self.reservations,
                             'fence_targets': {}}}
        with PrivateDirectory(directory) as private:
            private.create('family.json', canonical(state))
        self.anchor = self.base / 'anchor'
        self.fence = DevelopmentFence(_test_anchor=self.anchor)
        self.addCleanup(self.fence.close)

    def state(self):
        return decode(self.fence.private.read('fence.json', 2 * 1024 * 1024))

    def ordinary(self):
        directory = self.base / 'ordinary'
        directory.mkdir(mode=0o700, exist_ok=True)
        return {**deepcopy(self.reservations['child_0']), 'journal': _directory_pin(directory),
                'configuration_hash': 'b' * 64}

    def test_existing_ordinary_root_prevents_all_three_family_reservations(self):
        binding, operation = self.ordinary(), str(uuid.uuid4())
        self.fence.bind(binding, operation)
        before = self.state()
        with self.assertRaises(DevelopmentError):
            self.fence.reserve_family(self.reservations, self.descriptor)
        self.assertEqual(self.state(), before)

    def test_family_prevents_generic_binding_even_with_its_own_uuid_and_binding(self):
        self.fence.reserve_family(self.reservations, self.descriptor)
        before = self.state()
        for slot, binding in self.reservations.items():
            operation = str(uuid.uuid5(uuid.UUID(self.descriptor['operation_id']), slot))
            with self.subTest(slot=slot), self.assertRaises(DevelopmentError):
                self.fence.bind(binding, operation)
        self.assertEqual(self.state(), before)

    def test_concurrent_generic_and_family_admission_has_only_one_winner(self):
        other = DevelopmentFence(_test_anchor=self.anchor)
        self.addCleanup(other.close)
        binding, operation = self.ordinary(), str(uuid.uuid4())
        ready = threading.Barrier(2)
        def reserve_family():
            ready.wait(timeout=5)
            try:
                self.fence.reserve_family(self.reservations, self.descriptor)
                return 'family'
            except DevelopmentError:
                return 'refused'
        def reserve_ordinary():
            ready.wait(timeout=5)
            try:
                other.bind(binding, operation)
                return 'ordinary'
            except DevelopmentError:
                return 'refused'
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(reserve_family), pool.submit(reserve_ordinary)
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual(results.count('refused'), 1)
        records = self.state()['records']
        self.assertEqual(len(records), 3 if 'family' in results else 1)
        self.assertEqual(sum('family' in record for record in records.values()),
                         3 if 'family' in results else 0)

    def test_write_failure_leaves_no_partial_family_and_exact_retry_uses_same_ids(self):
        before = self.state()
        with patch.object(self.fence.private, 'replace', side_effect=DevelopmentError('private_write_failed')):
            with self.assertRaises(DevelopmentError):
                self.fence.reserve_family(self.reservations, self.descriptor)
        self.assertEqual(self.state(), before)
        self.fence.reserve_family(self.reservations, self.descriptor)
        after = self.state()
        self.fence.reserve_family(self.reservations, self.descriptor)
        self.assertEqual(self.state(), after)
        self.assertEqual(len(after['records']), 3)

    def test_lost_success_after_atomic_write_does_not_create_replacement_reservations(self):
        original = self.fence.private.replace
        def lost(name, content):
            original(name, content)
            raise DevelopmentError('synthetic_lost_write_confirmation')
        with patch.object(self.fence.private, 'replace', side_effect=lost):
            with self.assertRaises(DevelopmentError):
                self.fence.reserve_family(self.reservations, self.descriptor)
        committed = self.state()
        self.assertEqual(len(committed['records']), 3)
        self.fence.reserve_family(self.reservations, self.descriptor)
        self.assertEqual(self.state(), committed)


class FamilyTransferReviewTests(unittest.TestCase):
    setUp = transfer_fixture.FamilyFenceTests.setUp
    raw = transfer_fixture.FamilyFenceTests.raw
    reserve = transfer_fixture.FamilyFenceTests.reserve
    verify = transfer_fixture.FamilyFenceTests.verify

    def test_lost_parent_transfer_retries_exact_same_target_without_new_rows(self):
        self.reserve()
        parent = self.fixture.target()
        original = self.fence.private.replace
        def lost(name, content):
            original(name, content)
            raise DevelopmentError('synthetic_lost_transfer_confirmation')
        with patch.object(self.fence.private, 'replace', side_effect=lost):
            with self.assertRaises(DevelopmentError):
                self.fence.transfer_family_slot(parent)
        committed = self.raw()
        self.assertEqual(self.fence.transfer_family_slot(parent), (parent['workflow_id'], False))
        self.assertEqual(self.raw(), committed)
        self.verify()

    def test_lost_planner_attach_remains_exact_and_cannot_revert_to_parent(self):
        self.reserve()
        parent = self.fixture.target()
        self.fence.transfer_family_slot(parent)
        planner = self.fixture.target(stage='planner')
        original = self.fence.private.replace
        def lost(name, content):
            original(name, content)
            raise DevelopmentError('synthetic_lost_attach_confirmation')
        with patch.object(self.fence.private, 'replace', side_effect=lost):
            with self.assertRaises(DevelopmentError):
                self.fence.attach(planner['binding'], planner['workflow_id'], parent['binding'], planner)
        committed = self.raw()
        self.assertEqual(self.fence.attach(planner['binding'], planner['workflow_id'], parent['binding'], planner),
                         (planner['workflow_id'], False))
        with self.assertRaises(DevelopmentError):
            self.fence.transfer_family_slot(parent)
        self.assertEqual(self.raw(), committed)
        self.verify()

    def test_missing_private_target_cannot_be_recovered_by_adopting_fence_record(self):
        self.reserve()
        before_target = deepcopy(self.fixture.state)
        parent = self.fixture.target()
        self.fence.transfer_family_slot(parent)
        committed = self.raw()
        self.fixture.state = before_target
        self.fixture.save()
        with self.assertRaises(DevelopmentError):
            self.verify()
        with self.assertRaises(DevelopmentError):
            self.fence.transfer_family_slot(parent)
        self.assertEqual(self.raw(), committed)

    def test_changed_private_intent_before_transfer_write_keeps_reserved_prefix(self):
        self.reserve()
        parent = self.fixture.target()
        before = self.raw()
        original = self.fence._save
        def changed(state, current):
            self.fixture.state['phase'] = 'needs_review'
            self.fixture.save()
            return original(state, current)
        with patch.object(self.fence, '_save', side_effect=changed):
            with self.assertRaises(DevelopmentError):
                self.fence.transfer_family_slot(parent)
        self.assertEqual(self.raw(), before)
        self.verify()


if __name__ == '__main__':
    unittest.main()
