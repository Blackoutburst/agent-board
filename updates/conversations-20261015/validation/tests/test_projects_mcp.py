"""MCP permissions, typed patch semantics, and actual official SDK transport."""
import asyncio
import json
import socket
import threading

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app import create_app
from test_api import application, call_tool, rpc, tool_data


HUMAN = {"kind": "human", "owner_id": "blackoutburst"}


def register(board, client, *, requests=False, label="MCP fixture", session="project-chat"):
    board.set_policy(HUMAN, True, True, True, requests, [], [])
    device = board.create_device(HUMAN, label, [])
    result = call_tool(client, device["token"], "read_inbox", {"session_id": session, "label": label, "capabilities": []})
    tool_data(result)
    return device, session


def seed_project(board, participants=None):
    project = board.create_project(HUMAN, "Shared platform", participants or ["blackoutburst", "thescreamer"], idempotency_key="project-seed")
    ticket = board.create_ticket(HUMAN, project["id"], "Review login", idempotency_key="ticket-seed")
    return project, ticket


def test_mcp_read_policy_does_not_grant_metadata_mutations(application):
    board = application.state.board
    project, ticket = seed_project(board)
    with TestClient(application) as client:
        device, session = register(board, client)
        reads = [
            ("list_projects", {}), ("get_project", {"project_id": project["id"]}),
            ("list_tickets", {"project_id": project["id"]}), ("get_ticket", {"ticket_id": ticket["id"]}),
        ]
        for name, args in reads:
            assert not call_tool(client, device["token"], name, {"session_id": session, **args}).get("isError")
        mutations = [
            ("create_project", {"name": "Denied", "participants": ["blackoutburst"], "idempotency_key": "denied-project"}),
            ("update_project", {"project_id": project["id"], "expected_version": 1, "changes": {"context": "Denied"}}),
            ("create_ticket", {"project_id": project["id"], "title": "Denied", "idempotency_key": "denied-ticket"}),
            ("claim_ticket", {"ticket_id": ticket["id"], "expected_version": 1}),
            ("update_ticket", {"ticket_id": ticket["id"], "expected_version": 1, "changes": {"title": "Denied"}}),
        ]
        for name, args in mutations:
            denied = call_tool(client, device["token"], name, {"session_id": session, **args})
            assert denied.get("isError"), (name, denied)
        board.set_policy(HUMAN, True, False, True, False, [], [])
        for name, args in reads:
            assert call_tool(client, device["token"], name, {"session_id": session, **args}).get("isError")
        assert board.get_ticket(HUMAN, ticket["id"])["ticket"]["claimant"] is None
        assert board.get_project(HUMAN, project["id"])["version"] == 1


def test_mcp_membership_session_claim_and_patch_contract(application):
    board = application.state.board
    project, ticket = seed_project(board)
    private = board.create_project({"kind": "human", "owner_id": "thescreamer"}, "Private", ["thescreamer"], idempotency_key="private")
    with TestClient(application) as client:
        device, session = register(board, client, requests=True)

        def call(name, **args):
            return call_tool(client, device["token"], name, {"session_id": session, **args})

        assert call("get_project", project_id=private["id"]).get("isError")
        assert call("list_tickets", project_id=private["id"]).get("isError")
        assert call("create_ticket", project_id=private["id"], title="Denied", idempotency_key="private-denied").get("isError")
        assert call("update_ticket", ticket_id=ticket["id"], expected_version=1, changes={"title": "Unclaimed"}).get("isError")
        assert call("claim_ticket", ticket_id=ticket["id"], expected_version=True).get("isError")
        ticket = tool_data(call("claim_ticket", ticket_id=ticket["id"], expected_version=1))
        assert ticket["claimant"]["session_id"] == session
        updated = tool_data(call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"],
                                changes={"description": "Preserve assignment"}))
        assert updated["assignee_owner_id"] == "blackoutburst"
        ticket = updated
        assert call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"], changes={"status": None}).get("isError")
        assert call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"], changes={"claimant": "another"}).get("isError")
        second, second_session = register(board, client, requests=True, label="Second device", session="second-chat")
        conflict = call_tool(client, second["token"], "claim_ticket", {
            "session_id": second_session, "ticket_id": ticket["id"], "expected_version": ticket["version"],
        })
        assert conflict.get("isError")
        # Explicit null is transported, rather than silently omitted; only a
        # human may change assignment, so the core rejects this agent edit.
        denied = call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"],
                      changes={"assignee_owner_id": None})
        assert denied.get("isError")
        released = tool_data(call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"],
                                 changes={"release_claim": True}))
        assert released["assignee_owner_id"] == "blackoutburst"
        assert released["claimant"] is None
        board.revoke_device(HUMAN, device["id"])
        assert rpc(client, device["token"], "tools/list").status_code == 401


