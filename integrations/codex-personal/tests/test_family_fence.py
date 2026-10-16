"""Family fence metadata/effects on disposable directories, no provider/process."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch

from blackoutburst_agents.dev_fence import DevelopmentFence, FAMILY_SLOTS
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical, digest
from blackoutburst_agents.dev_runtime import development_work_id


def pin(path):
    info = path.stat()
    return {'path': str(path), 'identity': [info.st_dev, info.st_ino]}


class FamilyFixture:
    """A complete private intent without importing the separate driver lot."""
    def __init__(self, base, name='one', anchor=None):
        self.base = Path(base) / name
        self.base.mkdir(mode=0o700)
        self.journal = self.directory('family')
        self.anchor = Path(anchor) if anchor is not None else Path(base) / 'anchor'
        self.operation = str(uuid.uuid4())
        self.scope = dict(owner_id='owner', agent_id='agent', project_id='project', ticket_id='parent-ticket',
            board_session_id=name+'-parent-session', board_endpoint='https://board.test/mcp')
        self.workflows = {slot: str(uuid.uuid5(uuid.UUID(self.operation), slot)) for slot in FAMILY_SLOTS}
        config = {'scope': self.scope, 'roots': {}, 'child_stores': {}, 'pins': {}, 'provisional_scopes': {}}
        for slot in FAMILY_SLOTS:
            config['roots'][slot] = pin(self.directory(slot+'-root'))
            config['child_stores'][slot] = {key: pin(self.directory(slot+'-'+key)) for key in ('runs', 'runtime')}
            config['pins'][slot] = {key: pin(self.directory(slot+'-'+key)) for key in ('journal', 'broker_directory')}
            config['provisional_scopes'][slot] = {**self.scope, 'board_session_id': name+'-'+slot+'-session' if slot != 'parent' else self.scope['board_session_id']}
        self.state = {'schema': 1, 'operation_id': self.operation, 'workflow_ids': self.workflows,
            'configuration': config, 'configuration_sha256': digest(canonical(config)),
            'revision': 1, 'started_at': 1.0, 'phase': 'prepared', 'payload': {}, 'fence_ready': False}
        self.rebuild()

    def directory(self, name):
        value = self.base / name
        value.mkdir(mode=0o700)
        return value

    def rebuild(self):
        config = self.state['configuration']
        self.state['configuration_sha256'] = digest(canonical(config))
        self.descriptor = {'schema': 1, 'operation_id': self.operation,
            'configuration_sha256': self.state['configuration_sha256'], 'journal': pin(self.journal),
            'roots': deepcopy(config['roots']),
            'sessions': {slot: config['provisional_scopes'][slot]['board_session_id'] for slot in FAMILY_SLOTS}}
        self.reservations = {slot: {'scope': deepcopy(config['provisional_scopes'][slot]),
            'root_identity': config['roots'][slot]['identity'], 'journal': pin(self.journal),
            'configuration_hash': self.state['configuration_sha256'],
            'child_scope': {'work_id': development_work_id(self.workflows[slot]), **config['child_stores'][slot]}}
            for slot in FAMILY_SLOTS}
        self.state['payload'] = {'fence_descriptor': deepcopy(self.descriptor),
            'fence_reservations': deepcopy(self.reservations), 'fence_targets': {}}
        self.save()

    def save(self):
        self.state['revision'] += 1
        path = self.journal / 'family.json'
        path.write_bytes(canonical(self.state)); path.chmod(0o600)

    def target(self, slot='child_0', stage='parent'):
        config = self.state['configuration']
        payload = self.state['payload']
        payload['family_id'] = 'server-family'
        payload.setdefault('slot_scopes', {name: {**config['provisional_scopes'][name],
            'ticket_id': self.scope['ticket_id'] if name == 'parent' else name+'-ticket'} for name in FAMILY_SLOTS})
        if stage == 'parent':
            parent_scope = {'operation_id': self.workflows[slot], 'journal': config['pins'][slot]['journal'], 'configuration_hash': 'a'*64}
            binding = {**deepcopy(self.reservations[slot]), 'scope': deepcopy(payload['slot_scopes'][slot]),
                'journal': deepcopy(config['pins'][slot]['broker_directory']), 'configuration_hash': 'a'*64,
                'parent_scope': parent_scope}
        else:
            binding = {**deepcopy(payload['fence_targets'][slot]['parent']['binding']), 'configuration_hash': 'b'*64}
        proof = {'descriptor': deepcopy(self.descriptor), 'slot': slot, 'workflow_id': self.workflows[slot],
            'server_family_id': payload['family_id'], 'stage': stage, 'binding': binding}
        payload['fence_targets'].setdefault(slot, {})[stage] = deepcopy(proof)
        self.save()
        return proof


class FamilyFenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.fixture = FamilyFixture(self.base)
        self.fence = DevelopmentFence(_test_anchor=self.fixture.anchor)
        self.addCleanup(self.fence.close)

    def raw(self):
        return (self.fixture.anchor/'fence.json').read_bytes()

    def reserve(self):
        return self.fence.reserve_family(self.fixture.reservations, self.fixture.descriptor)

    def verify(self):
        self.fence.verify_family(self.fixture.reservations, self.fixture.descriptor,
            self.fixture.state['payload']['fence_targets'])

    def test_all_three_reserved_once_and_generic_calls_refuse(self):
        self.assertEqual(self.reserve(), (self.fixture.operation, True))
        self.assertEqual(self.reserve(), (self.fixture.operation, False))
        state = json.loads(self.raw())
        self.assertEqual(state['schema'], 3)
        self.assertEqual(len(state['records']), 3)
        self.verify()
        for slot, binding in self.fixture.reservations.items():
            with self.subTest(slot=slot):
                with self.assertRaises(DevelopmentError):
                    self.fence.bind(binding, self.fixture.workflows[slot])
                with self.assertRaises(DevelopmentError):
                    self.fence.verify(binding, self.fixture.workflows[slot])
                with self.assertRaises(DevelopmentError):
                    self.fence.attach(binding, self.fixture.workflows[slot], binding)

    def test_parent_then_planner_transfer_exact_and_retryable(self):
        self.reserve()
        parent = self.fixture.target()
        self.verify()  # A durable target may precede the atomic transfer.
        self.assertEqual(self.fence.transfer_family_slot(parent), (parent['workflow_id'], True))
        self.assertEqual(self.fence.transfer_family_slot(parent), (parent['workflow_id'], False))
        self.assertEqual(self.reserve(), (self.fixture.operation, False))
        planner = self.fixture.target(stage='planner')
        self.verify()
        self.assertEqual(self.fence.attach(planner['binding'], planner['workflow_id'], parent['binding'], planner), (planner['workflow_id'], True))
        self.assertEqual(self.fence.attach(planner['binding'], planner['workflow_id'], parent['binding'], planner), (planner['workflow_id'], False))
        self.verify()
        with self.assertRaises(DevelopmentError):
            self.fence.transfer_family_slot(parent)
        self.assertEqual(len(json.loads(self.raw())['records']), 3)

    def test_arguments_must_be_written_in_private_document_before_effect(self):
        before = self.raw()
        changed = deepcopy(self.fixture.reservations)
        changed['child_0']['scope']['owner_id'] = 'other'
        with self.assertRaises(DevelopmentError):
            self.fence.reserve_family(changed, self.fixture.descriptor)
        self.assertEqual(self.raw(), before)
        self.reserve()
        proof = self.fixture.target()
        before = self.raw()
        changed = deepcopy(proof); changed['binding']['configuration_hash'] = 'c'*64
        with self.assertRaises(DevelopmentError):
            self.fence.transfer_family_slot(changed)
        with self.assertRaises(DevelopmentError):
            self.fence.verify_family(self.fixture.reservations, self.fixture.descriptor, {})
        self.assertEqual(self.raw(), before)

    def test_sealed_target_cannot_change_origin_owner_profile_project_or_session(self):
        self.reserve()
        original = deepcopy(self.fixture.state)
        for key in ('owner_id', 'agent_id', 'project_id', 'board_session_id', 'board_endpoint'):
            with self.subTest(key=key):
                self.fixture.state = deepcopy(original)
                proof = self.fixture.target()
                value = 'https://other.test/mcp' if key == 'board_endpoint' else 'other'
                self.fixture.state['payload']['slot_scopes']['child_0'][key] = value
                proof['binding']['scope'][key] = value
                self.fixture.state['payload']['fence_targets']['child_0']['parent'] = proof
                self.fixture.save()
                before = self.raw()
                with self.assertRaises(DevelopmentError):
                    self.fence.transfer_family_slot(proof)
                self.assertEqual(self.raw(), before)

    def test_journal_store_work_uuid_and_parent_pin_are_not_adopted(self):
        self.reserve()
        original = deepcopy(self.fixture.state)
        for key in ('journal', 'child_scope', 'parent_scope'):
            with self.subTest(key=key):
                self.fixture.state = deepcopy(original)
                proof = self.fixture.target()
                if key == 'journal': proof['binding'][key] = pin(self.fixture.journal)
                elif key == 'child_scope': proof['binding'][key]['work_id'] = str(uuid.uuid4())
                else: proof['binding'][key]['journal'] = pin(self.fixture.journal)
                self.fixture.state['payload']['fence_targets']['child_0']['parent'] = proof
                self.fixture.save(); before = self.raw()
                with self.assertRaises(DevelopmentError): self.fence.transfer_family_slot(proof)
                self.assertEqual(self.raw(), before)

    def test_provisional_scopes_only_change_declared_sessions(self):
        for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id', 'board_endpoint'):
            with self.subTest(key=key):
                fixture = FamilyFixture(self.base, name=key, anchor=self.fixture.anchor)
                fixture.state['configuration']['provisional_scopes']['child_0'][key] = 'https://other.test/mcp' if key == 'board_endpoint' else 'other'
                fixture.rebuild(); before = self.raw()
                with self.assertRaises(DevelopmentError): self.fence.reserve_family(fixture.reservations, fixture.descriptor)
                self.assertEqual(self.raw(), before)

    def test_private_pins_must_stay_outside_all_sibling_roots(self):
        config = self.fixture.state['configuration']
        sibling = Path(config['roots']['child_1']['path'])/'private-journal'; sibling.mkdir(mode=0o700)
        config['pins']['child_0']['journal'] = pin(sibling)
        self.fixture.rebuild(); before = self.raw()
        with self.assertRaises(DevelopmentError): self.reserve()
        self.assertEqual(self.raw(), before)

    def test_missing_or_replaced_current_root_and_journal_fail_without_mutation(self):
        self.reserve(); before = self.raw()
        root = Path(self.fixture.descriptor['roots']['child_0']['path'])
        root.rename(root.with_name('old-root')); root.mkdir(mode=0o700)
        with self.assertRaises(DevelopmentError): self.verify()
        self.assertEqual(self.raw(), before)

    def test_unrelated_deleted_historical_roots_are_metadata_only_and_schema_never_downgrades(self):
        other = FamilyFixture(self.base, name='history', anchor=self.fixture.anchor)
        binding = other.reservations['parent']; identifier = str(uuid.uuid4())
        self.fence.bind(binding, identifier)
        root = Path(other.descriptor['roots']['parent']['path']); root.rmdir()
        self.reserve(); self.verify()
        newest = FamilyFixture(self.base, name='ordinary', anchor=self.fixture.anchor)
        ordinary = newest.reservations['parent']; operation = str(uuid.uuid4())
        ordinary['parent_scope'] = {'operation_id': operation, 'journal': ordinary['journal'], 'configuration_hash': ordinary['configuration_hash']}
        self.fence.bind(ordinary, operation)
        final = {**ordinary, 'configuration_hash': 'f'*64}
        self.fence.attach(final, operation, ordinary)
        self.assertEqual(json.loads(self.raw())['schema'], 3)

    def test_no_partial_reservation_when_one_root_busy(self):
        selected = self.fixture.reservations['child_1']
        self.fence.bind(selected, str(uuid.uuid4())); before = self.raw()
        with self.assertRaises(DevelopmentError): self.reserve()
        self.assertEqual(self.raw(), before)

    def test_private_document_changed_before_replace_refuses_all_mutation(self):
        before = self.raw()
        original = self.fence._save
        def changed(state, current):
            self.fixture.state['phase'] = 'opening'; self.fixture.save()
            return original(state, current)
        with patch.object(self.fence, '_save', side_effect=changed):
            with self.assertRaises(DevelopmentError): self.reserve()
        self.assertEqual(self.raw(), before)

    def test_empty_anchor_cannot_reconstruct_an_activated_or_open_family(self):
        initial = deepcopy(self.fixture.state)
        for field, value in (('fence_ready', True), ('phase', 'opening'), ('family_id', 'server-family'),
                             ('slot_scopes', {})):
            with self.subTest(field=field):
                self.fixture.state = deepcopy(initial)
                if field in ('family_id', 'slot_scopes'):
                    self.fixture.state['payload'][field] = value
                else:
                    self.fixture.state[field] = value
                self.fixture.save(); before = self.raw()
                with self.assertRaises(DevelopmentError): self.reserve()
                self.assertEqual(self.raw(), before)

    def test_partial_existing_family_is_never_completed(self):
        self.reserve()
        state = json.loads(self.raw())
        state['records'].pop(next(iter(state['records'])))
        (self.fixture.anchor/'fence.json').write_bytes(canonical(state))
        before = self.raw()
        with self.assertRaises(DevelopmentError): self.reserve()
        self.assertEqual(self.raw(), before)

    def test_symlink_family_document_and_replaced_family_directory_are_refused(self):
        source = self.fixture.journal/'family.json'
        saved = self.fixture.journal/'original.json'; source.rename(saved); source.symlink_to(saved)
        before = self.raw()
        with self.assertRaises(DevelopmentError): self.reserve()
        self.assertEqual(self.raw(), before)
        source.unlink(); saved.rename(source)
        self.fixture.journal.rename(self.fixture.journal.with_name('old-family'))
        self.fixture.journal.mkdir(mode=0o700)
        (self.fixture.journal/'family.json').write_bytes(canonical(self.fixture.state))
        (self.fixture.journal/'family.json').chmod(0o600)
        with self.assertRaises(DevelopmentError): self.reserve()
        self.assertEqual(self.raw(), before)
