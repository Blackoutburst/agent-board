"""Verify against the repository's real Board 2 API, using only temporary SQLite."""
from pathlib import Path
import sys
import tempfile
import unittest

from blackoutburst_agents.context import ContextScope, build_context
from blackoutburst_agents.context_cache import validate_artifact

REPO = Path(__file__).resolve().parents[3]
Board = None
if (REPO/'board.py').is_file() and (REPO/'coordination.py').is_file():
    sys.path.insert(0, str(REPO))
    try:
        from board import Board
    finally:
        sys.path.remove(str(REPO))


@unittest.skipIf(Board is None, 'Repository Board sources not bundled in the standalone package')
class BoardContextContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.board = Board(Path(temporary.name)/'board', base_url='https://board.test')
        b = self.board
        b.ensure_owner('owner', 'Owner')
        self.human = {'kind': 'human', 'owner_id': 'owner'}
        b.set_policy(self.human, True, True, True, True, [], [])
        self.project = b.create_project(self.human, 'Project', ['owner'], context='大'*20000, idempotency_key='p')
        dependencies = [b.create_ticket(self.human, self.project['id'], 'Dependency '+str(i),
                                       idempotency_key='d'+str(i)) for i in range(30)]
        self.ticket = b.create_ticket(self.human, self.project['id'], 'Task', description='大'*20000,
            acceptance_criteria='é'*20000, depends_on=[v['id'] for v in dependencies], idempotency_key='t')
        device = b.create_device(self.human, 'Test device', [])
        raw = b.authenticate(device['token'])
        self.registration = b.register_agent(raw, 'test-session', 'Test', [], 'codex', False, 'fake')
        self.actor = b.actor_session(raw, 'test-session')
        self.scope = ContextScope('owner', self.registration['identity']['id'], self.project['id'], self.ticket['id'])

    def build(self):
        return build_context(lambda name, args: getattr(self.board, name)(self.actor, **args),
                             scope=self.scope, registration=self.registration)

    def test_real_human_requests_dependencies_and_large_unicode_history(self):
        b = self.board
        request = b.request_human_input(self.human, self.ticket['id'], 'owner', 'Quel scope ?', 'q')
        before = self.build()
        validate_artifact(before)
        self.assertEqual(before['data']['ticket']['human_requests'][0]['status'], 'open')
        b.resolve_human_request(self.human, request['id'], 'Ce ticket seulement')
        for i in range(45):
            b.post_message(self.human, self.ticket['thread_id'], '大'*20000, idempotency_key='m'+str(i))
        after = self.build()
        encoded, _ = validate_artifact(after)
        self.assertEqual(after['data']['ticket']['human_requests'][0]['answer'], 'Ce ticket seulement')
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertLessEqual(len(encoded.encode()), 48000)
        self.assertTrue(after['omissions'])
        self.assertEqual(len(after['data']['messages']), 40)

    def test_new_message_alone_invalidates_reviewed_snapshot(self):
        before = self.build()
        self.board.post_message(self.human, self.ticket['thread_id'], 'Decision retained', idempotency_key='m')
        after = self.build()
        self.assertNotEqual(before['fingerprint'], after['fingerprint'])
        self.assertEqual(after['data']['messages'][-1]['body'], 'Decision retained')
        self.assertEqual(before['sources']['ticket']['version'], after['sources']['ticket']['version'])
