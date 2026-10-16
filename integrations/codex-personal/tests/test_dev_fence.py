"""Ordinary local fence validation; fixture directories only, no process/model."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from blackoutburst_agents.dev_fence import DevelopmentFence, canonical_anchor, normalized_origin
from blackoutburst_agents.dev_manifest import DevelopmentError


class FenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()

    def test_canonical_anchor_uses_os_account_not_environment(self):
        with patch.dict(os.environ, {'HOME': str(self.base / 'different-home'),
                                     'CODEX_HOME': str(self.base / 'different-codex')}):
            import pwd
            expected = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.local/state/blackoutburst-agents/development'
            self.assertEqual(canonical_anchor(), expected)

    def test_origin_normalization_and_invalid_authorities(self):
        self.assertEqual(normalized_origin('https://BOARD.example:443/mcp/'), 'https://board.example')
        self.assertEqual(normalized_origin('http://127.0.0.1:8080/mcp'), 'http://127.0.0.1:8080')
        for value in ('https://user:password@board.example/mcp', 'http://board.example/mcp',
                      'https://board.example/mcp?token=x', 'https://board.example/mcp#private', None):
            with self.subTest(value=value), self.assertRaises(DevelopmentError):
                normalized_origin(value)

    def test_symlink_anchor_is_refused(self):
        target = self.base / 'target'; target.mkdir(mode=0o700)
        alias = self.base / 'alias'; alias.symlink_to(target, target_is_directory=True)
        with self.assertRaises(DevelopmentError):
            DevelopmentFence(_test_anchor=alias)
        self.assertEqual(list(target.iterdir()), [])

    def test_corrupt_record_is_a_fixed_error(self):
        fence = DevelopmentFence(_test_anchor=self.base / 'anchor')
        self.addCleanup(fence.close)
        (self.base / 'anchor/fence.json').write_text(json.dumps({'schema': 1, 'records': {'bad': None}}))
        with self.assertRaises(DevelopmentError) as caught:
            fence.verify({}, 'unused')
        self.assertEqual(caught.exception.code, 'fence_corrupt')

    def test_replaced_lock_is_refused(self):
        fence = DevelopmentFence(_test_anchor=self.base / 'anchor')
        self.addCleanup(fence.close)
        path = self.base / 'anchor/fence.lock'
        path.rename(self.base / 'anchor/old-lock')
        path.write_bytes(b''); path.chmod(0o600)
        with self.assertRaises(DevelopmentError) as caught:
            with fence.lock():
                pass
        self.assertEqual(caught.exception.code, 'fence_lock_changed')
