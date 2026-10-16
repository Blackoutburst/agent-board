"""Explicit CLI routing/offline contracts with fixture-only execution."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_development_worker as fixture
from blackoutburst_agents.cli import main
from blackoutburst_agents.development_worker import DevelopmentBoardWorker


@unittest.skipIf(fixture.Board is None,'Board sources not bundled')
class DevelopmentCLITests(unittest.TestCase):
    setUp=fixture.DevelopmentWorkerTests.setUp
    backend_factory=fixture.DevelopmentWorkerTests.backend_factory
    make_worker=fixture.DevelopmentWorkerTests.make_worker
    transport=fixture.DevelopmentWorkerTests.transport
    prepare=fixture.DevelopmentWorkerTests.prepare
    action=fixture.DevelopmentWorkerTests.action
    successful_actions=fixture.DevelopmentWorkerTests.successful_actions
    read_ticket=fixture.DevelopmentWorkerTests.read_ticket

    def cli(self,*args):
        output=io.StringIO()
        with redirect_stdout(output):code=main(['development','--journal',str(self.journal.directory),*args])
        self.assertNotIn(str(self.base),output.getvalue())
        return code,json.loads(output.getvalue())

    def test_status_stop_and_inspect_never_construct_live_connection_or_runner(self):
        prepared=self.prepare();op=prepared['operation_id']
        with (patch('blackoutburst_agents.development_cli.DevelopmentBoardClient.from_connection_file') as connect,
              patch('blackoutburst_agents.development_cli.RunStore') as runs,
              patch('blackoutburst_agents.development_cli.RuntimeJournal') as runtime):
            self.assertEqual(self.cli('status','--operation',op)[1]['status'],'prepared')
            self.assertTrue(self.cli('stop','--operation',op)[1]['stop_requested'])
            target=self.base/'private-inspection.json'
            result=self.cli('inspect','--operation',op,'--output',str(target))
            self.assertEqual(result[0],0);self.assertEqual(target.stat().st_mode&0o777,0o600)
            self.assertIn('Change the fixture',target.read_text())
            connect.assert_not_called();runs.assert_not_called();runtime.assert_not_called()

    def test_prepare_cli_uses_exact_selected_scope_and_never_claims(self):
        instruction=self.base/'instruction.txt';instruction.write_text('Change the fixture and verify it.')
        def worker(journal,store,runtime,client,control):
            return self.make_worker(journal=journal,store=store,runtime_journal=runtime,client=client,control_client=control)
        with patch('blackoutburst_agents.development_cli.DevelopmentBoardClient.from_connection_file',return_value=self.client),\
             patch('blackoutburst_agents.development_cli.RuntimeBoardClient.from_worker',return_value=self.control),\
             patch('blackoutburst_agents.development_cli.DevelopmentBoardWorker',side_effect=worker):
            code,result=self.cli('prepare','--state',str(self.store.state_dir),'--runtime-journal',str(self.runtime.directory),
                '--connection-file',str(self.base/'not-read-fixture'),'--session',self.client.session_id,
                '--owner','owner','--agent',self.scope['agent_id'],'--project',self.project['id'],'--ticket',self.ticket['id'],
                '--request','cli-request','--model','fixture-model','--instruction-file',str(instruction),
                '--broker',str(self.broker_dir),'--test-backend',str(self.backend_dir),'--planner-policy',str(self.policy_dir))
        self.assertEqual((code,result['status']),(0,'prepared'),result)
        self.assertIsNone(self.read_ticket()['claimant']);self.assertFalse(self.inferences);self.assertFalse(self.control.calls)

    def test_inspection_cannot_write_into_checkout_or_empty_planner(self):
        prepared=self.prepare();op=prepared['operation_id']
        for directory in (self.root,self.planner_root):
            with self.subTest(directory=directory):
                code,result=self.cli('inspect','--operation',op,'--output',str(directory/'inspection.json'))
                self.assertEqual(code,2);self.assertEqual(result['status'],'blocked')
                self.assertFalse((directory/'inspection.json').exists())
