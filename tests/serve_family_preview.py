"""Loopback-only coordinated-work UI fixture; no real model or provider."""
import os
import tempfile
import time

os.environ['BLACKOUT_SHARED_AUTH']='0'
os.environ['BOARD_ALLOW_LOCAL_PASSWORDS']='1'

from app import create_app
from delegation_families import _result_hash
from fastapi import HTTPException, Request
import uvicorn

with tempfile.TemporaryDirectory(prefix='board-family-ui-') as directory:
    app=create_app(directory,base_url='http://127.0.0.1:18770',filebrowser_url='',allow_local_passwords=True)
    board=app.state.board
    humans={owner:{'kind':'human','owner_id':owner} for owner in ('blackoutburst','thescreamer')}
    actions=['repo.read_file','repo.patch_file','repo.run_test']
    for owner,human in humans.items():
        board.ensure_owner(owner,owner,'local-ui-fixture-only')
        board.set_policy(human,True,True,True,True,actions,['fixture:family:active','fixture:family:changing'])
    human=humans['blackoutburst'];board.update_runtime_limits(human,1,4,False)
    project=board.create_project(human,'Coordinated work fixture',list(humans),'family-ui-project')
    fixtures={}
    for label in ('active','changing'):
        device=board.create_device(human,'Synthetic family '+label,[]);raw=board.authenticate(device['token'])
        sessions=[label+'-coordinator',label+'-one',label+'-two']
        for session in sessions:
            board.register_agent(raw,session,'Family fixture',[],'codex',False,'family-'+label,
                'Family helper <img src=x onerror=alert(1)>')
        coordinator=board.actor_session(raw,sessions[0])
        resource='fixture:family:'+label
        parent=board.create_ticket(human,project['id'],'Coordinated '+label,resource=resource,idempotency_key=label)
        parent=board.claim_ticket(coordinator,parent['id'],parent['version'])
        children=[dict(session_id=session,role='Rôle '+str(i)+' <svg onload=alert(2)>',model='fixture-model-'+str(i),
            title='Agent '+str(i)+' <img src=x onerror=alert(3)> '+('unbroken' * 12),
            description='Bounded task visible to project participants',acceptance_criteria='Synthetic evidence required',
            files=['private-part'+str(i)+'-PRIVATE_FAMILY_PLAN.txt'])
            for i,session in enumerate(sessions[1:])]
        family=board.open_delegation_family(coordinator,parent['id'],parent['version'],children,
            dict(max_model_turns=4,max_tool_actions=2,max_concurrent=2,deadline=time.time()+1800),
            actions,resource,'a'*64,'family-'+label)
        receipts=[]
        for member in family['children']:
            with board._connection() as db:
                session=db.execute('SELECT session_id FROM delegation_members WHERE ticket_id=?',(member['ticket_id'],)).fetchone()[0]
            actor=board.actor_session(raw,session);child=board.get_ticket(actor,member['ticket_id'])['ticket']
            child=board.claim_ticket(actor,child['id'],child['version'])
            model=next(item['model'] for item in children if item['session_id']==session)
            execution=board.reserve_execution(actor,'ui-execution-'+session,project['id'],child['id'],
                'ui-work-'+session,'ui-turn-'+session,'codex_chatgpt',model)['execution']
            execution=board.start_execution(actor,execution['id'],execution['revision'])
            if label=='active':
                # Keep one real reservation occupied; no OS/model process exists.
                break
            board.finish_execution(actor,execution['id'],execution['revision'],'completed',True,'completed')
            child=board.update_ticket(actor,child['id'],child['version'],status='review',release_claim=True,
                outcome='Synthetic bounded result',evidence='Synthetic independent verification')
            with board._connection() as db:
                hashed=_result_hash(db.execute('SELECT * FROM tickets WHERE id=?',(child['id'],)).fetchone())
            receipts.append((child['id'],child['version'],hashed))
        fixtures[label]=dict(family_id=family['id'],parent_ticket_id=parent['id'],
            child_ticket_ids=[child['ticket_id'] for child in family['children']],coordinator=coordinator,receipts=receipts,advanced=0,
            renamed=False,original_title=board.get_ticket(human,family['children'][0]['ticket_id'])['ticket']['title'])

    def local(request):
        if request.client.host not in ('127.0.0.1','::1') or request.headers.get('X-Family-Fixture')!='local-only':
            raise HTTPException(403)

    @app.get('/__fixture__/ids')
    async def fixture_ids(request:Request):
        local(request)
        return {name:{key:values[key] for key in ('family_id','parent_ticket_id','child_ticket_ids')} for name,values in fixtures.items()}

    @app.post('/__fixture__/advance')
    async def advance(request:Request):
        local(request);value=fixtures['changing'];index=value['advanced']
        if index>=len(value['receipts']):raise HTTPException(409)
        child_id,_,hashed=value['receipts'][index]
        child=board.get_ticket(value['coordinator'],child_id)['ticket']
        with board._connection() as db:
            current=_result_hash(db.execute('SELECT * FROM tickets WHERE id=?',(child_id,)).fetchone())
        if current!=hashed:raise HTTPException(409)
        family=board.get_delegation_family(value['coordinator'],value['family_id'])
        result=board.accept_delegation_result(value['coordinator'],family['id'],family['revision'],child_id,child['version'],
            hashed,'Coordinator inspected synthetic result','ui-accepted-'+str(index))
        value['advanced']+=1
        return {'revision':result['revision'],'state':result['state']}

    @app.post('/__fixture__/rename-child')
    async def rename_child(request:Request):
        local(request);value=fixtures['changing'];child_id=value['child_ticket_ids'][0]
        child=board.get_ticket(human,child_id)['ticket']
        title=value['original_title'] if value['renamed'] else value['original_title']+' updated'
        board.update_ticket(human,child_id,child['version'],title=title)
        value['renamed']=not value['renamed']
        return {'title':title}

    controls=[route for route in app.router.routes if getattr(route,'path','').startswith('/__fixture__/')]
    app.router.routes[:]=controls+[route for route in app.router.routes if route not in controls]
    print('Synthetic family UI: 127.0.0.1:18770',flush=True)
    uvicorn.run(app,host='127.0.0.1',port=18770,log_level='warning')
