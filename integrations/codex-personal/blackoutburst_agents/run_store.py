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
import math
import os
import re
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


def _executed_disposition_proof(proof, work, turn, expected, intent_id):
    """Check closed evidence data; the coordinator authenticates observations.

    This is deliberately not a live grant or a process-liveness test. The caller
    must have recorded the original positive Runtime observations under its
    verified owner/source boundary before invoking the private storage mutation.
    """
    code = 'executed_disposition_proof_invalid'
    def fail():
        raise StoreError(code)
    def sha(value):
        return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None
    def opaque(value):
        return type(value) is str and re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}', value) is not None
    def positive(value):
        return type(value) is int and 1 <= value < 2**63
    def canonical(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    try:
        fields = {'schema', 'intent_id', 'state_sha256', 'result_sha256',
                  'configuration', 'identity', 'local_runtime', 'lookup', 'control'}
        if (type(proof) is not dict or set(proof) != fields
                or proof['schema'] != 'message-executed-runstore-proof-v1'
                or proof['intent_id'] != intent_id or proof['state_sha256'] != expected
                or not sha(proof['state_sha256']) or not sha(proof['result_sha256'])):
            fail()
        identity = proof['identity']
        if (type(identity) is not dict
                or set(identity) != {'owner_id', 'agent_id', 'device_id', 'profile_version', 'session_id'}
                or any(not opaque(identity[k]) for k in ('owner_id', 'agent_id', 'device_id', 'session_id'))
                or not positive(identity['profile_version'])):
            fail()
        config = proof['configuration']
        if (type(config) is not dict or set(config) != {'scope', 'work_id', 'turn_id', 'provider', 'model'}
                or type(config['scope']) is not dict
                or set(config['scope']) != {'board_endpoint', 'board_session_id', 'owner_id',
                    'agent_id', 'scope_kind', 'thread_id', 'message_id', 'source_sha256'}
                or any(type(v) is not str for v in config['scope'].values())
                or config['provider'] != 'codex_chatgpt' or type(config['model']) is not str):
            fail()
        from .runtime_journal import RuntimeJournal
        from .message_scope import local_agent
        scope = config['scope']
        validated = RuntimeJournal.configuration(scope, config['work_id'], config['turn_id'],
                                                 config['model'], provider=config['provider'])
        if (canonical(config) != canonical(validated)
                or scope['owner_id'] != work['owner_id'] or local_agent(scope) != work['agent_id']
                or scope['thread_id'] != work['project_id'] or scope['message_id'] != work['ticket_id']
                or config['work_id'] != work['id'] or config['turn_id'] != turn['id']
                or config['model'] != work['model']
                or any(identity[k] != scope[k] for k in ('owner_id', 'agent_id'))
                or identity['session_id'] != scope['board_session_id']):
            fail()
        local = proof['local_runtime']
        if (type(local) is not dict or set(local) != {'id', 'key', 'phase', 'revision', 'start_confirmed', 'receipt'}
                or type(local['id']) is not str or str(uuid.UUID(local['id'])) != local['id']
                or local['key'] != RuntimeJournal.key(config) or local['phase'] != 'finished'
                or not positive(local['revision']) or local['start_confirmed'] is not True):
            fail()
        terminal_fields = {'id', 'revision', 'state', 'owner_id', 'agent_id', 'device_id',
            'profile_version', 'session_id', 'scope_kind', 'thread_id', 'message_id',
            'source_sha256', 'work_id', 'turn_id', 'provider', 'model', 'outcome', 'reason',
            'cleanup_confirmed'}
        terminal = local['receipt']
        for value in (terminal, proof['lookup'], proof['control']):
            if (type(value) is not dict or set(value) != terminal_fields
                    or not opaque(value['id']) or not positive(value['revision'])
                    or value['state'] != 'finished' or value['outcome'] != 'blocked'
                    or value['reason'] != 'protocol_error' or value['cleanup_confirmed'] is not True
                    or any(type(value[k]) is not str for k in terminal_fields - {
                        'revision', 'profile_version', 'cleanup_confirmed'})
                    or any(value[k] != identity[k] or type(value[k]) is not type(identity[k]) for k in identity)
                    or any(value[k] != scope[k] for k in ('scope_kind', 'thread_id', 'message_id', 'source_sha256'))
                    or any(value[k] != config[k] for k in ('work_id', 'turn_id', 'provider', 'model'))
                    or canonical(value) != canonical(terminal)):
                fail()
        if len(canonical(proof)) > 32768:
            fail()
        raw = turn['result_json']
        if type(raw) is not str or len(raw) > 1024 * 1024:
            fail()
        data = raw.encode('utf-8')
        if len(data) > 1024 * 1024 or hashlib.sha256(data).hexdigest() != proof['result_sha256']:
            fail()
        def pairs(items):
            value = {}
            for key, item in items:
                if key in value: fail()
                value[key] = item
            return value
        result = json.loads(raw, object_pairs_hook=pairs)
        flags = {'cleanup_confirmed': True, 'exit_zero': False, 'session_verified': True,
                 'tool_free': True, 'turn_completed': False}
        if (type(result) is not dict or result.get('status') != 'blocked'
                or result.get('reason') != 'protocol_error'
                or type(result.get('verification')) is not dict
                or any(result['verification'].get(k) is not v for k, v in flags.items())
                or result.get('session_id') != turn['provider_session_id']
                or ('provider_session_id' in result and result['provider_session_id'] != turn['provider_session_id'])
                or _result_json(result) != raw):
            fail()
    except StoreError:
        raise
    except Exception:
        raise StoreError(code) from None


class RunStore:
    def __init__(self, state_dir: Path, *, existing_only=False):
        if type(existing_only) is not bool:raise StoreError('invalid_existing_only')
        self.state_dir = Path(state_dir).expanduser().absolute()
        self._locks = {}
        self._db = None
        try:
            try:
                if not existing_only:self.state_dir.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = self.state_dir.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise StoreError("insecure_state_directory")
            database = self.state_dir / "runs.sqlite3"
            if existing_only:
                fd=os.open(database,os.O_RDWR|os.O_NOFOLLOW)
                try:
                    info=os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                            or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1):
                        raise StoreError('insecure_state_file')
                    header=os.pread(fd,100,0)
                    if len(header)!=100 or header[:16]!=b'SQLite format 3\x00' or header[18:20]!=b'\x01\x01':
                        raise StoreError('state_schema_invalid')
                finally:os.close(fd)
            else:os.close(_private_file(database))
            # SQLite may touch these itself; reject a pre-planted alias first.
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = Path(str(database) + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    if existing_only:raise StoreError('state_sidecar_present')
                    os.close(_private_file(sidecar))
            self._db = sqlite3.connect(database.as_uri()+'?mode=rw' if existing_only else database,
                uri=existing_only,timeout=5,isolation_level=None)
            self._db.row_factory = sqlite3.Row
            version=self._db.execute("PRAGMA user_version").fetchone()[0]
            if existing_only:
                if version!=2 or self._db.execute('PRAGMA application_id').fetchone()[0]!=0:
                    raise StoreError('unsupported_state_version')
                self._check_existing_schema()
                return
            if version not in (0,1,2):raise StoreError("unsupported_state_version")
            if version==2 and tuple(row['name'] for row in self._db.execute('PRAGMA table_info(context_guards)'))!=('work_id','guard_json'):
                raise StoreError('context_guard_invalid')
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA journal_mode=DELETE")
            self._db.execute("PRAGMA synchronous=FULL")
            schema = """
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
                CREATE TABLE IF NOT EXISTS context_guards (
                    work_id TEXT PRIMARY KEY REFERENCES works(id), guard_json TEXT NOT NULL
                );
                PRAGMA user_version=2;
            """
            with self._transaction() as db:
                for statement in schema.split(';'):
                    if statement.strip():db.execute(statement)
        except StoreError:
            if self._db is not None:
                self._db.close()
            raise
        except (OSError, sqlite3.Error):
            if self._db is not None:
                self._db.close()
            raise StoreError("state_unavailable") from None


    def _check_existing_schema(self):
        from .message_abandon_protocol import guard_sql
        db=self._db
        try:guard_sql(db,('works','turns','context_guards'))
        except Exception:raise StoreError('state_schema_invalid') from None
        columns={
            'works':('id','owner_id','agent_id','project_id','ticket_id','workspace','executable','model','provider_session_id','status','created_at','updated_at'),
            'turns':('id','work_id','request_id','prompt_sha256','status','pid','provider_session_id','cancel_requested','requires_review','result_json','resolution','resolution_reason','created_at','updated_at'),
            'context_guards':('work_id','guard_json')}
        table_sql={
            'works':"""CREATE TABLE works(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,agent_id TEXT NOT NULL,
                project_id TEXT NOT NULL,ticket_id TEXT NOT NULL,workspace TEXT NOT NULL,executable TEXT NOT NULL,
                model TEXT,provider_session_id TEXT UNIQUE,status TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)""",
            'turns':"""CREATE TABLE turns(id TEXT PRIMARY KEY,work_id TEXT NOT NULL REFERENCES works(id),
                request_id TEXT NOT NULL,prompt_sha256 TEXT NOT NULL,status TEXT NOT NULL,pid INTEGER,provider_session_id TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,requires_review INTEGER NOT NULL DEFAULT 0,result_json TEXT,
                resolution TEXT,resolution_reason TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(work_id,request_id))""",
            'context_guards':"""CREATE TABLE context_guards(work_id TEXT PRIMARY KEY REFERENCES works(id),guard_json TEXT NOT NULL)"""}
        indexes={
            'one_open_scope':('works',('owner_id','agent_id','project_id','ticket_id'),"CREATE UNIQUE INDEX one_open_scope ON works (owner_id, agent_id, project_id, ticket_id) WHERE status != 'closed'"),
            'one_running_turn':('turns',('work_id',),"CREATE UNIQUE INDEX one_running_turn ON turns(work_id) WHERE status = 'running'")}
        names=set(columns)|set(indexes)|{'sqlite_autoindex_works_1','sqlite_autoindex_works_2',
            'sqlite_autoindex_turns_1','sqlite_autoindex_turns_2','sqlite_autoindex_context_guards_1'}
        rows=db.execute("SELECT type,name,tbl_name,sql FROM main.sqlite_master LIMIT 20").fetchall()
        if {r['name'] for r in rows}!=names:raise StoreError('state_schema_invalid')
        if any(tuple(r['name'] for r in db.execute('PRAGMA main.table_info('+table+')'))!=wanted for table,wanted in columns.items()):
            raise StoreError('state_schema_invalid')
        def normalize(value):
            # Ignore only lexical whitespace/case of SQL keywords/identifiers.
            # Quoted literals and token boundaries retain their semantics.
            token=re.compile(r"\s+|'(?:''|[^'])*'|[A-Za-z_][A-Za-z_0-9]*|[0-9]+|!=|[(),=]")
            result=[];position=0
            for match in token.finditer(value):
                if match.start()!=position:raise StoreError('state_schema_invalid')
                part=match.group();position=match.end()
                if not part.isspace():result.append(part if part.startswith("'") else part.lower())
            if position!=len(value):raise StoreError('state_schema_invalid')
            return tuple(result)
        for name,sql in table_sql.items():
            row=next(r for r in rows if r['name']==name)
            if row['type']!='table' or normalize(row['sql'] or '')!=normalize(sql):
                raise StoreError('state_schema_invalid')
        for name,(table,cols,sql) in indexes.items():
            row=next(r for r in rows if r['name']==name)
            if (row['type']!='index' or row['tbl_name']!=table or normalize(row['sql'] or '')!=normalize(sql)
                    or tuple(r['name'] for r in db.execute('PRAGMA main.index_info('+name+')'))!=cols):
                raise StoreError('state_schema_invalid')
        if (any(r['type'] not in ('table','index') for r in rows)
                or db.execute('SELECT 1 FROM temp.sqlite_master LIMIT 1').fetchone()):
            raise StoreError('state_schema_invalid')

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

    def create_work(self, *, owner_id, agent_id, project_id, ticket_id, workspace, executable, model=None, work_id=None):
        if work_id is not None:_uuid(work_id)
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
                if work_id is not None and row["id"]!=work_id:raise StoreError("work_identity_conflict")
                if (row["workspace"], row["executable"], row["model"]) != (workspace, executable, model):
                    raise StoreError("scope_configuration_conflict")
                return dict(row)
            if work_id is not None and db.execute("SELECT 1 FROM works WHERE id=?",(work_id,)).fetchone():
                raise StoreError("work_identity_conflict")
            work_id, now = work_id or str(uuid.uuid4()), time.time()
            db.execute("""INSERT INTO works(id,owner_id,agent_id,project_id,ticket_id,workspace,
                executable,model,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (work_id, *scope, workspace, executable, model, "open", now, now))
            return self._work(work_id)

    @staticmethod
    def _guard_value(value):
        try:
            if not isinstance(value,dict) or set(value)!={'instance_id','scope_key','generation','revision','sha256'}:raise ValueError
            _uuid(value['instance_id'])
            for key in ('scope_key','sha256'):
                if not isinstance(value[key],str) or len(value[key])!=64 or any(c not in '0123456789abcdef' for c in value[key]):raise ValueError
            if any(type(value[k]) is not int or value[k]<1 for k in ('generation','revision')):raise ValueError
            return value
        except Exception:raise StoreError('context_guard_invalid') from None

    def get_context_guard(self,work_id):
        self._work(work_id)
        try:
            row=self._db.execute('SELECT guard_json FROM context_guards WHERE work_id=?',(work_id,)).fetchone()
            if row is None:return None
            if len(row['guard_json'].encode())>2048:raise ValueError
            def pairs(items):
                out={}
                for key,value in items:
                    if key in out:raise ValueError
                    out[key]=value
                return out
            return self._guard_value(json.loads(row['guard_json'],object_pairs_hook=pairs))
        except Exception:raise StoreError('context_guard_invalid') from None

    def check_context_guard(self,work_id,supplied):
        self._require_lock(work_id)
        with self._transaction() as db:
            previous=self.get_context_guard(work_id)
            if previous is not None:
                if supplied is None:raise StoreError('context_guard_required')
                if previous!=self._guard_value(supplied):raise StoreError('context_guard_changed')
                return
            if supplied is None:return
            supplied=self._guard_value(supplied)
            if self._work(work_id)['provider_session_id'] is not None:raise StoreError('context_guard_session_unbound')
            if db.execute('SELECT COUNT(*) FROM context_guards').fetchone()[0]>=256:raise StoreError('context_guard_capacity')
            db.execute('INSERT INTO context_guards VALUES(?,?)',(work_id,json.dumps(supplied,sort_keys=True,separators=(',',':'))))

    def get_work(self, work_id):
        return self._work(work_id)

    @contextmanager
    def work_lock(self, work_id, *, inspection_only=False):
        if type(inspection_only) is not bool:
            raise StoreError('invalid_lock_mode')
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
                    if inspection_only:
                        raise StoreError('work_busy')
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

    def interruption_state_sha256(self, work_id):
        """CAS fingerprint for the explicit single-turn administrative path."""
        work=self._work(work_id)
        turns=[dict(row) for row in self._db.execute('SELECT * FROM turns WHERE work_id=? ORDER BY rowid LIMIT 2',(work_id,))]
        if len(turns)!=1:raise StoreError('interruption_state_conflict')
        try:
            raw=json.dumps({'work':work,'turn':turns[0]},sort_keys=True,ensure_ascii=False,
                separators=(',',':'),allow_nan=False).encode()
        except (ValueError,UnicodeError,TypeError):raise StoreError('interruption_state_conflict') from None
        if len(raw)>2*1024*1024:raise StoreError('interruption_state_conflict')
        return hashlib.sha256(raw).hexdigest()

    def _dispose_executed_blocked(self, work_id, *, expected_state_sha256, intent_id, proof):
        """Close one exact executed-blocked work without clearing its review.

        Only the explicit coordinator may use this storage boundary, after its
        durable intent and positive authenticated Runtime reads. There is no PID
        probe, Runtime mutation, retry, or acceptance of an already closed work.
        """
        self._require_lock(work_id)
        _uuid(intent_id)
        if (type(expected_state_sha256) is not str
                or re.fullmatch('[0-9a-f]{64}', expected_state_sha256) is None):
            raise StoreError('interruption_state_conflict')
        with self._transaction() as db:
            def schema():
                if (db.execute('PRAGMA user_version').fetchone()[0] != 2
                        or db.execute('PRAGMA application_id').fetchone()[0] != 0):
                    raise StoreError('state_schema_invalid')
                self._check_existing_schema()
            schema()
            if self.interruption_state_sha256(work_id) != expected_state_sha256:
                raise StoreError('interruption_state_conflict')
            work = self._work(work_id)
            turn = dict(db.execute('SELECT * FROM turns WHERE work_id=?', (work_id,)).fetchone())
            if (work['status'] != 'needs_review' or turn['status'] != 'blocked'
                    or type(turn['requires_review']) is not int or turn['requires_review'] != 1
                    or type(turn['pid']) is not int or turn['pid'] <= 0
                    or turn['resolution'] is not None or turn['resolution_reason'] is not None
                    or type(turn['provider_session_id']) is not str
                    or turn['provider_session_id'] != work['provider_session_id']
                    or db.execute('SELECT 1 FROM context_guards WHERE work_id=?', (work_id,)).fetchone()):
                raise StoreError('executed_disposition_predecessor_invalid')
            _uuid(turn['provider_session_id'])
            _executed_disposition_proof(proof, work, turn, expected_state_sha256, intent_id)
            now = time.time()
            if type(now) not in (int, float) or not math.isfinite(now) or now <= 0:
                raise StoreError('executed_disposition_clock_invalid')
            reason = 'message_executed_blocked_disposed:' + intent_id
            changes = db.total_changes
            db.execute('UPDATE turns SET resolution=?,resolution_reason=?,updated_at=? WHERE id=?',
                       ('abandon', reason, now, turn['id']))
            db.execute('UPDATE works SET status=?,updated_at=? WHERE id=?', ('closed', now, work_id))
            schema()
            expected_work = {**work, 'status': 'closed', 'updated_at': now}
            expected_turn = {**turn, 'resolution': 'abandon', 'resolution_reason': reason, 'updated_at': now}
            if (db.total_changes - changes != 2 or self._work(work_id) != expected_work
                    or self._turn(turn['id']) != expected_turn):
                raise StoreError('executed_disposition_postcondition')
            return self._work(work_id)

    def resolve_interruption(self, work_id, *, decision, reason, expected_state_sha256=None):
        self._require_lock(work_id)
        if decision not in {"continue", "abandon"}:
            raise StoreError("invalid_resolution")
        _identifier(reason, "invalid_resolution_reason")
        with self._transaction() as db:
            if expected_state_sha256 is not None:
                from .message_abandon_protocol import guard_sql
                guard_sql(db,('works','turns','context_guards'))
                if (type(expected_state_sha256) is not str or len(expected_state_sha256)!=64
                        or any(c not in '0123456789abcdef' for c in expected_state_sha256)
                        or self.interruption_state_sha256(work_id)!=expected_state_sha256):
                    raise StoreError('interruption_state_conflict')
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
