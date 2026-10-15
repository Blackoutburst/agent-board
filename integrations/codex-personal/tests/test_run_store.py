"""Local storage tests: no credentials, Codex or inference."""
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.run_store import RunStore, StoreError


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='run-store-test-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.directory = self.parent / 'state'
        self.store = RunStore(self.directory)
        self.addCleanup(self.store.close)

    def work(self, **changes):
        args = dict(owner_id='owner', agent_id='agent', project_id='project', ticket_id='ticket',
                    workspace=self.parent, executable=sys.executable)
        args.update(changes)
        return self.store.create_work(**args)

    def error(self, code, fn):
        with self.assertRaises(StoreError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def complete(self, work):
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'request', 'PRIVATE PROMPT')['turn']['id']
            self.store.on_process(turn, 123456789)
            sid = work['provider_session_id'] or str(uuid.uuid4())
            self.store.on_session(turn, sid)
            result = dict(status='completed', session_id=sid, text='PRIVATE RESPONSE')
            self.store.finish_turn(turn, result)
            return turn, result

    def interrupt(self, pid=None, session=True):
        work = self.work()
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'request', 'prompt')['turn']['id']
            if pid is not None:
                self.store.on_process(turn, pid)
            if session:
                self.store.on_session(turn, str(uuid.uuid4()))
        return work, turn

    def test_private_permissions_and_prompt_not_stored(self):
        work = self.work()
        turn, result = self.complete(work)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        for file in self.directory.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        data = (self.directory / 'runs.sqlite3').read_bytes()
        self.assertNotIn(b'PRIVATE PROMPT', data)
        self.assertIn(b'PRIVATE RESPONSE', data)
        output = json.dumps(self.store.status(work['id']))
        self.assertNotIn('PRIVATE', output)
        self.assertNotIn('prompt_sha256', output)
        self.assertEqual(self.store.turn_result(turn), result)

    def test_immutable_scope_deduplication_and_separation(self):
        work = self.work()
        self.assertEqual(self.work()['id'], work['id'])
        self.error('scope_configuration_conflict', lambda: self.work(model='other'))
        for field in ('owner_id', 'agent_id', 'project_id', 'ticket_id'):
            self.assertNotEqual(self.work(**{field: 'other'})['id'], work['id'])

    def test_terminal_cache_survives_restart_and_hash_conflict(self):
        work = self.work()
        turn, result = self.complete(work)
        second = RunStore(self.directory)
        self.addCleanup(second.close)
        with second.work_lock(work['id']):
            entry = second.begin_turn(work['id'], 'request', 'PRIVATE PROMPT')
            self.assertTrue(entry['cached'])
            self.assertEqual(entry['turn']['id'], turn)
            self.assertEqual(second.turn_result(turn), result)
            self.error('request_conflict', lambda: second.begin_turn(work['id'], 'request', 'different'))
            next_turn = second.begin_turn(work['id'], 'next', 'next')['turn']
            self.assertEqual(next_turn['provider_session_id'], result['session_id'])
            second.finish_turn(next_turn['id'], {'status': 'blocked', 'reason': 'doctor_failed'})

    def test_lock_and_transaction_guards_across_connections(self):
        work = self.work()
        second = RunStore(self.directory)
        self.addCleanup(second.close)
        self.error('work_lock_required', lambda: self.store.begin_turn(work['id'], 'x', 'prompt'))
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'a', 'prompt')['turn']['id']
            with self.assertRaises(StoreError) as caught:
                with second.work_lock(work['id']):
                    self.fail('concurrent coordinator accepted')
            self.assertEqual(caught.exception.code, 'work_busy')
            self.assertEqual(second.status(work['id'])['turns'][0]['status'], 'running')
            for request in ('a', 'b'):
                self.error('work_busy', lambda: self.store.begin_turn(work['id'], request, 'prompt'))
            self.error('turn_still_running', lambda: self.store.resolve_interruption(work['id'], decision='abandon', reason='review'))
            self.store.finish_turn(turn, {'status': 'blocked', 'reason': 'doctor_failed'})

    def test_sessions_are_uuids_immutable_and_scoped(self):
        first, second = self.work(), self.work(ticket_id='second')
        sid = str(uuid.uuid4())
        with self.store.work_lock(first['id']):
            turn = self.store.begin_turn(first['id'], 'a', 'prompt')['turn']['id']
            self.error('invalid_session_id', lambda: self.store.on_session(turn, 'bad'))
            self.store.on_session(turn, sid)
            self.store.on_session(turn, sid)
            self.error('session_conflict', lambda: self.store.on_session(turn, str(uuid.uuid4())))
            self.store.finish_turn(turn, {'status': 'completed', 'session_id': sid, 'text': 'done'})
        with self.store.work_lock(second['id']):
            turn = self.store.begin_turn(second['id'], 'a', 'prompt')['turn']['id']
            self.error('session_scope_conflict', lambda: self.store.on_session(turn, sid))
            self.error('session_not_recorded', lambda: self.store.finish_turn(turn, {'status': 'completed'}))
            self.store.finish_turn(turn, {'status': 'blocked', 'reason': 'no_session'})

    def test_cancel_from_another_connection(self):
        work = self.work()
        second = RunStore(self.directory)
        self.addCleanup(second.close)
        self.assertFalse(second.request_cancel(work['id']))
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'a', 'prompt')['turn']['id']
            self.assertFalse(self.store.is_cancelled(turn))
            self.assertTrue(second.request_cancel(work['id']))
            self.assertTrue(self.store.is_cancelled(turn))
            self.store.finish_turn(turn, {'status': 'cancelled', 'reason': 'before_start'})
            self.assertFalse(second.request_cancel(work['id']))
        self.assertEqual(self.store.get_work(work['id'])['status'], 'open')

    def test_recovered_turn_never_replayed_and_resolution_is_explicit(self):
        work, turn = self.interrupt(pid=123456789)
        with self.store.work_lock(work['id']):
            self.assertEqual(self.store.get_work(work['id'])['status'], 'needs_review')
            self.assertEqual(self.store.status(work['id'])['turns'][0]['status'], 'interrupted')
            self.error('request_needs_review', lambda: self.store.begin_turn(work['id'], 'request', 'prompt'))
            self.error('work_needs_review', lambda: self.store.begin_turn(work['id'], 'next', 'prompt'))
            with patch('blackoutburst_agents.run_store.os.kill', side_effect=ProcessLookupError), patch('blackoutburst_agents.run_store.os.killpg', side_effect=ProcessLookupError):
                result = self.store.resolve_interruption(work['id'], decision='continue', reason='PRIVATE REVIEW REASON')
            self.assertEqual(result['status'], 'open')
            self.error('request_needs_review', lambda: self.store.begin_turn(work['id'], 'request', 'prompt'))
            next_turn = self.store.begin_turn(work['id'], 'next', 'prompt')['turn']['id']
            self.store.finish_turn(next_turn, {'status': 'blocked', 'reason': 'doctor_failed'})
        self.assertNotIn('PRIVATE REVIEW REASON', json.dumps(self.store.status(work['id'])))
        self.assertEqual(self.store.turn_result(turn), {'status': 'blocked', 'reason': 'interrupted'})

    def test_launch_gap_can_only_be_explicitly_abandoned(self):
        work, _ = self.interrupt(session=False)
        with self.store.work_lock(work['id']):
            self.error('process_state_unknown', lambda: self.store.resolve_interruption(work['id'], decision='continue', reason='Reviewed'))
            self.error('invalid_resolution_reason', lambda: self.store.resolve_interruption(work['id'], decision='abandon', reason=''))
            result = self.store.resolve_interruption(work['id'], decision='abandon', reason='Manually confirmed launch gap safe')
        self.assertEqual(result['status'], 'closed')
        self.assertNotEqual(self.work()['id'], work['id'])

    def test_live_process_and_unknown_permission_block_both_decisions(self):
        work, _ = self.interrupt(pid=os.getpid())
        with self.store.work_lock(work['id']):
            for decision in ('continue', 'abandon'):
                action = lambda: self.store.resolve_interruption(work['id'], decision=decision, reason='Reviewed')
                self.error('process_still_running', action)
                with patch('blackoutburst_agents.run_store.os.kill', side_effect=PermissionError):
                    self.error('process_state_unknown', action)
        self.assertEqual(self.work()['id'], work['id'])

    def test_absent_leader_with_surviving_group_blocks_both_decisions(self):
        work, _ = self.interrupt(pid=123456789)
        with self.store.work_lock(work['id']):
            with patch('blackoutburst_agents.run_store.os.kill', side_effect=ProcessLookupError), patch('blackoutburst_agents.run_store.os.killpg', return_value=None) as group:
                for decision in ('continue', 'abandon'):
                    self.error('process_still_running', lambda: self.store.resolve_interruption(work['id'], decision=decision, reason='Reviewed'))
                group.assert_called_with(123456789, 0)

    def test_failure_after_start_is_uncertain_but_result_is_cached(self):
        work = self.work()
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'a', 'prompt')['turn']['id']
            self.store.on_process(turn, 123456789)
            self.store.on_session(turn, str(uuid.uuid4()))
            self.store.finish_turn(turn, {'status': 'blocked', 'reason': 'timeout'})
            self.assertTrue(self.store.begin_turn(work['id'], 'a', 'prompt')['cached'])
            self.error('work_needs_review', lambda: self.store.begin_turn(work['id'], 'b', 'prompt'))
        self.assertEqual(self.store.get_work(work['id'])['status'], 'needs_review')

    def test_cleanup_failure_cannot_continue_with_absent_process_and_group(self):
        work = self.work()
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'a', 'prompt')['turn']['id']
            self.store.on_process(turn, 123456789)
            self.store.on_session(turn, str(uuid.uuid4()))
            self.store.finish_turn(turn, {'status': 'blocked', 'reason': 'cleanup_failed'})
            with patch('blackoutburst_agents.run_store.os.kill', side_effect=ProcessLookupError), patch('blackoutburst_agents.run_store.os.killpg', side_effect=ProcessLookupError):
                self.error('cleanup_unconfirmed', lambda: self.store.resolve_interruption(work['id'], decision='continue', reason='Reviewed'))

    def test_continue_requires_session_and_lock(self):
        work, _ = self.interrupt(pid=123456789, session=False)
        self.error('work_lock_required', lambda: self.store.resolve_interruption(work['id'], decision='continue', reason='Reviewed'))
        with self.store.work_lock(work['id']):
            with patch('blackoutburst_agents.run_store.os.kill', side_effect=ProcessLookupError), patch('blackoutburst_agents.run_store.os.killpg', side_effect=ProcessLookupError):
                self.error('session_not_recorded', lambda: self.store.resolve_interruption(work['id'], decision='continue', reason='Reviewed'))

    def test_insecure_or_symlink_state_is_rejected(self):
        insecure = self.parent / 'insecure'
        insecure.mkdir(mode=0o755)
        self.error('insecure_state_directory', lambda: RunStore(insecure))
        alias = self.parent / 'alias'
        alias.symlink_to(self.directory, target_is_directory=True)
        self.error('insecure_state_directory', lambda: RunStore(alias))
        target = self.parent / 'target'
        target.mkdir(mode=0o700)
        (target / 'runs.sqlite3').symlink_to(self.directory / 'runs.sqlite3')
        self.error('invalid_state_file', lambda: RunStore(target))
        (target / 'runs.sqlite3').unlink()
        (target / 'runs.sqlite3').touch(mode=0o644)
        self.error('insecure_state_file', lambda: RunStore(target))

    def test_symlink_lock_and_journal_rejected(self):
        work = self.work()
        (self.directory / (work['id'] + '.lock')).symlink_to(self.directory / 'runs.sqlite3')
        with self.assertRaises(StoreError) as caught:
            with self.store.work_lock(work['id']):
                self.fail('symlink lock accepted')
        self.assertEqual(caught.exception.code, 'invalid_state_file')
        (self.directory / 'runs.sqlite3-journal').symlink_to(self.directory / 'runs.sqlite3')
        self.error('invalid_state_file', lambda: RunStore(self.directory))

    def test_future_schema_is_rejected_without_downgrade_or_data_change(self):
        work = self.work()
        connection = sqlite3.connect(self.directory / 'runs.sqlite3')
        try:
            connection.execute('PRAGMA user_version=99')
            connection.commit()
            self.error('unsupported_state_version', lambda: RunStore(self.directory))
            self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 99)
            self.assertEqual(connection.execute('SELECT id FROM works').fetchone()[0], work['id'])
        finally:
            connection.close()

    def test_result_rejects_prompt_and_credential_fields(self):
        work = self.work()
        with self.store.work_lock(work['id']):
            turn = self.store.begin_turn(work['id'], 'a', 'prompt')['turn']['id']
            for bad in ({'prompt': 'private'}, {'nested': {'access_token': 'private'}}, {'number': float('nan')}):
                self.error('invalid_result', lambda: self.store.finish_turn(turn, {'status': 'blocked', **bad}))
            self.store.finish_turn(turn, {'status': 'blocked', 'reason': 'doctor_failed'})


if __name__ == '__main__':
    unittest.main()
