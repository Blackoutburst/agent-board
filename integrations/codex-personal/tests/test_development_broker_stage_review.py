"""Independent stage ownership checks; fake fixtures, no process/model/network."""
from pathlib import Path
import tempfile
import unittest

from blackoutburst_agents.dev_manifest import DevelopmentError, digest
from test_development_broker_security import Harness


class StageOwnershipReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temporary.cleanup)
        self.fixture = Harness(Path(temporary.name).resolve())
        self.addCleanup(self.fixture.close)

    def test_reconcile_never_removes_a_stage_that_preexisted_its_attempt(self):
        fixture = self.fixture
        stage = fixture.root / 'src' / ('.bb-dev-' + digest(b'patch') + '.tmp')
        stage.write_text('updated fixture\n')
        before = stage.stat()
        with self.assertRaises(DevelopmentError):
            fixture.patch()
        self.assertEqual(fixture.target.read_text(), 'original fixture\n')
        with self.assertRaises(DevelopmentError) as caught:
            fixture.broker.reconcile('patch')
        self.assertEqual(caught.exception.code, 'stage_cleanup_requires_review')
        self.assertTrue(stage.exists(), 'recovery removed a foreign stage that O_EXCL refused')
        self.assertEqual(stage.stat().st_ino, before.st_ino)
        self.assertEqual(stage.read_text(), 'updated fixture\n')
        fixture.broker.close()
        reopened = fixture.open()
        with self.assertRaises(DevelopmentError) as caught:
            reopened.reconcile('patch')
        self.assertEqual(caught.exception.code, 'stage_cleanup_requires_review')
        self.assertTrue(stage.exists())
        self.assertEqual(fixture.target.read_text(), 'original fixture\n')
        self.assertEqual(fixture.state()['operations'][digest(b'patch')]['phase'], 'intent')
