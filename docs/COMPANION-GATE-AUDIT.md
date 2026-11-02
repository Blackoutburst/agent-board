# Board task50 — companion owner-power gate map

Read-only orientation. Checkout: `work/agent-board-task50`, main `b310da0575dc1c70c03b0162b670dfb7d539db08` (release.py writeability fix). Code is **2.18.0 / schema18**, including task51 ticket resolutions (`app.py:1267`, `board.py:191`). Task50 must build on that work as **schema19**. README/CONTRACT still claim schema17 and are stale; code owns the migration fact. No repository AGENTS.md was present; session AGENTS instructions apply. Read README, CONTRACT, PLAN, architecture, delegated-management and workflow docs; PLAN.md is the original historical plan rather than a current implementation description.

No source/test changes, live Board operation, claim, deployment, provider operation, credential or private owner file was performed for this map.

## Task50 boundary

- One current companion identity per owner, human-created/paired/deleted. A companion is an **agent transport**, with server-derived `companion_for`/acts-for-owner metadata; it must never be normalized into a human actor.
- Owner project visibility may bypass the companion profile's project filter, while owner membership, live owner enabled/read/reply/requests switches, device/binding validity and profile active/version fences remain effective.
- Explicit owner powers: dispatch; assign/reassign/release; resolve requests addressed to its owner; accept/reject review; pause/resume owned profiles; toggle project automation. Target-owner/project and normal CAS, retry, dependency, release/cleanup conditions remain checked.
- No ticket/task claims, even for review or the human destination. No creation of any agent/companion. Creation/pairing/deletion, binding changes, permissions/auth/settings/account and project deletion stay human-only. Operations not named above do not acquire a new human-equivalent shortcut.
- Preserve `_human()` unchanged. Human REST authentication/CSRF stays human-only; expose bounded companion operations through authenticated agent/session paths rather than accepting agent bearer tokens as browser users.

## Authoritative identity and policy sources

| Source | Meaning / required treatment |
| --- | --- |
| `board.py:77`, `:81`, `:84` | Owners, revocable devices, registered sessions are distinct. Existing sessions gain profile identity through migrations; no caller chooses an owner. |
| `profiles.py:42`, `:45`, `:48` | Profiles, explicit `(device, kind, identity_key)` bindings and immutable profile versions. Add companion authority here through schema19; ordinary profile CRUD must not accept a companion marker. |
| `profiles.py:24`, `:31` | Central insertion and version snapshot. Fresh-database/legacy migrations call insertion before schema19 exists, so avoid making old migration helpers require a new column prematurely. |
| `board.py:251` | `_actor()` authenticates owner/device, checks live owner policy and registered session, and calls profile authorization. Drop supplied companion/acts-for claims; rederive the marker from the bound SQL profile here. |
| `profiles.py:338` | Profile active state, pinned configuration version and permission switches. Preserve these for companions. A fixed power set is not a permission-revocation bypass. |
| `profiles.py:353`, `:362` | Profile project/thread filters. These are the narrow visibility extension points for a verified companion; owner membership queries remain mandatory. |
| `profiles.py:382` | Effective owner/profile intersection. Preserve enabled/read/reply/requests revocations; fixed owner powers must not become arbitrary delegated grants. |
| `board.py:296`, `:307` | Author and actor/idempotency provenance. Keep `kind=agent`, device/session/profile metadata and add `companion_for` only when verified. Existing ordinary actors and keys should remain byte-compatible. |
| `app.py:621`, `:627` | MCP authentication resolves device token then bound session. Neither tool arguments nor a raw actor dict may create companion authority. |
| `app.py:1240`, `:1258` | Human browser auth / attachment token split. Keep browser auth strict; shared attachments continue normal membership checks. |
| `profiles.py:92`, `:115` | Human profile management view exposes bindings/active work, while runtime profile view omits those. Do not turn a pause/resume capability into unrestricted settings/binding disclosure. |

Suggested central predicate: `has_owner_power(actor, power, owner_id)`, consuming only an actor returned by `_actor()`. It should require a known fixed power, matching authenticated owner and server-derived companion marker. Human callers may retain their ordinary matching-owner authority. A caller-provided marker is never sufficient. Shared-domain callers must validate the actor before consulting this predicate.

