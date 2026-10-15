"""Local fake Codex processes only: no model, authentication, or network calls."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

from blackoutburst_agents.codex_turn import execute_turn
from blackoutburst_agents.subscription_probe import DEFAULT_DISABLED_FEATURES, ProbeSettings


SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_SESSION = "11111111-2222-4333-8444-666666666666"


@unittest.skipUnless(os.name == "posix", "The transport supports POSIX process groups")
class CodexTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-text-turn-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.workspace = self.directory / "workspace"
        self.workspace.mkdir()

    def fake_cli(self, body: str = "", *, login: str = "Logged in using ChatGPT", login_code: int = 0) -> Path:
        executable = self.directory / "fake-codex"
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, signal, subprocess, sys, time\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            "    print('codex-cli 0.162.0-alpha.17.2')\n"
            "    raise SystemExit(0)\n"
            "if args == ['login', 'status']:\n"
            f"    print({login!r}, file=sys.stderr)\n"
            f"    raise SystemExit({login_code})\n"
            "prompt = sys.stdin.read()\n"
            "def event(value):\n"
            "    print(json.dumps(value), flush=True)\n"
            "def started():\n"
            f"    event({{'type': 'thread.started', 'thread_id': {SESSION!r}}})\n"
            "    event({'type': 'turn.started'})\n"
            "def message(text):\n"
            "    event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})\n"
            "def success():\n"
            "    started()\n"
            "    message('Final private reply')\n"
            "    event({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}})\n"
            + textwrap.dedent(body or "success()\n"), encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable

    def run_turn(self, body: str = "", *, prompt: str = "Private prompt", settings=None, **kwargs):
        if settings is None:
            settings = ProbeSettings(self.fake_cli(body), timeout_seconds=2, metadata_timeout_seconds=2)
        return execute_turn(settings, prompt, self.workspace, **kwargs)

    def test_completed_turn_contains_only_final_response_and_safe_checks(self):
        result = self.run_turn("""
            started()
            event({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private scratch'}})
            message('Earlier commentary')
            message('Final private reply')
            event({'type': 'turn.completed'})
        """)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual(result["text"], "Final private reply")
        self.assertTrue(result["verification"]["turn_completed"])
        self.assertTrue(result["verification"]["cleanup_confirmed"])
        serialized = json.dumps(result)
        self.assertNotIn("Private prompt", serialized)
        self.assertNotIn("private scratch", serialized)
        self.assertNotIn("Earlier commentary", serialized)

    def test_prompt_is_stdin_and_argument_vector_forces_safe_personal_mode(self):
        capture = self.directory / "capture.json"
        prompt = "Secrets stay out of argv: ' \" $(anything) `any`\nBonjour 🌍"
        result = self.run_turn(f"""
            with open({str(capture)!r}, 'w') as output:
                json.dump({{'args': args, 'prompt': prompt, 'cwd': os.getcwd(), 'env': dict(os.environ)}}, output)
            success()
        """, prompt=prompt)
        self.assertEqual(result["status"], "completed")
        capture_data = json.loads(capture.read_text())
        args = capture_data["args"]
        self.assertEqual(args[:3], ["exec", "--sandbox", "read-only"])
        self.assertEqual(args[-1], "-")
        self.assertNotIn(prompt, args)
        self.assertEqual(capture_data["prompt"], prompt)
        self.assertEqual(capture_data["cwd"], str(self.workspace.resolve()))
        self.assertIn('--ignore-user-config', args)
        self.assertNotIn('--ephemeral', args)
        for config in ('forced_login_method="chatgpt"', 'model_provider="openai"', 'approval_policy="never"', 'web_search="disabled"'):
            self.assertIn(config, args)
        for feature in DEFAULT_DISABLED_FEATURES:
            self.assertIn(f"features.{feature}=false", args)

    def test_exact_uuid_resume_never_uses_latest_or_all_sessions(self):
        capture = self.directory / "args.json"
        settings = ProbeSettings(self.fake_cli(f"""
            with open({str(capture)!r}, 'w') as output:
                json.dump(args, output)
            success()
        """), model="selected-model")
        result = self.run_turn(settings=settings, resume_session_id=SESSION)
        self.assertEqual(result["status"], "completed")
        args = json.loads(capture.read_text())
        self.assertEqual(args[:4], ["exec", "--sandbox", "read-only", "resume"])
        self.assertEqual(args[-2:], [SESSION, "-"])
        self.assertEqual(args.count(SESSION), 1)
        self.assertNotIn('--last', args)
        self.assertNotIn('--all', args)
        self.assertEqual(args[args.index('--model') + 1], "selected-model")

    def test_resume_mismatch_is_blocked_before_session_callback(self):
        called = []
        result = self.run_turn(resume_session_id=OTHER_SESSION, on_session=called.append)
        self.assertEqual(result["reason"], "session_mismatch")
        self.assertIsNone(result["session_id"])
        self.assertIsNone(result["text"])
        self.assertEqual(called, [])

    def test_callbacks_are_early_and_in_order(self):
        marker = self.directory / "session-recorded"
        calls = []

        def on_started(pid):
            self.assertIsInstance(pid, int)
            calls.append("started")

        def on_session(session_id):
            calls.append(session_id)
            marker.write_text("recorded")

        result = self.run_turn(f"""
            event({{'type':'thread.started', 'thread_id':{SESSION!r}}})
            deadline = time.monotonic() + 1
            while not os.path.exists({str(marker)!r}) and time.monotonic() < deadline:
                time.sleep(0.01)
            assert os.path.exists({str(marker)!r})
            event({{'type':'turn.started'}})
            message('confirmed after callback')
            event({{'type':'turn.completed'}})
        """, on_started=on_started, on_session=on_session)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, ["started", SESSION])

    def test_callbacks_fail_closed_and_hide_exception_messages(self):
        def fail(*args):
            raise RuntimeError("secret callback details")

        for callback in ("on_started", "on_session", "should_cancel"):
            with self.subTest(callback=callback):
                result = self.run_turn(**{callback: fail})
                self.assertEqual(result["reason"], "callback_failed")
                self.assertEqual(result["status"], "blocked")
                self.assertNotIn("secret callback details", json.dumps(result))
                self.assertIsNone(result["text"])

    def test_cancellation_before_launch_does_not_invoke_codex(self):
        settings = ProbeSettings(self.fake_cli())
        with patch("blackoutburst_agents.codex_turn.inspect_cli") as doctor:
            result = self.run_turn(settings=settings, should_cancel=lambda: True)
        doctor.assert_not_called()
        self.assertEqual(result["status"], "cancelled")

    def test_doctor_cleanup_failure_is_not_reported_as_confirmed_cleanup(self):
        settings = ProbeSettings(self.fake_cli())
        with patch("blackoutburst_agents.codex_turn.inspect_cli", return_value={
            "ok": False, "reason": "cleanup_failed", "authentication": "unknown",
        }), patch("blackoutburst_agents.codex_turn.subprocess.Popen") as launch:
            result = self.run_turn(settings=settings)
        launch.assert_not_called()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "cleanup_failed")
        self.assertFalse(result["verification"]["cleanup_confirmed"])
        self.assertIsNone(result["text"])

    def test_cancellation_stops_own_process_and_retains_recorded_session(self):
        cancelled = []
        pids = []
        started = time.monotonic()
        result = self.run_turn("started()\ntime.sleep(30)\n", on_started=pids.append,
                               on_session=lambda value: cancelled.append(True),
                               should_cancel=lambda: bool(cancelled))
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["session_id"], SESSION)
        self.assertTrue(result["verification"]["cleanup_confirmed"])
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_timeout_stops_process_without_usable_final_text(self):
        settings = ProbeSettings(self.fake_cli("started()\ntime.sleep(30)\n"), timeout_seconds=0.1)
        started = time.monotonic()
        result = self.run_turn(settings=settings)
        self.assertEqual(result["reason"], "timeout")
        self.assertEqual(result["session_id"], SESSION)
        self.assertIsNone(result["text"])
        self.assertLess(time.monotonic() - started, 3)

    def test_cleanup_failure_never_claims_completion_or_cancellation(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                original = __import__("blackoutburst_agents.codex_turn", fromlist=["_stop_group"])._stop_group

                def uncertain(process):
                    original(process)
                    return False

                seen = []
                with patch("blackoutburst_agents.codex_turn._stop_group", side_effect=uncertain):
                    result = self.run_turn(on_session=seen.append, should_cancel=lambda: cancel and bool(seen))
                self.assertEqual(result["reason"], "cleanup_failed")
                self.assertEqual(result["status"], "blocked")
                self.assertIsNone(result["text"])

    def test_selector_setup_failure_still_records_pid_and_stops_process(self):
        pids = []
        settings = ProbeSettings(self.fake_cli("time.sleep(30)\n"))
        with patch("blackoutburst_agents.codex_turn.inspect_cli", return_value={
            "ok": True, "authentication": "chatgpt", "version": "test",
        }), patch("blackoutburst_agents.codex_turn.selectors.DefaultSelector", side_effect=OSError("private host detail")):
            result = self.run_turn(settings=settings, on_started=pids.append)
        self.assertEqual(result["reason"], "process_io_failed")
        self.assertEqual(len(pids), 1)
        self.assertTrue(result["verification"]["cleanup_confirmed"])
        self.assertNotIn("private host detail", json.dumps(result))
        with self.assertRaises(ProcessLookupError):
            os.kill(pids[0], 0)

    def test_api_and_missing_login_do_not_launch_inference(self):
        for login, code, reason in (
            ("Logged in using an API key - SECRET", 0, "api_key_auth_not_allowed"),
            ("Not logged in", 1, "login_required"),
        ):
            with self.subTest(login=login):
                marker = self.directory / "inference"
                settings = ProbeSettings(self.fake_cli(f"open({str(marker)!r}, 'w').close()", login=login, login_code=code))
                result = self.run_turn(settings=settings)
                self.assertEqual(result["reason"], reason)
                self.assertFalse(marker.exists())
                self.assertNotIn("SECRET", json.dumps(result))

    def test_environment_strips_api_keys_and_preserves_owned_home(self):
        capture = self.directory / "environment.json"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret-key", "OPENAI_BASE_URL": "https://untrusted", "ANTHROPIC_API_KEY": "secret-other"}):
            result = self.run_turn(f"with open({str(capture)!r}, 'w') as output:\n    json.dump(dict(os.environ), output)\nsuccess()\n")
        self.assertEqual(result["status"], "completed")
        environment = json.loads(capture.read_text())
        self.assertEqual(environment.get("HOME"), os.environ.get("HOME"))
        self.assertEqual(environment.get("CODEX_HOME"), os.environ.get("CODEX_HOME"))
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("OPENAI_BASE_URL", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)

    def test_quota_and_authentication_errors_have_safe_categories(self):
        for text, reason in (("usage limit reached SECRET", "quota_or_rate_limit"), ("authentication invalid token SECRET", "authentication_failed")):
            for body in (
                f"event({{'type':'error','message':{text!r}}})\n",
                f"success()\nevent({{'type':'turn.failed','error':{{'message':{text!r}}}}})\n",
                f"success()\nprint({text!r}, file=sys.stderr)\nraise SystemExit(1)\n",
            ):
                with self.subTest(body=body):
                    result = self.run_turn(body)
                    self.assertEqual(result["reason"], reason)
                    self.assertEqual(result["status"], "blocked")
                    self.assertIsNone(result["text"])
                    self.assertNotIn("SECRET", json.dumps(result))

    def test_unknown_duplicate_nan_or_invalid_utf8_protocol_is_blocked(self):
        for data in (b"bad PRIVATE\n", b"[]\n", b'{"type":"unknown"}\n',
                     b'{"type":"turn.started","x":NaN}\n',
                     b'{"type":"turn.failed","type":"turn.started"}\n', b"\xff\n"):
            with self.subTest(data=data):
                result = self.run_turn(f"os.write(1, {data!r})\nsuccess()\n")
                self.assertEqual(result["reason"], "protocol_error")
                self.assertIsNone(result["text"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_session_ids_are_strict_and_only_one_session_may_be_observed(self):
        for body in (
            "event({'type':'thread.started','thread_id':'not-a-uuid'})\n",
            "started()\nstarted()\n",
            "event({'type':'turn.started'})\n",
        ):
            with self.subTest(body=body):
                result = self.run_turn(body)
                self.assertEqual(result["reason"], "protocol_error")

    def test_terminal_event_is_required_and_cannot_repeat_or_be_followed(self):
        cases = (
            ("started()\nmessage('partial')\n", "turn_not_completed"),
            ("success()\nevent({'type':'turn.completed'})\n", "protocol_error"),
            ("success()\nmessage('late')\n", "protocol_error"),
            ("started()\nevent({'type':'turn.completed'})\n", "missing_response"),
            ("started()\nevent({'type':'item.completed','item':{'type':'agent_message'}})\n", "protocol_error"),
        )
        for body, reason in cases:
            with self.subTest(body=body):
                result = self.run_turn(body)
                self.assertEqual(result["reason"], reason)
                self.assertIsNone(result["text"])

    def test_tools_are_rejected_immediately_without_retaining_their_content(self):
        for kind in ("command_execution", "file_change", "mcp_tool_call", "web_search", "unknown"):
            with self.subTest(kind=kind):
                result = self.run_turn(f"started()\nevent({{'type':'item.started','item':{{'type':{kind!r},'text':'TOOL SECRET'}}}})\ntime.sleep(30)\n")
                self.assertEqual(result["reason"], "unexpected_tool_event")
                self.assertFalse(result["verification"]["tool_free"])
                self.assertNotIn("TOOL SECRET", json.dumps(result))

    def test_total_output_cap_covers_both_streams(self):
        for stream in (1, 2):
            with self.subTest(stream=stream):
                settings = ProbeSettings(self.fake_cli(f"os.write({stream}, b'x' * 4096)\nsuccess()\n"), max_output_bytes=1024)
                result = self.run_turn(settings=settings)
                self.assertEqual(result["reason"], "output_limit")

    def test_prompt_can_fill_pipe_without_deadlock_and_is_bounded_in_utf8_bytes(self):
        prompt = "x" * 65536
        result = self.run_turn(prompt=prompt)
        self.assertEqual(result["status"], "completed")
        for bad in ("", "x" * 65537, "é" * 32769, "\ud800", None):
            with self.subTest(length=len(bad) if bad is not None else None):
                result = self.run_turn(prompt=bad)
                self.assertEqual(result["reason"], "invalid_input")

    def test_invalid_workspace_and_resume_are_rejected_before_doctor(self):
        settings = ProbeSettings(self.fake_cli())
        for workspace in (Path("relative"), self.directory / "missing", settings.executable):
            with patch("blackoutburst_agents.codex_turn.inspect_cli") as doctor:
                result = execute_turn(settings, "prompt", workspace)
            self.assertEqual(result["reason"], "invalid_workspace")
            doctor.assert_not_called()
        for session in ("", "--last", "../session", "{11111111-2222-4333-8444-555555555555}", "11111111222243338444555555555555"):
            with patch("blackoutburst_agents.codex_turn.inspect_cli") as doctor:
                result = execute_turn(settings, "prompt", self.workspace, resume_session_id=session)
            self.assertEqual(result["reason"], "invalid_session_id")
            doctor.assert_not_called()

    def test_successful_json_without_last_newline_is_accepted(self):
        result = self.run_turn("started()\nmessage('done')\nsys.stdout.write(json.dumps({'type':'turn.completed'}))\n")
        self.assertEqual(result["status"], "completed")

    def test_nonzero_exit_without_diagnostic_is_blocked(self):
        result = self.run_turn("success()\nraise SystemExit(2)\n")
        self.assertEqual(result["reason"], "execution_failed")
        self.assertFalse(result["verification"]["exit_zero"])
        self.assertIsNone(result["text"])


if __name__ == "__main__":
    unittest.main()
