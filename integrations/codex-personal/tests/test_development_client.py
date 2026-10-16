"""Narrow development MCP contract; stub transport and synthetic token only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

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

    def test_invalid_session_or_timeout_refuses_before_connection_read(self):
        for session,timeout in [('',3),('session',float('nan')),('session',True),('session',0)]:
            with self.subTest(session=session,timeout=timeout),self.assertRaises(BoardReadError):
                DevelopmentBoardClient.from_connection_file(self.base/'absent',session_id=session,timeout=timeout)
