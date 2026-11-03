# Human-delegated Management / Lead

The role name is descriptive. Management authority comes only from an explicit
human grant, checked against the authenticated session and live owner policy on
every call. This feature does not start an agent or change an existing connection.
It adds no schema migration or Runtime capability.

## Human activation

Use a verified administrator session belonging to Blackoutburst or TheScreamer.
The production platform session, board participant mapping, Origin and CSRF
checks are the same as for the existing human profile editor. Agent bearer tokens
cannot use this route.

1. Open **Agents → New profile**, and optionally choose **Management / Lead**.
2. Select explicit followed projects. Enable read and work requests, and choose
   requested or queue work. Set the ordinary actions/resources that this Lead may
   delegate. Those grants must also be available in the account policy to become
   effective. The template changes descriptive fields only.
3. Select **Gérer les profils d’agents ordinaires** and save. This explicit action
   grants these four actions to the profile and account, preserving other grants:
   `agent.profile.read`, `agent.profile.create`, `agent.profile.update`,
   `agent.profile.archive`. It also grants the exact resource
   `agent-profiles:<owner_id>`. No wildcard or role name substitutes for these
   explicit profile grants. Legacy account-wide profiles must be replaced with a
   new scoped profile for this delegation.
4. In the existing human **Connections** flow, bind an executor/device to the new
   profile. Select that explicit profile identity in the executor/MCP connection
   and register a **new session**. Existing sessions cannot switch identity.
   Creating the profile or checking management never performs this binding.

The dedicated human operation is
`PUT /api/agent-profiles/{profile_id}/management` with
`{"expected_version": 3, "enabled": true}`. A stale version conflicts. Reload
after an uncertain response; no automatic grant retry is attempted. Clearing the
checkbox removes management grants from that profile. The account grant remains
available for other explicitly authorized Leads; legacy `*` profiles never gain
management by inheritance. Profile version changes require the normal explicit
session refresh before future work.

Claim recovery has a **separate, optional human grant**, disabled by default.
In the same profile editor, select **Libérer les réservations expirées des
agents gérés** after enabling profile management. The human-only endpoint
`PUT /api/agent-profiles/{profile_id}/claim-management` accepts the same
`expected_version`/`enabled` body. It adds only `agent.profile.release_claim`
to this profile and the account ceiling; existing Leads receive nothing
automatically. Clearing the base Management checkbox also removes this grant.
An agent cannot use a profile create/update operation to delegate it onwards.

Inactive-ticket resource routing has its own optional human grant, also disabled
by default. In the same editor, enable **Rattacher les tickets inactifs aux
espaces autorisés**. The human-only endpoint
`PUT /api/agent-profiles/{profile_id}/ticket-resource-management` accepts the
same `expected_version`/`enabled` body. It grants only
`agent.ticket.retarget_resource` to the selected profile and account ceiling.
Clearing base Management removes this permission from the profile as well.

Assigned-ticket creation has a separate optional human grant, disabled by
default. Select **Créer des tickets affectés aux agents autorisés** after
enabling profile management. The human-only endpoint
`PUT /api/agent-profiles/{profile_id}/ticket-creation-management` accepts the
same strict `expected_version`/`enabled` body. It grants only
`agent.ticket.create_assigned` to the selected ordinary scoped profile and the
account ceiling. Existing profile management and SDK plan approval do not imply
this permission. Clearing base Management removes it from the profile too.
Revocation remains available after the prerequisites were removed; grant
changes require the normal session/profile refresh before further work.

The existing `create_ticket` tool then permits assigned creation only to a
same-owner ordinary managed worker, or the exact authenticated Lead self-slot,
inside its current project and grant ceilings. The task's nonempty exact
ordinary resource must be stored and effective for the Lead and allowed by the
destination. Other managers, companions, legacy/inactive/out-of-ceiling targets
are refused. It grants no reassign/release or execution authority. Authorization
is rechecked before exact-key creation receipt replay.

## MCP tools

All calls require the registered `session_id`; the server derives the owner,
device and profile from authentication. Optional `view` is `full` or `compact`.
Profile mutation and individual read defaults preserve the exact full profile.
Claim release returns the normal compact ticket view unless `view=full` is chosen.

| Tool | Arguments in addition to session_id |
| --- | --- |
| `list_managed_agent_profiles` | `include_archived=false`, `limit=20`, `cursor=null`, `view=compact` |
| `get_managed_agent_profile` | `profile_id` |
| `create_managed_agent_profile` | `profile` JSON object including `name`, `idempotency_key` and editable profile fields |
| `update_managed_agent_profile` | `profile_id`, integer `expected_version`, `idempotency_key`, `changes` JSON object |
| `archive_managed_agent_profile` | `profile_id`, integer `expected_version`, `idempotency_key` |
| `release_managed_agent_claim` | `ticket_id`, integer `expected_version`, `idempotency_key`, nonempty `reason` and `recovery_evidence` |
| `retarget_managed_ticket_resource` | `ticket_id`, integer `expected_version`, `expected_resource`, `new_resource`, `idempotency_key`, nonempty `reason` |

