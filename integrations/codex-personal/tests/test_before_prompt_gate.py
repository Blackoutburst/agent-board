"""Real production transport path with synthetic CLI, no account or model."""
from copy import deepcopy
from functools import partial
import os
import time
import unittest
from unittest.mock import patch

import test_codex_turn as transport_fixture
import test_dev_planner as planner_fixture
from blackoutburst_agents.codex_turn import execute_turn
from blackoutburst_agents.dev_manifest import DevelopmentError
from blackoutburst_agents.dev_planner_journal import PlannerBudgets
from blackoutburst_agents.planner_policy import PlannerPolicy
from blackoutburst_agents.subscription_probe import ProbeSettings,inspect_cli


class BeforePromptGateTests(unittest.TestCase):
    create=planner_fixture.PlannerTests.create
    transport=planner_fixture.PlannerTests.transport
    action=planner_fixture.PlannerTests.action
    fake_cli=transport_fixture.CodexTurnTests.fake_cli

    def setUp(self):
        planner_fixture.PlannerTests.setUp(self)
        self.directory=self.base/'fake-cli';self.directory.mkdir()

    def expired_authority_after_doctor(self,kind):
        executable=self.fake_cli();self.policy.executable=str(executable)
        native_policy=object.__new__(PlannerPolicy)
        stale={'context':False};writes=[];original_write=os.write
        def authority():
            if stale['context']:raise DevelopmentError('synthetic_context_changed')
            return deepcopy(self.grant)
        def doctor(settings):
            value=inspect_cli(settings)
            if kind=='context':stale['context']=True
            else:self.grant['resources']=[]
            return value
        def write(fd,data):
            if b'"one_action_only":true' in data:writes.append(bytes(data))
            return original_write(fd,data)
        with patch.object(PlannerPolicy,'validate_for',return_value=['-c','default_permissions="fixture"']), \
                patch('blackoutburst_agents.codex_turn.inspect_cli',side_effect=doctor), \
                patch('blackoutburst_agents.codex_turn.os.write',side_effect=write):
            result=self.create(authorization=authority,budgets=PlannerBudgets(max_turns=1),
                _transport=partial(execute_turn,planner_policy=native_policy)).run()
        self.assertEqual(result['status'],'needs_review')
        self.assertEqual(writes,[])
        self.assertEqual(len(self.control.rows),1)
        self.assertTrue(all(row['state']=='finished' and row['cleanup_confirmed'] for row in self.control.rows.values()))
        self.assertFalse(self.backend.calls)
        self.assertEqual((self.root/'main.py').read_text(),'value = 1\n')

    def test_native_planner_path_rechecks_grants_after_doctor_before_prompt(self):
        self.expired_authority_after_doctor('grants')

    def test_native_planner_path_rechecks_context_after_doctor_before_prompt(self):
        self.expired_authority_after_doctor('context')

    def test_slow_before_prompt_cannot_write_after_transport_deadline(self):
        executable=self.fake_cli();native_policy=object.__new__(PlannerPolicy)
        original_write=os.write;original_clock=time.monotonic;tick=[None];writes=[]
        def clock():return original_clock() if tick[0] is None else tick[0]
        def gate():tick[0]=original_clock()+100
        def write(fd,data):
            if data==b'SYNTHETIC_PROMPT':writes.append(bytes(data))
            return original_write(fd,data)
        with patch.object(PlannerPolicy,'validate_for',return_value=['-c','default_permissions="fixture"']), \
                patch('blackoutburst_agents.codex_turn.time.monotonic',side_effect=clock), \
                patch('blackoutburst_agents.codex_turn.os.write',side_effect=write):
            result=execute_turn(ProbeSettings(executable,timeout_seconds=2,metadata_timeout_seconds=2),
                'SYNTHETIC_PROMPT',self.planner_root,planner_policy=native_policy,before_prompt=gate)
        self.assertEqual(result['reason'],'timeout')
        self.assertTrue(result['verification']['cleanup_confirmed'])
        self.assertEqual(writes,[])

    def test_false_guard_result_refuses_prompt_instead_of_becoming_permission(self):
        executable=self.fake_cli();native_policy=object.__new__(PlannerPolicy)
        with patch.object(PlannerPolicy,'validate_for',return_value=['-c','default_permissions="fixture"']):
            result=execute_turn(ProbeSettings(executable,timeout_seconds=2,metadata_timeout_seconds=2),
                'SYNTHETIC_PROMPT',self.planner_root,planner_policy=native_policy,before_prompt=lambda:False)
        self.assertEqual(result['reason'],'callback_failed')
        self.assertTrue(result['verification']['cleanup_confirmed'])
