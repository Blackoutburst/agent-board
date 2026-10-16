"""Real temporary Board Runtime/claim integration; all model/test work simulated."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from test_board_worker import Board
from test_runtime_guard_security import Client
from test_dev_planner import FakePolicy
import test_dev_planner as fixture
from test_development_broker import FakeTestBackend
from blackoutburst_agents.dev_broker import DevelopmentBroker
from blackoutburst_agents.dev_manifest import NamedTest, prepare_manifest
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runtime_journal import RuntimeJournal


@unittest.skipIf(Board is None, 'Board is not bundled in standalone adapter package')
class PlannerBoardTests(unittest.TestCase):
    action = fixture.PlannerTests.action
    transport = fixture.PlannerTests.transport
    successful_actions = fixture.PlannerTests.successful_actions

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.board = Board(self.base / 'board')
        self.board.ensure_owner('owner', 'Owner')
        self.human = {'kind': 'human', 'owner_id': 'owner'}
        self.board.set_policy(self.human, True, True, True, True, [], [])
        project = self.board.create_project(self.human, 'Fixture', ['owner'], idempotency_key='project')
        ticket = self.board.create_ticket(self.human, project['id'], 'Fixture', idempotency_key='ticket')
        device = self.board.create_device(self.human, 'Fixture', [])
        raw = self.board.authenticate(device['token'])
        registration = self.board.register_agent(raw, Client.session_id, 'Fixture', [], 'codex', False, 'fixture')
        self.actor = {**raw, 'session_id': Client.session_id}
        self.board.claim_ticket(self.actor, ticket['id'], ticket['version'])
        self.scope = dict(owner_id='owner', agent_id=registration['identity']['id'],
            project_id=project['id'], ticket_id=ticket['id'], board_endpoint=Client.endpoint,
            board_session_id=Client.session_id)
        self.grant_scope = {key: self.scope[key] for key in ('owner_id', 'agent_id', 'project_id', 'ticket_id')}
        self.grant_scope.update(device_id=registration['identity']['device_id'], session_id=Client.session_id,
                                profile_version=registration['profile_version'])
        self.grant = dict(scope=self.grant_scope, claim_id='fixture-claim', owner_enabled=True,
            profile_enabled=True, project_allowed=True, claim_current=True,
            actions=['repo.read_file', 'repo.patch_file', 'repo.run_test'], resources=['repo:fixture'])
        self.root = self.base / 'checkout'; self.root.mkdir(mode=0o700)
        (self.root / 'main.py').write_text('value = 1\n')
        self.planner_root = self.base / 'empty'; self.planner_root.mkdir(mode=0o700)
        private = self.base / 'broker'
        self.backend = FakeTestBackend()
        prepare_manifest(private, root=self.root, base_sha='a' * 40, scope=self.grant_scope,
            paths=['main.py'], resource='repo:fixture', authorize_scope=lambda *_: True,
            tests=[NamedTest('unit', backend_id=self.backend.backend_id)])
        self.broker = DevelopmentBroker(private, authorize_scope=lambda *_: True,
                                       enabled=True, test_backend=self.backend)
        self.addCleanup(self.broker.close)
        self.store = RunStore(self.base / 'runs'); self.addCleanup(self.store.close)
        self.runtime = RuntimeJournal(self.base / 'runtime'); self.addCleanup(self.runtime.close)
        self.control = Client(self.board, self.actor)
        self.actions, self.transports, self.during_model = [], [], None
        self.planner = DevelopmentPlanner(broker=self.broker, store=self.store, runtime_journal=self.runtime,
            control_client=self.control, board_scope=self.scope, planner_policy=FakePolicy(self.planner_root),
            authorization=self.authorization, claim_id='fixture-claim', instruction='Change fixture.',
            model='fixture-model', enabled=True, _test_anchor=self.base / 'anchor', _transport=self.transport, _allow_fixture_backend=True)
        self.addCleanup(self.planner.close)

    def authorization(self):
        # Fixture adapter deliberately queries live Board claim/profile, rather
        # than deriving authority from ticket title, comments or model text.
        grant = deepcopy(self.grant)
        ticket = self.board.get_ticket(self.actor, self.scope['ticket_id'])['ticket']
        profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
        grant['claim_current'] = (ticket['claimant'] is not None and
            ticket['claimant']['session_id'] == self.scope['board_session_id'])
        grant['profile_enabled'] = profile['status'] == 'active'
        if profile['version'] != self.grant_scope['profile_version']:
            grant['scope']['profile_version'] = profile['version']
        return grant

    def test_capacity_one_handles_alternating_model_tool_without_nested_reservations(self):
        self.successful_actions()
        result = self.planner.run()
        self.assertEqual(result['status'], 'review_ready', result)
        runtime = self.board.get_runtime(self.human)
        self.assertEqual(runtime['active_count'], 0)
        self.assertEqual(len(runtime['executions']), 8)
        self.assertEqual({r['provider'] for r in runtime['executions']}, {'codex_chatgpt', 'local_broker'})
        self.assertTrue(all(r['cleanup_confirmed'] for r in runtime['executions']))
        self.assertEqual(len(self.backend.calls), 1)
        # Controller has no publication/claim creation/release side effect.
        self.assertNotIn('update_ticket', [name for name, _ in self.control.calls])
        self.assertIsNotNone(self.board.get_ticket(self.human, self.scope['ticket_id'])['ticket']['claimant'])

    def test_real_owner_pause_between_model_finish_and_tool_refuses_admission(self):
        self.actions = [self.action('read', path='main.py')]
        original = self.control.call
        def pause(name, args):
            result = original(name, args)
            if name == 'finish_execution':
                limits = self.board.get_runtime(self.human)['limits']
                self.board.update_runtime_limits(self.human, limits['version'], 1, True)
            return result
        self.control.call = pause
        result = self.planner.run()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(len(self.board.get_runtime(self.human)['executions']), 1)
        self.assertFalse(self.backend.calls)

    def test_real_profile_pause_after_model_preserves_claim_and_no_tool(self):
        self.actions = [self.action('read', path='main.py')]
        def pause(_):
            profile = self.board.get_agent_profile(self.human, self.scope['agent_id'])
            self.board.update_agent_profile(self.human, profile['id'], profile['version'], status='paused')
        self.during_model = pause
        result = self.planner.run()
        self.assertEqual(result['status'], 'needs_review')
        self.assertEqual(len(self.transports), 1)
        self.assertEqual(len(self.board.get_runtime(self.human)['executions']), 1)
        self.assertFalse(self.backend.calls)
