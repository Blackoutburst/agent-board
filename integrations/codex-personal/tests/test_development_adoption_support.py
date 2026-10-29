"""Actual RPC envelopes, per-client support caching, never permission caching."""
import json
import pytest
from blackoutburst_agents.board_reader import BoardReadError,PROTOCOL_VERSION
from blackoutburst_agents.development_client import DevelopmentBoardClient
from test_board_reader import Response
from test_development_ticket_frame_client import TOOL


class Wire:
    def __init__(self):self.calls=[];self.tools=[TOOL];self.denied=False
    def open(self,request,timeout):
        message=json.loads(request.data);method=message['method'];self.calls.append(message)
        if method=='initialize':value={'protocolVersion':PROTOCOL_VERSION}
        elif method=='tools/list':value={'tools':self.tools}
        else:
            assert method=='tools/call' and message['params']['name']=='get_development_ticket_frame'
            if self.denied:return Response(b'{}',url='https://board.test/mcp',status=403)
            value={'structuredContent':{'observed':len(self.calls)}}
        return Response(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':value}).encode(),url='https://board.test/mcp')
    def count(self,name):return sum(row['method']==name for row in self.calls)


def client(wire):
    value=DevelopmentBoardClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
        'client':'codex','identity_key':'explicit','name':'fixture'},'session',3)
    value._opener=wire;return value


def test_repeated_adoption_reuses_only_this_instances_observed_tool_support():
    wire=Wire();c=client(wire)
    try:
        c.adopt_registration();one=c.get_development_ticket_frame(ticket_id='ticket')
        c.adopt_registration();two=c.get_development_ticket_frame(ticket_id='ticket')
        assert wire.count('initialize')==wire.count('tools/list')==1
        assert wire.count('tools/call')==2 and one!=two
        assert all(row['params']['arguments']=={'session_id':'session','ticket_id':'ticket'}
            for row in wire.calls if row['method']=='tools/call')
    finally:c.close()


def test_new_client_cannot_adopt_prior_support_observation():
    wire=Wire();first=client(wire);first.adopt_registration();first.close()
    second=client(wire)
    try:
        second.adopt_registration()
        assert wire.count('initialize')==wire.count('tools/list')==2
    finally:second.close()


def test_close_dominates_cached_support_before_network():
    wire=Wire();c=client(wire);c.adopt_registration();c.close();before=len(wire.calls)
    for call in (c.adopt_registration,c.require_development_ticket_frame):
        with pytest.raises(BoardReadError,match='connection_closed'):call()
    assert len(wire.calls)==before and c._development_ticket_frame_ready is False


def test_explicit_refresh_invalidates_on_missing_catalogue_without_fallback():
    wire=Wire();c=client(wire);c.adopt_registration();wire.tools=[]
    try:
        with pytest.raises(BoardReadError,match='development_ticket_frame_unavailable'):c.require_development_ticket_frame()
        assert c._development_ticket_frame_ready is False and wire.count('tools/list')==2
        with pytest.raises(BoardReadError):c.get_development_ticket_frame(ticket_id='ticket')
        assert wire.count('tools/call')==0
        with pytest.raises(BoardReadError):c.adopt_registration()
        assert wire.count('tools/list')==3
    finally:c.close()


def test_unknown_or_missing_tool_on_new_client_never_becomes_ready():
    wire=Wire();wire.tools=[];c=client(wire)
    try:
        with pytest.raises(BoardReadError):c.adopt_registration()
        assert c._development_ticket_frame_ready is False and wire.count('tools/list')==1
        assert wire.count('tools/call')==0
    finally:c.close()


def test_cached_support_cannot_cache_authorization_or_suppress_403():
    wire=Wire();c=client(wire);c.adopt_registration();c.get_development_ticket_frame(ticket_id='ticket');wire.denied=True
    try:
        c.adopt_registration()
        with pytest.raises(BoardReadError,match='permission_denied'):c.get_development_ticket_frame(ticket_id='ticket')
        assert wire.count('tools/list')==1 and wire.count('tools/call')==2
    finally:c.close()
