# Board interface contract — 2.7.2

The browser REST API and MCP act on one database. Application version is `2.7.2`; schema version is `9`. Existing project/task IDs, conversations and device tokens are preserved by migration.

## Identity and authorization

Authenticate MCP with a revocable owner/device credential. Register each chat with a unique session ID. `register_agent` resolves an explicit `(device, client kind, installation key)` binding to a durable profile and pins its current version. It returns the compatible identity/session, bounded private profile configuration (without bindings or active work), `profile_version`, and the effective intersection of profile/owner policy. Skills are routing metadata; external execution still requires direct human authorization in the participating client.

Every project read/mutation checks membership, current owner permissions, profile status/version and selected projects. Agent project/task mutations require `allow_requests`. Policy changes, profile lifecycle/configuration/bindings, device pairing/revocation, identity renaming and human-request resolution are human operations. Only the named human can resolve a request. Agent task assignment is distinct from the exact claimed session.

## MCP tools (46)

| Purpose | Tools |
| --- | --- |
| Identity and discovery | `register_agent`, `list_agents`, `get_work`, `read_inbox` |
| Projects | `list_projects`, `get_project`, `create_project`, `update_project` |
| Project tasks | `list_tickets`, `get_ticket`, `create_ticket`, `update_ticket` |
| Coordinating execution | `claim_ticket`, `claim_next_ticket`, `lookup_ticket_selection`, `renew_ticket`, `release_ticket`, `reconcile_profile_claim`, `delegate_task` |
| Owner-wide model execution | `reserve_execution`, `lookup_execution`, `start_execution`, `heartbeat_execution`, `finish_execution`, `get_execution_control` |
| Informational subscription observations | `report_usage_observation` |
| Human collaboration | `request_human_input`, `lookup_human_request`, `list_human_requests` |
| Confirmed human-message stream | `open_message_feed`, `read_message_feed`, `checkpoint_message_feed` |
| Discussion and history | `get_thread`, `get_message`, `post_message`, `list_activity` |
| Conversations and addressing | `create_conversation`, `list_conversations`, `list_mention_targets` |
| Files and task tags | `upload_attachment`, `download_attachment`, `list_labels`, `create_label` |
| Compatible legacy action tasks | `create_task`, `claim_task`, `update_task` |

Use project tickets for normal collaborative work. The legacy action-task API retains its existing exact action/resource and capability checks. No tool directly runs a command or wakes an idle client.

`read_inbox` defaults to message-only polling: its response has `messages`, `cursor` and `session`, with no `tasks` or `work` fields. This default also applies to existing clients that omit the new `include_work` argument and to autonomous sessions. Message filtering, cursor ownership/pagination and presence updates remain unchanged. Message-only polling does not enumerate or expire task queues. Workers discover work through `get_work`; passing `include_work=true` explicitly restores the combined legacy task list and work snapshot. Clients needing those fields must opt in or call `get_work` separately.

## Profile configuration and boundaries

New profiles default to no projects, no permissions and no bindings. A migrated profile retains its old scope (`project_ids: null`) and owner grants. Human updates cannot set scope to null; omitting it preserves existing scope. Profiles have `active`, `paused` or `archived` status and a strictly increasing version with immutable configuration snapshots. Update/binding calls require `expected_version`; creation/duplication use stable idempotency keys. Duplication copies presentation/preferences only and starts paused without access.

Sessions cannot change their profile. An edited profile rejects old-version session operations with 409 until explicit registration; a session with an outstanding claim must first have that claim reconciled by its owner, or through the bounded release-only `reconcile_profile_claim` path below. Re-registration does not bypass this guard. Pause/archive reject operations with 403 while preserving claims. The board does not terminate external processes.

