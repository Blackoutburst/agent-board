"""Pure closed seal client, no connection, registration or local journal."""
from copy import deepcopy

import pytest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.message_admission_seal import (
    FIELDS, METHOD, SCHEMA, MessageAdmissionSealClient,
    request_sha256, validate_seal, wire_arguments,
)
from blackoutburst_agents.runtime_admission import validate_refusal
from blackoutburst_agents.runtime_client import RUNTIME_TOOLS


ARGS=dict(idempotency_key='original-key',thread_id='thread',message_id='message',
    source_sha256='a'*64,work_id='work',turn_id='turn',provider='codex_chatgpt',model='model/fixed')
IDENTITY=dict(owner_id='owner',agent_id='agent',device_id='device',profile_version=1)
WIRE={**ARGS,'session_id':'original-session'}


def response():
    return {'reserved':False,'refusal':{'schema':SCHEMA,'reason':'explicit_pretransport_abandonment',
        'request_sha256':request_sha256(METHOD,WIRE),
        'admission_request_sha256':request_sha256('reserve_message_execution',WIRE),
        **IDENTITY,'session_id':'original-session'}}


class MemoryClient(MessageAdmissionSealClient):
    def __init__(self,value=None,failure=False):
        super().__init__({'mcp_url':'https://board.test/mcp','token':'SYNTHETIC_ONLY',
            'client':'codex','identity_key':'fixture','name':'Fixture'},'original-session',3)
        self.calls=[];self.value=response() if value is None else value;self.failure=failure

    def _initialize(self):self.calls.append('initialize')

    def _tool(self,name,args):
        self.calls.append((name,deepcopy(args)))
        if self.failure:raise BoardReadError('timeout')
        return self.value


def test_exact_request_and_identity_are_bound_without_adding_capacity_mode():
    value=response();client=MemoryClient(value)
    proof=client.seal(ARGS,**IDENTITY)
    assert proof==value['refusal']
    assert client.calls==['initialize',(METHOD,WIRE)]
    assert set(WIRE)==FIELDS|{'session_id'} and 'refusal_mode' not in WIRE
    assert proof['request_sha256']!=proof['admission_request_sha256']
    proof['owner_id']='edited'
    assert value['refusal']['owner_id']=='owner'
    with pytest.raises(ValueError):
        validate_refusal(value,{**WIRE,'refusal_mode':'capacity-v1'},**IDENTITY)


@pytest.mark.parametrize('change',[
    {'idempotency_key':''},{'idempotency_key':'_bad'},{'work_id':True},
    {'turn_id':None},{'source_sha256':'A'*64},{'source_sha256':True},
    {'provider':' bad'},{'model':'has space'},{'model':'x\n'},
    {'model':'x'*201},{'refusal_mode':'capacity-v1'},
])
def test_invalid_original_request_fails_before_initialize_or_rpc(change):
    client=MemoryClient()
    with pytest.raises(BoardReadError,match='runtime_seal_invalid'):
        client.seal({**ARGS,**change},**IDENTITY)
    assert client.calls==[]


@pytest.mark.parametrize('change',[
    {'owner_id':''},{'agent_id':False},{'device_id':{}},
    {'profile_version':True},{'profile_version':0},{'profile_version':2**63},
])
def test_missing_or_invalid_pinned_identity_fails_before_rpc(change):
    client=MemoryClient()
    with pytest.raises(BoardReadError,match='runtime_seal_invalid'):
        client.seal(ARGS,**{**IDENTITY,**change})
    assert client.calls==[]


@pytest.mark.parametrize('field,value',[
    ('schema','message-runtime-admission-refusal-v1'),('reason','capacity_exhausted'),
    ('request_sha256','b'*64),('admission_request_sha256','c'*64),
    ('owner_id','other'),('agent_id','other'),('device_id','other'),
    ('session_id','other'),('profile_version',True),('profile_version',2),
    ('owner_id','PRIVATE_UNEXPECTED_SENTINEL'*10000),
])
def test_wrong_or_malformed_proof_is_refused_after_one_call_without_retry(field,value):
    result=response();result['refusal'][field]=value;client=MemoryClient(result)
    with pytest.raises(BoardReadError,match='runtime_seal_invalid') as error:
        client.seal(ARGS,**IDENTITY)
    assert str(error.value)=='runtime_seal_invalid' and len(client.calls)==2


@pytest.mark.parametrize('shape',['reserved-int','extra','missing','nested-extra','list','positive','nan'])
def test_proof_shape_is_exact_and_never_promoted(shape):
    value=response()
    if shape=='reserved-int':value['reserved']=0
    elif shape=='extra':value['arbitrary']='private'
    elif shape=='missing':value['refusal'].pop('admission_request_sha256')
    elif shape=='nested-extra':value['refusal']['cleanup_confirmed']=True
    elif shape=='list':value['refusal']=[]
    elif shape=='positive':value={'reserved':True,'execution':{}}
    else:value['refusal']['profile_version']=float('nan')
    client=MemoryClient(value)
    with pytest.raises(BoardReadError,match='runtime_seal_invalid'):client.seal(ARGS,**IDENTITY)
    assert len(client.calls)==2


def test_transport_uncertainty_is_not_retried_or_classified_as_a_seal():
    client=MemoryClient(failure=True)
    with pytest.raises(BoardReadError,match='timeout'):client.seal(ARGS,**IDENTITY)
    assert client.calls==['initialize',(METHOD,WIRE)]


def test_explicit_client_never_registers_or_exposes_generic_runtime_or_message_tools():
    client=MemoryClient()
    with pytest.raises(BoardReadError,match='runtime_registration_forbidden'):client.register()
    for name in ('reserve_message_execution','finish_execution',METHOD,'post_message'):
        with pytest.raises(BoardReadError,match='tool_not_allowed'):client.call(name,{})
    assert METHOD not in RUNTIME_TOOLS and client.calls==[]


def test_every_original_request_field_changes_the_admission_and_seal_digest():
    proof=response()['refusal']
    for field in WIRE:
        changed={**WIRE,field:'b'*64 if field=='source_sha256' else 'other'}
        assert request_sha256(METHOD,changed)!=proof['request_sha256']
        assert request_sha256('reserve_message_execution',changed)!=proof['admission_request_sha256']
        with pytest.raises(ValueError):validate_seal(response(),changed,**IDENTITY)
    assert wire_arguments(ARGS,'original-session')==WIRE
