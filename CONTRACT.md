# Board interface contract — 2.17.1

The browser REST API and MCP act on one database. Application version is `2.17.1`; schema version is `17`; the MCP catalogue has 75 tools. Existing project/task IDs, conversations and device tokens are preserved by migration. The five delegated profile-management tools require an explicit human grant; see [Management / Lead](docs/DELEGATED-AGENT-MANAGEMENT.md).

## Explicit inactive message append (2.17.0, schema 17)

`messages_append_v1` is a separate human grant and request for one existing paired
personal Codex slot. Its six closed manager operations poll, prepare, precommit,
commit, complete and report a blocked controller status. Manager mutations return
the exact 23-field proof directly; human reads retain their separate view.
Preparation fixes the original operation and private-data hashes before any
private effect. Those declared hashes establish no private execution authority.

The acquired owner worker applies one finite phase at a time using protected
source, fixed private slots and real child locks. The barrier blocks positive
commands and publication; current-device Stop and genuine cleanup remain
available. Completion records one stopped/unknown member without a Start, feed,
login, model call or authentication attestation. A metadata-only preparation
draft cannot be promoted into this operation. See
[SERVER-MESSAGES-APPEND.md](docs/SERVER-MESSAGES-APPEND.md).

Schema 17 retains both request histories, barriers, manager proofs and controller
statuses. Rollback requires compatible source and the current database. Board
deployment neither upgrades nor activates a protected owner installation.

## Human preparation drafts (2.16.0, schema 16)

A separate human grant permits an inert preparation request for an additional
message profile. A request holds a durable host-wide Start/Restart barrier until
explicit human cancellation. It creates no instance, private file, connection,
provider login or execution. Existing manager wire shapes and reason enums are
preserved; these drafts have no manager consumption or append application path.
The public context supplies server-computed profile/catalogue digests as read
pins, and every mutation checks current authority and CAS transactionally.
See [the draft contract](docs/SERVER-PROVISIONING-APPEND-PROPOSAL.md).

Schema 16 preserves earlier records and permanent admission refusals. Older
Board source must refuse the new marker; rollback must retain draft barriers
and use compatible code. Protected owner source and private journals are
unchanged by this Board migration.

## Explicit source catalogue replacement (2.15.2, schema unchanged)

`replace_server_catalog` is a separate manager tool. It requires a confirmed
human Stop for every existing instance, exact host/catalogue versions and a
closed instance/command fence. Only launch hashes may change; instance IDs,
profiles, generations and stopped intent are retained. A replayed receipt is
historical delivery evidence; the operator must poll fresh before committing
its local binding. See [SERVER-CATALOG-REPLACEMENT.md](docs/SERVER-CATALOG-REPLACEMENT.md).

Adapter 0.30.2 also provides a finite explicit owner recovery entry for a sealed
legacy message attempt. Private Host3, Runtime4 and Message2 transitions occur
only through that reviewed operation, after installing compatible protected
source. Deploying the Board does not upgrade, start or enable an owner service.
See [MESSAGE-RECOVERY.md](deploy/isolated_host/MESSAGE-RECOVERY.md).

## Explicit pretransport admission seal (2.15.1, schema unchanged)

`seal_message_execution_admission` permanently denies the exact original message
reservation key and owner/work/turn when no real execution exists. It rechecks
current identity, permissions and message source and is serialized with every
reservation. The closed seal is distinct from a capacity refusal and binds both
the seal call and original legacy admission request. A positive execution or an
existing capacity refusal is never reclassified. The catalogue now has 63 tools.
See [the seal contract](docs/MESSAGE-ADMISSION-SEAL.md).

The explicit client is not called by normal workers. This API neither resolves a
private journal nor attests local process cleanup, releases capacity or changes
a dispatch budget. It must never be invoked automatically for an uncertain turn;
local abandonment and owner/catalog upgrades remain separate reviewed steps.

## Explicit message admission refusal (2.15.0, schema 15)

The optional `refusal_mode="capacity-v1"` on `reserve_message_execution` returns a
closed, request-bound refusal only for a confirmed capacity rejection. The
serialized transaction stores a permanent tombstone for its key and owner/work/turn;
subsequent legacy or ticket calls cannot turn it into an execution. Fresh
permission and exact-source checks remain required. No Runtime or cleanup receipt
is fabricated. Generic errors, timeouts and lost responses remain uncertain.
See [the normative refusal contract](docs/RUNTIME-ADMISSION-REFUSALS.md).

Adapter 0.30 records fresh confirmed refusals as terminal `not_admitted` in private
Runtime journal schema 3. It never converts existing uncertain attempts, restarts
a model or refunds the consumed dispatch. Message readiness requires a confirmed
feed. The [append-only staging primitive](integrations/codex-personal/HOST_APPEND_STAGE.md)
prepares one inactive target under the existing owner lock; applying, provider login,
provisioning and activation remain separate work. Existing prepared owner catalogs
are not replaced by rerunning `managed-prepare` after a source upgrade.

## Server capacity display (2.14.2, schema unchanged)

Agents reads the existing human-only Runtime summary independently of its controls. It displays only occupied/maximum places, or an explicit unknown state. A full snapshot disables START for a cleanly stopped instance; Stop, Restart and the server admission rules remain unchanged. This UI observation grants no capacity, releases no execution and does not repair pending work.

## Provider preparation (2.14.1, schema unchanged)

`GET /api/provider-onboarding` is human-only and owner-scoped, with an optional
single `profile_id`. It returns a closed provider descriptor list, this owner's
active host and published targets, and the requested profile preferences.
Connection, model catalogue and model access remain `not_attested`; no login,
provisioning, credential read or model call is performed. There is no MCP
counterpart. The interface reuses normal idempotent profile creation and shows
the host identifier for initial manager setup. See
[PROVIDER-ONBOARDING.md](docs/PROVIDER-ONBOARDING.md) for the delivered boundary
and the separate durable login/provisioning work still required.

## Server instance controls (2.14, schema 14)

Human owners explicitly grant one paired device the server-manager role through
`POST /api/server-hosts`. They inspect `/api/server-hosts` and
`/api/server-hosts/{id}`, revoke a grant through `/{id}/revoke`, and submit
`start`, `stop` or `restart` to `/api/server-instances/{id}/commands`. These
mutations retain browser authentication/CSRF, owner isolation, version/generation
CAS and immutable idempotency payloads. Start/restart budgets are integers 1–20;
stop uses 0. A role cannot be self-granted through MCP.

