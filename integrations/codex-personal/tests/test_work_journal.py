"""Private effect-journal tests; no model, server or credentials."""
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.work_journal import JournalError, STAGES, WorkJournal


class WorkJournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='work-journal-test-')
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.directory = self.parent / 'state'
        self.journal = WorkJournal(self.directory)
        self.addCleanup(self.journal.close)
        self.scope = dict(owner_id='owner', agent_id='agent', project_id='project',
                          ticket_id='ticket', board_session_id='board-session',
                          board_endpoint='https://board.test/mcp')
        self.configuration = {'instruction': 'PRIVATE INSTRUCTION', 'workspace': '/private/workspace',
                              'executable': '/private/codex', 'model': None}

    def error(self, code, action):
        with self.assertRaises(JournalError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def begin(self, scope=None, request='request', configuration=None):
        return self.journal.begin(scope or self.scope, request,
                                  self.configuration if configuration is None else configuration)

    def advance(self, operation, target, payload=None):
        for stage in STAGES[STAGES.index(operation['stage']) + 1:]:
            operation = self.journal.update(operation['id'], operation['revision'],
                                            stage=stage, payload={} if payload is None else payload)
            if stage == target:
                return operation
        self.fail('Unreachable test stage')

    def test_private_configuration_survives_reopen_metadata_excludes_content(self):
        with self.journal.lock(self.scope):
            operation = self.begin()
            operation = self.journal.update(operation['id'], 1, stage='claiming',
                                            payload={'response': 'PRIVATE RESPONSE'})
        self.journal.close()
        with WorkJournal(self.directory) as reopened:
            self.assertEqual(reopened.get(operation['id']), operation)
            status = reopened.status(operation['id'])
            self.assertEqual(status['stage'], 'claiming')
            self.assertEqual(status['scope'], self.scope)
            encoded = json.dumps(status)
            for omitted in ('PRIVATE', '/private/', 'configuration', 'payload', 'instruction'):
                self.assertNotIn(omitted, encoded)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for path in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = (self.directory / 'work-journal.sqlite3').read_bytes()
        self.assertIn(b'PRIVATE INSTRUCTION', data)
        self.assertIn(b'PRIVATE RESPONSE', data)

    def test_request_is_idempotent_with_canonical_configuration(self):
        with self.journal.lock(self.scope):
            original = self.begin()
            ordered = dict(reversed(list(self.configuration.items())))
            self.assertEqual(self.begin(configuration=ordered), original)
            self.error('request_configuration_conflict', lambda: self.begin(configuration={'instruction': 'different'}))
            self.error('scope_has_unfinished_operation', lambda: self.begin(request='another'))
            self.assertEqual(self.journal.get(original['id']), original)
            complete = self.advance(original, 'complete')
            self.assertEqual(self.begin(), complete)
            self.assertNotEqual(self.begin(request='another')['id'], original['id'])

    def test_every_scope_field_is_part_of_idempotency_and_lock_identity(self):
        with self.journal.lock(self.scope):
            original = self.begin()
            for field in self.scope:
                changed = {**self.scope, field: 'https://other.test/mcp' if field == 'board_endpoint' else 'different'}
                with self.journal.lock(changed):
                    other = self.begin(changed)
                self.assertNotEqual(other['id'], original['id'])
                self.error('scope_lock_required', lambda: self.journal.update(
                    other['id'], 1, stage='claiming', payload={}))

    def test_lock_is_required_and_contending_connections_fail(self):
        other = WorkJournal(self.directory)
        self.addCleanup(other.close)
        self.error('scope_lock_required', self.begin)
        with self.journal.lock(self.scope):
            operation = self.begin()
            for journal in (self.journal, other):
                with self.assertRaises(JournalError) as caught:
                    with journal.lock(self.scope):
                        self.fail('Lock contention allowed')
                self.assertEqual(caught.exception.code, 'scope_busy')
            self.error('scope_lock_required', lambda: other.update(operation['id'], 1, stage='claiming', payload={}))
            self.error('journal_locked', self.journal.close)
            self.assertEqual(other.get(operation['id']), operation)
        with other.lock(self.scope):
            changed = other.update(operation['id'], 1, stage='claiming', payload={})
        self.assertEqual(changed['revision'], 2)

    def test_lock_excludes_other_process_and_is_released_on_exception(self):
        code = '''
import json, sys
from blackoutburst_agents.work_journal import WorkJournal, JournalError
with WorkJournal(sys.argv[1]) as journal:
    try:
        with journal.lock(json.loads(sys.argv[2])):
            print('acquired')
    except JournalError as error:
        print(error.code)
'''
        args = [sys.executable, '-c', code, str(self.directory), json.dumps(self.scope)]
        with self.assertRaisesRegex(RuntimeError, 'test interruption'):
            with self.journal.lock(self.scope):
                output = subprocess.run(args, text=True, capture_output=True, timeout=10, check=True)
                self.assertEqual(output.stdout.strip(), 'scope_busy')
                raise RuntimeError('test interruption')
        output = subprocess.run(args, text=True, capture_output=True, timeout=10, check=True)
        self.assertEqual(output.stdout.strip(), 'acquired')

    def test_pending_lock_is_not_stolen_by_another_thread(self):
        from blackoutburst_agents import work_journal
        opened = threading.Event()
        proceed = threading.Event()
        errors = []
        original = work_journal._private_file

        def delayed(path):
            opened.set()
            if not proceed.wait(5):
                raise RuntimeError('test timeout')
            return original(path)

        def worker():
            try:
                with self.journal.lock(self.scope):
                    pass
            except Exception as error:
                errors.append(error)

        with patch.object(work_journal, '_private_file', delayed):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(opened.wait(5))
                with self.assertRaises(JournalError) as caught:
                    with self.journal.lock(self.scope):
                        self.fail('Pending lock stolen')
                self.assertEqual(caught.exception.code, 'scope_busy')
                self.error('journal_locked', self.journal.close)
            finally:
                proceed.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        with self.journal.lock(self.scope):
            self.begin()

    def test_compare_and_swap_and_stage_transitions(self):
        with self.journal.lock(self.scope):
            operation = self.begin()
            self.error('invalid_stage_transition', lambda: self.journal.update(operation['id'], 1, stage='running', payload={}))
            operation = self.journal.update(operation['id'], 1, stage='claiming', payload={'old': 1})
            self.error('revision_conflict', lambda: self.journal.update(operation['id'], 1, stage='claiming', payload={}))
            operation = self.journal.update(operation['id'], 2, stage='claiming', payload={'new': 2})
            self.assertEqual(operation['payload'], {'new': 2})
            complete = self.advance(operation, 'complete')
            for stage in STAGES:
                if stage != 'complete':
                    self.error('invalid_stage_transition', lambda: self.journal.update(
                        complete['id'], complete['revision'], stage=stage, payload={}))
            again = self.journal.update(complete['id'], complete['revision'], stage='complete', payload={'published': True})
            self.assertEqual(again['revision'], complete['revision'] + 1)

    def test_needs_review_is_terminal_and_reopening_does_not_mutate(self):
        with self.journal.lock(self.scope):
            operation = self.begin()
            uncertain = self.journal.update(operation['id'], 1, stage='needs_review', payload={'reason': 'ambiguous'})
        with WorkJournal(self.directory) as reopened:
            with reopened.lock(self.scope):
                self.assertEqual(reopened.begin(self.scope, 'request', self.configuration), uncertain)
                self.error('invalid_stage_transition', lambda: reopened.update(
                    uncertain['id'], uncertain['revision'], stage='claiming', payload={}))
                self.error('scope_has_unfinished_operation', lambda: reopened.begin(self.scope, 'fresh-key', self.configuration))
                self.assertEqual(reopened.get(operation['id']), uncertain)

    def test_json_bounds_utf8_types_and_invalid_numbers(self):
        invalid = [{'text': '🙂' * 17000}, {'number': float('nan')}, {'number': float('inf')},
                   {1: 'nonstring key'}, {'object': object()}, {'text': '\ud800'}, {'tuple': (1,)}]
        with self.journal.lock(self.scope):
            for value in invalid:
                self.error('invalid_configuration', lambda: self.begin(configuration=value))
            self.error('invalid_configuration', lambda: self.begin(configuration=[]))
            operation = self.begin()
            self.error('invalid_payload', lambda: self.journal.update(operation['id'], 1,
                stage='claiming', payload={'text': '🙂' * 66000}))
            self.error('invalid_payload', lambda: self.journal.update(operation['id'], 1,
                stage='claiming', payload={'number': float('nan')}))
            self.error('invalid_payload', lambda: self.journal.update(operation['id'], 1,
                stage='claiming', payload=['Private text', None, 7]))
            accepted = self.journal.update(operation['id'], 1, stage='claiming', payload={'values': ['Private text', None, 7]})
            self.assertEqual(accepted['payload'], {'values': ['Private text', None, 7]})

    def test_nested_credentials_are_rejected_but_bounded_auth_receipt_is_valid(self):
        with self.journal.lock(self.scope):
            for key in ('token', 'Auth', 'credentials', 'access-token', 'API_Key', 'authorization', 'client secret', 'cookie'):
                self.error('credential_fields_forbidden', lambda: self.begin(configuration={'nested': [{key: 'not-retained'}]}))
            self.error('credential_fields_forbidden', lambda: self.begin(configuration={'authentication': 'Bearer private'}))
            operation = self.begin()
            self.error('credential_fields_forbidden', lambda: self.journal.update(operation['id'], 1,
                stage='claiming', payload={'result': {'refresh_token': 'not-retained'}}))
            result = self.journal.update(operation['id'], 1, stage='claiming', payload={
                'result': {'verification': {'authentication': 'chatgpt'}, 'input_tokens': 17}})
            self.assertEqual(result['payload']['result']['verification']['authentication'], 'chatgpt')

    def test_invalid_scope_identifiers_and_urls_fail_without_private_errors(self):
        invalid = [{}, {**self.scope, 'other': 'extra'}, {**self.scope, 'ticket_id': ''},
                   {**self.scope, 'owner_id': 'owner\nINSTRUCTION'},
                   {**self.scope, 'board_endpoint': 'https://user:private@board.test/mcp'},
                   {**self.scope, 'board_endpoint': 'https://board.test/mcp?token=private'},
                   {**self.scope, 'board_endpoint': 'http://remote.test/mcp'},
                   {**self.scope, 'board_endpoint': 'https://board.test:invalid/mcp'}]
        for scope in invalid:
            with self.assertRaises(JournalError) as caught:
                with self.journal.lock(scope):
                    self.fail('Invalid scope accepted')
            self.assertEqual(str(caught.exception), 'invalid_scope')
        for endpoint in ('http://localhost:9140/mcp', 'http://127.0.0.1/mcp', 'http://[::1]/mcp'):
            scope = {**self.scope, 'board_endpoint': endpoint}
            with self.journal.lock(scope):
                self.assertEqual(self.begin(scope)['scope'], scope)

    def test_invalid_ids_revisions_and_closed_store(self):
        self.error('invalid_operation_id', lambda: self.journal.get('private-unknown'))
        self.error('operation_not_found', lambda: self.journal.get(str(uuid.uuid4())))
        with self.journal.lock(self.scope):
            self.error('invalid_request_id', lambda: self.begin(request=''))
            operation = self.begin()
            for revision in (0, True, None, '1'):
                self.error('invalid_revision', lambda: self.journal.update(operation['id'], revision, stage='claiming', payload={}))
            self.error('invalid_stage', lambda: self.journal.update(operation['id'], 1, stage='unsupported', payload={}))
        self.journal.close()
        self.error('journal_closed', lambda: self.journal.get(operation['id']))
        self.journal.close()

    def test_insecure_or_aliased_state_is_rejected(self):
        insecure = self.parent / 'insecure'
        insecure.mkdir(mode=0o755)
        self.error('insecure_journal_directory', lambda: WorkJournal(insecure))
        alias = self.parent / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.error('insecure_journal_directory', lambda: WorkJournal(alias))
        target = self.parent / 'target'
        target.mkdir(mode=0o700)
        database = target / 'work-journal.sqlite3'
        database.symlink_to(self.directory / 'work-journal.sqlite3')
        self.error('invalid_journal_file', lambda: WorkJournal(target))
        database.unlink()
        database.touch(mode=0o644)
        self.error('insecure_journal_file', lambda: WorkJournal(target))
        database.unlink()
        os.link(self.directory / 'work-journal.sqlite3', database)
        self.error('insecure_journal_file', lambda: WorkJournal(target))

    def test_lock_and_sidecar_symlinks_are_rejected(self):
        with self.journal.lock(self.scope):
            self.begin()
        lock = next(self.directory.glob('*.lock'))
        lock.unlink()
        lock.symlink_to(self.directory / 'work-journal.sqlite3')
        with self.assertRaises(JournalError) as caught:
            with self.journal.lock(self.scope):
                self.fail('Symlink accepted')
        self.assertEqual(caught.exception.code, 'invalid_journal_file')
        sidecar = self.directory / 'work-journal.sqlite3-journal'
        sidecar.symlink_to(self.directory / 'work-journal.sqlite3')
        self.error('invalid_journal_file', lambda: WorkJournal(self.directory))

    def test_future_schema_refused_without_modifying_file(self):
        with self.journal.lock(self.scope):
            self.begin()
        self.journal.close()
        path = self.directory / 'work-journal.sqlite3'
        with sqlite3.connect(path) as connection:
            connection.execute('PRAGMA user_version=99')
        before = path.read_bytes()
        self.error('unsupported_journal_version', lambda: WorkJournal(self.directory))
        self.assertEqual(path.read_bytes(), before)

    def test_corrupt_sqlite_and_unrelated_schema_are_refused(self):
        self.journal.close()
        path = self.directory / 'work-journal.sqlite3'
        path.write_bytes(b'not a sqlite database')
        self.error('journal_unavailable', lambda: WorkJournal(self.directory))
        path.unlink()
        path.touch(mode=0o600)
        with sqlite3.connect(path) as connection:
            connection.execute('CREATE TABLE unrelated(value TEXT)')
        self.error('journal_corrupt', lambda: WorkJournal(self.directory))
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 0)

    def test_corrupt_record_cannot_be_read_retried_or_overwritten(self):
        with self.journal.lock(self.scope):
            operation = self.begin()
            path = self.directory / 'work-journal.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.execute('UPDATE operations SET payload_json=? WHERE id=?',
                                   ('{"response":"first","response":"second"}', operation['id']))
            self.error('journal_corrupt', lambda: self.journal.get(operation['id']))
            self.error('journal_corrupt', self.begin)
            self.error('journal_corrupt', lambda: self.journal.update(operation['id'], 1, stage='claiming', payload={}))

    def test_scalar_persisted_payload_is_rejected_as_corrupt(self):
        with self.journal.lock(self.scope):
            operation = self.begin()
            with sqlite3.connect(self.directory / 'work-journal.sqlite3') as connection:
                connection.execute('UPDATE operations SET payload_json=? WHERE id=?', ('null', operation['id']))
            self.error('journal_corrupt', lambda: self.journal.get(operation['id']))

    def test_explicit_abandonment_preserves_private_evidence_and_old_key_tombstone(self):
        with self.journal.lock(self.scope):
            operation = self.begin()
            self.error('invalid_stage_transition', lambda: self.journal.abandon(operation['id'], 1, 'Verified'))
            operation = self.journal.update(operation['id'], 1, stage='needs_review', payload={'result': 'PRIVATE RESULT'})
            self.error('invalid_stage_transition', lambda: self.journal.update(operation['id'], 2, stage='abandoned', payload={}))
            self.error('revision_conflict', lambda: self.journal.abandon(operation['id'], 1, 'Verified'))
            self.error('invalid_abandonment_reason', lambda: self.journal.abandon(operation['id'], 2, ''))
            abandoned = self.journal.abandon(operation['id'], 2, 'PRIVATE HUMAN FINDING')
            self.assertEqual(abandoned['stage'], 'abandoned')
            self.assertEqual(abandoned['payload']['result'], 'PRIVATE RESULT')
            self.assertEqual(abandoned['payload']['abandonment']['reason'], 'PRIVATE HUMAN FINDING')
            self.assertEqual(self.begin(), abandoned)
            self.assertEqual(self.journal.abandon(operation['id'], 3, 'PRIVATE HUMAN FINDING'), abandoned)
            self.error('abandonment_conflict', lambda: self.journal.abandon(operation['id'], 3, 'Changed reason'))
            self.assertNotIn('PRIVATE', json.dumps(self.journal.status(operation['id'])))
            self.assertNotEqual(self.begin(request='new-request')['id'], operation['id'])


if __name__ == '__main__':
    unittest.main()
