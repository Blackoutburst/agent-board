import importlib.util
import io
import json
from pathlib import Path
import stat
import pytest
try:
    import tomllib as tomli
except ModuleNotFoundError:  # Python 3.10
    import tomli


spec=importlib.util.spec_from_file_location('board_connector',Path(__file__).parents[1]/'connect.py')
connector=importlib.util.module_from_spec(spec)
spec.loader.exec_module(connector)


def test_connector_updates_only_board_and_removes_stale_filter(tmp_path):
    path=tmp_path/'config.toml'
    path.write_text('model = "kept"\n[mcp_servers.blackoutburst_board]\nurl="https://old.test/mcp"\nenabled_tools=["read_inbox"]\nbearer_token_env_var="OLD"\n[mcp_servers.other]\ncommand="kept"\n')
    connector.write_codex(path,tmp_path/'connect.py',tmp_path,'codex')
    parsed=tomli.loads(path.read_text())
    assert parsed['model']=='kept'
    assert parsed['mcp_servers']['other']['command']=='kept'
    board=parsed['mcp_servers']['blackoutburst_board']
    assert board['command']
    assert 'enabled_tools' not in board and 'bearer_token_env_var' not in board
    assert '--stdio' in board['args']
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert len(list(tmp_path.glob('config.toml.before-board-*')))==1


def test_claude_configuration_and_identity_are_separate(tmp_path):
    path=tmp_path/'claude.json'
    path.write_text(json.dumps({'preferences':{'keep':True},'mcpServers':{'other':{'command':'keep'}}}))
    connector.write_claude(path,tmp_path/'connect.py',tmp_path,'claude-code')
    value=json.loads(path.read_text())
    assert value['preferences']['keep']
    assert value['mcpServers']['other']['command']=='keep'
    assert 'claude-code' in value['mcpServers']['blackoutburst_board']['args']
    assert stat.S_IMODE(path.stat().st_mode)==0o600


def test_windows_credential_acl_uses_current_user(monkeypatch,tmp_path):
    path=tmp_path/'credential.json'
    path.write_text('synthetic')
    calls=[]
    class Result:
        returncode=0
    def invoke(arguments,**kwargs):
        calls.append(arguments)
        return Result()
    with monkeypatch.context() as patch:
        patch.setattr(connector.os,'name','nt')
        patch.setenv('USERDOMAIN','TESTDOMAIN')
        patch.setattr(connector.getpass,'getuser',lambda:'fixture-user')
        patch.setattr(connector.subprocess,'run',invoke)
        connector.protect_path(path)
    assert calls==[['icacls',str(path),'/inheritance:r','/grant:r','TESTDOMAIN\\fixture-user:F']]


def profile_connection(tmp_path):
    path=tmp_path/'codex.json'
    config={'mcp_url':'https://board.test/mcp','client':'codex','token':'synthetic-private-device-token',
            'identity_key':'old','name':'Old agent'}
    path.write_text(json.dumps(config));path.chmod(0o600)
    return path,config


def profile_registration():
    return {'structuredContent':{
        'profile':{'id':'profile','name':'Reviewer','owner_id':'owner'},
        'identity':{'id':'profile','owner_id':'owner','device_id':'device','agent_kind':'codex'},
        'session':{'agent_id':'profile','owner_id':'owner','device_id':'device'}}}


def test_select_bound_profile_reuses_private_connection_without_pairing(monkeypatch,tmp_path):
    path,config=profile_connection(tmp_path);calls=[]
    def rpc(selected,method,params):
        calls.append((selected,method,params))
        return {} if method=='initialize' else profile_registration()
    monkeypatch.setattr(connector,'rpc',rpc)
    assert connector.select_agent(path,'bound-key')=={'profile_id':'profile','name':'Reviewer'}
    stored=json.loads(path.read_text())
    assert stored=={**config,'identity_key':'bound-key','name':'Reviewer'}
    assert path.with_name('connect.py').read_text()==Path(connector.__file__).read_text()
    assert stat.S_IMODE(path.stat().st_mode)==0o600
    assert len(calls)==2 and calls[1][2]['name']=='register_agent'
    assert calls[1][2]['arguments']['autonomous'] is False
    assert calls[1][2]['arguments']['skills']==[]