Four device-only MCP tools are added, bringing the total to 62 at version 2.14:
`publish_server_catalog`, `poll_instance_commands`, `ack_instance_command`,
`report_instance_state`. These narrow manager operations take no model
`session_id` and never register a chat or grant work permissions. They require
the exact human-granted owner/device/host binding. Work withdrawal retains the
cleanup channel; revoking the paired device ends it. Catalogs have at most 16
fixed profile/version/mode/hash entries, without paths, shell, prompts or secrets.
The channel never launches a process or edits Runtime cleanup evidence.

Command acceptance, requested state and observed state are distinct. A new
instance is unknown until a clean stop is observed. Restart requires a stopped
observation before a new activation. Observations use their own revision CAS.
Completed activations that stop cannot become active again without a new human
command, including when cleanup is uncertain. A manager may reject an unstarted
launch after work authorization is withdrawn, without accepting execution.
Historical idempotency responses are transport receipts, never fresh authority;
the controller must collect current policy, version, generation and hash again.
All arguments and responses have closed fields and bounded wire sizes. See
[server controls](docs/SERVER-AGENT-CONTROLS.md) for the owner-process boundary.

## Identity and authorization

Authenticate MCP with a revocable owner/device credential. Register each chat with a unique session ID. `register_agent` resolves an explicit `(device, client kind, installation key)` binding to a durable profile and pins its current version. It returns the compatible identity/session, bounded private profile configuration (without bindings or active work), `profile_version`, and the effective intersection of profile/owner policy. Skills are routing metadata; external execution still requires direct human authorization in the participating client.

Every project read/mutation checks membership, current owner permissions, profile status/version and selected projects. Agent project/task mutations require `allow_requests`. Policy changes, profile lifecycle/configuration/bindings, device pairing/revocation, identity renaming and human-request resolution are human operations. Only the named human can resolve a request. Agent task assignment is distinct from the exact claimed session.

## MCP tools (64)

| Purpose | Tools |
| --- | --- |
| Human-granted server management | `publish_server_catalog`, `replace_server_catalog`, `poll_instance_commands`, `ack_instance_command`, `report_instance_state` |
| Identity and discovery | `register_agent`, `inspect_profile_refresh`, `refresh_agent_session`, `list_agents`, `get_work`, `read_inbox` |
| Projects | `list_projects`, `get_project`, `create_project`, `update_project` |
| Project tasks | `list_tickets`, `get_ticket`, `create_ticket`, `update_ticket` |
| Coordinating execution | `claim_ticket`, `claim_next_ticket`, `lookup_ticket_selection`, `renew_ticket`, `release_ticket`, `reconcile_profile_claim`, `delegate_task` |
| Bounded delegation families | `open_delegation_family`, `get_delegation_family`, `lookup_delegation_family`, `stop_delegation_family`, `accept_delegation_result`, `finalize_delegation_family` |
| Fixed development observations | `get_development_frame` |
| Owner-wide model execution | `reserve_execution`, `lookup_execution`, `start_execution`, `heartbeat_execution`, `finish_execution`, `get_execution_control` |
| Addressed-message model execution | `reserve_message_execution` with the same lookup/start/control/heartbeat/finish tools; explicit `seal_message_execution_admission` |
| Informational subscription observations | `report_usage_observation` |
| Human collaboration | `request_human_input`, `lookup_human_request`, `list_human_requests` |
| Confirmed human-message stream | `open_message_feed`, `read_message_feed`, `checkpoint_message_feed` |
| Bounded detail | `get_text`, `list_thread_tasks` |
| Discussion and history | `get_thread`, `get_message`, `post_message`, `list_activity` |
| Conversations and addressing | `create_conversation`, `list_conversations`, `list_mention_targets` |
| Files and task tags | `upload_attachment`, `download_attachment`, `list_labels`, `create_label` |
| Compatible legacy action tasks | `create_task`, `claim_task`, `update_task` |

Use project tickets for normal collaborative work. The legacy action-task API retains its existing exact action/resource and capability checks. No tool directly runs a command or wakes an idle client.

MCP reads default to compact, byte-bounded pages. Omitted prose is labelled with previews and hashes; use `get_text` or explicit `view="full"` for original source. Collection reads follow `next_cursor`, and threads use `next_before`. See [the bounded read and migration contract](docs/MCP-READS.md).

`read_inbox` defaults to `messages`, `cursor`, `session` only. Persist the whole page and cursor before processing; presenting the compact cursor acknowledges the prior page, and cursorless retries replay it. Explicit full mode retains legacy optimistic progression. Compact `include_work=true` returns a labelled `get_work` pointer; workers discover work separately. Polling never claims work.

## Fixed development observation frame

`get_development_frame(session_id, ticket_id, family_id, terminal)` accepts only
these four required arguments, with a strict boolean `terminal` and bounded
opaque identifiers; extra arguments and coercions are refused. The authenticated
session must be the exact stored coordinator or selected child of that family,
with matching owner, profile/version and device. Project and coordinator session
are derived from the family, never supplied by the caller. Both sessions are
reauthenticated for every ordinary Board read and at both selection brackets.

One HTTP call performs 12 ordinary business reads (at most 16 with pagination):
initial policies, tickets and family, both discussion histories, final tickets
and family, then both final policies. Each role retains at most 40 distinct
messages across three real cursor pages. Full sources and exact page arguments
are returned for the adapter's existing claim, context, family and grant checks.
The window is sequential, not atomic, and confers no cached authority. Existing
thread-read expiration maintenance remains; no new claim, Runtime, renewal,
message or execution is added.

Arguments are bounded at 2 KiB, ASCII-escaped result data at 384 KiB, and the
MCP CallToolResult including text/structured copies plus a 1 KiB envelope
reserve at 1.5 MiB. This reserve covers the official client’s integer request ID,
not an arbitrarily large JSON-RPC ID supplied by another client. Failures return a
fixed code without partial observations or private exceptions. This tool has no
compact view or arbitrary method batching. See [the sealed opt-in adapter mode](integrations/codex-personal/DEVELOPMENT_FRAMES.md).