Creation produces an **unbound ordinary profile**. Text, project scope, preferences,
permission switches and literal actions/resources use the existing profile
validation. Preferred model/provider names are preferences; they contain no
credential and do not connect a provider or approve a model.

Every current target and proposed result must be within the Lead's live effective
permission switches, actions, resources, autonomy and explicit projects. Project
membership is checked again. A Lead cannot manage itself, any other management
profile (including paused or revoked ones), another owner, a legacy profile or a
profile with higher grants. It cannot grant project creation, management,
wildcards, Runtime/server/provider/device/credential/policy authority. It cannot
bind a device, edit a connection identity, change account policy, assign Runtime
capacity or access execution credentials. Connection keys, bindings and active
work identities are excluded from this channel.

Each mutation needs a stable retry key. The exact original response is returned
when the same payload/key is retried, after checking current authorization and
target bounds. A retry does not overwrite a later human edit. Read the current
profile before composing another change. A different payload with that key or a
fresh mutation with an old version conflicts. Owner/profile revocation applies
before idempotent retries as well.

## Expired claim recovery

`release_managed_agent_claim` requires current read/work permissions, its explicit
stored and effective action, and the exact owner management resource. Both the
current target profile and the immutable source-session profile version must
be ordinary profiles within the Lead's current owner/project/grant ceiling.
The ticket's project and nonempty resource must remain authorized by both
profiles and the Lead. A historical Management claim cannot become manageable
merely by removing that profile's management grant later. Missing or incomplete
source identity/version records are refused.

The tool refuses human claims, live or unknown leases, completed tickets, and
**any unresolved Runtime execution on the ticket**, including another agent or
owner. A Runtime must be finished with confirmed cleanup; null/unknown values
do not qualify. If no Runtime history exists, the caller must still inspect
prior effects and supply concrete evidence. Missing records or lease expiry
alone are not proof that an OS process stopped.

On success, only the claim/lease is cleared and the ticket version advances;
`in_progress` becomes `todo`, while other statuses, assignments, progress,
results, evidence, dependencies and human requests remain intact. The operation
does not stop a process, reset Runtime capacity, resume work or complete a task.
It records the manager, source session/profile version, reason and evidence in
the audit. After a lost response, retry the exact key and payload: it returns the
current permitted ticket without clearing any newer claim. Read again before
requesting a different effect.

## Inactive ticket resource routing

`retarget_managed_ticket_resource` requires current read/work permissions,
`agent.ticket.retarget_resource` explicitly stored and effective, and the exact
owner management resource. The ticket must belong to the same owner and a
followed project. Its assignee must be unset, the Lead itself, or an ordinary
profile inside the Lead's delegated bounds. Both the original nonempty resource
and destination must already be authorized for the Lead and any managed
assignee. Reserved management, Runtime and credential resources cannot be used
as workspaces. Resource names are exact strings of at most 200 characters.

The tool refuses completed tickets, archived projects, delegation-family
tickets, any claim or retained lease, open human requests, and any unfinished or
uncertain Runtime cleanup on this ticket. An expired claim must first use its
normal recovery path. The operation changes only resource/version/time plus
audit and retry records; it keeps assignment, status, dependencies, questions,
results and evidence intact. It never stops or resumes a process, clears an old
family's fence, claims the ticket, or moves files.

An occupied destination can be selected for future work, but the normal claim
still refuses until that resource is free. Thus a Lead can move an inactive
ordinary ticket away from an old locked workspace without releasing that lock.
The caller must supply the exact `expected_version` and `expected_resource`, a
different nonempty `new_resource`, a reason and a stable retry key. Retrying the
same key rechecks current grants and requires the exact resulting ticket version,
destination and unchanged ticket/profile/project context. A later edit or claim
conflicts; a saved result never authorizes another move or an execution.

## Archive and cleanup

Archive is a reversible status change. It hides the ordinary profile from the
default list and stops its authorization for future work. Restore explicitly with
`update_managed_agent_profile` and its current version (`active` or `paused`).
Archiving requires its separate action; update cannot bypass it with
`status=archived`.

All audits, versions, connections, active claims, leases and Runtime records remain.
An archive neither kills a process nor confirms cleanup, releases claims or marks
work done. The existing restricted execution-control/cleanup channel continues
to require truthful evidence from the executor. No merge, publication or task
completion is implied by any profile management operation.
