#!/usr/bin/env python3
"""Apply a pinned projects/tickets release without changing auth, nginx or units.

Run on the server as root: sudo python3 updates/<release>/apply_projects_release.py
The sibling payload/ and manifest.json contain only reviewed source files.
"""
from __future__ import annotations

import ast
from contextlib import closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

RUNTIME = {"app.py", "board.py", "projects.py", "web/index.html", "web/app.js", "web/style.css"}
PINS = RUNTIME | {"platform_auth.py", "backup.py", "notifications.py", "manage.py", "requirements.txt"}
SOURCE = Path("/home/ubuntu/collaboration-board")
DATA = Path("/home/ubuntu/.local/share/collaboration-board")
SERVICE = "collaboration-board.service"
UNCHECKED = object()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def regular(path, absent=False):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise RuntimeError(f"Not a regular file: {path}")
    if not path.exists() and not absent:
        raise RuntimeError(f"Required file missing: {path}")


def file_hash(path):
    if path.parent.is_symlink():
        raise RuntimeError(f"Symlink source directory refused: {path.parent}")
    regular(path, absent=True)
    return digest(path) if path.exists() else None


def require_hash(path, expected):
    if file_hash(path) != expected:
        raise RuntimeError(f"Source changed since review: {path}. Reconcile before retrying.")


def validate(release, source):
    for root in (release, source):
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Release and source must be real directories")
    regular(release / "manifest.json")
    manifest = json.loads((release / "manifest.json").read_text())
    if manifest.get("format") != "blackoutburst-board-projects-v1":
        raise RuntimeError("Unexpected release format")
    if set(manifest.get("files", {})) != RUNTIME or set(manifest.get("previous", {})) != PINS:
        raise RuntimeError("Release file scope differs from the fixed allowlist")
    for relative in PINS:
        target = source / relative
        if target.parent.is_symlink():
            raise RuntimeError("Symlink source directory refused")
        regular(target, absent=True)
        actual = digest(target) if target.exists() else None
        if actual != manifest["previous"][relative]:
            raise RuntimeError(f"Source changed since review: {relative}. Reconcile before retrying.")
    for relative in RUNTIME:
        candidate = release / "payload" / relative
        if candidate.parent.is_symlink() or (release / "payload").is_symlink():
            raise RuntimeError("Symlink payload directory refused")
        regular(candidate)
        if digest(candidate) != manifest["files"][relative]:
            raise RuntimeError(f"Candidate checksum mismatch: {relative}")
        if relative.endswith(".py"):
            ast.parse(candidate.read_text(), filename=relative)
    return manifest


def database_snapshot(database, destination):
    regular(database)
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database integrity check failed")
            target.execute("PRAGMA journal_mode=DELETE")
    destination.chmod(0o600)