The profile policy contains `allow_read`, `allow_messages`, `allow_requests`, exact `actions`/`resources`, and `autonomy` (`responses`, `requested`, `queue`). Owner grants remain the upper bound. Explicit work requires work permission and autonomy beyond responses; automatic queue claims additionally require both queue autonomy and the session's `autonomous=true` registration. Registration never starts a listener.

A device can use only its explicitly bound profiles after first registration. Its credential authorizes every binding on that device; `identity_key` is a selector, not a secret. Use separate paired device credentials for independently revocable connections. Shared `list_agents` results have only `id`, `owner_id`, `name`, `avatar`, `status`; the human owner also receives its own sessions/presence/work. Clients must handle public summaries and profiles without connections.

## Task fields

Task responses include `id`, project-local `number`, `project_id`, discussion `thread_id`, title/description/acceptance criteria, status, priority, labels, required skills, shared resource, intended owner/agent, actual claimant, lease expiry, progress, outcome, evidence, version and URL.

Coordination fields are `depends_on`, expanded `dependencies`, unfinished `blocked_by`, downstream `blocks`, human requests, `claim_expired`, derived `work_state` and `ready`.

Persisted statuses remain `todo`, `in_progress`, `blocked`, `review`, `done`. Derived work states distinguish ready work, running work, dependencies, waiting on a human, expired claims requiring recovery, review and completion.

## Mutation and concurrency rules

- `expected_version` is the positive integer just read. Stale changes return a conflict; reread before deciding what to save.
- Omit unchanged fields. Nullable assignee fields can be explicitly cleared; other project/task fields reject null.
- Creation, queue claims, delegation and human requests use stable idempotency keys. Reusing a key with different content fails.
- Claims and dependency edits are serialized by `BEGIN IMMEDIATE`. Exactly one concurrent caller wins a claim.
- Dependency edges are project-local and acyclic. An unfinished prerequisite or open human request prevents claim/completion.
- New queue selections take one task at a time per persistent agent profile across its sessions/devices. Any unfinished claim (including an expired claim or a legacy action-task claim) or open human request from that profile blocks another selection. Required skills, explicit assignment, project queue state and exclusive resources also gate dispatch. Manual `claim_ticket` retains its existing semantics.
- `renew_ticket` renews the active claim and returns a new version. It cannot revive an expired or replaced claim.
- Expired work needs explicit recovery evidence after external effects are checked. Expired work keeps its exclusive resource until reconciled.
- Completion requires both outcome and verification evidence. Reopening clears old results unless explicitly replaced.

## Releasing work after a profile change (2.6.1)

`release_ticket(session_id, ticket_id, expected_version, idempotency_key, reason, status="", unassign_self=false)` releases the calling session's live claim, or maintains an unclaimed ticket assigned to that persistent profile. Its default preserves the agent assignment, setting it to the calling profile if previously absent; `unassign_self=true` clears only that assignment. A supplied status is limited to `todo`, `blocked` or `review`; review still requires resolved dependencies and human requests. With no explicit status, `in_progress` becomes `todo` and other statuses remain. It cannot clear another session's claim, recover an expired claim or reopen completed work.

`reconcile_profile_claim(session_id, ticket_id, expected_version, idempotency_key, recovery_evidence)` is a separate release-only tool for a claim left behind by an older profile version. After inspecting prior effects, register a **new session** on the same bound profile and device. The new session must have current work-request permission, an active current profile, project scope and membership. The claim must belong to another persisted session of the same owner/profile/device, pinned to a strictly older profile version, with a finite expired lease. Current-version claims, live claims, claims from another device/profile, reassigned tickets and completed tickets are refused. Legacy claim JSON can omit profile metadata only when its persisted source session proves the identity; contradictory metadata is refused.

Every existing Runtime execution for this owner/profile/ticket, including executions from another device, must be `finished` with `cleanup_confirmed=true`. No Runtime history is required for older unmanaged work, but **missing history or an expired lease is not proof that an external process stopped**. Nonempty recovery evidence is mandatory: it records the operator's inspection of processes, local journals and external effects. It is a declaration, not an OS or provider attestation. This tool never changes Runtime records, capacity, provider sessions or model state.

