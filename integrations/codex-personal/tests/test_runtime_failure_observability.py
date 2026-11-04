"""Real local RunStore/Runtime and Pool; MCP error transport is synthetic."""
from contextlib import closing
from copy import deepcopy
import hashlib,json,sys,uuid
from pathlib import Path
from types import SimpleNamespace
import pytest

from blackoutburst_agents.board_reader import BoardReadError,PROTOCOL_VERSION
from blackoutburst_agents.runtime_client import RuntimeBoardClient,RESERVATION_FAILURES,runtime_failure
from blackoutburst_agents.runtime_guard import RuntimeGuard,RuntimeControlError
from blackoutburst_agents.runtime_journal import RuntimeJournal
from blackoutburst_agents.run_store import RunStore
from blackoutburst_agents.runner import PersonalRunner
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import DevelopmentError,canonical
from blackoutburst_agents.worker_pool import WorkerPool,PoolDriverBinding,PoolOutcome
from blackoutburst_agents import review_recovery_service as service_module
from blackoutburst_agents.review_pool import _ReviewRun
from test_review_recovery_service import recovery,case,write

PRIVATE='PRIVATE_TEXT_NEVER_DIAGNOSTIC'
SCOPE=dict(owner_id='owner',agent_id='profile',project_id='project',ticket_id='ticket',
    board_session_id='session',board_endpoint='https://board.test/mcp')
ARGS=dict(idempotency_key='runtime-key',project_id='project',ticket_id='ticket',
    work_id=str(uuid.uuid4()),turn_id=str(uuid.uuid4()),provider='codex_chatgpt',model='model')

def rejected(name,code):
    return dict(isError=True,content=[dict(type='text',text='Error executing tool '+name+': '+code)])

class WireClient(RuntimeBoardClient):
    def __init__(self,response):
        super().__init__({'mcp_url':SCOPE['board_endpoint']},SCOPE['board_session_id'],3)
        self.response=response;self.calls=[]
    def _rpc(self,method,params):
        if method=='initialize':return dict(protocolVersion=PROTOCOL_VERSION)
        assert method=='tools/call'
        self.calls.append(params['name'])
        return self.response(params['name'],params['arguments']) if callable(self.response) else self.response

@pytest.mark.parametrize('code',sorted(RESERVATION_FAILURES))
def test_exact_runtime_tool_error_keeps_public_error_and_closed_code(code):
    client=WireClient(rejected('reserve_execution',code))
    with pytest.raises(BoardReadError) as caught:client.call('reserve_execution',ARGS)
    assert caught.value.code==str(caught.value)=='tool_rejected'
    assert caught.value._runtime_tool_code==code
    assert client.calls==['reserve_execution']

@pytest.mark.parametrize('content',[
    [dict(type='text',text=PRIVATE)],
    [dict(type='text',text='Error executing tool lookup_execution: capacity_exhausted')],
    [dict(type='text',text='Error executing tool reserve_execution: capacity_exhausted\n')],
    [dict(type='text',text='Error executing tool reserve_execution: '+PRIVATE)],
    [dict(type='text',text='Error executing tool reserve_execution: capacity_exhausted'),dict(type='text',text=PRIVATE)],
    [dict(type='image',text='Error executing tool reserve_execution: capacity_exhausted')],
])
def test_remote_prose_or_ambiguous_content_cannot_become_a_runtime_code(content):
    client=WireClient(dict(isError=True,content=content))
    with pytest.raises(BoardReadError) as caught:client.call('reserve_execution',ARGS)
    assert not hasattr(caught.value,'_runtime_tool_code')
    assert PRIVATE not in repr(caught.value)

@pytest.mark.parametrize('value',[None,{},dict(stage='reserve',code=PRIVATE),
    dict(stage='reserve',code='capacity_exhausted',authority=True),dict(stage=True,code='owner_paused')])
def test_declared_metadata_is_closed_and_never_extra_authority(value):
    assert runtime_failure(value) is None