Approved foundation contract: `agent_profiles.kind` is immutable `agent|companion`, default `agent`, with an owner uniqueness index for every companion including archived profiles. Humans resume the same historical companion rather than creating a replacement. Profile JSON reports `kind` and computed `acts_for`; resolved transport actors add only SQL-derived `companion_for`. The four fixed helper powers are **dispatch**, **answer_requests**, **accept_reviews**, **manage_agents**. Companion creation defaults to owner-wide project scope, nonlegacy, read/messages/requests enabled, autonomy requested and project creation explicitly false. Owner and profile permission revocations, status/version and live binding still apply.

## Existing work gates that need narrow changes

| Pointer | Decision |
| --- | --- |
| `projects.py:270` | Extend project automation to the verified companion owner power; keep member/version checks. This does not grant project deletion/archive or arbitrary settings powers. |
| `projects.py:358`, `:361`, `:365` | Dispatch/review/reassignment hits the generic exact-claim/live-lease guard **before** human-only assignment clauses. A bounded owner-power edit must bypass only the relevant work-management requirement, without granting a claim or arbitrary worker completion. |
| `projects.py:376`, `:391` | Extend owner/agent assignment to companion work-management authority after current target eligibility and CAS checks. Preserve automatic exact claim clearing and audit notices. |
| `projects.py:397` | Owner-side release is an explicit mutation. Preserve progress/results/dependencies, exact version and any Runtime/cleanup fencing; do not treat lease expiry as cleanup. |
| `projects.py:408`, `:414` | Review return/reopen and completion/resolution logic. Keep task51 completed/superseded/wont_do semantics and required reason/evidence. Companion review decisions must work without a worker claim and retain agent provenance. |
| `coordination.py:472`, `:489` | Current ordinary-agent reviews require a claim and exact `ticket.review` grant. Do not reuse the review-claim path for the companion; route its bounded owner accept/reject decision separately. |
| `coordination.py:1057`, `:1065` | Allow own-addressed request resolution only; preserve target-owner check, dependencies, current assignment and idempotent already-resolved behavior. Replace the literal “Human response” label when the author is a companion; never report it as an actual human approval. |
| `profiles.py:218` | Allow only bounded owned-profile pause/resume (`active`/`paused`) through a new owner-power operation. Do not expose the full profile-edit field set, archive, permissions or binding changes to this path. |
| `profile_management.py:154`, `:170` | Existing delegated management is not a loophole: a companion must never create profiles, and must not acquire unrestricted profile editing/archiving via inherited management grants. Ordinary authorized Leads keep their existing behavior. |
| `profiles.py:317` | Compatibility registration can create a first ordinary profile on an unmanaged device. A companion pairing/binding must make its device managed and must not allow a new key/client kind to create another identity. Test this fallback explicitly. |

### Explicit no-claim coverage

Fence the resolved companion before every claim implementation: `board.py:774` legacy `claim_task`; `coordination.py:545` public ticket claim and its `_claim_ticket_tx` at `:508`; `coordination.py:877` queue selection; base `projects.py:511` ticket claim. Keep `_same_claim` (`projects.py:87`) exact: making it human-like would authorize renewals/completion outside task50. Legacy human-destination eligibility (`board.py:748`) and `_task_authorized` (`:763`) must not become companion claim authority. Delegation family claim checks (`delegation_families.py:130`, `:353`) and admission paths should continue requiring actual permitted worker claims, never companion owner powers. `renew_ticket` (`coordination.py:695`) and legacy `update_task` (`board.py:811`) remain worker-claim operations.

Existing `release_ticket` (`coordination.py:551`) releases only this agent/session or its waiting assignment. Companion release of another agent's work needs the bounded owner-side release route; do not globally loosen that method's exact-source assertions. `reconcile_profile_claim` (`:620`) and delegated expired release (`profile_management.py:208`) remain distinct prior-effects protocols.

## Migration, compatibility and rollback

Use one authoritative owner/profile association with a database uniqueness constraint, not a display role/name or JSON preference. Ordinary existing profiles must migrate to no companion authority, without changing IDs, bindings, claims, histories, cursors or existing grants. Human companion creation must be transactional, including cardinality and immutable initial snapshot; subsequent human pairing remains explicit. Retries must not create a second identity. The approved archived-inclusive cardinality retains the historical companion and its audit provenance; human archival does not free a replacement slot.

Update the constructor's maximum schema (`board.py:71`), add a transactional18→19 migration after the task51 resolution migration (`:191`), and update both exact schema allowlists: `server_provisioning.py:109` and `server_messages_append.py:187`. Those helpers run during Board construction; overlooking them makes every migrated startup fail. Preserve both request histories, barriers, refusal records and exact append proofs.

