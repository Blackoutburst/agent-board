"""Durable alerts to one explicitly configured private Discord channel."""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time

import httpx

from board import BoardError

CONFIG = "discord-notifications.json"
NAMES = {"blackoutburst": "Blackoutburst", "thescreamer": "TheScreamer"}
KINDS = {"mention": "You were mentioned", "task_created": "New task",
         "needs_input": "A task needs input", "ready_for_verification": "Ready for verification",
         "complete": "Task completed"}


class DiscordNotifications:
    def __init__(self, board, transport=None):
        self.board = board
        self.transport = transport
        self.lock = asyncio.Lock()
        with board._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS notification_delivery_state(id INTEGER PRIMARY KEY CHECK(id=1), next_delivery_at REAL NOT NULL)")
            db.execute("INSERT OR IGNORE INTO notification_delivery_state VALUES(1,0)")

    def _cooldown(self):
        with self.board._connection() as db:
            return max(0, db.execute("SELECT next_delivery_at FROM notification_delivery_state WHERE id=1").fetchone()[0] - time.time())

    def _rate_limit(self, error, retry):
        if error == "Discord rate limited delivery":
            with self.board._tx() as db:
                db.execute("UPDATE notification_delivery_state SET next_delivery_at=? WHERE id=1", (time.time() + retry,))

    def _config(self):
        path = self.board.data_dir / CONFIG
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                return None
            if (not isinstance(value.get("bot_token"), str) or not value["bot_token"] or not str(value.get("guild_id", "")).isdigit()
                    or not str(value.get("channel_id", "")).isdigit()):
                return None
            value["guild_id"] = str(value["guild_id"])
            value["channel_id"] = str(value["channel_id"])
            if not isinstance(value.get("user_ids", {}), dict):
                return None
            value["enabled_since"] = float(value.get("enabled_since", path.stat().st_mtime))
            if not math.isfinite(value["enabled_since"]) or value["enabled_since"] < 0:
                return None
            return value
        except (OSError, ValueError, TypeError):
            return None

    def status(self, actor):
        with self.board._connection() as db:
            actor = self.board._human(db, actor)
            config = self._config()
            start = float(config.get("enabled_since", time.time())) if config else time.time()
            pending = db.execute("SELECT COUNT(*) FROM notification_events WHERE sent_at IS NULL AND created_at>=?",
                                 (start,)).fetchone()[0]
            failed = db.execute("SELECT last_error FROM notification_events WHERE last_error!='' AND created_at>=? ORDER BY created_at DESC LIMIT 1",
                                (start,)).fetchone()
        return {"enabled": bool(config), "guild_id": config["guild_id"] if config else None,
                "channel_id": config["channel_id"] if config else None,
                "channel_url": f"https://discord.com/channels/{config['guild_id']}/{config['channel_id']}" if config else None,
                "pending": pending, "last_error": failed[0] if failed else "",
                "can_test": actor["owner_id"] == "blackoutburst"}

    def _payload(self, config, event):
        owners = json.loads(event["target_owners"])
        user_ids = config.get("user_ids", {})
        allowed = [str(user_ids[owner]) for owner in owners if str(user_ids.get(owner, "")).isdigit()]
        audience = ", ".join(f"<@{user_ids[owner]}>" if str(user_ids.get(owner, "")).isdigit()
                             else NAMES.get(owner, owner) for owner in owners)
        # Titles are bounded and mentions are explicitly allowlisted; message bodies,
        # instructions, attachments and credentials never leave the board.
        title = event["title"].replace("\n", " ").replace("\r", " ")[:200]
        link = self.board.base_url + "/?thread=" + event["thread_id"]
        return {"content": f"{audience}\n{KINDS.get(event['kind'], 'Board update')}: {title}\n{link}",
                "allowed_mentions": {"parse": [], "users": allowed}}

    async def _send(self, config, payload, nonce):
        try:
            async with httpx.AsyncClient(base_url="https://discord.com/api/v10", timeout=15,
                                         transport=self.transport, follow_redirects=False, trust_env=False,
                                         headers={"Authorization": "Bot " + config["bot_token"]}) as client:
                # Discord deduplicates recent retries for the same bot and nonce.
                response = await client.post("/channels/" + config["channel_id"] + "/messages",
                                             json={**payload, "nonce": nonce[:25], "enforce_nonce": True})
        except httpx.HTTPError:
            return None, "Discord connection unavailable", 60
        if response.status_code in (200, 201):
            try:
                return str(response.json()["id"]), "", 0
            except (ValueError, KeyError):
                return None, "Discord returned an invalid delivery response", 60
        if response.status_code == 429:
            try:
                retry = min(3600, max(1, float(response.json().get("retry_after", 60))))
            except (ValueError, TypeError):
                retry = 60
            return None, "Discord rate limited delivery", retry
        if response.status_code in (401, 403, 404):
            return None, "Discord bot needs access to the configured channel", 300
        return None, "Discord delivery failed (HTTP " + str(response.status_code) + ")", 60

    async def test(self, actor):
        with self.board._connection() as db:
            actor = self.board._human(db, actor)
        if actor["owner_id"] != "blackoutburst":
            raise BoardError("Only Blackoutburst can send a channel setup test", 403)
        config = self._config()
        if not config:
            raise BoardError("Discord alerts are not configured", 409)
        if self._cooldown():
            raise BoardError("Discord rate limit cooldown; try again shortly", 429)
        message_id, error, retry = await self._send(config, {
            "content": "Blackoutburst board: Discord alerts are connected. New tasks, mentions and requests for input will link back to the board.",
            "allowed_mentions": {"parse": []}}, "test" + str(time.time_ns()))
        if error:
            self._rate_limit(error, retry)
            raise BoardError(error, 503)
        return {"delivered": True, "message_id": message_id}

    async def dispatch_once(self):
        async with self.lock:
            config = self._config()
            if not config or self._cooldown():
                return
            now = time.time()
            with self.board._connection() as db:
                events = [dict(row) for row in db.execute(
                    "SELECT * FROM notification_events WHERE sent_at IS NULL AND created_at>=? AND next_attempt_at<=? ORDER BY created_at LIMIT 20",
                    (float(config.get("enabled_since", now)), now))]
            for event in events:
                with self.board._connection() as db:
                    members = {row[0] for row in db.execute("SELECT owner_id FROM thread_members WHERE thread_id=?", (event["thread_id"],))}
                if not set(NAMES).issubset(members):
                    with self.board._tx() as db:
                        db.execute("UPDATE notification_events SET sent_at=?,last_error='Thread is no longer shared' WHERE id=?",
                                   (time.time(), event["id"]))
                    continue
                message_id, error, retry = await self._send(config, self._payload(config, event), event["id"])
                with self.board._tx() as db:
                    db.execute("UPDATE notification_events SET attempts=attempts+1,sent_at=?,discord_message_id=?,last_error=?,next_attempt_at=? WHERE id=?",
                               (time.time() if message_id else None, message_id, error,
                                time.time() + min(3600, max(retry, 30 * 2 ** min(event["attempts"], 7))) if error else 0,
                                event["id"]))
                if error:
                    self._rate_limit(error, retry)
                    # A channel or rate-limit failure affects all queued messages.
                    with self.board._tx() as db:
                        db.execute("UPDATE notification_events SET next_attempt_at=MAX(next_attempt_at,?) WHERE sent_at IS NULL",
                                   (time.time() + retry,))
                    break

    async def run(self):
        while True:
            try:
                await self.dispatch_once()
            except (OSError, ValueError, sqlite3.Error):
                # Keep malformed configuration from disrupting the board service.
                pass
            await asyncio.sleep(5)
