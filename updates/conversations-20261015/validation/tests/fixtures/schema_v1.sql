-- Frozen v1 schema from the pre-projects Board, including the notification outbox.
BEGIN IMMEDIATE;
CREATE TABLE attachments(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                        filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE TABLE audit(id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL NOT NULL,
                        actor TEXT NOT NULL, event TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE cursors(token_hash TEXT PRIMARY KEY, reader TEXT NOT NULL, seq INTEGER NOT NULL);
CREATE TABLE devices(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                        label TEXT NOT NULL, capabilities TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                        created_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE human_sessions(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                        token_hash TEXT UNIQUE NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, revoked_at REAL);
CREATE TABLE idempotency(actor TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
                        payload_hash TEXT NOT NULL, record_id TEXT NOT NULL, PRIMARY KEY(actor,operation,key));
CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                        thread_id TEXT NOT NULL REFERENCES threads(id), body TEXT NOT NULL, author TEXT NOT NULL,
                        created_at REAL NOT NULL, reply_to TEXT, mentions TEXT NOT NULL, attachments TEXT NOT NULL);
CREATE TABLE notification_events(
                id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                thread_id TEXT NOT NULL, kind TEXT NOT NULL,
                target_owners TEXT NOT NULL, title TEXT NOT NULL,
                created_at REAL NOT NULL, sent_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', discord_message_id TEXT);
CREATE TABLE owners(id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                        password TEXT, policy TEXT NOT NULL);
CREATE TABLE read_state(owner_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                        seq INTEGER NOT NULL, PRIMARY KEY(owner_id,thread_id));
CREATE TABLE reader_state(reader TEXT PRIMARY KEY, seq INTEGER NOT NULL);
CREATE TABLE resource_locks(resource TEXT PRIMARY KEY, task_id TEXT UNIQUE NOT NULL REFERENCES tasks(id));
CREATE TABLE sessions(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                        device_id TEXT NOT NULL REFERENCES devices(id), label TEXT NOT NULL,
                        capabilities TEXT NOT NULL, created_at REAL NOT NULL, last_seen_at REAL);
CREATE TABLE tasks(id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id),
                        title TEXT NOT NULL, instructions TEXT NOT NULL, requirements TEXT NOT NULL,
                        destination_kind TEXT NOT NULL, destination_id TEXT NOT NULL, creator TEXT NOT NULL,
                        action TEXT NOT NULL, resource TEXT NOT NULL, required_capabilities TEXT NOT NULL,
                        prerequisites TEXT NOT NULL, status TEXT NOT NULL, claim_version INTEGER NOT NULL DEFAULT 0,
                        claimant TEXT, lease_expires_at REAL, progress TEXT NOT NULL,
                        outcome TEXT NOT NULL DEFAULT '', evidence TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE thread_members(thread_id TEXT NOT NULL REFERENCES threads(id),
                        owner_id TEXT NOT NULL REFERENCES owners(id), PRIMARY KEY(thread_id,owner_id));
CREATE TABLE threads(id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX messages_thread_seq ON messages(thread_id,seq);
CREATE INDEX tasks_thread ON tasks(thread_id);
PRAGMA user_version=1;
COMMIT;
