"""Private queue durability and cross-session exclusion, without a server."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
import uuid

from blackoutburst_agents.queue_journal import QueueJournal, QueueJournalError


class QueueJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / 'queue'
        self.journal = QueueJournal(self.directory)
        self.addCleanup(self.journal.close)
        self.scope = {'owner_id': 'owner', 'agent_id': 'agent', 'board_session_id': 'session', 'board_endpoint': 'https://board.test/mcp'}
        self.config = {'project_id': 'project', 'instruction': 'PRIVATE instruction', 'workspace': '/private/work',
                       'executable': '/private/codex', 'model': 'explicit-model', 'timeout': 120}

    def begin(self, request='request', **changes):
        return self.journal.begin(self.scope, request, {**self.config, **changes})

    def error(self, code, action):
        with self.assertRaises(QueueJournalError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def test_private_reopen_metadata_and_generated_keys(self):
        with self.journal.lock(self.scope):
            op = self.begin()
        self.assertEqual(op['payload']['selection_key'], 'queue-select-' + op['id'])
        self.assertEqual(op['payload']['worker_request_id'], 'queue-work-' + op['id'])
        self.journal.close()
        with QueueJournal(self.directory) as reopened:
            self.assertEqual(reopened.get(op['id']), op)
            public = json.dumps(reopened.status(op['id']))
            for secret in ('PRIVATE', '/private', 'board.test', 'scope', 'instruction', 'configuration'):
                self.assertNotIn(secret, public)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertTrue(all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in self.directory.iterdir()))

    def test_configuration_and_session_cannot_change_on_same_key(self):
        with self.journal.lock(self.scope):
            original = self.begin()
            self.assertEqual(self.begin(), original)
            for name, change in [('project_id', 'another'), ('instruction', 'different'), ('model', 'other'), ('human_owner_id', 'human')]:
                self.error('request_configuration_conflict', lambda: self.begin(**{name: change}))
            self.error('request_configuration_conflict', lambda: self.journal.begin(
                {**self.scope, 'board_session_id': 'other'}, 'request', self.config))

    def test_new_request_or_project_or_session_cannot_escape_unfinished_operation(self):
        with self.journal.lock(self.scope):
            op = self.begin()
            for scope, config in [(self.scope, self.config), ({**self.scope, 'board_session_id': 'new'}, self.config),
                                  (self.scope, {**self.config, 'project_id': 'other'})]:
                self.error('scope_has_unfinished_operation', lambda: self.journal.begin(scope, 'next', config))
            op = self.journal.update(op['id'], op['revision'], stage='needs_review', payload=op['payload'])
            self.error('scope_has_unfinished_operation', lambda: self.begin('next'))
            self.journal.abandon(op['id'], op['revision'], 'Explicit human reconciliation')
            self.assertNotEqual(self.begin('next')['id'], op['id'])
            self.assertEqual(self.begin()['stage'], 'abandoned')

    def test_lock_excludes_other_sessions_and_connections(self):
        other = QueueJournal(self.directory)
        self.addCleanup(other.close)
        with self.journal.lock(self.scope):
            with self.assertRaises(QueueJournalError):
                with other.lock({**self.scope, 'board_session_id': 'other'}):
                    self.fail('Another session acquired the same profile lock')
        with other.lock(self.scope):
            other.begin(self.scope, 'request', self.config)

    def test_different_profiles_and_boards_are_independent(self):
        with self.journal.lock(self.scope):
            first = self.begin()
            for change in ({'agent_id': 'other'}, {'owner_id': 'other'}, {'board_endpoint': 'https://other.test/mcp'}):
                scope = {**self.scope, **change}
                with self.journal.lock(scope):
                    self.assertNotEqual(first['id'], self.journal.begin(scope, 'request', self.config)['id'])

    def test_missing_model_invalid_bounds_and_credentials_rejected(self):
        with self.journal.lock(self.scope):
            for change in ({'model': None}, {'model': ''}, {'timeout': True}, {'timeout': float('nan')},
                           {'timeout': 10**400}, {'project_id': ''}, {'workspace': 'relative'}, {'instruction': 'é'*6001},
                           {'instruction': '\ud800'}, {'token': 'SECRET'}, {'human_owner_id': ''}):
                self.error('invalid_configuration', lambda: self.begin(**change))

    def test_cas_requires_lock_and_preserves_selection_identity(self):
        self.error('scope_lock_required', self.begin)
        with self.journal.lock(self.scope):
            op = self.begin()
            new = self.journal.update(op['id'], op['revision'], stage='selecting', payload=op['payload'])
            self.error('revision_conflict', lambda: self.journal.update(op['id'], op['revision'], stage='selecting', payload=op['payload']))
            self.error('immutable_queue_identity', lambda: self.journal.update(new['id'], new['revision'], stage='selecting', payload={**new['payload'], 'selection_key': 'different'}))
            self.error('invalid_stage_transition', lambda: self.journal.update(new['id'], new['revision'], stage='dispatched', payload={**new['payload'], 'ticket_id': 't', 'selected_version': 1, 'worker_operation_id': str(uuid.uuid4())}))

    def dispatched(self):
        op = self.begin()
        for stage, extra in [('selecting', {}), ('selected', {'ticket_id': 'ticket', 'selected_version': 2}), ('dispatched', {'worker_preparing': True, 'worker_operation_id': str(uuid.uuid4())})]:
            op = self.journal.update(op['id'], op['revision'], stage=stage, payload={**op['payload'], **extra})
        return op

    def proof(self, op, stage='complete'):
        return {'id': op['payload'].get('worker_operation_id', str(uuid.uuid4())), 'revision': 3, 'scope': {**self.scope, 'project_id': 'project', 'ticket_id': 'ticket'},
                'request_id': op['payload']['worker_request_id'], 'configuration': {k:v for k,v in self.config.items() if k != 'project_id'},
                'stage': stage, 'payload': {'publication': {'changes': {'status': 'review'}}}}

    def test_exact_worker_completion_and_status(self):
        with self.journal.lock(self.scope):
            op = self.dispatched()
            proof = self.proof(op)
            receipt = {'operation_id': proof['id'], 'status': 'complete', 'stage': 'complete', 'reason': 'publication_confirmed', 'cached': False, 'published_status': 'review'}
            final = self.journal.finish_worker(op['id'], op['revision'], receipt, proof)
            self.assertEqual(final['stage'], 'complete')
            self.assertEqual(final['payload']['worker_operation_id'], proof['id'])
            self.assertEqual(self.journal.status(final['id'])['worker_operation_id'], proof['id'])
            self.assertEqual(self.journal.status(final['id'])['ticket_id'], 'ticket')
            self.assertNotEqual(self.begin('next')['id'], op['id'])

    def test_foreign_work_cannot_resolve_queue(self):
        with self.journal.lock(self.scope):
            op = self.dispatched()
            proof = self.proof(op)
            for mutate in (lambda p:p['scope'].update(ticket_id='other'), lambda p:p.update(request_id='manual'),
                           lambda p:p['configuration'].update(model='other')):
                wrong = deepcopy(proof); mutate(wrong)
                self.error('worker_proof_mismatch', lambda:self.journal.resolve(op['id'], wrong))
            self.assertEqual(self.journal.get(op['id']), op)
            final = self.journal.resolve(op['id'], proof)
            self.assertEqual(final['stage'], 'complete')
            self.assertEqual(self.journal.resolve(op['id'], proof), final)

    def test_blocked_publication_remains_exclusive_until_human_abandon(self):
        with self.journal.lock(self.scope):
            op = self.dispatched(); proof = self.proof(op)
            proof['payload']['publication']['changes']['status'] = 'blocked'
            receipt = {'operation_id':proof['id'], 'status':'complete', 'stage':'complete', 'reason':'publication_confirmed', 'cached':False, 'published_status':'blocked'}
            final = self.journal.finish_worker(op['id'], op['revision'], receipt, proof)
            self.assertEqual(final['stage'], 'needs_review')
            self.error('worker_not_successful', lambda:self.journal.resolve(op['id'], proof))
            self.error('scope_has_unfinished_operation', lambda:self.begin('next'))
            self.error('worker_proof_mismatch', lambda:self.journal.abandon(final['id'],final['revision'],'Inspected'))
            self.error('invalid_worker_absence', lambda:self.journal.abandon(final['id'],final['revision'],'Inspected',worker_absent=True))
            nonterminal=deepcopy(proof); nonterminal['stage']='needs_review'
            self.error('worker_not_resolved', lambda:self.journal.abandon(final['id'],final['revision'],'Inspected',proof=nonterminal))
            abandoned=self.journal.abandon(final['id'],final['revision'],'Inspected',proof=proof)
            self.assertEqual(abandoned['stage'],'abandoned')

    def test_unconfirmed_selection_cannot_be_abandoned_or_reset(self):
        with self.journal.lock(self.scope):
            op=self.begin()
            op=self.journal.update(op['id'],op['revision'],stage='selecting',payload={**op['payload'],'selection_attempted':True})
            op=self.journal.update(op['id'],op['revision'],stage='needs_review',payload=op['payload'])
            self.error('selection_unconfirmed',lambda:self.journal.abandon(op['id'],op['revision'],'Inspected'))
            self.error('immutable_queue_identity',lambda:self.journal.update(op['id'],op['revision'],stage='needs_review',payload={**op['payload'],'selection_attempted':False}))
            self.error('selection_unconfirmed',lambda:self.journal.record_selection(op['id'],op['revision'],{'found':False}))
            resolved=self.journal.record_selection(op['id'],op['revision'],{'found':True,'claimed':False,'retried':True,'reason':'no_ready_ticket'})
            self.assertEqual(resolved['stage'],'needs_review')
            self.assertTrue(resolved['payload']['selection_confirmed'])
            self.assertEqual(self.journal.abandon(resolved['id'],resolved['revision'],'Inspected')['stage'],'abandoned')

    def test_child_absence_proof_only_applies_before_any_dispatch(self):
        with self.journal.lock(self.scope):
            op=self.begin()
            op=self.journal.update(op['id'],op['revision'],stage='selecting',payload=op['payload'])
            op=self.journal.update(op['id'],op['revision'],stage='selected',payload={**op['payload'],'ticket_id':'ticket','selected_version':2,'worker_preparing':True})
            self.error('invalid_operation_id',lambda:self.journal.update(op['id'],op['revision'],stage='dispatched',payload=op['payload']))
            op=self.journal.update(op['id'],op['revision'],stage='needs_review',payload=op['payload'])
            self.error('worker_proof_mismatch',lambda:self.journal.abandon(op['id'],op['revision'],'Inspected'))
            final=self.journal.abandon(op['id'],op['revision'],'Inspected with both locks',worker_absent=True)
            self.assertTrue(final['payload']['abandonment']['worker_absence_confirmed'])

    def test_insecure_directory_symlink_and_unrelated_database_refused(self):
        bad = self.directory.parent/'insecure'; bad.mkdir(mode=0o755)
        with self.assertRaises(QueueJournalError): QueueJournal(bad)
        linked = self.directory.parent/'link'; linked.symlink_to(self.directory)
        with self.assertRaises(QueueJournalError): QueueJournal(linked)
        self.journal.close()
        database=self.directory/'queue-journal.sqlite3'
        with sqlite3.connect(database) as db: db.execute('PRAGMA application_id=0')
        with self.assertRaises(QueueJournalError): QueueJournal(self.directory)


if __name__ == '__main__': unittest.main()
