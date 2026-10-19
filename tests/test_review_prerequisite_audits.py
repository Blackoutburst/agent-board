"""Malformed retained history fails closed on real temporary Runtime records."""
import json

import pytest

import runtime
from board import BoardError
from test_coordination import space, new  # noqa: F401
from test_review_prerequisite_epochs import setup
from test_review_runtime import reserve


def replace_status_audit(board, dependency, detail):
    with board._tx() as db:
        db.execute("""UPDATE ticket_events SET detail=? WHERE id=(SELECT MAX(id)
            FROM ticket_events WHERE ticket_id=? AND event='ticket_updated')""", (detail, dependency['id']))


def assert_stopped_with_cleanup(fixture, row, running=False):
    board, _, _, _, actor, _ = fixture
    control = board.get_execution_control(actor, row['id'])
    assert control['should_stop'] and control['control_reason'] == 'authorization_changed'
    assert board.lookup_execution(actor, 'review-run')['execution']['should_stop']
    with pytest.raises(BoardError, match='review_prerequisites_history_unavailable'):
        reserve(fixture)
    with pytest.raises(BoardError):
        board.start_execution(actor, row['id'], row['revision'])
    if running:
        row = board.heartbeat_execution(actor, row['id'], row['revision'])
        assert row['should_stop'] and row['control_reason'] == 'authorization_changed'
    assert 'secret-audit-sentinel' not in json.dumps(control)
    assert board.finish_execution(actor, row['id'], row['revision'], 'cancelled', True)['cleanup_confirmed']


@pytest.mark.parametrize('detail', ['{', '{"fields":"status"}'])
@pytest.mark.parametrize('running', [False, True])
def test_exact_independent_malformed_and_scalar_reproductions_stop_cleanly(space, detail, running):
    fixture, dep, row = setup(space, 'done')
    board, _, _, _, actor, _ = fixture
    if running:
        row = board.start_execution(actor, row['id'], row['revision'])
    replace_status_audit(board, dep, detail)
    assert_stopped_with_cleanup(fixture, row, running)


@pytest.mark.parametrize('fields', [
    'status', None, True, 1, {'status': True}, [], ['status', 'status'],
    ['status', 1], ['status', False], ['status', {}], ['status', []], ['not-an-edit-field'],
])
def test_fields_requires_an_unambiguous_nonempty_edit_field_list(space, fields):
    fixture, dep, row = setup(space, 'done')
    replace_status_audit(fixture[0], dep, json.dumps({'ticket_id': dep['id'], 'version': 2, 'fields': fields}))
    assert_stopped_with_cleanup(fixture, row)


@pytest.mark.parametrize('damage', [
    'duplicate-key', 'nonfinite', 'wrong-ticket', 'boolean-version', 'unknown-member',
    'deep', 'invalid-utf8', 'blob', 'oversize', 'surrogate-release',
])
def test_invalid_audit_encoding_shape_and_size_never_leak_or_raise_sqlite(space, damage):
    fixture, dep, row = setup(space, 'done')
    board = fixture[0]
    detail = {'ticket_id': dep['id'], 'version': 2, 'fields': ['status']}
    if damage == 'duplicate-key':
        raw = json.dumps(detail)[:-1] + ',"fields":["progress"]}'
    elif damage == 'nonfinite':
        raw = json.dumps(detail).replace('"version": 2', '"version": NaN')
    elif damage == 'wrong-ticket':
        detail['ticket_id'] = 'secret-audit-sentinel'; raw = json.dumps(detail)
    elif damage == 'boolean-version':
        detail['version'] = True; raw = json.dumps(detail)
    elif damage == 'unknown-member':
        detail['secret-audit-sentinel'] = 'untrusted'; raw = json.dumps(detail)
    elif damage == 'deep':
        raw = '[' * 2000 + '0' + ']' * 2000
    elif damage in ('invalid-utf8', 'blob'):
        raw = b'\xff'
    elif damage == 'oversize':
        raw = '{"secret-audit-sentinel":"' + 'x' * runtime._REVIEW_AUDIT_BYTES + '"}'
    else:
        raw = json.dumps({'ticket_id': dep['id'], 'reason': '\ud800', 'status': '',
                          'version': 2, 'unassign_self': False})
        with board._tx() as db:
            board._audit(db, fixture[1], 'ticket_released', {'ticket_id': dep['id'], 'reason': 'Valid original',
                         'status': '', 'version': 2, 'unassign_self': False})
        # Store the escaped surrogate as text: SQLite correctly rejects direct
        # insertion of an unpaired surrogate in the UTF-8 connection itself.
        with board._tx() as db:
            db.execute("UPDATE ticket_events SET detail=? WHERE ticket_id=? AND event='ticket_released'", (raw, dep['id']))
        assert_stopped_with_cleanup(fixture, row)
        return
    if damage == 'invalid-utf8':
        with board._tx() as db:
            db.execute("""UPDATE ticket_events SET detail=CAST(? AS TEXT) WHERE ticket_id=?
                AND event='ticket_updated'""", (raw, dep['id']))
    else:
        replace_status_audit(board, dep, raw)
    assert_stopped_with_cleanup(fixture, row)


