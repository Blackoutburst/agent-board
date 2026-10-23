"""Narrow development MCP contract; stub transport and synthetic token only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.dev_manifest import DevelopmentError


class DevelopmentClientTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').exists() else '/tmp')
        self.addCleanup(temp.cleanup);self.base=Path(temp.name).resolve()
        self.path=self.base/'connection.json'
        self.path.write_text(json.dumps({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_TOKEN',
            'client':'codex','identity_key':'fixture','name':'Fixture'}));self.path.chmod(0o600)
        self.client=DevelopmentBoardClient.from_connection_file(self.path,session_id='chosen-session')
        self.client._initialize=Mock();self.client._tool=Mock(return_value={'session':{'id':'chosen-session'}})
        self.client._rpc=Mock(return_value={'tools':[{'name':'get_development_ticket_frame','inputSchema':{
            'properties':{'session_id':{'type':'string'},'ticket_id':{'type':'string'}},
            'required':['session_id','ticket_id'],'additionalProperties':False}}]})

    def test_only_initial_explicit_registration_can_touch_registration(self):
        with self.assertRaises(BoardReadError):self.client.call('get_ticket',{'ticket_id':'ticket'})
        self.client._tool.assert_not_called()
        self.client.register_initial()
        self.assertEqual(self.client._tool.call_count,1)
        with self.assertRaises(BoardReadError):self.client.register()
        with self.assertRaises(BoardReadError):self.client.register_initial()
        self.client.call('get_work',{'project_id':'project','limit':1})
        self.assertEqual(self.client._tool.call_args.args,
                         ('get_work',{'project_id':'project','limit':1,'session_id':'chosen-session'}))

    def test_adopted_session_never_registers_or_accepts_queue_shell_messages_or_session_override(self):
        self.client.adopt_registration()
        for name,args in [('claim_next_ticket',{}),('post_message',{}),('read_inbox',{}),('shell.exec',{}),
                          ('get_work',{'project_id':'project','limit':2}),
                          ('get_work',{'project_id':'project','limit':True}),
                          ('get_ticket',{'ticket_id':'ticket','session_id':'other'})]:
            with self.subTest(name=name),self.assertRaises(BoardReadError):self.client.call(name,args)
        self.client._tool.assert_not_called()
        self.client.call('get_ticket',{'ticket_id':'ticket'})
        self.assertEqual(self.client._tool.call_count,1)

    def test_connection_hardlink_and_symlink_are_refused(self):
        alias=self.base/'alias.json';os.link(self.path,alias)
        with self.assertRaises(DevelopmentError):
            DevelopmentBoardClient.from_connection_file(self.path,session_id='session')
        alias.unlink();alias.symlink_to(self.path)
        with self.assertRaises((DevelopmentError,BoardReadError)):
            DevelopmentBoardClient.from_connection_file(alias,session_id='session')

    def test_review_self_unassignment_has_one_closed_explicit_shape(self):
        self.client.adopt_registration()
        args=dict(ticket_id='ticket',expected_version=7,idempotency_key='handoff',
            reason='Exact published delivery ready.',status='review',unassign_self=True)
        self.client.call('release_ticket',args)
        self.assertEqual(self.client._tool.call_args.args,
            ('release_ticket',{**args,'session_id':'chosen-session'}))
        self.assertEqual(args['expected_version'],7)

    def test_release_cannot_change_status_claim_scope_or_expand_the_worker_channel(self):
        from blackoutburst_agents.worker_client import WorkerBoardClient
        self.client.adopt_registration()
        args=dict(ticket_id='ticket',expected_version=7,idempotency_key='handoff',
            reason='Exact published delivery ready.',status='review',unassign_self=True)
        variants=[{**args,key:value} for key,value in [
            ('expected_version',True),('expected_version',0),('expected_version',2**63),
            ('unassign_self',False),('unassign_self',1),('status','todo'),('status','done'),
            ('reason',' '),('reason','\x00'),('reason','\ud800'),('idempotency_key',''),
            ('ticket_id',''),('session_id','other'),('assignee_agent_id','chosen-reviewer')]]
        variants += [{key:value for key,value in args.items() if key!=omitted} for omitted in args]
        for values in variants:
            with self.subTest(keys=list(values)),self.assertRaises(BoardReadError):
                self.client.call('release_ticket',values)
        self.client._tool.assert_not_called()
        with self.assertRaises(BoardReadError):
            WorkerBoardClient.call(self.client,'release_ticket',args)
        self.client._tool.assert_not_called()

    def test_credential_bytes_are_parsed_from_one_held_open(self):
        original=os.open;opens=[]
        def tracked(path,*args,**kwargs):
            if str(path).endswith('connection.json'):opens.append((str(path),kwargs.get('dir_fd')))
            return original(path,*args,**kwargs)
        with patch('os.open',side_effect=tracked):
            value=DevelopmentBoardClient.from_connection_file(self.path,session_id='session')
        self.assertEqual(len(opens),1)
        self.assertEqual(opens[0][0],'connection.json')
        self.assertIsNotNone(opens[0][1])
        self.assertEqual(value.endpoint,'https://board.test/mcp')

    def test_invalid_session_or_timeout_refuses_before_connection_read(self):
        for session,timeout in [('',3),('session',float('nan')),('session',True),('session',0)]:
            with self.subTest(session=session,timeout=timeout),self.assertRaises(BoardReadError):
                DevelopmentBoardClient.from_connection_file(self.base/'absent',session_id=session,timeout=timeout)
