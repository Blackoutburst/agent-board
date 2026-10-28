import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from backup import create_backup, restore_backup
from board import Board


def test_backup_restore_complete_state(tmp_path):
    board = Board(tmp_path / "data")
    board.ensure_owner("blackoutburst", "Blackoutburst", password="fixture-password")
    board.ensure_owner("thescreamer", "TheScreamer")
    owner = board.human_session("blackoutburst")
    actor = owner["actor"]
    board.set_policy(actor, True, True, True, True, ["inspect_dns"], ["games.blackoutburst.com"])
    device = board.create_device(actor, "Windows", ["dns"])
    agent = board.register_session(board.authenticate(device["token"], "agent"), "session", "DNS", ["dns"])
    thread = board.create_thread(actor, "Backup fixture", ["blackoutburst", "thescreamer"], "thread")
    attachment = board.add_attachment(actor, thread["id"], "dns.txt", "text/plain", b"verification fixture")
    board.post_message(actor, thread["id"], "DNS requirements", attachment_ids=[attachment["id"]], idempotency_key="message")
    task = board.create_task(actor, thread["id"], "Verify DNS", "Inspect public values", "Evidence", "group", "blackoutburst", action="inspect_dns", resource="games.blackoutburst.com", idempotency_key="task")
    claimed = board.claim_task(agent, task["id"])["task"]
    board.update_task(agent, task["id"], claimed["claim_version"], progress="Fixture progress persisted")
    inbox = board.read_inbox(agent)
    backup = tmp_path / "backup.zip"
    manifest = create_backup(board.data_dir, backup)
    assert len(manifest["files"]) == 2
    restore_backup(backup, tmp_path / "restored")
    restored = Board(tmp_path / "restored")
    with sqlite3.connect(board.db_path) as old, sqlite3.connect(restored.db_path) as new:
        tables = [r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            assert list(old.execute(f'SELECT * FROM "{table}" ORDER BY rowid')) == list(new.execute(f'SELECT * FROM "{table}" ORDER BY rowid'))
    assert restored.password_login("blackoutburst", "fixture-password")["owner_id"] == "blackoutburst"
    assert restored.authenticate(owner["token"], "human") == actor
    restored_agent = restored.actor_session(restored.authenticate(device["token"], "agent"), "session")
    assert restored.read_inbox(restored_agent, cursor=inbox["cursor"])["messages"] == []
    history = restored.get_thread(actor, thread["id"])
    assert len(history["messages"]) == 1
    assert history["tasks"][0]["progress"][0]["note"] == "Fixture progress persisted"
    assert Path(restored.get_attachment(actor, attachment["id"])["path"]).read_bytes() == b"verification fixture"
    duplicate = restored.post_message(actor, thread["id"], "DNS requirements", attachment_ids=[attachment["id"]], idempotency_key="message")
    assert duplicate["id"] == history["messages"][0]["id"]


def test_restore_rejects_corruption_traversal_and_existing_destination(tmp_path):
    board = Board(tmp_path / "data")
    original = tmp_path / "backup.zip"
    create_backup(board.data_dir, original)
    corrupted = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(corrupted, "w") as dst:
        for name in src.namelist():
            content = src.read(name)
            dst.writestr(name, content + b"corrupt" if name.endswith("sqlite3") else content)
    with pytest.raises(ValueError):
        restore_backup(corrupted, tmp_path / "bad-restore")
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(original) as src, zipfile.ZipFile(traversal, "w") as dst:
        for name in src.namelist():
            dst.writestr(name, src.read(name))
        dst.writestr("../outside", b"bad")
    with pytest.raises(ValueError):
        restore_backup(traversal, tmp_path / "traversal-restore")
    assert not (tmp_path / "outside").exists()
    with pytest.raises(FileExistsError):
        restore_backup(original, board.data_dir)
