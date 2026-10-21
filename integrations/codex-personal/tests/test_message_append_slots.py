"""Only the existing private preparation selects a candidate target."""
from copy import deepcopy
import json
import os

import pytest

from blackoutburst_agents.host_config import HostError, connection_digest, digest
from blackoutburst_agents.message_append_protocol import _ascii_hash
from blackoutburst_agents.message_append_slots import PreparedMessageSlots


def prepared(tmp_path):
    base=tmp_path/'owner';base.mkdir(mode=0o700)
    def directory(relative):
        path=base
        for name in relative.split('/'):
            path=path/name;path.mkdir(mode=0o700,exist_ok=True)
        return path
    directory('.config/blackoutburst-agent-host/message-slots')
    connections=directory('.config/blackoutburst-board')
    tools=directory('tools');exe=tools/'codex';exe.write_text('fixture, never executed');exe.chmod(0o700)
    targets=[]
    for name in ('old','new'):
        work=directory('workspaces/'+name)
        conn=connections/(name+'.json')
        conn.write_text(json.dumps(dict(mcp_url='http://localhost:9999/mcp',client='codex',
            identity_key=name,name='Fixture',token='synthetic-no-network-token')));conn.chmod(0o600)
        instruction='Private instruction '+name
        targets.append(dict(id=name,agent_id=name+'-profile',board_session_id=name+'-session',
            profile_version=2,connection_file=str(conn),instruction=instruction,workspace=str(work),
            executable=str(exe),model='explicit-model',timeout=10,thread_ids=['selected-thread'],start_at='now',
            connection_sha256=connection_digest(conn),instruction_sha256=digest(instruction)))
    cfg=dict(schema_version=2,owner_id='owner',board_endpoint='http://localhost:9999/mcp',
        interval_seconds=5,max_pages=1,page_limit=10,dispatch_budget=1,targets=[targets[0]])
    slot=dict(profile_id='new-profile',profile_version=2,device_id='slot-device',session_id='new-session',
        agent_kind='codex',identity_key='new')
    slot={'slot_id':_ascii_hash(slot),**slot}
    body=dict(slot_id=slot['slot_id'],profile_id='new-profile',profile_version=2,
        profile_sha256='b'*64,requested_model='explicit-model')
    data=dict(schema='owner-prepared-message-slot-v1',slot=slot,profile_sha256=body['profile_sha256'],target=targets[1])
    path=base/'.config/blackoutburst-agent-host/message-slots'/(slot['slot_id']+'.json')
    def save():path.write_text(json.dumps(data));path.chmod(0o600)
    save()
    return base,cfg,dict(body=body,slot=slot),data,path,save


def test_slot_is_read_only_and_preserves_original_configuration(tmp_path):
    base,cfg,record,data,path,save=prepared(tmp_path);original=deepcopy(cfg);guards=[]
    loader=PreparedMessageSlots(base,configuration_guard=lambda candidate:guards.append(deepcopy(candidate)))
    value=loader.candidate(record,cfg)
    assert cfg==original and value['targets'][:-1]==original['targets']
    assert value['targets'][-1]==data['target'] and value['targets'][-1]['start_at']=='now'
    assert 'token' not in json.dumps(value) and guards==[value]
    assert not (base/'new-files').exists()


@pytest.mark.parametrize('field,new', [('agent_id','another-profile'),('board_session_id','different-session'),
    ('profile_version',3),('model','other-model'),('start_at','beginning'),('instruction','changed instruction')])
def test_foreign_or_changed_private_slot_refuses_before_configuration_guard(tmp_path,field,new):
    base,cfg,record,data,path,save=prepared(tmp_path);data['target'][field]=new;save();guards=[]
    with pytest.raises(HostError):PreparedMessageSlots(base,configuration_guard=lambda value:guards.append(value)).candidate(record,cfg)
    assert guards==[]


def test_missing_slot_is_not_created_and_no_alternate_path_is_selected(tmp_path):
    base,cfg,record,data,path,save=prepared(tmp_path);path.unlink()
    with pytest.raises(HostError):PreparedMessageSlots(base,configuration_guard=lambda _:None).candidate(record,cfg)
    assert not path.exists()
    record['slot']['slot_id']='../outside'
    with pytest.raises(HostError):PreparedMessageSlots(base,configuration_guard=lambda _:None).candidate(record,cfg)


@pytest.mark.parametrize('kind',['symlink','hardlink','public-mode','parent-mode','duplicate-json','extra-secret-field'])
def test_ambiguous_files_and_unexpected_fields_are_rejected(tmp_path,kind):
    base,cfg,record,data,path,save=prepared(tmp_path)
    if kind=='symlink':
        actual=path.with_suffix('.actual');path.rename(actual);path.symlink_to(actual)
    elif kind=='hardlink':os.link(path,path.with_suffix('.alias'))
    elif kind=='public-mode':path.chmod(0o644)
    elif kind=='parent-mode':path.parent.chmod(0o755)
    elif kind=='duplicate-json':path.write_text('{"schema":"ignored",'+json.dumps(data)[1:])
    else:data['token']='must not be accepted';save()
    with pytest.raises(HostError):PreparedMessageSlots(base,configuration_guard=lambda _:None).candidate(record,cfg)