## Profile configuration and boundaries

New profiles default to no projects, no permissions and no bindings. A migrated profile retains its old scope (`project_ids: null`) and owner grants. Human updates cannot set scope to null; omitting it preserves existing scope. Profiles have `active`, `paused` or `archived` status and a strictly increasing version with immutable configuration snapshots. Update/binding calls require `expected_version`; creation/duplication use stable idempotency keys. Duplication copies presentation/preferences only and starts paused without access.

Sessions cannot change their profile. An edited profile rejects old-version session operations with 409 until explicit acknowledgement. With an outstanding claim, ordinary re-registration remains fenced; the original session can use the bounded inspection/refresh protocol below after cleanup, or its claim can be reconciled through the existing owner/expired-claim paths. Pause/archive reject work operations with 403 while preserving claims. The board does not terminate external processes.

`inspect_profile_refresh(session_id, view="full")` is an explicit identity-bound read of this exact device/session's current private configuration, effective policy, profile versions, configuration SHA256 and claim versions. Current read permission is required. Revoked claim metadata stays private. `blocked_reasons` identifies missing work authorization, expired claims and unresolved Runtime/family cleanup. Ordinary polling does not acknowledge configuration changes.

`refresh_agent_session(session_id, from_profile_version, expected_profile_version, configuration_sha256, expected_claims, idempotency_key, recovery_evidence)` requires the inspected current configuration/hash, the exact complete claim set/versions, live current work authorization, valid live leases and concrete inspection evidence. No unresolved Runtime execution for this exact identity/session or its claimed tickets, across any executing profile/device/owner, or active delegation family, is permitted. Only a generic blocker is disclosed for another executor's receipt; its private metadata stays private. A prior human transfer or stop instruction is not cleanup. Cleanup is an operator declaration plus retained Runtime receipts, not an OS attestation. No Runtime row, model operation or delegation plan is repinned.

Every intermediate saved profile version is checked. Only name/avatar/description edits and additive project scope are compatible; these preserve the same session's claims and locks, increment write versions and keep the original lease expiry. Other edits require a claim handoff: tickets lose the claim and return from `in_progress` to `todo`, preserving other statuses, results, progress, assignment, dependencies and requests; legacy tasks enter recovery and release their checked resource locks. Every refresh requires a **new model operation** using the returned configuration. Queue autonomy is disabled until explicitly registered again. The tool never starts work. Repeated keys/arguments return the recorded acknowledgement after current authorization checks without changing a later claim. Changed profile/hash or claim versions require reinspection. Removed permission/scope, pause/archive and device revocation remain immediately effective.

The profile policy contains `allow_read`, `allow_messages`, `allow_requests`, optional boolean `allow_create_projects`, exact `actions`/`resources`, and `autonomy` (`responses`, `requested`, `queue`). Owner grants remain the upper bound. Explicit work requires work permission and autonomy beyond responses; automatic queue claims additionally require both queue autonomy and the session's `autonomous=true` registration. Registration never starts a listener.

Only the owner can grant project creation through the profile editor. `allow_create_projects` does not replace the existing account and profile work-permission checks. When absent, it is false for profiles with selected project scope; an existing legacy profile with unrestricted account scope preserves its prior creation behavior. Explicit false denies creation in either case. An untouched legacy setting stays absent when editing other fields or narrowing scope, and duplication grants no creation permission. Creation never adds a project to the profile's followed list. A scoped creator receives only `{id, url, profile_scope_access: false, owner_scope_grant_required: true}` until the owner separately adds that project to its scope. Exact retries return that creation receipt without exposing current project metadata; they remain bound to the original authenticated idempotency scope.

This permission requires no schema migration, and deployment does not change existing profiles or policies. Effective-policy responses include the new key only when it is explicitly stored, preserving untouched clients' existing authorization snapshots. Older server code does not enforce an explicit creation denial on an unrestricted legacy profile. Before rolling back code while those denials must remain in force, pause the affected profiles or use matching data and owner-reviewed controls.

A device can use only its explicitly bound profiles after first registration. Its credential authorizes every binding on that device; `identity_key` is a selector, not a secret. Use separate paired device credentials for independently revocable connections. Shared `list_agents` results have `id`, `owner_id`, `name`, `avatar`, lifecycle `status`, and aggregate `presence`, `online`, `last_seen_at`, `idle_after_seconds`; the human owner also receives its own sessions/work. Clients must handle public summaries and profiles without connections.

`presence` is `online` when a session on a non-revoked device checked in within the last 900 seconds, `idle` when its latest known check-in is older, and `offline` when there is no known check-in on a non-revoked device. `last_seen_at` is that aggregate timestamp or null and `idle_after_seconds` is 900. Agent mention targets carry the same controls. Presence is independent of profile lifecycle (`active`, `paused`, `archived`) and does not prove an executor is running work or that a task lease is free. Reading presence never renews a session or releases work.

## Task fields

Task responses include `id`, project-local `number`, `project_id`, discussion `thread_id`, title/description/acceptance criteria, status, priority, labels, required skills, shared resource, intended owner/agent, actual claimant, lease expiry, progress, outcome, evidence, version and URL.

Coordination fields are `depends_on`, expanded `dependencies`, unfinished `blocked_by`, downstream `blocks`, human requests, `claim_expired`, derived `work_state` and `ready`. `assignee_presence` describes the assigned profile's aggregate check-in; `assignment_stale` indicates that its reservation may yield during a successful claim.

Persisted statuses remain `todo`, `in_progress`, `blocked`, `review`, `done`. Derived work states distinguish ready work, running work, dependencies, waiting on a human, expired claims requiring recovery, review and completion.

## Verifiable blocks (2.11, schema 13)

A stored `blocked` status must be explained by an open human request, an unfinished dependency, or an explicit `blocked_reason`. Agents setting `status=blocked` through `update_ticket` must supply `blocked_reason` unless one of the first two applies; `recheck_after_seconds` (300–1209600, default 86400) sets `recheck_at`, and `unblock_owner` defaults to the agent profile. `release_ticket(status="blocked")` stores its `reason` the same way with the default recheck. A human editor may block without a reason; it is recorded as a manual block without recheck and is never released automatically.

