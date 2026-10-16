"""Feed transport fields conform to the public MCP signatures; no real server."""
import ast
from copy import deepcopy
import json
from pathlib import Path
import unittest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.message_client import MessageBoardClient
from test_board_reader import Opener,URL


class MessageFeedTransportSchemaTests(unittest.TestCase):
    def test_open_read_checkpoint_send_only_supported_fields_and_full_read(self):
        schemas={
            'open_message_feed':{'session_id','idempotency_key','start_at','limit'},
            'read_message_feed':{'session_id','feed_id','view'},
            'checkpoint_message_feed':{'session_id','feed_id','delivery_id','expected_checkpoint'}}
        app=Path(__file__).resolve().parents[3]/'app.py'
        if app.is_file():
            functions={node.name:node for node in ast.walk(ast.parse(app.read_text()))
                       if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in schemas}
            self.assertEqual(set(functions),set(schemas))
            for name,fields in schemas.items():
                self.assertEqual({arg.arg for arg in functions[name].args.args+functions[name].args.kwonlyargs},fields)
        client=MessageBoardClient({'mcp_url':URL,'token':'SYNTHETIC','client':'codex',
            'identity_key':'fixture','name':'Fixture'},'explicit-session',3)
        client._initialized=client._registered=True
        opener=Opener();client._opener=opener
        inputs={
            'open_message_feed':{'idempotency_key':'fixed','start_at':'beginning','limit':50},
            'read_message_feed':{'feed_id':'a'*32},
            'checkpoint_message_feed':{'feed_id':'a'*32,'delivery_id':'b'*32,'expected_checkpoint':0}}
        original=deepcopy(inputs)
        for name,args in inputs.items():self.assertEqual(client.call(name,args),{'read':name})
        calls=[json.loads(request.data)['params'] for request,_ in opener.requests]
        self.assertEqual([call['name'] for call in calls],list(inputs))
        for call in calls:
            name=call['name'];args=call['arguments']
            self.assertEqual(set(args),schemas[name])
            expected={**inputs[name],'session_id':'explicit-session'}
            if name=='read_message_feed':expected['view']='full'
            self.assertEqual(args,expected)
        self.assertEqual(inputs,original)
        for name,args in inputs.items():
            with self.assertRaises(BoardReadError):client.call(name,{**args,'view':'compact'})
        self.assertEqual(len(opener.requests),3)


if __name__=='__main__':unittest.main()
