# Companion identities

A human owner can create one companion in Agents → Profiles (also linked from
Settings). The companion has its own immutable profile kind and identity. Its
display is `Name · for Owner`, with a Companion badge; transport actor kind stays
`agent`. Messages, activity and ticket history retain the companion profile,
device/session and owner attribution. The actor's `companion_for` marker is
derived from its current bound database profile, never accepted from a caller.

The four fixed owner powers are `dispatch`, `answer_requests`, `accept_reviews`
and `manage_agents`. A companion can:

- Create and assign tickets, reassign them, and release claims in its owner's
  projects, including shared-project work belonging to other members.
- Resolve Needs-you requests addressed to its owner, with a distinct companion
  response in the discussion and audit.
- Accept a delivered review using its existing outcome/evidence, or return it
  to `todo`/`in_progress`. A decision clears the review claim.
- Pause/resume its owner's ordinary agent profiles and toggle project automation.

It cannot claim tickets/tasks, submit worker progress/results, deliver work for
review, create any identities, edit grants/policies, pair/bind devices, manage
accounts/auth/settings, or delete projects. Normal agent restrictions continue
to apply. Profile pause/version, live binding and owner participation/read/reply/
request revocations remain authoritative. Owner membership always bounds reads;
a companion's scope follows its owner rather than a separate project allow-list.

Human profile creation takes optional `kind: "agent" | "companion"`. Kind cannot
be changed or duplicated into another companion. The one-per-owner database
constraint includes archived companions; a human may resume the existing identity.
Creation and pairing remain separate human actions. Companion defaults enable
read/messages/requests, requested autonomy and owner project scope; they do not
enable project creation or autonomous ticket claiming.

MCP uses existing `create_ticket`, `update_ticket`, `release_ticket`, `get_work`,
`list_agents` and `update_project` operations. `resolve_human_request` answers an
owner-addressed request. `set_companion_agent_status` accepts only an ordinary
owned profile ID, current version, `active`/`paused` and a stable retry key. Its
response and `list_agents` expose a public version for CAS, without bindings,
credentials, instructions or policy. Status changes do not stop running processes
or fabricate cleanup; owners/workers still reconcile existing effects normally.

## Schema 18 → 19 and rollback

The transactional, additive migration adds `agent_profiles.kind` with default
`agent` and a unique companion-owner index. Existing IDs, profile content/policy,
versions, bindings, claims, resolutions and immutable ordinary snapshots stay
unchanged. Reopening an upgraded database is idempotent. Backups retain schema19.

The reviewer/deployer must test a server-side copy of the live database; no live
database or private contents need to leave the server. Stop old writers before
migration: overlapping schema18 servers do not understand companion authority.
Start only schema19-compatible code afterward. Older code refuses schema19 on
startup; rollback must use compatible source and retain the upgraded database,
audit and identity records. Do not restore an old database to undo source changes.
This source change neither deploys nor creates/pairs a live companion.

The exhaustive baseline human-gate decisions are in
[COMPANION-GATE-AUDIT.md](COMPANION-GATE-AUDIT.md). Browser human authentication and
CSRF routes stay human-only; the bounded companion operations use agent MCP.

## Separately delegated assigned-ticket creation

Ordinary workers still cannot create tickets assigned to a specific profile.
A human can separately enable `agent.ticket.create_assigned` for an explicit,
scoped Management/Lead profile in its editor. Profile management, a role label,
legacy wildcard or an existing SDK plan approval does not grant this action.
It requires the exact action and owner management resource in both stored and
effective policy, plus current profile-read delegation. Existing Leads receive
no automatic grant.

The manager may create only same-owner assigned work in its current projects,
with an exact nonempty ordinary resource already allowed to both manager and
destination. Targets are bounded ordinary managed workers, or the exact current
manager itself; other managers, companions, legacy and inactive profiles are
refused. A self-slot transfers no profile-management grants. The permission
changes no reassign/release, identity, pairing, policy, Runtime or deployment
rights. Current authorization is checked before any creation receipt replay.
Human/companion owner dispatch keeps its existing scope.
