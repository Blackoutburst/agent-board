"""Cross-mode cleanup dispatch with temporary Board and fake model results."""
from contextlib import redirect_stdout
import io
import json
import unittest
import uuid
from unittest.mock import patch

import test_message_host as message_fixture
import test_runtime_integration as legacy_fixture
import test_development_worker as development_fixture
from blackoutburst_agents.cli import main


class MessageCleanupDispatchTests(unittest.TestCase):
    setUp=message_fixture.MessageHostTests.setUp
    resources=message_fixture.MessageHostTests.resources
    transport=message_fixture.MessageHostTests.transport
    post=message_fixture.MessageHostTests.post
    enable=message_fixture.MessageHostTests.enable
    tick_host=message_fixture.MessageHostTests.tick_host

    def arguments(self):
        self.post();self.enable();self.control.lost.add('finish_execution');self.tick_host()
        operation=self.message_runtime.list_status()[0]
        return ['runtime','--journal',str(self.message_runtime.directory),'reconcile',
            '--operation',operation['id'],'--state',str(self.message_runs.state_dir),
            '--connection-file',self.config['targets'][0]['connection_file']]

    def test_message_refuses_development_parent_before_connection_or_lookup(self):
        arguments=self.arguments();before=list(self.control.calls);output=io.StringIO()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file') as legacy, \
             patch('blackoutburst_agents.message_runtime.MessageRuntimeClient.from_connection_file') as message, \
             patch('blackoutburst_agents.runtime_cli.DevelopmentRuntimeGuard') as development,redirect_stdout(output):
            code=main(arguments+['--development-operation',str(uuid.uuid4())])
        self.assertEqual(code,2)
        self.assertEqual(json.loads(output.getvalue())['reason'],'runtime_development_scope_mismatch')
        legacy.assert_not_called();message.assert_not_called();development.assert_not_called()
        self.assertEqual(self.control.calls,before);self.assertEqual(self.calls,1)

    def test_message_cleanup_uses_only_message_guard_without_new_model(self):
        arguments=self.arguments();before=list(self.control.calls);output=io.StringIO()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file') as legacy, \
             patch('blackoutburst_agents.message_runtime.MessageRuntimeClient.from_connection_file',return_value=self.control), \
             patch('blackoutburst_agents.runtime_cli.DevelopmentRuntimeGuard') as development,redirect_stdout(output):
            code=main(arguments)
        self.assertEqual(code,0,output.getvalue());legacy.assert_not_called();development.assert_not_called()
        self.assertEqual(self.control.calls[len(before):],['lookup_execution'])
        self.assertEqual(self.calls,1);self.assertNotIn('PRIVATE',output.getvalue())


class LegacyCleanupDispatchTests(unittest.TestCase):
    setUp=legacy_fixture.RuntimeIntegrationTests.setUp
    transport=legacy_fixture.RuntimeIntegrationTests.transport
    run_worker=legacy_fixture.RuntimeIntegrationTests.run_worker

    def test_legacy_cleanup_keeps_default_guard_and_never_constructs_message_or_development(self):
        self.control.lost.add('finish_execution');self.run_worker()
        operation=self.runtime.list_status()[0];before=list(self.control.calls);output=io.StringIO()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',return_value=self.control), \
             patch('blackoutburst_agents.message_runtime.MessageRuntimeClient.from_connection_file') as message, \
             patch('blackoutburst_agents.runtime_cli.DevelopmentRuntimeGuard') as development,redirect_stdout(output):
            code=main(['runtime','--journal',str(self.runtime.directory),'reconcile',
                '--operation',operation['id'],'--state',str(self.store.state_dir),
                '--connection-file',str(self.root/'unused-connection')])
        self.assertEqual(code,0,output.getvalue());message.assert_not_called();development.assert_not_called()
        self.assertEqual(self.control.calls[len(before):],['lookup_execution'])
        self.assertEqual(self.calls,1)


class DevelopmentCleanupDispatchTests(unittest.TestCase):
    setUp=development_fixture.DevelopmentWorkerTests.setUp
    backend_factory=development_fixture.DevelopmentWorkerTests.backend_factory
    make_worker=development_fixture.DevelopmentWorkerTests.make_worker
    prepare=development_fixture.DevelopmentWorkerTests.prepare
    transport=development_fixture.DevelopmentWorkerTests.transport
    action=development_fixture.DevelopmentWorkerTests.action
    successful_actions=development_fixture.DevelopmentWorkerTests.successful_actions

    def test_development_cleanup_requires_explicit_parent_and_never_uses_message_channel(self):
        self.successful_actions();prepared=self.prepare();self.control.lost.add('finish_execution')
        self.worker.run(prepared['operation_id']);operation=self.runtime.list_status()[0]
        args=['runtime','--journal',str(self.runtime.directory),'reconcile','--operation',operation['id'],
            '--state',str(self.store.state_dir),'--connection-file',str(self.base/'unused-connection')]
        before=list(self.control.calls);output=io.StringIO()
        with patch('blackoutburst_agents.runtime_cli.RuntimeBoardClient.from_connection_file',return_value=self.control), \
             patch('blackoutburst_agents.message_runtime.MessageRuntimeClient.from_connection_file') as message,redirect_stdout(output):
            self.assertEqual(main(args),2)
            self.assertEqual(self.control.calls,before)
            self.assertEqual(main(args+['--development-operation',prepared['operation_id']]),0)
        message.assert_not_called()
        self.assertEqual(self.control.calls[len(before):],['lookup_execution'])
        self.assertEqual(len(self.inferences),1)


if __name__=='__main__':unittest.main()