def atomic_copy(source, target, owner=None, mode=0o644,
                expected_target=UNCHECKED, expected_source=UNCHECKED):
    fd, temporary = tempfile.mkstemp(prefix=".board-release-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        if owner:
            os.chown(temporary, *owner)
        # Check copied bytes, not just the candidate before its potentially slow
        # read. Recheck the target after staging and immediately before replace.
        if expected_source is not UNCHECKED and digest(Path(temporary)) != expected_source:
            raise RuntimeError(f"Copy source changed during staging: {source}")
        if expected_target is not UNCHECKED:
            require_hash(target, expected_target)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_release(release, source, data, backup_root, control, migrate, health, preflight, owner=None):
    """Callbacks make the transaction testable against synthetic data and services."""
    manifest = validate(release, source)
    if data.is_symlink() or not data.is_dir():
        raise RuntimeError("Data directory must be a real existing directory")
    database = data / "board.sqlite3"
    regular(database)
    preflight()
    validate(release, source)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-projects")
    backup = backup_root / stamp
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    backup.chmod(0o700)
    (backup / "source").mkdir(mode=0o700)
    db_stat = database.stat()
    stopped = False
    installed = []
    migration_started = False

    def check_sources():
        for relative in PINS:
            expected = manifest["files"][relative] if relative in installed else manifest["previous"][relative]
            require_hash(source / relative, expected)

    def preserve_failed_database():
        # Preserve records accepted during a failed startup before any rollback.
        try:
            database_snapshot(database, backup / "failed-board.sqlite3")
        except Exception:
            # A corrupt candidate DB must not prevent restoration of its checked
            # snapshot. Retain raw bytes and sidecars for manual investigation.
            for suffix in ("", "-wal", "-shm"):
                failed = Path(str(database) + suffix)
                if failed.exists() and not failed.is_symlink():
                    destination = backup / ("failed-raw-board.sqlite3" + suffix)
                    shutil.copyfile(failed, destination)
                    destination.chmod(0o600)

    def manual_recovery(error, restored):
        # Keep candidate DB/source untouched on conflict; an automatic rollback
        # must never destroy a colleague's edit or start an unreviewed mixture.
        try:
            (backup / "manual-recovery.json").write_text(json.dumps({
                "service_stopped": stopped, "installed": installed, "restored": restored,
                "database_migration_started": migration_started,
            }, indent=2) + "\n")
        except OSError:
            pass
        service_state = "Board service remains stopped" if stopped else "Board service stop could not be confirmed"
        return RuntimeError("Automatic rollback stopped to preserve concurrent changes or because restoration failed. "
                            f"{service_state}; reconcile source and database manually. "
                            f"Private snapshot: {backup}. Cause: {error}")

    try:
        control("stop")
        stopped = True
        # Refuse concurrent source edits that arrived during preflight or shutdown.
        validate(release, source)
        database_snapshot(database, backup / "board.sqlite3")
        for relative in RUNTIME:
            current = source / relative
            if current.exists():
                saved = backup / "source" / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, saved)
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        # Snapshot/copy operations can be slow. Revalidate both the live source
        # and the captured originals before the first runtime mutation.
        if validate(release, source) != manifest:
            raise RuntimeError("Release manifest changed after preflight")
        for relative in RUNTIME:
            require_hash(backup / "source" / relative, manifest["previous"][relative])
        for relative in sorted(RUNTIME):
            check_sources()
            atomic_copy(release / "payload" / relative, source / relative, owner,
                        expected_target=manifest["previous"][relative],
                        expected_source=manifest["files"][relative])
            installed.append(relative)
        check_sources()
        migration_started = True
        migrate()
        check_sources()
        control("start")
        stopped = False
        health()
        check_sources()
        (backup / "applied.json").write_text(json.dumps({"applied": True, "schema": 2,
            "files": manifest["files"], "completed_at": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    except BaseException as original_error:
        if installed or migration_started:
            restored = []
            try:
                control("stop")
                stopped = True
                preserve_failed_database()
                # Check every installed file before changing any of them. A
                # conflict leaves the live candidate state intact and stopped.
                for relative in installed:
                    require_hash(source / relative, manifest["files"][relative])
                for relative in installed:
                    saved = backup / "source" / relative
                    target = source / relative
                    expected = manifest["previous"][relative]
                    if expected is not None:
                        atomic_copy(saved, target, owner, saved.stat().st_mode & 0o777,
                                    expected_target=manifest["files"][relative], expected_source=expected)
                    else:
                        require_hash(target, manifest["files"][relative])
                        target.unlink()
                    restored.append(relative)
                for relative in restored:
                    require_hash(source / relative, manifest["previous"][relative])
                if migration_started:
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(database) + suffix)
                        if sidecar.exists():
                            regular(sidecar)
                            sidecar.unlink()
                    atomic_copy(backup / "board.sqlite3", database,
                                (db_stat.st_uid, db_stat.st_gid) if owner else None, db_stat.st_mode & 0o777)
                for relative in restored:
                    require_hash(source / relative, manifest["previous"][relative])
            except BaseException as rollback_error:
                raise manual_recovery(rollback_error, restored) from original_error
        if stopped:
            try:
                control("start")
            except BaseException as restart_error:
                raise RuntimeError(f"Original board service could not restart. Private snapshot: {backup}") from restart_error
        raise
    return backup


def run_as_owner(code, args, python, env):
    subprocess.run(["/usr/sbin/runuser", "-u", "ubuntu", "--", str(python), "-c", code, *map(str, args)],
                   check=True, env=env, cwd=SOURCE)


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run this release installer with sudo; no password is stored by the script.")
    release = Path(__file__).resolve().parent
    user = pwd.getpwnam("ubuntu")
    python = SOURCE / ".venv/bin/python"
    if not python.exists():
        raise RuntimeError("Existing board virtual environment is missing")
    # Read only the non-secret data path from the unit's established environment file.
    env_file = Path("/etc/collaboration-board.env")
    configured = [line.partition("=")[2].strip().strip('"\'') for line in env_file.read_text().splitlines()
                  if line.startswith("BOARD_DATA_DIR=")]
    if configured != [str(DATA)]:
        raise RuntimeError("Unexpected board data directory; review deployment configuration first")
    directory = subprocess.check_output(["systemctl", "show", SERVICE, "-p", "WorkingDirectory", "--value"], text=True).strip()
    if directory != str(SOURCE):
        raise RuntimeError("Unexpected service working directory")
    subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=True)
    with open("/var/lock/blackoutburst-board-update.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = {"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1", "BLACKOUT_SHARED_AUTH": "0",
               "BOARD_BASE_URL": "http://127.0.0.1:9140", "BOARD_ALLOW_LOCAL_PASSWORDS": "1"}

        def control(action):
            subprocess.run(["systemctl", action, SERVICE], check=True)
            if action == "stop":
                if subprocess.call(["systemctl", "is-active", "--quiet", SERVICE]) == 0:
                    raise RuntimeError("Board service did not stop")

        def preflight():
            with tempfile.TemporaryDirectory(prefix="board-projects-preflight-") as temporary:
                path = Path(temporary)
                os.chown(path, user.pw_uid, user.pw_gid)
                environment = {**env, "PYTHONPATH": str(release / "payload") + ":" + str(SOURCE),
                               "CANDIDATE_DIR": str(release / "payload"),
                               "BOARD_DATA_DIR": str(path / "data")}
                code = """import os, sys
sys.path.insert(0, os.environ['CANDIDATE_DIR'])
from board import Board
b=Board(os.environ['BOARD_DATA_DIR'])
b.ensure_owner('thescreamer','TheScreamer')
a=b.human_session('thescreamer')['actor']
p=b.create_project(a,'Preflight',['thescreamer'],idempotency_key='project')
t=b.create_ticket(a,p['id'],'Preflight ticket',idempotency_key='ticket')
t=b.claim_ticket(a,t['id'],t['version'])
t=b.update_ticket(a,t['id'],t['version'],status='done',outcome='Checked',evidence='Synthetic preflight')
assert t['status']=='done'
from app import create_app
app=create_app(data_dir=os.environ['BOARD_DATA_DIR'])
assert any(getattr(r,'path','')=='/api/projects' for r in app.routes)
"""
                run_as_owner(code, [], python, environment)

        def migrate():
            run_as_owner("from board import Board; import sys; Board(sys.argv[1])", [DATA], python,
                         {**env, "PYTHONPATH": str(SOURCE), "BOARD_DATA_DIR": str(DATA)})

        def health():
            last = None
            for attempt in range(20):
                try:
                    with urllib.request.urlopen("http://127.0.0.1:9140/health", timeout=2) as response:
                        result = json.load(response)
                    if result.get("status") != "ok" or not {"projects", "tickets"}.issubset(result.get("features", [])):
                        raise RuntimeError("New board health features not present")
                    try:
                        urllib.request.urlopen("http://127.0.0.1:9140/api/projects", timeout=2)
                    except urllib.error.HTTPError as error:
                        if error.code != 401:
                            raise RuntimeError("Project API authentication check failed")
                    else:
                        raise RuntimeError("Project API unexpectedly accessible anonymously")
                    return
                except (OSError, ValueError, RuntimeError) as error:
                    last = error
                    time.sleep(0.5)
            raise RuntimeError("New board routing/health check failed") from last

        snapshot = apply_release(release, SOURCE, DATA, Path("/var/backups/collaboration-board"),
                                 control, migrate, health, preflight, (user.pw_uid, user.pw_gid))
        print("Projects and tickets active. Private snapshot: " + str(snapshot))
        print("Only the board service restarted. Existing accounts, policies, nginx and other services retained.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit("Projects/tickets update stopped: " + str(error))
