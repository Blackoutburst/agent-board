import pytest

from legacy_schema_fixtures import remove_server_schema14_tables

from board import Board
from test_coordination import space, new, reject


def test_task_files_are_persisted_without_creating_discussion_messages(space):
    board, human, other, project, *_ = space
    task = new(space)
    first = board.add_attachment(human, task['thread_id'], 'reference.png', 'image/png', b'png')
    second = board.add_attachment(human, task['thread_id'], 'details.txt', 'text/plain', b'details')
    saved = board.update_ticket(human, task['id'], task['version'], attachment_ids=[second['id'], first['id'], second['id']])
    assert [item['id'] for item in saved['attachments']] == [second['id'], first['id']]
    assert board.get_thread(other, task['thread_id'])['messages'] == []
    reopened = Board(board.data_dir, base_url=board.base_url)
    assert reopened.get_ticket(other, task['id'])['ticket']['attachments'] == saved['attachments']
    assert reopened.list_tickets(other, project['id'])[0]['attachments'] == saved['attachments']
    reject(board.update_ticket, human, task['id'], task['version'], attachment_ids=[], expected_status=409)
    cleared = board.update_ticket(human, task['id'], saved['version'], attachment_ids=[])
    assert cleared['attachments'] == []
    assert board.get_attachment(human, first['id'])['filename'] == 'reference.png'


@pytest.mark.parametrize('invalid', [None, 'file', [1], [''], ['x' * 201], ['missing'], ['x'] * 101])
def test_invalid_task_files_do_not_partially_change_task(space, invalid):
    board, human, *_ = space
    task = new(space)
    reject(board.update_ticket, human, task['id'], task['version'], title='Changed', attachment_ids=invalid)
    assert board.get_ticket(human, task['id'])['ticket']['title'] == task['title']


def test_other_thread_files_and_unclaimed_agent_edits_are_rejected(space):
    board, human, _, _, agent, *_ = space
    task, other_task = new(space), new(space, 'Other task')
    foreign = board.add_attachment(human, other_task['thread_id'], 'private.txt', 'text/plain', b'private')
    reject(board.update_ticket, human, task['id'], task['version'], attachment_ids=[foreign['id']])
    file = board.add_attachment(human, task['thread_id'], 'file.txt', 'text/plain', b'file')
    reject(board.update_ticket, agent, task['id'], task['version'], attachment_ids=[file['id']], expected_status=403)
    claimed = board.claim_ticket(agent, task['id'], task['version'])
    assert board.update_ticket(agent, task['id'], claimed['version'], attachment_ids=[file['id']])['attachments'] == [file]


def test_schema_seven_upgrade_preserves_history_and_does_not_promote_orphan_files(space):
    board, human, _, _, agent, *_ = space
    task = new(space)
    claimed = board.claim_ticket(agent, task['id'], task['version'])
    cursor = board.read_inbox(agent)['cursor']
    file = board.add_attachment(human, task['thread_id'], 'discussion.txt', 'text/plain', b'old')
    board.post_message(human, task['thread_id'], 'Task attachments', attachment_ids=[file['id']], idempotency_key='old-comment')
    with board._connection() as db:
        remove_server_schema14_tables(db)
        db.execute('DROP TABLE IF EXISTS message_feeds')
        db.execute('DROP TABLE IF EXISTS mcp_page_cursors')
        db.execute('DROP TABLE IF EXISTS ticket_attachments')
        db.execute('PRAGMA user_version=7')
    upgraded = Board(board.data_dir, base_url=board.base_url)
    assert upgraded.get_ticket(human, task['id'])['ticket']['attachments'] == []
    assert len(upgraded.get_thread(human, task['thread_id'])['messages']) == 1
    current = upgraded.get_ticket(agent, task['id'])['ticket']
    assert current['claimant'] == claimed['claimant'] and current['version'] == claimed['version']
    assert upgraded.read_inbox(agent, cursor=cursor)['cursor']
    with upgraded._connection() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 17
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_backup_restore_keeps_task_files_and_bytes(space, tmp_path):
    from pathlib import Path
    from backup import create_backup, restore_backup
    board, human, *_ = space
    task = new(space)
    file = board.add_attachment(human, task['thread_id'], 'file.txt', 'text/plain', b'task-file')
    saved = board.update_ticket(human, task['id'], task['version'], attachment_ids=[file['id']])
    archive = tmp_path / 'backup.zip'
    assert create_backup(board.data_dir, archive)['schema']['user_version'] == 17
    restore_backup(archive, tmp_path / 'restored')
    restored = Board(tmp_path / 'restored', base_url=board.base_url)
    assert restored.get_ticket(human, task['id'])['ticket']['attachments'] == saved['attachments']
    assert Path(restored.get_attachment(human, file['id'])['path']).read_bytes() == b'task-file'
