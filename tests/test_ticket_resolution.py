"""Closing a ticket as superseded or won't do records a decision, not delivered work."""
import sqlite3

from board import Board

from test_coordination import new, reject, space  # noqa: F401  (fixture)


def blocked_pair(space):
    prerequisite = new(space, 'Prerequisite')
    dependent = new(space, 'Dependent', depends_on=[prerequisite['id']])
    return prerequisite, dependent


def test_completed_still_requires_finished_prerequisites_outcome_and_evidence(space):
    board, human, *_ = space
    _, dependent = blocked_pair(space)
    reject(board.update_ticket, human, dependent['id'], dependent['version'], status='done',
           outcome='Done', evidence='Checked', expected_status=409)
    reject(board.update_ticket, human, dependent['id'], dependent['version'], status='done',
           resolution='completed', outcome='Done', evidence='Checked', expected_status=409)
    task = new(space, 'Plain')
    reject(board.update_ticket, human, task['id'], task['version'], status='done', outcome='Done')
    closed = board.update_ticket(human, task['id'], task['version'], status='done', outcome='Done', evidence='Checked')
    assert closed['resolution'] == 'completed'


def test_superseded_close_skips_prerequisites_and_needs_a_reason(space):
    board, human, *_ = space
    prerequisite, dependent = blocked_pair(space)
    reject(board.update_ticket, human, dependent['id'], dependent['version'], status='done', resolution='superseded')
    closed = board.update_ticket(human, dependent['id'], dependent['version'], status='done', resolution='superseded',
                                 outcome='Replaced by Personal Agent #7.')
    assert closed['status'] == 'done' and closed['resolution'] == 'superseded'
    assert closed['evidence'] == 'No verification: closed as superseded by Blackoutburst.'
    assert board.get_ticket(human, dependent['id'])['ticket']['resolution'] == 'superseded'
    # The prerequisite itself is untouched.
    assert board.get_ticket(human, prerequisite['id'])['ticket']['status'] == 'todo'
    # Editing a superseded ticket later doesn't re-apply the prerequisite check.
    edited = board.update_ticket(human, dependent['id'], closed['version'], outcome='Replaced by Personal Agent #7 (done).')
    assert edited['resolution'] == 'superseded'


def test_wont_do_keeps_given_evidence_and_ignores_open_human_requests(space):
    board, human, _, _, first, *_ = space
    task = new(space, 'Asked')
    claimed = board.claim_ticket(first, task['id'], task['version'])
    board.request_human_input(first, task['id'], 'blackoutburst', 'Which way?', 'ask-1')
    current = board.get_ticket(human, task['id'])['ticket']
    assert current['waiting_on_human']
    closed = board.update_ticket(human, task['id'], current['version'], status='done', resolution='wont_do',
                                 outcome='Dropped from scope.', evidence='Owner decision in thread.')
    assert closed['resolution'] == 'wont_do' and closed['evidence'] == 'Owner decision in thread.'
    assert claimed['id'] == task['id']


def test_resolution_only_with_done_and_cleared_on_reopen(space):
    board, human, *_ = space
    task = new(space, 'Reopen')
    reject(board.update_ticket, human, task['id'], task['version'], status='review', resolution='superseded')
    reject(board.update_ticket, human, task['id'], task['version'], status='done', resolution='maybe', outcome='x')
    closed = board.update_ticket(human, task['id'], task['version'], status='done', resolution='wont_do', outcome='No')
    reopened = board.update_ticket(human, task['id'], closed['version'], status='todo')
    assert reopened['status'] == 'todo' and reopened['resolution'] == ''
    assert reopened['outcome'] == '' and reopened['evidence'] == ''


def test_dependents_stop_waiting_on_a_superseded_prerequisite(space):
    board, human, *_ = space
    prerequisite, dependent = blocked_pair(space)
    assert board.get_ticket(human, dependent['id'])['ticket']['blocked_by']
    board.update_ticket(human, prerequisite['id'], prerequisite['version'], status='done', resolution='superseded',
                        outcome='Not needed any more.')
    assert not board.get_ticket(human, dependent['id'])['ticket']['blocked_by']


def test_agent_can_close_its_claimed_ticket_as_superseded(space):
    board, human, _, _, first, *_ = space
    task = new(space, 'Agent close')
    claimed = board.claim_ticket(first, task['id'], task['version'])
    closed = board.update_ticket(first, task['id'], claimed['version'], status='done', resolution='superseded',
                                 outcome='Covered by another ticket.')
    assert closed['resolution'] == 'superseded'
    assert closed['evidence'].startswith('No verification: closed as superseded by ')


def test_schema_18_migration_marks_existing_done_tickets_completed(space, tmp_path):
    board, human, *_ = space
    task = new(space, 'Legacy done')
    board.update_ticket(human, task['id'], task['version'], status='done', outcome='Done', evidence='Checked')
    open_task = new(space, 'Legacy open')
    with sqlite3.connect(board.db_path) as db:
        db.execute("UPDATE tickets SET resolution=''")
        db.execute('DROP INDEX companion_per_owner')
        db.execute('ALTER TABLE agent_profiles DROP COLUMN kind')
        db.execute('PRAGMA user_version=17')
    reopened = Board(board.data_dir, base_url=board.base_url)
    with sqlite3.connect(reopened.db_path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 19
        rows = dict(db.execute('SELECT id,resolution FROM tickets WHERE id IN (?,?)', (task['id'], open_task['id'])))
    assert rows == {task['id']: 'completed', open_task['id']: ''}


def test_superseded_ticket_stays_editable_with_its_dependency_list(space):
    board, human, *_ = space
    prerequisite, dependent = blocked_pair(space)
    closed = board.update_ticket(human, dependent['id'], dependent['version'], status='done', resolution='superseded',
                                 outcome='Replaced.')
    # The web editor re-sends depends_on with every save.
    edited = board.update_ticket(human, dependent['id'], closed['version'], title='Superseded plan',
                                 depends_on=[prerequisite['id']], status='done', resolution='superseded')
    assert edited['title'] == 'Superseded plan' and edited['resolution'] == 'superseded'
    # Turning it into a completed close still needs the prerequisite finished.
    reject(board.update_ticket, human, dependent['id'], edited['version'], status='done', resolution='completed',
           evidence='Checked', depends_on=[prerequisite['id']], expected_status=409)
    reject(board.update_ticket, human, dependent['id'], edited['version'], status='done', resolution='completed',
           evidence='Checked', expected_status=409)