`blocked_reason`, `unblock_owner`, `recheck_at` and the derived `block_reasons` (`human_request`, `dependency`, `explicit`, `runtime_cleanup`, `delegation`) are returned with each task. A dependency in `review` has delivered its result: it no longer appears in `blocked_by`, is listed in `delivered_dependencies`, and only prevents the dependent from reaching `done`.

The board reevaluates a blocked task whenever a dependency moves to `review` or `done`, a human request is answered, its dependencies are edited, and on ordinary `get_work` calls. This maintenance remains under `allow_read` within the caller's current project membership and profile scope. An explicit project is authorized before any mutation; a denied project read cannot commit a sweep first. A task with no remaining reason returns to `todo`, clearing its block fields, with a `ticket_unblocked` audit event and a thread message; answering a human request stays silent beyond the answer itself.

Unfinished executions or unconfirmed cleanup on the ticket or its family retain a generic `runtime_cleanup` block; a family stop retains `delegation`. These reasons disclose no other executor identity. A clean ordinary execution can become eligible for reevaluation, while a cancelled family is never reopened by this maintenance. Reasonless legacy blocks are released only after all current reasons are absent. The fixed `get_development_frame` endpoint skips the sweep on all four internal policy reads; it adds no ticket or discussion mutation. Existing `get_thread` legacy expiration maintenance remains.

Before reevaluating a legacy block without a recorded reason or expiry, the board checks its status-changing audit history. A last human `ticket_updated` status edit preserves the block as `Blocked manually by <name>`, with that human as `unblock_owner`, no recheck and one `legacy_block_classified` event. A claim release with no explicit status preserves the prior status and is skipped when reconstructing this provenance. The classification runs only after the caller's current project scope and membership checks. It covers already-migrated schema 13 data; it never re-blocks tickets that were previously released or reconstructs unaudited writes.

An open, unclaimed ticket (`todo`, or `in_progress` with an expired claim) whose delegation family is cancelled, finished (`review_ready`) or past its deadline, or whose Runtime cleanup is unconfirmed, is projected with `work_state=blocked`, `ready=false` and the derived `block_reasons` (`delegation`, `runtime_cleanup`); `get_work` lists it neither in `ready` nor in `recovery`. Its stored status is unchanged and a human status edit does not resume the family. Family stops that a policy or grant change could undo are not projected on such tickets; the claim and Runtime guards still refuse them (2.13.1).

When an explicit block expires and its `unblock_owner` is an active agent profile of the project, the task returns to `todo` assigned to that agent with a recheck message: expiry is not evidence that the wait is over, and that agent must verify it or block again with a reason before work starts (2.12.1).

## Review queue, reminders and block escalation (2.12)

`get_work` adds a `review` list: unclaimed `review` tasks in the caller's projects, oldest review first, offered to any eligible agent whose persistent profile did not request the review (recorded by a `review_requested` event when a task enters review; older reviews fall back to the last claimer) and whose skills cover the task. Each item carries `review_since` and `review_author_agent_id`. Claiming a task in review keeps its status `review`; the reviewer completes it with outcome and evidence, or returns it to `todo` with progress and `release_claim`.

During the `get_work` reevaluation, an unclaimed review older than 24 hours receives exactly one thread reminder and one `ready_for_verification` notification per review cycle (`review_reminder` audit event). Reminder maintenance is restricted to the same current profile/project scope as blocked-task reevaluation and is skipped by every policy read inside `get_development_frame`.

Every explicit agent block records `agent_block_recorded`. After three such blocks without an intervening human action or review request, the board stops automatic retries: `recheck_at` is cleared, `unblock_owner` becomes `human`, a thread message and one `needs_input` notification are queued, and `block_escalated` is audited. Explicit human ticket edits, claims/recovery and resolved human requests restart the count, as does a delivered review. Human `get_work` maintenance audits (`ticket_unblocked`, `review_reminder`) do not count as decisions.

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

An active profile's idle/offline assignment no longer excludes other eligible agents after 900 seconds without a ticket update. Unclaimed `todo` work can be selected normally; claimed work becomes recoverable only after its lease expires, through `claim_ticket` with nonempty recovery evidence. Any unfinished Runtime execution or unconfirmed cleanup prevents this handoff, and also prevents expired-claim recovery. Fresh assignments, live leases, paused/archived profiles, disabled owner/profile work permissions, owner Runtime pauses, human-owner assignments, dependencies, open requests, source/destination project scope, required skills, family fences and resource locks remain authoritative. Reads report eligibility without modifying records. A successful claim atomically clears the stale agent assignment, advances the version, records `ticket_idle_assignment_released`, and notifies the previous assignee; the old worker cannot renew or update the replacement claim. A new check-in or ticket edit before claiming cancels stale eligibility.

## Releasing work after a profile change (2.6.1)

`release_ticket(session_id, ticket_id, expected_version, idempotency_key, reason, status="", unassign_self=false)` releases the calling session's live claim, or maintains an unclaimed ticket assigned to that persistent profile. Its default preserves the agent assignment, setting it to the calling profile if previously absent; `unassign_self=true` clears only that assignment. A supplied status is limited to `todo`, `blocked` or `review`; review still requires resolved dependencies and human requests. With no explicit status, `in_progress` becomes `todo` and other statuses remain. It cannot clear another session's claim, recover an expired claim or reopen completed work.

`reconcile_profile_claim(session_id, ticket_id, expected_version, idempotency_key, recovery_evidence)` is a separate release-only tool for a claim left behind by an older profile version. After inspecting prior effects, register a **new session** on the same bound profile and device. The new session must have current work-request permission, an active current profile, project scope and membership. The claim must belong to another persisted session of the same owner/profile/device, pinned to a strictly older profile version, with a finite expired lease. Current-version claims, live claims, claims from another device/profile, reassigned tickets and completed tickets are refused. Legacy claim JSON can omit profile metadata only when its persisted source session proves the identity; contradictory metadata is refused.

Every existing Runtime execution for this owner/profile/ticket, including executions from another device, must be `finished` with `cleanup_confirmed=true`. No Runtime history is required for older unmanaged work, but **missing history or an expired lease is not proof that an external process stopped**. Nonempty recovery evidence is mandatory: it records the operator's inspection of processes, local journals and external effects. It is a declaration, not an OS or provider attestation. This tool never changes Runtime records, capacity, provider sessions or model state.

