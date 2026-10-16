"""Confirmed, replayable delivery of addressed human messages only.

This is separate from the legacy inbox's optimistic reader_state cursor. One
pending delivery per feed stays durable until an exact checkpoint is confirmed.
"""
import json
import secrets
import time

from projects import _fail

MAX_READER_FEEDS = 8
MAX_OWNER_FEEDS = 64
MAX_SCAN_ROWS = 500
MAX_MESSAGE_BYTES = 256 * 1024


def migrate_message_feed(db):
    db.execute('''CREATE TABLE message_feeds(
        id TEXT PRIMARY KEY,
        owner_id TEXT NOT NULL REFERENCES owners(id) ON DELETE CASCADE,
        device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
        reader TEXT NOT NULL,
        opening_key TEXT NOT NULL,
        opening_payload TEXT NOT NULL,
        profile_id TEXT NOT NULL REFERENCES agent_profiles(id) ON DELETE CASCADE,
        start_at TEXT NOT NULL,
        page_limit INTEGER NOT NULL,
        initial_seq INTEGER NOT NULL,
        confirmed_seq INTEGER NOT NULL,
        checkpoint INTEGER NOT NULL DEFAULT 0,
        pending_id TEXT,
        pending_ids TEXT,
        pending_end_seq INTEGER,
        pending_high_watermark INTEGER,
        last_ack_id TEXT,
        last_ack_checkpoint INTEGER,
        created_at REAL NOT NULL,
        UNIQUE(reader,opening_key))''')
    db.execute('CREATE INDEX message_feeds_owner ON message_feeds(owner_id)')


