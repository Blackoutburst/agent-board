"""Real temporary Board/Git journals; Linux/model/GitHub observations synthetic."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest

import test_development_delivery as delivery_fixture
from blackoutburst_agents.development_review_handoff import DevelopmentReviewHandoff
from blackoutburst_agents.dev_journal import PrivateDirectory, decode
from blackoutburst_agents.dev_manifest import DevelopmentError, canonical


class Crash(BaseException): pass


@unittest.skipIf(delivery_fixture.fixture.Board is None,'Board sources not bundled')
class DevelopmentReviewHandoffTests(unittest.TestCase):
    def fixture(self, *, assigned=True, before=None):
        f = delivery_fixture.DevelopmentDeliveryTests(); f.setUp(); self.addCleanup(f.doCleanups)
        if before is not None: before(f)
        if assigned:
            f.ticket = self.ticket(f)
            f.ticket = f.board.update_ticket(f.human,f.ticket['id'],f.ticket['version'],assignee_agent_id=f.scope['agent_id'])
        f.published()
        result = f.worker.publish(f.operation,delivery_publication=f.link)
        self.assertEqual(result['stage'],'complete',result)
        f.handoff = DevelopmentReviewHandoff(f.worker,f.link)
        return f

    @staticmethod
    def ticket(f): return f.board.get_ticket(f.human,f.ticket['id'])['ticket']

    @staticmethod
    def calls(f): return [args for name,args in f.client.calls if name=='release_ticket']

    @staticmethod
    def origin(f):
        with f.board._connection() as db:
            return f.board._review_origin(db,f.ticket['id'])

    def test_exact_delivery_self_unassigns_once_preserving_review_origin_and_evidence(self):
        f = self.fixture(); before = self.ticket(f); origin = self.origin(f)
        posts, updates = f.posts, len([1 for name,_ in f.client.calls if name=='update_ticket'])
        result = f.handoff.release(f.operation); after = self.ticket(f)
        self.assertEqual(result['status'],'complete'); self.assertEqual(result['result']['version'],before['version']+1)
        self.assertIsNone(after['assignee_agent_id']); self.assertIsNone(after['claimant'])
        for field in ('status','outcome','evidence','progress','depends_on','human_requests'):
            self.assertEqual(before[field],after[field])
        self.assertEqual(self.origin(f),origin); self.assertEqual(f.posts,posts)
        self.assertEqual(len([1 for name,_ in f.client.calls if name=='update_ticket']),updates)
        self.assertEqual(len(self.calls(f)),1)
        self.assertEqual(self.calls(f)[0]['idempotency_key'],'development-review-handoff-'+f.operation)
        self.assertEqual(f.handoff.release(f.operation),result); self.assertEqual(len(self.calls(f)),1)
        self.assertEqual(f.handoff.status(f.operation),result)
        # Ordinary Board review claiming by a distinct identity is now possible;
        # the handoff itself never creates or chooses this reviewer.
        device = f.board.create_device(f.human,'Independent reviewer',[])
        raw = f.board.authenticate(device['token'])
        registration = f.board.register_agent(raw,'review-session','Reviewer',[],'codex',False,'reviewer')
        actor = f.board.actor_session(raw,'review-session')
        claimed = f.board.claim_ticket(actor,after['id'],after['version'])
        self.assertEqual(claimed['claimant']['agent_id'],registration['identity']['id'])
        self.assertNotEqual(claimed['claimant']['agent_id'],f.scope['agent_id'])
        self.assertEqual(f.handoff.release(f.operation),result)
        self.assertEqual(self.ticket(f),claimed)

    def test_existing_completed_dependency_is_not_mistaken_for_a_blocker(self):
        def setup(f):
            dependency = f.board.create_ticket(f.human,f.project['id'],'Completed prerequisite',idempotency_key='dependency')
            dependency = f.board.update_ticket(f.human,dependency['id'],dependency['version'],status='done',outcome='Complete',evidence='Synthetic test')
            f.board.update_ticket(f.human,f.ticket['id'],f.ticket['version'],depends_on=[dependency['id']])
        f = self.fixture(before=setup)
        self.assertTrue(self.ticket(f)['depends_on']); self.assertFalse(self.ticket(f)['blocked_by'])
        self.assertEqual(f.handoff.release(f.operation)['status'],'complete')

    def test_current_dependency_blocker_refuses_even_without_ticket_version_change(self):
        def setup(f):
            dependency = f.board.create_ticket(f.human,f.project['id'],'Completed prerequisite',idempotency_key='dependency')
            f.dependency = f.board.update_ticket(f.human,dependency['id'],dependency['version'],status='done',outcome='Complete',evidence='Synthetic test')
            f.board.update_ticket(f.human,f.ticket['id'],f.ticket['version'],depends_on=[dependency['id']])
        f = self.fixture(before=setup)
        f.board.update_ticket(f.human,f.dependency['id'],f.dependency['version'],status='todo')
        before = self.ticket(f); self.assertTrue(before['blocked_by'])
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f)); self.assertEqual(self.ticket(f),before)

    def test_never_adopts_unassigned_ticket_without_its_own_intent(self):
        f = self.fixture(); before = self.ticket(f)
        changed = f.board.update_ticket(f.human,before['id'],before['version'],assignee_agent_id=None)
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertEqual(self.ticket(f),changed); self.assertFalse(self.calls(f))
        self.assertEqual(f.handoff.status(f.operation)['status'],'not_started')

    def test_lost_response_requires_exact_same_key_acknowledgement(self):
        f = self.fixture(); call = f.client.call; sent = []
        def lose(name,args):
            result = call(name,args)
            if name=='release_ticket': sent.append(deepcopy(args)); raise TimeoutError('PRIVATE_TRANSPORT')
            return result
        f.client.call = lose
        with self.assertRaises(DevelopmentError) as caught: f.handoff.release(f.operation)
        self.assertEqual(caught.exception.code,'development_review_handoff_unconfirmed')
        post = self.ticket(f); self.assertIsNone(post['assignee_agent_id'])
        self.assertEqual(f.handoff.status(f.operation)['status'],'releasing')
        f.client.call = call
        cold = DevelopmentReviewHandoff(f.worker,f.link)
        self.assertEqual(cold.release(f.operation)['status'],'complete')
        self.assertEqual(self.calls(f),[sent[0],sent[0]])
        self.assertEqual(self.ticket(f),post)

    def test_crash_before_rpc_preserves_intent_and_uses_same_id(self):
        f = self.fixture(); before = self.ticket(f); call = f.client.call; pending = []
        def crash(name,args):
            if name=='release_ticket': pending.append(deepcopy(args)); raise Crash()
            return call(name,args)
        f.client.call = crash
        with self.assertRaises(Crash): f.handoff.release(f.operation)
        self.assertEqual(self.ticket(f),before); self.assertFalse(self.calls(f))
        path = f.journal.directory/('review-handoff-'+f.operation+'.json')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        record = decode(path.read_bytes()); self.assertEqual(record['request']['rpc'],pending[0])
        self.assertNotIn(before['evidence'],path.read_text())
        f.client.call = call
        self.assertEqual(DevelopmentReviewHandoff(f.worker,f.link).release(f.operation)['status'],'complete')
        self.assertEqual(self.calls(f),pending)

    def test_identical_unassignment_without_server_key_is_not_idempotent_confirmation(self):
        f = self.fixture(); call = f.client.call
        def interrupt(name,args):
            if name=='release_ticket': raise Crash()
            return call(name,args)
        f.client.call = interrupt
        with self.assertRaises(Crash): f.handoff.release(f.operation)
        before = self.ticket(f)
        # A normal human reassignment additionally posts a notice and already
        # fails the context fence. Here synthesize the harder identical remote
        # post-state, with NO matching server idempotency record.
        with sqlite3.connect(f.board.db_path) as db:
            db.execute('UPDATE tickets SET assignee_agent_id=NULL,version=version+1 WHERE id=?',(before['id'],))
        changed = self.ticket(f)
        f.client.call = call
        with self.assertRaises(DevelopmentError) as caught: f.handoff.release(f.operation)
        self.assertEqual(caught.exception.code,'development_review_handoff_unconfirmed')
        self.assertEqual(self.ticket(f),changed); self.assertEqual(len(self.calls(f)),1)
        self.assertEqual(f.handoff.status(f.operation)['status'],'releasing')

    def test_maintenance_accepts_removed_execution_grants(self):
        f = self.fixture()
        f.board.set_policy(f.human,True,True,True,True,[],[])
        self.assertEqual(f.handoff.release(f.operation)['status'],'complete')

    def test_missing_author_assignment_refuses(self):
        f = self.fixture(assigned=False)
        with self.assertRaises(DevelopmentError) as caught: f.handoff.release(f.operation)
        self.assertEqual(caught.exception.code,'development_review_handoff_author_assignment_required')
        self.assertFalse(self.calls(f))

    def test_claim_even_expired_cannot_be_cleared_by_handoff(self):
        f = self.fixture(); before = self.ticket(f)
        # Simulate an external session's claim with unchanged semantic version;
        # the full claimant check, not only the context projection, must fence it.
        with sqlite3.connect(f.board.db_path) as db:
            db.execute('UPDATE tickets SET claimant=?,lease_expires_at=0 WHERE id=?',
                (json.dumps({**f.client.actor,'session_id':'foreign-session'}),before['id']))
        changed = self.ticket(f)
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertEqual(self.ticket(f),changed); self.assertFalse(self.calls(f))

    def test_omitted_claim_fields_never_mean_an_unclaimed_ticket(self):
        for field in ('claimant','lease_expires_at'):
            with self.subTest(field=field):
                f = self.fixture(); call = f.client.call; before = self.ticket(f)
                def incomplete(name,args):
                    value = call(name,args)
                    if name=='get_ticket': value['ticket'].pop(field)
                    return value
                f.client.call = incomplete
                with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
                self.assertFalse(self.calls(f)); self.assertEqual(self.ticket(f),before)

    def test_remote_text_version_and_assignment_mutations_refuse(self):
        for changes in ({'evidence':'Changed evidence'},{'outcome':'Changed outcome'},
                        {'title':'Changed title'},{'assignee_agent_id':None}):
            with self.subTest(changes=changes):
                f = self.fixture(); before = self.ticket(f)
                changed = f.board.update_ticket(f.human,before['id'],before['version'],**changes)
                with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
                self.assertEqual(self.ticket(f),changed); self.assertFalse(self.calls(f))

    def test_context_discussion_change_refuses_without_new_message(self):
        f = self.fixture(); before = self.ticket(f)
        ids = [m['id'] for m in f.board.get_thread(f.human,before['thread_id'])['messages']]
        message = f.board.post_message(f.human,before['thread_id'],'New human decision',idempotency_key='decision')
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f))
        messages = f.board.get_thread(f.human,before['thread_id'])['messages']
        self.assertEqual([m['id'] for m in messages],ids+[message['id']])

    def test_revocation_during_last_github_lookup_prevents_release(self):
        f = self.fixture(); before = self.ticket(f); target = f.lookups+2
        def revoke():
            if f.lookups == target: f.board.set_policy(f.human,True,True,True,False,[],[])
        f.during_lookup = revoke
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f)); self.assertEqual(self.ticket(f),before)
        self.assertEqual(f.handoff.status(f.operation)['status'],'not_started')

    def test_revocation_during_context_read_is_reread_before_rpc(self):
        f = self.fixture(); before = self.ticket(f); call = f.client.call; fired = []
        def revoke(name,args):
            result = call(name,args)
            if name=='get_thread' and not fired:
                fired.append(True); f.board.set_policy(f.human,True,True,True,False,[],[])
            return result
        f.client.call = revoke
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f)); self.assertEqual(self.ticket(f),before)

    def test_modified_pr_or_checkout_prevents_any_handoff(self):
        for change in ('pr','checkout'):
            with self.subTest(change=change):
                f = self.fixture(); before = self.ticket(f)
                if change=='pr': f.prs[0]['draft']=False
                else: (f.root/'main.py').write_text('unverified = True\n')
                with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
                self.assertFalse(self.calls(f)); self.assertEqual(self.ticket(f),before)
                self.assertEqual(f.posts,1)

    def test_stop_during_context_read_prevents_release(self):
        f = self.fixture(); before = self.ticket(f); call = f.client.call; fired = []
        def stop(name,args):
            result = call(name,args)
            if name=='get_thread' and not fired:
                fired.append(True); f.worker.stop(f.operation)
            return result
        f.client.call = stop
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f)); self.assertEqual(self.ticket(f),before)

    def test_unconfirmed_release_cannot_adopt_later_reviewer_claim(self):
        f = self.fixture(); call = f.client.call; f.client.lost.add('release_ticket')
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        after = self.ticket(f)
        device = f.board.create_device(f.human,'Independent reviewer',[])
        raw = f.board.authenticate(device['token'])
        f.board.register_agent(raw,'review-session','Reviewer',[],'codex',False,'reviewer')
        actor = f.board.actor_session(raw,'review-session')
        claimed = f.board.claim_ticket(actor,after['id'],after['version'])
        with self.assertRaises(DevelopmentError): DevelopmentReviewHandoff(f.worker,f.link).release(f.operation)
        self.assertEqual(self.ticket(f),claimed); self.assertEqual(len(self.calls(f)),1)
        self.assertEqual(f.handoff.status(f.operation)['status'],'releasing')

    def test_intent_mutation_during_authority_read_is_never_repaired(self):
        f = self.fixture(); call = f.client.call
        def crash(name,args):
            if name=='release_ticket': raise Crash()
            return call(name,args)
        f.client.call = crash
        with self.assertRaises(Crash): f.handoff.release(f.operation)
        f.client.call = call; path = f.journal.directory/('review-handoff-'+f.operation+'.json'); changed = []
        def tamper():
            if not changed:
                value = decode(path.read_bytes()); value['request']['rpc']['idempotency_key']='different'
                with PrivateDirectory(path.parent) as private: private.replace(path.name,canonical(value))
                changed.append(path.read_bytes())
        f.during_lookup = tamper
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f)); self.assertEqual(path.read_bytes(),changed[0])

    def test_incomplete_or_arbitrary_publication_cannot_enable_handoff(self):
        f = self.fixture()
        with self.assertRaises(DevelopmentError): DevelopmentReviewHandoff(f.worker,{'url':'https://example.invalid'})
        op = f.journal.get(f.operation)
        with f.journal.lock(op['scope']): f.worker._save(op,publication_confirmed=False)
        with self.assertRaises(DevelopmentError): f.handoff.release(f.operation)
        self.assertFalse(self.calls(f))