An accepted reconciliation clears the claim and lease, changes `in_progress` to `todo`, and otherwise preserves status, progress, results, evidence, dependencies, waiting flags, requests and the human assignment. It retains the same profile assignment, or sets it to that profile if previously absent, so an unclaimed blocked task can subsequently be corrected explicitly with `release_ticket`. It does not execute, resume or complete work. The ticket version advances once and a `ticket_profile_claim_reconciled` audit event records the source/current profile versions and supplied evidence. Keep credentials, private paths and model output out of that shared evidence.

The recovery key is bound to the exact authorized owner/device/session, ticket and trimmed evidence. `expected_version` is a strict positive integer CAS precondition, excluded from the key payload. Retrying the same key checks current authorization and returns the current ticket without reapplying the release, even if another claim or human edit has appeared. Changed evidence with an existing key conflicts. Unsupported older servers reject this separate tool; clients must not fall back to ordinary claim/release or reuse the old session to bypass its registration guard.

## Bounded coordinator families (2.9, schema 11)

Six separate MCP tools provide temporary workers without creating permanent
profiles: `open_delegation_family`, `get_delegation_family`,
`lookup_delegation_family`, `stop_delegation_family`,
`accept_delegation_result` and `finalize_delegation_family`. Each takes the
registered `session_id` in addition to the arguments below. These tools record
coordination and verification declarations; they launch no model, inspect no
checkout and supply no filesystem or process attestation.

`open_delegation_family(parent_ticket_id, expected_version, children, limits,
actions, resource, manifest_sha256, idempotency_key)` requires the exact live
coordinator claim, current read/work permissions and an active current profile.
The parent keeps that claim and gains dependencies on exactly two newly created
tickets. The children inherit its project, owner, profile, priority and exact
resource. There is no nested delegation. Each child descriptor has exactly
`session_id`, `role`, `model`, `title`, `description`, `acceptance_criteria` and
`files`; its already registered session must belong to the same owner, device
and profile version as the coordinator. The three sessions must be distinct,
idle except for the coordinator's parent claim, and never previously bound to
another family. Outstanding Runtime cleanup on the parent also prevents opening,
including records left by a previous owner after a human reassignment.

`limits` contains exactly `max_model_turns` (2–64), `max_tool_actions` (0–128),
`max_concurrent` (1–4) and a finite absolute Unix `deadline` in the next 24 hours.
`actions` is a unique subset of `repo.read_file`, `repo.patch_file` and
`repo.run_test`, bounded by current effective grants; any action requires the
exact resource grant. `manifest_sha256` is a lowercase SHA256 digest supplied by
the caller. Each child has at most 64 relative file paths of at most 200 UTF-8
bytes, with no hidden, empty, traversal, absolute or backslash components.
Normalized case-insensitive aliases and overlaps between the two sets are
refused. These declarations constrain participating local controllers; the
server does not read the files or validate a manifest's contents. Family history
is retained, capped at 256 entries per owner, and never silently purged.

Opening is atomic with both tickets, discussion membership, dependencies,
numbers, audit and the exact-session retry record. Reusing its key with a
different plan conflicts; `expected_version` is a strict positive CAS
precondition, excluded from retry content. An accepted retry returns the current
family after current coordinator authorization, without recreating children.
`lookup_delegation_family(parent_ticket_id, idempotency_key)` is read-only and
returns `{"found": false}` or `{"found": true, "family": ...}` for that exact
session's opening. It never opens, claims or renews work.

`get_delegation_family(family_id)` checks current read permissions and project
membership. Its public view contains `id`, `owner_id`, `agent_id`, `project_id`,
`parent_ticket_id`, `state`, `revision`, timestamps, `stop_reason`,
`plan_sha256`, `final_snapshot_sha256`, `should_stop`, `control_reason` and
`children`. Each child has `ticket_id`, `role`, `model`, `state`,
`result_sha256` and `accepted_version`. Only the owner human or an authorized
session of that same owner/profile receives the additional `plan`, including
the temporary sessions, file bounds, actions, resource and parent manifest
digest. No device credential or provider authentication is returned.

Family-linked ticket responses add `delegation` with `family_id`,
`parent_ticket_id`, `owner_id`, `agent_id`, `state`, `revision`, `role` and
`model` (`null` for the coordinator). Ordinary tickets keep their existing
shape. This public metadata carries no private session or file bounds. A
participating development worker must refuse an ordinary unbound run for such
a ticket, and resolve and validate the private family plan before choosing its
local manifest, model or effects. The [local family controller](integrations/codex-personal/FAMILY.md)
provides those checks for its explicit CLI path. The server enforces coordination
bounds and does not inspect local files or establish native sandbox qualification.

The project-visible ticket projection also includes a `coordinator` summary, exactly two `children` summaries and a boolean `should_stop`. Summaries contain only task IDs/titles/numbers/statuses, public agent name, role, declared child model and accepted-result state. The coordinator model is null rather than inferred from private Runtime history or preferences. No bounded-plan files, session/device references or digests enter these summaries. The browser renders these as text and local task links. Family metadata may change without the selected task version; visible task refresh compares it separately. Terminal `review_ready` is an audit-ready state even though further family execution is stopped.

Only each child's exact temporary session can claim that ticket. A session
bound to a family cannot claim or reserve another ticket, including after
cancellation. These dedicated sessions create only ticket-scoped Runtime;
addressed-message reservations are refused even in the parent or child's own
discussion, so a separate response host cannot spend their family budget. The
same fence refuses legacy `claim_task` before claim expiry or lock mutations;
legacy discovery no longer advertises those tasks as authorized to the dedicated
sessions. Existing unrelated sessions retain the legacy API's behavior. The
coordinator likewise remains bound to its parent. Children use explicit
`claim_ticket`; the normal queue's
one unfinished claim per profile rule is preserved. The shared resource lock
permits only members of that same family to coexist. Opening refuses an existing
legacy resource lock. Other ticket and legacy work remains blocked while any
family claim is retained or any family Runtime record has unresolved cleanup,
including after stop and after claims have been released. The resource fence is
removed only when both kinds of uncertainty are gone. This exception is not
permission to share a checkout: local file bounds and isolated checkouts still
require verification.

