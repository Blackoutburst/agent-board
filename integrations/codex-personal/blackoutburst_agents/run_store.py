"""Private, local state for explicitly requested personal Codex turns.

Labels identify local context; they are not board grants or authentication.
The coordinator must hold ``work_lock`` from before ``begin_turn`` until after
``finish_turn``. An abandoned running turn is never replayed automatically.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
import uuid


class StoreError(Exception):
    """A bounded error code, without paths, prompts or process diagnostics."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _identifier(value, code="invalid_identifier"):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise StoreError(code)
    return value


def _uuid(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise StoreError("invalid_session_id") from None
    if parsed != value:
        raise StoreError("invalid_session_id")
    return value


def _private_file(path: Path):
    """Open without following a final symlink; refuse insecure existing files."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            os.close(fd)
            raise StoreError("insecure_state_file")
        return fd
    except OSError:
        raise StoreError("invalid_state_file") from None


def _result_json(result):
    if not isinstance(result, dict) or result.get("status") not in {"completed", "blocked", "cancelled"}:
        raise StoreError("invalid_result")
    forbidden = {"prompt", "api_key", "access_token", "refresh_token", "authorization", "credentials"}

    def validate(value, depth=0):
        if depth > 12:
            raise StoreError("invalid_result")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str) or key.lower() in forbidden:
                    raise StoreError("invalid_result")
                validate(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                validate(child, depth + 1)
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            raise StoreError("invalid_result")

    validate(result)
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError):
        raise StoreError("invalid_result") from None
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise StoreError("result_too_large")
    return encoded


class RunStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser().absolute()
        self._locks = {}
        self._db = None
        try:
            try:
                self.state_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = self.state_dir.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise StoreError("insecure_state_directory")
            database = self.state_dir / "runs.sqlite3"
            os.close(_private_file(database))
            # SQLite may touch these itself; reject a pre-planted alias first.
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    os.close(_private_file(sidecar))
            self._db = sqlite3.connect(database, timeout=5, isolation_level=None)
            self._db.row_factory = sqlite3.Row
            if self._db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise StoreError("unsupported_state_version")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS works (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    project_id TEXT NOT NULL, ticket_id TEXT NOT NULL, workspace TEXT NOT NULL,
                    executable TEXT NOT NULL, model TEXT, provider_session_id TEXT UNIQUE,
                    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_open_scope ON works
                    (owner_id, agent_id, project_id, ticket_id) WHERE status != 'closed';
                CREATE TABLE IF NOT EXISTS turns (
                    id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
                    request_id TEXT NOT NULL, prompt_sha256 TEXT NOT NULL, status TEXT NOT NULL,
                    pid INTEGER, provider_session_id TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                    requires_review INTEGER NOT NULL DEFAULT 0, result_json TEXT,
                    resolution TEXT, resolution_reason TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(work_id, request_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_running_turn ON turns(work_id)
                    WHERE status = 'running';
                PRAGMA user_version=1;
            """)
        except StoreError:
            if self._db is not None:
                self._db.close()
            raise
        except (OSError, sqlite3.Error):
            if self._db is not None:
                self._db.close()
            raise StoreError("state_unavailable") from None

    def close(self):
        if self._locks:
            raise StoreError("work_locked")
        if self._db is not None:
            self._db.close()
            self._db = None

    @contextmanager
    def _transaction(self):
        if self._db is None:
            raise StoreError("store_closed")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            yield self._db
            self._db.execute("COMMIT")
        except BaseException as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                raise StoreError("state_unavailable") from None
            raise

    def _work(self, work_id):
        _uuid(work_id)
        if self._db is None:
            raise StoreError("store_closed")
        row = self._db.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if row is None:
            raise StoreError("work_not_found")
        return dict(row)

    def _turn(self, turn_id):
        _uuid(turn_id)
        if self._db is None:
            raise StoreError("store_closed")
        row = self._db.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None:
            raise StoreError("turn_not_found")
        return dict(row)

    def _require_lock(self, work_id):
        if work_id not in self._locks:
            raise StoreError("work_lock_required")

    @staticmethod
    def _turn_metadata(turn):
        return {key: value for key, value in turn.items()
                if key not in {"prompt_sha256", "result_json", "resolution_reason"}}

    def create_work(self, *, owner_id, agent_id, project_id, ticket_id, workspace, executable, model=None):
        scope = tuple(_identifier(value) for value in (owner_id, agent_id, project_id, ticket_id))
        if model is not None:
            _identifier(model, "invalid_model")
        try:
            workspace = str(Path(workspace).expanduser().resolve(strict=True))
            executable = str(Path(executable).expanduser().resolve(strict=True))
            if not Path(workspace).is_dir() or not Path(executable).is_file():
                raise StoreError("invalid_work_paths")
        except (OSError, TypeError, ValueError):
            raise StoreError("invalid_work_paths") from None
        with self._transaction() as db:
            row = db.execute("""SELECT * FROM works WHERE owner_id=? AND agent_id=?
                AND project_id=? AND ticket_id=? AND status != 'closed'""", scope).fetchone()
            if row is not None:
                if (row["workspace"], row["executable"], row["model"]) != (workspace, executable, model):
                    raise StoreError("scope_configuration_conflict")
                return dict(row)
            work_id, now = str(uuid.uuid4()), time.time()
            db.execute("""INSERT INTO works(id,owner_id,agent_id,project_id,ticket_id,workspace,
                executable,model,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (work_id, *scope, workspace, executable, model, "open", now, now))
            return self._work(work_id)

    def get_work(self, work_id):
        return self._work(work_id)

    @contextmanager
    def work_lock(self, work_id):
        self._work(work_id)
        if work_id in self._locks:
            raise StoreError("work_busy")
        fd = _private_file(self.state_dir / (work_id + ".lock"))
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StoreError("work_busy") from None
            except OSError:
                raise StoreError("work_lock_unavailable") from None
            self._locks[work_id] = fd
            # A legitimate live coordinator owns this lock until it finalizes.
            # Finding running state after acquiring it is an interrupted run.
            with self._transaction() as db:
                interrupted = db.execute("SELECT id FROM turns WHERE work_id=? AND status='running'",
                                         (work_id,)).fetchall()
                if interrupted:
                    now = time.time()
                    result = json.dumps({"status": "blocked", "reason": "interrupted"})
                    db.execute("""UPDATE turns SET status='interrupted',requires_review=1,
                        result_json=?,updated_at=? WHERE work_id=? AND status='running'""",
                        (result, now, work_id))
                    db.execute("UPDATE works SET status='needs_review',updated_at=? WHERE id=?", (now, work_id))
            yield
        finally:
            self._locks.pop(work_id, None)
            os.close(fd)

    def begin_turn(self, work_id, request_id, prompt):
        self._require_lock(work_id)
        _identifier(request_id, "invalid_request_id")
        if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > 1024 * 1024:
            raise StoreError("invalid_prompt")
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self._transaction() as db:
            work = self._work(work_id)
            existing = db.execute("SELECT * FROM turns WHERE work_id=? AND request_id=?",
                                  (work_id, request_id)).fetchone()
            if existing is not None:
                if existing["prompt_sha256"] != digest:
                    raise StoreError("request_conflict")
                if existing["status"] in {"running", "interrupted"}:
                    raise StoreError("request_needs_review" if existing["status"] == "interrupted" else "work_busy")
                return {"cached": True, "turn": self._turn_metadata(dict(existing))}
            if work["status"] != "open":
                raise StoreError("work_needs_review" if work["status"] == "needs_review" else "work_closed")
            if db.execute("SELECT 1 FROM turns WHERE work_id=? AND status='running'", (work_id,)).fetchone():
                raise StoreError("work_busy")
            turn_id, now = str(uuid.uuid4()), time.time()
            db.execute("""INSERT INTO turns(id,work_id,request_id,prompt_sha256,status,
                provider_session_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)""",
                (turn_id, work_id, request_id, digest, "running", work["provider_session_id"], now, now))
            db.execute("UPDATE works SET updated_at=? WHERE id=?", (now, work_id))
            return {"cached": False, "turn": self._turn_metadata(self._turn(turn_id))}

    def on_process(self, turn_id, pid):
        if type(pid) is not int or pid <= 0:
            raise StoreError("invalid_process_id")
        with self._transaction() as db:
            turn = self._turn(turn_id)
            self._require_lock(turn["work_id"])
            if turn["status"] != "running":
                raise StoreError("turn_not_running")
            if turn["pid"] is not None and turn["pid"] != pid:
                raise StoreError("process_conflict")
            db.execute("UPDATE turns SET pid=?,updated_at=? WHERE id=?", (pid, time.time(), turn_id))

    def on_session(self, turn_id, session_id):
        _uuid(session_id)
        with self._transaction() as db:
            turn = self._turn(turn_id)
            self._require_lock(turn["work_id"])
            if turn["status"] != "running":
                raise StoreError("turn_not_running")
            work = self._work(turn["work_id"])
            if work["provider_session_id"] not in (None, session_id):
                raise StoreError("session_conflict")
            other = db.execute("SELECT id FROM works WHERE provider_session_id=? AND id!=?",
                               (session_id, work["id"])).fetchone()
            if other is not None:
                raise StoreError("session_scope_conflict")
            now = time.time()
            db.execute("UPDATE works SET provider_session_id=?,updated_at=? WHERE id=?", (session_id, now, work["id"]))
            db.execute("UPDATE turns SET provider_session_id=?,updated_at=? WHERE id=?", (session_id, now, turn_id))

    def finish_turn(self, turn_id, result):
        encoded = _result_json(result)
        with self._transaction() as db:
            turn = self._turn(turn_id)
            self._require_lock(turn["work_id"])
            if turn["status"] != "running":
                if turn["result_json"] == encoded:
                    return self._turn_metadata(turn)
                raise StoreError("turn_not_running")
            if result["status"] == "completed" and turn["provider_session_id"] is None:
                raise StoreError("session_not_recorded")
            session = result.get("provider_session_id", result.get("session_id"))
            if session is not None and session != turn["provider_session_id"]:
                raise StoreError("session_conflict")
            uncertain = result["status"] != "completed" and (
                turn["pid"] is not None or result.get("uncertain", False)
                or result.get("reason") in {"cleanup_failed", "timeout", "protocol_error", "protocol_failure"})
            now = time.time()
            db.execute("""UPDATE turns SET status=?,requires_review=?,result_json=?,updated_at=?
                WHERE id=?""", (result["status"], int(uncertain), encoded, now, turn_id))
            db.execute("UPDATE works SET status=?,updated_at=? WHERE id=?",
                       ("needs_review" if uncertain else "open", now, turn["work_id"]))
            return self._turn_metadata(self._turn(turn_id))

    def request_cancel(self, work_id):
        with self._transaction() as db:
            self._work(work_id)
            cursor = db.execute("UPDATE turns SET cancel_requested=1,updated_at=? WHERE work_id=? AND status='running'",
                                (time.time(), work_id))
            return cursor.rowcount > 0

    def is_cancelled(self, turn_id):
        return bool(self._turn(turn_id)["cancel_requested"])

    def status(self, work_id):
        work = self._work(work_id)
        rows = self._db.execute("SELECT * FROM turns WHERE work_id=? ORDER BY created_at,id", (work_id,)).fetchall()
        return {"work": work, "turns": [self._turn_metadata(dict(row)) for row in rows]}

    def turn_result(self, turn_id):
        turn = self._turn(turn_id)
        if turn["result_json"] is None:
            raise StoreError("result_not_ready")
        return json.loads(turn["result_json"])

    def resolve_interruption(self, work_id, *, decision, reason):
        self._require_lock(work_id)
        if decision not in {"continue", "abandon"}:
            raise StoreError("invalid_resolution")
        _identifier(reason, "invalid_resolution_reason")
        with self._transaction() as db:
            work = self._work(work_id)
            if work["status"] == "closed":
                raise StoreError("work_closed")
            if db.execute("SELECT 1 FROM turns WHERE work_id=? AND status='running'", (work_id,)).fetchone():
                raise StoreError("turn_still_running")
            pending = db.execute("SELECT * FROM turns WHERE work_id=? AND requires_review=1", (work_id,)).fetchall()
            if decision == "continue" and not pending:
                raise StoreError("no_interruption")
            for turn in pending:
                if turn["pid"] is not None:
                    # The transport starts a new session: PID is also PGID.
                    # A vanished leader does not prove its children stopped.
                    for check in (os.kill, os.killpg):
                        try:
                            check(turn["pid"], 0)
                        except ProcessLookupError:
                            pass
                        except OSError:
                            raise StoreError("process_state_unknown") from None
                        else:
                            raise StoreError("process_still_running")
                elif decision == "continue":
                    raise StoreError("process_state_unknown")
                result = json.loads(turn["result_json"] or "{}")
                if decision == "continue" and (result.get("reason") == "cleanup_failed"
                        or result.get("cleanup_failed") is True
                        or result.get("verification", {}).get("cleanup_confirmed") is False):
                    raise StoreError("cleanup_unconfirmed")
            if decision == "continue" and work["provider_session_id"] is None:
                raise StoreError("session_not_recorded")
            now = time.time()
            db.execute("""UPDATE turns SET requires_review=0,resolution=?,resolution_reason=?,updated_at=?
                WHERE work_id=? AND requires_review=1""", (decision, reason, now, work_id))
            db.execute("UPDATE works SET status=?,updated_at=? WHERE id=?",
                       ("open" if decision == "continue" else "closed", now, work_id))
            return self._work(work_id)
