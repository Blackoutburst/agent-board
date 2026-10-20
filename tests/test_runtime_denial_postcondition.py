"""Synthetic DDL corruption: denial evidence must survive its own SQL writes."""
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from board import BoardError
from test_runtime_api import browser
from test_runtime_admission_refusal import message_arguments,actor,occupy


def snapshot(board):
    with board._connection() as db:
        return tuple(tuple(tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid'))
            for table in ('runtime_executions','runtime_admission_refusals','owner_execution_limits','audit'))


def quoted(value):
    # Only fixture strings, never private input or production SQL.
    return "'"+value.replace("'","''")+"'"


def call(board,current,arguments,mode):
    if mode=='seal':return board.seal_message_execution_admission(current,**arguments)
    return board.reserve_message_execution(current,**arguments,refusal_mode='capacity-v1')


def corrupt_after_write(db,mode,body):
    event=("AFTER INSERT ON audit WHEN NEW.event='message_admission_sealed'" if mode=='seal'
        else 'AFTER INSERT ON runtime_admission_refusals')
    db.execute('CREATE TRIGGER synthetic_corruption '+event+' BEGIN '+body+' END')


@pytest.mark.parametrize('mode',['seal','capacity'])
@pytest.mark.parametrize('collision',['namespace-key','owner-work-turn'])
def test_postwrite_execution_conflict_rolls_back_proof_audit_and_injected_runtime(browser,mode,collision):
    arguments=message_arguments(browser);old=occupy(browser);board=browser[1];current=actor(browser)
    key=arguments['idempotency_key'] if collision=='namespace-key' else 'injected-alias'
    work=arguments['work_id'] if collision=='owner-work-turn' else 'injected-work'
    turn=arguments['turn_id'] if collision=='owner-work-turn' else 'injected-turn'
    body='''INSERT INTO runtime_executions(id,owner_id,agent_id,profile_version,device_id,session_id,
        project_id,ticket_id,work_id,turn_id,provider,model,idempotency_key,payload,state,created_at,updated_at)
        SELECT 'synthetic-injected',owner_id,agent_id,profile_version,device_id,session_id,project_id,ticket_id,
        {work},{turn},provider,model,{key},'{{}}','reserved',0,0 FROM runtime_executions WHERE id={identifier};'''.format(
            work=quoted(work),turn=quoted(turn),key=quoted(key),identifier=quoted(old[0]))
    with board._tx() as db:corrupt_after_write(db,mode,body)
    before=snapshot(board)
    with pytest.raises(BoardError,match='execution_admission_conflict'):call(board,current,arguments,mode)
    assert snapshot(board)==before


@pytest.mark.parametrize('mode',['seal','capacity'])
@pytest.mark.parametrize('mutation',['delete','payload','response'])
def test_postwrite_denial_must_still_exist_with_exact_payload_and_canonical_response(browser,mode,mutation):
    arguments=message_arguments(browser);occupy(browser);board=browser[1]
    where=' WHERE idempotency_key='+quoted(arguments['idempotency_key'])+';'
    body=('DELETE FROM runtime_admission_refusals'+where if mutation=='delete' else
        'UPDATE runtime_admission_refusals SET '+('payload_sha256='+quoted('b'*64)
        if mutation=='payload' else 'response='+quoted('{"reserved":false}'))+where)
    with board._tx() as db:corrupt_after_write(db,mode,body)
    before=snapshot(board)
    with pytest.raises(BoardError,match='execution_admission_conflict'):call(board,actor(browser),arguments,mode)
    assert snapshot(board)==before


@pytest.mark.parametrize('mode',['seal','capacity'])
def test_postwrite_split_key_and_turn_tombstones_are_not_a_unique_exact_proof(browser,mode):
    arguments=message_arguments(browser);occupy(browser);board=browser[1]
    key=quoted(arguments['idempotency_key']);work=quoted(arguments['work_id']);turn=quoted(arguments['turn_id'])
    # Two rows satisfy the OR lookup without violating either unique index.
    body=f'''UPDATE runtime_admission_refusals SET work_id='changed-work',turn_id='changed-turn'
        WHERE idempotency_key={key};
        INSERT INTO runtime_admission_refusals(owner_id,device_id,session_id,agent_id,profile_version,
            idempotency_key,work_id,turn_id,payload_sha256,response)
        SELECT owner_id,device_id,session_id,agent_id,profile_version,'injected-denial-key',{work},{turn},
            payload_sha256,response FROM runtime_admission_refusals WHERE idempotency_key={key};'''
    if mode=='capacity':
        with board._tx() as db:
            db.execute('CREATE TRIGGER synthetic_corruption AFTER INSERT ON runtime_admission_refusals '
                f'WHEN NEW.idempotency_key={key} BEGIN '+body+' END')
    else:
        with board._tx() as db:corrupt_after_write(db,mode,body)
    before=snapshot(board)
    with pytest.raises(BoardError,match='execution_admission_conflict'):call(board,actor(browser),arguments,mode)
    assert snapshot(board)==before


@pytest.mark.parametrize('mode',['seal','capacity'])
def test_normal_committed_proof_lost_to_caller_replays_without_slot_or_audit_changes(browser,mode):
    arguments=message_arguments(browser);occupy(browser);board=browser[1];current=actor(browser)
    original=board._tx
    @contextmanager
    def lost_after_commit():
        with original() as db:yield db
        raise TimeoutError('synthetic_postcommit_loss')
    with patch.object(board,'_tx',lost_after_commit):
        with pytest.raises(TimeoutError):call(board,current,arguments,mode)
    before=snapshot(board);result=call(board,current,arguments,mode)
    assert result['reserved'] is False and snapshot(board)==before
    with pytest.raises(BoardError):board.reserve_message_execution(current,**arguments)
    assert snapshot(board)==before


def test_legacy_capacity_error_does_not_write_a_denial_or_run_its_insert_trigger(browser):
    arguments=message_arguments(browser);occupy(browser);board=browser[1]
    with board._tx() as db:
        db.execute('''CREATE TRIGGER synthetic_abort AFTER INSERT ON runtime_admission_refusals
            BEGIN SELECT RAISE(ABORT,'denial trigger must not run'); END''')
    before=snapshot(board)
    with pytest.raises(BoardError,match='capacity_exhausted'):
        board.reserve_message_execution(actor(browser),**arguments)
    assert snapshot(board)==before
