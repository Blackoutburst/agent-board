"""Actual Board/Git journals, synthetic Linux/model/GitHub observations only."""
import json
import unittest
from unittest.mock import patch

import test_development_delivery as delivery_fixture
from blackoutburst_agents.development_git_authority import DevelopmentGitPublicationAuthority
from blackoutburst_agents.development_delivery import DevelopmentPublishedDelivery
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.git_publication import GitHubDraftPublisher


@unittest.skipIf(delivery_fixture.fixture.Board is None,'Board sources not bundled')
class DevelopmentGitAuthorityTests(unittest.TestCase):
    def fixture(self):
        f = delivery_fixture.DevelopmentDeliveryTests(); f.setUp(); self.addCleanup(f.doCleanups)
        # Use the real sealed fixture and its closed local command transport,
        # stopping immediately before its first publication call.
        with patch.object(GitHubDraftPublisher,'publish',return_value=None): f.published()
        old = f.publisher
        f.authority = DevelopmentGitPublicationAuthority(f.worker,f.operation)
        f.publisher = GitHubDraftPublisher(f.deliveries,old.root,grant=old._grant,
            gh_executable=old.gh,python_executable=old.python,authority=f.authority)
        f.publisher._command = old._command
        f.pushes = 0; command = f.publisher._command
        def tracked(argv,**options):
            if options.get('git') and argv[1]=='push': f.pushes += 1
            return command(argv,**options)
        f.publisher._command = tracked
        return f

    @staticmethod
    def publish(f):
        return f.publisher.publish(operation_id=f.operation,delivery_sha=f.seal['delivery_sha'],
            title='Synthetic delivery',body='Selected controller summary.')

    def test_guarded_push_and_post_publish_and_descriptor_survives_board_publication(self):
        f = self.fixture(); pin = f.authority.descriptor(); result = self.publish(f)
        self.assertEqual(result['status'],'complete'); self.assertEqual((f.pushes,f.posts),(1,1))
        record = json.loads((f.publisher.root/f.operation/'publication.json').read_bytes())
        self.assertEqual(record['request']['authority'],pin)
        link = DevelopmentPublishedDelivery(f.proof,f.deliveries,f.publisher)
        self.assertEqual(f.worker.publish(f.operation,delivery_publication=link)['stage'],'complete')
        self.assertEqual(f.authority.descriptor(),pin)
        self.assertEqual(link.verify(f.worker,f.operation,f.scope)['delivery_sha'],f.seal['delivery_sha'])
        self.assertEqual(self.publish(f),result); self.assertEqual((f.pushes,f.posts),(1,1))

    def test_stop_or_revocation_after_last_remote_reads_blocks_each_effect(self):
        for effect in ('push','post'):
            for change in ('stop','revoke'):
                with self.subTest(effect=effect,change=change):
                    f = self.fixture(); before = f.board.get_ticket(f.human,f.ticket['id'])['ticket']
                    command = f.publisher._command; fired = []
                    def interrupt(argv,**options):
                        result = command(argv,**options)
                        path = f.publisher.root/f.operation
                        if options.get('git') and argv[1]=='ls-remote' and not fired:
                            record = json.loads((path/'publication.json').read_bytes())
                            final = (record['phase']=='prepared' if effect=='push' else
                                     record['phase']=='branch_confirmed' and (path/'create-pr.json').exists())
                            if final:
                                fired.append(True)
                                if change=='stop': f.worker.stop(f.operation)
                                else: f.board.set_policy(f.human,True,True,True,True,[],[])
                        return result
                    f.publisher._command = interrupt
                    with self.assertRaises(DevelopmentError): self.publish(f)
                    self.assertEqual(fired,[True]); self.assertEqual(f.posts,0)
                    self.assertEqual(f.pushes,0 if effect=='push' else 1)
                    self.assertEqual(f.board.get_ticket(f.human,f.ticket['id'])['ticket'],before)
                    record = json.loads((f.publisher.root/f.operation/'publication.json').read_bytes())
                    self.assertIn(record['phase'],('prepared','pushing') if effect=='push' else ('branch_confirmed','creating'))

    def test_new_human_context_after_last_get_is_not_publication_authority(self):
        f = self.fixture(); command = f.publisher._command; fired = []
        def change(argv,**options):
            result = command(argv,**options)
            if options.get('git') and argv[1]=='ls-remote' and not fired:
                fired.append(True); f.board.post_message(f.human,f.ticket['thread_id'],'Changed decision',idempotency_key='new-decision')
            return result
        f.publisher._command = change
        with self.assertRaises(DevelopmentError): self.publish(f)
        self.assertEqual((f.pushes,f.posts),(0,0))

    def test_authority_cannot_be_added_removed_or_replaced_on_retry(self):
        f = self.fixture(); result = self.publish(f)
        old = f.publisher
        f.publisher = GitHubDraftPublisher(f.deliveries,old.root,grant=old._grant,
            gh_executable=old.gh,python_executable=old.python)
        f.publisher._command = old._command
        with self.assertRaises(DevelopmentError): self.publish(f)
        self.assertEqual((f.pushes,f.posts),(1,1))
        with self.assertRaises(DevelopmentError):
            GitHubDraftPublisher(f.deliveries,old.root,grant=old._grant,
                gh_executable=old.gh,python_executable=old.python,authority=lambda:True)

    def test_standalone_intent_cannot_gain_authority_on_cold_retry(self):
        f = self.fixture(); guarded = f.publisher
        f.publisher = GitHubDraftPublisher(f.deliveries,guarded.root,grant=guarded._grant,
            gh_executable=guarded.gh,python_executable=guarded.python)
        f.publisher._command = guarded._command
        self.assertEqual(self.publish(f)['status'],'complete')
        f.publisher = guarded
        with self.assertRaises(DevelopmentError): self.publish(f)
        self.assertEqual((f.pushes,f.posts),(1,1))

    def test_revocation_during_authority_context_is_reread_before_effect(self):
        f = self.fixture(); call = f.client.call; fired = []
        def revoke(name,args):
            result = call(name,args)
            if name=='get_thread' and not fired:
                fired.append(True); f.board.set_policy(f.human,True,True,True,True,[],[])
            return result
        f.client.call = revoke
        with self.assertRaises(DevelopmentError): self.publish(f)
        self.assertEqual(fired,[True]); self.assertEqual((f.pushes,f.posts),(0,0))

    def test_missing_or_foreign_proof_cannot_use_a_real_board_authority(self):
        f = self.fixture(); old = f.publisher; f.deliveries.test_guard = lambda **_:True
        with self.assertRaises(DevelopmentError):
            GitHubDraftPublisher(f.deliveries,old.root,grant=old._grant,
                gh_executable=old.gh,python_executable=old.python,authority=f.authority)

    def test_local_stop_marker_is_not_written_or_repaired_by_authority(self):
        f = self.fixture(); before = {p.name:p.read_bytes() for p in f.journal.directory.iterdir() if p.is_file()}
        self.assertIsInstance(f.authority.descriptor(),dict)
        after = {p.name:p.read_bytes() for p in f.journal.directory.iterdir() if p.is_file()}
        self.assertEqual(before,after)
