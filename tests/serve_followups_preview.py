"""Synthetic, isolated data for the conversation follow-up browser acceptance."""
import os
import tempfile

os.environ['BOARD_DATA_DIR']=tempfile.mkdtemp(prefix='board-followups-ui-')
os.environ['BOARD_ALLOW_LOCAL_PASSWORDS']='1'
os.environ['BOARD_REQUIRE_LOGIN'] = '1'
os.environ.setdefault('BOARD_OWNERS', '{"blackoutburst": "Blackoutburst", "thescreamer": "TheScreamer"}')
os.environ['BOARD_BASE_URL']='http://127.0.0.1:18766'
from app import create_app
import uvicorn

app=create_app(os.environ['BOARD_DATA_DIR'],base_url=os.environ['BOARD_BASE_URL'],filebrowser_url='',allow_local_passwords=True)
b=app.state.board
h={'kind':'human','owner_id':'blackoutburst'}
other={'kind':'human','owner_id':'thescreamer'}
for human,name in [(h,'Blackoutburst'),(other,'TheScreamer')]:
    b.ensure_owner(human['owner_id'],name,'local-ui-fixture-only')
    b.set_policy(human,True,True,True,True,[],[])
p=b.create_project(h,'Board follow-ups',['blackoutburst','thescreamer'],idempotency_key='p')
archived=b.create_project(h,'Archived reference',['blackoutburst'],idempotency_key='archived')
b.update_project(h,archived['id'],archived['version'],archived=True)
for kind,name in [('codex','Codex · Mac Solem'),('claude-code','Claude Code · Mac Solem')]:
    device=b.create_device(h,kind+' Mac',[])
    raw=b.authenticate(device['token'])
    b.register_agent(raw,kind+'-browser','Browser fixture',[] ,kind,False,'default',name)
    if kind=='codex':
        for index in range(3):
            task=b.create_ticket(h,p['id'],'Codex task '+str(index+1),idempotency_key='codex-'+str(index))
            b.claim_ticket(b.actor_session(raw,kind+'-browser'),task['id'],task['version'])
b.create_ticket(h,p['id'],'Unassigned task',idempotency_key='unassigned')
b.create_ticket(h,p['id'],'Other owner task',assignee_owner_id='thescreamer',idempotency_key='other')
thread=b.create_thread(h,'Live conversation',['blackoutburst','thescreamer'],'live')
b.post_message(other,thread['id'],'Unrelated message',idempotency_key='noise')
b.post_message(other,thread['id'],'@Blackoutburst initial mention',idempotency_key='mention')
print('Follow-up preview: 127.0.0.1:18766; isolated synthetic records.',flush=True)
uvicorn.run(app,host='127.0.0.1',port=18766,log_level='warning')
