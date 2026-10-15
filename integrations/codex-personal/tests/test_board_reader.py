"""MCP transport and credential handling tests; no real board or credentials."""
import io
import json
import os
from pathlib import Path
import ssl
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from blackoutburst_agents.board_reader import (
    BoardReader, BoardReadError, MAX_CONNECTION_BYTES, MAX_RESPONSE_BYTES, PROTOCOL_VERSION,
    _NoRedirect, _tls_context,
)


TOKEN = "TEST_SECRET_NEVER_IN_DIAGNOSTICS"
URL = "https://board.example.test/mcp"


class Response:
    def __init__(self, body, *, content_type="application/json", headers=None, url=URL, status=200):
        self._body = io.BytesIO(body)
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status, self.url, self.closed = status, url, False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def geturl(self):
        return self.url

    def read(self, size):
        return self._body.read(size)

    read1 = read


class Opener:
    def __init__(self, responses=None):
        self.requests = []
        self.responses = list(responses or [])

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.responses:
            result = self.responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        payload = json.loads(request.data)
        if payload["method"] == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}
        else:
            name = payload["params"]["name"]
            value = {"read": name}
            if name == "register_agent":
                value["session"] = {"id": payload["params"]["arguments"]["session_id"]}
            result = {"structuredContent": {"result": value}, "content": []}
        return Response(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result}).encode())


class BoardReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "codex.json"
        self.config = {"mcp_url": URL, "token": TOKEN, "client": "codex",
                       "identity_key": "paired-identity", "name": "Codex · Test Mac"}
        self.write_config()

    def write_config(self, config=None):
        self.path.write_text(json.dumps(config or self.config))
        self.path.chmod(0o600)

    def reader(self, opener=None, **kwargs):
        reader = BoardReader.from_connection_file(self.path, session_id="explicit-session", **kwargs)
        reader._opener = opener or Opener()
        return reader

    def assert_code(self, code, operation):
        with self.assertRaises(BoardReadError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(TOKEN, repr(caught.exception))
        return caught.exception

    def rpc_response(self, result, request_id=1, **kwargs):
        return Response(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode(), **kwargs)

    def initialized(self, response):
        reader = self.reader(Opener([response]))
        reader._initialized = reader._registered = True
        return reader

    def test_registration_uses_paired_identity_and_explicit_non_autonomous_chat(self):
        reader = self.reader()
        original = self.path.read_bytes()
        result = reader.register()
        self.assertEqual(result, {"read": "register_agent", "session": {"id": "explicit-session"}})
        requests = [json.loads(req.data) for req, _ in reader._opener.requests]
        self.assertEqual(requests[0]["params"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(requests[1]["params"], {"name": "register_agent", "arguments": {
            "session_id": "explicit-session", "agent_kind": "codex", "identity_key": "paired-identity",
            "name": "Codex · Test Mac", "label": "Codex · Test Mac context",
            "skills": [], "autonomous": False}})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(reader._opener.requests[0][0].get_header("Authorization"), "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, repr(reader))

    def test_reads_auto_register_once_and_leave_caller_arguments_unmodified(self):
        reader = self.reader()
        args = {"ticket_id": "chosen-ticket"}
        self.assertEqual(reader.call("get_ticket", args), {"read": "get_ticket"})
        self.assertEqual(reader.call("get_thread", {"thread_id": "chosen-thread", "limit": 50}), {"read": "get_thread"})
        requests = [json.loads(req.data) for req, _ in reader._opener.requests]
        self.assertEqual(len(requests), 4)
        self.assertEqual(args, {"ticket_id": "chosen-ticket"})
        self.assertEqual(requests[2]["params"]["arguments"]["session_id"], "explicit-session")

    def test_long_paired_name_is_preserved_and_session_label_fits_board_limit(self):
        self.write_config({**self.config, "name": "n" * 200})
        reader = self.reader()
        reader.register()
        arguments = json.loads(reader._opener.requests[1][0].data)["params"]["arguments"]
        self.assertEqual(arguments["name"], "n" * 200)
        self.assertEqual(arguments["label"], "n" * 192 + " context")

    def test_forbidden_tools_and_session_overrides_never_contact_board(self):
        reader = self.reader()
        for name in ("post_message", "claim_ticket", "get_work", "read_inbox", "register_agent", "get_project"):
            self.assert_code("tool_not_allowed", lambda: reader.call(name, {}))
        self.assert_code("invalid_arguments", lambda: reader.call("get_ticket", {"session_id": "other"}))
        self.assert_code("invalid_arguments", lambda: reader.call("get_thread", []))
        self.assert_code("invalid_arguments", lambda: reader.call("get_thread", {"limit": float("nan")}))
        self.assertEqual(reader._opener.requests, [])

    def test_invalid_session_and_timeout_are_rejected(self):
        for value in ("", "   ", "x\ny", None, "s" * 201):
            self.assert_code("invalid_session", lambda: BoardReader.from_connection_file(self.path, session_id=value))
        for value in (0, -1, 61, float("nan"), float("inf"), True, "20"):
            self.assert_code("invalid_timeout", lambda: self.reader(timeout=value))

    def test_file_requires_private_mode_and_current_owner(self):
        self.path.chmod(0o644)
        self.assert_code("insecure_connection_file", self.reader)
        self.path.chmod(0o600)
        with patch("blackoutburst_agents.board_reader.os.geteuid", return_value=os.geteuid() + 1):
            self.assert_code("insecure_connection_file", self.reader)

    def test_symlink_directory_fifo_and_missing_file_are_rejected(self):
        target = self.path.with_name("real.json")
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assert_code("connection_unavailable", self.reader)
        self.path.unlink()
        self.path.mkdir()
        self.assert_code("insecure_connection_file", self.reader)
        self.path.rmdir()
        os.mkfifo(self.path, 0o600)
        self.assert_code("insecure_connection_file", self.reader)
        self.path.unlink()
        self.assert_code("connection_unavailable", self.reader)

    def test_file_size_and_strict_json_limits(self):
        self.path.write_bytes(b"x" * (MAX_CONNECTION_BYTES + 1))
        self.assert_code("connection_too_large", self.reader)
        for value in (b'{"token":"a","token":"b"}', b'{"a":NaN}', b'{"a":1e999}', b'\xff', b'[]'):
            self.path.write_bytes(value)
            self.assert_code("invalid_connection", self.reader)

    def test_endpoint_requires_exact_mcp_https_without_credentials_or_query(self):
        for value in ("http://example.test/mcp", "https://user:pass@example.test/mcp",
                      "https://example.test/mcp?q=1", "https://example.test/mcp#fragment",
                      "https://example.test/mcp?", "https://example.test/mcp#",
                      "https://example.test/mcp/", "https://example.test/other",
                      "file:///mcp", "https://example.test:99999/mcp", "https://exam ple.test/mcp"):
            with self.subTest(value=value):
                self.write_config({**self.config, "mcp_url": value})
                self.assert_code("invalid_connection", self.reader)
        for value in ("http://127.0.0.1:12345/mcp", "http://localhost:12345/mcp", "http://[::1]:12345/mcp"):
            self.write_config({**self.config, "mcp_url": value})
            self.reader()

    def test_config_fields_reject_header_injection_or_unknown_client(self):
        for key, value in (("token", "x\r\nInjected: x"), ("token", "é"), ("token", "x y"),
                           ("client", "unknown"), ("identity_key", ""), ("name", "x\ny")):
            self.write_config({**self.config, key: value})
            self.assert_code("invalid_connection", self.reader)

    def test_rpc_id_version_and_envelope_must_match(self):
        bodies = [
            {"jsonrpc": "2.0", "id": 2, "result": {}},
            {"jsonrpc": "2.0", "id": True, "result": {}},
            {"jsonrpc": "2.0", "id": "1", "result": {}},
            {"jsonrpc": "1.0", "id": 1, "result": {}},
            {"jsonrpc": "2.0", "id": 1},
            {"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}},
        ]
        for value in bodies:
            reader = self.initialized(Response(json.dumps(value).encode()))
            self.assert_code("invalid_response", lambda: reader.call("get_ticket", {}))

    def test_remote_error_text_and_network_errors_never_escape(self):
        response = Response(json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"message": TOKEN}}).encode())
        reader = self.initialized(response)
        self.assert_code("rpc_rejected", lambda: reader.call("get_ticket", {}))
        reader = self.initialized(self.rpc_response({"isError": True, "content": [{"type": "text", "text": TOKEN}]}))
        self.assert_code("tool_rejected", lambda: reader.call("get_ticket", {}))
        for error, code in ((urllib.error.URLError(TOKEN), "connection_failed"),
                            (TimeoutError(TOKEN), "timeout"),
                            (urllib.error.URLError(TimeoutError(TOKEN)), "timeout")):
            reader = self.reader(Opener([error]))
            self.assert_code(code, reader.register)

    def test_http_errors_and_redirects_are_fixed_safe_diagnostics(self):
        for status, code in ((302, "redirect_blocked"), (307, "redirect_blocked"),
                             (401, "authentication_failed"), (403, "permission_denied"), (500, "http_error")):
            error = urllib.error.HTTPError(URL, status, TOKEN, {"Location": "https://evil.test/mcp"}, io.BytesIO(TOKEN.encode()))
            reader = self.reader(Opener([error]))
            self.assert_code(code, reader.register)
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, TOKEN, {}, "https://evil.test"))
        reader = self.reader(Opener([Response(b"{}", url="https://evil.test/mcp")]))
        self.assert_code("redirect_blocked", reader.register)

    def test_response_bounds_apply_with_and_without_content_length(self):
        for response in (Response(b"{}", headers={"Content-Length": str(MAX_RESPONSE_BYTES + 1)}),
                         Response(b"x" * (MAX_RESPONSE_BYTES + 1))):
            reader = self.reader(Opener([response]))
            self.assert_code("response_too_large", reader.register)
            self.assertTrue(response.closed)

    def test_slow_stream_exceeding_absolute_deadline_is_rejected(self):
        reader = self.reader(Opener([Response(b"{}")]), timeout=1)
        with patch("blackoutburst_agents.board_reader.time.monotonic", side_effect=[10, 10.1, 11.1]):
            self.assert_code("timeout", reader.register)

    def test_json_malformed_utf8_duplicate_keys_and_nonfinite_numbers_rejected(self):
        for body in (b'\xff', b'{"jsonrpc":"2.0","id":1,"id":1,"result":{}}',
                     b'{"jsonrpc":"2.0","id":1,"result":{"x":NaN}}',
                     b'{"jsonrpc":"2.0","id":1,"result":{"x":1e999}}'):
            reader = self.initialized(Response(body))
            self.assert_code("invalid_response", lambda: reader.call("get_ticket", {}))

    def test_sse_single_finite_result_and_json_text_fallback(self):
        value = {"jsonrpc": "2.0", "id": 1, "result": {"content": [
            {"type": "text", "text": json.dumps({"result": {"ticket": {"id": "t"}}})}]}}
        sse = (": keepalive\nevent: message\nid: 1\ndata: " + json.dumps(value) + "\n\n").encode()
        reader = self.initialized(Response(sse, content_type="text/event-stream; charset=utf-8"))
        self.assertEqual(reader.call("get_ticket", {}), {"ticket": {"id": "t"}})

    def test_sse_multiple_results_empty_invalid_and_unknown_type_rejected(self):
        value = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})
        for body in (b": keepalive\n\n", ("data: " + value + "\n\ndata: " + value + "\n\n").encode(),
                     b"data: [DONE]\n\n", b"data: \xff\n\n"):
            reader = self.initialized(Response(body, content_type="text/event-stream"))
            self.assert_code("invalid_response", lambda: reader.call("get_ticket", {}))
        reader = self.initialized(Response(b"{}", content_type="text/html"))
        self.assert_code("invalid_response", lambda: reader.call("get_ticket", {}))

    def test_negotiated_protocol_must_match(self):
        reader = self.reader(Opener([self.rpc_response({"protocolVersion": "1999-01-01"})]))
        self.assert_code("protocol_mismatch", reader.register)
        self.assertEqual(len(reader._opener.requests), 1)

    def test_transport_session_is_retained_without_overriding_chat_identity(self):
        response = self.rpc_response({"protocolVersion": PROTOCOL_VERSION}, headers={"Mcp-Session-Id": "transport-session"})
        reader = self.reader(Opener([response]))
        reader.register()
        self.assertEqual(reader._opener.requests[1][0].get_header("Mcp-session-id"), "transport-session")
        arguments = json.loads(reader._opener.requests[1][0].data)["params"]["arguments"]
        self.assertEqual(arguments["session_id"], "explicit-session")

    def test_content_is_not_returned_unless_tool_result_is_valid(self):
        for result in ({"isError": "false", "structuredContent": {}}, {"structuredContent": []},
                       {"structuredContent": {"result": []}}, {"content": []},
                       {"content": [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}]}):
            reader = self.initialized(self.rpc_response(result))
            self.assert_code("invalid_response", lambda: reader.call("get_ticket", {}))

    def test_registration_session_must_match_before_any_ticket_read(self):
        for value in ({}, {"session": []}, {"session": {"id": "other-session"}}):
            reader = self.initialized(self.rpc_response({"structuredContent": value}))
            self.assert_code("session_mismatch", reader.register)
            self.assertFalse(reader._registered)
            self.assertEqual(len(reader._opener.requests), 1)

    def test_macos_adds_system_roots_without_disabling_tls_verification(self):
        context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
        with patch("blackoutburst_agents.board_reader.sys.platform", "darwin"), \
             patch("blackoutburst_agents.board_reader.Path.is_file", return_value=True), \
             patch("blackoutburst_agents.board_reader.ssl.create_default_context", return_value=context) as create:
            self.assertIs(_tls_context(), context)
        create.assert_called_once_with()
        context.load_verify_locations.assert_called_once_with(cafile="/etc/ssl/cert.pem")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_other_systems_and_absent_mac_roots_keep_default_tls_context(self):
        for system, present in (("linux", True), ("darwin", False)):
            context = Mock(check_hostname=True, verify_mode=ssl.CERT_REQUIRED)
            with patch("blackoutburst_agents.board_reader.sys.platform", system), \
                 patch("blackoutburst_agents.board_reader.Path.is_file", return_value=present), \
                 patch("blackoutburst_agents.board_reader.ssl.create_default_context", return_value=context):
                self.assertIs(_tls_context(), context)
            context.load_verify_locations.assert_not_called()
            self.assertTrue(context.check_hostname)
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_tls_setup_failures_are_safe_and_do_not_fall_back_to_unverified(self):
        with patch("blackoutburst_agents.board_reader.ssl.create_default_context", side_effect=OSError(TOKEN)):
            self.assert_code("tls_unavailable", _tls_context)
        context = Mock()
        context.load_verify_locations.side_effect = ssl.SSLError(TOKEN)
        with patch("blackoutburst_agents.board_reader.sys.platform", "darwin"), \
             patch("blackoutburst_agents.board_reader.Path.is_file", return_value=True), \
             patch("blackoutburst_agents.board_reader.ssl.create_default_context", return_value=context):
            self.assert_code("tls_unavailable", _tls_context)


if __name__ == "__main__":
    unittest.main()
