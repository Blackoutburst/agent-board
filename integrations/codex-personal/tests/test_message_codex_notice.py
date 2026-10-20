"""MessageWorker/Runtime/RunStore chain with real parsing of a fake CLI only."""
import json
import os
import unittest

import test_codex_turn as turn_fixture
import test_message_host as fixture
from blackoutburst_agents.codex_turn import execute_turn
from blackoutburst_agents.host_config import validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.message_host import MessageHostSupervisor


@unittest.skipUnless(os.name == 'posix' and fixture.Board is not None,
    'Local POSIX fake processes and repository Board sources required')
class MessageCodexNoticeTests(unittest.TestCase):
    resources = fixture.MessageHostTests.resources
    post = fixture.MessageHostTests.post
    enable = fixture.MessageHostTests.enable
    tick_host = fixture.MessageHostTests.tick_host
    replies = fixture.MessageHostTests.replies
    operation = fixture.MessageHostTests.operation

    def setUp(self):
        fixture.MessageHostTests.setUp(self)
        self.directory = self.root
        self.body = (
            "event({'type':'item.completed','item':{'id':'notice_0','type':'error',"
            "'message':'PRIVATE_NOTICE_NOT_RETAINED'}})\n"
            "started()\nmessage('PRIVATE_REPLY')\nevent({'type':'turn.completed'})\n")
        self.cli = turn_fixture.CodexTurnTests.fake_cli(self, self.body)
        self.config['targets'][0]['executable'] = str(self.cli.resolve())
        self.config = validate(self.config, bind_connections=True)
        self.host.close()
        self.host = HostJournal(self.root / 'notice-host')
        self.addCleanup(self.host.close)
        self.host.configure(self.config)
        self.supervisor = MessageHostSupervisor(self.host, resource_factory=self.resources)

    def transport(self, settings, prompt, workspace, **callbacks):
        self.calls += 1
        return execute_turn(settings, prompt, workspace, **callbacks)

    def execution(self):
        runtime = self.board.get_runtime(self.human)
        self.assertEqual(len(runtime['executions']), 1)
        self.assertEqual(runtime['active_count'], 0)
        return runtime['executions'][0]

    def test_notice_then_verified_turn_posts_once_and_finishes_real_journals(self):
        self.post()
        self.enable()
        result = self.tick_host()
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.replies()), 1)
        op = self.operation()
        self.assertEqual(op['state'], 'complete')
        turn = self.message_runs._db.execute('SELECT id FROM turns').fetchone()
        stored = self.message_runs.turn_result(turn['id'])
        self.assertEqual(stored['status'], 'completed')
        self.assertTrue(all(stored['verification'][name] is True for name in (
            'exit_zero', 'turn_completed', 'session_verified', 'tool_free', 'cleanup_confirmed')))
        self.assertNotIn('PRIVATE_NOTICE_NOT_RETAINED', json.dumps(stored))
        execution = self.execution()
        self.assertEqual((execution['state'], execution['outcome'], execution['cleanup_confirmed']),
            ('finished', 'completed', True))
        self.assertEqual(self.host.dispatches()[0]['state'], 'complete')
        self.assertEqual(self.host.status()['remaining_dispatches'], 0)
        self.assertEqual(self.tick_host()['status'], 'disabled')
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(self.replies()), 1)
        self.assertEqual(self.control.calls.count('reserve_message_execution'), 1)

    def test_notice_does_not_turn_a_fatal_result_into_a_reply_or_replay(self):
        turn_fixture.CodexTurnTests.fake_cli(self, self.body.split('started()')[0] +
            "event({'type':'turn.failed','error':{'message':'usage limit PRIVATE_FATAL'}})\n")
        self.post()
        self.enable()
        result = self.tick_host()
        self.assertEqual(result['status'], 'needs_review', result)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.replies(), [])
        self.assertEqual(self.operation()['state'], 'needs_review')
        turn = self.message_runs._db.execute('SELECT id FROM turns').fetchone()
        stored = self.message_runs.turn_result(turn['id'])
        self.assertEqual((stored['status'], stored['reason']), ('blocked', 'quota_or_rate_limit'))
        self.assertTrue(stored['verification']['cleanup_confirmed'])
        self.assertIsNone(stored['text'])
        self.assertNotIn('PRIVATE_FATAL', json.dumps(stored))
        execution = self.execution()
        self.assertEqual((execution['state'], execution['outcome'], execution['cleanup_confirmed']),
            ('finished', 'blocked', True))
        self.assertEqual(self.host.status()['remaining_dispatches'], 0)
        self.assertEqual(self.tick_host()['status'], 'disabled')
        self.assertEqual(self.calls, 1)
        self.assertFalse(any(name in ('post_message', 'checkpoint_message_feed') for name, _ in self.client.calls))


if __name__ == '__main__':
    unittest.main()
