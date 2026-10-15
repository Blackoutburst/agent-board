"""Isolated local-only browser fixture; never points at production data."""
import os
import tempfile
from pathlib import Path

os.environ['BOARD_DATA_DIR']=tempfile.mkdtemp(prefix='board-overhaul-ui-')
os.environ['BOARD_ALLOW_LOCAL_PASSWORDS']='1'
os.environ['BLACKOUT_SHARED_AUTH']='0'
os.environ['BOARD_BASE_URL']='http://127.0.0.1:18765'

from app import create_app
import uvicorn

app=create_app(os.environ['BOARD_DATA_DIR'],base_url='http://127.0.0.1:18765',filebrowser_url='',allow_local_passwords=True)
b=app.state.board
for owner,name in [('blackoutburst','Blackoutburst'),('thescreamer','TheScreamer')]:
 b.ensure_owner(owner,name,'local-ui-fixture-only')
 b.set_policy({'kind':'human','owner_id':owner},True,True,True,True,[],[])
human={'kind':'human','owner_id':'blackoutburst'}
p=b.create_project(human,'Board overhaul',['blackoutburst','thescreamer'],description='A shared workspace for people and autonomous agents.',context='Persistent identities, clear dependencies, and collaboration across machines. A Windows agent can delegate Linux-only work and resume when the result is ready.',repository_url='https://github.com/Blackoutburst/agent-board',environment='Home server / Production',idempotency_key='preview')
a=b.create_ticket(human,p['id'],'Persistent agent identities',description='Keep a stable name across new chats and restarts. Distinguish Codex and Claude Code on the same computer.',acceptance_criteria='Identity and name survive reconnects.',labels=['backend'],idempotency_key='a')
b.update_ticket(human,a['id'],a['version'],status='done',outcome='Persistent installation records added.',evidence='Restart and identity separation tests passed.')
parent=b.create_ticket(human,p['id'],'Verify the cross-platform release',description='Finish the release checks from the Windows workstation. Delegate Linux-only tools to a Linux agent.',acceptance_criteria='All platform checks pass and their outputs are recorded.',priority='high',labels=['release'],idempotency_key='parent')
dev=b.create_device(human,'Home PC',[])
raw=b.authenticate(dev['token'])
b.register_agent(raw,'windows-preview','Release verification',['windows'],'codex',True,'codex-home')
actor=b.actor_session(raw,'windows-preview')
claim=b.claim_ticket(actor,parent['id'],parent['version'])
b.delegate_task(actor,parent['id'],claim['version'],'Run Linux-only verification','Run the native Linux tool and return the test report.',['linux'],'linux-child','Tool succeeds and exact output is recorded.')
linux=b.create_device({'kind':'human','owner_id':'thescreamer'},'Linux server',[])
linux_raw=b.authenticate(linux['token'])
b.register_agent(linux_raw,'linux-preview','Linux release checks',['linux','python'],'claude-code',True,'claude-linux')
b.claim_next_ticket(b.actor_session(linux_raw,'linux-preview'),'linux-preview-claim',p['id'])
mac=b.create_device(human,'Work Mac',[])
b.register_agent(b.authenticate(mac['token']),'mac-preview','Interface verification',['macos','browser'],'codex',False,'codex-work')
c=b.create_ticket(human,p['id'],'Review the GitHub-inspired task workspace',description='Keep work, discussions, and dependencies readable on desktop and mobile.',priority='high',labels=['ux'],idempotency_key='c')
d=b.create_ticket(human,p['id'],'Confirm the release owner',description='Agree on the account that will own the release.',idempotency_key='d')
b.request_human_input(human,d['id'],'blackoutburst','Which account should own the production release?','human-preview')
print('Preview listening on 127.0.0.1:18765; isolated synthetic records only.',flush=True)
uvicorn.run(app,host='127.0.0.1',port=18765,log_level='warning')
