# Operations and release procedure

Current source is Board 2.17.4/schema 17 with adapter 0.45.3. Standalone Development
authorization uses the bounded `get_development_ticket_frame` read; the native
1.5-second control window and SOURCE checks are unchanged. See
[DEVELOPMENT-TICKET-FRAME.md](DEVELOPMENT-TICKET-FRAME.md) and the
[stopped pool upgrade](../deploy/worker_pool/README.md#lecture-de-lautorisation-et-mise-à-jour-arrêtée).

## Explicit development continuation: adapter 0.45.2

The complete opt-in source joins private source checkpoints, normal retirement
and release of the old generation, fresh preparation/admission, final tested
publication from the original public base, and the real lifecycle of a distinct
reviewer. A completed local development cycle does not mark the ticket done or
approve or merge the PR. Historical uncertain work, requests, sessions, counters
and Stop records are retained; no old pool is reopened.

`continuation-plan` reads the three explicit operator documents without a
registration, qualification or model. After the public bundle is installed and
the finite unit is stopped and disabled, `install-continuation` adds a separate
service and timer, both disabled. Use `-I -B` for all standalone manager/entry
commands, reverify the exact public manager and `pool_package.py` beforehand,
and follow the [installation procedure](../deploy/worker_pool/README.md).
Never precreate the private `continuations` directory or adopt a partial install.
Only a separate human `systemctl enable --now` of the exact timer activates
passes and boot startup. It selects the configured tasks, not an unbounded queue.

A clean provider pause waits for backoff and fresh structural availability; it
neither invents a quota reset nor clears a human owner pause. An unsafe effect,
missing permission or unknown cleanup remains `needs_you`. With the actual
Worker/Core still open, the visibility helper can ask the owner a fixed question
only under a current own claim, fresh source/fence checks and positively closed
Runtime. Otherwise it may post the existing closed failure notice with reply
permission, or retain a local pending result. Stop stays silent. A human reply
does not reopen the Core, rewrite evidence or authorize execution.

As of 29 October 2026, the finite **0.44.2** pilot completed both coding and
independent review with confirmed cleanup; its PR #74 was merged at
`7012bbc02e096d5fa9cfde9546518a68c341a061`. This does not validate 0.45 on the
server. The first 0.45.0 continuation pilot #62 completed three model turns and
confirmed its read and patch, then stopped at the native test result boundary
with `linux_result_boundary_invalid`. Its six Runtime receipts were then
confirmed by the normal identity-only cleanup path. The historical Native
receipt records `linux_authorization_expired` before SOURCE, without a source
release intent. That does not validate the test; the generation, patch and
Core `needs_you` state remain retained. The human stopped the timer and service.
The full two-generation publication and distinct-review path remains unvalidated. Component tests use actual temporary
Board/Git/Runtime with synthetic provider, GitHub and native qualification seams.

Adapter 0.45.1 separates cheap local logical Stop/identity/deadline checks from
full continuation observations at action boundaries. A native heartbeat keeps
the existing concrete authorization, SOURCE captures and 1.5-second window;
complete logical checks no longer multiply inside a supposedly local poll. A
reproduction with three 0.8-second scope callbacks exceeded the old window and
produced the observed refusal pattern. This is a sufficient reproducer, not a
measurement or exclusive diagnosis of server latency. The component changes have independent reviews and synthetic regression
coverage. A fresh selected server pilot still has to exercise the composed
release on the installed unit.

The subsequent 0.45.1 pilot reached a first checkpoint and a second generation
whose Worker is `result_ready` and Planner is `review_ready`. Its Core stopped
in `needs_you` during finalization. The human diagnostic confirmed twelve
finished local Runtime records with cleanup receipts; the normal Lead release
then passed the server Runtime gate. The stopped historical task is retained.
A synthetic counter reproduces this shape when the last generation only reads,
tests and finalizes: its private parent delta is empty although the aggregate
public delta is not. Adapter 0.45.2 handles that private source case while
keeping ordinary public capture and the final nonempty publication checks.
No historical failed Core is reopened by this update. Real composed publication
and independent-review acceptance remain pending.

Adapter 0.39 can post a closed failure notice to the selected ticket discussion
from the code worker's existing initialized session. The message requires its
current reply permission; denied or unavailable notification preserves the
original run result, claim and cleanup state. A durable intent and stable key
prevent duplicate messages after a lost acknowledgement. This adds no model
retry, Runtime repair or new service, and does not resolve the failed owner
workflow's cause. See [failure notices](../integrations/codex-personal/docs/FAILURE-NOTICES.md).

Adapter 0.38 adds closed, in-memory backend qualification diagnostics. A
refusal reports the current synthetic case, native checks and cleanup flags,
and the first controller refusal without exception text, commands or paths.
The preparation callback also retains its most recent result and duration,
including a successful collection that exceeds the existing control window.
The refusal decision, exit status, private receipt schemas, 1.5-second window
and SOURCE boundary checks stay unchanged. See
[backend qualification diagnostics](../integrations/codex-personal/docs/BACKEND-QUALIFICATION-DIAGNOSTICS.md).

Adapter 0.37 permits `AF_NETLINK` in the outer pool unit so Codex 0.163's
bubblewrap helper can configure loopback in its new network namespace. Its
previous `RestrictAddressFamilies` list blocked that setup before the canary
could run. The stopped upgrader accepts only the exact shipped 0.35/0.36 to
0.37.0/0.37.1 template change; tools, owner, endpoint, budgets and all other unit
controls remain pinned. Parent processes gain access to this socket family;
no host network administration capability is added. The planner's inner
network restrictions and the native test backend's socket denial remain in
force. See [sandbox Netlink setup](../integrations/codex-personal/docs/SANDBOX-NETLINK-SETUP.md).

Adapter 0.37.1 normalizes the CLI string paths before reading the two unit
templates. The regression tests exercise `main` with real argument strings,
temporary releases and receipts, and simulated root/systemd metadata; successful
upgrades remain stopped and disabled. A template mismatch still refuses before
publishing a release or writing an upgrade intent.

Adapter 0.35 avoids repeated content reads of protected public binaries and
runtime files in the parent controller. Its process-local held-descriptor pins
require a complete first hash and a non-root, no-new-privileges controller in
the initial user namespace, with no active, permitted, inheritable or ambient
capabilities. Every use rechecks the controller and root-owned, non-writable
file ancestry and metadata; drift refuses instead of adopting a new baseline.
Private and mutable files still use full reads, and the fresh supervisor
disables this cache before loading its job and applying its descriptor limit.
See [protected public pins](../integrations/codex-personal/docs/PROTECTED-PUBLIC-PINS.md).
This adapter-only change does not require a Board database migration or restart.

Adapter 0.36 adds a closed diagnostic line after the existing refusal summary
when Linux planner qualification fails. It exposes only the fixed five canary
booleans (or no checks if none were decoded), confirmed cleanup, a fixed phase,
and an optional bounded controller errno. The entry revalidates the exact error
type and payload; messages, paths, command output, fingerprints and owner
context are excluded. Persisted receipts, refusal decisions and exit status
are unchanged. See [planner refusal diagnostics](../integrations/codex-personal/docs/PLANNER-REFUSAL-DIAGNOSTICS.md).

Board 2.17.2 added explicitly human-delegated Management / Lead profiles and five
bounded MCP tools for ordinary profile management. Schema 17 is unchanged.
No existing agent receives this authority automatically. Follow the human-only
[activation and revocation procedure](DELEGATED-AGENT-MANAGEMENT.md) before using
a new Lead identity; creating a profile never starts or binds an executor.

Since Board 2.17.1, Needs you is simplified on the inbox and task page: short prompts,
explicit choices or a custom reply, copyable commands, and expandable context.
The browser form and MCP handoff tools accept the same optional presentation
fields. Schema 17 is unchanged; existing requests and retry identities remain
valid. This release updates Board source only and does not update or activate
any separately installed personal owner adapter.

An explicit pretransport admission seal is available as a separate metadata-only API; it is not wired into normal workers and does not resolve private journals. See [MESSAGE-ADMISSION-SEAL.md](MESSAGE-ADMISSION-SEAL.md). Confirmed capacity refusals are now durable non-admission records, distinct from executions and cleanup. Code rollback must keep schema17, both request barriers, append proofs/statuses and every refusal tombstone; restoring an earlier database is not a supported rollback. See [RUNTIME-ADMISSION-REFUSALS.md](RUNTIME-ADMISSION-REFUSALS.md). Existing uncertain attempts are not converted; protected owner releases and prepared catalogs require a separate reviewed transition before upgrade. Message managers report ready only after their feed is confirmed. Local append-only staging is inactive preparation and performs no login or provisioning. The browser shows owner Runtime capacity in Agents without delaying Stop/Restart or changing admission. It performs no cleanup or pending-dispatch recovery. It adds explicit owner/device server controls and the managed owner process. Version 2.14.1 also provides human-only profile/provider preparation and visible initial host identifiers; it performs no login or provisioning. Read [SERVER-AGENT-CONTROLS.md](SERVER-AGENT-CONTROLS.md) before installing or enabling that separate service. It preserves
verifiable task blocks, the explicit fixed development-frame protocol, and an
inactive Linux input-lock lease foundation. The latter adds no Linux backend
or planner; its separate kernel observation covers synthetic UID 1000 fixtures
only. Historical native receipts are not adopted by adapter 0.30.
Older qualification receipts remain tied to their exact source and private pins;
they do not qualify this integrated source or authorize a deployment.

Board 2.17.0/schema17 and adapter 0.31.0 add the finite
[inactive message-target append](SERVER-MESSAGES-APPEND.md). The existing personal
slot, compatible protected source and explicit C0 source catalogue transition
must already be prepared. A separate human B grant is required; an A metadata
draft cannot authorize it. A pending B holds positive commands while actual Stop
delivery and cleanup remain available. Installation performs no upgrade, login
or activation of an owner service. This reliability path does not deliver the
hierarchical cloud team or parallel development task execution.

## Finite cloud workflow: adapter 0.33.0

The [cloud workflow](../integrations/codex-personal/CLOUD-WORKFLOW.md) now
composes one to four explicitly selected coding jobs, sealed tested commits,
draft PRs, author handoffs and independent reviewer opinions visible on the
same tickets. Publication keeps each ticket in `review`; it does not mark it
done or merge its PR. A completed workflow may contain a reviewer requesting
corrections.

| Entry and schema | Result |
| --- | --- |
| Historical `run-batch`, `linux-finite-pool-batch-v1` | Exactly four private coding results |
| `run-workflow`, `linux-finite-pool-batch-v2` | One to four selected coders, draft PR and visible independent review per ticket |

Version-two batches also work with `run-batch`; that entry retains private
results and does not run the publication/reviewer phases. `run-workflow`
requires both private operator files, `config/batch.json` and
`config/workflow.json`, with the same batch UUID. Existing paths, fixed named
Python verifier, repository/base, profiles, paired connections, models and
budgets are explicit operator selections. No arbitrary shell or model-created
new files are added.

As of 23 October 2026, the new pool unit is **installed, stopped and boot-disabled**.
The [separate addon installer](../deploy/worker_pool/README.md) prepares a
disabled, stopped unit; source publication or Board deployment does not start
it or upgrade existing owner hosts. The `workflow-plan` action inspects the
selected metadata without registration, qualification or model execution.
Owner pairing and normal official Codex/gh authentication, exact-source
backend/planner qualification in the final unit, and an explicit
start remain separate operator steps.

The integration tests exercise real temporary Board, Git, Pool, journals and
Runtime with synthetic model/GitHub transports and Linux qualification
observations. They do not validate real models on this new server path, four
simultaneous real coders, or both owners' installations. Keep historical native
receipts tied to their original source and environment; see
[CLOUD-WORKERS.md](../integrations/codex-personal/CLOUD-WORKERS.md).

The [manager and writer adapters](../integrations/codex-personal/TEAM-ROLES.md)
produce private bounded plans and historical summaries. The general hierarchy
and assignment controller remain ticket #52 work. An interrupted finite batch
keeps its intentions and returns history without replaying claims, models or
publication. Stop reaches the actual publication and reviewer phases, and
uncertain cleanup remains unresolved rather than being treated as completion.


Board 2.16.3 keeps Stop available when a budget-limited instance has stopped
cleanly while its requested state is still running. A fresh human Stop can then
settle that requested state. An already confirmed requested Stop and a pending
Stop still disable duplicate actions. API permissions, command CAS, budgets and
cleanup rules are unchanged; the button never submits a command while rendering.
This Board/UI release does not upgrade the separately installed owner adapter.

Adapter 0.30.6 adds only the pure launch-origin consistency foundation described
in [MESSAGE_LAUNCH_BINDINGS.md](../integrations/codex-personal/MESSAGE_LAUNCH_BINDINGS.md).
Its detached data checks preserve old launch hashes when a new target is added
structurally; they neither authenticate their inputs nor create launch authority.
No runtime caller, journal migration, Board operation, pairing or login is wired
to these functions. Board/schema/API and Host4/Message3 remain unchanged. Existing
protected owner releases remain separately pinned; publication does not upgrade
or activate them. This historical foundation did not apply B; adapter 0.31.0 adds the separate fenced worker path described above.

Adapter 0.30.5 closes the recovery dependency between an already queued human
Stop and an executed-message disposition. The disposition pins the exact bounded
Stop commands and permitted Stop effects without applying or acknowledging them.
Only a v2 source intent may precede their completion; its finite controller then
settles the actual Stop before catalogue replacement. Start/Restart, review/error
reports, changed fences and the older recovery path remain refused. This is an
adapter-only source change: Board 2.16.2, schema16 and the 64-tool API are unchanged.
A new protected owner release still requires exact-source qualification and an
explicit operator action; a prepared older bundle must not be used for this fix.

Adapter 0.30.4 adds an explicit, owner-only disposition of one already executed
message blocked by the text protocol. It requires the original transport cleanup
proof and two authenticated read-only observations of the exact finished Runtime.
It preserves the failed result, PID, provider session, review flag and consumed
budget. No reply, feed acknowledgement, new execution, Runtime mutation or provider
session continuation is performed. Its durable Host4 intent and Message3 terminal
make older adapters refuse these journals; never restore or downgrade them. A
pending disposition blocks work and catalogue transitions. Before a later source
upgrade, the exhaustive child verifier checks both this disposition and the prior
pretransport seal as separate histories. Installation still requires an explicit
protected source and catalogue transition with both owner services stopped and
disabled. A successful source deployment does not activate either owner.

Adapter 0.30.3 recognizes the same bounded completed diagnostic items in
personal text turns and subscription probes. These notices may precede the
session; they do not establish a session, a valid response or successful
completion. Fatal errors, unknown/tool events, invalid ordering, exit and
cleanup checks still apply. No private diagnostic text is published. Existing
failed turns and budgets are preserved: upgrading does not retry or reclassify
them. A transport that already started cannot use the separate pretransport
admission seal; its failed message needs an explicit reviewed disposition.

Schema 16 adds only human preparation drafts and their durable Start/Restart
barrier. It does not install a profile, consume a manager request, pair a device,
log in to a provider or start a model. A draft remains until explicit human
cancellation; silence, expiry and rollback are not cancellation. Keep schema16
and all barriers/refusals for code rollback. Complete any explicit owner
catalogue recovery before enabling this draft workflow, because that recovery
requires current Start authority while a draft intentionally suspends it.

Adapter 0.30.2 includes a separate finite owner entry for explicitly abandoning
one exact legacy pretransport message attempt. Follow
[MESSAGE-RECOVERY.md](../deploy/isolated_host/MESSAGE-RECOVERY.md) after the
protected source-only update; keep both owner services stopped and disabled.
The compatible local journals use Host3, Runtime4 and Message2. Do not restore
old journals or start an older adapter after this transition. The Board's new
`replace_server_catalog` API is a separate fenced operation following real human
Stops; [SERVER-CATALOG-REPLACEMENT.md](SERVER-CATALOG-REPLACEMENT.md) defines it.
Neither a Board deployment nor a historical receipt grants a new activation,
refunds budget or proves an unrelated Runtime clean.

Board 2.10/schema 12 introduces compact MCP pages, authorized text chunks and
transactional assignment notices. Read [MCP-READS.md](MCP-READS.md) before updating
a client. Qualify the complete transport envelope and full-source worker/event
adapters; REST browser behavior remains unchanged. Retain a source + data backup
before migration, including pending confirmed-feed deliveries and page cursors.

## Server layout

This section describes a shared server deployment. A personal board needs none of it: see the README quickstart. A shared board must set `BOARD_REQUIRE_LOGIN=1` and `BOARD_OWNERS`; with login disabled the board refuses to start on a non-loopback `BOARD_BASE_URL`.

| Item | Path |
| --- | --- |
| Source served by systemd | `/home/ubuntu/collaboration-board` |
| Git checkout | `/home/ubuntu/agent-board` |
| Database and attachments | `/home/ubuntu/.local/share/collaboration-board` |
| Service configuration | `/etc/collaboration-board.env` |
| Service | `collaboration-board.service` |
| Loopback backend | `http://127.0.0.1:9140` |
| Browser/MCP | your public board URL, for example `https://board.example.com` and `/mcp` |

`BOARD_TICKET_LEASE_SECONDS` defaults to 1800. Scheduled worker checks should run at least every 600 seconds while the client is awake. Presence is online for fifteen minutes after the last check-in. Existing direct HTTP credentials remain valid.

## Before release

Run the checks appropriate to the changed paths, syntax checks and connector smoke tests; include browser acceptance when browser behavior changes. Validate schema changes on representative synthetic migration fixtures, including row counts, foreign keys and integrity. A restored private-data validation copy requires explicit authorization and must remain on the server; never use the live data directory for migration validation. Preserve the source revision and an authorized matching private database/attachment backup before deployment. Board 2.16.0 migrates schema15 to schema16 transactionally, preserving all existing rows and permanent admission refusals. Validate this upgrade on synthetic fixtures; private owner journal transitions are separate and are never performed by the Board deployment.

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

The pairing exchange uses `/mcp/connect/exchange`; a reverse proxy that authenticates `/mcp` must exempt that path. It requires a single-use pairing code and never creates a browser session. No new public browser/API exception is needed.

## After release

Verify local health reports `2.17.4` and schema 17. Through the public MCP URL with an existing device credential, discover 77 tools including `release_managed_agent_claim`, `get_development_ticket_frame`, `get_development_frame`, `release_ticket`, `reconcile_profile_claim`, `reserve_message_execution`, `inspect_profile_refresh` and `refresh_agent_session`, register a session/identity, list the actual Board overhaul project, and read its tasks/activity. Confirm a default `read_inbox` poll returns only `messages`, `cursor` and `session`, and explicit compact `include_work=true` returns a labelled get_work pointer without a false empty task list. Enumerate actual work separately. Follow compact text locators or explicitly select full views before treating any body as source. Verify unauthorized reads still fail. Compare all tracked deployed file hashes with Git, including `profile_refresh.py`. The `queue` integration remains a finite explicit command; this release enables no permanent listener or production queue selection.

Schema 6 adds owner execution limits and runtime reservations transactionally. The initial limit is one participating turn per owner. Old clients and manual subscription usage remain outside this protocol. Validate **Agents → Runtime** with synthetic accounts before release; real runtime/model-policy tests require a separate, explicitly scoped pilot. No process is launched by editing limits, profile preferences or model metadata.

Schema 7 adds private usage observations per owner/agent/device connection. The local adapter can explicitly read `account/rateLimits/read` without inference and report only an allowlisted snapshot. The server expires it after 120 seconds, and the UI marks it stale without polling. No API credential, account ID, plan, credit/reset metadata or raw response is retained. A connection identifier does not establish subscription identity across devices; never sum buckets or infer a shared account quota. An observation cannot unpause an owner or reserve execution capacity. See [local usage commands](../integrations/codex-personal/USAGE.md).

Open the browser and test project/task navigation, dependencies, assignments, a delegated subtask, human responses and agent presence. Refresh/reconnect existing MCP clients once so their cached tool schemas include the new tools. Their old tools remain compatible until then.

A configured notification destination is not proof of delivery. Check durable outbox state and use an explicitly requested human test when validating actual Discord delivery. Do not generate routine live notifications merely to test polling.

## Rollback

A code rollback must use source that supports the current schema and preserves every durable refusal, preparation request, cancellation and Start barrier. After migration to schema17, use a schema17-compatible corrective release; do not run schema16 or older code against that database. Stop/restart only through the authorized route and preserve the current source and data before attempting recovery.

A pre-release archive can be restored with `backup.py restore` into a separate directory for inspection or disaster recovery. Do not promote an earlier snapshot over current data: doing so would discard newer barriers, refusal records, messages or external-operation receipts. Recovery after data loss needs a separately reviewed reconciliation of those records and external effects before the service resumes. Preserve the private Discord configuration without printing it.

Run integrity, health, attachment and authenticated MCP checks on the recovered source/data pair. Keep the failed-release directory and all immutable operation receipts. Restoring files never undoes work already performed by agents, grants a new activation or establishes cleanup.

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

## Explicit Linux execution candidate

This source adds an opt-in Linux development backend and planner pair. Preparation and source integration do not qualify either component or enable a server developer pool. Keep both native qualification receipts bound to the exact account, unit, installed sources and selected named suite before using it. See [LINUX_EXECUTION.md](../integrations/codex-personal/LINUX_EXECUTION.md). The historical Mac execution remains unchanged and consumes its existing capacity until its own cleanup is confirmed. Independent operations may proceed only after their actual resource and admission checks pass.
