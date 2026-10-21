"""Substituted public receipts never become private append authority."""
from copy import deepcopy
import json

import pytest

from blackoutburst_agents.board_reader import BoardReadError
from blackoutburst_agents.host_config import HostError
from blackoutburst_agents.message_append_protocol import (PIN_FIELDS, _ascii_hash,
    arguments, proof, response, slot)
from blackoutburst_agents.server_control_client import ServerControlClient


def prepared():
    values = dict(schema='server-messages-append-proof-v1', operation_id='11111111-1111-4111-8111-111111111111',
        request_id='request', request_sha256='a'*64, owner_id='owner', host_id='host', device_id='manager',
        grant_version=1, request_revision=2, expected_host_version=1, expected_catalog_version=3,
        expected_instances_sha256='b'*64, phase='prepared', instance_id=None,
        catalog_version=None, instances_sha256=None)
    values.update({key:hex(index+1)[2:]*64 for index,key in enumerate(PIN_FIELDS)})
    return values


def prepare_args():
    p = prepared()
    return dict(host_id=p['host_id'], request_id=p['request_id'], expected_revision=1,
        operation_id=p['operation_id'], idempotency_key='one-exact-operation',
        **{key:p[key] for key in PIN_FIELDS})


@pytest.mark.parametrize('field,value', [
    ('request_revision', True), ('grant_version', True), ('instance_id', 'uncommitted'),
    ('phase', 'connected'), ('operation_id', '11111111111141118111111111111111'),
    ('catalog_version', 4), ('new_configuration_sha256', '2'*64),
    ('new_catalog_sha256', '4'*64), ('source_release_sha256', 'A'*64)])
def test_proof_rejects_confusable_or_uncommitted_values(field, value):
    p=prepared(); p[field]=value
    with pytest.raises(HostError): proof(p)


@pytest.mark.parametrize('field', ['host_id','request_id','operation_id',*PIN_FIELDS])
def test_valid_but_foreign_prepared_receipt_cannot_advance(field):
    p=prepared(); a=prepare_args()
    p[field] = 'foreign' if field in ('host_id','request_id') else (
        '22222222-2222-4222-8222-222222222222' if field=='operation_id' else 'f'*64)
    proof(p)
    with pytest.raises(HostError): response('prepare_server_messages_append', p, a)


def test_completion_keeps_the_appended_identity_and_fence():
    p=prepared();p.update(phase='committed',request_revision=4,instance_id='new-instance',
        catalog_version=4,instances_sha256='c'*64)
    a=dict(host_id='host',request_id='request',expected_revision=4,proof=deepcopy(p),idempotency_key='completion')
    p.update(phase='complete',request_revision=5)
    assert response('complete_server_messages_append',p,a)==p
    for field,new in [('instance_id','different'),('instances_sha256','d'*64)]:
        bad=deepcopy(p);bad[field]=new
        with pytest.raises(HostError):response('complete_server_messages_append',bad,a)


def poll_item():
    s=dict(device_id='slot-device',session_id='slot-session',agent_kind='codex',identity_key='identity',
        profile_id='profile',profile_version=3)
    s={'slot_id':_ascii_hash(s),**s}
    b=dict(schema='server-messages-append-request-v1',expected_host_version=1,expected_catalog_version=3,
        expected_instances_sha256='b'*64,grant_version=1,profile_id='profile',profile_version=3,
        profile_sha256='d'*64,provider_id='codex_chatgpt',requested_model='explicit-model',
        slot_id=s['slot_id'],idempotency_key='request-key')
    v=dict(schema='server-messages-append-view-v1',request_id='request',owner_id='owner',
        host_id='host',device_id='manager',revision=1,state='requested',reason='requested',barrier_held=True,
        profile_id=b['profile_id'],profile_version=b['profile_version'],provider_id=b['provider_id'],
        requested_model=b['requested_model'],slot_id=b['slot_id'],can_cancel=True,instance_id=None,proof=None,
        connection_status='not_attested',model_catalog_status='not_attested',model_access_status='not_attested',
        controller_status='not_reported',controller_reason='')
    v['request_sha256']=_ascii_hash(dict(purpose='messages_append_v1',owner_id='owner',host_id='host',device_id='manager',body=b))
    return dict(request=v,body=b,slot=s)


def test_poll_binds_original_body_exact_slot_and_human_authority():
    item=poll_item();a=dict(host_id='host',limit=1)
    value=dict(schema='server-messages-append-poll-v1',host_id='host',requests=[item])
    assert response('poll_server_messages_appends',value,a)==value
    mutations=[('body','requested_model','different-model'),('slot','identity_key','other'),
        ('request','request_sha256','f'*64),('request','connection_status','connected'),
        ('request','can_cancel',False)]
    for section,field,new in mutations:
        bad=deepcopy(value);bad['requests'][0][section][field]=new
        with pytest.raises(HostError):response('poll_server_messages_appends',bad,a)


def test_client_rejects_untyped_extra_field_before_transport():
    client=object.__new__(ServerControlClient);calls=[]
    client._initialize=lambda:calls.append('initialize')
    client._tool=lambda name,args:calls.append('tool')
    a=prepare_args();a['shell_command']='must never cross transport'
    with pytest.raises(BoardReadError):client.call('prepare_server_messages_append',a)
    assert calls==[]


def test_client_does_not_register_or_coerce_string_payloads():
    client=object.__new__(ServerControlClient);calls=[]
    client._initialize=lambda:calls.append('initialize')
    client._tool=lambda name,args:prepared()
    assert client.call('prepare_server_messages_append',prepare_args())==prepared()
    assert calls==['initialize']
    with pytest.raises(BoardReadError):client.call('prepare_server_messages_append',json.dumps(prepare_args()))
    assert calls==['initialize']


@pytest.mark.parametrize('reason',['PRIVATE_FAILURE_TEXT',[],{},None,True])
def test_incapacity_report_cannot_carry_free_text_or_complex_content(reason):
    a=dict(host_id='host',request_id='request',expected_revision=2,status='blocked',reason=reason,idempotency_key='status')
    with pytest.raises(HostError):arguments('report_server_messages_append_status',a)


def test_incapacity_report_is_data_only_and_correlated():
    a=dict(host_id='host',request_id='request',expected_revision=2,status='blocked',reason='source_changed',idempotency_key='status')
    value=dict(schema='server-messages-append-controller-status-v1',owner_id='owner',host_id='host',
        device_id='manager',request_id='request',request_revision=2,status='blocked',reason='source_changed')
    assert response('report_server_messages_append_status',value,a)==value
    for key,new in [('request_id','other'),('request_revision',3),('status','ready'),('reason','slot_unavailable')]:
        bad=deepcopy(value);bad[key]=new
        with pytest.raises(HostError):response('report_server_messages_append_status',bad,a)