The existing Runtime admission paths enforce the family bounds inside their
central transaction after retry lookup and before inserting a reservation.
Ticket-scoped family executions also consume the normal owner-wide capacity
limit shared with other ticket and message work. Every new `codex_chatgpt`
family reservation spends one
model turn; every `local_broker` reservation spends one tool action, including
cancelled attempts. Retries and `start_execution` never spend a second turn.
Only those providers are accepted. Child model reservations must match their
declared model; local action reservations must fit the plan's action set.
The coordinator cannot execute until both results have been accepted.

Admission, start and live control recheck deadline, parent claim/lease,
membership, profile and grants, the parent's exact resource, and both children's
scope. A pending child's reassignment, foreign/expired claim, changed resource,
title, description or acceptance criteria, unfinished dependency, human request
or `blocked`/unaccepted `done` state stops participation. A released `review`
result is allowed. Accepted children must remain at their exact accepted
version and outcome/evidence digest. Runtime reports any family control refusal
as the existing closed code `authorization_changed`; family reads expose a
bounded diagnostic `control_reason`. These are cooperative stop instructions,
not proof that a process ended.

`accept_delegation_result(family_id, expected_revision, child_ticket_id,
expected_child_version, result_sha256, verification_evidence, idempotency_key)`
requires the exact authorized coordinator and a current, unclaimed child in
`review`, with resolved dependencies/human requests and nonempty outcome and
evidence. `result_sha256` is SHA256 of compact, sorted-key UTF-8 JSON with
unescaped Unicode and exactly `{"outcome": ..., "evidence": ...}`. All Runtime
records for that child must be finished with cleanup confirmed. The caller must
have independently checked its local artifacts before supplying nonempty
verification evidence; the server checks identity, CAS and this declared digest,
not those artifacts. Acceptance marks the child done, records its resulting
version, advances family and parent versions, and retains the parent claim.
The family changes from `running` to `integrating` after the second acceptance.
An exact retry returns the current family without accepting twice; changed
result/evidence content conflicts. The two expected versions are strict CAS
preconditions, excluded from retry content.

`finalize_delegation_family(family_id, expected_revision, snapshot_sha256,
verification_evidence, idempotency_key)` requires `integrating`, both unchanged
accepted children and confirmed cleanup for every parent/child Runtime record.
It stores the caller's integration snapshot digest and verification declaration,
then changes the family to `review_ready`. This is an audit receipt for an
inspected snapshot, not a fresh filesystem validation. It never marks the parent
done, releases its claim, publishes code or merges changes. A later application
or acceptance must revalidate through its separately authorized effect. Retry
content is the family, snapshot digest and evidence, excluding the CAS revision.

`stop_delegation_family(family_id, expected_revision)` is available to the exact
original coordinator through the identity-only execution channel even after
profile/policy withdrawal; a revoked device still refuses. The owner human can
also stop using CSRF-protected `POST /api/delegation-families/{id}/stop` with
`{"expected_revision": ...}`. Owner-only `GET /api/delegation-families/{id}`
returns 404 for an absent or different owner's family. Stop changes the family
to `cancelled`; an exact lost-response retry is idempotent. It never marks
Runtime finished, confirms cleanup, releases capacity or removes claims.
Existing exact-identity Runtime lookup/control/finish remain the cleanup path.

Schema 11 adds the two family tables and an owner-history index in the same
transaction as the schema version. Existing ticket/message Runtime rows,
claims, accounts, credentials and messages are retained. A failed migration
rolls back both tables and version; full backups retain plans, accepted results,
claims and unresolved capacity. No listener or new execution is activated by
migration.

## Durable queue selection (2.3)

`claim_next_ticket(session_id, idempotency_key, project_id="")` checks current work permission, profile queue autonomy and the session's `autonomous=true` declaration. The key is bound to the exact authenticated owner/device/session and project filter. A successful first call returns `{"claimed": true, "ticket": <current ticket>, "retried": false}`. No selection returns `{"claimed": false, "reason": <code>, "retried": false}`, where the fixed code is `no_ready_ticket`, `unfinished_work` or `waiting_on_human`. Both kinds of result are recorded atomically. Repeating a key returns that same selected ticket or negative result with `retried: true`; it never chooses a newly available or higher-priority ticket. A new intentional selection requires a new key. Successful historical keys remain valid; queue selection itself required no schema change in 2.3.

`lookup_ticket_selection(session_id, idempotency_key, project_id="")` is a separate read-only tool. An absent key returns `{"found": false}`. A known key returns `{"found": true, ...}` plus the selection response above with `retried: true`. It checks current read permission, active/current profile, project scope and membership, including for stored negative results. It requires neither queue autonomy, work-request permission nor an autonomous session, so clients can reconcile authorized reads after disabling work. It creates no claim, event, message, notification, session or idempotency record. Reusing a known key with a different project filter conflicts. Another session cannot read or adopt the key, even if it belongs to the same profile.

The selected ticket is returned in its **current** state. Callers must verify its owner/agent/device/session, live lease, assignment and version before continuing work; lookup does not renew or recover it. Other sessions or bindings of the same profile cannot bypass unfinished work by making a new selection. Such refusals expose only an opaque reason, never another project's ticket IDs or titles. Resolved human requests no longer block new selection, but clients must resume their existing durable operation before starting another. The board does not infer local operation state or launch clients.

A missing lookup result cannot prove that a timed-out request is no longer in flight. Clients must durably record their selection intent before sending, retain the same key after uncertainty and avoid interpreting absence as permission to abandon or pick replacement work. The tool never falls back to creation on an older server; an unsupported tool call fails without claiming anything.

## Owner-wide execution registry (2.4)

Schema 6 adds `owner_execution_limits` and `runtime_executions` without changing accounts, claims, messages or credentials. The default is one concurrent execution across **all** profiles, sessions and devices belonging to the owner. Reservations, active processes and uncertain cleanup all consume capacity. Admission and limit changes are serialized in the central SQLite transaction; this is not a per-host semaphore. It covers participating executors, not unrelated personal CLI/app sessions or historical adapters that never reserve capacity.