Profile snapshots (`profiles.py:31`) and refresh comparison (`profile_refresh.py:24`, `:102`) need compatible handling of an absent ordinary marker versus new null/default metadata. Do not automatically classify ordinary sessions as companions, silently repin old operations, or edit published historical snapshots. Old runtime/client closed schemas must not receive new fields on ordinary records unnecessarily. Compact actor/profile projections and request/event hashes need review when companion metadata is added.

`backup.py:29` captures user_version and schema digest generically; preserve and test schema19 backup/restore. Rollback uses schema19-compatible source. Schema18 binaries explicitly refuse a newer DB; do not reset user_version, delete the companion association or restore an old DB to make old code run. Update health/version, docs and schema-sensitive tests together. No migration or install may create a companion, grant permission, pair a device, claim work or start a runtime automatically.

## Proposed parallel ownership

- Foundation owner: `board.py`, `profiles.py`, `profile_management.py`, central owner-power helper, migration compatibility allowlists/tests. Own legacy `claim_task` denial and agent-creation loophole tests. Coordinate the two small schema allowlist edits explicitly.
- Root work owner: `projects.py`, `coordination.py`, `app.py` MCP/models/routes, work/review/request tests, base/queue ticket claim denial.
- UI owner: `web/workspace.js`, `web/app.js` only if required for provenance, focused browser/VM tests; consume the agreed human-only companion lifecycle and bounded owner-work APIs. Never infer authority from display labels.
- Root integrates `mcp_views.py`, docs/health/catalogue counts and full regression review after the helper/schema contract is agreed.

The following exhaustive inventories list direct human assertions, raw human-kind branches and every browser `Depends(human)` route in root application modules at this exact commit. Route authentication stays human even when the corresponding domain operation gains a separate bounded companion MCP path.


## Exhaustive direct `_human()` call inventory

