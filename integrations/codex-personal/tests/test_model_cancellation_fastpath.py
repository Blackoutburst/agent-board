"""Local polling and forced boundaries; fake models and temporary Board only."""
from copy import deepcopy
import unittest

import test_dev_planner as planner_fixture
import test_family_worker as family_fixture
from blackoutburst_agents.development_worker import DevelopmentBoardWorker
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_planner import DevelopmentPlanner
from blackoutburst_agents.dev_planner_journal import PlannerBudgets


class PlannerCancellationTests(unittest.TestCase):
    setUp=planner_fixture.PlannerTests.setUp
    create=planner_fixture.PlannerTests.create
    transport=planner_fixture.PlannerTests.transport
    action=planner_fixture.PlannerTests.action

    def one_turn(self,**options):
        self.actions=[self.action('read',path='main.py')]
        return self.create(budgets=PlannerBudgets(max_turns=1),**options)

    def test_hundred_polls_do_not_fetch_grants_and_heartbeat_is_five_seconds(self):
        reads=[];tick=[0.0]
        def authority():reads.append(1);return deepcopy(self.grant)
        planner=self.one_turn(authorization=authority);planner.model_guard.clock=lambda:tick[0]
        def poll(callbacks):
            count=len(reads);calls=len(self.control.calls)
            for number in range(100):
                tick[0]=number*0.05
                self.assertFalse(callbacks['should_cancel']())
            self.assertEqual(len(reads),count)
            self.assertEqual(len(self.control.calls),calls)
            tick[0]=5.0;self.assertFalse(callbacks['should_cancel']())
            self.assertEqual([name for name,_ in self.control.calls[calls:]],['heartbeat_execution'])
            self.assertEqual(len(reads),count)
        self.during_model=poll;planner.run()

    def test_runtime_control_failure_is_sticky_without_refetching_context(self):
        tick=[0.0];planner=self.one_turn();planner.model_guard.clock=lambda:tick[0]
        def poll(callbacks):
            self.control.lost.add('heartbeat_execution');tick[0]=5.0
            self.assertTrue(callbacks['should_cancel']())
            calls=len(self.control.calls);tick[0]=100.0
            for _ in range(100):self.assertTrue(callbacks['should_cancel']())
            self.assertTrue(planner.model_guard.control_failed)
            self.assertEqual(len(self.control.calls),calls)
            # A compliant transport stops on cancellation. Simulate its blocked
            # return by failing before it can emit a fake completed action.
            raise RuntimeError('synthetic cancelled transport')
        self.during_model=poll;result=planner.run()
        self.assertEqual(result['status'],'needs_review')
        self.assertFalse(self.backend.calls)
        self.assertEqual(len(self.control.rows),1)

    def local_failure(self,control):
        local={'fail':False}
        def allowed(**_):
            if local['fail']:raise RuntimeError('synthetic local failure')
            return True
        planner=self.one_turn(local_guard=allowed)
        def poll(callbacks):
            state=planner.journal.read();self.clock+=1
            self.assertFalse(callbacks['should_cancel']())
            if control=='rollback':self.clock-=0.5
            elif control=='deadline':self.clock=state['deadline']
            else:local['fail']=True
            self.assertTrue(callbacks['should_cancel']())
        self.during_model=poll
        result=planner.run();self.assertEqual(result['status'],'needs_review')

    def test_local_clock_rollback_cancels(self):self.local_failure('rollback')
    def test_local_deadline_cancels(self):self.local_failure('deadline')
    def test_local_callback_exception_cancels(self):self.local_failure('exception')

    def test_grant_changed_during_model_is_refused_before_tool_admission(self):
        planner=self.one_turn()
        def revoke(callbacks):
            self.grant['resources']=[]
            self.assertFalse(callbacks['should_cancel']())
        self.during_model=revoke;result=planner.run()
        self.assertEqual(result['reason'],'planner_authorization_changed')
        self.assertEqual(len(self.control.rows),1)
        self.assertFalse(self.backend.calls)

    def test_stop_or_deadline_during_full_snapshot_is_checked_after_rpc(self):
        calls=[]
        def authority():
            calls.append(1)
            if len(calls)==2:self.clock=1000000
            return deepcopy(self.grant)
        result=self.one_turn(authorization=authority).run()
        self.assertEqual(result['reason'],'planner_wall_budget_exhausted')
        self.assertEqual(self.transports,[])


@unittest.skipIf(family_fixture.Board is None,'Board sources not bundled')
class FamilyCancellationTests(unittest.TestCase):
    setUp=family_fixture.FamilyWorkerTests.setUp
    make_manifest=family_fixture.FamilyWorkerTests.make_manifest
    ready=family_fixture.FamilyWorkerTests.ready
    transport=family_fixture.FamilyWorkerTests.transport
    ticket=family_fixture.FamilyWorkerTests.ticket

    def test_hundred_family_polls_have_no_board_rpc_and_stop_local_is_immediate(self):
        self.ready();planners={}
        for slot,worker in self.workflows.items():
            def factory(chosen=slot,**options):
                planner=DevelopmentPlanner(**options);planner.model_guard.clock=lambda:0.0
                planners[chosen]=planner;return planner
            worker.planner_factory=factory
        def poll(slot,callbacks):
            counts={key:len(w.client.calls) for key,w in self.workflows.items()}
            runtime={key:len(w.control.calls) for key,w in self.workflows.items()}
            for _ in range(100):self.assertFalse(callbacks['should_cancel']())
            self.assertEqual(counts,{key:len(w.client.calls) for key,w in self.workflows.items()})
            self.assertEqual(runtime,{key:len(w.control.calls) for key,w in self.workflows.items()})
            self.journal.request_stop()
            self.assertTrue(callbacks['should_cancel']())
            self.assertEqual(counts,{key:len(w.client.calls) for key,w in self.workflows.items()})
        self.after_model=poll;result=self.worker.run()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(len(self.inferences),1)
        self.assertFalse(any(backend.calls for backend in self.backend.values()))
        self.assertEqual((self.roots['child_0']/'left.py').read_text(),'value = 1\n')

    def test_family_worker_without_explicit_local_guard_refuses(self):
        self.ready();state=self.journal.read();base=self.workflows['child_0']
        delegated=self.worker._worker(state,'child_0')
        with self.assertRaises(DevelopmentError):
            DevelopmentBoardWorker(base.journal,base.store,base.runtime,base.client,base.control,
                controller_scope=delegated.controller_scope,controller_guard=delegated.controller_guard,
                delegation_guard=delegated.delegation_guard,fence_intent=delegated.fence_intent)

    def test_local_family_deadline_and_rollback_refuse_without_rpc(self):
        self.ready();state=self.journal.read();worker=self.worker._worker(state,'child_0')
        operation=state['workflow_ids']['child_0'];counts=[len(w.client.calls) for w in self.workflows.values()]
        self.now+=1;self.assertTrue(worker._local_control(operation))
        self.now-=0.5
        with self.assertRaises(DevelopmentError):worker._local_control(operation)
        self.now=state['configuration']['opening']['limits']['deadline']
        with self.assertRaises(DevelopmentError):worker._local_control(operation)
        self.assertEqual(counts,[len(w.client.calls) for w in self.workflows.values()])
