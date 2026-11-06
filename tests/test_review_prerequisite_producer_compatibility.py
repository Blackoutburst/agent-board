"""Current producer audits remain strict Runtime prerequisite evidence."""
import json
import pytest
import runtime
from board import BoardError
from test_coordination import space,new
from test_review_queue import deliver,reviewer
from test_review_runtime import reserve

def latest(board,ticket_id,event):
    with board._connection() as db:
        row=db.execute('SELECT id,event,detail FROM ticket_events WHERE ticket_id=? AND event=? ORDER BY id DESC LIMIT 1',
            (ticket_id,event)).fetchone()
        return dict(row)

def test_current_closure_audit_with_resolution_admits_review_without_rewriting_history(space):
    board,human,_,project,author,*_=space
    dep=new(space,'Dependency closed through the current product')
    dep=board.update_ticket(human,dep['id'],dep['version'],status='done',resolution='completed',
        release_claim=True,outcome='Ready',evidence='Verified')
    audit=latest(board,dep['id'],'ticket_updated');detail=json.loads(audit['detail'])
    assert detail['fields']==['evidence','outcome','release_claim','resolution','status']
    actor=reviewer(space)
    delivered=deliver(board,author,new(space,'Dependent review',depends_on=[dep['id']]))
    task=board.claim_ticket(actor,delivered['id'],delivered['version'])
    fixture=(board,human,project,author,actor,task)
    with board._connection() as db:before=tuple(db.execute('SELECT * FROM ticket_events WHERE id=?',(audit['id'],)).fetchone())
    # This is the exact pre-fix parser failure, without modifying retained data.
    original=runtime._TICKET_EDIT_FIELDS
    try:
        runtime._TICKET_EDIT_FIELDS=original-{'resolution'}
        with pytest.raises(BoardError,match='review_prerequisites_history_unavailable'):reserve(fixture)
        assert board.get_runtime(human)['executions']==[]
    finally:runtime._TICKET_EDIT_FIELDS=original
    row=reserve(fixture)['execution']
    assert row['state']=='reserved' and not row['should_stop']
    with board._connection() as db:assert tuple(db.execute('SELECT * FROM ticket_events WHERE id=?',(audit['id'],)).fetchone())==before
    # Adding disposition to the compatibility grammar never weakens ABA epochs.
    changed=board.update_ticket(human,dep['id'],dep['version'],status='todo')
    board.update_ticket(human,dep['id'],changed['version'],status='done',resolution='completed',outcome='Ready',evidence='Checked')
    assert board.get_execution_control(actor,row['id'])['should_stop']
    with pytest.raises(BoardError):board.start_execution(actor,row['id'],row['revision'])
    assert board.finish_execution(actor,row['id'],row['revision'],'cancelled',True)['cleanup_confirmed']

def test_resolution_only_note_is_neutral_but_unknown_edit_field_is_still_rejected(space):
    board,human,_,_,_,*_=space;dep=new(space)
    dep=board.update_ticket(human,dep['id'],dep['version'],status='done',outcome='Ready',evidence='Checked')
    board.update_ticket(human,dep['id'],dep['version'],resolution='superseded')
    audit=latest(board,dep['id'],'ticket_updated')
    assert runtime._review_status_event(audit['detail'].encode(),audit['event'],dep['id']) is False
    detail=json.loads(audit['detail']);detail['fields']=['unrecognized_edit']
    with pytest.raises(BoardError,match='review_prerequisites_history_unavailable'):
        runtime._review_status_event(json.dumps(detail).encode(),audit['event'],dep['id'])

@pytest.mark.parametrize('presentation',[
    dict(summary='Choose a route'),dict(choices=['First','Second']),dict(command='printf ok\n'),
    dict(summary='Résumé',choices=['Oui','Non'],command="printf '%s\\n' 'été 日本語'\n"),
    dict(summary='😀'*240,choices=[str(i)+'😀'*159 for i in range(6)],command='😀'*20000),
])
def test_current_human_request_presentation_audit_is_recognized_without_execution(space,presentation):
    board,human,_,_,_,*_=space;task=new(space)
    board.request_human_input(human,task['id'],'thescreamer','Question?','request',**presentation)
    audit=latest(board,task['id'],'human_input_requested')
    assert json.loads(audit['detail'])['presentation']==presentation
    assert runtime._review_status_event(audit['detail'].encode(),audit['event'],task['id']) is True
    assert board.get_runtime(human)['executions']==[]

@pytest.mark.parametrize('presentation',[
    {},[],None,{'unknown':'value'},{'summary':''},{'summary':' x '},{'summary':'x\ny'},
    {'summary':'x'*241},{'summary':True},{'command':''},{'command':'x'*20001},
    {'choices':['One']},{'choices':['One','One']},{'choices':[' A','B']},
    {'choices':['A','B\r']},{'choices':['A',False]},{'choices':['A']*7},
    {'choices':['x'*161,'B']},{'summary':'\ud800'},
])
def test_invalid_optional_presentation_remains_closed(presentation):
    detail=dict(ticket_id='ticket',request_id='request',owner_id='owner',message_id='message',presentation=presentation)
    with pytest.raises(BoardError,match='review_prerequisites_history_unavailable'):
        runtime._review_status_event(json.dumps(detail).encode(),'human_input_requested','ticket')