def test_private_exception_attributes_are_never_read_for_telemetry(tmp_path):
    class PrivateError(Exception):
        def __getattribute__(self,key):
            if key in ('__dict__','args','code'):raise AssertionError('Private exception inspected')
            return super().__getattribute__(key)
        def __str__(self):raise AssertionError('Private exception stringified')
    def refused(name,args):raise PrivateError(PRIVATE)
    scope=dict(SCOPE);identity={k:scope[k] for k in ('board_endpoint','board_session_id','agent_id')}
    alias='board-agent-'+hashlib.sha256(canonical(identity)).hexdigest();client=WireClient(refused)
    with closing(RunStore(tmp_path/'runs')) as store,RuntimeJournal(tmp_path/'runtime') as journal:
        work=store.create_work(owner_id='owner',agent_id=alias,project_id='project',ticket_id='ticket',
            workspace=tmp_path,executable=Path(sys.executable).resolve(),model='model')
        runner=PersonalRunner(store,lambda *_a,**_k:pytest.fail('model invoked'),runtime_guard=RuntimeGuard(journal,client,scope))
        with pytest.raises(RuntimeControlError):runner.run(work['id'],'one',PRIVATE)
        turns=store.status(work['id'])['turns'];assert len(turns)==1
        result=store.turn_result(turns[0]['id'])
        assert result['verification']['runtime_failure']==dict(stage='reserve',code='unrecognized_code')
        assert PRIVATE not in json.dumps(result)

@pytest.mark.parametrize('stage',['reserve','receipt','start'])
def test_real_blocked_turn_persists_stage_code_without_transport_or_retry(tmp_path,stage):
    scope=dict(SCOPE)
    identity={k:scope[k] for k in ('board_endpoint','board_session_id','agent_id')}
    alias='board-agent-'+hashlib.sha256(canonical(identity)).hexdigest()
    work_id=str(uuid.uuid4());remote=[None]
    def response(name,args):
        if name=='reserve_execution':
            if stage=='reserve':return rejected(name,'capacity_exhausted')
            if stage=='receipt':return dict(structuredContent=dict(reserved=False,retried=False))
            remote[0]=dict(id='e'*32,revision=1,state='reserved',stop_requested=False,should_stop=False,
                cleanup_confirmed=False,profile_version=1,device_id='device',owner_id=scope['owner_id'],
                agent_id=scope['agent_id'],project_id=scope['project_id'],ticket_id=scope['ticket_id'],
                session_id=scope['board_session_id'],work_id=work_id,turn_id=args['turn_id'],
                provider=args['provider'],model=args['model'],reason='',outcome='',control_reason='')
            return dict(structuredContent=dict(reserved=True,retried=False,execution=remote[0]))
        if name=='start_execution':return rejected(name,'owner_paused')
        if name=='lookup_execution':
            return dict(structuredContent=dict(found=False) if remote[0] is None else dict(found=True,execution=remote[0]))
        assert name=='finish_execution'
        remote[0]=dict(remote[0],revision=2,state='finished',cleanup_confirmed=True,outcome='blocked',
            reason='execution_failed',should_stop=True,control_reason='finished')
        return dict(structuredContent=remote[0])
    client=WireClient(response);model=[]
    def forbidden(*args,**kwargs):model.append(True);pytest.fail('Admission failure invoked a model')
    with closing(RunStore(tmp_path/'runs')) as store,RuntimeJournal(tmp_path/'runtime') as journal:
        store.create_work(owner_id=scope['owner_id'],agent_id=alias,project_id=scope['project_id'],
            ticket_id=scope['ticket_id'],workspace=tmp_path,executable=Path(sys.executable).resolve(),
            model='model',work_id=work_id)
        guard=RuntimeGuard(journal,client,scope)
        runner=PersonalRunner(store,forbidden,runtime_guard=guard)
        with pytest.raises(RuntimeControlError,match='runtime_start_requires_review'):
            runner.run(work_id,'selected-turn',PRIVATE,timeout=10)
        turns=store.status(work_id)['turns'];assert len(turns)==1 and turns[0]['status']=='blocked'
        result=store.turn_result(turns[0]['id'])
        expected=dict(stage=stage,code=dict(reserve='capacity_exhausted',receipt='runtime_reservation_unconfirmed',start='owner_paused')[stage])
        assert result['verification']['runtime_failure']==expected
        assert result['reason']=='runtime_start_requires_review' and result['uncertain'] is True
        assert result['verification']['cleanup_confirmed'] is True and result['text']==''
        assert not model and client.calls.count('reserve_execution')==1
        if stage=='start':
            cached=runner.run(work_id,'selected-turn',PRIVATE,timeout=10)
            assert cached['cached'] and cached['result']['status']=='blocked'
        else:
            with pytest.raises(RuntimeControlError):runner.run(work_id,'selected-turn',PRIVATE,timeout=10)
        assert client.calls.count('reserve_execution')==1 and not model
        assert journal.get(journal.list_status()[0]['id'])['start_confirmed']==0
        assert PRIVATE not in json.dumps(result)
        # Exercise the actual failed-review queue projection from that durable
        # RunStore result; its source/lifecycle are irrelevant to telemetry.
        queue=tmp_path/'queue';queue.mkdir(mode=0o700)
        run=_ReviewRun.__new__(_ReviewRun)
        run.context=SimpleNamespace(operation_id=work_id,session_id=scope['board_session_id'],
            journals={'queue':str(queue)},should_stop=lambda:False)
        run.bound=dict(selection=dict(scope=scope,model='model',timeout=10),
            review=dict(request=dict(selection={})),instruction=PRIVATE,material=PRIVATE,
            workspace=str(tmp_path),executable=str(Path(sys.executable).resolve()))
        run.worker=SimpleNamespace(run=lambda **_:dict(status='needs_review'),run_store=store)
        run.driver=SimpleNamespace(_cleanup=lambda *_:False)
        outcome=run.run();receipt=json.loads((queue/'review-result.json').read_bytes())
        assert receipt['runtime_failure']==expected and outcome.cleanup_confirmed is False
        assert PRIVATE not in json.dumps(receipt)