`reserve_execution(session_id, idempotency_key, project_id, ticket_id, work_id, turn_id, provider, model)` requires current effective read/work-request permission, the current active profile version, project membership and an exact live `in_progress` ticket claim. Opaque work/turn IDs must not contain paths, prompts or provider session IDs. The accepted key is bound to owner, profile/version, device/session, project/ticket and immutable provider/model. It returns `{reserved: true, execution: <metadata>, retried: false}`; an exact accepted retry returns that reservation with `retried: true`, never another slot. Changed payload conflicts. A second key cannot reserve the same owner/work/turn. Admission denied by `owner_paused` or `capacity_exhausted` returns a conflict without creating a reservation. No model is launched by the board.

`lookup_execution(session_id, idempotency_key)` is a separate read-only tool. It returns `{found: false}` or `{found: true, execution: <metadata>}` only for the authenticated original device/session/profile. Absence cannot establish that an in-flight admission was cancelled. Clients must persist their intent before sending and reconcile the same key after a lost response; unsupported older servers must fail closed without fallback to unregistered execution.


Ordinary tickets claimed in `review` also admit a Runtime turn by a profile
other than the agent that requested that review. This is a separate server-only
authorization path; family admission and coordinator checks still require
`in_progress`. There is no caller-supplied scope selector or fallback for an old
ticket execution. The private payload pins the exact review event, claim-start
version, delivered fields and applicable grants. Renewals and progress remain
valid; changed status, review cycle, claim, scope, skills, delivery or grants stop
the old turn. Delivery-edit audit IDs prevent an edit followed by restoration
from reviving it. At most 64 subsequent policy audits of that owner are checked
against this profile's effective grants; an applicable revocation/restoration,
missing/ambiguous history or exceeded bound refuses continuation. Unrelated
owners and grant changes not effective for this profile do not stop it. This is
an observation of current state and retained server audits, not a guarantee
against arbitrary in-process mutations between observations. Exact-identity
lookup/control/finish remain available to attest cleanup after authorization is
lost. Current prerequisite IDs, eligible statuses and their latest retained
status-change audits form a separate eligibility pin: a dependency status
round-trip does not revive a review reservation. The proof reads at most 100
current prerequisites and, newest first, 64 candidate audits per prerequisite
until its latest status audit is established. Details are fetched individually
only after their text type and byte length are checked: at most 128 KiB each and
1 MiB in total per control. Explicit JSON/event shapes reject duplicate keys,
non-finite values, ambiguous field lists and invalid Unicode; no SQL JSON
function parses these unvalidated prerequisite details. Missing, malformed,
ambiguous or over-bound history refuses that path with the same bounded stop
instruction, without audit text. These limits bound fetched/parsed records and
bytes; they do not bound SQLite's internal heap while measuring stored text,
nor impose a wall-clock limit on its metadata search. The
read sequence remains an observation, not an atomic snapshot against external
writers. Dependency claims that keep review status, progress, comments and
content edits outside this guard do not invalidate it. Their delivered text is
not silently added to the review's direct model source. Automatic task selection
is unchanged.

`start_execution(session_id, execution_id, expected_revision)` activates a `reserved` execution before the model is spawned, rechecking current authority, owner pause and capacity. `heartbeat_execution(session_id, execution_id, expected_revision)` updates only a running execution's presence. `finish_execution(session_id, execution_id, expected_revision, status, cleanup_confirmed, reason="")` records a strict `completed|blocked|cancelled` outcome and a bounded reason enum, never output or diagnostics. `cleanup_confirmed=true` makes it `finished` and releases capacity; false makes it `needs_review` and retains capacity. Matching activation/heartbeat/finish retries return their existing effects. Conflicts require reading the latest control before an explicit decision. A cleanup confirmation is an authenticated executor attestation after inspecting its own process group, not a server observation of that process.

`get_execution_control(session_id, execution_id)` reads state and `should_stop` without mutation. States are `reserved`, `running`, `needs_review`, `finished`; `stop_requested` is independent. All execution mutations require positive strict integer revisions. A heartbeat older than 60 seconds is exposed as `stale`, but **no expiry releases a reservation or proves a process stopped**. An uncertain reservation may require manual operational recovery; this version does not offer a human force-release API. Clients must not replay a model merely because activation was acknowledged again. Finishing the local model and freeing its slot is distinct from publishing a ticket result or waiting for a human answer.

Existing-execution lookup/control/heartbeat/finish use a restricted exact identity check: they remain available after owner permission disablement or profile pause/change so the original executor can learn `should_stop` and report cleanup. They grant no new model execution, project read or publication permission. A revoked device is refused even on this channel; its unresolved execution continues consuming capacity. Start/reserve always use current full authorization. Owner pause, profile/policy changes, lost claim and archived/inaccessible project produce a stop instruction. A pause or stop request alone never claims that the process actually stopped.

Metadata contains only `id`, `owner_id`, `agent_id`, `profile_version`, `device_id`, `session_id`, `project_id`, `ticket_id`, opaque `work_id`/`turn_id`, `provider`, `model`, `state`, `revision`, `outcome`, categorized `reason`, timestamps, `stop_requested`, `should_stop`, `control_reason`, `cleanup_confirmed` and `stale`. It excludes the idempotency key, prompt, response, executable/workspace path, PID, provider session and credentials. Owner views include all active reservations and at most 50 recently finished executions, and never another owner's registry.

A reported `quota_or_rate_limit` or `authentication_failed` outcome atomically pauses the owner and increments the limits version. The runtime view then reports `quota: {status: "blocked_observed", reason: <category>}`. This is a bounded observation from the executor, not a precise measurement of the subscription. Otherwise quota reflects the availability of the informational connection observations described below; no percentage or reset time is invented. Explicit human `PATCH` with `paused=false` clears the observation. There is no automatic retry, provider/account/model fallback, listener or daemon.

### Addressed-message reservations (schema 10)

`reserve_message_execution(session_id, idempotency_key, thread_id, message_id, source_sha256, work_id, turn_id, provider, model)` admits one reply turn without creating, claiming or completing a ticket. It requires current effective `allow_read` and `allow_reply`, an active exact profile/device/session/version, conversation membership and a **human** source addressed to this executor according to `_inbox_targets`. A named Codex/Claude profile receives its `agent:PROFILE` mentions; an owner mention alone is not permission for that named profile. The existing generic-agent owner-addressing behavior remains unchanged. Message permission grants no work-request, repository, shell or ticket-management authority.

