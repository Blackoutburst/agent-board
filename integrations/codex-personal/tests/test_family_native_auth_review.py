"""Independent live inspection regressions; real temporary Board, no native/model."""
from copy import deepcopy
import unittest
from unittest.mock import patch

import test_family_native_authorization as fixture
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical
from blackoutburst_agents.family_audit import manifest_document
from blackoutburst_agents.family_plan import build_configuration


class FamilyNativeInspectionReviewTests(unittest.TestCase):
    setUp = fixture.FamilyNativeAuthorizationTests.setUp
    make_manifest = fixture.FamilyNativeAuthorizationTests.make_manifest
    ready = fixture.FamilyNativeAuthorizationTests.ready
    transport = fixture.FamilyNativeAuthorizationTests.transport
    ticket = fixture.FamilyNativeAuthorizationTests.ticket
    assert_no_execution = fixture.FamilyNativeAuthorizationTests.assert_no_execution

    def callback(self, slot='child_0'):
        self.ready()
        state = self.journal.read()
        config = state['configuration']
        manifest, _ = manifest_document(self.items[slot]['broker_directory'])
        callback = self.worker._qualification_authorizer(config, slot, state)
        self.assertTrue(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()
        return callback, manifest, state

    def test_every_scope_component_and_type_is_exact(self):
        callback, manifest, _ = self.callback()
        replacements = {'owner_id': 'other-owner', 'agent_id': 'other-profile',
            'device_id': 'other-device', 'session_id': 'other-session',
            'project_id': 'other-project', 'ticket_id': 'other-ticket',
            'profile_version': manifest['scope']['profile_version'] + 1}
        for key, value in replacements.items():
            with self.subTest(field=key):
                self.assertFalse(callback({**manifest['scope'], key: value}, 'repo.run_test', manifest['resource']))
        self.assertFalse(callback({**manifest['scope'], 'profile_version': True}, 'repo.run_test', manifest['resource']))
        self.assertFalse(callback({**manifest['scope'], 'extra': 'field'}, 'repo.run_test', manifest['resource']))
        self.assertFalse(callback(dict(list(manifest['scope'].items())[1:]), 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_preopening_authorizer_cannot_be_reused_after_open(self):
        config = build_configuration(self.request, self.workflows, self.registrations, clock=lambda: self.now)
        callback = self.worker._qualification_authorizer(config, 'parent')
        manifest, _ = manifest_document(self.items['parent']['broker_directory'])
        self.assertTrue(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.worker.prepare(self.request)
        self.worker.open()
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_captured_callback_rechecks_manifest_caps(self):
        callback, manifest, _ = self.callback()
        changed = deepcopy(manifest)
        changed['files']['right.py'] = deepcopy(changed['files']['left.py'])
        with PrivateDirectory(self.items['child_0']['broker_directory']) as directory:
            directory.replace('manifest.json', canonical(changed))
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_captured_callback_rechecks_actual_root_identity(self):
        callback, manifest, _ = self.callback()
        root = self.roots['child_0']
        root.rename(self.base / 'previous-child-root')
        root.mkdir(mode=0o700)
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_parent_source_edit_revokes_child_inspection(self):
        callback, manifest, _ = self.callback()
        parent = self.ticket()
        # The UI blocks edits to an in-progress parent with unresolved children;
        # model a changed persisted source without altering claim or dependencies.
        with self.board._tx() as db:
            db.execute('UPDATE tickets SET description=?,version=version+1 WHERE id=?',
                       ('Changed human instruction', parent['id']))
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_profile_revision_revokes_captured_inspection_without_reregistration(self):
        callback, manifest, _ = self.callback()
        profile = self.registrations['parent']['profile']
        self.board.update_agent_profile(self.human, profile['id'], profile['version'], instructions='Changed profile')
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_deadline_and_stop_revoke_captured_inspection(self):
        callback, manifest, state = self.callback()
        original = self.now
        self.now = state['configuration']['opening']['limits']['deadline'] + 1
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.now = original
        self.journal.request_stop()
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_device_revocation_refuses_captured_inspection(self):
        callback, manifest, _ = self.callback()
        self.board.revoke_device(self.human, manifest['scope']['device_id'])
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_grants_revoked_after_bind_refuse_before_child_workflow_claim(self):
        callback, manifest, _ = self.callback()
        self.board.set_policy(self.human, True, True, True, True, ['repo.read_file'], ['repo:fixture'])
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        result = self.worker.run()
        self.assertEqual(result['status'], 'needs_review', result)
        self.assertFalse(self.journal.read()['payload'].get('workflow_intents'))
        self.assert_no_execution()

    def test_captured_child_inspection_cannot_adopt_another_slot(self):
        callback, manifest, state = self.callback()
        state['payload']['slot_scopes']['child_0'] = deepcopy(state['payload']['slot_scopes']['child_1'])
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()

    def test_inspected_backend_cannot_execute_during_or_after_yield(self):
        self.ready()
        state = self.journal.read()
        config = state['configuration']
        with self.worker._qualification_resources(config, 'child_0', state) as (document, policy, backend):
            manifest = document.manifest
            self.assertTrue(backend.document['plan_hash'])
            self.assertTrue(policy.fingerprint)
            self.assertFalse(backend.qualified_for(document.manifest_hash, manifest['tests'][0]))
            with self.assertRaises(DevelopmentError):
                backend.run(None)
            callback = backend.authorize_scope
        self.assertFalse(callback(manifest['scope'], 'repo.run_test', manifest['resource']))
        with self.assertRaises(DevelopmentError):
            backend.run(None)
        self.assert_no_execution()

    def test_failed_inspection_irreversibly_closes_its_authorizer(self):
        self.ready()
        state = self.journal.read()
        config = state['configuration']
        backend = self.backend['child_0']
        with patch.object(backend, 'qualified_for', return_value=False), self.assertRaises(DevelopmentError):
            with self.worker._qualification_resources(config, 'child_0', state):
                self.fail('Unqualified inspection must not yield.')
        manifest, _ = manifest_document(self.items['child_0']['broker_directory'])
        self.assertFalse(backend.authorize_scope(manifest['scope'], 'repo.run_test', manifest['resource']))
        self.assert_no_execution()


if __name__ == '__main__':
    unittest.main()
