import json
import sqlite3

import pytest

from board import Board, BoardError


@pytest.fixture
def board_setup(tmp_path):
    board = Board(tmp_path / "data")
    board.ensure_owner("blackoutburst", "Blackoutburst")
    board.ensure_owner("thescreamer", "TheScreamer")
    first = board.human_session("blackoutburst")["actor"]
    second = board.human_session("thescreamer")["actor"]
    thread = board.create_thread(first, "Shared games project", ["blackoutburst", "thescreamer"], "thread")
    return board, first, second, thread["id"]


def events(board):
    with sqlite3.connect(board.db_path) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute("SELECT * FROM notification_events ORDER BY created_at,rowid")]


def make_task(board, actor, thread_id, **kwargs):
    values = {"title": "Configure DNS", "instructions": "Use exact provider records", "requirements": "Domain validated",
              "destination_kind": "human", "destination_id": "thescreamer", "idempotency_key": "task"}
    values.update(kwargs)
    return board.create_task(actor, thread_id, **values)


def test_only_foreign_explicit_mentions_queue_idempotent_notifications(board_setup):
    board, first, _, thread_id = board_setup
    board.post_message(first, thread_id, "Ordinary discussion", idempotency_key="plain")
    board.post_message(first, thread_id, "Note to myself", mentions=["blackoutburst"], idempotency_key="self")
    assert events(board) == []
    message = board.post_message(first, thread_id, "Please check", mentions=["blackoutburst", "thescreamer"], idempotency_key="mention")
    queued = events(board)
    assert len(queued) == 1
    assert queued[0]["kind"] == "mention"
    assert queued[0]["event_key"] == "mention:" + message["id"]
    assert json.loads(queued[0]["target_owners"]) == ["thescreamer"]
    assert queued[0]["thread_id"] == thread_id
    assert queued[0]["sent_at"] is None
    assert queued[0]["attempts"] == 0 and queued[0]["next_attempt_at"] == 0
    assert queued[0]["last_error"] == "" and queued[0]["discord_message_id"] is None
    assert board.post_message(first, thread_id, "Please check", mentions=["blackoutburst", "thescreamer"], idempotency_key="mention")["id"] == message["id"]
    assert len(events(board)) == 1
    with pytest.raises(BoardError):
        board.post_message(first, thread_id, "Changed", mentions=["thescreamer"], idempotency_key="mention")
    assert len(events(board)) == 1


@pytest.mark.parametrize("destination_kind", ["human", "group", "device", "session"])
def test_all_task_destinations_resolve_owner_and_retry_once(board_setup, destination_kind):
    board, first, second, thread_id = board_setup
    destination_id = second["owner_id"]
    if destination_kind in ("device", "session"):
        device = board.create_device(second, "TheScreamer laptop", [])
        destination_id = device["id"]
        if destination_kind == "session":
            board.set_policy(second, True, True, False, False, [], [])
            actor = board.register_session(board.authenticate(device["token"]), "screamer-chat", "Games", [])
            destination_id = actor["session_id"]
    task = make_task(board, first, thread_id, destination_kind=destination_kind, destination_id=destination_id)
    queued = events(board)
    assert len(queued) == 1
    assert queued[0]["kind"] == "task_created"
    assert queued[0]["event_key"] == "task_created:" + task["id"]
    assert json.loads(queued[0]["target_owners"]) == ["thescreamer"]
    assert make_task(board, first, thread_id, destination_kind=destination_kind, destination_id=destination_id)["id"] == task["id"]
    assert len(events(board)) == 1