An accepted reconciliation clears the claim and lease, changes `in_progress` to `todo`, and otherwise preserves status, progress, results, evidence, dependencies, waiting flags, requests and the human assignment. It retains the same profile assignment, or sets it to that profile if previously absent, so an unclaimed blocked task can subsequently be corrected explicitly with `release_ticket`. It does not execute, resume or complete work. The ticket version advances once and a `ticket_profile_claim_reconciled` audit event records the source/current profile versions and supplied evidence. Keep credentials, private paths and model output out of that shared evidence.

The recovery key is bound to the exact authorized owner/device/session, ticket and trimmed evidence. `expected_version` is a strict positive integer CAS precondition, excluded from the key payload. Retrying the same key checks current authorization and returns the current ticket without reapplying the release, even if another claim or human edit has appeared. Changed evidence with an existing key conflicts. Unsupported older servers reject this separate tool; clients must not fall back to ordinary claim/release or reuse the old session to bypass its registration guard.

## Durable queue selection (2.3)

`claim_next_ticket(session_id, idempotency_key, project_id="")` checks current work permission, profile queue autonomy and the session's `autonomous=true` declaration. The key is bound to the exact authenticated owner/device/session and project filter. A successful first call returns `{"claimed": true, "ticket": <current ticket>, "retried": false}`. No selection returns `{"claimed": false, "reason": <code>, "retried": false}`, where the fixed code is `no_ready_ticket`, `unfinished_work` or `waiting_on_human`. Both kinds of result are recorded atomically. Repeating a key returns that same selected ticket or negative result with `retried: true`; it never chooses a newly available or higher-priority ticket. A new intentional selection requires a new key. Successful historical keys remain valid; queue selection itself required no schema change in 2.3.

`lookup_ticket_selection(session_id, idempotency_key, project_id="")` is a separate read-only tool. An absent key returns `{"found": false}`. A known key returns `{"found": true, ...}` plus the selection response above with `retried: true`. It checks current read permission, active/current profile, project scope and membership, including for stored negative results. It requires neither queue autonomy, work-request permission nor an autonomous session, so clients can reconcile authorized reads after disabling work. It creates no claim, event, message, notification, session or idempotency record. Reusing a known key with a different project filter conflicts. Another session cannot read or adopt the key, even if it belongs to the same profile.

The selected ticket is returned in its **current** state. Callers must verify its owner/agent/device/session, live lease, assignment and version before continuing work; lookup does not renew or recover it. Other sessions or bindings of the same profile cannot bypass unfinished work by making a new selection. Such refusals expose only an opaque reason, never another project's ticket IDs or titles. Resolved human requests no longer block new selection, but clients must resume their existing durable operation before starting another. The board does not infer local operation state or launch clients.

A missing lookup result cannot prove that a timed-out request is no longer in flight. Clients must durably record their selection intent before sending, retain the same key after uncertainty and avoid interpreting absence as permission to abandon or pick replacement work. The tool never falls back to creation on an older server; an unsupported tool call fails without claiming anything.

## Owner-wide execution registry (2.4)

Schema 6 adds `owner_execution_limits` and `runtime_executions` without changing accounts, claims, messages or credentials. The default is one concurrent execution across **all** profiles, sessions and devices belonging to the owner. Reservations, active processes and uncertain cleanup all consume capacity. Admission and limit changes are serialized in the central SQLite transaction; this is not a per-host semaphore. It covers participating executors, not unrelated personal CLI/app sessions or historical adapters that never reserve capacity.

