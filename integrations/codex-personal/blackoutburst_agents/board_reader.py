"""Explicit, bounded reads through the board's existing paired MCP connection.

This adapter never pairs a device, edits credentials, claims work or publishes
messages. Registration records this read-only chat with autonomous=False.
"""
from __future__ import annotations

import copy
import http.client
import json
import math
import os
from pathlib import Path
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


MAX_CONNECTION_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PROTOCOL_VERSION = "2025-06-18"
READ_TOOLS = frozenset({"get_ticket", "get_thread", "get_message"})


class BoardReadError(Exception):
    """A fixed diagnostic code, never remote text or credential material."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _constant(_value):
    raise ValueError("non-finite number")


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _json(data, code):
    try:
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="strict")
        return json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant, parse_float=_float)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BoardReadError(code) from None


def _text(value, maximum):
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _endpoint(value):
    if not _text(value, 2048) or any(char.isspace() for char in value):
        raise BoardReadError("invalid_connection")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
        safe_scheme = parts.scheme == "https" or (
            parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1", "::1"))
        if (not safe_scheme or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.path != "/mcp"
                or parts.query or parts.fragment or "?" in value or "#" in value
                or "\\" in value or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("invalid endpoint")
    except ValueError:
        raise BoardReadError("invalid_connection") from None
    return value


def _connection_file(path):
    """Open without following the final symlink and validate the opened inode."""
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise BoardReadError("unsupported_platform")
    fd = None
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise BoardReadError("insecure_connection_file")
        if info.st_size > MAX_CONNECTION_BYTES:
            raise BoardReadError("connection_too_large")
        chunks, total = [], 0
        while True:
            part = os.read(fd, min(8192, MAX_CONNECTION_BYTES + 1 - total))
            if not part:
                break
            total += len(part)
            if total > MAX_CONNECTION_BYTES:
                raise BoardReadError("connection_too_large")
            chunks.append(part)
        config = _json(b"".join(chunks), "invalid_connection")
    except BoardReadError:
        raise
    except (OSError, TypeError, ValueError):
        raise BoardReadError("connection_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
    return _connection_data(config)


def _connection_data(config):
    """Validate already decoded held bytes without reopening a path."""
    if not isinstance(config, dict):
        raise BoardReadError("invalid_connection")
    endpoint = _endpoint(config.get("mcp_url"))
    token = config.get("token")
    if (not _text(token, 8192) or not token.isascii() or any(c.isspace() for c in token)
            or config.get("client") not in ("codex", "claude-code", "other")
            or not _text(config.get("identity_key"), 200)
            or not _text(config.get("name"), 200)):
        raise BoardReadError("invalid_connection")
    return {"mcp_url": endpoint, **{key: config[key] for key in ("token", "client", "identity_key", "name")}}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _tls_context():
    """Retain normal verification and add macOS's system PEM roots when present."""
    try:
        context = ssl.create_default_context()
        # Python.org installations may not have run Install Certificates.command.
        # Add the OS trust store; never disable verification or retry untrusted TLS.
        if sys.platform == "darwin" and Path("/etc/ssl/cert.pem").is_file():
            context.load_verify_locations(cafile="/etc/ssl/cert.pem")
        return context
    except (OSError, ValueError):
        raise BoardReadError("tls_unavailable") from None


def _sse(data):
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeError:
        raise BoardReadError("invalid_response") from None
    events, lines = [], []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") + [""]:
        if not line:
            if lines:
                events.append("\n".join(lines))
                lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "data":
            lines.append(value)
        elif field not in ("event", "id", "retry"):
            raise BoardReadError("invalid_response")
    if len(events) != 1:
        raise BoardReadError("invalid_response")
    return _json(events[0], "invalid_response")