@pytest.mark.asyncio
async def test_projects_official_sdk_schemas_and_roundtrip(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    application = create_app(tmp_path / "data", base_url=base + "/board", root_path="/board", filebrowser_url="", allow_local_passwords=False)
    board = application.state.board
    board.set_policy(HUMAN, True, True, True, True, [], [])
    device = board.create_device(HUMAN, "Official SDK projects", [])
    server = uvicorn.Server(uvicorn.Config(application, log_level="error", access_log=False))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + device["token"]}) as http:
            async with streamable_http_client(base + "/board/mcp", http_client=http) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    schema = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
                    assert len(schema) == 15
                    for name in ("update_ticket", "update_project"):
                        assert set(schema[name]["required"]) == {"session_id", "ticket_id" if name == "update_ticket" else "project_id", "expected_version", "changes"}
                        assert schema[name]["properties"]["expected_version"]["type"] == "integer"
                        assert schema[name]["properties"]["expected_version"]["minimum"] == 1
                        nested = schema[name]["$defs"]["TicketChanges" if name == "update_ticket" else "ProjectChanges"]
                        assert nested["additionalProperties"] is False
                    assert not (await session.call_tool("read_inbox", {"session_id": "sdk-projects", "label": "SDK projects", "capabilities": []})).isError

                    async def call(tool_name, **args):
                        result = await session.call_tool(tool_name, {"session_id": "sdk-projects", **args})
                        assert not result.isError, result
                        return result.structuredContent or json.loads(result.content[0].text)

                    project = await call("create_project", name="MCP project", participants=["blackoutburst"], idempotency_key="sdk-project")
                    ticket = await call("create_ticket", project_id=project["id"], title="MCP ticket", idempotency_key="sdk-ticket")
                    retrieved = await call("get_ticket", ticket_id=ticket["id"])
                    assert retrieved["ticket"]["id"] == ticket["id"]
                    ticket = await call("claim_ticket", ticket_id=ticket["id"], expected_version=ticket["version"])
                    ticket = await call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"],
                                        changes={"description": "New detail"})
                    assert ticket["assignee_owner_id"] == "blackoutburst"
                    rejected = await session.call_tool("update_ticket", {
                        "session_id": "sdk-projects", "ticket_id": ticket["id"], "expected_version": ticket["version"],
                        "changes": {"assignee_owner_id": None},
                    })
                    assert rejected.isError
                    ticket = await call("update_ticket", ticket_id=ticket["id"], expected_version=ticket["version"],
                                        changes={"release_claim": True})
                    assert ticket["claimant"] is None
                    project = await call("update_project", project_id=project["id"], expected_version=project["version"],
                                         changes={"context": "SDK roundtrip verified"})
                    assert project["context"] == "SDK roundtrip verified"
        async with httpx.AsyncClient() as http:
            assert (await http.get(base + "/health")).json()["features"] == ["projects", "tickets", "conversations_on_message"]
            assert (await http.get(base + "/board/api/projects")).status_code == 401
    finally:
        server.should_exit = True
        await asyncio.to_thread(worker.join, 10)
        listener.close()
        assert not worker.is_alive()