@pytest.mark.parametrize('failure',['rejected','old_server','mismatched','changed'])
def test_profile_selection_failure_preserves_connection_and_hides_remote_error(monkeypatch,tmp_path,failure):
    path,config=profile_connection(tmp_path);original=path.read_text()
    def rpc(selected,method,params):
        if method=='initialize':return {}
        value=profile_registration()
        if failure=='rejected':return {'isError':True,'content':[{'text':'PRIVATE REMOTE ERROR'}]}
        if failure=='old_server':value['structuredContent'].pop('profile')
        if failure=='mismatched':value['structuredContent']['session']['owner_id']='another'
        if failure=='changed':path.write_text('CHANGED BY OWNER')
        return value
    monkeypatch.setattr(connector,'rpc',rpc)
    with pytest.raises(RuntimeError) as error:connector.select_agent(path,'bound-key')
    assert 'PRIVATE' not in str(error.value)
    assert path.read_text()==('CHANGED BY OWNER' if failure=='changed' else original)


def test_profile_selection_rejects_insecure_files_and_bad_keys_before_network(monkeypatch,tmp_path):
    path,_=profile_connection(tmp_path);calls=[]
    monkeypatch.setattr(connector,'rpc',lambda *args:calls.append(args))
    for key in ('', ' ', 'x\ny', 'x'*201):
        with pytest.raises(ValueError):connector.select_agent(path,key)
    path.chmod(0o644)
    with pytest.raises(RuntimeError):connector.select_agent(path,'bound-key')
    link=tmp_path/'symlink.json';link.symlink_to(path)
    with pytest.raises(RuntimeError):connector.select_agent(link,'bound-key')
    assert calls==[]


@pytest.mark.parametrize('explicit_first', [False, True])
def test_bridge_never_forwards_work_after_rejected_selected_profile(monkeypatch,tmp_path,capsys,explicit_first):
    _, config=profile_connection(tmp_path)
    calls=[]
    rejected={'isError':True,'content':[{'type':'text','text':'synthetic remote refusal'}]}
    def rpc(*args):
        calls.append('preregister')
        return rejected
    def request(url,message,token):
        calls.append(message['params']['name'])
        assert message['params']['name']=='register_agent', 'No work may use the previous session after a rejected profile switch'
        return {'jsonrpc':'2.0','id':message['id'],'result':rejected}
    messages=[]
    if explicit_first:
        messages.append({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'register_agent','arguments':{'session_id':'old-session'}}})
    messages += [{'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'claim_next_ticket','arguments':{'session_id':'old-session'}}} for i in (2,3)]
    monkeypatch.setattr(connector,'rpc',rpc)
    monkeypatch.setattr(connector,'request',request)
    monkeypatch.setattr(connector.sys,'stdin',io.StringIO('\n'.join(map(json.dumps,messages))))
    connector.bridge(config)
    replies=[json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(replies)==len(messages) and all('error' in reply for reply in replies)
    assert calls==(['register_agent'] if explicit_first else [])+['preregister','preregister']


def test_bridge_confirms_selected_identity_and_caches_only_success(monkeypatch,tmp_path,capsys):
    _,config=profile_connection(tmp_path);calls=[]
    response=profile_registration()
    response['structuredContent']['identity']['identity_key']=config['identity_key']
    response['structuredContent']['session']['id']='selected-session'
    def rpc(*args):calls.append('preregister');return response
    def request(url,message,token):
        calls.append(message['params']['name'])
        return {'jsonrpc':'2.0','id':message['id'],'result':{'ok':True}}
    messages=[{'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'get_work','arguments':{'session_id':'selected-session'}}} for i in (1,2)]
    monkeypatch.setattr(connector,'rpc',rpc);monkeypatch.setattr(connector,'request',request)
    monkeypatch.setattr(connector.sys,'stdin',io.StringIO('\n'.join(map(json.dumps,messages))))
    connector.bridge(config)
    assert calls==['preregister','get_work','get_work']
    assert all(json.loads(line)['result']=={'ok':True} for line in capsys.readouterr().out.splitlines())
    response['structuredContent']['identity']['identity_key']='different-bound-key'
    with pytest.raises(RuntimeError):connector.confirm_registration(response,config,'selected-session')