| Gate | Operation | Task50 decision |
| --- | --- | --- |
| `board.py:386` | `get_settings` (definition `board.py:384`) | Keep strict human-only; no new companion power for this full operation. |
| `board.py:403` | `set_policy` (definition `board.py:397`) | Keep strict human-only; no new companion power for this full operation. |
| `board.py:425` | `create_device` (definition `board.py:419`) | Keep strict human-only; no new companion power for this full operation. |
| `board.py:437` | `rename_device` (definition `board.py:432`) | Keep strict human-only; no new companion power for this full operation. |
| `board.py:448` | `revoke_device` (definition `board.py:446`) | Keep strict human-only; no new companion power for this full operation. |
| `board.py:659` | `mark_read` (definition `board.py:657`) | Keep strict human-only; no new companion power for this full operation. |
| `communication.py:104` | `inbox_snapshot` (definition `communication.py:102`) | Keep strict human-only; no new companion power for this full operation. |
| `coordination.py:781` | `rename_agent` (definition `coordination.py:778`) | Keep strict human-only; no new companion power for this full operation. |
| `coordination.py:1060` | `resolve_human_request` (definition `coordination.py:1057`) | Allow companion owner power only for requests addressed to its own owner. |
| `coordination.py:1172` | `create_pairing` (definition `coordination.py:1163`) | Keep strict human-only; no new companion power for this full operation. |
| `delegation_families.py:463` | `stop_delegation_family` (definition `delegation_families.py:459`) | Mixed human/executor cleanup route; preserve existing executor identity path, no new companion owner privilege. |
| `notifications.py:63` | `status` (definition `notifications.py:61`) | Keep strict human-only; no new companion power for this full operation. |
| `notifications.py:116` | `test` (definition `notifications.py:114`) | Keep strict human-only; no new companion power for this full operation. |
| `profile_management.py:301` | `set_agent_profile_claim_management` (definition `profile_management.py:296`) | Keep strict human-only; no new companion power for this full operation. |
| `profile_management.py:391` | `set_agent_profile_ticket_resource_management` (definition `profile_management.py:386`) | Keep strict human-only; no new companion power for this full operation. |
| `profile_management.py:418` | `set_agent_profile_management` (definition `profile_management.py:413`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:170` | `list_agent_profiles` (definition `profiles.py:166`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:176` | `get_agent_profile` (definition `profiles.py:174`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:182` | `get_agent_profile_version` (definition `profiles.py:179`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:191` | `create_agent_profile` (definition `profiles.py:189`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:223` | `update_agent_profile` (definition `profiles.py:218`) | Bounded companion exception for owned ordinary-profile active/paused only; full editor remains human. |
| `profiles.py:236` | `duplicate_agent_profile` (definition `profiles.py:232`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:259` | `bind_agent_profile` (definition `profiles.py:254`) | Keep strict human-only; no new companion power for this full operation. |
| `profiles.py:285` | `unbind_agent_profile` (definition `profiles.py:279`) | Keep strict human-only; no new companion power for this full operation. |
| `provider_onboarding.py:47` | `overview` (definition `provider_onboarding.py:37`) | Keep strict human-only; no new companion power for this full operation. |
| `runtime.py:822` | `get_runtime` (definition `runtime.py:820`) | Keep strict human-only; no new companion power for this full operation. |
| `runtime.py:840` | `update_runtime_limits` (definition `runtime.py:834`) | Keep strict human-only; no new companion power for this full operation. |
| `runtime.py:855` | `stop_execution` (definition `runtime.py:851`) | Keep strict human-only; no new companion power for this full operation. |
| `server_instances.py:332` | `list_server_hosts` (definition `server_instances.py:330`) | Keep strict human-only; no new companion power for this full operation. |
| `server_instances.py:339` | `get_server_host` (definition `server_instances.py:336`) | Keep strict human-only; no new companion power for this full operation. |
| `server_instances.py:347` | `create_server_host` (definition `server_instances.py:342`) | Keep strict human-only; no new companion power for this full operation. |
| `server_instances.py:553` | `command_server_instance` (definition `server_instances.py:545`) | Keep strict human-only; no new companion power for this full operation. |
| `server_instances.py:581` | `revoke_server_host` (definition `server_instances.py:578`) | Keep strict human-only; no new companion power for this full operation. |
| `server_messages_append.py:276` | `_messages_append_host` (definition `server_messages_append.py:272`) | Keep human branch strict; preserve separately authenticated manager=True device branch. |
| `server_provisioning.py:166` | `_provisioning_host` (definition `server_provisioning.py:165`) | Keep strict human-only; no new companion power for this full operation. |

Total: 35 direct assertions, including mixed human/executor helper branches. The central `_human` definition at `board.py:283` remains strict.

## Exhaustive raw human-kind branch inventory

These include provenance/eligibility branches, not only permission checks. Preserve literal human provenance; extend only the explicitly allowed work gates listed above.

| Branch | Function | Existing condition |
| --- | --- | --- |
| `board.py:252` | `_actor` | `not isinstance(actor, dict) or actor.get('kind') not in ('human', 'agent')` |
| `board.py:255` | `_actor` | `actor['kind'] == 'human'` |
| `board.py:285` | `_human` | `actor['kind'] != 'human'` |
| `board.py:369` | `authenticate` | `kind in (None, 'human')` |
| `board.py:666` | `_destination_owner` | `kind in ('human', 'group')` |
| `board.py:719` | `_task` | `row['destination_kind'] in ('human', 'group')` |
| `board.py:750` | `_eligible` | `actor['kind'] == 'human'` |
| `board.py:752` | `_eligible` | `kind == 'human'` |
| `board.py:764` | `_task_authorized` | `actor['kind'] == 'human'` |
| `board.py:870` | `read_inbox` | `actor['kind'] == 'human' and reader_id` |
| `communication.py:81` | `_inbox_targets` | `actor['kind'] == 'human'` |
| `communication.py:97` | `_addressed_messages` | `actor['kind'] == 'human' and author['kind'] == 'human' and (author['owner_id'] == actor['owner_id'])` |
| `mcp_views.py:323` | `get_text` | `kind == 'human_request'` |
| `message_feed.py:77` | `_feed_pending_messages` | `message is None or not self._profile_thread_allowed(db, actor, message['thread_id']) or json.loads(message['author']).get('kind') != 'human' or (not targets.intersection(json.loads(message['mentions'])))` |
| `profiles.py:354` | `_profile_project_allowed` | `actor['kind'] == 'human'` |
| `profiles.py:363` | `_profile_thread_allowed` | `actor['kind'] == 'human'` |
| `projects.py:270` | `update_project` | `'automation_enabled' in changes and actor['kind'] != 'human'` |
| `projects.py:376` | `update_ticket` | `actor['kind'] != 'human'` |
| `projects.py:391` | `update_ticket` | `actor['kind'] != 'human'` |
| `runtime.py:427` | `_runtime_review_authorize` | `type(origin) is not dict or origin.get('kind') not in ('human', 'agent') or (origin['kind'] == 'agent' and (not isinstance(origin.get('agent_id'), str))) or (origin.get('agent_id') == profile['id']) or (claimant.get('agent_id') != profile['id']) or (type(claimant.get('profile_version')) is not int) or (claimant['profile_version'] != profile['version']) or (type(ticket['claim_started_version']) is not int) or (ticket['claim_started_version'] <= 0)` |
| `runtime.py:481` | `_runtime_message_authorize` | `not isinstance(author, dict) or author.get('kind') != 'human' or (not set(self._inbox_targets(db, actor)).intersection(json.loads(message['mentions'])))` |

## Exhaustive browser human-dependency inventory

Every route below retains `Depends(human)` and browser CSRF/origin checks. Companion semantics use agent/MCP entry points; browser bearer-auth relaxation is not required. Dispatch/review/assignment/request/status/automation domains have only the bounded exceptions described above; claim endpoints remain forbidden to companions. Settings/auth/bindings/grants/provider/server/runtime control routes gain no new companion authority. No project DELETE endpoint exists at this commit; do not add a companion deletion route.

| Route | Handler |
| --- | --- |
| `GET /api/notifications` | `notification_status` at `app.py:1270` |
| `POST /api/notifications/test` | `notification_test` at `app.py:1274` |
| `POST /api/logout` | `logout` at `app.py:1327` |
| `GET /api/me` | `me` at `app.py:1342` |
| `GET /api/threads` | `threads` at `app.py:1347` |
| `POST /api/threads` | `new_thread` at `app.py:1353` |
| `GET /api/threads/{thread_id}` | `thread` at `app.py:1357` |
| `POST /api/threads/{thread_id}/read` | `read` at `app.py:1361` |
| `GET /api/threads/{thread_id}/mentionables` | `mentionables` at `app.py:1365` |
| `GET /api/projects/{project_id}/labels` | `labels` at `app.py:1369` |
| `POST /api/projects/{project_id}/labels` | `new_label` at `app.py:1373` |
| `POST /api/threads/{thread_id}/messages` | `message` at `app.py:1377` |
| `POST /api/threads/{thread_id}/tasks` | `task` at `app.py:1381` |
| `POST /api/tasks/{task_id}/claim` | `claim` at `app.py:1385` |
| `POST /api/tasks/{task_id}/update` | `update` at `app.py:1389` |
| `GET /api/projects` | `projects` at `app.py:1393` |
| `POST /api/projects` | `new_project` at `app.py:1398` |
| `GET /api/projects/{project_id}` | `project` at `app.py:1402` |
| `PATCH /api/projects/{project_id}` | `patch_project` at `app.py:1406` |
| `GET /api/projects/{project_id}/tickets` | `tickets` at `app.py:1410` |
| `POST /api/projects/{project_id}/tickets` | `new_ticket` at `app.py:1419` |
| `GET /api/tickets/{ticket_id}` | `ticket` at `app.py:1423` |
| `PATCH /api/tickets/{ticket_id}` | `patch_ticket` at `app.py:1427` |
| `POST /api/tickets/{ticket_id}/claim` | `take_ticket` at `app.py:1431` |
| `GET /api/agents` | `agents` at `app.py:1435` |
| `PATCH /api/agents/{agent_id}` | `rename_agent` at `app.py:1439` |
| `GET /api/agent-profiles` | `agent_profiles` at `app.py:1443` |
| `POST /api/agent-profiles` | `create_agent_profile` at `app.py:1447` |
| `GET /api/agent-profiles/{profile_id}` | `agent_profile` at `app.py:1451` |
| `PATCH /api/agent-profiles/{profile_id}` | `update_agent_profile` at `app.py:1455` |
| `PUT /api/agent-profiles/{profile_id}/management` | `grant_profile_management` at `app.py:1459` |
| `PUT /api/agent-profiles/{profile_id}/claim-management` | `grant_profile_claim_management` at `app.py:1463` |
| `PUT /api/agent-profiles/{profile_id}/ticket-resource-management` | `grant_profile_ticket_resource_management` at `app.py:1467` |
| `GET /api/agent-profiles/{profile_id}/versions/{version}` | `agent_profile_version` at `app.py:1471` |
| `POST /api/agent-profiles/{profile_id}/duplicate` | `duplicate_agent_profile` at `app.py:1475` |
| `POST /api/agent-profiles/{profile_id}/bindings` | `bind_agent_profile` at `app.py:1479` |
| `POST /api/agent-profiles/{profile_id}/bindings/remove` | `unbind_agent_profile` at `app.py:1483` |
| `GET /api/provider-onboarding` | `provider_onboarding` at `app.py:1487` |
| `GET /api/server-hosts` | `server_hosts` at `app.py:1494` |
| `POST /api/server-hosts` | `create_server_host` at `app.py:1498` |
| `GET /api/server-hosts/{host_id}` | `server_host` at `app.py:1502` |
| `GET /api/server-hosts/{host_id}/provisioning-grant` | `provisioning_grant` at `app.py:1529` |
| `PUT /api/server-hosts/{host_id}/provisioning-grant` | `set_provisioning_grant` at `app.py:1535` |
| `GET /api/server-hosts/{host_id}/provisioning-context` | `provisioning_context` at `app.py:1539` |
| `GET /api/server-hosts/{host_id}/provisioning-requests` | `provisioning_requests` at `app.py:1545` |
| `POST /api/server-hosts/{host_id}/provisioning-requests` | `create_provisioning_request` at `app.py:1555` |
| `GET /api/server-hosts/{host_id}/provisioning-requests/{request_id}` | `provisioning_request` at `app.py:1559` |
| `POST /api/server-hosts/{host_id}/provisioning-requests/{request_id}/cancel` | `cancel_provisioning_request` at `app.py:1565` |
| `GET /api/server-hosts/{host_id}/messages-append/grant` | `messages_append_grant` at `app.py:1569` |
| `PUT /api/server-hosts/{host_id}/messages-append/grant` | `set_messages_append_grant` at `app.py:1575` |
| `GET /api/server-hosts/{host_id}/messages-append/context` | `messages_append_context` at `app.py:1579` |
| `GET /api/server-hosts/{host_id}/messages-append/requests` | `messages_append_requests` at `app.py:1585` |
| `POST /api/server-hosts/{host_id}/messages-append/requests` | `create_messages_append` at `app.py:1595` |
| `GET /api/server-hosts/{host_id}/messages-append/requests/{request_id}` | `messages_append_request` at `app.py:1599` |
| `POST /api/server-hosts/{host_id}/messages-append/requests/{request_id}/cancel` | `cancel_messages_append` at `app.py:1606` |
| `POST /api/server-hosts/{host_id}/revoke` | `revoke_server_host` at `app.py:1611` |
| `POST /api/server-instances/{instance_id}/commands` | `command_server_instance` at `app.py:1615` |
| `GET /api/runtime` | `runtime_status` at `app.py:1619` |
| `PATCH /api/runtime/limits` | `runtime_limits` at `app.py:1623` |
| `POST /api/runtime/executions/{execution_id}/stop` | `runtime_stop` at `app.py:1627` |
| `GET /api/delegation-families/{family_id}` | `delegation_family` at `app.py:1631` |
| `POST /api/delegation-families/{family_id}/stop` | `delegation_family_stop` at `app.py:1643` |
| `GET /api/work` | `work` at `app.py:1647` |
| `GET /api/human-requests` | `human_requests` at `app.py:1651` |
| `POST /api/human-requests/{request_id}/resolve` | `resolve_request` at `app.py:1655` |
| `GET /api/activity` | `activity` at `app.py:1659` |
| `POST /api/tickets/{ticket_id}/delegate` | `delegate` at `app.py:1664` |
| `POST /api/tickets/{ticket_id}/input` | `request_input` at `app.py:1668` |
| `POST /api/tickets/{ticket_id}/renew` | `renew` at `app.py:1672` |
| `POST /api/settings/pairings` | `create_pairing` at `app.py:1676` |
| `GET /api/inbox` | `inbox` at `app.py:1705` |
| `GET /api/settings` | `settings` at `app.py:1709` |
| `PUT /api/settings/policy` | `policy` at `app.py:1713` |
| `POST /api/settings/devices` | `device` at `app.py:1717` |
| `PATCH /api/settings/devices/{device_id}` | `rename_device` at `app.py:1721` |
| `DELETE /api/settings/devices/{device_id}` | `revoke` at `app.py:1725` |
| `POST /api/threads/{thread_id}/attachments` | `upload` at `app.py:1729` |

Total: 77 browser handlers/routes with explicit human dependency. Public login/pairing exchange and static resources are separate transport flows, not companion authority; bearer attachment routes retain actor-session validation.
