import base64
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import COOKIE, create_app


@pytest.fixture
def application(tmp_path):
    app = create_app(tmp_path / "data", base_url="http://testserver", filebrowser_url="", allow_local_passwords=True, root_path="")
    app.state.board.ensure_owner("blackoutburst", "Blackoutburst", password="test-Blackoutburst-password")
    app.state.board.ensure_owner("thescreamer", "TheScreamer", password="test-Screamer-password")
    return app


def login(client, username="blackoutburst"):
    password = "test-Blackoutburst-password" if username == "blackoutburst" else "test-Screamer-password"
    response = client.post("/api/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return response.json()["actor"]


def new_thread(client, participants=None):
    response = client.post("/api/threads", json={"title": "Games OAuth and email setup", "participants": participants or ["blackoutburst", "thescreamer"], "idempotency_key": "thread-test"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_human_handoff_and_verified_completion(application):
    screamer = TestClient(application)
    with TestClient(application) as blackoutburst:
        login(blackoutburst)
        login(screamer, "thescreamer")
        thread = new_thread(screamer)
        message = screamer.post(f"/api/threads/{thread}/messages", json={"body": "@blackoutburst Provider requirements attached; do not post secrets.", "mentions": ["blackoutburst"], "idempotency_key": "message-1"}).json()
        reply = blackoutburst.post(f"/api/threads/{thread}/messages", json={"body": "I will review the supplied DNS records.", "reply_to": message["id"], "idempotency_key": "message-2"})
        assert reply.status_code == 200, reply.text
        assert reply.json()["author"]["kind"] == "human"
        task = screamer.post(f"/api/threads/{thread}/tasks", json={"title": "Add supplied DNS records", "instructions": "Review existing SPF before editing. Exact provider values still required.", "requirements": "Provider accepts validation", "destination_kind": "human", "destination_id": "blackoutburst", "idempotency_key": "dns-1"}).json()
        denied = screamer.post(f"/api/tasks/{task['id']}/claim", json={})
        assert denied.status_code == 403
        claimed = blackoutburst.post(f"/api/tasks/{task['id']}/claim", json={}).json()
        assert claimed["claimed"]
        version = claimed["task"]["claim_version"]
        attempt = blackoutburst.post(f"/api/tasks/{task['id']}/update", json={"claim_version": version, "status": "complete", "outcome": "DNS edit attempted"})
        assert attempt.status_code == 400
        completed = blackoutburst.post(f"/api/tasks/{task['id']}/update", json={"claim_version": version, "status": "complete", "outcome": "Supplied records added", "evidence": "Test fixture: authoritative DNS and provider accepted. Application email still separate.", "verified": True})
        assert completed.status_code == 200, completed.text
        assert completed.json()["verified"]


def test_cookie_csrf_and_owner_scoping(application):
    with TestClient(application) as client:
        assert client.get("/api/threads").status_code == 401
        login(client)
        assert client.cookies.get(COOKIE)
        saved = client.headers.pop("X-CSRF-Token")
        assert client.post("/api/settings/devices", json={"label": "Windows", "capabilities": []}).status_code == 403
        client.headers["X-CSRF-Token"] = saved
        assert client.post("/api/settings/devices", json={"label": "Windows", "capabilities": [], "owner_id": "thescreamer"}).status_code == 422
        assert client.post("/api/settings/devices", headers={"Origin": "https://attacker.invalid"}, json={"label": "Windows", "capabilities": []}).status_code == 403
        client.post("/api/logout")
        assert client.get("/api/me").status_code == 401


def test_attachments_authenticated_membership_and_size(application):
    outsider = TestClient(application)
    with TestClient(application) as member:
        login(member)
        login(outsider, "thescreamer")
        thread = new_thread(member, ["blackoutburst"])
        upload = member.post(f"/api/threads/{thread}/attachments", files={"file": ("records.txt", b"DNS fixture", "text/plain")})
        assert upload.status_code == 200, upload.text
        message = member.post(f"/api/threads/{thread}/messages", json={"body": "", "attachment_ids": [upload.json()["id"]], "idempotency_key": "attachment-only"})
        assert message.status_code == 200, message.text
        assert message.json()["attachments"][0]["id"] == upload.json()["id"]
        empty = member.post(f"/api/threads/{thread}/messages", json={"body": "", "idempotency_key": "empty"})
        assert empty.status_code == 400
        url = "/api/attachments/" + upload.json()["id"]
        response = member.get(url)
        assert response.content == b"DNS fixture"
        assert "attachment" in response.headers["content-disposition"]
        assert outsider.get(url).status_code in {403, 404}
        assert member.post(f"/api/threads/{thread}/attachments", files={"file": ("too-large.txt", b"x" * (10*1024*1024 + 1), "text/plain")}).status_code == 413
    assert TestClient(application).get(url).status_code == 401


def test_filebrowser_identity_verified_by_renew(tmp_path):
    calls = []
    def jwt(username):
        claims = json.dumps({"user": {"username": username}, "exp": time.time() + 60}).encode()
        return "header." + base64.urlsafe_b64encode(claims).decode().rstrip("=") + ".signature"

    def provider(request):
        calls.append(request.url.path)
        if request.url.path == "/api/login":
            # Login token's apparent name is not identity authority.
            return httpx.Response(200, text=jwt("blackout"))
        assert request.headers["X-Auth"]
        return httpx.Response(200, text=jwt("TheScreamer"))

    app = create_app(tmp_path / "data", base_url="http://testserver", filebrowser_url="http://filebrowser", filebrowser_transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        response = client.post("/api/login", json={"username": "blackout", "password": "fixture"})
        assert response.status_code == 200, response.text
        assert response.json()["actor"]["owner_id"] == "thescreamer"
        assert calls == ["/api/login", "/api/renew"]


def test_filebrowser_unknown_user_and_failed_renew_denied(tmp_path):
    calls = []
    def provider(request):
        calls.append(request.url.path)
        return httpx.Response(200, text="unverified.token.text") if request.url.path.endswith("login") else httpx.Response(401)
    app = create_app(tmp_path / "data", base_url="http://testserver", filebrowser_url="http://filebrowser", filebrowser_transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        response = client.post("/api/login", json={"username": "admin", "password": "fixture"})
        assert response.status_code == 401
        assert not client.cookies.get(COOKIE)


def rpc(client, token, method, params=None, id=1):
    return client.post("/mcp", headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"},
                       json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})


def call_tool(client, token, tool, args):
    response = rpc(client, token, "tools/call", {"name": tool, "arguments": args})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result


def tool_data(result):
    assert not result.get("isError"), result
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


def test_mcp_same_records_both_owners_revocation_and_human_task_guard(application):
    with TestClient(application) as client:
        login(client)
        blackoutburst = client.get("/api/me").json()["actor"]
        thread = new_thread(client)
        tokens = {}
        for owner in ["blackoutburst", "thescreamer"]:
            actor = {"kind": "human", "owner_id": owner}
            application.state.board.set_policy(actor, enabled=True, allow_read=True, allow_reply=True, allow_requests=True, actions=["inspect_dns"], resources=["games.blackoutburst.com"])
            tokens[owner] = application.state.board.create_device(actor, "Test device", ["dns"])
        for owner, device in tokens.items():
            initialized = rpc(client, device["token"], "initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "board-test", "version": "1"}})
            assert initialized.status_code == 200, initialized.text
            names = {tool["name"] for tool in rpc(client, device["token"], "tools/list").json()["result"]["tools"]}
            assert names == {"read_inbox", "get_thread", "post_message", "create_task", "claim_task", "update_task"}
            inbox = tool_data(call_tool(client, device["token"], "read_inbox", {"session_id": owner + "-chat", "label": "DNS setup", "capabilities": ["dns"]}))
            assert inbox["cursor"]
            message = tool_data(call_tool(client, device["token"], "post_message", {"session_id": owner + "-chat", "thread_id": thread, "body": "MCP reply " + owner, "idempotency_key": "agent-" + owner}))
            assert message["author"]["owner_id"] == owner
            assert message["author"]["kind"] == "agent"
        history = client.get(f"/api/threads/{thread}").json()
        assert len(history["messages"]) == 2
        task = client.post(f"/api/threads/{thread}/tasks", json={"title": "Human DNS edit", "instructions": "Wait for human", "requirements": "exact records", "destination_kind": "human", "destination_id": "blackoutburst", "idempotency_key": "human-task"}).json()
        denied = call_tool(client, tokens["blackoutburst"]["token"], "claim_task", {"session_id": "blackoutburst-chat", "task_id": task["id"]})
        assert denied["isError"]
        application.state.board.revoke_device(blackoutburst, tokens["blackoutburst"]["id"])
        assert rpc(client, tokens["blackoutburst"]["token"], "tools/list").status_code == 401
        assert rpc(client, tokens["thescreamer"]["token"], "tools/list").status_code == 200


def test_mcp_requires_agent_credentials_and_cannot_impersonate_owner(application):
    with TestClient(application) as client:
        login(client)
        assert rpc(client, client.cookies[COOKIE], "tools/list").status_code == 401
        device = application.state.board.create_device({"kind": "human", "owner_id": "blackoutburst"}, "Windows", [])
        application.state.board.set_policy({"kind": "human", "owner_id": "blackoutburst"}, enabled=True, allow_read=True, allow_reply=True, allow_requests=False, actions=[], resources=[])
        result = call_tool(client, device["token"], "read_inbox", {"session_id": "my-chat", "label": "Work", "owner_id": "thescreamer"})
        if not result.get("isError"):
            assert tool_data(result)["session"]["owner_id"] == "blackoutburst"
