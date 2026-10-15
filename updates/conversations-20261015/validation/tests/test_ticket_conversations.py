"""Conversations contain exchanges; creating a ticket alone adds no conversation."""
import sqlite3

import pytest

from board import Board, BoardError


@pytest.fixture
def setup(tmp_path):
    board = Board(tmp_path / "board", base_url="https://board.example.test")
    people = {}
    for owner in ("blackoutburst", "thescreamer", "outsider"):
        board.ensure_owner(owner, owner.title())
        people[owner] = board.human_session(owner)["actor"]
    project = board.create_project(people["blackoutburst"], "Platform", ["thescreamer"])
    ticket = board.create_ticket(people["blackoutburst"], project["id"], "Configure Google")
    return board, people, project, ticket


def reject_not_found(function, *args):
    with pytest.raises(BoardError) as caught:
        function(*args)
    assert caught.value.status == 404


def test_empty_ticket_threads_are_hidden_from_list_and_search_but_directly_accessible(setup):
    board, people, project, ticket = setup
    human = people["blackoutburst"]
    general = board.create_thread(human, "Google questions", ["thescreamer"], "general")
    second_ticket = board.create_ticket(human, project["id"], "Google callback")
    for owner in ("blackoutburst", "thescreamer"):
        actor = people[owner]
        assert [row["id"] for row in board.list_threads(actor)] == [general["id"]]
        assert [row["id"] for row in board.list_threads(actor, "Google")] == [general["id"]]
        assert board.list_threads(actor, "Configure") == []
        empty = board.get_thread(actor, ticket["thread_id"])
        assert empty["messages"] == []
        assert empty["thread"]["message_preview"] == ""
        assert empty["thread"]["title"] == "[Platform #1] Configure Google"
        assert empty["thread"]["ticket"] == {
            "id": ticket["id"], "number": 1, "title": "Configure Google",
            "project_id": project["id"], "project_name": "Platform", "url": ticket["url"],
        }
        assert board.get_thread(actor, second_ticket["thread_id"])["messages"] == []
    assert general["ticket"] is None and general["message_preview"] == ""
    # Visibility is a read filter, not a migration or deletion of ticket state.
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM threads").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 2


def test_first_agent_question_reveals_thread_to_participants_only(setup):
    board, people, project, ticket = setup
    human = people["blackoutburst"]
    # A question needs reply permission; it must not require project management.
    board.set_policy(human, True, True, True, False, [], [])
    device = board.create_device(human, "Synthetic device", [])
    actor = board.register_session(board.authenticate(device["token"]), "question", "Question", [])
    assert board.list_threads(actor) == []
    question = board.post_message(actor, ticket["thread_id"], "Peux-tu fournir le client ID ?", idempotency_key="question")
    for reader in (actor, people["blackoutburst"], people["thescreamer"]):
        rows = board.list_threads(reader)
        assert len(rows) == 1 and rows[0]["id"] == ticket["thread_id"]
        assert rows[0]["participants"] == project["participants"]
        assert rows[0]["message_preview"] == question["body"]
        assert rows[0]["unread_count"] == 1
        assert rows[0]["ticket"]["url"] == ticket["url"]
        assert board.list_threads(reader, "client ID")[0]["id"] == ticket["thread_id"]
        assert board.list_threads(reader, "Configure Google")[0]["id"] == ticket["thread_id"]
    outsider = people["outsider"]
    assert board.list_threads(outsider) == board.list_threads(outsider, "client ID") == []
    reject_not_found(board.get_thread, outsider, ticket["thread_id"])
    reject_not_found(board.post_message, outsider, ticket["thread_id"], "Not a participant")


def test_existing_discussions_search_and_read_state_survive_reopen(setup):
    board, people, _, ticket = setup
    first = board.post_message(people["blackoutburst"], ticket["thread_id"], "Need access to the console")
    reply = board.post_message(people["thescreamer"], ticket["thread_id"], "The redirect URI is ready", reply_to=first["id"])
    board.mark_read(people["thescreamer"], ticket["thread_id"])
    with sqlite3.connect(board.db_path) as db:
        original = list(db.execute("SELECT * FROM messages ORDER BY seq"))
    reopened = Board(board.data_dir, base_url=board.base_url)
    rows = reopened.list_threads(people["thescreamer"], "redirect")
    assert len(rows) == 1 and rows[0]["unread_count"] == 0
    assert rows[0]["message_preview"] == first["body"]
    page = reopened.get_thread(people["thescreamer"], ticket["thread_id"], limit=1)
    assert page["messages"][0]["id"] == reply["id"]
    assert page["thread"]["message_preview"] == first["body"]
    earlier = reopened.get_thread(people["thescreamer"], ticket["thread_id"], before=page["next_before"])
    assert earlier["messages"][0]["id"] == first["id"]
    with sqlite3.connect(board.db_path) as db:
        assert list(db.execute("SELECT * FROM messages ORDER BY seq")) == original


def test_preview_uses_first_message_normalizes_whitespace_and_is_bounded(setup):
    board, people, _, ticket = setup
    body = "  Une\n question\tavec\u00a0des\r\nespaces  " + "é" * 220
    first = board.post_message(people["blackoutburst"], ticket["thread_id"], body)
    board.post_message(people["thescreamer"], ticket["thread_id"], "A later reply")
    thread = board.get_thread(people["blackoutburst"], ticket["thread_id"])["thread"]
    assert thread["message_preview"] == " ".join(body.split())[:180]
    assert len(thread["message_preview"]) == 180
    assert board.get_thread(people["blackoutburst"], ticket["thread_id"])["messages"][0]["body"] == first["body"]
    assert thread["title"] == "[Platform #1] Configure Google"


def test_attachment_only_exchange_is_visible_without_inventing_a_preview(setup):
    board, people, _, ticket = setup
    human = people["blackoutburst"]
    attachment = board.add_attachment(human, ticket["thread_id"], "request.txt", "text/plain", b"Synthetic question")
    assert board.list_threads(human) == []
    board.post_message(human, ticket["thread_id"], "", attachment_ids=[attachment["id"]])
    rows = board.list_threads(people["thescreamer"])
    assert len(rows) == 1 and rows[0]["id"] == ticket["thread_id"]
    assert rows[0]["message_preview"] == ""
    assert rows[0]["ticket"]["id"] == ticket["id"]


def test_conversation_membership_does_not_grant_ticket_metadata(setup):
    board, people, _, ticket = setup
    board.post_message(people["blackoutburst"], ticket["thread_id"], "Shared question")
    # Defensive fixture for independently managed conversation membership. The
    # public project API currently keeps project participants immutable.
    with sqlite3.connect(board.db_path) as db:
        db.execute("INSERT INTO thread_members VALUES(?,?)", (ticket["thread_id"], "outsider"))
    outsider = people["outsider"]
    direct = board.get_thread(outsider, ticket["thread_id"])["thread"]
    listed = board.list_threads(outsider)[0]
    assert direct["ticket"] is listed["ticket"] is None
    assert direct["message_preview"] == listed["message_preview"] == "Shared question"
    reject_not_found(board.get_ticket, outsider, ticket["id"])
    assert board.get_thread(people["thescreamer"], ticket["thread_id"])["thread"]["ticket"]["id"] == ticket["id"]
