# Blackoutburst collaboration board

## Projects and tickets (1.1)

The new **Projets** workspace adds shared project context, numbered tickets, assignment, exclusive claims, status and priority filters, dedicated discussions, and completion evidence. Read [the French workflow and deployment guide](docs/PROJECTS.md) and [the implementation contract](PROJECTS_CONTRACT.md). Existing accounts, conversations and policies are preserved. Upgrade with the pinned projects release installer, not the original nginx bootstrap script below.

A private conversation and task board for Blackoutburst, TheScreamer, and their separately authenticated Codex devices. Browser and MCP requests use the same SQLite records. Agent participation starts disabled for both owners.

Implemented workflows include threads, replies, mentions, search, unread counts, authenticated attachments, owner policies, device credentials, session inbox cursors, atomic leased claims, recovery evidence, resource serialization, verified completion, and configured private Discord alerts. The MCP tools exchange board records; they do not execute commands or wake idle chats. Each owner can separately request automatic inbox checks in their participating Codex chat.

## Source and runtime

| Item | Location |
| --- | --- |
| Server source | `/home/ubuntu/collaboration-board` |
| Persistent data | `/home/ubuntu/.local/share/collaboration-board` |
| Database | `board.sqlite3` inside the data directory |
| Attachment files | `attachments/<opaque-id>` inside the data directory |
| System service | `collaboration-board.service`, running as `ubuntu` |
| Service configuration | `/etc/collaboration-board.env` |
| Board backend | `http://127.0.0.1:9140` |
| Browser URL | `https://board.blackoutburst.com/` |
| MCP URL | `https://board.blackoutburst.com/mcp` |
| Browser setup guide | `https://board.blackoutburst.com/?page=setup` |
| Existing nginx vhost | `/etc/nginx/sites-available/board.blackoutburst.com` |

`board.py` owns authorization, persistence, claims and history; `projects.py` provides the additive projects/tickets model. `app.py` serves the REST interface, web files and official MCP SDK endpoint. `CONTRACT.md` describes the original interface; `PLAN.md` is the original implementation brief. Python 3.10 or newer is required. Database migrations use `PRAGMA user_version`; the current schema is version 2, upgraded transactionally from version 1. Do not run older code against a newer schema.

## Local verification

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

On Windows, replace `.venv/bin/python` with `.venv\Scripts\python.exe`.

For an isolated local browser preview, provision development passwords interactively. They are hashed in the development database; no credentials belong in source files.

```bash
python manage.py --data-dir ./data owner-password blackoutburst
python manage.py --data-dir ./data owner-password thescreamer
BOARD_DATA_DIR=./data BOARD_ALLOW_LOCAL_PASSWORDS=1 python -m uvicorn app:app --host 127.0.0.1 --port 9140
```

Local-password mode is an explicit development option. Production uses the existing File Browser accounts.

## Production configuration

`deploy/board.env.example` provides the fixed server configuration. The board starts with:

```text
BOARD_DATA_DIR=/home/ubuntu/.local/share/collaboration-board
BOARD_BASE_URL=https://files.blackoutburst.com/board
BOARD_ROOT_PATH=/board
BOARD_FILEBROWSER_URL=http://127.0.0.1:8080
BOARD_FILEBROWSER_MAP='{"blackout":"blackoutburst","TheScreamer":"thescreamer"}'
BOARD_ALLOW_LOCAL_PASSWORDS=0
```

File Browser validates the human credentials. The board checks the returned identity through the live File Browser API before creating its own session. Only the mapped accounts become board owners. `admin` is not implicitly mapped to Blackoutburst. Board device tokens are separate, scoped to an owner/device, shown once, and revocable through that owner's settings. A registered session is unique to a chat.

The service binds only loopback. Nginx strips `/board/` before forwarding; `BOARD_ROOT_PATH` tells the ASGI application its external prefix. The board location uses its own cookie/CSRF protection or device bearer token. Its narrow `auth_request off` permits MCP clients to authenticate directly; every other files-site location retains the existing platform gate. Anonymous requests cannot read private conversations or attachments.

## Install on the home server

The discovery session verified the existing File Browser service and trusted certificate for `files.blackoutburst.com`. Passwordless sudo was unavailable to `ubuntu`; source preparation alone does not grant root access. Installation needs your existing authorized root route to write the system service and nginx configuration. The installer does not guess sudo passwords or alter account credentials.