`reserve_execution(session_id, idempotency_key, project_id, ticket_id, work_id, turn_id, provider, model)` requires current effective read/work-request permission, the current active profile version, project membership and an exact live `in_progress` ticket claim. Opaque work/turn IDs must not contain paths, prompts or provider session IDs. The accepted key is bound to owner, profile/version, device/session, project/ticket and immutable provider/model. It returns `{reserved: true, execution: <metadata>, retried: false}`; an exact accepted retry returns that reservation with `retried: true`, never another slot. Changed payload conflicts. A second key cannot reserve the same owner/work/turn. Admission denied by `owner_paused` or `capacity_exhausted` returns a conflict without creating a reservation. No model is launched by the board.

`lookup_execution(session_id, idempotency_key)` is a separate read-only tool. It returns `{found: false}` or `{found: true, execution: <metadata>}` only for the authenticated original device/session/profile. Absence cannot establish that an in-flight admission was cancelled. Clients must persist their intent before sending and reconcile the same key after a lost response; unsupported older servers must fail closed without fallback to unregistered execution.

`start_execution(session_id, execution_id, expected_revision)` activates a `reserved` execution before the model is spawned, rechecking current authority, owner pause and capacity. `heartbeat_execution(session_id, execution_id, expected_revision)` updates only a running execution's presence. `finish_execution(session_id, execution_id, expected_revision, status, cleanup_confirmed, reason="")` records a strict `completed|blocked|cancelled` outcome and a bounded reason enum, never output or diagnostics. `cleanup_confirmed=true` makes it `finished` and releases capacity; false makes it `needs_review` and retains capacity. Matching activation/heartbeat/finish retries return their existing effects. Conflicts require reading the latest control before an explicit decision. A cleanup confirmation is an authenticated executor attestation after inspecting its own process group, not a server observation of that process.

`get_execution_control(session_id, execution_id)` reads state and `should_stop` without mutation. States are `reserved`, `running`, `needs_review`, `finished`; `stop_requested` is independent. All execution mutations require positive strict integer revisions. A heartbeat older than 60 seconds is exposed as `stale`, but **no expiry releases a reservation or proves a process stopped**. An uncertain reservation may require manual operational recovery; this version does not offer a human force-release API. Clients must not replay a model merely because activation was acknowledged again. Finishing the local model and freeing its slot is distinct from publishing a ticket result or waiting for a human answer.

Existing-execution lookup/control/heartbeat/finish use a restricted exact identity check: they remain available after owner permission disablement or profile pause/change so the original executor can learn `should_stop` and report cleanup. They grant no new model execution, project read or publication permission. A revoked device is refused even on this channel; its unresolved execution continues consuming capacity. Start/reserve always use current full authorization. Owner pause, profile/policy changes, lost claim and archived/inaccessible project produce a stop instruction. A pause or stop request alone never claims that the process actually stopped.

Metadata contains only `id`, `owner_id`, `agent_id`, `profile_version`, `device_id`, `session_id`, `project_id`, `ticket_id`, opaque `work_id`/`turn_id`, `provider`, `model`, `state`, `revision`, `outcome`, categorized `reason`, timestamps, `stop_requested`, `should_stop`, `control_reason`, `cleanup_confirmed` and `stale`. It excludes the idempotency key, prompt, response, executable/workspace path, PID, provider session and credentials. Owner views include all active reservations and at most 50 recently finished executions, and never another owner's registry.

A reported `quota_or_rate_limit` or `authentication_failed` outcome atomically pauses the owner and increments the limits version. The runtime view then reports `quota: {status: "blocked_observed", reason: <category>}`. This is a bounded observation from the executor, not a precise measurement of the subscription. Otherwise quota reflects the availability of the informational connection observations described below; no percentage or reset time is invented. Explicit human `PATCH` with `paused=false` clears the observation. There is no automatic retry, provider/account/model fallback, listener or daemon.

Owner-only REST routes:

