"""Local fake executable tests. These never invoke Codex or make inference."""

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

from blackoutburst_agents.subscription_probe import (
    DEFAULT_DISABLED_FEATURES,
    ProbeSettings,
    inspect_cli,
    run_subscription_probe,
    sanitized_environment,
    validate_executable,
)


class SubscriptionProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="subscription-proof-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def fake_cli(
        self, body: str = "", *, login: str = "Logged in using ChatGPT",
        login_code: int = 0, version: str = "codex-cli 0.162.0-alpha.17.2",
        name: str = "fake-codex", version_body: str | None = None,
    ) -> Path:
        executable = self.directory / name
        executable.write_text(
            "#!" + sys.executable + "\n"
            "import json, os, re, signal, subprocess, sys, time\n"
            "args = sys.argv[1:]\n"
            "if args == ['--version']:\n"
            + (textwrap.indent(version_body, "    ") if version_body else f"    print({version!r})\n")
            + "    raise SystemExit(0)\n"
            "if args == ['login', 'status']:\n"
            f"    print({login!r}, file=sys.stderr)\n"
            f"    raise SystemExit({login_code})\n"
            "assert args[:2] == ['exec', '--ignore-user-config']\n"
            "nonce = re.search(r'BLACKOUTBURST_SUBSCRIPTION_PROBE_[0-9a-f]{32}', args[-1]).group()\n"
            "def event(value):\n"
            "    print(json.dumps(value), flush=True)\n"
            "def success():\n"
            "    event({'type': 'thread.started', 'thread_id': 'fake-thread'})\n"
            "    event({'type': 'turn.started'})\n"
            "    event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': nonce}})\n"
            "    event({'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}})\n"
            + textwrap.dedent(body or "success()\n"),
            encoding="utf-8",
        )
        executable.chmod(0o700)
        return executable

    def settings(self, executable: Path, **kwargs: object) -> ProbeSettings:
        return ProbeSettings(executable, metadata_timeout_seconds=2, timeout_seconds=2, **kwargs)

    def test_doctor_confirms_chatgpt_without_inference(self) -> None:
        executable = self.fake_cli("raise AssertionError('doctor must not execute a turn')\n")
        result = inspect_cli(self.settings(executable))
        self.assertTrue(result["ok"])
        self.assertEqual(result["authentication"], "chatgpt")
        self.assertEqual(result["version"], "0.162.0-alpha.17.2")

    def test_success_receipt_is_bounded_and_does_not_include_transcript(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli()))
        self.assertTrue(result["ok"])
        self.assertEqual(result["reason"], "verified")
        self.assertEqual(result["proof_scope"], "local_personal_codex_cli_only")
        self.assertEqual(result["verification"], {
            "exit_zero": True, "turn_completed": True, "nonce_verified": True,
        })
        serialized = json.dumps(result)
        self.assertNotIn("BLACKOUTBURST_SUBSCRIPTION_PROBE_", serialized)
        self.assertNotIn("fake-thread", serialized)
        self.assertNotIn("input_tokens", serialized)

    def test_nonzero_exit_never_passes(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("success()\nraise SystemExit(1)\n")))
        self.assertFalse(result["ok"])
        self.assertFalse(result["verification"]["exit_zero"])

    def test_turn_failed_after_completed_never_passes(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("""
            success()
            event({'type': 'turn.failed', 'error': {'message': 'usage limit reached SECRET'}})
        """)))
        self.assertEqual(result["reason"], "quota_or_rate_limit")
        self.assertFalse(result["ok"])
        self.assertNotIn("SECRET", json.dumps(result))

    def test_error_event_after_completed_never_passes(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("""
            success()
            event({'type': 'error', 'message': 'authentication failed secret-token'})
        """)))
        self.assertEqual(result["reason"], "authentication_failed")
        self.assertNotIn("secret-token", json.dumps(result))

    def test_no_terminal_event_never_passes(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("""
            event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': nonce}})
        """)))
        self.assertEqual(result["reason"], "turn_not_completed")

    def test_wrong_or_duplicate_nonce_never_passes(self) -> None:
        for message in ("'wrong'", "nonce + '\\nextra'"):
            with self.subTest(message=message):
                body = f"event({{'type':'item.completed','item':{{'type':'agent_message','text':{message}}}}})\nevent({{'type':'turn.completed'}})\n"
                result = run_subscription_probe(self.settings(self.fake_cli(body)))
                self.assertEqual(result["reason"], "nonce_mismatch")

    def test_multiple_turns_never_pass(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("success()\nsuccess()\n")))
        self.assertEqual(result["reason"], "protocol_error")

    def test_malformed_events_poison_otherwise_successful_receipt(self) -> None:
        for bad_line in (
            "garbage secret", "[]", "{}", '{"type":"new.event"}',
            '{"type":"turn.started","x":NaN}',
            '{"type":"turn.failed","type":"turn.started"}',
        ):
            with self.subTest(bad_line=bad_line):
                body = f"print({bad_line!r})\nsuccess()\n"
                result = run_subscription_probe(self.settings(self.fake_cli(body)))
                self.assertEqual(result["reason"], "protocol_error")
                self.assertFalse(result["ok"])
                self.assertNotIn("garbage secret", json.dumps(result))

    def test_message_after_terminal_event_is_protocol_error(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("""
            event({'type': 'turn.completed'})
            event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': nonce}})
        """)))
        self.assertEqual(result["reason"], "protocol_error")

    def test_completed_item_without_message_text_is_protocol_error(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("""
            event({'type': 'item.completed', 'item': {'type': 'agent_message'}})
            event({'type': 'turn.completed'})
        """)))
        self.assertEqual(result["reason"], "protocol_error")

    def test_invalid_utf8_is_protocol_error(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("os.write(1, b'\\xff')\nsuccess()\n")))
        self.assertEqual(result["reason"], "protocol_error")

    def test_unexpected_tool_event_fails_closed(self) -> None:
        for kind in ("command_execution", "mcp_tool_call", "web_search", "file_change", "unknown"):
            with self.subTest(kind=kind):
                body = f"event({{'type':'item.started','item':{{'type':{kind!r}}}}})\nsuccess()\n"
                result = run_subscription_probe(self.settings(self.fake_cli(body)))
                self.assertEqual(result["reason"], "unexpected_tool_event")

    def test_reasoning_event_is_allowed_but_not_retained(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli("""
            event({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'private scratch'}})
            success()
        """)))
        self.assertTrue(result["ok"])
        self.assertNotIn("private scratch", json.dumps(result))

    def test_api_key_login_rejected_before_exec_without_echoing_key(self) -> None:
        executable = self.fake_cli(
            "raise AssertionError('must not execute')\n",
            login="Logged in using an API key - secret-key-fragment",
        )
        result = run_subscription_probe(self.settings(executable))
        self.assertEqual(result["reason"], "api_key_auth_not_allowed")
        self.assertNotIn("secret-key-fragment", json.dumps(result))

    def test_missing_login_rejected_before_exec(self) -> None:
        result = run_subscription_probe(self.settings(self.fake_cli(
            "raise AssertionError('must not execute')\n", login="Not logged in", login_code=1,
        )))
        self.assertEqual(result["reason"], "login_required")

    def test_ambiguous_login_rejected(self) -> None:
        for status in ("Logged in using ChatGPT\nunexpected", "ChatGPT", "Logged in using ChatGPT\nAPI key"):
            with self.subTest(status=status):
                result = inspect_cli(self.settings(self.fake_cli(login=status)))
                self.assertFalse(result["ok"])

    def test_exact_known_cli_path_warning_is_ignored(self) -> None:
        status = ("WARNING: proceeding, even though we could not create PATH aliases: "
                  "Operation not permitted (os error 1)\nLogged in using ChatGPT")
        result = inspect_cli(self.settings(self.fake_cli(login=status)))
        self.assertTrue(result["ok"])
        self.assertNotIn("WARNING", json.dumps(result))

    def test_output_cap_includes_stderr(self) -> None:
        for stream in (1, 2):
            with self.subTest(stream=stream):
                body = f"os.write({stream}, b'x' * 4096)\nsuccess()\n"
                result = run_subscription_probe(self.settings(self.fake_cli(body), max_output_bytes=1024))
                self.assertEqual(result["reason"], "output_limit")

    def test_simple_timeout_is_bounded(self) -> None:
        executable = self.fake_cli("time.sleep(30)\n")
        settings = ProbeSettings(executable, timeout_seconds=0.15, metadata_timeout_seconds=2)
        started = time.monotonic()
        result = run_subscription_probe(settings)
        self.assertEqual(result["reason"], "timeout")
        self.assertLess(time.monotonic() - started, 3)

    @unittest.skipUnless(os.name == "posix", "Process-group behavior is POSIX-specific")
    def test_timeout_kills_descendant_even_when_leader_exits(self) -> None:
        child_pid = self.directory / "child.pid"
        script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)"
        executable = self.fake_cli(f"""
            child = subprocess.Popen([sys.executable, '-c', {script!r}])
            with open({str(child_pid)!r}, 'w') as handle:
                handle.write(str(child.pid))
            time.sleep(0.1)
        """)
        settings = ProbeSettings(executable, timeout_seconds=0.5, metadata_timeout_seconds=2)
        result = run_subscription_probe(settings)
        self.assertEqual(result["reason"], "timeout")
        pid = int(child_pid.read_text())
        # A killed child can briefly remain a zombie awaiting system reaping.
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("descendant remained alive after timeout")

    def test_metadata_timeout_is_sanitized(self) -> None:
        executable = self.fake_cli(version_body="time.sleep(30)\n")
        result = inspect_cli(ProbeSettings(executable, metadata_timeout_seconds=0.15))
        self.assertEqual(result["reason"], "timeout")
        self.assertFalse(result["ok"])

    def test_cleanup_permission_denial_is_blocked_not_an_exception(self) -> None:
        executable = self.fake_cli()
        with patch("blackoutburst_agents.subscription_probe.os.killpg",
                   side_effect=PermissionError("PRIVATE_HOST_DETAIL")):
            result = run_subscription_probe(self.settings(executable))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cleanup_failed")
        self.assertNotIn("PRIVATE_HOST_DETAIL", json.dumps(result))

    def test_safe_argument_vector_and_isolated_directory(self) -> None:
        capture = self.directory / "capture.json"
        executable = self.fake_cli(f"""
            with open({str(capture)!r}, 'w') as handle:
                json.dump({{'args': args, 'cwd': os.getcwd(), 'files': os.listdir('.'), 'env': dict(os.environ)}}, handle)
            success()
        """, name="fake codex;$unexpected")
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "DO_NOT_INHERIT",
            "OPENAI_BASE_URL": "https://wrong.example",
            "ANTHROPIC_API_KEY": "DO_NOT_INHERIT_EITHER",
            "AZURE_OPENAI_ENDPOINT": "https://wrong.example",
            "CODEX_MODEL_PROVIDER": "wrong",
        }):
            result = run_subscription_probe(self.settings(executable, model="model-safe-v1"))
        self.assertTrue(result["ok"])
        captured = json.loads(capture.read_text())
        args = captured["args"]
        self.assertEqual(args[:2], ["exec", "--ignore-user-config"])
        self.assertIn("--ephemeral", args)
        self.assertIn("read-only", args)
        self.assertIn('forced_login_method="chatgpt"', args)
        self.assertIn('model_provider="openai"', args)
        self.assertIn('web_search="disabled"', args)
        for feature in DEFAULT_DISABLED_FEATURES:
            self.assertIn(f"features.{feature}=false", args)
        self.assertEqual(args[args.index("--model") + 1], "model-safe-v1")
        self.assertNotIn("--ignore-rules", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertEqual(captured["files"], [])
        self.assertFalse(Path(captured["cwd"]).exists())
        self.assertNotIn("OPENAI_API_KEY", captured["env"])
        self.assertNotIn("OPENAI_BASE_URL", captured["env"])
        self.assertNotIn("ANTHROPIC_API_KEY", captured["env"])
        self.assertNotIn("AZURE_OPENAI_ENDPOINT", captured["env"])
        self.assertNotIn("CODEX_MODEL_PROVIDER", captured["env"])

    def test_environment_preserves_home_and_codex_home_verbatim(self) -> None:
        result = sanitized_environment({
            "HOME": "/unchanged/home", "CODEX_HOME": "/unchanged/codex",
            "PATH": "/usr/bin:/bin", "LC_ALL": "C", "SOME_TOKEN": "secret",
            "OPENAI_API_KEY": "secret", "CODEX_CONFIG_PATH": "/malicious",
            "AWS_SECRET_ACCESS_KEY": "secret", "HTTP_PROXY": "http://secret",
        })
        self.assertEqual(result, {
            "HOME": "/unchanged/home", "CODEX_HOME": "/unchanged/codex",
            "PATH": "/usr/bin:/bin", "LC_ALL": "C",
        })

    def test_unrecognized_version_does_not_echo_output(self) -> None:
        result = inspect_cli(self.settings(self.fake_cli(version="oops SECRET")))
        self.assertEqual(result["reason"], "unrecognized_version")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_executable_validation(self) -> None:
        for value in ("codex", "/does/not/exist", "relative\npath", str(self.directory)):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_executable(value)
        file = self.directory / "not-executable"
        file.write_text("placeholder")
        with self.assertRaises(ValueError):
            validate_executable(file)
        self.assertEqual(validate_executable(self.fake_cli()).parent, self.directory.resolve())

    def test_settings_reject_injection_and_dangerous_controls(self) -> None:
        for model in ("-x", "model;touch /tmp/pwn", "model\nsecret", "", "$(touch x)"):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    ProbeSettings("/bin/echo", model=model)
        for timeout in (0, -1, float("nan"), float("inf"), True):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    ProbeSettings("/bin/echo", timeout_seconds=timeout)
        for features in ((), ("danger-full-access",), DEFAULT_DISABLED_FEATURES + ("hooks",)):
            with self.subTest(features=features):
                with self.assertRaises(ValueError):
                    ProbeSettings("/bin/echo", disabled_features=features)


if __name__ == "__main__":
    unittest.main()
