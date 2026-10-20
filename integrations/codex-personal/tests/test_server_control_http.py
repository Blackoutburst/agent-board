"""Actual loopback REST -> MCP -> controller -> journal -> REST state.

The worker is deterministic and has no inference or process capability.
"""
import json
import socket
import threading
import time

import httpx
import uvicorn

from app import create_app
from blackoutburst_agents.host_config import validate
from blackoutburst_agents.host_journal import HostJournal
from blackoutburst_agents.server_control_client import ServerControlClient
from blackoutburst_agents.server_controller import ServerController,launch_catalog
from test_host_journal import configuration


def test_actual_http_lifecycle_has_no_registration_feed_or_runtime_side_effect(tmp_path):
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    base=f'http://127.0.0.1:{port}'
    app=create_app(tmp_path/'data',base_url=base,filebrowser_url='',allow_local_passwords=True)
    board=app.state.board;board.ensure_owner('blackoutburst','Blackoutburst','fixture-password-only')
    human={'kind':'human','owner_id':'blackoutburst'};board.set_policy(human,True,True,True,True,[],[])
    device=board.create_device(human,'Owner controller',[])
    profile=board.create_agent_profile(human,'One worker',policy={'allow_read':True,'allow_messages':True,
        'allow_requests':True,'actions':[],'resources':[],'autonomy':'requested'})
    profile=board.bind_agent_profile(human,profile['id'],profile['version'],device['id'],'worker','codex')
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='on'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True);thread.start()
    controller=None
    try:
        deadline=time.monotonic()+5
        while not server.started and time.monotonic()<deadline:time.sleep(.01)
        assert server.started
        with httpx.Client(base_url=base,timeout=5) as browser:
            login=browser.post('/api/login',json={'username':'blackoutburst','password':'fixture-password-only'})
            assert login.status_code==200;browser.headers['X-CSRF-Token']=login.json()['csrf_token']
            h=browser.post('/api/server-hosts',json={'device_id':device['id'],'name':'Owner server','idempotency_key':'grant'})
            assert h.status_code==200;host=h.json()
            cfg=configuration(tmp_path);cfg['owner_id']='blackoutburst';cfg['board_endpoint']=base+'/mcp'
            target=cfg['targets'][0];target.update(agent_id=profile['id'],profile_version=profile['version'])
            connection=tmp_path/'connection.json'
            connection.write_text(json.dumps({'mcp_url':base+'/mcp','client':'codex','identity_key':'worker',
                'name':'Worker','token':device['token']}));connection.chmod(0o600)
            cfg=validate(cfg,bind_connections=True);catalog=launch_catalog(cfg,'b'*64)
            with HostJournal(tmp_path/'host') as journal:
                journal.configure(cfg)
                with journal.owner_lock(lock_directory=tmp_path/'locks'):journal.prepare_managed(host['id'],catalog)
                with ServerControlClient.from_connection_file(connection,session_id='unused-manager-session',timeout=5) as client:
                    class Idle:
                        def __init__(self,j):self.journal=j
                        def tick(self,**_):return {'status':'idle','reason':'no_pending_events'}
                    controller=ServerController(journal,client,supervisor_factory=Idle,lock_directory=tmp_path/'locks')
                    controller.publish(catalog);controller.start_worker();controller.step()
                    def current():
                        r=browser.get('/api/server-hosts/'+host['id']);assert r.status_code==200
                        return r.json()['instances'][0]
                    def command(action,key):
                        i=current();r=browser.post('/api/server-instances/'+i['id']+'/commands',json={
                            'action':action,'expected_version':i['version'],'expected_generation':i['generation'],
                            'launchspec_sha256':i['launchspec_sha256'],'budget':0 if action=='stop' else 1,'idempotency_key':key})
                        assert r.status_code==200,r.text
                    assert current()['observed_state']=='stopped'
                    command('start','start');controller.step()
                    deadline=time.monotonic()+3
                    while not journal.managed_target('primary')['enabled'] and time.monotonic()<deadline:time.sleep(.01)
                    assert journal.managed_target('primary')['enabled'];controller.step()
                    assert current()['observed_state']=='ready'
                    command('stop','stop');controller.step();controller.step()
                    assert current()['observed_state']=='stopped' and current()['cleanup_confirmed']
                    assert journal.dispatches()==[] and board.get_runtime(human)['active_count']==0
                    assert client._registered is False
                    assert controller.shutdown();controller=None
    finally:
        if controller is not None:controller.shutdown()
        server.should_exit=True;thread.join(5);sock.close()
