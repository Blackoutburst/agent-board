"""Pure synthetic consistency, not an authority or journal transition."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import time

import pytest

from blackoutburst_agents import message_launch_bindings as bindings
from blackoutburst_agents.host_config import digest
from blackoutburst_agents.server_controller import launch_catalog

R='a'*64
OPS=[f'00000000-0000-4000-8000-{n:012d}' for n in range(1,19)]


def target(number, *, timeout=1):
    instruction='Réponse synthétique α '+str(number)
    return dict(id='synthetic-target-'+str(number),agent_id='synthetic-profile-'+str(number),
        board_session_id='synthetic-session-'+str(number),profile_version=1,
        connection_file='/synthetic/connections/'+str(number)+'.json',instruction=instruction,
        workspace='/synthetic/workspaces/'+str(number),executable='/synthetic/codex',model='synthetic-model',
        timeout=timeout,thread_ids=['synthetic-thread'],start_at='now',connection_sha256='c'*64,
        instruction_sha256=digest(instruction))


def config(count=1):
    return dict(schema_version=2,owner_id='synthetic-owner',board_endpoint='https://synthetic.invalid/mcp',
        interval_seconds=5,max_pages=1,page_limit=20,dispatch_budget=1,targets=[target(i) for i in range(count)])


def seed(c):
    return bindings.seed_legacy_anchor(c,expected_release_sha256=R,
        expected_predecessor_entries=launch_catalog(c,R),anchor_operation_id=OPS[0])


def check(state,c,release=R):
    return bindings.check_binding_set(state,current_configuration=c,expected_release_sha256=release)


def append(state,old,new,operation=OPS[1]):
    return bindings.append_binding(state,new,previous_configuration=old,expected_release_sha256=R,
        expected_predecessor_entries=bindings.binding_catalog(state,current_configuration=old,expected_release_sha256=R),
        append_operation_id=operation)


@pytest.mark.parametrize('count',[1,3,16])
def test_initial_anchor_has_one_current_operation_for_all_old_targets_without_old_birth(count):
    c=config(count);state=seed(c)
    assert state['origins'][0]['kind']=='anchor' and len(state['origins'])==1
    assert {b['kind'] for b in state['bindings']}=={'anchor'}
    assert {b['operation_id'] for b in state['bindings']}=={OPS[0]}
    assert not any('birth_intent' in k for row in state['bindings'] for k in row)
    assert bindings.binding_catalog(state,current_configuration=c,expected_release_sha256=R)==launch_catalog(c,R)
    assert check(state,c)==state


def test_c0_c1_c2_keep_old_launchspec_and_truthful_global_hash():
    old=config(3);a=seed(old);c1=deepcopy(old);c1['targets'].append(target(3))
    b=append(a,old,c1);c2=deepcopy(c1);c2['targets'].append(target(4));c=append(b,c1,c2,OPS[2])
    assert [len(o['configuration']['targets']) for o in c['origins']]==[3,4,5]
    assert c['bindings'][:3]==a['bindings'] and c['bindings'][:4]==b['bindings']
    assert [b['kind'] for b in c['bindings']]==['anchor']*3+['append']*2
    assert c['configuration_sha256']==digest(c2) and c['configuration_sha256']!=a['configuration_sha256']
    assert launch_catalog(c2,R)[0]['launchspec_sha256']!=a['bindings'][0]['launchspec_sha256']
    assert bindings.binding_catalog(c,current_configuration=c2,expected_release_sha256=R)[:3]==launch_catalog(old,R)


@pytest.mark.parametrize('mutation',['timeout','bool_schema','bool_profile','bool_global'])
def test_python_equal_numbers_do_not_equal_canonical_predecessor_bytes(mutation):
    old=config();state=seed(old);new=deepcopy(old);new['targets'].append(target(1))
    if mutation=='timeout':new['targets'][0]['timeout']=1.0
    elif mutation=='bool_schema':new['schema_version']=True
    elif mutation=='bool_profile':new['targets'][0]['profile_version']=True
    else:new['max_pages']=True
    if mutation=='timeout':assert old['targets'][0]==new['targets'][0]  # == would miss it.
    with pytest.raises(bindings.BindingConsistencyError):append(state,old,new)


@pytest.mark.parametrize('change',['release','catalog_release','catalog_hash','catalog_profile_bool','catalog_duplicate'])
def test_external_expected_release_and_predecessor_catalog_are_required(change):
    c=config();entries=launch_catalog(c,R);expected=R
    if change=='release':expected='b'*64
    elif change=='catalog_release':entries=launch_catalog(c,'b'*64)
    elif change=='catalog_hash':entries[0]['launchspec_sha256']='b'*64
    elif change=='catalog_profile_bool':entries[0]['profile_version']=True
    else:entries.append(deepcopy(entries[0]))
    with pytest.raises(bindings.BindingConsistencyError):
        bindings.seed_legacy_anchor(c,expected_release_sha256=expected,expected_predecessor_entries=entries,anchor_operation_id=OPS[0])
    with pytest.raises(bindings.BindingConsistencyError):check(seed(c),c,'b'*64)


def test_catalogue_order_is_explicitly_a_set_not_a_false_configuration_reorder():
    c=config(3);entries=launch_catalog(c,R);entries.reverse()
    state=bindings.seed_legacy_anchor(c,expected_release_sha256=R,expected_predecessor_entries=entries,anchor_operation_id=OPS[0])
    assert state==seed(c)
    c2=deepcopy(c);c2['targets'].reverse()
    with pytest.raises(bindings.BindingConsistencyError):check(state,c2)


@pytest.mark.parametrize('change',['missing_origin','duplicate_origin','extra_origin','duplicate_binding','wrong_kind',
    'wrong_op','wrong_index','wrong_origin_hash','different_release','false_global_hash','unused_origin','unknown_field'])
def test_rehashed_documents_cannot_hide_inconsistent_origin_chains(change):
    old=config(2);new=deepcopy(old);new['targets'].append(target(2));state=append(seed(old),old,new)
    if change=='missing_origin':state['origins']=state['origins'][1:]
    elif change=='duplicate_origin':state['origins'].append(deepcopy(state['origins'][-1]))
    elif change=='extra_origin':state['origins'].append({**deepcopy(state['origins'][-1]),'operation_id':OPS[2]})
    elif change=='duplicate_binding':state['bindings'][1]=deepcopy(state['bindings'][0])
    elif change=='wrong_kind':state['bindings'][0]['kind']='append'
    elif change=='wrong_op':state['bindings'][0]['operation_id']=OPS[1]
    elif change=='wrong_index':state['bindings'].reverse()
    elif change=='wrong_origin_hash':state['bindings'][0]['origin_configuration_sha256']=digest(new)
    elif change=='different_release':state['bindings'][0]['source_release_sha256']='b'*64
    elif change=='false_global_hash':state['configuration_sha256']=digest(old)
    elif change=='unused_origin':state['origins'].pop()
    else:state['bindings'][0]['authority']=True
    # Recalculate public checksums; they are not authority and cannot fix shape/chain.
    state['origins_sha256']=bindings._digest(bindings._manifest(state['origins']))
    state['bindings_sha256']=bindings._digest(state['bindings'])
    state['state_sha256']=bindings._digest({k:v for k,v in state.items() if k not in ('origins','bindings','state_sha256')})
    with pytest.raises(bindings.BindingConsistencyError):check(state,new)


@pytest.mark.parametrize('change',['skip','reorder','global','old_target','workspace','connection','beginning','operation'])
def test_append_is_exactly_one_new_target_and_does_not_mutate_old_state(change):
    old=config(2);state=seed(old);before=deepcopy(state);new=deepcopy(old);new['targets'].append(target(2));op=OPS[1]
    if change=='skip':new['targets'].append(target(3))
    elif change=='reorder':new['targets'][:2]=list(reversed(new['targets'][:2]))
    elif change=='global':new['page_limit']=21
    elif change=='old_target':new['targets'][0]['model']='other'
    elif change=='workspace':new['targets'][-1]['workspace']=old['targets'][0]['workspace']
    elif change=='connection':new['targets'][-1]['connection_file']=old['targets'][0]['connection_file']
    elif change=='beginning':new['targets'][-1]['start_at']='beginning'
    else:op=OPS[0]
    with pytest.raises(bindings.BindingConsistencyError):append(state,old,new,op)
    assert state==before


@pytest.mark.parametrize('field,value',[('timeout',math.nan),('timeout',math.inf),('timeout',True),('timeout',10**1000),
    ('model',''),('model','x'*201),('instruction','x'*12001),('workspace','relative'),('workspace','/'+'x'*4096),
    ('connection_sha256','not-sha'),('profile_version',1.0),('thread_ids',['same','same'])])
def test_structural_bounds_fail_closed_without_filesystem_validation(field,value):
    c=config();c['targets'][0][field]=value
    with pytest.raises(bindings.BindingConsistencyError):
        bindings.seed_legacy_anchor(c,expected_release_sha256=R,
            expected_predecessor_entries=launch_catalog(config(),R),anchor_operation_id=OPS[0])


def test_detached_outputs_are_not_live_authorities_or_mutable_aliases():
    c=config();state=seed(c);snapshot=deepcopy(state)
    c['targets'][0]['instruction']='mutated input'
    assert state==snapshot
    copy=check(state,config());copy['origins'][0]['configuration']['owner_id']='changed'
    assert state==snapshot
    catalog=bindings.binding_catalog(state,current_configuration=config(),expected_release_sha256=R)
    catalog[0]['profile_version']=99
    assert state==snapshot
    with pytest.raises(bindings.BindingConsistencyError):check(copy,config())


def test_all_public_functions_are_pure_and_never_use_normalize_validate_or_io(monkeypatch):
    import builtins
    from blackoutburst_agents import host_config
    old=config(2);new=deepcopy(old);new['targets'].append(target(2));expected=launch_catalog(old,R)
    def forbidden(*args,**kwargs):raise AssertionError('forbidden side effect')
    with monkeypatch.context() as patch:
        for obj,name in ((builtins,'open'),(os,'open'),(os,'stat'),(os,'lstat'),(os,'getenv'),
            (os,'scandir'),(os,'listdir'),(os,'getpid'),(os,'urandom'),(socket,'socket'),(subprocess,'Popen'),
            (Path,'open'),(Path,'read_bytes'),(Path,'is_symlink'),(Path,'resolve'),(Path,'expanduser'),
            (Path,'stat'),(Path,'exists'),(Path,'iterdir'),
            (time,'time'),(time,'monotonic'),(host_config,'validate')):
            patch.setattr(obj,name,forbidden)
        a=bindings.seed_legacy_anchor(old,expected_release_sha256=R,expected_predecessor_entries=expected,anchor_operation_id=OPS[0])
        b=bindings.append_binding(a,new,previous_configuration=old,expected_release_sha256=R,
            expected_predecessor_entries=expected,append_operation_id=OPS[1])
        assert bindings.binding_catalog(b,current_configuration=new,expected_release_sha256=R)[:2]==expected
        assert bindings.structural_configuration(old)==old


def test_raw_json_is_not_an_api_and_custom_objects_are_not_evaluated():
    class Dangerous:
        def __eq__(self,other):raise AssertionError('custom comparison')
        def __str__(self):raise AssertionError('custom string')
    with pytest.raises(bindings.BindingConsistencyError):
        bindings.seed_legacy_anchor(json.dumps(config()),expected_release_sha256=R,
            expected_predecessor_entries=launch_catalog(config(),R),anchor_operation_id=OPS[0])
    state=seed(config());state['schema']=Dangerous()
    with pytest.raises(bindings.BindingConsistencyError):check(state,config())


def test_maximum_chain_and_no_seventeenth_target():
    old=config();state=seed(old)
    for count in range(2,17):
        candidate=config(count);state=append(state,old,candidate,OPS[count-1]);old=candidate
    assert len(state['origins'])==len(state['bindings'])==16
    with pytest.raises(bindings.BindingConsistencyError):append(state,old,config(17),OPS[16])


def test_total_origin_byte_limit_is_checked_even_for_valid_individual_snapshots(monkeypatch):
    old=config(2);new=config(3);state=append(seed(old),old,new)
    actual=sum(len(bindings._canonical(o['configuration'],bindings.MAX_CONFIGURATION_BYTES)) for o in state['origins'])
    monkeypatch.setattr(bindings,'MAX_ORIGIN_BYTES',actual)
    assert check(state,new)==state
    monkeypatch.setattr(bindings,'MAX_ORIGIN_BYTES',actual-1)
    with pytest.raises(bindings.BindingConsistencyError):check(state,new)


def test_configuration_total_bytes_and_unknown_or_duplicate_collections_are_bounded():
    c=config(16)
    for i,t in enumerate(c['targets']):
        t['instruction']='x'*12000;t['instruction_sha256']=digest(t['instruction'])
        for key in ('workspace','connection_file','executable'):
            t[key]='/'+str(i)+'x'*4000
    with pytest.raises(bindings.BindingConsistencyError):bindings.structural_configuration(c)
    for key in ('targets','thread_ids'):
        c=config()
        if key=='targets':c['targets'].append(deepcopy(c['targets'][0]))
        else:c['targets'][0]['thread_ids']*=2
        with pytest.raises(bindings.BindingConsistencyError):bindings.structural_configuration(c)
    c=config();c['filesystem_verified']=True
    with pytest.raises(bindings.BindingConsistencyError):bindings.structural_configuration(c)


def test_message_reader_and_runtime_identity_are_not_rebased_by_new_binding():
    from blackoutburst_agents.message_host import MessageHostSupervisor
    from blackoutburst_agents.runtime_journal import RuntimeJournal
    class NoIO:
        def reader(self,*values):return values
    old=config(2);new=config(3);state=append(seed(old),old,new)
    assert MessageHostSupervisor._reader(None,NoIO(),old['targets'][0],old,'synthetic-host')==MessageHostSupervisor._reader(None,NoIO(),new['targets'][0],new,'synthetic-host')
    def runtime(c):
        t=c['targets'][0]
        scope=dict(scope_kind='message',owner_id=c['owner_id'],board_endpoint=c['board_endpoint'],
            board_session_id=t['board_session_id'],agent_id=t['agent_id'],thread_id='synthetic-thread',
            message_id='synthetic-message',source_sha256='f'*64)
        return RuntimeJournal.configuration(scope,OPS[0],OPS[1],t['model'])
    assert RuntimeJournal.key(runtime(old))==RuntimeJournal.key(runtime(new))
    assert state['bindings'][0]['target_sha256']==digest(old['targets'][0])==digest(new['targets'][0])
