"""Independent boundary checks with temporary Board and simulated transports."""
import unittest

import test_dev_planner as planner_fixture
import test_family_worker as family_fixture
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.dev_planner_journal import PlannerBudgets


@unittest.skipIf(family_fixture.Board is None, 'Board sources not bundled')
class FamilyRuntimePollingReviewTests(unittest.TestCase):
    setUp = family_fixture.FamilyWorkerTests.setUp
    make_manifest = family_fixture.FamilyWorkerTests.make_manifest
    ready = family_fixture.FamilyWorkerTests.ready
    transport = family_fixture.FamilyWorkerTests.transport
    ticket = family_fixture.FamilyWorkerTests.ticket

    def install_planners(self):
        self.tick = [0.0]
        self.planners = {}
        for slot, worker in self.workflows.items():
            def factory(chosen=slot, **options):
                planner = DevelopmentPlanner(**options)
                planner.model_guard.clock = lambda: self.tick[0]
                self.planners[chosen] = planner
                return planner
            worker.planner_factory = factory

    def assert_no_tool_effect(self, result):
        self.assertEqual(result['status'], 'needs_review', result)
        self.assertEqual(len(self.inferences), 1)
        self.assertFalse(any(backend.calls for backend in self.backend.values()))
        self.assertEqual((self.roots['child_0'] / 'left.py').read_text(), 'value = 1\n')
        with self.board._connection() as db:
            rows = db.execute('SELECT provider FROM runtime_executions').fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row['provider'] == 'codex_chatgpt' for row in rows))
        self.assertFalse(self.journal.read()['payload'].get('accepted'))

    def remote_revocation(self, change):
        self.ready()
        self.install_planners()
        def poll(slot, callbacks):
            self.assertEqual(slot, 'child_0')
            change()
            client_counts = {key: len(worker.client.calls) for key, worker in self.workflows.items()}
            control = self.workflows[slot].control
            before = len(control.calls)
            self.tick[0] = 5.0
            self.assertTrue(callbacks['should_cancel']())
            self.assertEqual([name for name, _ in control.calls[before:]], ['heartbeat_execution'])
            self.assertEqual(client_counts, {key: len(worker.client.calls) for key, worker in self.workflows.items()})
            after = len(control.calls)
            self.assertTrue(callbacks['should_cancel']())
            self.assertEqual(len(control.calls), after)
            # The simulated transport still returns an action; the full result
            # boundary must refuse it without any broker admission or effect.
        self.after_model = poll
        self.assert_no_tool_effect(self.worker.run())

    def test_remote_resource_revocation_is_seen_by_runtime_without_snapshot_polling(self):
        self.remote_revocation(lambda: self.board.set_policy(self.human, True, True, True, True,
            ['repo.read_file', 'repo.patch_file', 'repo.run_test'], []))

    def test_remote_profile_change_is_seen_by_runtime_without_reregistering(self):
        def change():
            profile = self.registrations['parent']['profile']
            self.board.update_agent_profile(self.human, profile['id'], profile['version'],
                instructions='A different explicit instruction')
        self.remote_revocation(change)

    def test_remote_device_revocation_cancels_without_context_read(self):
        self.remote_revocation(lambda: self.board.revoke_device(self.human,
            self.registrations['parent']['identity']['device_id']))

    def test_lost_parent_claim_is_seen_by_child_runtime_control(self):
        def change():
            with self.board._tx() as db:
                db.execute("UPDATE tickets SET claimant=NULL,lease_expires_at=NULL,status='todo',version=version+1 WHERE id=?",
                    (self.scope['ticket_id'],))
        self.remote_revocation(change)

    def test_remote_family_stop_is_seen_by_runtime_without_snapshot_polling(self):
        def change():
            state = self.journal.read()
            family = self.board.get_delegation_family(self.human, state['payload']['family_id'])
            self.board.stop_delegation_family(self.human, family['id'], family['revision'])
        self.remote_revocation(change)

    def test_parent_text_drift_during_model_is_rejected_before_action(self):
        self.ready()
        self.install_planners()
        def poll(slot, callbacks):
            # Simulate changed persisted source while preserving the claim and
            # family metadata. The normal UI prevents this in-progress edit.
            with self.board._tx() as db:
                db.execute('UPDATE tickets SET description=?,version=version+1 WHERE id=?',
                    ('Changed source after prompt', self.scope['ticket_id']))
            before = {key: len(worker.client.calls) for key, worker in self.workflows.items()}
            self.assertFalse(callbacks['should_cancel']())
            self.assertEqual(before, {key: len(worker.client.calls) for key, worker in self.workflows.items()})
        self.after_model = poll
        self.assert_no_tool_effect(self.worker.run())


class ForcedPromptBoundaryReviewTests(unittest.TestCase):
    setUp = planner_fixture.PlannerTests.setUp
    create = planner_fixture.PlannerTests.create
    transport = planner_fixture.PlannerTests.transport
    action = planner_fixture.PlannerTests.action

    def test_permission_change_after_process_callback_refuses_before_prompt(self):
        observed = []
        def transport(settings, prompt, workspace, **callbacks):
            callbacks['on_started'](999999999)
            observed.append('process_recorded')
            self.grant['resources'] = []
            callbacks['before_prompt']()
            observed.append('prompt_sent')
            raise AssertionError('A revoked prompt boundary must refuse')
        planner = self.create(_transport=transport, budgets=PlannerBudgets(max_turns=1))
        result = planner.run()
        self.assertEqual(result['status'], 'needs_review', result)
        self.assertEqual(observed, ['process_recorded'])
        self.assertFalse(self.backend.calls)
        self.assertEqual(len(self.control.rows), 1)
        self.assertTrue(all(row['provider'] == 'codex_chatgpt' for row in self.control.rows.values()))


if __name__ == '__main__':
    unittest.main()