class MessageFeedMixin:
    def _feed_actor(self, db, actor):
        actor = self._actor(db, actor, 'allow_read')
        if actor['kind'] != 'agent' or self._profile_for_actor(db, actor) is None:
            _fail('Message feeds require a registered agent session', 403)
        return actor

    def _feed_metadata(self, row):
        return {'feed_id':row['id'], 'start_at':row['start_at'], 'limit':row['page_limit'],
                'human_only':True, 'checkpoint':row['checkpoint']}

    def _feed_row(self, db, actor, feed_id):
        if not isinstance(feed_id, str) or len(feed_id) != 32 or any(c not in '0123456789abcdef' for c in feed_id):
            _fail('Message feed not found', 404)
        row = db.execute('SELECT * FROM message_feeds WHERE id=? AND reader=?',
                         (feed_id,self._actor_key(actor))).fetchone()
        profile = self._profile_for_actor(db, actor)
        if row is None or profile is None or row['profile_id'] != profile['id']:
            _fail('Message feed not found', 404)
        return row

    def _feed_pending_messages(self, db, actor, row, *, mcp_compact=False):
        # Do not expose a partial replay when any formerly permitted content is
        # withdrawn. Reconciliation must never silently checkpoint past it.
        ids = json.loads(row['pending_ids'])
        current, byte_count = [], 2
        targets = set(self._inbox_targets(db, actor))
        for identifier in ids:
            message = db.execute('SELECT x.* FROM messages x JOIN thread_members m ON m.thread_id=x.thread_id '
                                 'WHERE x.id=? AND m.owner_id=?', (identifier,actor['owner_id'])).fetchone()
            if (message is None or not self._profile_thread_allowed(db, actor, message['thread_id'])
                    or json.loads(message['author']).get('kind') != 'human'
                    or not targets.intersection(json.loads(message['mentions']))):
                _fail('Message feed delivery is no longer permitted; reconcile its access', 403)
            rendered = self._message(db, message)
            if mcp_compact:
                from mcp_views import compact
                rendered = compact(rendered)
            if mcp_compact:
                from mcp_views import size
                byte_count += size(rendered)+(1 if current else 0)
            else:
                byte_count += self._feed_message_size(rendered)+(1 if current else 0)
            if byte_count > (5500 if mcp_compact else MAX_MESSAGE_BYTES):
                _fail('Message feed delivery exceeds the byte limit; reconcile its content', 413)
            current.append(rendered)
        return current

    def _feed_message_size(self, message):
        return len(json.dumps(message,ensure_ascii=False,separators=(',',':')).encode('utf-8'))

    def open_message_feed(self, actor, idempotency_key, start_at='beginning', limit=50):
        limit = self._limit(limit)
        if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256
                or idempotency_key.strip() != idempotency_key or any(ord(c) < 32 for c in idempotency_key)):
            _fail('Supply a stable nonempty opening idempotency key')
        if start_at not in ('beginning','now'):
            _fail('start_at must be beginning or now')
        with self._tx() as db:
            actor = self._feed_actor(db, actor)
            reader = self._actor_key(actor)
            profile = self._profile_for_actor(db, actor)
            payload = json.dumps({'start_at':start_at,'limit':limit,'profile_id':profile['id']},sort_keys=True,separators=(',',':'))
            old = db.execute('SELECT * FROM message_feeds WHERE reader=? AND opening_key=?',
                             (reader,idempotency_key)).fetchone()
            if old:
                if old['opening_payload'] != payload:
                    _fail('Opening key was used with different feed bounds or scope', 409)
                return self._feed_metadata(self._feed_row(db,actor,old['id']))
            # All retained feeds count. Extra keys, sessions, profiles or
            # devices cannot bypass the owner cap.
            if (db.execute('SELECT COUNT(*) FROM message_feeds WHERE reader=?',(reader,)).fetchone()[0] >= MAX_READER_FEEDS
                    or db.execute('SELECT COUNT(*) FROM message_feeds WHERE owner_id=?',(actor['owner_id'],)).fetchone()[0] >= MAX_OWNER_FEEDS):
                _fail('Message feed limit reached; reuse an existing opening key', 429)
            now = time.time()
            initial = db.execute('SELECT COALESCE(MAX(seq),0) FROM messages').fetchone()[0] if start_at == 'now' else 0
            identifier = secrets.token_hex(16)
            db.execute('''INSERT INTO message_feeds(id,owner_id,device_id,session_id,reader,opening_key,
                opening_payload,profile_id,start_at,page_limit,initial_seq,confirmed_seq,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier,actor['owner_id'],actor['device_id'],actor['session_id'],reader,idempotency_key,
                 payload,profile['id'],start_at,limit,initial,initial,now))
            return self._feed_metadata(db.execute('SELECT * FROM message_feeds WHERE id=?',(identifier,)).fetchone())

    def read_message_feed(self, actor, feed_id, *, mcp_compact=False):
        with self._tx() as db:
            actor = self._feed_actor(db, actor)
            row = self._feed_row(db, actor, feed_id)
            if row['pending_id'] is None:
                high = db.execute('SELECT COALESCE(MAX(seq),0) FROM messages').fetchone()[0]
                targets = set(self._inbox_targets(db, actor))
                # Scan a fixed number of physical message headers, not an
                # unbounded set filtered by JSON targets/profile membership.
                candidates = db.execute('''SELECT seq,id,thread_id,author,mentions FROM messages
                    WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?''',
                    (row['confirmed_seq'],high,MAX_SCAN_ROWS))
                selected, scanned, end, byte_count = [], 0, row['confirmed_seq'], 2
                for message in candidates:
                    scanned += 1
                    permitted = (json.loads(message['author']).get('kind') == 'human'
                        and targets.intersection(json.loads(message['mentions']))
                        and db.execute('SELECT 1 FROM thread_members WHERE thread_id=? AND owner_id=?',
                                       (message['thread_id'],actor['owner_id'])).fetchone()
                        and self._profile_thread_allowed(db,actor,message['thread_id']))
                    if permitted:
                        full = db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone()
                        rendered = self._message(db,full)
                        if mcp_compact:
                            from mcp_views import compact
                            rendered = compact(rendered)
                        if mcp_compact:
                            from mcp_views import size as compact_size
                            size = compact_size(rendered)
                        else:
                            size = self._feed_message_size(rendered)
                        maximum = 5000 if mcp_compact else MAX_MESSAGE_BYTES
                        if size+2 > maximum:
                            _fail('One addressed message exceeds the feed byte limit; reconcile its content',413)
                        addition = size+(1 if selected else 0)
                        if byte_count+addition > maximum:
                            # This candidate remains after the confirmed bound.
                            break
                        selected.append(message['id']);byte_count += addition
                    end = message['seq']
                    if len(selected) == row['page_limit']: break
                else:
                    if scanned < MAX_SCAN_ROWS:
                        end = max(row['confirmed_seq'],high)
                db.execute('''UPDATE message_feeds SET pending_id=?,pending_ids=?,pending_end_seq=?,
                    pending_high_watermark=?,pending_view=? WHERE id=?''',
                    (secrets.token_hex(16),json.dumps(selected),end,high,'compact' if mcp_compact else 'full',feed_id))
                row = db.execute('SELECT * FROM message_feeds WHERE id=?',(feed_id,)).fetchone()
            messages = self._feed_pending_messages(db, actor, row,mcp_compact=mcp_compact)
            db.execute('UPDATE sessions SET last_seen_at=? WHERE id=?',(time.time(),actor['session_id']))
            return {'feed_id':feed_id, 'checkpoint':row['checkpoint'], 'delivery_id':row['pending_id'],
                    'messages':messages, 'human_only':True}

    def checkpoint_message_feed(self, actor, feed_id, delivery_id, expected_checkpoint):
        if type(expected_checkpoint) is not int or expected_checkpoint < 0:
            _fail('Supply the delivery checkpoint as a nonnegative integer')
        if not isinstance(delivery_id,str) or len(delivery_id)!=32 or any(c not in '0123456789abcdef' for c in delivery_id):
            _fail('Supply the exact delivery ID')
        with self._tx() as db:
            actor = self._feed_actor(db, actor)
            row = self._feed_row(db, actor, feed_id)
            if row['last_ack_id'] == delivery_id and row['last_ack_checkpoint'] == expected_checkpoint+1:
                return {'feed_id':feed_id,'delivery_id':delivery_id,'checkpoint':expected_checkpoint+1,'acknowledged':True}
            if row['checkpoint'] != expected_checkpoint or row['pending_id'] != delivery_id:
                _fail('Message feed checkpoint or delivery changed; reload without skipping pending delivery',409)
            self._feed_pending_messages(db,actor,row,mcp_compact=row['pending_view']=='compact')
            db.execute('''UPDATE message_feeds SET confirmed_seq=pending_end_seq,checkpoint=checkpoint+1,
                last_ack_id=pending_id,last_ack_checkpoint=checkpoint+1,pending_id=NULL,pending_ids=NULL,
                pending_end_seq=NULL,pending_high_watermark=NULL WHERE id=?''',
                (feed_id,))
            return {'feed_id':feed_id,'delivery_id':delivery_id,'checkpoint':expected_checkpoint+1,'acknowledged':True}
