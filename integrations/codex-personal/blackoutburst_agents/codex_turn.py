"""Bounded, tool-free personal Codex text turns with explicit session resumption.

The caller owns durable session records and must only supply one of its own
recorded session IDs. This module does not discover sessions, authenticate users,
read credentials, retry a turn, or choose a fallback provider/model. The returned
final response is private conversation content, not a public verification receipt.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time
from typing import Callable
import uuid

from .subscription_probe import (
    ProbeSettings, _is_completed_error_item, inspect_cli, sanitized_environment,
    validate_executable,
)

_PROMPT_LIMIT = 65536


def _uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        return None
    return value if str(parsed) == value else None


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate object key")
        result[key] = value
    return result


def _constant(value: str) -> object:
    raise ValueError("Non-JSON constant")


def _error_category(text: str) -> str:
    lowered = text.lower()
    if any(value in lowered for value in (
        "usage limit", "rate limit", "rate_limit", "quota", "insufficient credits",
    )):
        return "quota_or_rate_limit"
    if any(value in lowered for value in (
        "not logged in", "unauthorized", "authentication", "invalid token",
        "login required", "sign in", "login with", "api key", "api_key",
    )):
        return "authentication_failed"
    return "execution_failed"


def _arguments(settings: ProbeSettings, executable: Path, session_id: str | None,
               planner_arguments: list[str] | None = None) -> list[str]:
    # exec owns the sandbox flag; the resume subcommand does not accept it.
    arguments = [str(executable), "exec"]
    arguments.extend(["--sandbox", "read-only"] if planner_arguments is None else planner_arguments)
    if session_id is not None:
        arguments.append("resume")
    arguments.extend([
        "--ignore-user-config", "--skip-git-repo-check", "--json",
        "-c", 'forced_login_method="chatgpt"',
        "-c", 'model_provider="openai"',
        "-c", 'approval_policy="never"',
        "-c", 'web_search="disabled"',
    ])
    for feature in settings.disabled_features:
        arguments.extend(["-c", f"features.{feature}=false"])
    if settings.model is not None:
        arguments.extend(["--model", settings.model])
    if session_id is not None:
        arguments.append(session_id)
    arguments.append("-")  # Prompt bytes are written to stdin, never argv.
    return arguments


def _stop_group(process: subprocess.Popen[bytes]) -> bool:
    """Only signal the new process group created by this invocation."""
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
    except OSError:
        cleaned = False
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        cleaned = False
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        cleaned = False
    except OSError:
        cleaned = False
    return cleaned


def execute_turn(
    settings: ProbeSettings,
    prompt: str,
    workspace: Path,
    *,
    resume_session_id: str | None = None,
    on_started: Callable[[int], None] | None = None,
    on_session: Callable[[str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    planner_policy=None,
    before_prompt: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Run one text turn, returning only its final message and safe checks.

    ``on_started`` runs before any prompt bytes are sent. ``on_session`` runs
    synchronously on the first valid thread.started, before subsequent events are
    processed. Callers can therefore persist identities before accepting results.
    Callback failures stop the owned process group and block the turn.
    ``before_prompt`` is an opt-in forced authority check at the first stdin
    write, after metadata/policy checks. Polling callbacks cannot replace it.
    """
    verification: dict[str, object] = {
        "exit_zero": False, "turn_completed": False, "session_verified": False,
        "tool_free": True, "cleanup_confirmed": True,
        "authentication": "unknown", "cli_version": None,
    }
    result: dict[str, object] = {
        "status": "blocked", "reason": "invalid_input", "session_id": None,
        "text": None, "verification": verification,
    }
    if os.name != "posix":
        result["reason"] = "unsupported_platform"
        return result
    try:
        if not isinstance(prompt, str):
            return result
        if before_prompt is not None and not callable(before_prompt):
            return result
        prompt_bytes = prompt.encode("utf-8", "strict")
        if not prompt_bytes or len(prompt_bytes) > _PROMPT_LIMIT:
            return result
        directory = Path(workspace)
        if not directory.is_absolute() or not directory.is_dir():
            result["reason"] = "invalid_workspace"
            return result
        directory = directory.resolve(strict=True)
        if resume_session_id is not None and _uuid(resume_session_id) is None:
            result["reason"] = "invalid_session_id"
            return result
        executable = validate_executable(settings.executable)
    except (OSError, TypeError, ValueError, RuntimeError):
        return result

    def cancellation() -> str | None:
        if should_cancel is None:
            return None
        try:
            return "cancelled" if should_cancel() else None
        except Exception:
            return "callback_failed"

    planner_arguments = None
    if planner_policy is not None:
        from .planner_policy import PlannerPolicy
        from .linux_planner_policy import LinuxPlannerPolicy
        try:
            if type(planner_policy) not in (PlannerPolicy, LinuxPlannerPolicy):
                raise ValueError
            planner_arguments = planner_policy.validate_for(executable, directory)
        except Exception:
            result['reason'] = 'planner_policy_unqualified_or_changed'
            return result
    failure = cancellation()
    if failure:
        result.update(reason=failure, status="cancelled" if failure == "cancelled" else "blocked")
        return result
    doctor = inspect_cli(settings)
    verification.update(authentication=doctor.get("authentication"), cli_version=doctor.get("version"))
    if not doctor.get("ok"):
        result["reason"] = doctor.get("reason", "doctor_failed")
        if result["reason"] == "cleanup_failed":
            verification["cleanup_confirmed"] = False
        return result
    failure = cancellation()
    if failure:
        result.update(reason=failure, status="cancelled" if failure == "cancelled" else "blocked")
        return result
    if planner_policy is not None:
        try:
            # Metadata inspection can take time; recheck before the first
            # model launch, never fall back to the legacy sandbox.
            planner_arguments = planner_policy.validate_for(executable, directory)
        except Exception:
            result['reason'] = 'planner_policy_unqualified_or_changed'
            return result
    try:
        process = subprocess.Popen(
            _arguments(settings, executable, resume_session_id, planner_arguments), cwd=str(directory),
            env=sanitized_environment(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
            start_new_session=True,
        )
    except (OSError, ValueError):
        result["reason"] = "launch_failed"
        return result

    # Persist the freshly created PID before setting up I/O or sending bytes.
    if on_started is not None:
        try:
            on_started(process.pid)
        except Exception:
            cleaned = _stop_group(process)
            verification["cleanup_confirmed"] = cleaned
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            result["reason"] = "callback_failed" if cleaned else "cleanup_failed"
            return result
    try:
        selector = selectors.DefaultSelector()
    except OSError:
        cleaned = _stop_group(process)
        verification["cleanup_confirmed"] = cleaned
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        result["reason"] = "process_io_failed" if cleaned else "cleanup_failed"
        return result
    stdout_pending = bytearray()
    stderr = bytearray()
    total = 0
    written = 0
    session_id: str | None = None
    turn_started = False
    turn_completed = False
    final_message: str | None = None
    deadline = time.monotonic() + settings.timeout_seconds

    def event_line(line: bytes) -> str | None:
        nonlocal session_id, turn_started, turn_completed, final_message
        if not line.strip():
            return None
        try:
            event = json.loads(line.decode("utf-8", "strict"), object_pairs_hook=_object, parse_constant=_constant)
        except (UnicodeError, ValueError, RecursionError):
            return "protocol_error"
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return "protocol_error"
        kind = event["type"]
        # A late failure invalidates even an otherwise complete result.
        if kind in ("error", "turn.failed"):
            return _error_category(line.decode("utf-8", "replace"))
        if turn_completed:
            return "protocol_error"
        if (kind in ("item.started", "item.updated", "item.completed")
                and isinstance(event.get("item"), dict)
                and event["item"].get("type") == "error"):
            # A diagnostic item may precede the session. It cannot establish
            # one or bypass a terminal failure, valid reply, exit or cleanup.
            return None if _is_completed_error_item(event) else "protocol_error"
        if kind == "thread.started":
            found = _uuid(event.get("thread_id"))
            if found is None or session_id is not None:
                return "protocol_error"
            if resume_session_id is not None and found != resume_session_id:
                return "session_mismatch"
            session_id = found
            result["session_id"] = found
            verification["session_verified"] = True
            if on_session is not None:
                try:
                    on_session(found)
                except Exception:
                    return "callback_failed"
            return None
        if session_id is None:
            return "protocol_error"
        if kind == "turn.started":
            if turn_started:
                return "protocol_error"
            turn_started = True
            return None
        if not turn_started:
            return "protocol_error"
        if kind == "turn.completed":
            turn_completed = True
            verification["turn_completed"] = True
            return None
        if kind in ("item.started", "item.updated", "item.completed"):
            item = event.get("item")
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                return "protocol_error"
            if item["type"] not in ("agent_message", "reasoning"):
                verification["tool_free"] = False
                return "unexpected_tool_event"
            if item["type"] == "agent_message" and kind == "item.completed":
                message = item.get("text")
                if not isinstance(message, str):
                    return "protocol_error"
                try:
                    if len(message.encode("utf-8", "strict")) > settings.max_output_bytes:
                        return "output_limit"
                except UnicodeError:
                    return "protocol_error"
                final_message = message
            return None
        return "protocol_error"

    try:
        if failure is None:
            for name in ("stdout", "stderr", "stdin"):
                stream = getattr(process, name)
                assert stream is not None
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_WRITE if name == "stdin" else selectors.EVENT_READ, name)
        while failure is None and (selector.get_map() or process.poll() is None):
            failure = cancellation()
            if failure:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            for key, _ in selector.select(min(remaining, 0.05)):
                if key.data == "stdin":
                    if written == 0 and before_prompt is not None:
                        try:
                            if before_prompt() is not None:
                                raise ValueError('invalid_prompt_guard_result')
                        except Exception:
                            failure = "callback_failed"
                            break
                        failure = cancellation()
                        if failure:
                            break
                        if time.monotonic() >= deadline:
                            failure = "timeout"
                            break
                    try:
                        written += os.write(key.fd, prompt_bytes[written:written + 16384])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        failure = "prompt_write_failed"
                        break
                    if written == len(prompt_bytes):
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                try:
                    chunk = os.read(key.fd, min(16384, settings.max_output_bytes - total + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    if key.data == "stdout" and stdout_pending:
                        failure = event_line(bytes(stdout_pending))
                        stdout_pending.clear()
                    if failure:
                        break
                    continue
                total += len(chunk)
                if total > settings.max_output_bytes:
                    failure = "output_limit"
                    break
                if key.data == "stderr":
                    stderr.extend(chunk)
                    continue
                stdout_pending.extend(chunk)
                while b"\n" in stdout_pending:
                    line, _, tail = stdout_pending.partition(b"\n")
                    stdout_pending = bytearray(tail)
                    failure = event_line(bytes(line))
                    if failure:
                        break
                if failure:
                    break
        if failure is None:
            process.wait()
    except OSError:
        failure = "process_io_failed"
    finally:
        cleaned = _stop_group(process)
        verification["cleanup_confirmed"] = cleaned
        if not cleaned:
            failure = "cleanup_failed"
        selector.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    verification["exit_zero"] = process.returncode == 0
    if failure is None:
        if process.returncode != 0:
            failure = _error_category(stderr.decode("utf-8", "replace"))
        elif not turn_completed:
            failure = "turn_not_completed"
        elif final_message is None:
            failure = "missing_response"
        elif written != len(prompt_bytes):
            failure = "prompt_write_failed"
    if failure is not None:
        result.update(status="cancelled" if failure == "cancelled" else "blocked", reason=failure)
    else:
        result.update(status="completed", reason="verified", text=final_message)
    return result
