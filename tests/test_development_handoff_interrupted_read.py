"""Real PR/Board/handoff path with one typed local read interruption.

Board, Git/Runtime/worker/publisher components and MCP codec are real temporary
instances; native/model/GitHub and HTTP I/O endpoints are synthetic. No network
service, credentials, root command or model call is used.
"""
from copy import deepcopy
import http.client
import json
from unittest.mock import patch

import pytest
from board import BoardError
from development_ticket_frames import get_development_ticket_frame
from blackoutburst_agents.board_reader import PROTOCOL_VERSION
from blackoutburst_agents.development_client import DevelopmentBoardClient
from blackoutburst_agents.development_pool_selection import file_document
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from test_board_reader import Response
from test_development_ticket_frame_client import TOOL
import test_development_pool_delivery as helpers


@pytest.mark.parametrize('revoke_before_second_read',[False,True])
def test_interrupted_read_after_pr_retries_freshly_before_board_handoff(revoke_before_second_read):
    helper=helpers.DevelopmentPoolDeliveryTests(methodName='runTest')
    try:
        f=helper.fixture();interrupted=[];retry=[];methods=[];mutation_calls=[]
        class Wire:
            def open(self,request,timeout):
                packet=json.loads(request.data);method=packet['method'];params=packet['params']
                methods.append(method)
                if method=='initialize':result={'protocolVersion':PROTOCOL_VERSION}
                elif method=='tools/list':result={'tools':[TOOL]}
                else:
                    assert method=='tools/call'
                    name=params['name'];args=deepcopy(params['arguments']);args.pop('view',None)
                    sid=args.pop('session_id')
                    boundary=(f.pipeline.paths['operations']/f.operation/'board-intent.json').exists()
                    if name=='get_development_ticket_frame' and boundary:
                        observation={'packet':deepcopy(packet),'headers':dict(request.headers),
                            'endpoint':request.full_url,'timeout':timeout}
                        if not interrupted:
                            assert (f.pushes,f.posts)==(1,1)
                            interrupted.append(observation)
                            raise http.client.RemoteDisconnected('SYNTHETIC_PRIVATE_INTERRUPTION')
                        if not retry:
                            retry.append(observation)
                            if revoke_before_second_read:
                                f.board.set_policy(f.human,True,True,True,False,f.actions,['repo:fixture'])
                    if name in ('update_ticket','release_ticket'):mutation_calls.append((name,boundary))
                    actor=f.board.actor_session(f.raw,sid)
                    try:
                        if name=='get_development_ticket_frame':
                            value=get_development_ticket_frame(f.board,lambda selected:f.board.actor_session(f.raw,selected),
                                session_id=sid,**args)
                        else:
                            if name=='update_ticket':args.update(args.pop('changes'))
                            value=getattr(f.board,name)(actor,**args)
                        result={'structuredContent':value}
                    except BoardError:result={'isError':True,'content':[{'type':'text','text':'FIXED_REFUSAL'}]}
                return Response(canonical({'jsonrpc':'2.0','id':packet['id'],'result':result}),url='https://board.test/mcp')
        def create(path,*,session_id,timeout):
            client=DevelopmentBoardClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
                'client':'codex','identity_key':'fixture','name':'fixture'},session_id,timeout)
            client.connection_identity=tuple(file_document(path)[1]['identity']);client._opener=Wire()
            return client
        with patch('blackoutburst_agents.development_pool.DevelopmentBoardClient.from_connection_file',side_effect=create):
            if revoke_before_second_read:
                with pytest.raises(DevelopmentError):helper.deliver(f)
            else:
                result=helper.deliver(f)
                assert result['status']=='complete'
        assert len(interrupted)==len(retry)==1
        first,second=interrupted[0],retry[0]
        assert first['packet']['id']!=second['packet']['id']
        assert first['packet']['method']==second['packet']['method']=='tools/call'
        assert first['packet']['params']==second['packet']['params']
        assert first['headers']==second['headers'] and first['endpoint']==second['endpoint']
        assert 0<second['timeout']<=first['timeout']
        assert methods.count('tools/list')==1 and (f.pushes,f.posts)==(1,1)
        ticket=f.board.get_ticket(f.human,f.ticket['id'])['ticket']
        root=f.pipeline.paths['operations']/f.operation
        if revoke_before_second_read:
            assert ticket['status']=='in_progress' and ticket['claimant'] is not None
            assert not mutation_calls and not (root/'board.json').exists() and not (root/'handoff.json').exists()
        else:
            assert ticket['status']=='review' and ticket['claimant'] is None and ticket['assignee_agent_id'] is None
            assert [name for name,_ in mutation_calls]==['update_ticket','release_ticket']
            assert (root/'board.json').exists() and (root/'handoff.json').exists()
    finally:helper.doCleanups()
