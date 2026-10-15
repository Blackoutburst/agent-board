from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

import board as core
from board import Board, BoardError


@pytest.fixture
def setup(tmp_path):
    board = Board(tmp_path / "data", base_url="https://example.test/board/", lease_seconds=30)
    board.ensure_owner("blackoutburst", "Blackoutburst")
    board.ensure_owner("thescreamer", "TheScreamer")
    blackoutburst = board.human_session("blackoutburst")["actor"]
    screamer = board.human_session("thescreamer")["actor"]
    shared = board.create_thread(blackoutburst, "Games rework", ["blackoutburst", "thescreamer"], "shared")
    return board, blackoutburst, screamer, shared["id"]


def agent(board, human, session_id="chat-one", capabilities=None):
    capabilities = capabilities if capabilities is not None else ["dns:read"]
    board.set_policy(human, True, True, True, True, ["verify_dns", "deploy"], ["dns:example.test", "service:games"])
    device = board.create_device(human, "Desktop", capabilities)
    actor = board.authenticate(device["token"], "agent")
    return board.register_session(actor, session_id, session_id, capabilities), device


def task(board, human, thread_id, **kwargs):
    fields = {"actor": human, "thread_id": thread_id, "title": "Verify DNS", "instructions": "Check exact published records",
              "requirements": "TXT equals expected", "destination_kind": "group", "destination_id": "blackoutburst",
              "action": "verify_dns", "resource": "dns:example.test", "required_capabilities": ["dns:read"]}
    fields.update(kwargs)
    return board.create_task(**fields)


def rejects(expected_status, function, *args, **kwargs):
    with pytest.raises(BoardError) as caught:
        function(*args, **kwargs)
    assert caught.value.status == expected_status


def test_passwords_and_credentials_are_salted_hashed_and_revocable(setup):
    board, blackoutburst, screamer, _ = setup
    board.ensure_owner("blackoutburst", "Blackoutburst", "same-password")
    board.ensure_owner("thescreamer", "TheScreamer", "same-password")
    assert board.password_login("Blackoutburst", "same-password")["owner_id"] == "blackoutburst"
    rejects(401, board.password_login, "Blackoutburst", "wrong-password")
    login = board.human_session("blackoutburst")
    assert board.authenticate(login["token"], "human") == login["actor"]
    device = board.create_device(blackoutburst, "Laptop", ["dns:read"])
    with sqlite3.connect(board.db_path) as db:
        passwords = [row[0] for row in db.execute("SELECT password FROM owners")]
        assert passwords[0] != passwords[1]
        assert all("same-password" not in password for password in passwords)
        assert db.execute("SELECT token_hash FROM devices WHERE id=?", (device["id"],)).fetchone()[0] != device["token"]
    assert "token" not in board.get_settings(blackoutburst)["devices"][0]
    board.logout(login["token"])
    rejects(401, board.authenticate, login["token"])
    rejects(401, board.list_threads, login["actor"])


