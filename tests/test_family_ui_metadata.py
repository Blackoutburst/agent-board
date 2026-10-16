"""Family cards receive project-visible summaries, never the private plan."""
import json
import time

from test_delegation_families_api import family_api, opened
from test_runtime_api import browser


PUBLIC={'family_id','parent_ticket_id','owner_id','agent_id','state','revision','role','model',
        'should_stop','coordinator','children'}
MEMBER={'ticket_id','title','number','status','agent_name','role','model','result_state'}


def test_project_members_get_complete_same_family_summaries_without_private_plan(family_api):
    fixture,args=family_api;_,board,human,token,project,_,_=fixture;family=opened(family_api)
    other={'kind':'human','owner_id':'thescreamer'}
    with board._tx() as db:
        db.execute('INSERT INTO project_members(project_id,owner_id) VALUES(?,?)',(project['id'],other['owner_id']))
    ids={args['parent_ticket_id'],*[item['ticket_id'] for item in family['children']]}
    for actor in (human,other):
        for ticket_id in ids:
            value=board.get_ticket(actor,ticket_id)['ticket']['delegation']
            assert set(value)==PUBLIC
            assert value['family_id']==family['id'] and value['parent_ticket_id']==args['parent_ticket_id']
            assert value['coordinator']['ticket_id']==args['parent_ticket_id']
            assert value['coordinator']['model'] is None
            assert {item['ticket_id'] for item in value['children']}==ids-{args['parent_ticket_id']}
            assert len(value['children'])==2
            for member in [value['coordinator'],*value['children']]:
                assert set(member)<=MEMBER
                assert member['title'] and member['number']>0
            wire=json.dumps(value)
            for secret in ('manifest','plan_sha256','source_sha256','files','part0.txt','session_id','device_id','family-child-one'):
                assert secret not in wire
            assert 'a'*64 not in wire and token not in wire


def test_family_summary_tracks_stop_instruction_without_advancing_ticket_version(family_api):
    fixture,args=family_api;_,board,human,*_=fixture;family=opened(family_api)
    before=board.get_ticket(human,args['parent_ticket_id'])['ticket']
    assert before['delegation']['should_stop'] is False
    with board._tx() as db:
        db.execute('UPDATE tickets SET lease_expires_at=? WHERE id=?',(time.time()-1,args['parent_ticket_id']))
    after=board.get_ticket(human,args['parent_ticket_id'])['ticket']
    assert before['version']==after['version']
    assert after['delegation']['should_stop'] is True
