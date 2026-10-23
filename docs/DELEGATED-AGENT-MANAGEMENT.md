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

## MCP tools

All calls require the registered `session_id`; the server derives the owner,
device and profile from authentication. Optional `view` is `full` or `compact`.
The mutation and individual read defaults preserve the exact full profile.

| Tool | Arguments in addition to session_id |
| --- | --- |
| `list_managed_agent_profiles` | `include_archived=false`, `limit=20`, `cursor=null`, `view=compact` |
| `get_managed_agent_profile` | `profile_id` |
| `create_managed_agent_profile` | `profile` JSON object including `name`, `idempotency_key` and editable profile fields |
| `update_managed_agent_profile` | `profile_id`, integer `expected_version`, `idempotency_key`, `changes` JSON object |
| `archive_managed_agent_profile` | `profile_id`, integer `expected_version`, `idempotency_key` |

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