def test_agents_require_live_human_grant_and_registered_scoped_session(setup):
    board, human, other, thread_id = setup
    device = board.create_device(human, "Desktop", ["dns:read"])
    raw = board.authenticate(device["token"])
    rejects(403, board.register_session, raw, "first", "First", ["dns:read"])
    policy = board.set_policy(human, True, True, True, True, ["verify_dns"], ["dns:example.test"])
    assert policy["granted_by"] == "blackoutburst"
    actor = board.register_session(raw, "first", "First", ["dns:read"])
    rejects(403, board.register_session, raw, "more", "More", ["server:admin"])
    rejects(401, board.get_thread, {**actor, "owner_id": "thescreamer"}, thread_id)
    rejects(403, board.get_thread, {**actor, "session_id": "unknown"}, thread_id)
    rejects(403, board.get_thread, raw, thread_id)
    rejects(403, board.set_policy, actor, True, True, True, True, ["deploy"], ["anything"])
    other_device = board.create_device(human, "Laptop", ["dns:read"])
    rejects(409, board.register_session, board.authenticate(other_device["token"]), "first", "Imposter", ["dns:read"])
    board.set_policy(human, False, True, True, True, ["verify_dns"], ["dns:example.test"])
    rejects(403, board.post_message, actor, thread_id, "No longer authorized")
    with sqlite3.connect(board.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='policy_changed'").fetchone()[0] == 2


def test_membership_messages_replies_mentions_and_search(setup):
    board, human, other, shared = setup
    private = board.create_thread(human, "Private", ["blackoutburst"], "private")
    first = board.post_message(human, shared, "Required DKIM record", mentions=["thescreamer"], idempotency_key="m1")
    reply = board.post_message(other, shared, "Ready for verification", reply_to=first["id"], idempotency_key="m2")
    assert reply["author"]["kind"] == "human"
    assert reply["author"]["display_name"] == "TheScreamer"
    assert reply["url"].startswith("https://example.test/board/?")
    assert len(board.list_threads(other, "DKIM")) == 1
    assert board.list_threads(other)[0]["unread_count"] == 2
    board.mark_read(other, shared)
    assert board.list_threads(other)[0]["unread_count"] == 0
    rejects(404, board.get_thread, other, private["id"])
    rejects(404, board.post_message, other, private["id"], "Forbidden")
    rejects(400, board.post_message, human, private["id"], "Wrong reply", reply_to=first["id"])
    rejects(400, board.post_message, human, private["id"], "Wrong mention", mentions=["thescreamer"])
    page = board.get_thread(human, shared, limit=1)
    assert page["messages"][0]["id"] == reply["id"]
    assert board.get_thread(human, shared, before=page["next_before"])["messages"][0]["id"] == first["id"]


def test_retry_payload_identity_and_concurrent_message_retries(setup):
    board, human, other, shared = setup
    first = board.post_message(human, shared, "Same", idempotency_key="stable")
    assert board.post_message(human, shared, "Same", idempotency_key="stable")["id"] == first["id"]
    rejects(409, board.post_message, human, shared, "Changed", idempotency_key="stable")
    assert board.post_message(other, shared, "Same", idempotency_key="stable")["id"] != first["id"]
    item = task(board, human, shared, idempotency_key="task")
    assert task(board, human, shared, idempotency_key="task")["id"] == item["id"]
    rejects(409, task, board, human, shared, title="Changed", idempotency_key="task")
    barrier = Barrier(2)
    def post(_):
        barrier.wait()
        return board.post_message(human, shared, "Concurrent", idempotency_key="simultaneous")["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(post, range(2)))
    assert ids[0] == ids[1]
    assert len(board.get_thread(human, shared)["messages"]) == 3


def test_same_task_atomic_claims_and_same_resource_serialization(setup):
    board, human, _, shared = setup
    first, device = agent(board, human)
    second = board.register_session(board.authenticate(device["token"]), "chat-two", "Chat two", ["dns:read"])
    item = task(board, human, shared)
    barrier = Barrier(2)
    def claim(actor):
        barrier.wait()
        return board.claim_task(actor, item["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first, second)))
    assert sum(result["claimed"] for result in results) == 1
    winner = results[0]["task"]["claimant"]
    other_task = task(board, human, shared, title="Second check")
    assert not board.claim_task(second, other_task["id"])["claimed"]
    board.update_task(winner, item["id"], 1, status="complete", outcome="Matches", evidence="Public DNS query returned expected TXT", verified=True)
    assert board.claim_task(second, other_task["id"])["claimed"]


def test_different_tasks_on_same_resource_race_is_serialized(setup):
    board, human, _, shared = setup
    first, device = agent(board, human)
    second = board.register_session(board.authenticate(device["token"]), "second", "Second", ["dns:read"])
    items = [task(board, human, shared, title=f"Check {i}") for i in range(2)]
    barrier = Barrier(2)
    def claim(pair):
        barrier.wait()
        actor, item = pair
        return board.claim_task(actor, item["id"])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, zip((first, second), items)))
    assert sum(result["claimed"] for result in results) == 1


def test_human_and_agent_task_cannot_hold_same_resource(setup):
    board, human, _, shared = setup
    actor, _ = agent(board, human)
    human_task = task(board, human, shared, destination_kind="human", action="", required_capabilities=[])
    agent_task = task(board, human, shared)
    assert board.claim_task(human, human_task["id"])["claimed"]
    assert not board.claim_task(actor, agent_task["id"])["claimed"]
    board.update_task(human, human_task["id"], 1, status="complete", outcome="Published", evidence="Provider confirmed", verified=True)
    assert board.claim_task(actor, agent_task["id"])["claimed"]


def test_expiry_recovers_durable_progress_and_rejects_stale_claimant(setup, monkeypatch):
    board, human, _, shared = setup
    first, device = agent(board, human)
    second = board.register_session(board.authenticate(device["token"]), "second", "Second", ["dns:read"])
    now = core.time.time()
    monkeypatch.setattr(core.time, "time", lambda: now)
    item = task(board, human, shared)
    claimed = board.claim_task(first, item["id"])["task"]
    board.update_task(first, item["id"], claimed["claim_version"], progress="Changed record, awaiting propagation")
    now += 31
    recovered = board.get_thread(human, shared)["tasks"][0]
    assert recovered["status"] == "recovery"
    assert recovered["claimant"] is None
    assert any("Changed record" in note["note"] for note in recovered["progress"])
    rejects(409, board.update_task, first, item["id"], 1, status="complete", outcome="Done", evidence="Guess", verified=True)
    rejects(409, board.claim_task, second, item["id"])
    reclaimed = board.claim_task(second, item["id"], recovery_evidence="Queried DNS and found existing change; no edit repeated")["task"]
    assert reclaimed["claim_version"] == 2
    assert any("Recovery check" in note["note"] for note in reclaimed["progress"])
    rejects(409, board.update_task, first, item["id"], 1, progress="Late result")
    finished = board.update_task(second, item["id"], 2, status="complete", outcome="Record verified", evidence="dig TXT returned the exact provider record", verified=True)
    assert finished["verified"] and finished["status"] == "complete"
    rejects(409, board.update_task, second, item["id"], 2, progress="Duplicate late result")


def test_human_only_destinations_and_verified_completion(setup):
    board, human, other, shared = setup
    actor, _ = agent(board, human)
    item = task(board, human, shared, destination_kind="human", required_capabilities=[], action="", resource="")
    rejects(403, board.claim_task, actor, item["id"])
    rejects(403, board.claim_task, other, item["id"])
    claimed = board.claim_task(human, item["id"])["task"]
    rejects(403, board.update_task, actor, item["id"], claimed["claim_version"], status="complete", outcome="Done", evidence="Fake", verified=True)
    rejects(400, board.update_task, human, item["id"], 1, status="complete", outcome="Attempted", verified=True)
    rejects(400, board.update_task, human, item["id"], 1, status="complete", outcome="Attempted", evidence="Evidence", verified=False)
    staged = board.update_task(human, item["id"], 1, status="ready_for_verification", outcome="Added provider records", evidence="Provider accepted records")
    assert staged["status"] == "ready_for_verification" and not staged["verified"]
    assert board.update_task(human, item["id"], 1, status="complete", evidence="Provider check and actual email passed", verified=True)["status"] == "complete"


def test_destinations_capabilities_exact_owner_scope_and_dependencies(setup):
    board, human, other, shared = setup
    first, device = agent(board, human)
    second = board.register_session(board.authenticate(device["token"]), "second", "Second", [])
    item = task(board, human, shared)
    rejects(403, board.claim_task, second, item["id"])
    restricted = task(board, human, shared, action="unapproved", resource="service:games")
    rejects(403, board.claim_task, first, restricted["id"])
    unscoped = task(board, human, shared, action="")
    rejects(403, board.claim_task, first, unscoped["id"])
    no_resource = task(board, human, shared, resource="")
    rejects(403, board.claim_task, first, no_resource["id"])
    wrong_resource = task(board, human, shared, resource="dns:other.test")
    rejects(403, board.claim_task, first, wrong_resource["id"])
    dedicated = task(board, human, shared, destination_kind="session", destination_id="second", required_capabilities=[])
    rejects(403, board.claim_task, first, dedicated["id"])
    prerequisite = task(board, human, shared, destination_kind="human", action="", resource="", required_capabilities=[])
    dependent = task(board, human, shared, prerequisites=[prerequisite["id"]])
    assert not board.claim_task(first, dependent["id"])["claimed"]
    board.claim_task(human, prerequisite["id"])
    board.update_task(human, prerequisite["id"], 1, status="complete", outcome="Published", evidence="Authoritative DNS matches", verified=True)
    assert board.claim_task(first, dependent["id"])["claimed"]
    board.set_policy(human, True, True, True, True, ["verify_dns"], [])
    rejects(403, board.update_task, first, dependent["id"], 1, progress="Authorization revoked")
    private = board.create_thread(other, "Only TheScreamer", ["thescreamer"], "private")
    rejects(400, task, board, other, private["id"], destination_id="blackoutburst")
    rejects(400, task, board, human, shared, prerequisites=["missing"])


def test_revoking_device_stops_cached_actors_and_keeps_other_devices(setup):
    board, human, _, shared = setup
    first, device = agent(board, human)
    second, other_device = agent(board, human, "different-computer")
    board.revoke_device(human, device["id"])
    rejects(401, board.authenticate, device["token"])
    rejects(401, board.read_inbox, first)
    rejects(401, board.post_message, first, shared, "Cached revoked identity")
    assert board.get_thread(second, shared)["thread"]["id"] == shared
    assert board.authenticate(other_device["token"])["device_id"] == other_device["id"]
    assert board.get_settings(human)["owner"]["id"] == "blackoutburst"


def test_inbox_cursor_bound_to_each_session_and_task_eligibility_independent(setup):
    board, human, other, shared = setup
    first, device = agent(board, human)
    second = board.register_session(board.authenticate(device["token"]), "second", "Second", ["dns:read"])
    message = board.post_message(other, shared, "Please verify DNS", mentions=["blackoutburst"])
    item = task(board, other, shared)
    one = board.read_inbox(first)
    assert one["session"]["online"]
    assert one["messages"][0]["id"] == message["id"]
    assert one["tasks"][0]["id"] == item["id"]
    assert board.read_inbox(first)["messages"] == []
    assert board.read_inbox(first, one["cursor"])["tasks"][0]["id"] == item["id"]
    two = board.read_inbox(second)
    assert two["messages"][0]["id"] == message["id"]
    assert two["tasks"][0]["id"] == item["id"]
    rejects(400, board.read_inbox, second, one["cursor"])
    rejects(400, board.read_inbox, first, "fabricated")
    browser_one = board.read_inbox(human)
    browser_two_actor = board.human_session("blackoutburst")["actor"]
    assert board.read_inbox(browser_two_actor)["messages"]
    rejects(400, board.read_inbox, browser_two_actor, browser_one["cursor"])
    board.mark_read(human, shared)
    assert board.read_inbox(second)["tasks"][0]["id"] == item["id"]


def test_routing_directory_exposes_safe_live_destinations_and_presence(setup, monkeypatch):
    board, human, other, shared = setup
    actor, own_device = agent(board, human)
    other_actor, other_device = agent(board, other, "other-owner-chat")
    settings = board.get_settings(human)
    assert [d["id"] for d in settings["devices"]] == [own_device["id"]]
    assert {d["id"] for d in settings["routing"]["devices"]} == {own_device["id"], other_device["id"]}
    assert "token" not in str(settings["routing"])
    routed = task(board, human, shared, destination_kind="session", destination_id=other_actor["session_id"])
    assert not routed["destination_online"]
    board.read_inbox(other_actor)
    assert board.get_thread(human, shared)["tasks"][0]["destination_online"]
    now = core.time.time()
    monkeypatch.setattr(core.time, "time", lambda: now + 301)
    assert not board.get_thread(human, shared)["tasks"][0]["destination_online"]
    board.revoke_device(other, other_device["id"])
    assert len(board.get_settings(human)["routing"]["devices"]) == 1
    assert len(board.get_settings(human)["routing"]["sessions"]) == 1


def test_attachments_membership_limits_cross_thread_and_opaque_paths(setup):
    board, human, other, shared = setup
    private = board.create_thread(human, "Private", ["blackoutburst"], "private")
    attachment = board.add_attachment(human, private["id"], "../../screenshot.png", "image/png", b"png-example")
    assert attachment["filename"] == "screenshot.png"
    path = Path(board.get_attachment(human, attachment["id"])["path"])
    assert path.name == attachment["id"] and path.read_bytes() == b"png-example"
    rejects(404, board.get_attachment, other, attachment["id"])
    rejects(400, board.post_message, human, shared, "Wrong conversation", attachment_ids=[attachment["id"]])
    message = board.post_message(human, private["id"], "", attachment_ids=[attachment["id"]])
    assert message["attachments"][0]["size"] == 11
    rejects(413, board.add_attachment, human, shared, "too-big", "application/octet-stream", b"a" * (10 * 1024 * 1024 + 1))
    rejects(404, board.get_attachment, human, "../../outside")


def test_restart_and_full_backup_restore_retain_identity_history_and_attachments(setup, tmp_path):
    from backup import create_backup, restore_backup
    board, human, other, shared = setup
    actor, device = agent(board, human)
    board.ensure_owner("blackoutburst", "Blackoutburst", "restore-password")
    attachment = board.add_attachment(other, shared, "requirements.txt", "text/plain", b"Exact provider records")
    message = board.post_message(other, shared, "Requirements attached", attachment_ids=[attachment["id"]])
    item = task(board, other, shared)
    claimed = board.claim_task(actor, item["id"])["task"]
    board.update_task(actor, item["id"], claimed["claim_version"], progress="Checked public record, awaiting provider confirmation")
    inbox = board.read_inbox(actor)
    restarted = Board(board.data_dir)
    assert restarted.authenticate(device["token"])["owner_id"] == "blackoutburst"
    assert restarted.read_inbox(actor, inbox["cursor"])["messages"] == []
    archive = tmp_path / "backup.zip"
    create_backup(board.data_dir, archive)
    restored_path = tmp_path / "restored"
    restore_backup(archive, restored_path)
    restored = Board(restored_path)
    assert restored.password_login("Blackoutburst", "restore-password")["owner_id"] == "blackoutburst"
    assert restored.authenticate(device["token"])["device_id"] == device["id"]
    thread = restored.get_thread(human, shared)
    assert thread["messages"][0]["id"] == message["id"]
    assert thread["tasks"][0]["progress"][0]["note"].startswith("Checked public record")
    assert Path(restored.get_attachment(human, attachment["id"])["path"]).read_bytes() == b"Exact provider records"
    with sqlite3.connect(restored.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] >= 4
