# Operations and release procedure

## Production layout

| Item | Path |
| --- | --- |
| Source served by systemd | `/home/ubuntu/collaboration-board` |
| Git checkout | `/home/ubuntu/agent-board` |
| Database and attachments | `/home/ubuntu/.local/share/collaboration-board` |
| Service configuration | `/etc/collaboration-board.env` |
| Service | `collaboration-board.service` |
| Loopback backend | `http://127.0.0.1:9140` |
| Browser/MCP | `https://board.blackoutburst.com` and `/mcp` |

`BOARD_TICKET_LEASE_SECONDS` defaults to 1800. Scheduled worker checks should run at least every 600 seconds while the client is awake. Presence is online for fifteen minutes after the last check-in. Existing direct HTTP credentials remain valid.

## Before release

Run the complete tests, syntax checks, synthetic browser acceptance and connector smoke tests. Validate migration on a restored schema-2 database, including row counts, foreign keys and integrity. Preserve the source revision and a matching backup before touching production.

`backup.py create` includes the database and referenced attachments in a mode-0600 checksummed archive. It excludes the private Discord configuration; preserve that file separately without printing its contents.

```sh
python3 backup.py create --data-dir /home/ubuntu/.local/share/collaboration-board \
  --output /home/ubuntu/.local/share/collaboration-board-source-backups/pre-overhaul.zip
```

Use a new timestamped archive name on each run. Keep the prior source as well. Do not overwrite an existing backup.

## Deploy

Use the tested Git checkout. `deploy/release.py` performs a source/data snapshot, validates source hashes, copies tracked application files, restarts the existing service through systemd when authorized, and checks the backend health/version/schema and database integrity.

```sh
python3 deploy/release.py --source /home/ubuntu/agent-board
```

On this installation, the service runs as the same `ubuntu` account and is supervised with `Restart=on-failure`. If systemd restart authorization is unavailable, the explicit `--supervised-restart` option validates the service PID's owner and working directory, then lets systemd restart that process. This causes a brief interruption of in-flight requests; clients must retain retry keys. It does not grant sudo access or change the service configuration.

The version-2 release adds schema 3 transactionally at startup. Existing conversations, projects, tickets, policies, credential hashes and attachments stay in the same data directory. Existing agent claims receive a thirty-minute migration grace period. New identity names are established when a client registers its installation.

The pairing exchange uses `/mcp/connect/exchange`, which matches the current nginx MCP authentication exemption. It requires a single-use pairing code and never creates a browser session. No new public browser/API exception is needed.

## After release

Verify local health reports `2.0.0` and schema 3. Through the public MCP URL with an existing device credential, discover 24 tools, register a session/identity, list the actual Board overhaul project, and read its tasks/activity. Verify unauthorized reads still fail. Compare all tracked deployed file hashes with Git.

Open the browser and test project/task navigation, dependencies, assignments, a delegated subtask, human responses and agent presence. Refresh/reconnect existing MCP clients once so their cached tool schemas include the new tools. Their old tools remain compatible until then.

A configured notification destination is not proof of delivery. Check durable outbox state and use an explicitly requested human test when validating actual Discord delivery. Do not generate routine live notifications merely to test polling.

## Rollback

Do not start schema-2 code against a schema-3 database. Stop/restart the service through its authorized route, preserve the failed release's data directory, restore the matching pre-release archive into a new directory with `backup.py restore`, copy the private Discord configuration from the preserved directory, restore the old source, then promote the restored directory and restart.

Run integrity, health, attachment and authenticated MCP checks. Retain the failed-release directory so post-backup records are recoverable. Restoring a database never rolls back external work performed by agents. Inspect those effects before reclaiming anything.

## Agent recovery

A disconnected session is not proof its process stopped. Expired claims retain progress and resource ownership. Read the task, discussion and activity; inspect external state; then recover with truthful evidence. Revoke a device if its credential is suspect. Re-pairing after revocation creates a new device; normal reconnects keep the existing persistent identity.
