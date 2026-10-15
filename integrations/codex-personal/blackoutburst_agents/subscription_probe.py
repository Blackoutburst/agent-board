"""A deliberately narrow proof of a personal Codex ChatGPT-authenticated turn.

This module never reads authentication files, starts login, or retains subprocess
output. The only inference it can request is an exact, random nonce response.
Successful proof concerns this local CLI/account only, not hosting eligibility,
another owner's subscription, or future quota availability.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import secrets
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Mapping


DEFAULT_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "multi_agent",
    "browser_use", "browser_use_external", "computer_use", "image_generation",
    "view_image", "skill_search",
)
_ALLOWED_DISABLED_FEATURES = frozenset(DEFAULT_DISABLED_FEATURES)
_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_VERSION_PATTERN = re.compile(r"codex(?:-cli)? ([0-9][A-Za-z0-9._+-]{0,79})\Z")
_ENVIRONMENT_KEYS = frozenset({
    "HOME", "CODEX_HOME", "PATH", "TMPDIR", "TMP", "TEMP", "LANG",
    "USER", "LOGNAME", "SHELL", "TERM", "SYSTEMROOT", "WINDIR", "PATHEXT",
})
_KNOWN_LOGIN_WARNINGS = frozenset({
    "WARNING: proceeding, even though we could not create PATH aliases: Operation not permitted (os error 1)",
})


@dataclass(frozen=True)
class ProbeSettings:
    executable: str | Path
    model: str | None = None
    timeout_seconds: float = 120
    metadata_timeout_seconds: float = 10
    max_output_bytes: int = 65536
    disabled_features: tuple[str, ...] = DEFAULT_DISABLED_FEATURES

    def __post_init__(self) -> None:
        for value in (self.timeout_seconds, self.metadata_timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("Timeout must be a positive finite number")
        if (isinstance(self.max_output_bytes, bool)
                or not isinstance(self.max_output_bytes, int)
                or not 256 <= self.max_output_bytes <= 1024 * 1024):
            raise ValueError("Output limit must be between 256 and 1048576 bytes")
        if self.model is not None and not _MODEL_PATTERN.fullmatch(self.model):
            raise ValueError("Model must be a bounded identifier")
        if (not set(self.disabled_features) <= _ALLOWED_DISABLED_FEATURES
                or len(set(self.disabled_features)) != len(self.disabled_features)):
            raise ValueError("Only reviewed features may be disabled")
        if not _ALLOWED_DISABLED_FEATURES <= set(self.disabled_features):
            raise ValueError("The proof must disable all reviewed execution features")


@dataclass(frozen=True)
class _ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    failure: str | None = None


def validate_executable(executable: str | Path) -> Path:
    """Resolve an explicit executable path; never search a caller's shell."""
    raw = os.fspath(executable)
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("Executable path contains unsupported characters")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("An absolute executable path is required")
    try:
        path = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("Executable does not exist") from error
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Executable must be an executable regular file")
    return path


def sanitized_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Allow only OS/session variables; omit API keys and provider overrides.

    In particular, HOME and CODEX_HOME are passed through unchanged. Existing
    authentication remains owned by Codex rather than copied into this program.
    """
    values = os.environ if source is None else source
    return {
        name: value for name, value in values.items()
        if name in _ENVIRONMENT_KEYS or re.fullmatch(r"LC_[A-Z_]+", name)
    }


def _stop_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Terminate the whole new session, including descendants holding pipes."""
    cleaned = True
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        cleaned = False
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while a child ignores SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        cleaned = False
        # A restrictive host may deny the group signal while allowing the direct
        # child to be stopped. This cannot establish descendant cleanup.
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        cleaned = False
    return cleaned