- `GET /api/runtime` returns `{limits: {owner_id, max_concurrent, paused, version}, active_count, quota, usage_observations, executions}`.
- `PATCH /api/runtime/limits` accepts exactly `{expected_version, max_concurrent, paused}`; the strict integer limit is 1–8. Lowering it preserves existing reservations and blocks new starts beyond the limit.
- `POST /api/runtime/executions/{id}/stop` accepts exactly `{expected_revision}` and requests cooperative cancellation. It does not release capacity. Reading control then finishing after confirmed cleanup completes the cancellation.

Browser writes keep the existing human authentication and CSRF requirements. The owner must refresh after a stale version; agents cannot change owner limits or stop another execution.

## Delegation and human handoffs

`delegate_task` atomically creates a child task/discussion, adds it as a prerequisite, releases the parent lease and targets parent resumption to the original persistent identity. Required skills route the child. Once all delegated prerequisites complete, the parent returns to `todo` when no human request is open. Discussion and activity preserve the handoff.

`request_human_input` records a targeted information/action/approval request, blocks the task, releases the agent claim, retains its registered persistent identity in `assignee_agent_id`, appends discussion and queues configured notification delivery once. An existing assignment is preserved. The named human responds in the browser. Resolving the last request makes the task available to its assigned identity subject to remaining dependencies, including from a new session of that identity. Human reassignment or clearing the assignment while waiting takes precedence; request retries and repeated answers never restore an older assignment or release resumed work. Legacy sessions without a registered identity retain their existing assignment behavior. Callers can supply `expected_version` to reject a changed task atomically before creating a request. A successful retry returns the existing request before this precondition is checked, so a lost reply remains recoverable after a human answer. Omitting the version preserves older clients; new workers must provide it.

`lookup_human_request` reconciles an attempted creation using the exact original actor/session, idempotency key, ticket, requested owner, question and kind. It returns the current request, including its resolved answer, or `{"found": false}` if that actor has no matching key. Different content with an existing key conflicts. Current work-request permission, project membership and the requested owner's membership are checked even for an existing key. No active claim is required; lookup never creates or changes a request, claim, message, notification or idempotency record. This is a separate tool so an older server rejects an unsupported lookup instead of treating it as a creation request.

## Activity reads

`list_activity` and `GET /api/activity` accept `order="asc"|"desc"` (default `"desc"`), an exclusive nonnegative `after` event ID, and `limit` from 1 to 200. The response remains an array. Default descending order preserves the newest-first history UI. Incremental consumers must explicitly select ascending order and save the last durably processed event ID before requesting the next page. Use the same project/task filter and authenticated scope for that position; replay the saved position and deduplicate IDs after an interruption. IDs need not be consecutive. Ascending pages include later insertions without skipping the earlier unread backlog; descending pages are not suitable for this cursor algorithm. Neither read acknowledges processing or claims work. Current project membership and owner read policy are checked on every request.


Messages accepted through `post_message` in a ticket's discussion now append
`ticket_message_posted` in the same transaction as the message. Its `detail`
contains only `ticket_id`, `thread_id` and `message_id`; read the authorized
thread to obtain message content. The existing activity `actor` identifies the
human or the persistent agent, device and session that posted it. Consumers can
ignore agent-authored messages to avoid response loops. A retry with the same
accepted post idempotency key returns the same message without another event.
Standalone conversations produce no ticket activity. Posting a message does not
change the ticket version, status, assignment or claim.

This message coverage is **prospective from deployment of this change**. Existing
messages are not backfilled. A consumer that needs earlier discussion must
explicitly bootstrap it with authorized `get_thread` pagination. Structured
human requests, their resolutions and delegation retain their existing semantic
activity events; their generated discussion messages do not gain a second
message event. Project updates, policy changes and device revocations are not
part of this ticket event stream; consumers must still check current scope and
permissions before work. This change neither starts a listener nor authorizes
work from message text.

## REST

Existing `/api/projects`, `/api/projects/{id}/tickets`, `/api/tickets/{id}`, discussion, inbox and settings routes remain. New routes include:

