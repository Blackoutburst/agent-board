"""Inspectable local owner memory commands and cached-run reset fencing."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from blackoutburst_agents.cli import main
from blackoutburst_agents.context import ContextScope,build_context
from blackoutburst_agents.context_cache import ContextCache
from blackoutburst_agents.decision_memory import DecisionMemory,memory_scope
from test_context import registration
from test_decision_memory import ExactFixture


class DecisionCliTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve();self.state=self.root/'memory'
        self.scope=ContextScope('alice','agent-a','project-a','ticket-a')
        self.board=ExactFixture(80);self.reg=registration()
        self.reader=SimpleNamespace(endpoint='https://board.test/mcp',register=lambda:self.reg,call=self.board)
        self.summary=self.root/'summary.txt';self.summary.write_text('PRIVATE_LOCAL_DECISION');self.summary.chmod(0o600)
        self.common=['--owner','alice','--agent','agent-a','--project','project-a','--ticket','ticket-a','--endpoint',self.reader.endpoint]
        self.pin=['memory','--state',str(self.state),'pin',*self.common,'--connection-file',str(self.root/'ignored.json'),
                  '--session','session-a','--thread','thread-a','--message','message-1','--summary-file',str(self.summary),'--revision','0']

    def invoke(self,args):
        output=io.StringIO()
        with redirect_stdout(output):code=main(args)
        return code,json.loads(output.getvalue()),output.getvalue()

    def prepare(self):
        with patch('blackoutburst_agents.decision_cli.BoardReader.from_connection_file',return_value=self.reader):
            result=self.invoke(self.pin)
        self.assertEqual(result[0],0);return result

    def test_pin_inspect_reset_are_private_and_have_explicit_source_reads_only(self):
        code,result,stdout=self.prepare()
        self.assertNotIn('PRIVATE',stdout);self.assertNotIn(str(self.root),stdout)
        self.assertEqual([name for name,_ in self.board.calls],['get_ticket','get_message'])
        target=self.root/'inspect.json'
        with patch('blackoutburst_agents.decision_cli.BoardReader.from_connection_file',side_effect=AssertionError('network forbidden')):
            code,output,stdout=self.invoke(['memory','--state',str(self.state),'inspect',*self.common,'--output',str(target)])
            self.assertEqual(code,0);self.assertNotIn('PRIVATE',stdout)
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            self.assertIn('PRIVATE_LOCAL_DECISION',target.read_text())
            code,reset,stdout=self.invoke(['memory','--state',str(self.state),'reset',*self.common,'--revision','1'])
            self.assertEqual(code,0);self.assertEqual(reset['generation'],2)
        with DecisionMemory(self.state,create=False) as memory:
            self.assertEqual(memory.inspect(memory_scope(self.reader.endpoint,self.scope))['decisions'],[])

    def test_summary_symlink_is_refused_and_memory_scope_remains_empty(self):
        link=self.root/'link';link.symlink_to(self.summary)
        args=list(self.pin);args[args.index('--summary-file')+1]=str(link)
        with patch('blackoutburst_agents.decision_cli.BoardReader.from_connection_file',return_value=self.reader):
            code,result,stdout=self.invoke(args)
        self.assertEqual(code,2);self.assertEqual(result['reason'],'decision_summary_invalid')
        self.assertEqual(self.board.calls,[])
        self.assertNotIn('PRIVATE',stdout)

    def test_context_prepare_needs_explicit_memory_path_and_preserves_current_fingerprint(self):
        self.prepare();cache=self.root/'cache'
        args=['context','--state',str(cache),'prepare','--owner','alice','--agent','agent-a','--project','project-a',
              '--ticket','ticket-a','--connection-file','ignored','--session','session-a','--decision-memory',str(self.state)]
        with patch('blackoutburst_agents.context_cli.BoardReader.from_connection_file',return_value=self.reader):
            code,receipt,stdout=self.invoke(args)
        self.assertEqual(code,0);self.assertNotIn('PRIVATE',stdout)
        with ContextCache(cache) as contexts:
            artifact=contexts.get(receipt['context_key'])
            self.assertEqual(artifact['data']['decisions'][0]['summary'],'PRIVATE_LOCAL_DECISION')
            self.assertNotIn(str(self.state),json.dumps(artifact))

    def test_reset_blocks_runner_before_any_new_or_cached_model_turn(self):
        self.prepare();cache=self.root/'cache'
        with DecisionMemory(self.state,create=False) as memory,ContextCache(cache) as contexts:
            artifact=build_context(self.board,scope=self.scope,registration=self.reg,decision_memory=memory,board_endpoint=self.reader.endpoint)
            receipt=contexts.save(artifact)
            memory.forget(memory_scope(self.reader.endpoint,self.scope),expected_revision=1)
        code,work,_=self.invoke(['runner','--state',str(self.root/'runner'),'create','--owner','alice','--agent','agent-a',
            '--project','project-a','--ticket','ticket-a','--workspace',str(self.root),'--codex',sys.executable])
        self.assertEqual(code,0)
        prompt=self.root/'prompt';prompt.write_text('Summarize the decision.')
        args=['runner','--state',str(self.root/'runner'),'turn','--work',work['work_id'],'--request','once','--prompt-file',str(prompt),
            '--context-state',str(cache),'--context-key',receipt['context_key'],'--context-fingerprint',receipt['fingerprint'],
            '--board-connection','ignored','--board-session','session-a','--decision-memory',str(self.state)]
        with patch('blackoutburst_agents.context_cli.fetch_context',return_value=artifact) as fetch, \
                patch('blackoutburst_agents.runner_cli.PersonalRunner') as runner:
            code,result,stdout=self.invoke(args)
        self.assertEqual(code,2);self.assertEqual(result['reason'],'decision_memory_changed')
        fetch.assert_not_called();runner.assert_not_called();self.assertNotIn('PRIVATE',stdout)

    def test_memory_context_cannot_be_used_without_explicit_store(self):
        self.prepare();cache=self.root/'cache'
        with DecisionMemory(self.state,create=False) as memory,ContextCache(cache) as contexts:
            artifact=build_context(self.board,scope=self.scope,registration=self.reg,decision_memory=memory,board_endpoint=self.reader.endpoint)
            receipt=contexts.save(artifact)
        args=['context','--state',str(cache),'prompt','--key',receipt['context_key'],'--fingerprint',receipt['fingerprint'],
            '--connection-file','ignored','--session','session-a','--instruction-file',str(self.summary),'--output',str(self.root/'out')]
        with patch('blackoutburst_agents.context_cli.fetch_context') as fetch:
            code,result,_=self.invoke(args)
        self.assertEqual(code,2);self.assertEqual(result['reason'],'decision_memory_required');fetch.assert_not_called()
