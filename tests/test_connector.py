import importlib.util
import json
from pathlib import Path
import stat
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
