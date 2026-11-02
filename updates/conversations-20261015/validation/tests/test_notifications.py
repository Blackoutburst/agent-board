import asyncio
import json
import sqlite3
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import create_app
from board import Board, BoardError
from notifications import DiscordNotifications


def fixture(tmp_path, handler):
    board = Board(tmp_path / "data", base_url="https://files.example/board")
    for owner, name in [("blackoutburst", "Blackoutburst"), ("thescreamer", "TheScreamer")]:
        board.ensure_owner(owner, name)
    actor = {"kind": "human", "owner_id": "blackoutburst"}
    thread = board.create_thread(actor, "Shared title", ["blackoutburst", "thescreamer"], "thread")
    config = {"bot_token": "fixture-secret", "guild_id": "123", "channel_id": "456",
              "enabled_since": time.time() - 60, "user_ids": {"thescreamer": "789"}}
    (board.data_dir / "discord-notifications.json").write_text(json.dumps(config))
    return board, actor, thread, DiscordNotifications(board, httpx.MockTransport(handler))


def test_delivery_dedup_and_redaction(tmp_path):
    sent = []
    def provider(request):
        sent.append(json.loads(request.content))
        assert request.url.host == "discord.com"
        assert request.url.path == "/api/v10/channels/456/messages"
        return httpx.Response(200, json={"id": "delivered"})
    board, actor, thread, notifier = fixture(tmp_path, provider)
    board.post_message(actor, thread["id"], "Sensitive body @everyone", mentions=["thescreamer"], idempotency_key="m")
    asyncio.run(notifier.dispatch_once())
    asyncio.run(notifier.dispatch_once())
    assert len(sent) == 1
    assert "Sensitive body" not in sent[0]["content"]
    assert "fixture-secret" not in json.dumps(notifier.status(actor))
    assert sent[0]["allowed_mentions"] == {"parse": [], "users": ["789"]}
    assert sent[0]["enforce_nonce"] is True
    assert "https://files.example/board/?thread=" in sent[0]["content"]
    assert notifier.status(actor)["pending"] == 0


def test_rate_limit_and_restart_preserve_outbox(tmp_path):
    sent = []
    def provider(request):
        sent.append(request)
        return httpx.Response(429, json={"retry_after": 120})
    board, actor, thread, notifier = fixture(tmp_path, provider)
    for index in range(2):
        board.post_message(actor, thread["id"], "Attention", mentions=["thescreamer"], idempotency_key=str(index))
    asyncio.run(notifier.dispatch_once())
    assert notifier.status(actor)["pending"] == 2
    board.post_message(actor, thread["id"], "New event during rate limit", mentions=["thescreamer"], idempotency_key="new")
    restarted = DiscordNotifications(Board(board.data_dir, base_url=board.base_url), httpx.MockTransport(provider))
    asyncio.run(restarted.dispatch_once())
    assert len(sent) == 1
    with pytest.raises(BoardError) as error:
        asyncio.run(restarted.test(actor))
    assert error.value.status == 429
    assert len(sent) == 1
    with board._connection() as db:
        assert db.execute("SELECT next_delivery_at FROM notification_delivery_state").fetchone()[0] > time.time() + 100


def test_disabled_and_private_alerts_are_quiet(tmp_path):
    sent = []
    board, actor, thread, notifier = fixture(tmp_path, lambda request: sent.append(request) or httpx.Response(200, json={"id": "x"}))
    private = board.create_thread(actor, "Private", ["blackoutburst"], "private")
    board.post_message(actor, private["id"], "Private body", mentions=["blackoutburst"], idempotency_key="private")
    board.post_message(actor, thread["id"], "Attention", mentions=["thescreamer"], idempotency_key="m")
    (board.data_dir / "discord-notifications.json").unlink()
    asyncio.run(notifier.dispatch_once())
    assert not sent
    assert not notifier.status(actor)["enabled"]
    (board.data_dir / "discord-notifications.json").write_text('[]')
    assert not notifier.status(actor)["enabled"]


def test_legacy_config_cutoff_and_worker_contention(tmp_path, monkeypatch):
    board, actor, thread, notifier = fixture(tmp_path, lambda request: httpx.Response(200, json={"id": "x"}))
    path = board.data_dir / "discord-notifications.json"
    config = json.loads(path.read_text())
    del config["enabled_since"]
    path.write_text(json.dumps(config))
    cutoff = notifier._config()["enabled_since"]
    assert notifier._config()["enabled_since"] == cutoff
    assert abs(cutoff - path.stat().st_mtime) < 0.001
    attempts = []
    async def contended():
        attempts.append(1)
        if len(attempts) == 1:
            raise sqlite3.OperationalError("database is locked")
        raise asyncio.CancelledError
    async def no_wait(_):
        return
    monkeypatch.setattr(notifier, "dispatch_once", contended)
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(notifier.run())
    assert len(attempts) == 2


def test_notification_api_auth_csrf_owner_and_error_redaction(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(403, json={"message": "fixture-secret"})
    app = create_app(tmp_path / "data", base_url="http://testserver", filebrowser_url="", allow_local_passwords=True,
                     root_path="", discord_transport=httpx.MockTransport(provider))
    for owner in ["blackoutburst", "thescreamer"]:
        app.state.board.ensure_owner(owner, owner, password="fixture-password")
    (app.state.board.data_dir / "discord-notifications.json").write_text(json.dumps({
        "bot_token": "fixture-secret", "guild_id": "123", "channel_id": "456", "enabled_since": time.time()}))
    with TestClient(app) as client:
        assert client.get("/api/notifications").status_code == 401
        response = client.post("/api/login", json={"username": "thescreamer", "password": "fixture-password"})
        assert client.post("/api/notifications/test").status_code == 403
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        assert client.post("/api/notifications/test").status_code == 403
        assert not client.get("/api/notifications").json()["can_test"]
        response = client.post("/api/login", json={"username": "blackoutburst", "password": "fixture-password"})
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        status = client.get("/api/notifications")
        assert "fixture-secret" not in status.text
        response = client.post("/api/notifications/test")
        assert response.status_code == 503
        assert "fixture-secret" not in response.text
        assert len(calls) == 1