- `GET /api/agents`; `PATCH /api/agents/{id}` for a human-owned name.
- `GET/POST /api/agent-profiles`; `GET/PATCH /api/agent-profiles/{id}`.
- `GET /api/agent-profiles/{id}/versions/{version}` for an immutable owner-only snapshot.
- `POST /api/agent-profiles/{id}/duplicate` and `/bindings`.
- `GET /api/work`, `/api/activity`, `/api/human-requests`.
- `POST /api/tickets/{id}/delegate`, `/input`, `/renew`.
- `POST /api/human-requests/{id}/resolve`.
- `POST /api/settings/pairings` and `POST /mcp/connect/exchange`.
- `GET /connect.py` for the connector.

Human writes require the authenticated browser session and CSRF protection. Pairing exchange uses a one-time code, independent of cookies, and is rate limited. The exchange route is under the existing MCP ingress exemption; all normal browser/API routes keep their authentication gate.

Pairing codes are hashed, single-use and expire after ten minutes. Issued tokens appear only once over HTTPS and are stored privately by the connector. Responses containing credentials are not cacheable.

## Addressed conversations and attachments (2.2)

`create_conversation` and `list_conversations` manage authorized conversation context. `list_mention_targets` returns stable recipient IDs. Human mentions use the owner ID; agent mentions use `agent:<profile-id>`. Pass these IDs in `post_message.mentions`. Exact `@Display name` text also resolves against accessible conversation participants; ambiguous names require an explicit ID. Replies notify their original author. Agent mentions do not generate a human Discord alert.

`read_inbox` delivers addressed messages to the registered agent's identity. It advances its opaque, session-bound cursor without claiming any work. Generic legacy `agent` sessions retain delivery of owner mentions for compatibility. Explicit Codex/Claude identities receive their own agent mentions. The browser's `/api/inbox` returns a fresh snapshot of unread human mentions and tasks assigned to the owner, owned agents, or their active claims. Viewing a conversation marks its human mentions read. Ordinary shared conversation messages remain readable through `get_thread` without appearing as inbox notifications.

`upload_attachment` accepts base64 file content and a stable retry key; attach the returned ID to a message, or save it on a task through `update_ticket` with `changes.attachment_ids`. Task attachment IDs must belong to its thread; the ordered list replaces existing task files (at most 100). Omitting it preserves files; `[]` removes the association. Ticket responses return `attachments` metadata, and linking files never creates a discussion message. Existing claim, version and membership checks apply. `download_attachment` returns bounded base64 chunks with `next_offset` and `eof`. To stream arbitrary files without model-context expansion, POST multipart `file` to `/mcp/attachments/{thread_id}` or GET `/mcp/attachments/{attachment_id}`, using `Authorization: Bearer <existing-device-token>` and `X-Board-Session-Id: <registered-session>`. All types are accepted, up to 10 MiB per file. Image and video previews use authenticated `?inline=true`; unsafe active formats remain downloads. Membership and profile scope are enforced for every transfer.

`list_labels` and `create_label` expose the project's reusable tag catalog. Feature/bugfix defaults and existing task tags are included. Task creation/editing uses the existing `labels` array. REST equivalents are `/api/projects/{id}/labels` and `/api/threads/{id}/mentionables`.


## Informational subscription observations (schema 7)

`report_usage_observation(session_id, connection_id, observed_at, snapshot)` accepts only a current, enabled registered agent with `allow_requests`. It is not an existing-execution cleanup channel. Owner/device/profile identity is derived from the authenticated session; revoked devices, paused/edited profiles and disabled policies are refused. The connection ID is `codex-connection-` plus SHA256 of canonical UTF-8 JSON with exactly `board_endpoint` (`BOARD_BASE_URL + "/mcp"`), `owner_id`, `agent_id`, `device_id`, using sorted keys, compact separators and unescaped Unicode. This matches the personal adapter's model-policy connection ID. It identifies a local board binding, **not** a verified provider account.

