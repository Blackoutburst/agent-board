# Publish an approved team plan as tickets

The `team-plan` controller uses the normal paired MCP connection of an existing
scoped Lead. It previews a proposed plan and then creates ordinary tickets with
explicit approval of that preview's SHA256. It never claims work, starts an
agent, calls a model, grants permissions, creates profiles, posts a result or
changes an existing ticket. Manager text is input data, never shell authority.

This is the first usable part of hierarchy ticket #52. It does not establish
four concurrent developers (#51), a permanent scheduler, a running manager or
writer, or a completed project. The Board schema and worker tool channels are
unchanged. The caller must already have delegated profile-read and work-request
permissions for the selected project and resources.

On a Board with companion/assigned-creation gates, the human owner must also
separately enable **Créer des tickets affectés aux agents autorisés** for this
Lead. This grants `agent.ticket.create_assigned`; base profile management and
the local plan approval hash do not imply assigned-ticket authority. Existing
Leads are not upgraded automatically. Refresh the registered session normally
after the profile version changes. The current project/resource and same-owner
destination ceilings apply to both worker slots and the exact Lead self-slot.
Without the grant, preview may read permitted sources, but publication is refused
before any ticket creation or creation-receipt replay.

## Operator documents

Save a UTF-8 `catalog.json` using actual existing ordinary profile IDs and their
current versions. The exact calling Lead may also assign work to itself: its
slot must name the same ID and current version returned by this controller's
registration. This self-slot uses only that registered profile, with the same
owner, project, resource and policy checks. It does not grant access to any other
manager profile. Repeated profiles/resources are allowed: these are assignment
slots, not concurrent execution grants.

```json
{
  "schema": "team-ticket-catalog-v1",
  "owner_id": "thescreamer",
  "slots": [
    {
      "slot_id": "developer",
      "agent_id": "EXISTING_DEVELOPER_PROFILE_ID",
      "profile_version": 5,
      "resource": "repo:agent-board-cloud-pilot"
    }
  ]
}
```

Save `plan.json` with the selected project/parent IDs and their exact current
versions. A parent groups this plan; it is not inserted as a dependency of its
children. Each `depends_on` references another step in this plan.

```json
{
  "schema": "team-ticket-plan-v1",
  "owner_id": "thescreamer",
  "project_id": "EXISTING_PROJECT_ID",
  "parent_ticket_id": "EXISTING_PARENT_TICKET_ID",
  "project_version": 1,
  "parent_version": 2,
  "steps": [
    {
      "step_id": "implement",
      "slot_id": "developer",
      "title": "Implement the selected queue adapter",
      "description": "Reuse the existing Runtime and durable journals.",
      "acceptance_criteria": "The focused integration tests pass.",
      "depends_on": [],
      "milestone": "Queue adapter"
    },
    {
      "step_id": "document",
      "slot_id": "developer",
      "title": "Document the queue operator workflow",
      "description": "Document the delivered adapter and its actual limits.",
      "acceptance_criteria": "A runnable example matches the implementation.",
      "depends_on": ["implement"],
      "milestone": "Queue adapter"
    }
  ]
}
```

All displayed fields are required; unknown fields refuse the document. Each
document is at most 128 KiB, with 1–16 slots and 1–16 unique steps in a closed
acyclic graph. Titles are at most 200 UTF-8 bytes, descriptions and acceptance
criteria 4,000 each, milestone names 80; leading/trailing whitespace and control
characters are refused. Description and milestone may be empty. IDs are bounded
opaque strings; parent/step IDs must also fit their prefixed 200-character
labels. Secret-shaped content is refused. Duplicate dependencies, duplicate
slot IDs, reserved resources, stale versions and unavailable profiles refuse.

`milestone:<name>`, `team-plan:<hash>`, `team-parent:<parent ID>` and
`team-step:<step ID>` are ordinary labels. They do not introduce a native Board
milestone entity or prove that an executor is running. This projection schema
is deliberately separate from the existing private `team-manager-plan-v1`,
whose catalog has different uniqueness rules.

## Preview, then explicitly apply

Use an existing Lead connection file kept private on this machine. Do not put its
token in these documents, command arguments or the Board. Pick a dedicated,
stable controller session and keep it for both commands and later recovery.
From the repository's `integrations/codex-personal` directory:

```sh
python3 -m blackoutburst_agents team-plan preview --connection-file /absolute/private/lead.json --session team-plan-controller-01 --plan /absolute/operator/plan.json --catalog /absolute/operator/catalog.json
```

Inspect `effects`, `order` and `approval_sha256`. The fingerprint covers the
whole normalized plan/catalog, the project and parent content/versions, the
selected Lead/session and the current profile/policy context. Normal presence,
project ticket counts and last-seen timestamps do not change the fingerprint.
Preview creates no local journal or tickets, but normally registers the selected
non-autonomous controller session and reads its permitted context.

Apply with the exact approved fingerprint and a new private journal directory
whose parent already exists. The controller creates that directory with mode
0700; files are mode 0600. Keep the same absolute directory for every recovery.

```sh
python3 -m blackoutburst_agents team-plan apply --connection-file /absolute/private/lead.json --session team-plan-controller-01 --plan /absolute/operator/plan.json --catalog /absolute/operator/catalog.json --journal /absolute/private/team-plan-01 --approve-sha256 COPY_THE_EXACT_PREVIEW_SHA256
```

The controller takes a local lock and persists the approved intent and each
pending creation before calling `create_ticket`, in deterministic topological
order. Stable keys bind the full original payload at the Board. If a reply is
lost, repeat the exact apply command: the same key/payload retrieves that
ticket's current identity, including when somebody has already progressed it.
Confirmed receipts must have distinct ticket IDs, numbers and discussion IDs
before any further creation. Children are read by their recorded IDs and are never rewritten,
reassigned or recreated. A changed plan, context, profile, policy, collision or
damaged journal refuses with `needs_review`; do not discard its intent to retry.

An incomplete application can therefore leave useful children visible. A
failure does not roll them back or imply that their work has stopped. A complete
application means only that the plan's tickets exist, not that they are done.
