# Operations and release procedure

Current source is Board 2.13.0/schema 13 with adapter 0.28.0. It combines
verifiable task blocks, the explicit fixed development-frame protocol, and an
inactive Linux input-lock lease foundation. The latter adds no Linux backend
or planner; its separate kernel observation covers synthetic UID 1000 fixtures
only. Historical adapter 0.26 native receipts are not adopted by version 0.27.
Older qualification receipts remain tied to their exact source and private pins;
they do not qualify this integrated source or authorize a deployment.

Board 2.10/schema 12 introduces compact MCP pages, authorized text chunks and
transactional assignment notices. Read [MCP-READS.md](MCP-READS.md) before updating
a client. Qualify the complete transport envelope and full-source worker/event
adapters; REST browser behavior remains unchanged. Retain a source + data backup
before migration, including pending confirmed-feed deliveries and page cursors.

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

Run the complete tests, syntax checks, synthetic browser acceptance and connector smoke tests. Validate the historical schema-2/schema-3 upgrades and the schema-10 to schema-12 upgrade, and the current schema-12 to schema-13 upgrade on restored private database copies, including row counts, foreign keys and integrity. Preserve the source revision and a matching database/attachment backup before touching production. Migration validation must never open the live data directory.

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

Version 2.1 adds schema 4 transactionally at startup. Existing conversations, projects, tickets, policies, credential hashes and attachments stay in the same data directory. Profiles inherit existing identities and bindings, preserve active claims and cursors, and pin migrated sessions to configuration version 1. No listener is enabled. Both schema-2 and schema-3 upgrades are covered by migration tests.

The pairing exchange uses `/mcp/connect/exchange`, which matches the current nginx MCP authentication exemption. It requires a single-use pairing code and never creates a browser session. No new public browser/API exception is needed.

## After release

Verify local health reports `2.13.0` and schema 13. Through the public MCP URL with an existing device credential, discover 58 tools including `get_development_frame`, `release_ticket`, `reconcile_profile_claim`, `reserve_message_execution`, `inspect_profile_refresh` and `refresh_agent_session`, register a session/identity, list the actual Board overhaul project, and read its tasks/activity. Confirm a default `read_inbox` poll returns only `messages`, `cursor` and `session`, and explicit compact `include_work=true` returns a labelled get_work pointer without a false empty task list. Enumerate actual work separately. Follow compact text locators or explicitly select full views before treating any body as source. Verify unauthorized reads still fail. Compare all tracked deployed file hashes with Git, including `profile_refresh.py`. The `queue` integration remains a finite explicit command; this release enables no permanent listener or production queue selection.

Schema 6 adds owner execution limits and runtime reservations transactionally. The initial limit is one participating turn per owner. Old clients and manual subscription usage remain outside this protocol. Validate **Agents → Runtime** with synthetic accounts before release; real runtime/model-policy tests require a separate, explicitly scoped pilot. No process is launched by editing limits, profile preferences or model metadata.

Schema 7 adds private usage observations per owner/agent/device connection. The local adapter can explicitly read `account/rateLimits/read` without inference and report only an allowlisted snapshot. The server expires it after 120 seconds, and the UI marks it stale without polling. No API credential, account ID, plan, credit/reset metadata or raw response is retained. A connection identifier does not establish subscription identity across devices; never sum buckets or infer a shared account quota. An observation cannot unpause an owner or reserve execution capacity. See [local usage commands](../integrations/codex-personal/USAGE.md).

Open the browser and test project/task navigation, dependencies, assignments, a delegated subtask, human responses and agent presence. Refresh/reconnect existing MCP clients once so their cached tool schemas include the new tools. Their old tools remain compatible until then.

A configured notification destination is not proof of delivery. Check durable outbox state and use an explicitly requested human test when validating actual Discord delivery. Do not generate routine live notifications merely to test polling.

## Rollback

Do not start any pre-2.9 code against a schema-12 database, or source supporting only schema 12 against a schema-13 database. Stop/restart the service through its authorized route, preserve the failed release's data directory, restore the matching pre-release archive into a new directory with `backup.py restore`, copy the private Discord configuration from the preserved directory, restore the source that matches that backup's schema, then promote the restored directory and restart.

Run integrity, health, attachment and authenticated MCP checks. Retain the failed-release directory so post-backup records are recoverable. Restoring a database never rolls back external work performed by agents. Inspect those effects before reclaiming anything.