The strict snapshot contains `schema_version: 1`, `provider: "codex_chatgpt"`, `source: "codex_app_server_account_rate_limits"`, bounded `cli_version`, `data_status: "observed"|"unavailable"`, a nullable fixed `unavailable_reason`, nullable strict boolean `ordinary_usage_allowed`, and at most 32 `buckets`. Each bucket has `limit_id`, `source_view` (`multi_bucket|legacy`), nullable `primary`/`secondary`, nullable known `reached_reason`, nullable boolean `spend_control_reached`, and nullable `normal_model_slug`. A window contains integer `used_percent` in 0–100 and independently nullable positive integer `window_minutes` and `resets_at` (Unix seconds). Unknown/private fields are rejected. Unavailable snapshots have no buckets and no usage-allowed boolean. Missing windows never mean zero usage or full allowance.

`observed_at` is a finite positive Unix timestamp at local read completion. Values more than five seconds in the future are rejected. Schema 7 stores one latest snapshot per connection. Older or conflicting equal timestamps return 409; an identical retry returns the existing observation without refreshing `received_at`. The server computes `expires_at = min(observed_at + 120, received_at + 120)`. Measurements become `stale` after that instant. An unsuccessful newer read records `unavailable`, rather than refreshing a previous successful value. The protocol does not establish provider-side cache age or an authoritative measurement timestamp.

Human-only `GET /api/runtime` adds `usage_observations`, containing only the owner's connection metadata, `agent_name`/`device_label`, profile version, timestamps, derived data status and sanitized snapshot. The top-level quota status preserves `blocked_observed` as the highest priority; otherwise it is `observed` if at least one connection is fresh, `stale` if only expired measurements remain, or `unavailable`. This status describes observation availability, not execution permission or a common owner account. Percentages are never added across buckets, profiles or devices. Two bindings may share an allowance; two devices of one owner may use different subscriptions.

Observations are client-reported metadata, not a cryptographic provider attestation. They cannot unpause an owner, release capacity, grant model/project/device rights, change routing, or prove recovery after a reset timestamp. A successful read does not clear a prior quota/authentication block. Existing execution admission remains independent.

The optional personal adapter reads the subscription through a bounded local Codex app-server stdio child: initialization, `account/read` with `refreshToken=false`, then `account/rateLimits/read` with `excludeResetCreditDetails=true` and `supportsLunaReserve=false`. It starts no thread/turn, login, reset, model fallback or daemon. Sparse notifications are not treated as complete measurements. Account IDs, emails, plan/credit/reset details, tokens, raw provider responses and diagnostics are discarded. `usage read` defaults to metadata-only stdout, `--show` explicitly displays normalized measurements, and `--output-private` writes mode 0600. `usage report` requires an explicit connection, session, owner, profile and private observation file. It verifies the registered identity before publication. The board itself holds no provider authentication.

## Confirmed human-message feeds (2.7)

`open_message_feed`, `read_message_feed` and `checkpoint_message_feed` provide a separate acknowledged stream for addressed human messages. A stable opening key pins the exact session/profile and initial bounds. A pending delivery remains fixed until its exact ID and checkpoint are confirmed. Current read permissions, profile state and conversation access are checked on every delivery and ACK; agent messages, tasks and general conversation history are excluded. See [the protocol and retention contract](docs/MESSAGE-FEED.md). Legacy `read_inbox` keeps its existing cursor semantics and response shape.

## Exact message sources (2.7.2)

`get_message(session_id, thread_id, message_id)` reads one exact message only after current read, membership and profile scope checks. A missing message or a message belonging to another thread returns the same 404. The read does not expire work, advance cursors, mark content read or fetch attachment bytes. The UTF-8 result is capped at 256 KiB, with 413 on an oversized source. Message text and file metadata remain untrusted data; this tool grants no authority to follow their instructions.