def insert_candidate(board, human, dep, event, detail, count=1):
    with board._tx() as db:
        for _ in range(count):
            board._audit(db, human, event, detail)


@pytest.mark.parametrize('count,stopped', [(63, False), (64, True)])
def test_latest_status_proof_has_a_strict_candidate_read_bound(space, count, stopped, monkeypatch):
    fixture, dep, row = setup(space, 'done')
    board, human, _, _, actor, _ = fixture
    insert_candidate(board, human, dep, 'ticket_updated',
                     {'ticket_id': dep['id'], 'version': 2, 'fields': ['progress']}, count)
    reads = []
    parse = runtime._review_status_event
    def counted(raw, event, ticket_id):
        reads.append(len(raw))
        return parse(raw, event, ticket_id)
    monkeypatch.setattr(runtime, '_review_status_event', counted)
    assert board.get_execution_control(actor, row['id'])['should_stop'] is stopped
    assert len(reads) == 64
    assert sum(reads) < runtime._REVIEW_HISTORY_BYTES


def test_aggregate_byte_budget_applies_before_adopting_the_next_detail(space, monkeypatch):
    fixture, dep, row = setup(space, 'review')
    board, human, _, _, actor, _ = fixture
    # A real release audit may contain 20,000 Unicode characters in its reason.
    # Preserve review without changing status; these events must be classified
    # before reaching the preceding status audit.
    insert_candidate(board, human, dep, 'ticket_released', {
        'ticket_id': dep['id'], 'version': 2, 'status': 'review',
        'reason': '\U0001f642' * 20000, 'unassign_self': False,
    }, 14)
    reads = []
    parse = runtime._review_status_event
    def counted(raw, event, ticket_id):
        reads.append(len(raw))
        return parse(raw, event, ticket_id)
    monkeypatch.setattr(runtime, '_review_status_event', counted)
    statements = []
    with board._connection() as db:
        db.set_trace_callback(statements.append)
        with pytest.raises(BoardError, match='review_prerequisites_history_unavailable'):
            board._runtime_review_prerequisites(db, fixture[-1]['id'])
    assert len(reads) == 13 and sum(reads) <= runtime._REVIEW_HISTORY_BYTES
    assert sum(reads) + reads[-1] > runtime._REVIEW_HISTORY_BYTES
    assert sum('SELECT substr(CAST(detail AS BLOB)' in statement for statement in statements) == 13
    assert board.get_execution_control(actor, row['id'])['should_stop']


def test_oversized_detail_is_refused_before_body_read_or_json_parse(space, monkeypatch):
    fixture, dep, row = setup(space, 'done')
    board, _, _, _, actor, task = fixture
    replace_status_audit(board, dep, 'x' * (runtime._REVIEW_AUDIT_BYTES + 1))
    def forbidden(*_):
        pytest.fail('oversized detail was parsed')
    monkeypatch.setattr(runtime, '_review_status_event', forbidden)
    statements = []
    with board._connection() as db:
        db.set_trace_callback(statements.append)
        with pytest.raises(BoardError, match='review_prerequisites_history_unavailable'):
            board._runtime_review_prerequisites(db, task['id'])
    assert not any('SELECT substr(CAST(detail AS BLOB)' in statement for statement in statements)
    assert board.get_execution_control(actor, row['id'])['should_stop']


def test_invalid_prerequisite_count_stops_before_reading_details(space, monkeypatch):
    fixture, _, row = setup(space, 'done')
    board, _, _, _, actor, task = fixture
    extras = [new(space, f'Extra dependency {index}')['id'] for index in range(100)]
    # The normal product setter already rejects >100; simulate malformed
    # retained metadata without weakening that setter or its cycle checks.
    with board._tx() as db:
        db.executemany('INSERT INTO ticket_dependencies VALUES(?,?)', [(task['id'], item) for item in extras])
    def forbidden(*_):
        pytest.fail('over-bound prerequisite history was parsed')
    monkeypatch.setattr(runtime, '_review_status_event', forbidden)
    assert board.get_execution_control(actor, row['id'])['should_stop']
    assert board.finish_execution(actor, row['id'], row['revision'], 'cancelled', True)['cleanup_confirmed']
