"""Real MCP client+RPC codec+Board; in-memory HTTP and GitHub only."""
from copy import deepcopy
import json
import pytest
from unittest.mock import patch

from blackoutburst_agents.board_reader import PROTOCOL_VERSION
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.work_journal import WorkJournal
from development_ticket_frames import get_development_ticket_frame
from board import BoardError
from test_board_reader import Response
from test_development_ticket_frame_client import TOOL
import test_development_pool_delivery as helpers


@pytest.mark.parametrize('second_adoption',['healthy','timeout','catalogue_missing'])
def test_real_wire_second_adoption_boundary(second_adoption):
    helper=helpers.DevelopmentPoolDeliveryTests(methodName='runTest')
    try:
        f=helper.fixture();clients=[];catalogues=[];frames=[]
        class Wire:
            def open(self,request,timeout):
                payload=json.loads(request.data);method=payload['method'];params=payload['params']
                if method=='initialize':result={'protocolVersion':PROTOCOL_VERSION}
                elif method=='tools/list':
                    catalogues.append(1)
                    if (f.pipeline.paths['operations']/f.operation/'board-intent.json').exists() and second_adoption=='timeout':raise TimeoutError('PRIVATE_WIRE_ERROR')
                    result={'tools':[] if (f.pipeline.paths['operations']/f.operation/'board-intent.json').exists() and second_adoption=='catalogue_missing' else [TOOL]}
                else:
                    assert method=='tools/call';name=params['name'];args=deepcopy(params['arguments']);args.pop('view',None)
                    sid=args.pop('session_id');actor=f.board.actor_session(f.raw,sid)
                    try:
                        if name=='get_development_ticket_frame':
                            frames.append(1)
                            value=get_development_ticket_frame(f.board,lambda given:f.board.actor_session(f.raw,given),session_id=sid,**args)
                        else:
                            if name=='update_ticket':args.update(args.pop('changes'))
                            value=getattr(f.board,name)(actor,**args)
                        result={'structuredContent':value}
                    except BoardError:result={'isError':True,'content':[{'type':'text','text':'FIXED_REFUSAL'}]}
                return Response(canonical({'jsonrpc':'2.0','id':payload['id'],'result':result}),url='https://board.test/mcp')
        def create(path,*,session_id,timeout):
            c=DevelopmentBoardClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
                'client':'codex','identity_key':'fixture','name':'fixture'},session_id,timeout)
            c.connection_identity=tuple(file_document(path)[1]['identity']);c._opener=Wire();clients.append(c)
            return c
        before=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        with patch('blackoutburst_agents.development_pool.DevelopmentBoardClient.from_connection_file',side_effect=create):
            result=helper.deliver(f);assert result['status']=='complete'
            assert len(catalogues)==1 and len(frames)>2
            assert (f.pushes,f.posts)==(1,1)
            current=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
            assert current['status']=='review' and current['claimant'] is None
            assert current['assignee_agent_id'] is None
    finally:helper.doCleanups()