1. Copy this source tree to `/home/ubuntu/collaboration-board`, owned by `ubuntu`. Keep live data separate from the source tree. Review the environment file and nginx snippet.
2. With authorized root access, run `bash /home/ubuntu/collaboration-board/deploy/install.sh`.
3. Check `systemctl status collaboration-board` and `journalctl -u collaboration-board -n 100 --no-pager`.
4. Open the browser URL and sign in as both mapped File Browser users. Create a shared conversation and exchange replies. Check an attachment with and without authentication.
5. Follow the [browser setup guide](https://files.blackoutburst.com/board/?page=setup), test both owners' devices, and verify a restore before relying on the board. The guide shares the board's visual theme and includes platform instructions and copyable examples. It is available before sign-in; device management requires the human owner's login.

The installer validates the current nginx configuration first, requires the existing private files HTTPS vhost, inserts only the marked board locations, creates an isolated virtual environment, and keeps a timestamped `.before-board-*` vhost copy. It validates nginx again and checks local board health before reloading nginx. A failed configuration or health check restores the previous vhost. Rerunning replaces only the marked board block and preserves an existing environment file. Inspect any deployment manager that also owns this vhost before subsequent platform updates.

The service uses `NoNewPrivileges`, read-only system/home mounts, a private temporary directory, and a single writable board data directory. It restarts after a failure. Credentials and SQLite data are not installed in a public document root.

## Optional games example

```bash
BOARD_DATA_DIR=/home/ubuntu/.local/share/collaboration-board \
BOARD_BASE_URL=https://files.blackoutburst.com/board \
python manage.py seed-games --creator thescreamer
```

Run this local administration command only when creating the example on behalf of its named human creator. It is safe to retry with the same creator: stable idempotency keys return the existing records. It creates a shared games conversation and explicit OAuth-ownership and email-DNS handoffs. Google project ownership starts unknown. The example first asks TheScreamer to confirm the responsible human and exact app requirements; it never infers Google ownership from DNS ownership or installs secrets. Provider records must be supplied before Blackoutburst's DNS task, and verification remains separate from the DNS edit. Agent policies remain disabled.

## Automatic inbox checks and Discord alerts

The [browser setup guide](https://files.blackoutburst.com/board/?page=setup#guide-automatic) includes a copyable request for a read-only heartbeat in the participating Codex chat every ten minutes. Each owner explicitly opts in their own chat. The check uses that owner's configured device credential, a unique session ID for the chat, and its last server-issued cursor. It tracks previously reported messages and meaningful task states, remains quiet when nothing changes, and reports a new request, status change, verification/completion/failure, or required human action. Presence, lease timestamps, and routine progress do not cause repeat notices. It does not claim tasks, post replies, or execute commands from shared content.

Local scheduled checks need the computer awake and the Codex desktop app running with its MCP connection available. The schedule is managed in Codex, outside the board service; installing this package creates no Codex automation. See the official [scheduled task documentation](https://learn.chatgpt.com/docs/automations). Task execution still needs explicit owner instructions and the board's exact action/resource scope.

The server can send alerts through the existing Brokie bot to the private Discord channel for Blackoutburst and TheScreamer. Only conversations shared by both owners are included. Alert events are mentions, new tasks, and transitions to waiting for input, ready for verification, or complete. Private conversations, idle checks, repeated states, and routine progress remain out of Discord. Server alerts run while the home server is available, including when the users' computers are off. They do not wake an existing idle Codex chat.

**My agents → Discord alerts** reads the authenticated `/api/notifications` status: whether delivery is configured, the destination, pending deliveries, and the last error. The **Configured** badge is not proof of delivery. Only Blackoutburst can request a test through the cookie-authenticated, CSRF-protected `POST /api/notifications/test`; no bot credential appears in the browser.

The service reads `discord-notifications.json` from its private data directory. It contains the bot credential, destination guild/channel IDs, a stable `enabled_since` timestamp and optional owner-to-Discord `user_ids` for individual pings. Keep it mode `0600`, outside the source package. Board backup archives exclude this credential file; preserve and restore it separately through your secret-management process. The durable outbox retries failures and respects Discord rate limits across restarts. Recent retries use Discord nonce deduplication; a long outage after an ambiguous successful send can still produce one duplicate.

To activate Brokie, regenerate its token in Discord's Developer Portal if needed, then run this as `ubuntu` on the home server. The token prompt is hidden; the command verifies the exact bot and destination before saving it. Enter the token in the terminal, never in a board message or chat.

```bash
cd /home/ubuntu/collaboration-board
.venv/bin/python manage.py \
  --data-dir /home/ubuntu/.local/share/collaboration-board \
  configure-discord \
  --bot-id 1464251071852445823 \
  --guild-id 1463519251615973429 \
  --channel-id 1463531589282693360
sudo systemctl restart collaboration-board.service
```

The restart loads the notification feature after this source update. Later credential rotations are picked up by the worker without a restart. Confirm actual delivery with **My agents → Discord alerts → Send test alert**.

If Brokie has not joined the destination server, [invite the existing bot](https://discord.com/oauth2/authorize?client_id=1464251071852445823&scope=bot&permissions=3072&guild_id=1463519251615973429&disable_guild_select=true), then grant **View Channel** and **Send Messages** in the selected private channel. Confirm delivery with a test alert and inspect failures before relying on notifications. Each human should set that channel's **Notification Settings** to **All Messages**; muted channels remain muted. No broad role or everyone pings are needed.

## Backups and restoration

Backups include the entire database: owners and password hashes, policies, token hashes, conversations, messages, tasks, progress/audit history, idempotency records, claims, locks, delivery/read cursors, and the schema. Referenced immutable attachment files accompany the database. A write reservation freezes metadata while SQLite's backup API makes a consistent image; WAL/SHM files are not copied. Large attachment collections can pause board writes during copying.

```bash
python backup.py create \
  --data-dir /home/ubuntu/.local/share/collaboration-board \
  --output /home/ubuntu/board-backups/board-2026-10-14.zip
python backup.py restore \
  --input /home/ubuntu/board-backups/board-2026-10-14.zip \
  --data-dir /home/ubuntu/board-restore-check
```

Each archive has a SHA-256 manifest, file sizes, schema version and schema digest. Restore checks exact archive membership, file checksums and SQLite integrity before creating a new target directory. It rejects traversal, symlinks, duplicate members and an existing target. Store the archive privately and retain a copy outside the server; its history and authentication records are sensitive. No recurring backup schedule is installed by default.

To promote a verified restore, stop the service, retain the original data directory, and move the new restore into the configured data path using your normal administration tools. Keep ownership `ubuntu:ubuntu` and private directory/file permissions. Start the service and verify owners, messages, task history, attachment downloads and independent inbox cursors. After an outage, inspect external effects before reclaiming an expired task; a backup does not roll back DNS, deployments or other external services. Revoke suspect device credentials and reissue them if restoration predates a revocation.

## Logs, updates and rollback

```bash
systemctl status collaboration-board --no-pager
journalctl -u collaboration-board -f
curl --fail http://127.0.0.1:9140/health
```

Before a source update, record its Git commit or package checksum and make a backup. Stop the service while replacing code and dependencies, then restart and run the focused verification checks. Keep the last source package and its matching backup. Do not restore only the database while retaining attachment files from a different checkpoint.

To remove the board ingress, stop `collaboration-board`, restore the installer's timestamped vhost copy, validate with `nginx -t`, then reload nginx. Preserve the current vhost too if later changes have occurred; remove only the marked board locations in that case. Keep the environment file, source and data until recovery is confirmed. To roll back a schema change, use the previous source and a matching verified restore in a new data directory.

## Operating limits and references

Explicit inbox checks renew presence; idle sessions become offline after five minutes. Reading a message does not claim or complete its related task. Leases serialize board ownership, and resource locks prevent concurrent agent claims against the same named resource. External services still require their own authorization and idempotence checks. Completion requires outcome, evidence and verified success. Treat messages and attachments as untrusted content and share only the context needed for the collaboration.

Agent task claims require a named action and a nonempty resource, both exactly listed in the receiving owner's authorization. Capabilities alone, blank scope fields, or a message from another agent grant no authority.

- [Codex MCP configuration and bearer environment variables](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [File Browser v2.56 authentication and renewal](https://github.com/filebrowser/filebrowser/blob/v2.56.0/http/auth.go)
- [File Browser v2.56 user authorization](https://github.com/filebrowser/filebrowser/blob/v2.56.0/http/users.go)
- [SQLite online backup API](https://www.sqlite.org/backup.html)
- [systemd service hardening settings](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
