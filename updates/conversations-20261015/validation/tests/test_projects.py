"""Synthetic projects/tickets tests: migration, isolation, races and recovery."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

import board as core
from board import Board, BoardError


@pytest.fixture
def setup(tmp_path):
    board = Board(tmp_path / "board", base_url="https://board.example.test")
    for owner in ("blackoutburst", "thescreamer", "outsider"):
        board.ensure_owner(owner, owner.title())
    first = board.human_session("blackoutburst")["actor"]
    second = board.human_session("thescreamer")["actor"]
    outsider = board.human_session("outsider")["actor"]
    return board, first, second, outsider


def rejects(expected_status, function, *args, **kwargs):
    with pytest.raises(BoardError) as caught:
        function(*args, **kwargs)
    assert caught.value.status == expected_status


def shared(board, human, **kwargs):
    return board.create_project(human, "Platform", ["thescreamer"], idempotency_key="project", **kwargs)


def agent(board, human, session="one"):
    board.set_policy(human, True, True, True, True, [], [])
    device = board.create_device(human, "Test device", [])
    actor = board.register_session(board.authenticate(device["token"]), session, session, [])
    return actor, device


def table_count(board, name):
    with sqlite3.connect(board.db_path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]


def test_project_membership_plain_context_and_search(setup):
    board, first, second, outsider = setup
    project = shared(board, first, description="Shared service", context="<script>plain text</script>",
                     repository_url="https://example.test/repository", environment="Synthetic local only")
    assert project["participants"] == ["blackoutburst", "thescreamer"]
    assert project["context"] == "<script>plain text</script>"
    assert project["version"] == 1 and project["archived"] is False
    assert project["ticket_counts"] == {"todo": 0, "in_progress": 0, "blocked": 0, "review": 0, "done": 0, "total": 0}
    assert board.get_project(second, project["id"])["id"] == project["id"]
    assert board.list_projects(second, query="service")[0]["id"] == project["id"]
    assert board.list_projects(second, query="nonexistent") == []
    assert board.list_projects(outsider) == []
    rejects(404, board.get_project, outsider, project["id"])
    rejects(404, board.update_project, outsider, project["id"], 1, name="Leaked")
    rejects(404, board.list_tickets, outsider, project["id"])
    rejects(404, board.create_ticket, outsider, project["id"], "Forbidden")
    assert project["url"] == "https://board.example.test/?project=" + project["id"]


def test_ticket_discussion_membership_attachments_and_filters(setup):
    board, first, second, outsider = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Fix login", description="Alias resolution", acceptance_criteria="Both names work",
                                 priority="high", assignee_owner_id=second["owner_id"])
    assert ticket["number"] == 1 and ticket["version"] == 1 and ticket["status"] == "todo"
    assert ticket["claimant"] is None
    assert board.get_thread(second, ticket["thread_id"])["thread"]["participants"] == project["participants"]
    message = board.post_message(second, ticket["thread_id"], "Reproduced")
    attachment = board.add_attachment(first, ticket["thread_id"], "result.txt", "text/plain", b"Synthetic evidence")
    assert board.get_thread(first, ticket["thread_id"])["messages"][0]["id"] == message["id"]
    assert board.get_attachment(second, attachment["id"])["filename"] == "result.txt"
    for function, args in ((board.get_ticket, (ticket["id"],)), (board.update_ticket, (ticket["id"], 1)),
                           (board.claim_ticket, (ticket["id"], 1)), (board.get_thread, (ticket["thread_id"],)),
                           (board.get_attachment, (attachment["id"],))):
        extra = {"title": "no"} if function == board.update_ticket else {}
        rejects(404, function, outsider, *args, **extra)
    assert board.list_tickets(first, project["id"], priority="high", status="todo", assignee_owner_id=second["owner_id"], query="alias")[0]["id"] == ticket["id"]
    assert board.list_tickets(first, project["id"], priority="low") == []
    assert board.list_tickets(first, project["id"], query="1")[0]["id"] == ticket["id"]
    assert board.get_project(first, project["id"])["ticket_counts"]["todo"] == 1
    assert board.get_ticket(second, ticket["id"])["project"]["id"] == project["id"]


def test_live_read_and_metadata_permission_and_revoked_session(setup):
    board, first, _, _ = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Work")
    actor, device = agent(board, first)
    assert board.list_projects(actor)[0]["id"] == project["id"]
    for permission in ("allow_read", "allow_requests"):
        values = {"enabled": True, "allow_read": True, "allow_reply": True, "allow_requests": True, "actions": [], "resources": []}
        values[permission] = False
        board.set_policy(first, **values)
        if permission == "allow_read":
            for function, args in ((board.list_projects, ()), (board.get_project, (project["id"],)),
                                   (board.list_tickets, (project["id"],)), (board.get_ticket, (ticket["id"],))):
                rejects(403, function, actor, *args)
        else:
            rejects(403, board.create_project, actor, "Forbidden", [])
            rejects(403, board.update_project, actor, project["id"], 1, context="Forbidden")
            rejects(403, board.create_ticket, actor, project["id"], "Forbidden")
            rejects(403, board.claim_ticket, actor, ticket["id"], 1)
    board.set_policy(first, True, True, True, True, [], [])
    rejects(403, board.get_project, {**actor, "session_id": "unknown"}, project["id"])
    rejects(401, board.get_project, {**actor, "owner_id": "thescreamer"}, project["id"])
    board.revoke_device(first, device["id"])
    rejects(401, board.get_ticket, actor, ticket["id"])
    rejects(401, board.create_ticket, actor, project["id"], "Forbidden")
    login = board.human_session(first["owner_id"])
    board.logout(login["token"])
    rejects(401, board.list_projects, login["actor"])


def test_agent_cannot_update_unclaimed_or_other_session_claim_and_revoke_grant(setup):
    board, first, _, _ = setup
    actor, device = agent(board, first)
    other = board.register_session(board.authenticate(device["token"]), "two", "Other chat", [])
    project = board.create_project(actor, "Agent project", [], context="Shared scope")
    project = board.update_project(actor, project["id"], 1, environment="Local")
    ticket = board.create_ticket(actor, project["id"], "Explicit work")
    rejects(403, board.update_ticket, actor, ticket["id"], 1, status="in_progress")
    ticket = board.claim_ticket(actor, ticket["id"], 1)
    rejects(403, board.update_ticket, other, ticket["id"], ticket["version"], description="Another chat")
    rejects(409, board.claim_ticket, other, ticket["id"], ticket["version"])
    ticket = board.update_ticket(actor, ticket["id"], ticket["version"], description="Claimant update")
    board.set_policy(first, True, True, True, False, [], [])
    rejects(403, board.update_ticket, actor, ticket["id"], ticket["version"], release_claim=True)
    recovered = board.update_ticket(first, ticket["id"], ticket["version"], release_claim=True)
    assert recovered["claimant"] is None
    assert recovered["description"] == "Claimant update"


def test_project_idempotency_concurrent_creation_and_changed_payload(setup):
    board, first, second, _ = setup
    barrier = Barrier(2)
    def create(_):
        barrier.wait()
        return shared(board, first)
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(create, range(2)))
    assert rows[0]["id"] == rows[1]["id"] and table_count(board, "projects") == 1
    rejects(409, board.create_project, first, "Different", ["thescreamer"], idempotency_key="project")
    assert shared(board, second)["id"] != rows[0]["id"]
    assert board.create_project(first, "Platform", ["thescreamer", "blackoutburst", "thescreamer"], idempotency_key="project")["id"] == rows[0]["id"]


def test_ticket_idempotency_atomic_threads_and_monotonic_numbers(setup):
    board, first, second, _ = setup
    project = shared(board, first)
    barrier = Barrier(4)
    def create(_):
        barrier.wait()
        return board.create_ticket(first, project["id"], "Retry", idempotency_key="same")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(create, range(4)))
    assert len({r["id"] for r in rows}) == 1 and table_count(board, "tickets") == table_count(board, "threads") == 1
    rejects(409, board.create_ticket, first, project["id"], "Changed", idempotency_key="same")
    assert board.create_ticket(second, project["id"], "Different owner", idempotency_key="same")["number"] == 2
    barrier = Barrier(4)
    def distinct(n):
        barrier.wait()
        return board.create_ticket(first, project["id"], f"Ticket {n}", idempotency_key=f"new-{n}")
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(distinct, range(4)))
    assert sorted(r["number"] for r in rows) == [3, 4, 5, 6]
    project2 = board.create_project(first, "Second", [])
    assert board.create_ticket(first, project2["id"], "First")["number"] == 1
    assert table_count(board, "notification_events") == 0
    assert table_count(board, "tasks") == 0


def test_create_rolls_back_every_table_and_number_on_failure(setup, monkeypatch):
    board, first, _, _ = setup
    project = shared(board, first)
    original = board._audit
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic commit failure")
    monkeypatch.setattr(board, "_audit", fail)
    with pytest.raises(RuntimeError):
        board.create_project(first, "Atomic", [], idempotency_key="atomic-project")
    with pytest.raises(RuntimeError):
        board.create_ticket(first, project["id"], "Atomic", idempotency_key="atomic-ticket")
    assert table_count(board, "projects") == 1
    assert table_count(board, "project_members") == 2
    assert table_count(board, "threads") == table_count(board, "thread_members") == table_count(board, "tickets") == 0
    monkeypatch.setattr(board, "_audit", original)
    assert board.create_ticket(first, project["id"], "Atomic", idempotency_key="atomic-ticket")["number"] == 1


def test_project_version_conflict_archiving_and_retries(setup):
    board, first, second, _ = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Existing", idempotency_key="existing")
    updated = board.update_project(second, project["id"], 1, context="Decision", archived=True)
    assert updated["version"] == 2
    rejects(409, board.update_project, first, project["id"], 1, context="Stale overwrite")
    assert board.get_project(first, project["id"])["context"] == "Decision"
    assert board.list_projects(first) == []
    assert board.list_projects(first, include_archived=True)[0]["archived"] is True
    rejects(409, board.create_ticket, first, project["id"], "New")
    assert board.create_ticket(first, project["id"], "Existing", idempotency_key="existing")["id"] == ticket["id"]
    assert board.get_ticket(second, ticket["id"])["ticket"]["title"] == "Existing"
    board.update_project(first, project["id"], 2, archived=False)
    assert board.create_ticket(first, project["id"], "New")["number"] == 2


def test_concurrent_ticket_claims_exactly_one_winner_and_repeat_safe(setup):
    board, first, _, _ = setup
    actor, device = agent(board, first)
    other = board.register_session(board.authenticate(device["token"]), "two", "Other", [])
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Single responsibility")
    barrier = Barrier(2)
    def claim(who):
        barrier.wait()
        try:
            return board.claim_ticket(who, ticket["id"], 1)
        except BoardError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, [actor, other]))
    assert sum(isinstance(r, dict) for r in results) == 1 and 409 in results
    claimed = next(r for r in results if isinstance(r, dict))
    winner = actor if claimed["claimant"]["session_id"] == actor["session_id"] else other
    assert board.claim_ticket(winner, ticket["id"], 1)["version"] == 2
    assert board.claim_ticket(winner, ticket["id"], 2)["version"] == 2
    rejects(409, board.claim_ticket, winner, ticket["id"], 999)
    updated = board.update_ticket(winner, ticket["id"], 2, status="blocked")
    rejects(409, board.claim_ticket, winner, ticket["id"], 1)
    assert board.claim_ticket(winner, ticket["id"], updated["version"])["status"] == "blocked"


def test_human_agent_claim_contention_reassignment_and_explicit_recovery(setup):
    board, first, second, _ = setup
    actor, device = agent(board, first)
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Assignment", assignee_owner_id=second["owner_id"])
    rejects(403, board.claim_ticket, actor, ticket["id"], 1)
    claimed = board.claim_ticket(second, ticket["id"], 1)
    rejects(409, board.claim_ticket, actor, ticket["id"], claimed["version"])
    assigned = board.update_ticket(first, ticket["id"], claimed["version"], assignee_owner_id=first["owner_id"])
    assert assigned["claimant"] is None
    claimed = board.claim_ticket(actor, ticket["id"], assigned["version"])
    rejects(403, board.update_ticket, actor, ticket["id"], claimed["version"], assignee_owner_id=second["owner_id"])
    rejects(409, board.update_ticket, first, ticket["id"], assigned["version"], release_claim=True)
    # Device revocation leaves a visible claim for explicit human recovery.
    board.revoke_device(first, device["id"])
    current = board.get_ticket(second, ticket["id"])["ticket"]
    assert current["claimant"]["session_id"] == actor["session_id"]
    cleared = board.update_ticket(second, ticket["id"], current["version"], release_claim=True, assignee_owner_id=None)
    assert cleared["claimant"] is None and cleared["assignee_owner_id"] is None
    human_claim = board.claim_ticket(first, ticket["id"], cleared["version"])
    reconnected = board.human_session(first["owner_id"])["actor"]
    assert board.claim_ticket(reconnected, ticket["id"], human_claim["version"])["version"] == human_claim["version"]


def test_completion_evidence_reopening_and_optimistic_edits(setup):
    board, first, second, _ = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Complete")
    rejects(400, board.update_ticket, first, ticket["id"], 1, status="done")
    rejects(400, board.update_ticket, first, ticket["id"], 1, status="done", outcome="Fixed", evidence=" \n")
    review = board.update_ticket(second, ticket["id"], 1, status="review", outcome="Fixed", evidence="Tests passed")
    rejects(409, board.update_ticket, first, ticket["id"], 1, description="Stale")
    done = board.update_ticket(first, ticket["id"], review["version"], status="done")
    assert done["outcome"] == "Fixed" and done["evidence"] == "Tests passed"
    rejects(409, board.claim_ticket, second, ticket["id"], done["version"])
    rejects(400, board.update_ticket, second, ticket["id"], done["version"], evidence="")
    reopened = board.update_ticket(second, ticket["id"], done["version"], status="todo")
    assert reopened["outcome"] == reopened["evidence"] == ""
    done = board.update_ticket(first, ticket["id"], reopened["version"], status="done", outcome="Another fix", evidence="New tests")
    reopened = board.update_ticket(second, ticket["id"], done["version"], status="blocked", evidence="Still relevant")
    assert reopened["outcome"] == "" and reopened["evidence"] == "Still relevant"
    renamed = board.update_ticket(second, ticket["id"], reopened["version"], title="New title")
    assert "New title" in board.get_thread(first, ticket["thread_id"])["thread"]["title"]
    assert renamed["title"] == "New title"


@pytest.mark.parametrize("value", ["javascript:alert(1)", "http://example.test", "https://", "https://user:password@example.test",
                                 "https://example.test\n.evil", "https://example.test\\evil", "https://example.test:99999"])
def test_repository_url_rejects_unsafe_values(setup, value):
    board, first, _, _ = setup
    rejects(400, board.create_project, first, "URL", [], repository_url=value)


@pytest.mark.parametrize("field,value", [("name", " "), ("name", "x" * 121), ("description", "x" * 4001),
                                        ("context", "x" * 30001), ("environment", None), ("archived", "false"),
                                        ("participants", []), ("version", 999)])
def test_invalid_project_update_preserves_version(setup, field, value):
    board, first, _, _ = setup
    project = shared(board, first)
    rejects(400, board.update_project, first, project["id"], 1, **{field: value})
    assert board.get_project(first, project["id"])["version"] == 1


@pytest.mark.parametrize("field,value", [("title", ""), ("title", "x" * 201), ("priority", "panic"), ("status", "closed"),
                                        ("release_claim", 1), ("claimant", {"owner_id": "outsider"}),
                                        ("assignee_owner_id", "outsider"), ("description", None), ("evidence", "x" * 20001)])
def test_invalid_ticket_update_preserves_version(setup, field, value):
    board, first, _, _ = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Validation")
    rejects(400, board.update_ticket, first, ticket["id"], 1, **{field: value})
    assert board.get_ticket(first, ticket["id"])["ticket"]["version"] == 1


def test_invalid_versions_types_and_no_implicit_privileges(setup):
    board, first, _, _ = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Version")
    for value in (True, 0, -1, "1", None, 1.0):
        rejects(400, board.update_project, first, project["id"], value, name="No")
        rejects(400, board.update_ticket, first, ticket["id"], value, title="No")
        rejects(400, board.claim_ticket, first, ticket["id"], value)
    assert board.get_settings(first)["policy"]["enabled"] is False
    assert table_count(board, "tasks") == table_count(board, "resource_locks") == table_count(board, "notification_events") == 0
    assert board.read_inbox(first)["tasks"] == []
    assert board.read_inbox(first)["messages"] == []


def test_current_schema_reopens_and_newer_schema_fails_closed(setup):
    board, first, _, _ = setup
    project = shared(board, first)
    ticket = board.create_ticket(first, project["id"], "Persistent")
    reopened = Board(board.data_dir)
    assert reopened.get_ticket(first, ticket["id"])["ticket"]["number"] == 1
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        db.execute("PRAGMA user_version=3")
    with pytest.raises(RuntimeError, match="newer"):
        Board(board.data_dir)
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT title FROM tickets").fetchone()[0] == "Persistent"


def test_initial_schema_creation_failure_is_atomic(tmp_path, monkeypatch):
    def fail(db):
        db.execute("CREATE TABLE temporary_migration_row(id TEXT)")
        raise RuntimeError("Synthetic migration failure")
    original = core.migrate_projects
    monkeypatch.setattr(core, "migrate_projects", fail)
    with pytest.raises(RuntimeError):
        Board(tmp_path / "atomic")
    with sqlite3.connect(tmp_path / "atomic" / "board.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 0
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    monkeypatch.setattr(core, "migrate_projects", original)
    assert Board(tmp_path / "atomic")


def legacy_fixture(tmp_path):
    """Build an actual v1 database, without first initializing v2 or changing its version backwards."""
    from pathlib import Path
    data_dir = tmp_path / "legacy"
    data_dir.mkdir()
    (data_dir / "attachments").mkdir()
    database = data_dir / "board.sqlite3"
    with sqlite3.connect(database) as db:
        db.executescript((Path(__file__).parent / "fixtures" / "schema_v1.sql").read_text())
    # Populate the frozen v1 schema without the v2 constructor. Freeze the v1
    # thread serializer too: current serialization joins project/ticket tables,
    # which correctly do not exist in a genuine pre-migration database.
    class LegacyBoard(Board):
        def _thread(self, db, row, owner_id):
            participants = [r[0] for r in db.execute(
                "SELECT owner_id FROM thread_members WHERE thread_id=? ORDER BY owner_id", (row["id"],))]
            unread = db.execute("""SELECT COUNT(*) FROM messages WHERE thread_id=?
                AND seq>COALESCE((SELECT seq FROM read_state WHERE owner_id=? AND thread_id=?),0)""",
                (row["id"], owner_id, row["id"])).fetchone()[0]
            return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"], "created_at": row["created_at"],
                    "participants": participants, "unread_count": unread, "url": self.base_url + "/?thread=" + row["id"]}

    legacy = LegacyBoard.__new__(LegacyBoard)
    legacy.data_dir, legacy.db_path = data_dir, database
    legacy.base_url, legacy.lease_seconds = "https://board.example.test", 300
    legacy.ensure_owner("blackoutburst", "Blackoutburst")
    legacy.ensure_owner("thescreamer", "TheScreamer")
    first = legacy.human_session("blackoutburst")["actor"]
    second = legacy.human_session("thescreamer")["actor"]
    actor, device = agent(legacy, first)
    thread = legacy.create_thread(first, "Legacy discussion", ["thescreamer"], "legacy-thread")
    attachment = legacy.add_attachment(first, thread["id"], "legacy.txt", "text/plain", b"Synthetic v1 file")
    message = legacy.post_message(second, thread["id"], "Legacy message", attachment_ids=[attachment["id"]],
                                  mentions=["blackoutburst"], idempotency_key="legacy-message")
    task = legacy.create_task(first, thread["id"], "Legacy task", "Instructions", "Requirements", "human", "thescreamer",
                              idempotency_key="legacy-task")
    task = legacy.claim_task(second, task["id"])["task"]
    legacy.mark_read(first, thread["id"])
    inbox = legacy.read_inbox(actor)
    assert len(inbox["messages"]) == 1
    return legacy, first, second, actor, device, thread, message, attachment, task, inbox["cursor"]


def snapshot_tables(database):
    with sqlite3.connect(database) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'")]
        return {name: db.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in tables}


@pytest.mark.parametrize("with_outbox", [True, False])
def test_real_v1_migration_preserves_every_row_and_credentials(tmp_path, with_outbox):
    legacy, first, second, actor, device, thread, message, attachment, task, cursor = legacy_fixture(tmp_path)
    with sqlite3.connect(legacy.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name IN ('projects','project_members','tickets')").fetchall() == []
        if not with_outbox:
            db.execute("DROP TABLE notification_events")
    before = snapshot_tables(legacy.db_path)
    upgraded = Board(legacy.data_dir, base_url=legacy.base_url)
    after = snapshot_tables(legacy.db_path)
    assert all(after[name] == rows for name, rows in before.items())
    assert upgraded.authenticate(device["token"], "agent")["device_id"] == device["id"]
    assert upgraded.get_thread(first, thread["id"])["messages"][0]["id"] == message["id"]
    assert Path(upgraded.get_attachment(second, attachment["id"])["path"]).read_bytes() == b"Synthetic v1 file"
    assert upgraded.get_thread(first, thread["id"])["tasks"][0]["claim_version"] == task["claim_version"]
    assert upgraded.read_inbox(actor, cursor=cursor)["messages"] == []
    assert upgraded.post_message(second, thread["id"], "Legacy message", attachment_ids=[attachment["id"]],
                                 mentions=["blackoutburst"], idempotency_key="legacy-message")["id"] == message["id"]
    project = shared(upgraded, first)
    assert upgraded.create_ticket(first, project["id"], "First v2 ticket")["number"] == 1
    reopened = Board(legacy.data_dir)
    assert reopened.get_project(second, project["id"])["ticket_counts"]["total"] == 1
    with sqlite3.connect(legacy.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_real_v1_failure_preserves_schema_rows_and_version(tmp_path, monkeypatch):
    legacy, *_ = legacy_fixture(tmp_path)
    before = snapshot_tables(legacy.db_path)
    original = core.migrate_projects
    def fail(db):
        original(db)
        raise RuntimeError("Synthetic failure after all migration DDL")
    monkeypatch.setattr(core, "migrate_projects", fail)
    with pytest.raises(RuntimeError):
        Board(legacy.data_dir)
    assert snapshot_tables(legacy.db_path) == before
    with sqlite3.connect(legacy.db_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    monkeypatch.setattr(core, "migrate_projects", original)
    assert Board(legacy.data_dir)


def test_concurrent_v1_upgrade_and_ticket_updates_are_serialized(tmp_path):
    legacy, first, second, *_ = legacy_fixture(tmp_path)
    barrier = Barrier(2)
    def upgrade(_):
        barrier.wait()
        return Board(legacy.data_dir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        boards = list(pool.map(upgrade, range(2)))
    project = shared(boards[0], first)
    ticket = boards[0].create_ticket(first, project["id"], "No lost updates")
    barrier = Barrier(2)
    def edit(pair):
        board, human = pair
        barrier.wait()
        try:
            return board.update_ticket(human, ticket["id"], 1, description=human["owner_id"])
        except BoardError as error:
            return error.status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(edit, zip(boards, (first, second))))
    assert sum(isinstance(r, dict) for r in results) == 1 and 409 in results
    assert boards[0].get_ticket(first, ticket["id"])["ticket"]["version"] == 2
