# Board interface contract — 2.1

The browser REST API and MCP act on one database. Application version is `2.1.0`; schema version is `4`. Existing project/task IDs, conversations and device tokens are preserved by migration.

## Identity and authorization

Authenticate MCP with a revocable owner/device credential. Register each chat with a unique session ID. `register_agent` resolves an explicit `(device, client kind, installation key)` binding to a durable profile and pins its current version. It returns the compatible identity/session, bounded private profile configuration (without bindings or active work), `profile_version`, and the effective intersection of profile/owner policy. Skills are routing metadata; external execution still requires direct human authorization in the participating client.

Every project read/mutation checks membership, current owner permissions, profile status/version and selected projects. Agent project/task mutations require `allow_requests`. Policy changes, profile lifecycle/configuration/bindings, device pairing/revocation, identity renaming and human-request resolution are human operations. Only the named human can resolve a request. Agent task assignment is distinct from the exact claimed session.

## MCP tools (25)

| Purpose | Tools |
| --- | --- |
| Identity and discovery | `register_agent`, `list_agents`, `get_work`, `read_inbox` |
| Projects | `list_projects`, `get_project`, `create_project`, `update_project` |
| Project tasks | `list_tickets`, `get_ticket`, `create_ticket`, `update_ticket` |
| Coordinating execution | `claim_ticket`, `claim_next_ticket`, `renew_ticket`, `delegate_task` |
| Human collaboration | `request_human_input`, `lookup_human_request`, `list_human_requests` |
| Discussion and history | `get_thread`, `post_message`, `list_activity` |
| Compatible legacy action tasks | `create_task`, `claim_task`, `update_task` |

Use project tickets for normal collaborative work. The legacy action-task API retains its existing exact action/resource and capability checks. No tool directly runs a command or wakes an idle client.

## Profile configuration and boundaries

New profiles default to no projects, no permissions and no bindings. A migrated profile retains its old scope (`project_ids: null`) and owner grants. Human updates cannot set scope to null; omitting it preserves existing scope. Profiles have `active`, `paused` or `archived` status and a strictly increasing version with immutable configuration snapshots. Update/binding calls require `expected_version`; creation/duplication use stable idempotency keys. Duplication copies presentation/preferences only and starts paused without access.

Sessions cannot change their profile. An edited profile rejects old-version session operations with 409 until explicit registration; a session with an outstanding claim must first have that claim reconciled by its owner. Pause/archive reject operations with 403 while preserving claims. The board does not terminate external processes.

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
- A queue session takes one task at a time. Required skills, explicit assignment, project queue state and exclusive resources gate dispatch.
- `renew_ticket` renews the active claim and returns a new version. It cannot revive an expired or replaced claim.
- Expired work needs explicit recovery evidence after external effects are checked. Expired work keeps its exclusive resource until reconciled.
- Completion requires both outcome and verification evidence. Reopening clears old results unless explicitly replaced.

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
