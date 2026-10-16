"""Exercise the official SDK client over a real socket with the deployed URL prefix."""
import asyncio
import socket
import threading

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app import create_app


@pytest.mark.asyncio
async def test_official_clients_both_owners_with_proxy_prefix(tmp_path):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    app = create_app(tmp_path / "data", base_url=base + "/board", root_path="/board", filebrowser_url="", allow_local_passwords=False)
    board = app.state.board
    creator = {"kind": "human", "owner_id": "blackoutburst"}
    thread = board.create_thread(creator, "SDK network fixture", ["blackoutburst", "thescreamer"], "network-thread")
    devices = []
    for owner in ["blackoutburst", "thescreamer"]:
        actor = {"kind": "human", "owner_id": owner}
        board.set_policy(actor, True, True, True, True, ["inspect_dns"], ["games.blackoutburst.com"])
        devices.append((owner, board.create_device(actor, "SDK fixture device", ["dns"])))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        for _ in range(500):
            if server.started:
                break
            await asyncio.sleep(.01)
        assert server.started
        for owner, device in devices:
            async with httpx.AsyncClient(headers={"Authorization": "Bearer " + device["token"]}) as http:
                # The public prefix is understood by the parent ASGI app. Nginx also
                # strips it before forwarding; both paths must reach the same tool.
                async with streamable_http_client(base + "/board/mcp", http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        assert len(tools.tools) == 55
                        assert 'release_ticket' in {tool.name for tool in tools.tools}
                        assert {'lookup_human_request', 'lookup_ticket_selection'} <= {tool.name for tool in tools.tools}
                        result = await session.call_tool("read_inbox", {"session_id": owner + "-sdk", "label": "Real socket", "capabilities": ["dns"]})
                        assert not result.isError
                        result = await session.call_tool("post_message", {"session_id": owner + "-sdk", "thread_id": thread["id"], "body": "Verified official SDK: " + owner, "idempotency_key": owner + "-message"})
                        assert not result.isError
        assert len(board.get_thread(creator, thread["id"])["messages"]) == 2
        async with httpx.AsyncClient() as http:
            assert (await http.get(base + "/health")).status_code == 200
            assert (await http.get(base + "/board/api/threads")).status_code == 401
            assert (await http.get(base + "/mcp")).status_code == 401
    finally:
        server.should_exit = True
        await asyncio.to_thread(worker.join, 10)
        listener.close()
        assert not worker.is_alive()
