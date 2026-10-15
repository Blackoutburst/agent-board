"""CLI integration with a fake transport and real private local storage."""
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
from blackoutburst_agents.runner import PersonalRunner


class RunnerCliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / 'state'
        code, receipt, _ = self.invoke('create', '--owner', 'owner', '--agent', 'agent',
            '--project', 'project', '--ticket', 'ticket', '--workspace', str(self.root),
            '--codex', sys.executable)
        self.assertEqual(code, 0)
        self.work = receipt['work_id']
        self.prompt = self.root / 'prompt.txt'
        self.prompt.write_text('PRIVATE_PROMPT')
        self.calls = 0

    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(['runner', '--state', str(self.state), *args])
        text = output.getvalue()
        return code, json.loads(text), text

    def transport(self, settings, prompt, workspace, **callbacks):
        self.calls += 1
        callbacks['on_started'](999999999)
        session = callbacks['resume_session_id'] or str(uuid.uuid4())
        callbacks['on_session'](session)
        return {'status': 'completed', 'reason': 'verified', 'session_id': session,
                'text': 'PRIVATE_RESPONSE'}

    def test_output_is_explicit_private_and_retry_does_not_infer(self):
        args = ('turn', '--work', self.work, '--request', 'event-1', '--prompt-file', str(self.prompt))
        with patch('blackoutburst_agents.runner_cli.PersonalRunner',
                   side_effect=lambda store: PersonalRunner(store, self.transport)):
            code, receipt, stdout = self.invoke(*args)
            self.assertEqual(code, 0)
            self.assertNotIn('PRIVATE_RESPONSE', stdout)
            self.assertNotIn('PRIVATE_PROMPT', stdout)
            destination = self.root / 'response.txt'
            code, repeat, _ = self.invoke(*args, '--output-text', str(destination))
        self.assertEqual(code, 0)
        self.assertTrue(repeat['cached'])
        self.assertEqual(receipt['turn_id'], repeat['turn_id'])
        self.assertEqual(self.calls, 1)
        self.assertEqual(destination.read_text(), 'PRIVATE_RESPONSE')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)

    def test_refused_output_symlink_preserves_result_for_cached_retry(self):
        target = self.root / 'keep.txt'
        target.write_text('keep')
        alias = self.root / 'alias.txt'
        alias.symlink_to(target)
        args = ('turn', '--work', self.work, '--request', 'event-1', '--prompt-file', str(self.prompt))
        with patch('blackoutburst_agents.runner_cli.PersonalRunner',
                   side_effect=lambda store: PersonalRunner(store, self.transport)):
            code, receipt, _ = self.invoke(*args, '--output-text', str(alias))
            self.assertEqual(code, 2)
            self.assertEqual(receipt['reason'], 'invalid_configuration_or_local_io')
            code, repeat, _ = self.invoke(*args)
        self.assertEqual(target.read_text(), 'keep')
        self.assertEqual(code, 0)
        self.assertTrue(repeat['cached'])
        self.assertEqual(self.calls, 1)

    def test_invalid_prompt_never_launches_turn(self):
        self.prompt.write_bytes(b'\xff')
        with patch('blackoutburst_agents.runner_cli.PersonalRunner') as runner:
            code, receipt, _ = self.invoke('turn', '--work', self.work, '--request', 'event-1',
                                           '--prompt-file', str(self.prompt))
        self.assertEqual(code, 2)
        self.assertEqual(receipt['status'], 'blocked')
        runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
