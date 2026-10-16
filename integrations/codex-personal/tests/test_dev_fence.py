"""Ordinary local fence validation; fixture directories only, no process/model."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
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

    def test_parent_attach_keeps_uuid_and_never_adopts_other_parent(self):
        fence=DevelopmentFence(_test_anchor=self.base/'anchor');self.addCleanup(fence.close)
        identifier=str(uuid.uuid4());work_id=str(uuid.uuid4())
        pin={'path':str(self.base),'identity':[1,2]}
        scope=dict(owner_id='owner',agent_id='agent',project_id='project',ticket_id='ticket',
                   board_session_id='session',board_endpoint='https://board.test/mcp')
        parent_scope={'operation_id':identifier,'journal':pin,'configuration_hash':'a'*64}
        parent={'scope':scope,'root_identity':[3,4],'journal':pin,'configuration_hash':'a'*64,
                'child_scope':{'work_id':work_id,'runs':pin,'runtime':pin},'parent_scope':parent_scope}
        self.assertEqual(fence.bind(parent,identifier),(identifier,True))
        self.assertEqual(json.loads((self.base/'anchor/fence.json').read_text())['schema'],2)
        final={**parent,'configuration_hash':'b'*64}
        with self.assertRaises(DevelopmentError):fence.attach(final,str(uuid.uuid4()),parent)
        self.assertEqual(fence.inspect_binding(scope,[3,4])['binding'],parent)
        self.assertEqual(fence.attach(final,identifier,parent),(identifier,True))
        self.assertEqual(fence.attach(final,identifier,parent),(identifier,False))
        fence.verify(final,identifier)
        with self.assertRaises(DevelopmentError):fence.bind(parent,str(uuid.uuid4()))
        self.assertEqual(fence.inspect_binding(scope,[3,4])['operation_id'],identifier)

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
