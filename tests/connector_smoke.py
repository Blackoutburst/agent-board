"""Run against tests/serve_preview.py; all credentials are disposable fixtures."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import httpx

url='http://127.0.0.1:18765'
script=Path(__file__).resolve().parents[1]/'connect.py'
spec=importlib.util.spec_from_file_location('connector_smoke',script)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with httpx.Client(base_url=url) as client, tempfile.TemporaryDirectory(prefix='board-connector-test-') as scratch:
    login=client.post('/api/login',json={'username':'blackoutburst','password':'local-ui-fixture-only'}).json()
    client.headers['X-CSRF-Token']=login['csrf_token']
    ids=[]
    for client_kind in ('codex','claude-code'):
        pairing=client.post('/api/settings/pairings',json={'label':'Connector test machine','capabilities':[]}).json()
        folder=Path(scratch)/client_kind
        args=[str(script),'--url',url,'--client',client_kind,'--config-dir',str(folder),
            '--codex-config',str(folder/'codex.toml'),'--claude-config',str(folder/'claude.json')]
        with patch.object(sys,'argv',args),patch.object(module.getpass,'getpass',return_value=pairing['code']),contextlib.redirect_stdout(io.StringIO()) as output:
            module.main()
        assert 'Verified 24 MCP tools' in output.getvalue()
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'connector-smoke','version':'1'}}},
            {'jsonrpc':'2.0','method':'notifications/initialized'},
            {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'read_inbox','arguments':{'session_id':'smoke-'+client_kind,'label':'Connector smoke','limit':1}}}]
        process=subprocess.run([sys.executable,str(folder/'connect.py'),'--stdio','--client',client_kind,'--config-dir',str(folder)],
            input='\n'.join(json.dumps(r) for r in requests)+'\n',text=True,capture_output=True,timeout=45,check=True)
        responses={item['id']:item for item in map(json.loads,process.stdout.splitlines())}
        assert len(responses[2]['result']['tools'])==24
        inbox=responses[3]['result']
        assert not inbox.get('isError'),inbox
        data=inbox.get('structuredContent') or json.loads(inbox['content'][0]['text'])
        ids.append(data['session']['agent_id'])
        assert ids[-1]
    assert ids[0]!=ids[1]
print(json.dumps({'connector_clients':['codex','claude-code'],'verified_tools':24,'distinct_persistent_agent_ids':True,'private_temp_configuration_removed':True}))
