"""Real failed target -> closed Board visibility; no answer-driven execution."""
from copy import deepcopy
import json
import threading

import pytest

from blackoutburst_agents import continuation_pool_service as module
from blackoutburst_agents.continuation_human_handoff import ContinuationHumanHandoff
from blackoutburst_agents.development_client import DevelopmentBoardClient,CONTINUATION_HUMAN_QUESTIONS
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical,digest
from blackoutburst_agents.continuation_journal import ContinuationJournal
from test_board_reader import Response
from test_continuation_pool_service_integration import actual_service,linux_lock_observation
from board import BoardError


@pytest.mark.parametrize('mode',['question_return','question_exception','cleanup_unknown','stop','provider_pause','late_stop','core_stop'])
def test_target_failure_visibility_is_actual_bounded_and_never_replayed(actual_service,monkeypatch,mode):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    sequence.write_text(json.dumps([None if mode=='provider_pause' else dict(schema=1,kind='PRIVATE_MODEL_TEXT')]))
    packets=[];clients=[];constructed=[];stop=threading.Event()
    create=ContinuationHumanHandoff.__init__
    def handoff(self,*args,**kwargs):
        constructed.append(args[2]);return create(self,*args,**kwargs)
    monkeypatch.setattr(ContinuationHumanHandoff,'__init__',handoff)
    opened=module._Generation.open_task
    def generation_task(generation,*args,**kwargs):
        opened(generation,*args,**kwargs);run=generation.provider.run_checkpoint
        def failed(*args,**kwargs):
            result=run(*args,**kwargs)
            assert result['status']==('paused_provider' if mode=='provider_pause' else 'needs_you'),result
            worker=generation.worker
            class Wire:
                def open(self,request,timeout):
                    packet=json.loads(request.data);packets.append(packet)
                    assert packet['method']=='tools/call','Visibility must never initialize/register'
                    name=packet['params']['name'];values=deepcopy(packet['params']['arguments'])
                    values.pop('view',None);sid=values.pop('session_id')
                    assert name not in ('register_agent','claim_ticket','renew_ticket','release_ticket','update_ticket')
                    try:response={'structuredContent':getattr(f.board,name)(f.board.actor_session(f.raw,sid),**values)}
                    except BoardError:response={'isError':True,'content':[{'type':'text','text':'FIXED_REFUSAL'}]}
                    return Response(json.dumps({'jsonrpc':'2.0','id':packet['id'],'result':response}).encode(),url='https://board.test/mcp')
            client=DevelopmentBoardClient({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC','client':'codex',
                'identity_key':'fixture','name':'Fixture'},worker.client.session_id,3)
            client._registered=client._initialized=True;client._opener=Wire();clients.append(client)
            client.register=lambda:pytest.fail('Visibility registered')
            client.adopt_registration=lambda:pytest.fail('Visibility adopted registration')
            worker.client=client
            if mode in ('cleanup_unknown','late_stop','core_stop'):
                call=worker.control.call
                def unknown(name,arguments):
                    value=call(name,arguments)
                    if name=='lookup_execution':
                        if mode=='late_stop':
                            stop.set();worker.stop(generation.operation)
                        if mode=='core_stop':
                            state=generation.task.snapshot()['state']
                            generation.task.stop(expected_revision=state['revision'],request_id='stop-during-target-handoff')
                        value=deepcopy(value);value['execution'].update(cleanup_confirmed=False,state='needs_review')
                    return value
                worker.control.call=unknown
            if mode=='stop':stop.set()
            if mode=='question_exception':raise DevelopmentError('development_grants_unavailable')
            return result
        generation.provider.run_checkpoint=failed
    monkeypatch.setattr(module._Generation,'open_task',generation_task)
    try:
        result=construct().run_once(should_stop=stop.is_set)
        questions=f.board.list_human_requests(f.human);current=f.board.get_ticket(f.human,ticket['id'])['ticket']
        names=[packet['params']['name'] for packet in packets]
        assert len(calls.read_text().splitlines())==1
        assert 'PRIVATE_' not in json.dumps(result)
        if mode in ('question_return','question_exception'):
            report=result['tasks'][0]['human_visibility']
            assert result['status']=='needs_you' and report['status']=='question_confirmed'
            assert report['human_question_confirmed'] is True and report['launchable'] is False
            assert len(questions)==1 and questions[0]['id']==report['request_id']
            code='permissions' if mode=='question_exception' else 'effects'
            assert questions[0]['question']==CONTINUATION_HUMAN_QUESTIONS[code]
            assert current['status']=='blocked' and current['claimant'] is None
            assert names.count('request_human_input')==1 and names.count('post_message')==0
            key=digest(canonical({'owner_id':'owner','logical_task_id':task_id}))
            with ContinuationJournal.open(root/'continuations'/key/'logical') as journal:
                assert journal.snapshot()['state']['phase']=='needs_you'
            before=(len(calls.read_text().splitlines()),names.count('request_human_input'))
            construct().run_once()
            after=[packet['params']['name'] for packet in packets]
            assert before==(len(calls.read_text().splitlines()),after.count('request_human_input'))
            assert len(constructed)==1
        elif mode in ('cleanup_unknown','late_stop'):
            report=result['tasks'][0]['human_visibility']
            assert result['status']=='needs_you' and report['status'] in ('notice_confirmed','pending')
            assert not report['human_question_confirmed'] and not questions
            assert current['status']=='in_progress' and current['claimant'] is not None
            assert names.count('request_human_input')==0
            if mode=='late_stop':assert names.count('post_message')==0
        elif mode=='core_stop':
            assert result['tasks'][0]['phase']=='stopped' and 'human_visibility' not in result['tasks'][0]
            assert constructed and not questions and not names
            assert current['status']=='in_progress' and current['claimant'] is not None
        else:
            assert not questions and not names and not constructed
            assert 'human_visibility' not in result['tasks'][0]
            assert result['tasks'][0]['phase']==('paused_provider' if mode=='provider_pause' else 'needs_you')
    finally:
        for client in clients:client.close()


def test_failure_before_worker_or_core_never_fabricates_a_handoff(actual_service,monkeypatch):
    f,root,task_id,ticket,construct,calls,sequence=actual_service
    def failed(_generation):raise DevelopmentError('development_grants_unavailable')
    monkeypatch.setattr(module._Generation,'prepare',failed)
    monkeypatch.setattr(ContinuationHumanHandoff,'__init__',lambda *a,**k:pytest.fail('Uninitialized work requested a handoff'))
    result=construct().run_once()
    assert result['status']=='needs_you' and 'human_visibility' not in result['tasks'][0]
    assert not calls.exists() and not f.board.list_human_requests(f.human)
    assert f.board.get_runtime(f.human)['active_count']==0