@pytest.mark.parametrize('cleaned,fresh,reason',[
    (False,False,'review_recovery_cleanup_unconfirmed'),
    (True,True,'review_recovery_result_failed'),
    (True,False,'review_recovery_cleanup_unconfirmed')])
def test_service_failure_is_publicly_observable_without_ack_publish_or_new_attempt(recovery,monkeypatch,cleaned,fresh,reason):
    f=recovery;diagnostic=dict(stage='reserve',code='capacity_exhausted')
    class Driver:
        def _verify_cleanup(self,context,outcome):
            f.calls.append(('fresh-cleanup',outcome.status));return fresh
    def start(s,private):
        f.calls.append('start')
        class Run:
            def __init__(self,context):self.context=context
            def run(self):
                context=self.context;f.calls.append('failed-admission')
                with PrivateDirectory(context.journals['queue']) as queue:
                    queue.create('review-result.json',canonical(dict(operation_id=context.operation_id,
                        session_id=context.session_id,status='needs_review',cleanup_confirmed=cleaned,
                        runtime_failure=diagnostic)))
                return PoolOutcome(context.operation_id,context.session_id,'needs_review',cleaned)
            def close(self):pass
        binding=PoolDriverBinding('fixture-v1',Run,lambda ctx:None,lambda ctx,outcome:cleaned)
        s.pool=WorkerPool(s.directory/'pool',concurrency=1,ceiling=1,drivers={'review':binding})
        s.job=s.pool.submit(request_id='one-failed-review',driver_id='review',payload={})
        s.state['job']=deepcopy(s.job);private.replace('registry.json',canonical(s.state))
        s.driver=Driver()
        s.lifecycle=SimpleNamespace(publish=lambda *_:pytest.fail('Failure published an opinion'))
    monkeypatch.setattr(service_module.ReviewRecoveryService,'_start_review',start)
    result=f.service().run_once()
    assert result==dict(status='needs_review',reason=reason,runtime_failure=diagnostic,activated=False)
    assert f.queue_path.exists() and not f.next_calls
    before=list(f.calls);second=f.service().run_once()
    assert second['reason']=='review_recovery_pending' and second['runtime_failure']==diagnostic
    assert f.calls==before and f.calls.count('failed-admission')==1
    if cleaned:assert ('fresh-cleanup','needs_review') in f.calls
    else:assert not any(type(c) is tuple for c in f.calls)

def test_service_does_not_project_malformed_or_complete_receipt_telemetry(recovery):
    f=recovery;s=f.service();s.state=dict(job=dict(id=str(uuid.uuid4()),worker_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),driver_id='review',driver_revision='delivery-review-pool-v1'))
    job=s.state['job'];path=s.directory/'pool/workers'/job['worker_id']/'queue/review-result.json'
    value=dict(operation_id=job['id'],session_id=job['session_id'],status='complete',
        runtime_failure=dict(stage='reserve',code='capacity_exhausted'))
    write(path,value);assert s._runtime_failure() is None
    value['status']='needs_review';value['runtime_failure']['code']=PRIVATE
    write(path,value);assert s._runtime_failure() is None
