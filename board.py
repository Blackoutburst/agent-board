"""Persistent collaboration board. All authorization and claims live here."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from contextlib import contextmanager

from projects import migrate_projects
from coordination import CoordinationMixin, migrate_coordination
from profiles import ProfilesMixin, migrate_profiles
from communication import CommunicationMixin, migrate_communication
from runtime import RuntimeMixin, migrate_runtime, migrate_message_runtime
from usage_observations import UsageObservationsMixin, migrate_usage_observations
from message_feed import MessageFeedMixin, migrate_message_feed


class BoardError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value):
    return json.loads(value)


def _id():
    return secrets.token_hex(16)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _strings(values, name):
    if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() or len(v) > 256 for v in values):
        raise BoardError(f"{name} must be a list of nonempty strings")
    return list(dict.fromkeys(values))


class Board(MessageFeedMixin, UsageObservationsMixin, RuntimeMixin, CommunicationMixin, ProfilesMixin, CoordinationMixin):
    def __init__(self, data_dir, base_url="", lease_seconds=300):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "attachments").mkdir(exist_ok=True)
        self.db_path = self.data_dir / "board.sqlite3"
        self.base_url = base_url.rstrip("/")
        self.ticket_lease_seconds = float(os.environ.get("BOARD_TICKET_LEASE_SECONDS", "1800"))
        if self.ticket_lease_seconds <= 0:
            raise ValueError("BOARD_TICKET_LEASE_SECONDS must be positive")
        self.lease_seconds = float(lease_seconds)
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        with self._tx() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 10:
                raise RuntimeError("Database schema is newer than this application")
            if version == 0:
                # Run each statement in the surrounding transaction; executescript
                # would commit first and break atomic initialization/migration.
                for statement in """
                    CREATE TABLE owners(id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                        password TEXT, policy TEXT NOT NULL);
                    CREATE TABLE human_sessions(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                        token_hash TEXT UNIQUE NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);
                    CREATE TABLE devices(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                        label TEXT NOT NULL, capabilities TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                        created_at REAL NOT NULL, revoked_at REAL);
                    CREATE TABLE sessions(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                        device_id TEXT NOT NULL REFERENCES devices(id), label TEXT NOT NULL,
                        capabilities TEXT NOT NULL, created_at REAL NOT NULL, last_seen_at REAL);
                    CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    CREATE TABLE thread_members(thread_id TEXT NOT NULL REFERENCES threads(id),
                        owner_id TEXT NOT NULL REFERENCES owners(id), PRIMARY KEY(thread_id,owner_id));
                    CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                        thread_id TEXT NOT NULL REFERENCES threads(id), body TEXT NOT NULL, author TEXT NOT NULL,
                        created_at REAL NOT NULL, reply_to TEXT, mentions TEXT NOT NULL, attachments TEXT NOT NULL);
                    CREATE INDEX messages_thread_seq ON messages(thread_id,seq);
                    CREATE TABLE read_state(owner_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                        seq INTEGER NOT NULL, PRIMARY KEY(owner_id,thread_id));
                    CREATE TABLE reader_state(reader TEXT PRIMARY KEY, seq INTEGER NOT NULL);
                    CREATE TABLE cursors(token_hash TEXT PRIMARY KEY, reader TEXT NOT NULL, seq INTEGER NOT NULL);
                    CREATE TABLE tasks(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                        title TEXT NOT NULL, instructions TEXT NOT NULL, requirements TEXT NOT NULL,
                        destination_kind TEXT NOT NULL, destination_id TEXT NOT NULL, creator TEXT NOT NULL,
                        action TEXT NOT NULL, resource TEXT NOT NULL, required_capabilities TEXT NOT NULL,
                        prerequisites TEXT NOT NULL, status TEXT NOT NULL, claim_version INTEGER NOT NULL DEFAULT 0,
                        claimant TEXT, lease_expires_at REAL, progress TEXT NOT NULL,
                        outcome TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL);
                    CREATE INDEX tasks_thread ON tasks(thread_id);
                    CREATE TABLE resource_locks(resource TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id));
                    CREATE TABLE attachments(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                        filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL);
                    CREATE TABLE idempotency(actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(actor,operation,key));
                    CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                        actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
                """.split(";"):
                    if statement.strip():
                        db.execute(statement)
            # Additive outbox upgrade also applies to already deployed schema v1.
            db.execute("""CREATE TABLE IF NOT EXISTS notification_events(
                id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                thread_id TEXT NOT NULL, kind TEXT NOT NULL,
                target_owners TEXT NOT NULL, title TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', discord_message_id TEXT)""")
            if version < 2:
                migrate_projects(db)
                db.execute("PRAGMA user_version=2")
            if version < 3:
                migrate_coordination(db)
                db.execute("PRAGMA user_version=3")
            if version < 4:
                migrate_profiles(db)
                db.execute("PRAGMA user_version=4")
            if version < 5:
                migrate_communication(db)
                db.execute("PRAGMA user_version=5")
            if version < 6:
                migrate_runtime(db)
                db.execute("PRAGMA user_version=6")
            if version < 7:
                migrate_usage_observations(db)
                db.execute("PRAGMA user_version=7")
            if version < 8:
                db.execute("""CREATE TABLE ticket_attachments(
                    ticket_id TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
                    attachment_id TEXT NOT NULL REFERENCES attachments(id),
                    position INTEGER NOT NULL,
                    PRIMARY KEY(ticket_id,attachment_id))""")
                db.execute("PRAGMA user_version=8")
            if version < 9:
                migrate_message_feed(db)
                db.execute("PRAGMA user_version=9")
            if version < 10:
                migrate_message_runtime(db)
                db.execute("PRAGMA user_version=10")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _tx(self):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            else:
                db.execute("COMMIT")

    def _audit(self, db, actor, event, detail):
        event_id = db.execute("INSERT INTO audit(timestamp,actor,event,detail) VALUES(?,?,?,?)",
                          (time.time(), _json(actor), event, _json(detail))).lastrowid
        if detail.get("ticket_id"):
            db.execute("INSERT INTO ticket_events(ticket_id,actor,event,detail,created_at) VALUES(?,?,?,?,?)",
                       (detail["ticket_id"], _json(self._author(db, actor)), event, _json(detail), time.time()))
        return event_id

    def _queue_notification(self, db, thread_id, kind, event_key, target_owners, title):
        participants = {row[0] for row in db.execute("SELECT owner_id FROM thread_members WHERE thread_id=?", (thread_id,))}
        if not {"blackoutburst", "thescreamer"}.issubset(participants):
            return
        targets = sorted(set(target_owners) & participants)
        if not targets:
            return
        title = " ".join("".join(c if c.isprintable() else " " for c in title).split())[:200]
        db.execute("""INSERT OR IGNORE INTO notification_events(
            id,event_key,thread_id,kind,target_owners,title,created_at)
            VALUES(?,?,?,?,?,?,?)""", (_id(), event_key, thread_id, kind, _json(targets), title, time.time()))

    def _owner(self, db, owner_id):
        owner = db.execute("SELECT * FROM owners WHERE id=?", (owner_id,)).fetchone()
        if not owner:
            raise BoardError("Unknown owner", 404)
        return owner

    def _actor(self, db, actor, permission=None, session=True):
        if not isinstance(actor, dict) or actor.get("kind") not in ("human", "agent"):
            raise BoardError("Authentication required", 401)
        owner = self._owner(db, actor.get("owner_id"))
        if actor["kind"] == "human":
            if actor.get("session_id"):
                row = db.execute("SELECT * FROM human_sessions WHERE id=? AND owner_id=?",
                                 (actor["session_id"], owner["id"])).fetchone()
                if not row or row["revoked_at"] is not None or row["expires_at"] <= time.time():
                    raise BoardError("Human session expired or revoked", 401)
            return {"kind": "human", "owner_id": owner["id"], **({"session_id": actor["session_id"]} if actor.get("session_id") else {})}
        device = db.execute("SELECT * FROM devices WHERE id=? AND owner_id=?",
                            (actor.get("device_id"), owner["id"])).fetchone()
        if not device or device["revoked_at"] is not None:
            raise BoardError("Device revoked or unknown", 401)
        policy = _decode(owner["policy"])
        if not policy["enabled"]:
            raise BoardError("Your owner has not enabled agent participation", 403)
        if permission and not policy[permission]:
            raise BoardError("Your owner has not authorized this operation", 403)
        validated = {"kind": "agent", "owner_id": owner["id"], "device_id": device["id"]}
        if actor.get("session_id"):
            registered = db.execute("SELECT * FROM sessions WHERE id=? AND device_id=? AND owner_id=?",
                                    (actor["session_id"], device["id"], owner["id"])).fetchone()
            if not registered:
                raise BoardError("Unknown session for this device", 403)
            validated["session_id"] = registered["id"]
            self._profile_authorize(db, validated, permission)
        elif session:
            raise BoardError("Register a separate session for this chat first", 403)
        return validated

    def _human(self, db, actor):
        actor = self._actor(db, actor)
        if actor["kind"] != "human":
            raise BoardError("Only the human owner can manage permissions", 403)
        return actor

    def _member(self, db, actor, thread_id):
        row = db.execute("SELECT t.* FROM threads t JOIN thread_members m ON m.thread_id=t.id WHERE t.id=? AND m.owner_id=?",
                         (thread_id, actor["owner_id"])).fetchone()
        if not row or not self._profile_thread_allowed(db, actor, thread_id):
            raise BoardError("Conversation not found", 404)
        return row

    def _author(self, db, actor):
        result = dict(actor)
        result["display_name"] = self._owner(db, actor["owner_id"])["display_name"]
        if actor["kind"] == "agent":
            result["device_label"] = db.execute("SELECT label FROM devices WHERE id=?", (actor["device_id"],)).fetchone()[0]
            result["session_label"] = db.execute("SELECT label FROM sessions WHERE id=?", (actor["session_id"],)).fetchone()[0]
            identity = db.execute("SELECT p.id,p.fields,s.agent_kind,p.version FROM agent_profiles p JOIN sessions s ON s.agent_id=p.id WHERE s.id=?", (actor["session_id"],)).fetchone()
            if identity:
                result.update(agent_id=identity["id"], agent_name=_decode(identity["fields"])["name"], agent_kind=identity["agent_kind"], profile_version=identity["version"])
        return result

    def _actor_key(self, actor):
        return _json(actor)

    def _retry(self, db, actor, operation, key, payload):
        if not isinstance(key, str) or len(key) > 256:
            raise BoardError("Invalid idempotency key")
        if not key:
            return None
        row = db.execute("SELECT * FROM idempotency WHERE actor=? AND operation=? AND key=?",
                         (self._actor_key(actor), operation, key)).fetchone()
        if row and row["payload_hash"] != _hash(_json(payload)):
            raise BoardError("Idempotency key was used with different content", 409)
        return row["record_id"] if row else None

    def _remember(self, db, actor, operation, key, payload, record_id):
        if key:
            db.execute("INSERT INTO idempotency VALUES(?,?,?,?,?)",
                       (self._actor_key(actor), operation, key, _hash(_json(payload)), record_id))

    def ensure_owner(self, owner_id, display_name, password=None):
        if not isinstance(owner_id, str) or not owner_id or not isinstance(display_name, str) or not display_name:
            raise BoardError("Owner ID and display name required")
        encoded = None
        if password is not None:
            if not isinstance(password, str) or len(password) < 8:
                raise BoardError("Password must have at least eight characters")
            salt = secrets.token_hex(16)
            digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()
            encoded = f"pbkdf2_sha256$600000${salt}${digest}"
        with self._tx() as db:
            exists = db.execute("SELECT id FROM owners WHERE id=?", (owner_id,)).fetchone()
            if not exists:
                policy = {"enabled": False, "allow_read": False, "allow_reply": False, "allow_requests": False,
                          "actions": [], "resources": [], "granted_by": None, "granted_at": None}
                db.execute("INSERT INTO owners VALUES(?,?,?,?)", (owner_id, display_name, encoded, _json(policy)))
            elif encoded is not None:
                db.execute("UPDATE owners SET password=?,display_name=? WHERE id=?", (encoded, display_name, owner_id))
            return {"id": owner_id, "display_name": self._owner(db, owner_id)["display_name"]}

    def password_login(self, username, password):
        with self._connection() as db:
            row = db.execute("SELECT * FROM owners WHERE id=? COLLATE NOCASE OR display_name=? COLLATE NOCASE", (username, username)).fetchone()
            encoded = row["password"] if row and row["password"] else None
            if encoded:
                _, rounds, salt, expected = encoded.split("$")
                actual = hashlib.pbkdf2_hmac("sha256", str(password).encode(), bytes.fromhex(salt), int(rounds)).hex()
                if hmac.compare_digest(actual, expected):
                    return {"kind": "human", "owner_id": row["id"]}
        raise BoardError("Invalid username or password", 401)

    def human_session(self, owner_id):
        token, session_id, now = secrets.token_urlsafe(32), _id(), time.time()
        with self._tx() as db:
            self._owner(db, owner_id)
            db.execute("INSERT INTO human_sessions VALUES(?,?,?,?,?,NULL)",
                       (session_id, owner_id, _hash(token), now, now + 12 * 3600))
        return {"token": token, "actor": {"kind": "human", "owner_id": owner_id, "session_id": session_id}}

    def authenticate(self, token, kind=None):
        if not isinstance(token, str) or not token:
            raise BoardError("Authentication required", 401)
        with self._connection() as db:
            if kind in (None, "human"):
                row = db.execute("SELECT * FROM human_sessions WHERE token_hash=?", (_hash(token),)).fetchone()
                if row and row["revoked_at"] is None and row["expires_at"] > time.time():
                    return {"kind": "human", "owner_id": row["owner_id"], "session_id": row["id"]}
            if kind in (None, "agent"):
                row = db.execute("SELECT * FROM devices WHERE token_hash=?", (_hash(token),)).fetchone()
                if row and row["revoked_at"] is None:
                    return {"kind": "agent", "owner_id": row["owner_id"], "device_id": row["id"]}
        raise BoardError("Invalid or revoked credentials", 401)

    def logout(self, token):
        with self._tx() as db:
            db.execute("UPDATE human_sessions SET revoked_at=? WHERE token_hash=?", (time.time(), _hash(token)))
        return {"ok": True}

    def get_settings(self, actor):
        with self._connection() as db:
            actor = self._human(db, actor)
            owner = self._owner(db, actor["owner_id"])
            devices = [self._device(row) for row in db.execute("SELECT * FROM devices WHERE owner_id=? ORDER BY created_at", (owner["id"],))]
            sessions = [self._session(row) for row in db.execute("SELECT * FROM sessions WHERE owner_id=? ORDER BY created_at", (owner["id"],))]
            owners = [dict(row) for row in db.execute("SELECT id,display_name FROM owners ORDER BY id")]
            routing_devices = [self._device(row) for row in db.execute("SELECT * FROM devices WHERE revoked_at IS NULL ORDER BY owner_id,label")]
            routing_sessions = [self._session(row) for row in db.execute("SELECT s.* FROM sessions s JOIN devices d ON d.id=s.device_id WHERE d.revoked_at IS NULL ORDER BY s.owner_id,s.label")]
            return {"owner": {"id": owner["id"], "display_name": owner["display_name"]}, "policy": _decode(owner["policy"]),
                    "devices": devices, "sessions": sessions, "owners": owners,
                    "routing": {"devices": routing_devices, "sessions": routing_sessions}}

    def set_policy(self, actor, enabled, allow_read, allow_reply, allow_requests, actions, resources):
        if any(type(v) is not bool for v in (enabled, allow_read, allow_reply, allow_requests)):
            raise BoardError("Policy switches must be booleans")
        policy = {"enabled": enabled, "allow_read": allow_read, "allow_reply": allow_reply, "allow_requests": allow_requests,
                  "actions": _strings(actions, "actions"), "resources": _strings(resources, "resources")}
        with self._tx() as db:
            actor = self._human(db, actor)
            policy.update(granted_by=actor["owner_id"], granted_at=time.time())
            db.execute("UPDATE owners SET policy=? WHERE id=?", (_json(policy), actor["owner_id"]))
            self._audit(db, actor, "policy_changed", policy)
        return policy

    def _device(self, row):
        return {"id": row["id"], "owner_id": row["owner_id"], "label": row["label"], "capabilities": _decode(row["capabilities"]),
                "created_at": row["created_at"], "revoked_at": row["revoked_at"], "revoked": row["revoked_at"] is not None}

    def _session(self, row):
        return {"id": row["id"], "owner_id": row["owner_id"], "device_id": row["device_id"], "label": row["label"],
                "capabilities": _decode(row["capabilities"]), "last_seen_at": row["last_seen_at"],
                "online": row["last_seen_at"] is not None and row["last_seen_at"] > time.time() - 900,
                "skills": _decode(row["skills"]), "agent_kind": row["agent_kind"], "autonomous": bool(row["autonomous"]), "agent_id": row["agent_id"], "profile_version": row["profile_version"]}

    def create_device(self, actor, label, capabilities):
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise BoardError("Device label required (maximum 200 characters)")
        capabilities = _strings(capabilities, "capabilities")
        token, device_id, now = secrets.token_urlsafe(32), _id(), time.time()
        with self._tx() as db:
            actor = self._human(db, actor)
            db.execute("INSERT INTO devices(id,owner_id,label,capabilities,token_hash,created_at,revoked_at) VALUES(?,?,?,?,?,?,NULL)",
                       (device_id, actor["owner_id"], label.strip(), _json(capabilities), _hash(token), now))
            self._audit(db, actor, "device_created", {"device_id": device_id, "capabilities": capabilities})
            result = self._device(db.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())
        return {**result, "token": token}

    def revoke_device(self, actor, device_id):
        with self._tx() as db:
            actor = self._human(db, actor)
            row = db.execute("SELECT * FROM devices WHERE id=? AND owner_id=?", (device_id, actor["owner_id"])).fetchone()
            if not row:
                raise BoardError("Device not found", 404)
            db.execute("UPDATE devices SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (time.time(), device_id))
            self._audit(db, actor, "device_revoked", {"device_id": device_id})
        return {"ok": True, "id": device_id}

    def register_session(self, actor, session_id, label, capabilities):
        capabilities = _strings(capabilities, "capabilities")
        with self._tx() as db:
            raw = {key: actor.get(key) for key in ('kind', 'owner_id', 'device_id')} if isinstance(actor, dict) else actor
            actor = self._actor(db, raw, "allow_read", session=False)
            if actor["kind"] != "agent":
                raise BoardError("Device credentials required", 403)
            existing = db.execute('SELECT * FROM sessions WHERE id=?', (session_id,)).fetchone()
            if existing and (existing['device_id'] != actor['device_id'] or existing['owner_id'] != actor['owner_id']):
                raise BoardError('Session ID belongs to another device', 409)
            if existing and existing['agent_id']:
                profile = self._profile_owned(db, actor, existing['agent_id'])
                # read_inbox calls this compatibility registration on every
                # poll. Only register_agent returns the updated configuration,
                # so a poll must never silently lift the profile version fence.
                if existing['profile_version'] != profile['version']:
                    raise BoardError('Agent profile changed; explicitly call register_agent to read its new configuration', 409)
                kind = existing['agent_kind']
                skills = _decode(existing['skills'])
                autonomous = bool(existing['autonomous'])
            else:
                profile = self._binding_profile(db, actor, 'default', 'agent')
                kind, skills, autonomous = 'agent', [], False
            self._register_profile_session(db, actor, profile, session_id, label, capabilities, skills, kind, autonomous, seen=False)
            return {**actor, 'session_id': session_id}

    def actor_session(self, actor, session_id):
        with self._connection() as db:
            return self._actor(db, {**actor, "session_id": session_id})

    def _thread(self, db, row, owner_id):
        participants = [r[0] for r in db.execute("SELECT owner_id FROM thread_members WHERE thread_id=? ORDER BY owner_id", (row["id"],))]
        unread = db.execute("SELECT COUNT(*) FROM messages WHERE thread_id=? AND seq>COALESCE((SELECT seq FROM read_state WHERE owner_id=? AND thread_id=?),0)",
                            (row["id"], owner_id, row["id"])).fetchone()[0]
        first_message = db.execute("SELECT body FROM messages WHERE thread_id=? ORDER BY seq LIMIT 1", (row["id"],)).fetchone()
        # Keep the original conversation title intact. The first message supplies
        # the exchange's subject while the ticket remains its separate context.
        message_preview = " ".join(first_message["body"].split())[:180] if first_message else ""
        ticket_row = db.execute("""SELECT t.id,t.number,t.title,t.project_id,p.name AS project_name
            FROM tickets t JOIN projects p ON p.id=t.project_id
            JOIN project_members m ON m.project_id=t.project_id
            WHERE t.thread_id=? AND m.owner_id=?""", (row["id"], owner_id)).fetchone()
        # Conversation membership alone must not expose project metadata.
        ticket = dict(ticket_row) if ticket_row else None
        if ticket:
            ticket["url"] = self.base_url + "/?project=" + ticket["project_id"] + "&ticket=" + ticket["id"]
        return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"], "created_at": row["created_at"],
                "participants": participants, "unread_count": unread, "url": self.base_url + "/?thread=" + row["id"],
                "ticket": ticket, "message_preview": message_preview}

    def list_threads(self, actor, query=""):
        if not isinstance(query, str) or len(query) > 500:
            raise BoardError("Invalid search query")
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            rows = db.execute("""SELECT DISTINCT t.* FROM threads t JOIN thread_members m ON m.thread_id=t.id
                WHERE m.owner_id=?
                AND (NOT EXISTS (SELECT 1 FROM tickets k WHERE k.thread_id=t.id)
                     OR EXISTS (SELECT 1 FROM messages x WHERE x.thread_id=t.id))
                AND (?='' OR instr(lower(t.title),lower(?))>0 OR EXISTS
                (SELECT 1 FROM messages x WHERE x.thread_id=t.id AND instr(lower(x.body),lower(?))>0))
                ORDER BY t.updated_at DESC""", (actor["owner_id"], query, query, query))
            return [self._thread(db, row, actor["owner_id"]) for row in rows if self._profile_thread_allowed(db, actor, row["id"])]

    def create_thread(self, actor, title, participants, idempotency_key):
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise BoardError("Conversation title required (maximum 300 characters)")
        participants = sorted(_strings(participants, "participants"))
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_reply")
            profile = self._profile_for_actor(db, actor)
            if profile and _decode(profile["project_ids"]) is not None:
                raise BoardError("Scoped profiles can reply only within their selected project conversations", 403)
            participants = sorted(set(participants + [actor["owner_id"]]))
            for owner_id in participants:
                self._owner(db, owner_id)
            payload = {"title": title, "participants": participants}
            previous = self._retry(db, actor, "create_thread", idempotency_key, payload)
            if previous:
                return self._thread(db, self._member(db, actor, previous), actor["owner_id"])
            thread_id, now = _id(), time.time()
            db.execute("INSERT INTO threads VALUES(?,?,?,?)", (thread_id, title.strip(), now, now))
            db.executemany("INSERT INTO thread_members VALUES(?,?)", [(thread_id, owner_id) for owner_id in participants])
            self._remember(db, actor, "create_thread", idempotency_key, payload, thread_id)
            return self._thread(db, self._member(db, actor, thread_id), actor["owner_id"])

    def _message(self, db, row):
        author = _decode(row["author"])
        avatar = ""
        if author.get("kind") == "agent":
            agent_id = author.get("agent_id")
            if not agent_id:
                # Pre-profile messages retain their stamp; resolve only the same owner/device/session.
                session = db.execute("SELECT agent_id FROM sessions WHERE id=? AND owner_id=? AND device_id=?",
                                     (author.get("session_id"), author.get("owner_id"), author.get("device_id"))).fetchone()
                agent_id = session["agent_id"] if session else None
            profile = db.execute("SELECT json_extract(fields,'$.avatar') AS avatar FROM agent_profiles WHERE id=? AND owner_id=?",
                                 (agent_id, author.get("owner_id"))).fetchone()
            if profile:
                avatar = profile["avatar"] or ""
        attachments = []
        for attachment_id in _decode(row["attachments"]):
            attachment = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            if attachment:
                attachments.append(self._attachment(attachment))
        return {"id": row["id"], "thread_id": row["thread_id"], "body": row["body"], "author": author, "author_avatar": avatar,
                "created_at": row["created_at"], "reply_to": row["reply_to"], "mentions": _decode(row["mentions"]),
                "attachments": attachments, "mention_targets": self._mention_details(db, _decode(row["mentions"])),
                "url": self.base_url + "/?thread=" + row["thread_id"] + "&message=" + row["id"]}

    @staticmethod
    def _message_source_hash(row):
        """Hash immutable/raw source fields, excluding dynamic display metadata."""
        source = {key:row[key] for key in ('id','thread_id','body','created_at','reply_to')}
        source.update(author=_decode(row['author']),mentions=_decode(row['mentions']),
                      attachment_ids=_decode(row['attachments']))
        try:
            raw = json.dumps(source,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
        except (TypeError,ValueError,UnicodeError):
            raise BoardError('Message source unavailable',409) from None
        return hashlib.sha256(raw).hexdigest()

    def get_message(self, actor, thread_id, message_id):
        """Read one exact permitted source without pagination or read effects."""
        if any(type(value) is not str or not value or len(value) > 200
               for value in (thread_id, message_id)):
            raise BoardError("Message not found", 404)
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            self._member(db, actor, thread_id)
            row = db.execute("SELECT * FROM messages WHERE id=? AND thread_id=?",
                             (message_id, thread_id)).fetchone()
            if row is None:
                raise BoardError("Message not found", 404)
            result = {"message": self._message(db, row), "source_sha256": self._message_source_hash(row)}
            if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 256 * 1024:
                raise BoardError("Message exceeds the exact-source byte limit", 413)
            return result

    def get_thread(self, actor, thread_id, before=None, limit=50):
        limit = self._limit(limit)
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_read")
            thread = self._member(db, actor, thread_id)
            boundary = 9223372036854775807
            if before:
                row = db.execute("SELECT seq FROM messages WHERE id=? AND thread_id=?", (before, thread_id)).fetchone()
                if not row:
                    raise BoardError("Invalid message pagination marker")
                boundary = row["seq"]
            rows = list(db.execute("SELECT * FROM messages WHERE thread_id=? AND seq<? ORDER BY seq DESC LIMIT ?", (thread_id, boundary, limit + 1)))
            has_more = len(rows) > limit
            rows = rows[:limit]
            self._expire(db)
            tasks = [self._task(db, row) for row in db.execute("SELECT * FROM tasks WHERE thread_id=? ORDER BY created_at", (thread_id,))]
            return {"thread": self._thread(db, thread, actor["owner_id"]), "messages": [self._message(db, row) for row in reversed(rows)],
                    "tasks": tasks, "next_before": rows[-1]["id"] if has_more else None}

    def post_message(self, actor, thread_id, body, reply_to=None, mentions=None, attachment_ids=None, idempotency_key=""):
        if not isinstance(body, str) or len(body) > 100000:
            raise BoardError("Message exceeds 100,000 characters")
        mentions = _strings(mentions or [], "mentions")
        attachment_ids = _strings(attachment_ids or [], "attachment_ids")
        if not body.strip() and not attachment_ids:
            raise BoardError("Message or attachment required")
        payload = {"thread_id": thread_id, "body": body, "reply_to": reply_to, "mentions": mentions, "attachment_ids": attachment_ids}
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_reply")
            thread = self._member(db, actor, thread_id)
            previous = self._retry(db, actor, "post_message", idempotency_key, payload)
            if previous:
                return self._message(db, db.execute("SELECT * FROM messages WHERE id=?", (previous,)).fetchone())
            if reply_to and not db.execute("SELECT id FROM messages WHERE id=? AND thread_id=?", (reply_to, thread_id)).fetchone():
                raise BoardError("Reply must reference a message in this conversation")
            mentions = self._resolve_mentions(db, actor, thread_id, body, mentions, reply_to)
            for attachment_id in attachment_ids:
                if not db.execute("SELECT 1 FROM attachments WHERE id=? AND thread_id=?", (attachment_id, thread_id)).fetchone():
                    raise BoardError("Attachment must belong to this conversation")
            message_id, now = _id(), time.time()
            db.execute("INSERT INTO messages(id,thread_id,body,author,created_at,reply_to,mentions,attachments) VALUES(?,?,?,?,?,?,?,?)",
                       (message_id, thread_id, body, _json(self._author(db, actor)), now, reply_to, _json(mentions), _json(attachment_ids)))
            db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
            self._remember(db, actor, "post_message", idempotency_key, payload, message_id)
            # A discussion message and its ticket event commit together. The
            # retry above returns before this append, so replaying an accepted
            # post never creates a second event. Standalone threads stay out of
            # ticket activity; payloads contain references, never message text.
            ticket = db.execute("SELECT id FROM tickets WHERE thread_id=?", (thread_id,)).fetchone()
            if ticket:
                self._audit(db, actor, "ticket_message_posted", {
                    "ticket_id": ticket["id"], "thread_id": thread_id, "message_id": message_id})
            self._queue_notification(db, thread_id, "mention", "mention:" + message_id,
                                     [owner_id for owner_id in mentions if not owner_id.startswith('agent:') and owner_id != actor["owner_id"]],
                                     "Message in " + thread["title"])
            return self._message(db, db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def mark_read(self, actor, thread_id):
        with self._tx() as db:
            actor = self._human(db, actor)
            self._member(db, actor, thread_id)
            seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM messages WHERE thread_id=?", (thread_id,)).fetchone()[0]
            db.execute("INSERT INTO read_state VALUES(?,?,?) ON CONFLICT(owner_id,thread_id) DO UPDATE SET seq=excluded.seq", (actor["owner_id"], thread_id, seq))
        return {"ok": True}

    def _destination_owner(self, db, kind, destination_id):
        if kind in ("human", "group"):
            return self._owner(db, destination_id)["id"]
        table = {"device": "devices", "session": "sessions"}.get(kind)
        if not table:
            raise BoardError("Destination must be human, group, device, or session")
        row = db.execute(f"SELECT * FROM {table} WHERE id=?", (destination_id,)).fetchone()
        if not row:
            raise BoardError("Destination not found", 404)
        device_id = row["id"] if kind == "device" else row["device_id"]
        if db.execute("SELECT revoked_at FROM devices WHERE id=?", (device_id,)).fetchone()[0] is not None:
            raise BoardError("Destination device is revoked")
        return row["owner_id"]

    def create_task(self, actor, thread_id, title, instructions, requirements, destination_kind, destination_id,
                    action="", resource="", required_capabilities=None, prerequisites=None, idempotency_key=""):
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise BoardError("Task title required (maximum 300 characters)")
        if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 100000:
            raise BoardError("Task instructions required (maximum 100,000 characters)")
        if not isinstance(requirements, (str, list, dict)) or len(_json(requirements)) > 100000:
            raise BoardError("Task requirements must be text, a list, or an object")
        if not isinstance(action, str) or not isinstance(resource, str) or len(action) > 256 or len(resource) > 256:
            raise BoardError("Invalid action or resource")
        required_capabilities = _strings(required_capabilities or [], "required_capabilities")
        prerequisites = _strings(prerequisites or [], "prerequisites")
        payload = {"thread_id": thread_id, "title": title, "instructions": instructions, "requirements": requirements,
                   "destination_kind": destination_kind, "destination_id": destination_id, "action": action, "resource": resource,
                   "required_capabilities": required_capabilities, "prerequisites": prerequisites}
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            self._member(db, actor, thread_id)
            previous = self._retry(db, actor, "create_task", idempotency_key, payload)
            if previous:
                return self._task(db, db.execute("SELECT * FROM tasks WHERE id=?", (previous,)).fetchone())
            destination_owner = self._destination_owner(db, destination_kind, destination_id)
            if not db.execute("SELECT 1 FROM thread_members WHERE thread_id=? AND owner_id=?", (thread_id, destination_owner)).fetchone():
                raise BoardError("Destination owner must participate in this conversation")
            for prerequisite in prerequisites:
                if not db.execute("SELECT 1 FROM tasks WHERE id=? AND thread_id=?", (prerequisite, thread_id)).fetchone():
                    raise BoardError("Prerequisites must be existing tasks in this conversation")
            task_id, now = _id(), time.time()
            db.execute("""INSERT INTO tasks(id,thread_id,title,instructions,requirements,destination_kind,destination_id,creator,
                action,resource,required_capabilities,prerequisites,status,progress,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending','[]',?,?)""",
                       (task_id, thread_id, title.strip(), instructions, _json(requirements), destination_kind, destination_id,
                        _json(self._author(db, actor)), action, resource, _json(required_capabilities), _json(prerequisites), now, now))
            db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
            self._remember(db, actor, "create_task", idempotency_key, payload, task_id)
            self._audit(db, actor, "task_created", {"task_id": task_id})
            self._queue_notification(db, thread_id, "task_created", "task_created:" + task_id, [destination_owner], title)
            return self._task(db, db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def _task(self, db, row):
        if row["destination_kind"] in ("human", "group"):
            label = self._owner(db, row["destination_id"])["display_name"]
            if row["destination_kind"] == "group":
                label += "'s agents"
        else:
            table = "devices" if row["destination_kind"] == "device" else "sessions"
            label = db.execute(f"SELECT label FROM {table} WHERE id=?", (row["destination_id"],)).fetchone()[0]
        result = {key: row[key] for key in ("id", "thread_id", "title", "instructions", "destination_kind", "destination_id", "status",
                                          "claim_version", "lease_expires_at", "outcome", "evidence", "action", "resource", "created_at", "updated_at")}
        result.update({key: _decode(row[key]) for key in ("requirements", "progress", "required_capabilities", "prerequisites", "creator")})
        result.update(destination_label=label, claimant=_decode(row["claimant"]) if row["claimant"] else None,
                      verified=bool(row["verified"]), url=self.base_url + "/?thread=" + row["thread_id"] + "&task=" + row["id"])
        if row["destination_kind"] in ("device", "session"):
            predicate = "s.device_id=?" if row["destination_kind"] == "device" else "s.id=?"
            result["destination_online"] = bool(db.execute(
                f"SELECT 1 FROM sessions s JOIN devices d ON d.id=s.device_id WHERE {predicate} AND d.revoked_at IS NULL AND s.last_seen_at>? LIMIT 1",
                (row["destination_id"], time.time() - 300)).fetchone())
        return result

    def _expire(self, db):
        now = time.time()
        for row in list(db.execute("SELECT * FROM tasks WHERE claimant IS NOT NULL AND lease_expires_at<=? AND status!='complete'", (now,))):
            notes = _decode(row["progress"])
            notes.append({"timestamp": now, "author": {"kind": "system"}, "note": "Claim expired. Check existing external effects before recovery."})
            db.execute("UPDATE tasks SET status='recovery',claimant=NULL,lease_expires_at=NULL,progress=?,updated_at=? WHERE id=?",
                       (_json(notes), now, row["id"]))
            db.execute("DELETE FROM resource_locks WHERE task_id=?", (row["id"],))
            self._audit(db, {"kind": "system"}, "claim_expired", {"task_id": row["id"], "claim_version": row["claim_version"]})

    def _eligible(self, db, actor, row):
        kind = row["destination_kind"]
        if actor["kind"] == "human":
            return kind == "human" and row["destination_id"] == actor["owner_id"]
        if kind == "human":
            return False
        if kind == "group" and row["destination_id"] != actor["owner_id"]:
            return False
        if kind == "device" and row["destination_id"] != actor["device_id"]:
            return False
        if kind == "session" and row["destination_id"] != actor["session_id"]:
            return False
        capabilities = _decode(db.execute("SELECT capabilities FROM sessions WHERE id=?", (actor["session_id"],)).fetchone()[0])
        return set(_decode(row["required_capabilities"])).issubset(capabilities)

    def _task_authorized(self, db, actor, row):
        if actor["kind"] == "human":
            return True
        profile = self._profile_for_actor(db, actor)
        policy = self._effective_profile_policy(db, actor, profile) if profile else {}
        return policy.get("allow_requests", False) and bool(row["action"]) and row["action"] in policy["actions"] and bool(row["resource"]) and row["resource"] in policy["resources"]

    def claim_task(self, actor, task_id, recovery_evidence=""):
        if not isinstance(recovery_evidence, str) or len(recovery_evidence) > 100000:
            raise BoardError("Invalid recovery evidence")
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            self._expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise BoardError("Task not found", 404)
            self._member(db, actor, row["thread_id"])
            if not self._eligible(db, actor, row):
                raise BoardError("This task is not assigned to your human/device/session or requires additional capabilities", 403)
            if not self._task_authorized(db, actor, row):
                raise BoardError("Task action or resource is outside your owner's authorization", 403)
            if row["claimant"] or row["status"] not in ("pending", "recovery"):
                return {"claimed": False, "task": self._task(db, row)}
            for prerequisite in _decode(row["prerequisites"]):
                if db.execute("SELECT status FROM tasks WHERE id=?", (prerequisite,)).fetchone()[0] != "complete":
                    return {"claimed": False, "task": self._task(db, row), "reason": "A prerequisite is incomplete"}
            if row["status"] == "recovery" and not recovery_evidence.strip():
                raise BoardError("Recovery requires evidence that existing external effects were checked", 409)
            if row["resource"]:
                lock = db.execute("SELECT task_id FROM resource_locks WHERE resource=?", (row["resource"],)).fetchone()
                if lock:
                    return {"claimed": False, "task": self._task(db, row), "reason": "Resource is held by another task"}
                db.execute("INSERT INTO resource_locks VALUES(?,?)", (row["resource"], task_id))
            now, notes = time.time(), _decode(row["progress"])
            if recovery_evidence.strip():
                notes.append({"timestamp": now, "author": self._author(db, actor), "note": "Recovery check: " + recovery_evidence})
            db.execute("UPDATE tasks SET status='in_progress',claim_version=claim_version+1,claimant=?,lease_expires_at=?,progress=?,updated_at=? WHERE id=?",
                       (_json(actor), now + self.lease_seconds, _json(notes), now, task_id))
            self._audit(db, actor, "task_claimed", {"task_id": task_id, "recovery_evidence": recovery_evidence})
            return {"claimed": True, "task": self._task(db, db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())}

    def update_task(self, actor, task_id, claim_version, status=None, progress="", outcome="", evidence="", verified=False):
        statuses = ("needs_input", "in_progress", "ready_for_verification", "complete")
        if status is not None and status not in statuses:
            raise BoardError("Invalid task status")
        if type(claim_version) is not int or type(verified) is not bool or any(not isinstance(v, str) or len(v) > 100000 for v in (progress, outcome, evidence)):
            raise BoardError("Invalid task update")
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            self._expire(db)
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                raise BoardError("Task not found", 404)
            self._member(db, actor, row["thread_id"])
            if not self._eligible(db, actor, row) or not self._task_authorized(db, actor, row):
                raise BoardError("You are not authorized to update this task", 403)
            if row["claim_version"] != claim_version or not row["claimant"] or _decode(row["claimant"]) != actor or row["lease_expires_at"] is None or row["lease_expires_at"] <= time.time():
                raise BoardError("Claim expired, reassigned, or version is stale", 409)
            if row["status"] == "complete":
                raise BoardError("Task is already complete", 409)
            resulting_status = status or row["status"]
            resulting_outcome, resulting_evidence = outcome or row["outcome"], evidence or row["evidence"]
            if resulting_status == "complete" and (not resulting_outcome.strip() or not resulting_evidence.strip() or not verified):
                raise BoardError("Completion requires an outcome, verification evidence, and verified=true")
            if verified and (not resulting_outcome.strip() or not resulting_evidence.strip()):
                raise BoardError("Verified work requires an outcome and evidence")
            now, notes = time.time(), _decode(row["progress"])
            author = self._author(db, actor)
            if progress.strip():
                notes.append({"timestamp": now, "author": author, "note": progress})
            if outcome or evidence:
                notes.append({"timestamp": now, "author": author, "note": "Outcome: " + resulting_outcome + "\nEvidence: " + resulting_evidence})
            lease = None if resulting_status == "complete" else now + self.lease_seconds
            db.execute("UPDATE tasks SET status=?,progress=?,outcome=?,evidence=?,verified=?,lease_expires_at=?,updated_at=? WHERE id=?",
                       (resulting_status, _json(notes), resulting_outcome, resulting_evidence, int(verified or (row["verified"] and not outcome and not evidence)), lease, now, task_id))
            if resulting_status == "complete":
                db.execute("DELETE FROM resource_locks WHERE task_id=?", (task_id,))
            db.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, row["thread_id"]))
            audit_id = self._audit(db, actor, "task_updated", {"task_id": task_id, "claim_version": claim_version, "status": resulting_status,
                                                             "progress": progress, "outcome": outcome, "evidence": evidence, "verified": verified})
            if resulting_status != row["status"] and resulting_status in ("needs_input", "ready_for_verification", "complete"):
                targets = [self._destination_owner(db, row["destination_kind"], row["destination_id"])]
                if resulting_status in ("ready_for_verification", "complete"):
                    targets.append(_decode(row["creator"])["owner_id"])
                self._queue_notification(db, row["thread_id"], resulting_status,
                                         f"{resulting_status}:{task_id}:{audit_id}", targets, row["title"])
            return self._task(db, db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def _limit(self, limit):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise BoardError("Limit must be between 1 and 200")
        return limit

    def read_inbox(self, actor, cursor=None, limit=50, reader_id="", include_work=False):
        limit = self._limit(limit)
        if type(include_work) is not bool:
            raise BoardError("include_work must be a boolean")
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_read")
            reader = self._actor_key(actor)
            if actor["kind"] == "human" and reader_id:
                reader += ":" + reader_id
            previous = db.execute("SELECT seq FROM reader_state WHERE reader=?", (reader,)).fetchone()
            boundary = previous[0] if previous else 0
            if cursor:
                issued = db.execute("SELECT seq FROM cursors WHERE token_hash=? AND reader=?", (_hash(cursor), reader)).fetchone()
                if not issued:
                    raise BoardError("Cursor was not issued for this session", 400)
                boundary = issued[0]
            rows = self._addressed_messages(db, actor, boundary=boundary, limit=limit)
            next_seq = rows[-1]["seq"] if len(rows)==limit else db.execute('SELECT COALESCE(MAX(seq),0) FROM messages').fetchone()[0]
            token = secrets.token_urlsafe(32)
            db.execute("INSERT INTO cursors VALUES(?,?,?)", (_hash(token), reader, next_seq))
            db.execute("INSERT INTO reader_state VALUES(?,?) ON CONFLICT(reader) DO UPDATE SET seq=MAX(seq,excluded.seq)", (reader, next_seq))
            session = None
            if actor["kind"] == "agent":
                db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (time.time(), actor["session_id"]))
                session = self._session(db.execute("SELECT * FROM sessions WHERE id=?", (actor["session_id"],)).fetchone())
            result = {"messages": [self._message(db, row) for row in rows], "cursor": token, "session": session}
            if not include_work:
                return result
            self._expire(db)
            tasks = []
            for row in db.execute("SELECT t.* FROM tasks t JOIN thread_members m ON m.thread_id=t.thread_id WHERE m.owner_id=? AND t.status!='complete' ORDER BY t.created_at", (actor["owner_id"],)):
                if self._profile_thread_allowed(db, actor, row["thread_id"]) and self._eligible(db, actor, row):
                    task = self._task(db, row)
                    task["authorized"] = self._task_authorized(db, actor, row)
                    tasks.append(task)
            result.update(tasks=tasks, work=self.get_work(actor, limit=limit))
            return result

    def _attachment(self, row):
        return {"id": row["id"], "filename": row["filename"], "size": row["size"], "content_type": row["content_type"],
                "url": self.base_url + "/api/attachments/" + row["id"]}

    def add_attachment(self, actor, thread_id, filename, content_type, content, idempotency_key=""):
        if not isinstance(content, bytes) or len(content) > 10 * 1024 * 1024:
            raise BoardError("Attachments must be at most 10 MiB", 413)
        if not isinstance(filename, str) or not filename or len(filename) > 255:
            raise BoardError("Invalid attachment filename")
        filename = filename.replace("\\", "/").split("/")[-1]
        if not filename or filename in (".", "..") or any(ord(c) < 32 for c in filename):
            raise BoardError("Invalid attachment filename")
        if not isinstance(content_type, str) or len(content_type) > 200 or any(ord(c) < 32 for c in content_type):
            raise BoardError("Invalid content type")
        attachment_id = _id()
        path = self.data_dir / "attachments" / attachment_id
        try:
            with self._tx() as db:
                actor = self._actor(db, actor, "allow_reply")
                self._member(db, actor, thread_id)
                payload={'thread_id':thread_id,'filename':filename,'content_type':content_type,'sha256':hashlib.sha256(content).hexdigest()}
                previous=self._retry(db,actor,'add_attachment',idempotency_key,payload)
                if previous:
                    return self._attachment(db.execute('SELECT * FROM attachments WHERE id=?',(previous,)).fetchone())
                with path.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?)", (attachment_id, thread_id, filename, content_type or "application/octet-stream", len(content), time.time()))
                self._remember(db,actor,'add_attachment',idempotency_key,payload,attachment_id)
                return self._attachment(db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone())
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def get_attachment(self, actor, attachment_id):
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            row = db.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
            if not row:
                raise BoardError("Attachment not found", 404)
            self._member(db, actor, row["thread_id"])
            path = self.data_dir / "attachments" / row["id"]
            if not path.is_file():
                raise BoardError("Attachment file is missing", 404)
            return {"path": str(path), "filename": row["filename"], "content_type": row["content_type"]}