def _run_bounded(
    arguments: list[str], cwd: str, timeout: float, output_limit: int,
    environment: Mapping[str, str],
) -> _ProcessResult:
    if os.name != "posix":
        return _ProcessResult(None, b"", b"", "unsupported_platform")
    try:
        process = subprocess.Popen(
            arguments, cwd=cwd, env=dict(environment), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return _ProcessResult(None, b"", b"", "launch_failed")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    failure = None
    deadline = time.monotonic() + timeout
    total = 0
    selector = selectors.DefaultSelector()
    try:
        for name in buffers:
            stream = getattr(process, name)
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fd, min(65536, output_limit - total + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total += len(chunk)
                if total > output_limit:
                    failure = "output_limit"
                    break
                buffers[key.data].extend(chunk)
            if failure is not None:
                break
        if failure is None:
            process.wait()
    except OSError:
        failure = "process_io_failed"
    finally:
        # The nonce proof has no legitimate background work, even if a child
        # closes both inherited pipes and outlives an otherwise successful CLI.
        try:
            if not _stop_process_group(process):
                failure = "cleanup_failed"
        finally:
            selector.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    return _ProcessResult(
        process.returncode, bytes(buffers["stdout"]),
        bytes(buffers["stderr"]), failure,
    )


def _failure_category(text: str) -> str:
    lowered = text.lower()
    if any(word in lowered for word in (
        "usage limit", "rate limit", "rate_limit", "quota", "insufficient credits",
    )):
        return "quota_or_rate_limit"
    if any(word in lowered for word in (
        "not logged in", "unauthorized", "authentication", "invalid token",
        "login required", "sign in", "login with", "api key", "api_key",
    )):
        return "authentication_failed"
    return "execution_failed"


def _inspect_in_directory(settings: ProbeSettings, cwd: str) -> dict[str, object]:
    receipt: dict[str, object] = {
        "schema_version": 1, "check": "codex_subscription_doctor", "ok": False,
        "status": "blocked", "version": None, "authentication": "unknown",
    }
    try:
        executable = validate_executable(settings.executable)
    except (ValueError, TypeError):
        receipt["reason"] = "invalid_executable"
        return receipt
    environment = sanitized_environment()
    version_result = _run_bounded(
        [str(executable), "--version"], cwd, settings.metadata_timeout_seconds,
        settings.max_output_bytes, environment,
    )
    if version_result.failure:
        receipt["reason"] = version_result.failure
        return receipt
    try:
        version = version_result.stdout.decode("utf-8", "strict").strip()
    except UnicodeDecodeError:
        version = ""
    match = _VERSION_PATTERN.fullmatch(version)
    if version_result.returncode != 0 or match is None:
        receipt["reason"] = "unrecognized_version"
        return receipt
    receipt["version"] = match.group(1)
    login_result = _run_bounded(
        [str(executable), "login", "status"], cwd,
        settings.metadata_timeout_seconds, settings.max_output_bytes, environment,
    )
    if login_result.failure:
        receipt["reason"] = login_result.failure
        return receipt
    # Codex versions have written status on either stdout or stderr.
    status = (login_result.stdout + b"\n" + login_result.stderr).decode("utf-8", "replace")
    lines = [line.strip() for line in status.splitlines()
             if line.strip() and line.strip() not in _KNOWN_LOGIN_WARNINGS]
    lowered = status.lower()
    if "api key" in lowered or "api_key" in lowered:
        receipt.update(authentication="api_key", reason="api_key_auth_not_allowed")
    elif any("not logged in" in line.lower() for line in lines):
        receipt.update(authentication="missing", reason="login_required")
    elif login_result.returncode == 0 and lines == ["Logged in using ChatGPT"]:
        receipt.update(authentication="chatgpt", reason="ready", ok=True, status="ready")
    else:
        receipt["reason"] = "unrecognized_login_status"
    return receipt


def inspect_cli(settings: ProbeSettings) -> dict[str, object]:
    """Return sanitized version/login facts without inference or network requests."""
    with tempfile.TemporaryDirectory(prefix="blackoutburst-codex-doctor-") as cwd:
        return _inspect_in_directory(settings, cwd)


def _probe_arguments(settings: ProbeSettings, executable: Path, nonce: str) -> list[str]:
    arguments = [
        str(executable), "exec", "--ignore-user-config", "--skip-git-repo-check",
        "--ephemeral", "--sandbox", "read-only", "--json",
        "-c", 'forced_login_method="chatgpt"', "-c", 'model_provider="openai"',
        "-c", 'web_search="disabled"',
    ]
    for feature in settings.disabled_features:
        arguments.extend(["-c", f"features.{feature}=false"])
    if settings.model is not None:
        arguments.extend(["--model", settings.model])
    arguments.append(
        "This is a personal subscription connectivity check. "
        "Do not use tools, access files, browse, or perform any other action. "
        f"Reply with exactly the following text and nothing else: {nonce}"
    )
    return arguments


def _parse_probe(result: _ProcessResult, nonce: str) -> dict[str, object]:
    import json

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON object key")
            value[key] = item
        return value

    def reject_constant(value: str) -> object:
        raise ValueError("Non-JSON numeric constant")

    verification: dict[str, object] = {
        "exit_zero": result.returncode == 0,
        "turn_completed": False,
        "nonce_verified": False,
    }
    if result.failure is not None:
        return {"ok": False, "reason": result.failure, "verification": verification}
    try:
        text = result.stdout.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return {"ok": False, "reason": "protocol_error", "verification": verification}
    protocol_error = False
    terminal_failure = False
    tool_event = False
    failures: list[str] = []
    messages: list[str] = []
    completed_count = 0
    seen_terminal = False
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line, object_pairs_hook=strict_object, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            protocol_error = True
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            protocol_error = True
            continue
        event_type = event["type"]
        if seen_terminal and event_type not in ("turn.failed", "error"):
            protocol_error = True
        if event_type in ("error", "turn.failed"):
            seen_terminal = True
            terminal_failure = True
            failures.append(_failure_category(line))
        elif event_type == "turn.completed":
            seen_terminal = True
            completed_count += 1
        elif event_type in ("thread.started", "turn.started"):
            pass
        elif event_type in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                protocol_error = True
                continue
            if item["type"] not in ("agent_message", "reasoning"):
                tool_event = True
            if item["type"] == "agent_message" and event_type == "item.completed":
                message = item.get("text")
                if not isinstance(message, str):
                    protocol_error = True
                else:
                    messages.append(message)
        else:
            protocol_error = True
    verification["turn_completed"] = completed_count == 1
    verification["nonce_verified"] = messages == [nonce]
    if tool_event:
        reason = "unexpected_tool_event"
    elif protocol_error or completed_count > 1:
        reason = "protocol_error"
    elif terminal_failure:
        reason = next((reason for reason in failures if reason != "execution_failed"), "execution_failed")
    elif result.returncode != 0:
        reason = _failure_category(result.stderr.decode("utf-8", "replace"))
    elif completed_count != 1:
        reason = "turn_not_completed"
    elif messages != [nonce]:
        reason = "nonce_mismatch"
    else:
        reason = "verified"
    return {"ok": reason == "verified", "reason": reason, "verification": verification}


def run_subscription_probe(settings: ProbeSettings) -> dict[str, object]:
    """Run one fixed, harmless turn after confirming ChatGPT authentication.

    No retry, login, API fallback, provider selection, tool execution, or model
    fallback is performed by this harness. A blocked run cannot claim success.
    """
    receipt: dict[str, object] = {
        "schema_version": 1, "check": "codex_subscription_nonce_probe",
        "proof_scope": "local_personal_codex_cli_only", "ok": False,
        "status": "blocked", "requested_model": settings.model,
    }
    with tempfile.TemporaryDirectory(prefix="blackoutburst-codex-probe-") as cwd:
        doctor = _inspect_in_directory(settings, cwd)
        receipt["doctor"] = doctor
        if not doctor["ok"]:
            receipt["reason"] = doctor["reason"]
            return receipt
        # Re-resolve immediately before launch, without loading credential files.
        try:
            executable = validate_executable(settings.executable)
        except (ValueError, TypeError):
            receipt["reason"] = "invalid_executable"
            return receipt
        nonce = "BLACKOUTBURST_SUBSCRIPTION_PROBE_" + secrets.token_hex(16)
        result = _run_bounded(
            _probe_arguments(settings, executable, nonce), cwd,
            settings.timeout_seconds, settings.max_output_bytes,
            sanitized_environment(),
        )
        receipt.update(_parse_probe(result, nonce))
        receipt["status"] = "passed" if receipt["ok"] else "blocked"
        return receipt
