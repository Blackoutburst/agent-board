#!/usr/bin/env python3
"""Run a local Agent Board and optionally start it automatically at login.

    python3 scripts/local_board.py run          # foreground, http://127.0.0.1:9140
    python3 scripts/local_board.py install      # start now and at every login (macOS, Linux, Windows)
    python3 scripts/local_board.py uninstall    # remove the login item and stop the board
    python3 scripts/local_board.py status

Settings come from the environment, then from the env file (default
~/.config/agent-board/board.env, or %APPDATA%\\agent-board\\board.env on Windows),
as KEY=VALUE lines. Useful keys: BOARD_PORT, BOARD_DATA_DIR, BOARD_OWNER_ID,
BOARD_OWNER_NAME, BOARD_REQUIRE_LOGIN. Only the standard library is needed;
the first run creates .venv in the repository and installs requirements.txt.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WINDOWS = os.name == "nt"
MAC = sys.platform == "darwin"


def config_dir():
    if WINDOWS:
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "agent-board"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "agent-board"


def default_data_dir():
    if WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "agent-board"
    if MAC:
        return Path.home() / "Library" / "Application Support" / "agent-board"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agent-board"


def read_env_file(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def settings(env_file):
    # Real environment variables win over the env file.
    env = {**read_env_file(env_file), **os.environ}
    port = int(env.get("BOARD_PORT", "9140"))
    host = env.get("BOARD_HOST", "127.0.0.1")
    env.setdefault("BOARD_DATA_DIR", str(default_data_dir()))
    env.setdefault("BOARD_BASE_URL", f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}")
    return env, host, port


def venv_python():
    return REPO / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def ensure_venv():
    python = venv_python()
    if not python.exists():
        print("Creating .venv ...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(REPO / ".venv")])
    requirements = (REPO / "requirements.txt").read_bytes()
    stamp = REPO / ".venv" / "agent-board-requirements.sha256"
    digest = hashlib.sha256(requirements).hexdigest()
    if not stamp.exists() or stamp.read_text().strip() != digest:
        print("Installing requirements ...", flush=True)
        subprocess.check_call([str(python), "-m", "pip", "install", "-q", "-r", str(REPO / "requirements.txt")])
        stamp.write_text(digest)
    return python


def run(args):
    env, host, port = settings(args.env_file)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        log = open(args.log, "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stderr = log
    else:
        log = None
    python = ensure_venv()
    Path(env["BOARD_DATA_DIR"]).mkdir(parents=True, exist_ok=True)
    print(f"Agent Board on {env['BOARD_BASE_URL']} (data: {env['BOARD_DATA_DIR']})", flush=True)
    command = [str(python), "-m", "uvicorn", "app:app", "--host", host, "--port", str(port)]
    raise SystemExit(subprocess.call(command, cwd=REPO, env=env, stdout=log, stderr=log))


def unit_command(args):
    command = [sys.executable, str(Path(__file__).resolve()), "--env-file", str(args.env_file), "run"]
    if WINDOWS:
        # pythonw has no console window; output goes to the log file instead.
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        command[0] = str(pythonw if pythonw.exists() else sys.executable)
        command += ["--log", str(default_data_dir() / "board.log")]
    return command


def launchd_plist(args):
    return Path.home() / "Library" / "LaunchAgents" / f"{args.name}.plist"


def systemd_unit(args):
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / f"{args.name}.service"


def install(args):
    env, _, _ = settings(args.env_file)
    ensure_venv()
    command = unit_command(args)
    if MAC:
        path = launchd_plist(args)
        path.parent.mkdir(parents=True, exist_ok=True)
        log = Path(env["BOARD_DATA_DIR"]) / "board.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as stream:
            plistlib.dump({"Label": args.name, "ProgramArguments": command, "WorkingDirectory": str(REPO),
                           "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
                           "StandardOutPath": str(log), "StandardErrorPath": str(log)}, stream)
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
        subprocess.check_call(["launchctl", "bootstrap", domain, str(path)])
        print(f"Installed launchd agent {path}")
    elif WINDOWS:
        line = subprocess.list2cmdline(command)
        subprocess.check_call(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/RL", "LIMITED",
                               "/TN", args.name, "/TR", line])
        subprocess.check_call(["schtasks", "/Run", "/TN", args.name])
        print(f"Installed scheduled task {args.name}")
    else:
        if not shutil.which("systemctl"):
            raise SystemExit("systemctl not found; run 'local_board.py run' from your session startup instead.")
        path = systemd_unit(args)
        path.parent.mkdir(parents=True, exist_ok=True)
        quoted = " ".join('"' + part.replace('"', '\\"') + '"' for part in command)
        path.write_text(f"""[Unit]
Description=Agent Board ({args.name})
After=network.target

[Service]
ExecStart={quoted}
WorkingDirectory={REPO}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""", encoding="utf-8")
        subprocess.check_call(["systemctl", "--user", "daemon-reload"])
        subprocess.check_call(["systemctl", "--user", "enable", "--now", args.name])
        print(f"Installed systemd user unit {path}")
        print("To start it at boot before you log in: loginctl enable-linger $USER")
    wait_until_ready(env["BOARD_BASE_URL"])


def uninstall(args):
    if MAC:
        path = launchd_plist(args)
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
        path.unlink(missing_ok=True)
    elif WINDOWS:
        subprocess.run(["schtasks", "/End", "/TN", args.name], capture_output=True)
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", args.name], capture_output=True)
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", args.name], capture_output=True)
        systemd_unit(args).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    print(f"Removed {args.name}; board data was kept.")


def healthy(base_url):
    try:
        with urllib.request.urlopen(base_url + "/health", timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def wait_until_ready(base_url, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if healthy(base_url):
            print(f"Board is up: {base_url}")
            return
        time.sleep(1)
    raise SystemExit(f"Board did not answer on {base_url}; check the log in the data directory.")


def status(args):
    env, _, _ = settings(args.env_file)
    up = healthy(env["BOARD_BASE_URL"])
    print(f"{env['BOARD_BASE_URL']}: {'up' if up else 'down'} (data: {env['BOARD_DATA_DIR']})")
    raise SystemExit(0 if up else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", type=Path, default=config_dir() / "board.env")
    parser.add_argument("--name", default="dev.agent-board" if MAC else "agent-board",
                        help="login item name, to run several boards side by side")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--log", type=Path, help=argparse.SUPPRESS)
    commands.add_parser("install")
    commands.add_parser("uninstall")
    commands.add_parser("status")
    args = parser.parse_args()
    args.env_file = args.env_file.expanduser().resolve()
    {"run": run, "install": install, "uninstall": uninstall, "status": status}[args.command](args)


if __name__ == "__main__":
    main()
