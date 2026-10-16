"""Scoped audit reads and cached proof validation; all execution is simulated."""
import unittest

import test_development_worker as fixture
from blackoutburst_agents.family_audit import audit_workflow,manifest_document
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from blackoutburst_agents.dev_journal import PrivateDirectory,decode


class FamilyAuditTests(unittest.TestCase):
    setUp=fixture.DevelopmentWorkerTests.setUp
    backend_factory=fixture.DevelopmentWorkerTests.backend_factory
    make_worker=fixture.DevelopmentWorkerTests.make_worker
    prepare=fixture.DevelopmentWorkerTests.prepare
    transport=fixture.DevelopmentWorkerTests.transport
    action=fixture.DevelopmentWorkerTests.action
    successful_actions=fixture.DevelopmentWorkerTests.successful_actions

    def ready(self):
        self.successful_actions();prepared=self.prepare()
        self.assertEqual(self.worker.run(prepared['operation_id'])['status'],'review_ready')
        _,manifest_hash=manifest_document(self.broker_dir)
        return prepared['operation_id'],manifest_hash

    def test_audit_current_hashes_candidates_tests_and_exact_cleanup(self):
        operation,manifest=self.ready()
        audit=audit_workflow(self.worker,operation,manifest_hash=manifest,authorize=lambda:True)
        self.assertEqual(audit['candidates'][0]['content'],'value = 2\n')
        self.assertEqual(audit['audit']['tests'],['unit'])
        self.assertTrue(audit['audit']['cleanup_receipts_sha256'])
        self.assertNotIn('value =',canonical(audit['audit']).decode())

    def test_changed_source_after_dated_review_refuses_acceptance(self):
        operation,manifest=self.ready();(self.root/'main.py').write_text('value = 3\n')
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker,operation,manifest_hash=manifest,authorize=lambda:True)

    def test_unexpected_parent_integration_file_hashes_refuse(self):
        operation,manifest=self.ready()
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker,operation,manifest_hash=manifest,authorize=lambda:True,
                           expected_hashes={'main.py':'0'*64})

    def test_cached_test_receipt_cannot_be_replaced_by_evidence_text(self):
        operation,manifest=self.ready()
        with PrivateDirectory(self.broker_dir) as private:
            state=decode(private.read('state.json',2*1024*1024))
            for entry in state['operations'].values():
                if entry['kind']=='test':entry['receipt']['result']='failed'
            private.replace('state.json',canonical(state))
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker,operation,manifest_hash=manifest,authorize=lambda:True)

    def test_revoked_authority_refuses_audit_before_source_read(self):
        operation,manifest=self.ready()
        with self.assertRaises(DevelopmentError):
            audit_workflow(self.worker,operation,manifest_hash=manifest,authorize=lambda:False)


if __name__=='__main__':unittest.main()
