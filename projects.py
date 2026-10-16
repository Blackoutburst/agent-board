"""Projects and tickets; metadata only, with no execution or notification side effects.

The mixin shares Board's live authentication, transaction, audit, and idempotency
helpers. Project membership is immutable in v2, so each ticket's conversation can
safely reuse the existing conversation authorization and attachment APIs.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit


STATUSES = ("todo", "in_progress", "blocked", "review", "done")
PRIORITIES = ("low", "normal", "high", "urgent")
_PROJECT_TEXT = {"name": (1, 120), "description": (0, 4000), "context": (0, 30000),
                 "repository_url": (0, 2000), "environment": (0, 4000)}
_TICKET_TEXT = {"title": (1, 200), "description": (0, 20000), "acceptance_criteria": (0, 20000),
                "outcome": (0, 20000), "evidence": (0, 20000)}


def _fail(message, status=400):
    # Import lazily: board.py owns the stable public exception type and imports
    # this mixin before its Board class is defined.
    from board import BoardError
    raise BoardError(message, status)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value, name, minimum=0, maximum=500):
    if not isinstance(value, str) or len(value) > maximum or len(value.strip()) < minimum:
        _fail(f"{name} must be text ({minimum}..{maximum} characters)")
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        _fail(f'{name} must contain valid Unicode')
    return value.strip() if minimum else value


def _version(value):
    if type(value) is not int or value < 1:
        _fail("expected_version must be a positive integer")
    return value


def _choice(value, name, choices):
    if not isinstance(value, str) or value not in choices:
        _fail(f"Invalid {name}")
    return value


def _repository(value):
    value = _text(value, "repository_url", 0, 2000)
    if not value:
        return value
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and bool(parsed.hostname) and
                 parsed.username is None and parsed.password is None and
                 not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value) and
                 "\\" not in value)
        parsed.port  # Reject malformed/out-of-range ports as well.
    except ValueError:
        valid = False
    if not valid:
        _fail("repository_url must be an HTTPS URL without credentials")
    return value


def _same_claim(claimant, actor):
    if not claimant or claimant.get("kind") != actor["kind"] or claimant.get("owner_id") != actor["owner_id"]:
        return False
    # Humans can reconnect without losing access to their own claim. Separate
    # agent chats, even of the same owner, must never share an active claim.
    return actor["kind"] == "human" or all(claimant.get(k) == actor.get(k) for k in ("device_id", "session_id"))


def migrate_projects(db):
    """Called inside Board's BEGIN IMMEDIATE, including initial v0 creation."""
    statements = (
        """CREATE TABLE projects(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
            context TEXT NOT NULL, repository_url TEXT NOT NULL, environment TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1)),
            next_ticket_number INTEGER NOT NULL DEFAULT 1 CHECK(next_ticket_number > 0),
            created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
        """CREATE TABLE project_members(
            project_id TEXT NOT NULL REFERENCES projects(id),
            owner_id TEXT NOT NULL REFERENCES owners(id), PRIMARY KEY(project_id,owner_id))""",
        "CREATE INDEX project_members_owner ON project_members(owner_id,project_id)",
        """CREATE TABLE tickets(
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            number INTEGER NOT NULL CHECK(number > 0), thread_id TEXT NOT NULL UNIQUE REFERENCES threads(id),
            title TEXT NOT NULL, description TEXT NOT NULL, acceptance_criteria TEXT NOT NULL,
            priority TEXT NOT NULL CHECK(priority IN ('low','normal','high','urgent')),
            status TEXT NOT NULL CHECK(status IN ('todo','in_progress','blocked','review','done')),
            assignee_owner_id TEXT REFERENCES owners(id), claimant TEXT,
            claim_started_version INTEGER,
            version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
            outcome TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(project_id,number),
            FOREIGN KEY(project_id,assignee_owner_id) REFERENCES project_members(project_id,owner_id),
            CHECK(status != 'done' OR (length(trim(outcome)) > 0 AND length(trim(evidence)) > 0)))""",
        "CREATE INDEX tickets_project_updated ON tickets(project_id,updated_at DESC,number DESC)",
    )
    for statement in statements:
        db.execute(statement)


class ProjectsMixin:
    def _assignment_notice(self, db, actor, ticket, agent_id, kind, version):
        if not agent_id:
            return
        # The message's unique ID is the durable dedupe key. Insert in the same
        # transaction as the version-guarded assignment, including create retry.
        identifier = hashlib.sha256(f'{ticket["id"]}:{version}:{agent_id}:{kind}'.encode()).hexdigest()[:32]
        body = (f'Task #{ticket["number"]} {kind}: {ticket["title"]}\n'
                f'{self.base_url}/?project={ticket["project_id"]}&ticket={ticket["id"]}\n'
                'Assignment is context, not execution authority. Poll get_work for current eligibility.')
        now = time.time()
        db.execute('''INSERT OR IGNORE INTO messages(id,thread_id,body,author,created_at,reply_to,mentions,attachments)
            VALUES(?,?,?,?,?,NULL,?,?)''', (identifier,ticket['thread_id'],body,_json(self._author(db,actor)),
            now,_json(['agent:'+agent_id]),'[]'))
        db.execute('UPDATE threads SET updated_at=? WHERE id=?',(now,ticket['thread_id']))

    def _project_member(self, db, actor, project_id):
        row = db.execute("""SELECT p.* FROM projects p JOIN project_members m ON m.project_id=p.id
            WHERE p.id=? AND m.owner_id=?""", (project_id, actor["owner_id"])).fetchone()
        if row is None or not self._profile_project_allowed(db, actor, project_id):
            _fail("Project not found", 404)
        return row

    def _ticket_member(self, db, actor, ticket_id):
        row = db.execute("""SELECT t.* FROM tickets t JOIN project_members m ON m.project_id=t.project_id
            WHERE t.id=? AND m.owner_id=?""", (ticket_id, actor["owner_id"])).fetchone()
        if row is None or not self._profile_project_allowed(db, actor, row["project_id"]):
            _fail("Ticket not found", 404)
        return row

    def _project(self, db, row):
        result = {name: row[name] for name in ("id", "name", "description", "context", "repository_url", "environment",
                                               "version", "created_at", "updated_at")}
        result["archived"] = bool(row["archived"])
        result["participants"] = [r[0] for r in db.execute(
            "SELECT owner_id FROM project_members WHERE project_id=? ORDER BY owner_id", (row["id"],))]
        counts = dict.fromkeys(STATUSES, 0)
        counts.update({r[0]: r[1] for r in db.execute(
            "SELECT status,COUNT(*) FROM tickets WHERE project_id=? GROUP BY status", (row["id"],))})
        counts["total"] = sum(counts.values())
        result["ticket_counts"] = counts
        result["url"] = self.base_url + "/?project=" + row["id"]
        return result

    def _ticket(self, row, db=None):
        result = {name: row[name] for name in ("id", "project_id", "number", "thread_id", "title", "description",
                    "acceptance_criteria", "priority", "status", "assignee_owner_id", "version", "outcome", "evidence",
                    "created_at", "updated_at")}
        result["claimant"] = json.loads(row["claimant"]) if row["claimant"] else None
        result["attachments"] = [self._attachment(item) for item in db.execute(
            """SELECT a.* FROM ticket_attachments ta JOIN attachments a ON a.id=ta.attachment_id
                WHERE ta.ticket_id=? ORDER BY ta.position""", (row["id"],))] if db is not None else []
        result["url"] = self.base_url + "/?project=" + row["project_id"] + "&ticket=" + row["id"]
        return result

    def _project_assignee(self, db, project_id, owner_id):
        if owner_id is None:
            return None
        if not isinstance(owner_id, str) or not owner_id or len(owner_id) > 256:
            _fail("assignee_owner_id must be a project participant or null")
        if not db.execute("SELECT 1 FROM project_members WHERE project_id=? AND owner_id=?", (project_id, owner_id)).fetchone():
            _fail("Assignee must be a project participant")
        return owner_id

    def list_projects(self, actor, query="", include_archived=False):
        query = _text(query, "query")
        if type(include_archived) is not bool:
            _fail("include_archived must be a boolean")
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            rows = db.execute("""SELECT p.* FROM projects p JOIN project_members m ON m.project_id=p.id
                WHERE m.owner_id=? AND (?=1 OR p.archived=0)
                AND (?='' OR instr(lower(p.name),lower(?))>0 OR instr(lower(p.description),lower(?))>0)
                ORDER BY p.updated_at DESC,p.id""", (actor["owner_id"], int(include_archived), query, query, query))
            return [self._project(db, row) for row in rows if self._profile_project_allowed(db, actor, row["id"])]

    def create_project(self, actor, name, participants, description="", context="", repository_url="", environment="", idempotency_key=""):
        values = {"name": name, "description": description, "context": context, "repository_url": repository_url,
                  "environment": environment}
        values = {key: _text(value, key, *_PROJECT_TEXT[key]) for key, value in values.items()}
        values["repository_url"] = _repository(values["repository_url"])
        if not isinstance(participants, list) or any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in participants):
            _fail("participants must be a list of owner IDs")
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            participants = sorted(set(participants + [actor["owner_id"]]))
            for owner_id in participants:
                self._owner(db, owner_id)
            payload = {**values, "participants": participants}
            previous = self._retry(db, actor, "create_project", idempotency_key, payload)
            if previous:
                return self._project(db, self._project_member(db, actor, previous))
            project_id, now = secrets.token_hex(16), time.time()
            db.execute("""INSERT INTO projects(id,name,description,context,repository_url,environment,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""", (project_id, values["name"], values["description"], values["context"],
                                             values["repository_url"], values["environment"], now, now))
            db.executemany("INSERT INTO project_members(project_id,owner_id) VALUES(?,?)", [(project_id, x) for x in participants])
            self._remember(db, actor, "create_project", idempotency_key, payload, project_id)
            self._audit(db, actor, "project_created", {"project_id": project_id})
            return self._project(db, self._project_member(db, actor, project_id))

    def get_project(self, actor, project_id):
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            return self._project(db, self._project_member(db, actor, project_id))

    def update_project(self, actor, project_id, expected_version, **changes):
        _version(expected_version)
        if not changes or set(changes) - (set(_PROJECT_TEXT) | {"archived", "automation_enabled"}):
            _fail("Supply only editable project fields")
        values = {}
        for key, value in changes.items():
            if key in {"archived", "automation_enabled"}:
                if type(value) is not bool:
                    _fail(f"{key} must be a boolean")
                values[key] = int(value)
            elif key == "repository_url":
                values[key] = _repository(value)
            else:
                values[key] = _text(value, key, *_PROJECT_TEXT[key])
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            row = self._project_member(db, actor, project_id)
            if "automation_enabled" in changes and actor["kind"] != "human":
                _fail("Only a human can change project automation", 403)
            if row["version"] != expected_version:
                _fail("Project changed; reload before saving", 409)
            assignments = ",".join(key + "=?" for key in values)
            db.execute(f"UPDATE projects SET {assignments},version=version+1,updated_at=? WHERE id=? AND version=?",
                       (*values.values(), time.time(), project_id, expected_version))
            self._audit(db, actor, "project_updated", {"project_id": project_id, "version": expected_version + 1, "fields": sorted(changes)})
            return self._project(db, self._project_member(db, actor, project_id))

    def list_tickets(self, actor, project_id, status="", priority="", assignee_owner_id="", query=""):
        query = _text(query, "query")
        _choice(status, "status", ("", *STATUSES))
        _choice(priority, "priority", ("", *PRIORITIES))
        _text(assignee_owner_id, "assignee_owner_id", 0, 256)
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            self._project_member(db, actor, project_id)
            rows = db.execute("""SELECT * FROM tickets WHERE project_id=?
                AND (?='' OR status=?) AND (?='' OR priority=?) AND (?='' OR assignee_owner_id=?)
                AND (?='' OR instr(lower(title),lower(?))>0 OR instr(lower(description),lower(?))>0
                     OR CAST(number AS TEXT)=?) ORDER BY updated_at DESC,number DESC""",
                (project_id, status, status, priority, priority, assignee_owner_id, assignee_owner_id, query, query, query, query))
            return [self._ticket(row, db) for row in rows]

    def create_ticket(self, actor, project_id, title, description="", acceptance_criteria="", priority="normal", assignee_owner_id=None, idempotency_key="", depends_on=None, labels=None, required_skills=None, resource="", assignee_agent_id=None):
        values = {"title": title, "description": description, "acceptance_criteria": acceptance_criteria}
        values = {key: _text(value, key, *_TICKET_TEXT[key]) for key, value in values.items()}
        _choice(priority, "priority", PRIORITIES)
        coordination = self._coordination_values({"labels": labels or [], "required_skills": required_skills or [], "resource": resource})
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            project = self._project_member(db, actor, project_id)
            self._project_assignee(db, project_id, assignee_owner_id)
            self._project_agent(db, project_id, assignee_agent_id)
            self._check_assignment(db, assignee_owner_id, assignee_agent_id)
            payload = {**values, "project_id": project_id, "priority": priority, "assignee_owner_id": assignee_owner_id, **coordination, "depends_on": depends_on or [], "assignee_agent_id": assignee_agent_id}
            previous = self._retry(db, actor, "create_ticket", idempotency_key, payload)
            if previous:
                return self._ticket(self._ticket_member(db, actor, previous), db)
            if project["archived"]:
                _fail("Project is archived; restore it before creating tickets", 409)
            ticket_id, thread_id, now = secrets.token_hex(16), secrets.token_hex(16), time.time()
            number = project["next_ticket_number"]
            db.execute("INSERT INTO threads(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                       (thread_id, self._ticket_thread_title(project["name"], number, values["title"]), now, now))
            db.execute("""INSERT INTO thread_members(thread_id,owner_id)
                SELECT ?,owner_id FROM project_members WHERE project_id=?""", (thread_id, project_id))
            db.execute("""INSERT INTO tickets(id,project_id,number,thread_id,title,description,acceptance_criteria,
                priority,status,assignee_owner_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ticket_id, project_id, number, thread_id, values["title"], values["description"], values["acceptance_criteria"],
                 priority, "todo", assignee_owner_id, now, now))
            db.execute("UPDATE tickets SET labels=?,required_skills=?,resource=?,assignee_agent_id=? WHERE id=?",
                       (coordination["labels"], coordination["required_skills"], coordination["resource"], assignee_agent_id, ticket_id))
            self._set_dependencies(db, ticket_id, project_id, depends_on or [])
            self._remember_labels(db, project_id, labels or [])
            db.execute("UPDATE projects SET next_ticket_number=next_ticket_number+1,updated_at=? WHERE id=?", (now, project_id))
            self._remember(db, actor, "create_ticket", idempotency_key, payload, ticket_id)
            self._audit(db, actor, "ticket_created", {"project_id": project_id, "ticket_id": ticket_id, "number": number})
            self._assignment_notice(db,actor,self._ticket_member(db,actor,ticket_id),assignee_agent_id,'assigned',1)
            return self._ticket(self._ticket_member(db, actor, ticket_id), db)

    def get_ticket(self, actor, ticket_id):
        with self._connection() as db:
            actor = self._actor(db, actor, "allow_read")
            row = self._ticket_member(db, actor, ticket_id)
            return {"ticket": self._ticket(row, db), "project": self._project(db, self._project_member(db, actor, row["project_id"]))}

    @staticmethod
    def _ticket_thread_title(project_name, number, title):
        return f"[{project_name} #{number}] {title}"[:300]

    def update_ticket(self, actor, ticket_id, expected_version, **changes):
        _version(expected_version)
        allowed = set(_TICKET_TEXT) | {"priority", "assignee_owner_id", "status", "release_claim", "depends_on", "labels", "required_skills", "resource", "progress", "assignee_agent_id", "attachment_ids"}
        if not changes or set(changes) - allowed:
            _fail("Supply only editable ticket fields")
        values = {key: _text(value, key, *_TICKET_TEXT[key]) for key, value in changes.items() if key in _TICKET_TEXT}
        for key, choices in (("priority", PRIORITIES), ("status", STATUSES)):
            if key in changes:
                values[key] = _choice(changes[key], key, choices)
        values.update(self._coordination_values(changes))
        release = changes.get("release_claim", False)
        if type(release) is not bool:
            _fail("release_claim must be a boolean")
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            row = self._ticket_member(db, actor, ticket_id)
            claimant = json.loads(row["claimant"]) if row["claimant"] else None
            if actor["kind"] == "agent" and not _same_claim(claimant, actor):
                _fail("Claim this ticket before updating it", 403)
            if row["version"] != expected_version:
                _fail("Ticket changed; reload before saving", 409)
            if actor["kind"] == "agent" and row["status"] != "done" and (row["lease_expires_at"] or 0) <= time.time():
                _fail("Claim expired; inspect and recover before updating", 409)
            if "attachment_ids" in changes:
                self._set_ticket_attachments(db, row, changes["attachment_ids"])
            if "depends_on" in changes:
                self._set_dependencies(db, ticket_id, row["project_id"], changes["depends_on"])
            if "labels" in changes:
                self._remember_labels(db, row["project_id"], changes["labels"])
            if "assignee_owner_id" in changes:
                assigned = self._project_assignee(db, row["project_id"], changes["assignee_owner_id"])
                if assigned != row["assignee_owner_id"]:
                    if actor["kind"] != "human":
                        _fail("Only a human member can reassign a ticket; release your claim instead", 403)
                    values["claimant"] = None
                    values["claim_started_version"] = None
                    values["lease_expires_at"] = None
                values["assignee_owner_id"] = assigned
            if "assignee_agent_id" in changes:
                # Editing a task must preserve an existing assignment even if
                # its profile was paused, archived or lost this project scope.
                # New assignments still require an active, eligible profile;
                # normal claim checks independently prevent resumed execution.
                assigned_agent = changes["assignee_agent_id"]
                if assigned_agent != row["assignee_agent_id"]:
                    assigned_agent = self._project_agent(db, row["project_id"], assigned_agent)
                if assigned_agent != row["assignee_agent_id"]:
                    if actor["kind"] != "human":
                        _fail("Only a human can assign a specific agent identity", 403)
                    values.update(claimant=None, claim_started_version=None, lease_expires_at=None)
                values["assignee_agent_id"] = assigned_agent
            self._check_assignment(db, values.get("assignee_owner_id", row["assignee_owner_id"]),
                                   values.get("assignee_agent_id", row["assignee_agent_id"]))
            if release:
                # Human members can recover an abandoned claim explicitly. The
                # authorization above restricts agents to releasing their own.
                values["claimant"] = None
                values["claim_started_version"] = None
                values["lease_expires_at"] = None
                if row["status"] == "in_progress":
                    values.setdefault("status", "todo")
            status = values.get("status", row["status"])
            if row["status"] == "done" and status != "done":
                values.setdefault("outcome", "")
                values.setdefault("evidence", "")
                values.update(claimant=None, claim_started_version=None, lease_expires_at=None)
            if status == "done" and (not values.get("outcome", row["outcome"]).strip() or not values.get("evidence", row["evidence"]).strip()):
                _fail("A completed ticket requires an outcome and verification evidence")
            if actor["kind"] == "agent" and self._project_member(db, actor, row["project_id"])["archived"] and not release:
                _fail("Project is archived; release the claim instead", 409)
            if values.get("resource") and row["claimant"] and not release and status != "done":
                if db.execute("SELECT 1 FROM tickets WHERE resource=? AND id!=? AND claimant IS NOT NULL AND status!='done'",
                              (values["resource"], ticket_id)).fetchone():
                    _fail("This work resource is already reserved by another task", 409)
            detail = self._ticket(self._ticket_member(db, actor, ticket_id), db)
            if status in {"in_progress", "review", "done"} and (detail["blocked_by"] or detail["waiting_on_human"]):
                _fail("Resolve prerequisite tasks and human requests first", 409)
            if status == "done":
                values["lease_expires_at"] = None
            elif actor["kind"] == "agent" and not release:
                values["lease_expires_at"] = time.time() + self.ticket_lease_seconds
            now = time.time()
            # Even a no-op explicit release updates the optimistic version. No
            # caller may overwrite a newer state with a stale form.
            assignments = ",".join(key + "=?" for key in values)
            if assignments:
                assignments += ","
            db.execute(f"UPDATE tickets SET {assignments}version=version+1,updated_at=? WHERE id=? AND version=?",
                       (*values.values(), now, ticket_id, expected_version))
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, row["project_id"]))
            if "title" in values:
                project = self._project_member(db, actor, row["project_id"])
                db.execute("UPDATE threads SET title=?,updated_at=? WHERE id=?",
                           (self._ticket_thread_title(project["name"], row["number"], values["title"]), now, row["thread_id"]))
            self._audit(db, actor, "ticket_updated", {"ticket_id": ticket_id, "version": expected_version + 1, "fields": sorted(changes)})
            if 'assignee_agent_id' in values and values['assignee_agent_id'] != row['assignee_agent_id']:
                self._assignment_notice(db,actor,row,row['assignee_agent_id'],'unassigned',expected_version+1)
                self._assignment_notice(db,actor,row,values['assignee_agent_id'],'assigned',expected_version+1)
            if status == "done":
                self._unblock_dependents(db, actor, ticket_id)
            return self._ticket(self._ticket_member(db, actor, ticket_id), db)

    def _set_ticket_attachments(self, db, ticket, attachment_ids):
        if (not isinstance(attachment_ids, list) or len(attachment_ids) > 100 or
                any(not isinstance(item, str) or not item.strip() or len(item) > 200 for item in attachment_ids)):
            _fail("attachment_ids must be a list of at most 100 file IDs")
        ids = list(dict.fromkeys(attachment_ids))
        for attachment_id in ids:
            if not db.execute("SELECT 1 FROM attachments WHERE id=? AND thread_id=?",
                              (attachment_id, ticket["thread_id"])).fetchone():
                _fail("Each task attachment must belong to this task's thread")
        db.execute("DELETE FROM ticket_attachments WHERE ticket_id=?", (ticket["id"],))
        db.executemany("INSERT INTO ticket_attachments(ticket_id,attachment_id,position) VALUES(?,?,?)",
                       [(ticket["id"], attachment_id, index) for index, attachment_id in enumerate(ids)])

    def claim_ticket(self, actor, ticket_id, expected_version):
        _version(expected_version)
        with self._tx() as db:
            actor = self._actor(db, actor, "allow_requests")
            row = self._ticket_member(db, actor, ticket_id)
            claimant = json.loads(row["claimant"]) if row["claimant"] else None
            if row["status"] == "done":
                _fail("Reopen the completed ticket before claiming it", 409)
            if _same_claim(claimant, actor):
                # A lost successful response can be retried with its original
                # version only while no subsequent mutation has occurred.
                if expected_version == row["version"] or (
                    row["version"] == row["claim_started_version"] and expected_version == row["version"] - 1
                ):
                    return self._ticket(row, db)
                _fail("Ticket changed; reload before claiming", 409)
            if row["version"] != expected_version:
                _fail("Ticket changed; reload before claiming", 409)
            if claimant:
                _fail("Ticket is already claimed; its claimant or a human member must release it first", 409)
            if row["assignee_owner_id"] not in (None, actor["owner_id"]):
                _fail("Ticket is assigned to another owner", 403)
            now = time.time()
            db.execute("""UPDATE tickets SET claimant=?,claim_started_version=version+1,assignee_owner_id=?,
                status='in_progress',version=version+1,updated_at=? WHERE id=? AND version=?""",
                (_json(self._author(db, actor)), actor["owner_id"], now, ticket_id, expected_version))
            db.execute("UPDATE projects SET updated_at=? WHERE id=?", (now, row["project_id"]))
            self._audit(db, actor, "ticket_claimed", {"ticket_id": ticket_id, "version": expected_version + 1})
            return self._ticket(self._ticket_member(db, actor, ticket_id), db)
