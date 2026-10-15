"""Coordinator tests with fake text turns; never invoke Codex or a model."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

from blackoutburst_agents.run_store import RunStore, StoreError
from blackoutburst_agents.runner import PersonalRunner, public_result


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.workspace = self.root / 'workspace'
        self.workspace.mkdir()
        self.store = RunStore(self.state)
        self.work = self.create_work()
        self.calls = []

    def create_work(self, ticket='ticket-1'):
        return self.store.create_work(owner_id='owner', agent_id='agent', project_id='project',
            ticket_id=ticket, workspace=str(self.workspace), executable=sys.executable, model=None)

    def transport(self, settings, prompt, workspace, **callbacks):
        self.calls.append({'prompt': prompt, 'resume': callbacks['resume_session_id']})
        callbacks['on_started'](999999999)
        session = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        return {'status': 'completed', 'reason': 'verified', 'session_id': session,
                'text': 'PRIVATE_RESPONSE_' + prompt}

    def test_same_request_returns_cached_result_without_another_turn(self):
        runner = PersonalRunner(self.store, self.transport)
        first = runner.run(self.work['id'], 'event-1', 'hello')
        repeat = runner.run(self.work['id'], 'event-1', 'hello')
        self.assertFalse(first['cached'])
        self.assertTrue(repeat['cached'])
        self.assertEqual(first['turn_id'], repeat['turn_id'])
        self.assertEqual(first['result'], repeat['result'])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('PRIVATE_RESPONSE', json.dumps(public_result(repeat)))

    def test_restart_resumes_exact_persisted_session(self):
        first = PersonalRunner(self.store, self.transport).run(self.work['id'], 'first', 'hello')
        restarted = PersonalRunner(RunStore(self.state), self.transport)
        second = restarted.run(self.work['id'], 'second', 'continue')
        self.assertEqual(second['result']['session_id'], first['result']['session_id'])
        self.assertEqual(self.calls[1]['resume'], first['result']['session_id'])

    def test_conflicting_idempotency_input_never_reaches_transport(self):
        runner = PersonalRunner(self.store, self.transport)
        runner.run(self.work['id'], 'same', 'one')
        with self.assertRaises(StoreError):
            runner.run(self.work['id'], 'same', 'different')
        self.assertEqual(len(self.calls), 1)

    def test_contexts_have_separate_provider_sessions(self):
        runner = PersonalRunner(self.store, self.transport)
        first = runner.run(self.work['id'], 'one', 'first ticket')
        other = self.create_work('ticket-2')
        second = runner.run(other['id'], 'one', 'second ticket')
        self.assertNotEqual(first['result']['session_id'], second['result']['session_id'])
        self.assertIsNone(self.calls[0]['resume'])
        self.assertIsNone(self.calls[1]['resume'])
        self.assertEqual(self.create_work()['id'], self.work['id'])

    def test_transport_failure_stays_uncertain_until_explicit_resolution(self):
        def failed(settings, prompt, workspace, **callbacks):
            callbacks['on_started'](999999999)
            callbacks['on_session'](str(uuid.uuid4()))
            raise RuntimeError('PRIVATE_DIAGNOSTICS')

        result = PersonalRunner(self.store, failed).run(self.work['id'], 'failed', 'hello')
        self.assertEqual(result['result']['reason'], 'runner_error')
        self.assertNotIn('PRIVATE_DIAGNOSTICS', json.dumps(result))
        self.assertEqual(self.store.get_work(self.work['id'])['status'], 'needs_review')
        with self.assertRaises(StoreError):
            PersonalRunner(self.store, self.transport).run(self.work['id'], 'later', 'continue')
        with self.store.work_lock(self.work['id']):
            self.store.resolve_interruption(self.work['id'], decision='continue', reason='Process gone; continuation inspected.')
        self.assertEqual(PersonalRunner(self.store, self.transport).run(
            self.work['id'], 'later', 'continue')['result']['status'], 'completed')

    def test_cancel_flag_is_seen_without_signalling_saved_pid(self):
        def cancelled(settings, prompt, workspace, **callbacks):
            callbacks['on_started'](999999999)
            self.assertTrue(self.store.request_cancel(self.work['id']))
            self.assertTrue(callbacks['should_cancel']())
            return {'status': 'cancelled', 'reason': 'cancelled', 'session_id': None, 'text': ''}

        result = PersonalRunner(self.store, cancelled).run(self.work['id'], 'cancel', 'hello')
        self.assertEqual(result['result']['status'], 'cancelled')

    def test_status_contains_neither_prompt_nor_model_text(self):
        PersonalRunner(self.store, self.transport).run(self.work['id'], 'one', 'PRIVATE_PROMPT')
        status = json.dumps(self.store.status(self.work['id']))
        self.assertNotIn('PRIVATE_PROMPT', status)
        self.assertNotIn('PRIVATE_RESPONSE', status)


if __name__ == '__main__':
    unittest.main()