class BoardReader:
    """Use a pinned paired identity for explicitly selected ticket/thread reads."""

    @classmethod
    def from_connection_file(cls, path, *, session_id, timeout=20):
        if not _text(session_id, 200):
            raise BoardReadError("invalid_session")
        if (type(timeout) not in (int, float) or not math.isfinite(timeout)
                or not 0 < timeout <= 60):
            raise BoardReadError("invalid_timeout")
        return cls(_connection_file(path), session_id, float(timeout))

    def __init__(self, config, session_id, timeout):
        self._config = config
        self.session_id = session_id
        self.timeout = timeout
        self._next_id = 0
        self._initialized = False
        self._registered = False
        self._transport_session = None
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=_tls_context()))

    @property
    def endpoint(self):
        return self._config['mcp_url']

    def _rpc(self, method, params):
        self._next_id += 1
        request_id = self._next_id
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method,
                               "params": params}, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise BoardReadError("invalid_arguments") from None
        if len(body) > MAX_CONNECTION_BYTES:
            raise BoardReadError("invalid_arguments")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + self._config["token"],
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if self._transport_session is not None:
            headers["Mcp-Session-Id"] = self._transport_session
        request = urllib.request.Request(self._config["mcp_url"], body, headers)
        deadline = time.monotonic() + self.timeout
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.geturl() != self._config["mcp_url"]:
                    raise BoardReadError("redirect_blocked")
                if response.status != 200:
                    raise BoardReadError("http_error")
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                    raise BoardReadError("response_too_large")
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in ("application/json", "text/event-stream"):
                    raise BoardReadError("invalid_response")
                parts, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BoardReadError("timeout")
                    # HTTPResponse.read1 performs at most one underlying read.
                    # Shrink the socket deadline as well to bound a slow stream.
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                    if sock is not None:
                        sock.settimeout(remaining)
                    read = getattr(response, "read1", response.read)
                    chunk = read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                    if time.monotonic() > deadline:
                        raise BoardReadError("timeout")
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise BoardReadError("response_too_large")
                    parts.append(chunk)
                session = response.headers.get("Mcp-Session-Id")
                if session is not None:
                    if (not _text(session, 512) or not session.isascii()
                            or any(c.isspace() for c in session)
                            or (self._transport_session is not None and session != self._transport_session)):
                        raise BoardReadError("invalid_response")
                    self._transport_session = session
                raw = b"".join(parts)
        except BoardReadError:
            raise
        except urllib.error.HTTPError as error:
            code = ("redirect_blocked" if 300 <= error.code < 400 else
                    "authentication_failed" if error.code == 401 else
                    "permission_denied" if error.code == 403 else "http_error")
            error.close()
            raise BoardReadError(code) from None
        except (TimeoutError,):
            raise BoardReadError("timeout") from None
        except urllib.error.URLError as error:
            raise BoardReadError("timeout" if isinstance(error.reason, TimeoutError) else "connection_failed") from None
        except (OSError, ValueError, http.client.HTTPException):
            raise BoardReadError("connection_failed") from None
        result = _sse(raw) if content_type == "text/event-stream" else _json(raw, "invalid_response")
        if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not int or result["id"] != request_id
                or ("error" in result) == ("result" in result)):
            raise BoardReadError("invalid_response")
        if "error" in result:
            raise BoardReadError("rpc_rejected")
        return result["result"]

    def _initialize(self):
        if self._initialized:
            return
        result = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "blackoutburst-personal-context", "version": "1"}})
        if not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL_VERSION:
            raise BoardReadError("protocol_mismatch")
        self._initialized = True

    def _tool(self, name, args):
        # These callers seal full source text and validate claim/dependency
        # fields. Explicitly opt into the legacy full representation; never
        # mistake an MCP preview for the original source. The strict caller
        # argument allowlists still run before this trusted injection.
        if name in {'get_ticket','get_thread','get_project','get_work','claim_ticket',
                    'claim_next_ticket','lookup_ticket_selection','renew_ticket',
                    'update_ticket','release_ticket','request_human_input','lookup_human_request','register_agent','get_message','open_message_feed',
                    'read_message_feed','checkpoint_message_feed','post_message',
                    'open_delegation_family','get_delegation_family','lookup_delegation_family',
                    'stop_delegation_family','accept_delegation_result','finalize_delegation_family'}:
            args={**args,'view':'full'}
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        if not isinstance(result, dict) or ("isError" in result and type(result["isError"]) is not bool):
            raise BoardReadError("invalid_response")
        if result.get("isError"):
            raise BoardReadError("tool_rejected")
        if "structuredContent" in result:
            value = result["structuredContent"]
        else:
            content = result.get("content")
            if (not isinstance(content, list) or len(content) != 1
                    or not isinstance(content[0], dict) or content[0].get("type") != "text"):
                raise BoardReadError("invalid_response")
            value = _json(content[0].get("text"), "invalid_response")
        if not isinstance(value, dict):
            raise BoardReadError("invalid_response")
        if set(value) == {"result"}:
            value = value["result"]
        if not isinstance(value, dict):
            raise BoardReadError("invalid_response")
        return value

    def register(self):
        self._registered = False
        self._initialize()
        result = self._tool("register_agent", {"session_id": self.session_id,
            "agent_kind": self._config["client"], "identity_key": self._config["identity_key"],
            "name": self._config["name"], "label": self._config["name"][:192] + " context",
            "skills": [], "autonomous": False})
        if not isinstance(result.get("session"), dict) or result["session"].get("id") != self.session_id:
            raise BoardReadError("session_mismatch")
        self._registered = True
        return result

    def call(self, name, args):
        if not isinstance(name, str) or name not in READ_TOOLS:
            raise BoardReadError("tool_not_allowed")
        if not isinstance(args, dict) or "session_id" in args:
            raise BoardReadError("invalid_arguments")
        try:
            arguments = copy.deepcopy(args)
            json.dumps(arguments, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise BoardReadError("invalid_arguments") from None
        if not self._registered:
            self.register()
        arguments["session_id"] = self.session_id
        return self._tool(name, arguments)
