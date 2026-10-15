# Projects and tickets — implementation contract

This additive feature is isolated from Touhou6. Preserve existing authentication, policies, conversations, tasks, notifications, and default disabled automation. Never edit real data during development.

## Core/API contract

Implement on Board (a separate mixin module is acceptable). All operations authenticate current actor and check project membership. Project participants are fixed on creation for this first version, always include creator, and use existing owner IDs. Reads use allow_read; agent metadata mutations use allow_requests. No tool executes tasks or changes server permissions. Human members may edit project metadata and tickets. Agents may create projects/tickets only with allow_requests, and update a ticket only when they hold its claim. Agent project updates require allow_requests and membership. Preserve all prior permission semantics and make the expanded metadata permission explicit in UI/docs. Current device policies remain unchanged.

Projects: id, name (1..120), description (0..4000), context (0..30000, plain text shared brief), repository_url (0..2000, blank or HTTPS URL), environment (0..4000), participants, version (starts at 1), archived (bool), created_at, updated_at, ticket_counts, url. No deletion; archive supported. Use explicit transactional schema v2 migration preserving schema v1 data. SQLite migration and create operations must be atomic. Use per-project monotonic ticket numbers, unique(project_id,number). Repeated idempotency keys cannot duplicate threads/projects/tickets.

Methods:
- list_projects(actor, query="", include_archived=False) -> list[project summary]
- create_project(actor, name, participants, description="", context="", repository_url="", environment="", idempotency_key="") -> project
- get_project(actor, project_id) -> project
- update_project(actor, project_id, expected_version, **changes) -> project; allowed changes name,description,context,repository_url,environment,archived. Conflicts return BoardError 409 without overwriting.
- list_tickets(actor, project_id, status="", priority="", assignee_owner_id="", query="") -> list[ticket]
- create_ticket(actor, project_id, title, description="", acceptance_criteria="", priority="normal", assignee_owner_id=None, idempotency_key="") -> ticket; creates its dedicated conversation atomically with exactly project participants. Archived project rejects new tickets.
- get_ticket(actor, ticket_id) -> {ticket,project}; ticket.thread_id gives existing get_thread/post_message discussion and attachments.
- update_ticket(actor, ticket_id, expected_version, **changes) -> ticket; allowed changes title,description,acceptance_criteria,priority,assignee_owner_id,status,outcome,evidence,release_claim. Explicit assignee null clears assignment. Reassignment by human clears prior claim. Explicit release_claim allowed for current claimant or human; never silently replace another active claim. Mutation increments version atomically.
- claim_ticket(actor, ticket_id, expected_version) -> ticket; only own assigned ticket or unassigned, single human/agent claimant, repeated same claim safe, conflicting claim/version ->409. Claim assigns current owner and sets in_progress. No command execution. Agent claims require allow_requests, no implicit claim from read_inbox.

Tickets: id,project_id,number,thread_id,title (1..200),description (0..20000),acceptance_criteria (0..20000),priority,status,assignee_owner_id (null or project participant),claimant (null or author metadata),version,outcome,evidence,created_at,updated_at,url.
Statuses: todo,in_progress,blocked,review,done. Priorities: low,normal,high,urgent. Done requires nonempty outcome and evidence; no automatic completion. Reopening done clears stale completion metadata unless explicitly supplied. Assignment to another session through arbitrary input is forbidden; each agent claims as authenticated identity. Claim does not confer deployment or shell authorization. Human can release a stale agent claim from UI; there is no hidden timeout that starts competing work.

REST, existing cookie/CSRF authentication:
GET/POST /api/projects; GET/PATCH /api/projects/{project_id}; GET/POST /api/projects/{project_id}/tickets; GET/PATCH /api/tickets/{ticket_id}; POST /api/tickets/{ticket_id}/claim {expected_version}. Project/ticket PATCH {expected_version,...changes}. Query filters above. Existing /api/threads/{id} routes serve discussions. New MCP tools expose these same method names with session_id; update tools use expected_version and typed fields, preserving omitted vs null semantics. New tools are not automatically enabled in local user config. No change to existing inbox cursor delivery semantics; no unsolicited new notification dispatches.

## UI

Use existing vanilla DOM stack, no framework. Add Projets navigation and French labels for new project/ticket workflows, retaining current theme and legacy navigation. Projects list/search/create; project detail with shared brief/repository/environment and edit/archive; ticket list with status/priority/assignee/search filters, counts and creation; ticket detail with metadata editing, explicit claim/release, resolution+evidence, and access to its existing conversation for messages/files. Include obvious project/ticket links/back navigation. Preserve draft values on errors/conflict; 409 must invite reload and never overwrite silently. Responsive desktop/mobile, keyboard labels, plain text rendering (no HTML from content). Repository URLs only safe https hrefs. Existing conversations remain accessible.

## Validation

Tests on synthetic DBs: v1 migration and reopen, cross-owner membership, revoked device/session and denied agent policy, unauthorized read/write/no leaks, idempotency payload conflict and concurrent creates/claims, optimistic conflicts, assignment/claim transitions and completion evidence, unchanged existing APIs; REST CSRF/auth and MCP schemas/integration. Visual browser validation with synthetic local accounts only, no real passwords or production database. Deployment must preserve colleague modifications, backup source and DB consistently, restart only board, health check, and rollback source+DB on failure. No nginx/auth changes.