## Agent recovery

A disconnected session is not proof its process stopped. Expired claims retain progress and resource ownership. Read the task, discussion and activity; inspect external state; then recover with truthful evidence. Revoke a device if its credential is suspect. Re-pairing after revocation creates a new device; normal reconnects keep the existing persistent identity.

Version 2.2 adds the reusable project tag catalog as schema 5. Existing messages, identity/profile bindings, claims, cursors and attachment files are retained. Browser updates poll every four seconds while visible; MCP observers keep their independent cursors. Before release, run `tests/browser_followups.cjs` against the isolated `tests/serve_followups_preview.py` fixture on port 18766 (with a short silent MP4 at `/tmp/board-followup-preview.mp4`). Never use its synthetic mutations against production.

### Task description attachments (2.6)

Schema 8 adds an ordered ticket/file association. Existing discussion history and unposted uploads are retained in place; migration does not infer task files from comments. Ticket responses include `attachments`. Upload through the existing authorized attachment API, then use a versioned `update_ticket` with `attachment_ids` (at most 100 files, all from the ticket thread). Omit the field to retain files; an empty list removes the association while preserving file content and discussion history. Normal claim, membership and version guards apply.

The task editor saves selected files in the Description panel without posting a discussion message. Per-file upload keys and the saved task ID allow an interrupted form submission to finish without duplicating tasks/files. The multipart browser upload endpoint accepts an optional `idempotency_key`; reusing it with different file content fails. Run the follow-ups browser suite to verify creation, edit, reload, a lost upload response, and ordinary discussion attachments. Backup/restore includes the association and attachment bytes. Schema 7 backups remain valid upgrade inputs; rollback requires restoring the matching schema 7 backup and source.

## Profile changes during old work

A changed profile does not refresh an active session in place. Register a separate reconciliation session under the same bound profile/device and its current rights; never adopt an old model operation under changed instructions. Inspect the previous checkout, external effects and process state first.

`reconcile_profile_claim` releases only an expired ticket claim from another session of the same profile and device, pinned to an older profile version. It requires the current ticket version, a stable retry key and a concrete recovery statement. Any unfinished or uncleaned participating Runtime record for that profile/ticket blocks release. When no Runtime record exists, the statement is an operator assertion for legacy/manual work, not an operating-system proof. The tool neither stops a process nor frees Runtime capacity; it preserves progress, requests, dependencies and blocked/review states. Paused/archived profiles, revoked credentials and lost project scope still require human reconciliation.

After confirmed release, reuse the same retry key if the response was uncertain. A retry returns the current task and never releases a subsequent claim. Register the old session again only once all its nonterminal claims have been reconciled. Its old model journals remain pinned; they are not automatically resumed.

### Markdown rendering (2.6.2)

The browser serves a pinned markdown-it 15.0.2 UMD bundle locally under the existing CSP. Its npm archive integrity is recorded in `web/vendor/README.md`; the MIT license is retained in the source tree and distributed bundle. The shared renderer disables raw HTML, active URL schemes and typographic rewriting. Mentions are highlighted only in rendered text outside links and code. Remote image syntax becomes a link, preserving the existing same-origin media policy. Source text is stored and edited verbatim. No database migration is required.

Run `tests/browser_markdown.cjs` against the isolated follow-ups fixture on 18766. It covers project/task/message/request surfaces, source preservation, HTML/URL rejection, external-image requests, mentions and desktop/mobile layout. Also run the existing browser suites after shared rendering changes.

### Confirmed message-feed migration

Version 2.7 adds `message_feeds` as schema 9 without changing legacy cursors or read state. Verify the three new tools and their replay/ACK protocol in [MESSAGE-FEED.md](MESSAGE-FEED.md). Preserve a matching schema-8 backup and source before release; rolling back requires both. No feed, host, model call or listener is created by migration.

### Desktop sidebar viewport (2.6.3)

The desktop header and sidebar stay within the viewport; long conversation lists scroll independently so Settings, Connect agent and Documentation remain reachable on long pages. Window scrolling remains the content-scrolling model. The mobile navigation layout is unchanged. Verify `tests/browser_sidebar.cjs` against the synthetic follow-ups fixture, including long-page start/middle/end, expanded conversations and short desktop viewports.