def test_substantive_task_transitions_notify_without_lease_or_progress_noise(board_setup):
    board, first, second, thread_id = board_setup
    task = make_task(board, first, thread_id)
    assert board.claim_task(second, task["id"])["claimed"]
    board.update_task(second, task["id"], 1)
    board.update_task(second, task["id"], 1, progress="Checking provider requirements")
    assert len(events(board)) == 1
    board.update_task(second, task["id"], 1, status="needs_input", progress="Need exact TXT value")
    assert events(board)[-1]["kind"] == "needs_input"
    assert json.loads(events(board)[-1]["target_owners"]) == ["thescreamer"]
    board.update_task(second, task["id"], 1, status="needs_input", progress="Still waiting")
    board.update_task(second, task["id"], 1)
    assert len(events(board)) == 2
    board.update_task(second, task["id"], 1, status="in_progress")
    board.update_task(second, task["id"], 1, status="needs_input", progress="Another requirement needs confirmation")
    assert len(events(board)) == 3
    board.update_task(second, task["id"], 1, status="ready_for_verification", outcome="Records added", evidence="Provider records entered")
    queued = events(board)
    assert queued[-1]["kind"] == "ready_for_verification"
    assert json.loads(queued[-1]["target_owners"]) == ["blackoutburst", "thescreamer"]
    board.update_task(second, task["id"], 1, status="ready_for_verification")
    assert len(events(board)) == 4
    board.update_task(second, task["id"], 1, status="complete", outcome="Domain validated", evidence="Provider and DNS both verified", verified=True)
    queued = events(board)
    assert len(queued) == 5 and queued[-1]["kind"] == "complete"
    assert json.loads(queued[-1]["target_owners"]) == ["blackoutburst", "thescreamer"]
    with pytest.raises(BoardError):
        board.update_task(second, task["id"], 1, status="complete", outcome="Domain validated", evidence="Provider and DNS both verified", verified=True)
    assert len(events(board)) == 5
    assert len({event["event_key"] for event in queued}) == 5


def test_private_threads_never_enqueue_or_leak_titles(board_setup):
    board, first, _, _ = board_setup
    board.ensure_owner("guest", "Private guest")
    private = board.create_thread(first, "Private secret project", ["blackoutburst", "guest"], "private")
    board.post_message(first, private["id"], "Private discussion", mentions=["guest"], idempotency_key="private-message")
    task = make_task(board, first, private["id"], title="Secret task", destination_id="blackoutburst")
    board.claim_task(first, task["id"])
    board.update_task(first, task["id"], 1, status="needs_input", progress="Private follow-up")
    board.update_task(first, task["id"], 1, status="ready_for_verification", outcome="Prepared", evidence="Private evidence")
    board.update_task(first, task["id"], 1, status="complete", verified=True)
    assert events(board) == []


def test_notification_title_is_bounded_plain_text(board_setup):
    board, first, _, thread_id = board_setup
    make_task(board, first, thread_id, title="\n" + "A" * 240 + "\x01")
    title = events(board)[0]["title"]
    assert title == "A" * 200
    assert all(character.isprintable() for character in title)


def test_upgrade_existing_schema_v1_preserves_data_and_adds_outbox(board_setup):
    board, first, _, thread_id = board_setup
    message = board.post_message(first, thread_id, "Existing message", idempotency_key="existing")
    with sqlite3.connect(board.db_path) as db:
        db.execute("DROP TABLE notification_events")
        assert db.execute("PRAGMA user_version").fetchone()[0] == 11
    upgraded = Board(board.data_dir)
    with sqlite3.connect(upgraded.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 11
        assert {row[1] for row in db.execute("PRAGMA table_info(notification_events)")} == {
            "id", "event_key", "thread_id", "kind", "target_owners", "title", "created_at", "sent_at", "attempts",
            "next_attempt_at", "last_error", "discord_message_id"}
    assert upgraded.get_thread(first, thread_id)["messages"][0]["id"] == message["id"]
    upgraded.post_message(first, thread_id, "New mention", mentions=["thescreamer"], idempotency_key="after-upgrade")
    assert len(events(upgraded)) == 1
    assert len(events(Board(board.data_dir))) == 1


def test_queue_failure_rolls_back_message_and_retry_metadata(board_setup, monkeypatch):
    board, first, _, thread_id = board_setup
    original = board._queue_notification
    def fail(*args, **kwargs):
        raise RuntimeError("Queue unavailable")
    monkeypatch.setattr(board, "_queue_notification", fail)
    with pytest.raises(RuntimeError):
        board.post_message(first, thread_id, "Atomic", mentions=["thescreamer"], idempotency_key="atomic")
    assert board.get_thread(first, thread_id)["messages"] == []
    assert events(board) == []
    monkeypatch.setattr(board, "_queue_notification", original)
    board.post_message(first, thread_id, "Atomic", mentions=["thescreamer"], idempotency_key="atomic")
    assert len(events(board)) == 1
