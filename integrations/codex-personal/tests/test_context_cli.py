"""No network or inference: verify the inspect-then-use CLI boundary."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from blackoutburst_agents.cli import main
from blackoutburst_agents.context_cache import ContextCache
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.runner import PersonalRunner
from test_context_cache import artifact


class ContextCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.context_state = self.root/'context'
        with ContextCache(self.context_state) as cache:
            self.receipt = cache.save(artifact())
        self.instruction = self.root/'instruction.txt'
        self.instruction.write_text('Summarize the selected ticket.')
        self.runner_state = self.root/'runner'
        code, out, _ = self.invoke(['runner', '--state', str(self.runner_state), 'create',
            '--owner', 'owner', '--agent', 'agent', '--project', 'project', '--ticket', 'ticket',
            '--workspace', str(self.root), '--codex', sys.executable])
        self.assertEqual(code, 0)
        self.work = out['work_id']
        self.calls = []

    def invoke(self, args):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = main(args)
        return result, json.loads(stdout.getvalue()), stdout.getvalue()

    def ctx(self, *args):
        return self.invoke(['context', '--state', str(self.context_state), *args])

    def turn_args(self):
        return ['runner', '--state', str(self.runner_state), 'turn', '--work', self.work,
                '--request', 'context-event-1', '--prompt-file', str(self.instruction),
                '--context-state', str(self.context_state), '--context-key', self.receipt['context_key'],
                '--context-fingerprint', self.receipt['fingerprint'],
                '--board-connection', str(self.root/'not-opened.json'), '--board-session', 'session']

    def transport(self, settings, prompt, workspace, **callbacks):
        self.calls.append(prompt)
        callbacks['on_started'](999999999)
        session = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        return {'status': 'completed', 'reason': 'verified', 'session_id': session, 'text': 'PRIVATE_REPLY'}

    def test_inspect_and_forget_have_no_network_and_private_output(self):
        target = self.root/'view.json'
        with patch('blackoutburst_agents.context_cli.fetch_context', side_effect=AssertionError('network forbidden')):
            code, out, stdout = self.ctx('inspect', '--key', self.receipt['context_key'], '--output', str(target))
            self.assertEqual(code, 0)
            self.assertNotIn('PRIVATE_CONTEXT', stdout)
            self.assertEqual(json.loads(target.read_text())['data']['body'], 'PRIVATE_CONTEXT')
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            code, out, _ = self.ctx('forget', '--key', self.receipt['context_key'],
                                   '--fingerprint', self.receipt['fingerprint'])
            self.assertEqual(out['status'], 'forgotten')
            self.assertEqual(self.ctx('inspect', '--key', self.receipt['context_key'], '--output', str(target))[0], 2)

    def test_prepare_and_changed_check_do_not_silently_overwrite_reviewed_snapshot(self):
        with patch('blackoutburst_agents.context_cli.fetch_context', return_value=artifact(text='NEW')):
            code, out, stdout = self.ctx('check', '--key', self.receipt['context_key'],
                '--connection-file', 'unused', '--session', 'session')
            self.assertEqual((code, out['status']), (2, 'changed'))
            self.assertNotIn('NEW', stdout)
            with ContextCache(self.context_state) as cache:
                self.assertEqual(cache.get(self.receipt['context_key'])['data']['body'], 'PRIVATE_CONTEXT')
            code, out, _ = self.ctx('prepare', '--owner', 'owner', '--agent', 'agent',
                '--project', 'project', '--ticket', 'ticket', '--connection-file', 'unused', '--session', 'session')
            self.assertEqual((code, out['revision']), (0, 2))

    def test_prompt_export_checks_freshness_and_separates_instruction(self):
        target = self.root/'prompt.txt'
        with patch('blackoutburst_agents.context_cli.fetch_context', return_value=artifact()):
            code, out, stdout = self.ctx('prompt', '--key', self.receipt['context_key'],
                '--fingerprint', self.receipt['fingerprint'], '--connection-file', 'unused',
                '--session', 'session', '--instruction-file', str(self.instruction), '--output', str(target))
        self.assertEqual(code, 0)
        text = target.read_text()
        self.assertLess(text.index('HUMAN REQUEST'), text.index('BOARD SNAPSHOT'))
        self.assertIn('PRIVATE_CONTEXT', text)
        self.assertNotIn('PRIVATE_CONTEXT', stdout)

    def test_pinned_context_reaches_runner_once_and_duplicate_is_cached(self):
        with patch('blackoutburst_agents.context_cli.fetch_context', return_value=artifact()) as fetch, \
             patch('blackoutburst_agents.runner_cli.PersonalRunner', side_effect=lambda store: PersonalRunner(store, self.transport)):
            code, out, stdout = self.invoke(self.turn_args())
            self.assertEqual(code, 0)
            self.assertFalse(out['cached'])
            self.assertNotIn('PRIVATE_', stdout)
            code, out, _ = self.invoke(self.turn_args())
            self.assertTrue(out['cached'])
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(fetch.call_count, 2)
            self.assertIn('PRIVATE_CONTEXT', self.calls[0])

    def test_changed_revoked_or_wrong_scope_cannot_start_model(self):
        for fresh, error in ((artifact(text='CHANGED'), None), (None, BoardReadError('access_denied'))):
            with patch('blackoutburst_agents.context_cli.fetch_context', return_value=fresh, side_effect=error), \
                 patch('blackoutburst_agents.runner_cli.PersonalRunner') as runner:
                code, out, stdout = self.invoke(self.turn_args())
                self.assertEqual(code, 2)
                runner.assert_not_called()
        with ContextCache(self.context_state) as cache:
            other = cache.save(artifact(ticket='other'))
        args = self.turn_args()
        args[args.index('--context-key')+1] = other['context_key']
        args[args.index('--context-fingerprint')+1] = other['fingerprint']
        with patch('blackoutburst_agents.context_cli.fetch_context') as fetch, \
             patch('blackoutburst_agents.runner_cli.PersonalRunner') as runner:
            code, out, _ = self.invoke(args)
            self.assertEqual(out['reason'], 'context_scope_mismatch')
            fetch.assert_not_called()
            runner.assert_not_called()

    def test_partial_context_options_fail_before_inference(self):
        args = self.turn_args()[:-2]
        with patch('blackoutburst_agents.runner_cli.PersonalRunner') as runner:
            code, out, _ = self.invoke(args)
            self.assertEqual(out['reason'], 'incomplete_context_arguments')
            runner.assert_not_called()

    def test_inspect_refuses_symlink_output(self):
        target = self.root/'keep'
        target.write_text('KEEP')
        link = self.root/'view.json'
        link.symlink_to(target)
        code, _, _ = self.ctx('inspect', '--key', self.receipt['context_key'], '--output', str(link))
        self.assertEqual(code, 2)
        self.assertEqual(target.read_text(), 'KEEP')