`get_message` adds a top-level `source_sha256` beside its existing `message`. The SHA256 input is UTF-8 JSON with sorted keys, no insignificant whitespace and unescaped Unicode, containing exactly `{id, thread_id, body, author, created_at, reply_to, mentions, attachment_ids}` from the raw message row. `author`, `mentions` and `attachment_ids` are decoded stored values. Dynamic URLs, mention display names and attachment metadata are excluded. Neither this hash nor a source reference causes attachment/file/URL fetching.

Admission, activation and control revalidate the exact source hash and current addressing/access. An edited, withdrawn, retargeted or inaccessible source prevents start and yields `should_stop` during execution. The accepted key binds the message scope and source hash as well as all existing immutable execution identities. The owner/work/turn uniqueness and owner-wide capacity counter are common to ticket and message executions, including uncertain cleanup. Changing scope under an accepted key conflicts; no independent response capacity pool is created.

Message execution metadata adds `scope_kind="message"`, `thread_id`, `message_id` and `source_sha256`. `project_id`/`ticket_id` are null for general conversations, or the related ticket IDs when that link is currently visible to this owner. Changing those linked references or withdrawing that visible context stops the old reservation. These references are context only and confer no claim. Historical ticket execution response fields and the six original tools retain their previous shape and authorization rules. Cleanup/lookup/control remain available to the exact original identity after source, membership, owner permission or profile-version withdrawal; revoked devices are still refused. Finishing an execution never posts its reply.

Schema 10 transactionally rebuilds only `runtime_executions` to make project/ticket references nullable and add the checked message scope plus thread/message foreign keys. Legacy rows, values, uniqueness constraints, indexes and foreign keys are retained, with `scope_kind="ticket"` internally. Migration failure rolls back both table replacement and schema version. Complete backups preserve both scope kinds and unresolved capacity. This extension launches no model and activates no listener.

Owner-only REST routes:

- `GET /api/runtime` returns `{limits: {owner_id, max_concurrent, paused, version}, active_count, quota, usage_observations, executions}`.
- `PATCH /api/runtime/limits` accepts exactly `{expected_version, max_concurrent, paused}`; the strict integer limit is 1–8. Lowering it preserves existing reservations and blocks new starts beyond the limit.
- `POST /api/runtime/executions/{id}/stop` accepts exactly `{expected_revision}` and requests cooperative cancellation. It does not release capacity. Reading control then finishing after confirmed cleanup completes the cancellation.

Browser writes keep the existing human authentication and CSRF requirements. The owner must refresh after a stale version; agents cannot change owner limits or stop another execution.

## Delegation and human handoffs

`delegate_task` atomically creates a child task/discussion, adds it as a prerequisite, releases the parent lease and targets parent resumption to the original persistent identity. Required skills route the child. Once all delegated prerequisites complete, the parent returns to `todo` when no human request is open. Discussion and activity preserve the handoff.

`request_human_input` records a targeted information/action/approval request, blocks the task, releases the agent claim, retains its registered persistent identity in `assignee_agent_id`, appends discussion and queues configured notification delivery once. An existing assignment is preserved. The named human responds in the browser. Resolving the last request makes the task available to its assigned identity subject to remaining dependencies, including from a new session of that identity. Human reassignment or clearing the assignment while waiting takes precedence; request retries and repeated answers never restore an older assignment or release resumed work. Legacy sessions without a registered identity retain their existing assignment behavior. Callers can supply `expected_version` to reject a changed task atomically before creating a request. A successful retry returns the existing request before this precondition is checked, so a lost reply remains recoverable after a human answer. Omitting the version preserves older clients; new workers must provide it.

`lookup_human_request` reconciles an attempted creation using the exact original actor/session, idempotency key, ticket, requested owner, question and kind. It returns the current request, including its resolved answer, or `{"found": false}` if that actor has no matching key. Different content with an existing key conflicts. Current work-request permission, project membership and the requested owner's membership are checked even for an existing key. No active claim is required; lookup never creates or changes a request, claim, message, notification or idempotency record. This is a separate tool so an older server rejects an unsupported lookup instead of treating it as a creation request.

### Short human requests (2.17.1, unchanged schema 17)

Ask one short question or action. `request_human_input` and `lookup_human_request`
also accept optional `summary` (one line, at most 240 characters), `choices`
(two to six distinct single-line strings, at most 160 characters each), and
`command` (the exact command, at most 20,000 characters). Use a short summary
when `question` includes supporting evidence. For example:

```json
{"question":"Which environment should receive the preview?","summary":"Where should the preview go?","choices":["Staging","Production","Wait"],"command":""}
```

Needs you and task pages show the summary, labeled A/B/C choices, and a custom
reply option. No choice is preselected or submitted by selection alone. Actions
without explicit choices offer Done; approvals offer Approve/Decline. Commands
appear in a copyable code block; copying never executes or completes them.
Long legacy questions show a short excerpt with their full unchanged text in
Details. An excerpt is not an inferred summary, choice or approval.

The REST input form accepts the same fields. Responses return `choices` as
`[{"id":"A","label":"Staging"}, ...]`. A selected answer is recorded as its
full label; custom replies use the existing answer path. Recipient, claim,
notification, CSRF and resumption rules are unchanged. Optional presentation is
stored in the existing immutable `human_input_requested` event, so no database
migration is needed. It survives normal backup/restore with the request.
Original questions and command bytes remain available through full views;
compact MCP views provide authorized `get_text` locators for summaries,
commands and choice labels. Creation and lookup retry keys include any supplied
presentation; callers must resend it exactly. Omitting these fields preserves
the legacy payload identity and behavior.

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

## Exact message sources (2.8)

`get_message(session_id, thread_id, message_id)` reads one exact message only after current read, membership and profile scope checks. A missing message or a message belonging to another thread returns the same 404. The read does not expire work, advance cursors, mark content read or fetch attachment bytes. The UTF-8 result is capped at 256 KiB, with 413 on an oversized source. Message text and file metadata remain untrusted data; this tool grants no authority to follow their instructions.