### Message Runtime migration (2.8)

Schema 10 transactionally rebuilds `runtime_executions` for exact addressed-message scope while retaining existing ticket rows, indexes, foreign keys and capacity. General conversations have nullable project/ticket references; no visible ticket or claim is fabricated. Preserve a matching schema-9 backup and source before release. Validate migration/rollback, both scope kinds and backup restoration with synthetic fixtures. The Runtime view links message executions to their source conversation.

The adapter 0.18 [message host](../integrations/codex-personal/MESSAGE_HOST.md) uses confirmed deliveries, exact context checks and idempotent publication. Creation remains disabled; finite activation and foreground execution are separate explicit commands. This release does not install or start a listener, model, feed or cloud agent.

### Explicit development adapter (0.18)

The [development command](../integrations/codex-personal/DEVELOPMENT_CLI.md) is a
finite explicit operation, separate from both host modes. It requires an exclusive
isolated checkout, sealed grants/context, and already qualified native plans.
Preparation launches no model/test and claims no ticket. Publication is a separate
command that records a dated audit in review, never ticket done. Version changes
invalidate native source pins and require explicit requalification of the final
package before a pilot. Installation starts no listener or development work.

### Coordinator-family migration (2.9)

Schema 11 adds `delegation_families` and `delegation_members` transactionally.
Keep the pre-release schema-10 backup and its exact source revision together.
On a restored private copy, verify that migration preserves existing claims,
messages, ticket and message Runtime rows, active capacity, foreign keys and
attachments. An interrupted migration must leave schema 10 and its existing
rows intact. Also restore a schema-12 backup containing a family, child claim
and pending Runtime record; their identities, private plan and occupied capacity
must survive. The fixtures in `tests/test_delegation_runtime_integration.py`
exercise both cases with disposable accounts. Migration creates no family or
child ticket and starts no model, test, host or listener.

The local family controller separately introduces canonical development-fence
format 3. Stop every older coordinator sharing that private anchor before
opening it with the matching new adapter. Preserve the adapter revision and its
private journals, stores and anchor as one recovery set. Format 3 atomically
reserves three roots; transfers use only exact targets already recorded in the
private family journal. Generic bind/attach cannot adopt a family reservation.
An absent active reservation or a partial set requires review, never automatic
reconstruction or release. Rollback requires matching code and private state;
neither a database restore nor an anchor restore reverses external effects.
This local fence is limited to one OS user and machine and cannot detect an
administrator rolling back all state.

Before an explicitly authorized development pilot, requalify the final adapter
package on the actual supported host with disposable synthetic fixtures:
`qualify_native_named_tests.py`, `qualify_native_supervisor.py` and
`qualify_planner_policy.py` under `integrations/codex-personal/tests`.
Use the explicitly pinned absolute Codex executable and fresh private plan
directories; do not rewrite old pins to accept a new package. These fixtures
exercise filesystem/network/process guards, bounded outcomes, controller-crash
cleanup and exact replay without model inference, provider login or account RPC.
Keep their new receipts tied to the final source revision, separate from dated
historical receipts. Darwin qualification does not qualify Linux or Windows;
unsupported backends remain refused. See [native tests](../integrations/codex-personal/NATIVE_TESTS.md)
and [supervision](../integrations/codex-personal/SUPERVISED_TESTS.md).

### Coordinated-work cards (2.9)

Family-linked task pages show the coordinator and two temporary workers with their project-visible task links, roles, declared child models and current task states. The coordinator model remains unspecified when no public model is declared. Names and titles render as text; the private plan, file bounds, sessions, device details and digests stay out of the card.

Only the exact owner can consult the private family endpoint or request a stop. Confirmation sends the revision just consulted. A 409 refreshes the current task and requires a new deliberate confirmation; no blind retry is made. Stop leaves active or uncertain Runtime reservations occupied until the executor confirms cleanup. Other project members see the public card and links without requesting the private endpoint.

Validate `tests/browser_family.cjs` in Firefox against `tests/serve_family_preview.py` on loopback port 18770, with disposable accounts only. It covers desktop/mobile layout, text escaping, exact task links, owner isolation, confirmation, retained capacity, metadata updates without a task-version change and an actual revision conflict. The fixture records synthetic Runtime receipts and launches no model or provider. Stop it after the test.
