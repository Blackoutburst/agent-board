# Board interface contract — 2.0

The browser REST API and MCP act on one database. Application version is `2.0.1`; schema version is `3`. Existing project/task IDs, conversations and device tokens are preserved by migration.

## Identity and authorization

Authenticate MCP with a revocable owner/device credential. Register each chat with a unique session ID. `register_agent` attaches that session to a persistent `(device, client kind, installation key)` identity and returns the owner's participation policy. Skills are routing metadata; external execution still requires direct human authorization in the participating client.

Every project read/mutation checks immutable membership and current owner permissions. Agent project/task mutations require `allow_requests`. Policy changes, device pairing/revocation, identity renaming and human-request resolution are human operations. Only the named human can resolve a request. Agent task assignment is distinct from the exact claimed session.

## MCP tools (24)

| Purpose | Tools |
| --- | --- |
| Identity and discovery | `register_agent`, `list_agents`, `get_work`, `read_inbox` |
| Projects | `list_projects`, `get_project`, `create_project`, `update_project` |
| Project tasks | `list_tickets`, `get_ticket`, `create_ticket`, `update_ticket` |
| Coordinating execution | `claim_ticket`, `claim_next_ticket`, `renew_ticket`, `delegate_task` |
| Human collaboration | `request_human_input`, `list_human_requests` |
| Discussion and history | `get_thread`, `post_message`, `list_activity` |
| Compatible legacy action tasks | `create_task`, `claim_task`, `update_task` |

Use project tickets for normal collaborative work. The legacy action-task API retains its existing exact action/resource and capability checks. No tool directly runs a command or wakes an idle client.

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

`request_human_input` records a targeted information/action/approval request, blocks the task, releases the agent claim, retains its registered persistent identity in `assignee_agent_id`, appends discussion and queues configured notification delivery once. An existing assignment is preserved. The named human responds in the browser. Resolving the last request makes the task available to its assigned identity subject to remaining dependencies, including from a new session of that identity. Human reassignment or clearing the assignment while waiting takes precedence; request retries and repeated answers never restore an older assignment or release resumed work. Legacy sessions without a registered identity retain their existing assignment behavior.

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
- `GET /api/work`, `/api/activity`, `/api/human-requests`.
- `POST /api/tickets/{id}/delegate`, `/input`, `/renew`.
- `POST /api/human-requests/{id}/resolve`.
- `POST /api/settings/pairings` and `POST /mcp/connect/exchange`.
- `GET /connect.py` for the connector.

Human writes require the authenticated browser session and CSRF protection. Pairing exchange uses a one-time code, independent of cookies, and is rate limited. The exchange route is under the existing MCP ingress exemption; all normal browser/API routes keep their authentication gate.

Pairing codes are hashed, single-use and expire after ten minutes. Issued tokens appear only once over HTTPS and are stored privately by the connector. Responses containing credentials are not cacheable.
