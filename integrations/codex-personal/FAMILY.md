# Explicit two-worker development family

Adapter 0.19 and Board 2.9/schema 11 provide a finite local coordinator for one
explicit parent ticket and exactly two temporary child tickets. All three slots
use the same owner, device and persistent profile, with distinct registered
sessions, private state and isolated checkouts. Each slot has an explicitly fixed
model; the controller does not select models or discover tasks automatically.
No installation, configuration change, queue, host or listener starts this path.

The family driver has been exercised against a real temporary Board with
simulated model/test transports. Separate native qualifications measure the
recorded Darwin filesystem, network and process boundaries without inference.
Neither is a real subscription development pilot or proof for another host.
Linux and Windows native development remain refused. There is no API billing
fallback, account sharing, authentication-file copy or automatic login.

## Required preparation

The operator must already hold the parent claim in its exact current session.
The parent must have the selected resource, no existing family, no unrelated
dependencies or open human request, and sufficient remaining lease. The three
sessions must already be registered to the same current profile/device. Family
preparation never registers, takes, renews or recovers a claim. Lost membership,
profile/version changes, reassignment, expired leases or pauses require review;
a new configuration cannot silently resume the old provider dialogue.

Grant the exact manifest resource and each requested action in the effective
owner/profile policy: `repo.read_file`, `repo.patch_file` and/or `repo.run_test`.
The active current profile also needs read and requested-work permission,
non-response-only autonomy and scope for the selected project. Empty or
insufficient grants refuse. Role names, ticket descriptions, model
output and digests confer no rights. Every model/broker effect uses fresh scope
and claim checks and the existing owner-wide Runtime capacity protocol.

Prepare three exclusive disposable checkouts at the same declared base. The two
child file lists must be nonempty, disjoint subsets of the parent manifest's
allowlist. Their initial bytes must match that manifest. Keep all journals,
stores, connection/configuration files, controller code, toolchain and policies
outside all three checkouts. Planner workspaces must be empty and separate.
Use owner-only 0700 private directories and regular single-link 0600 files,
without symlink path components. The native test policy covers its whole isolated
checkout, not just patch-allowlisted files; place no secrets or unrelated private
material there. The final audit covers the declared files/tests, not arbitrary
unlisted repository files or dependencies.

Prepare and explicitly qualify the parent's broker manifest, supervised named-test
backend and planner policy before `family prepare`, using the controller APIs in
[DEVELOPMENT.md](DEVELOPMENT.md), [SUPERVISED_TESTS.md](SUPERVISED_TESTS.md) and
[PLANNER_POLICY.md](PLANNER_POLICY.md). Create the children's private input
directories now, but prepare their actual manifests and qualified plans only
after `open` supplies their real ticket IDs. No family command performs native
qualification. A changed package, executable, policy or source pin requires
explicit requalification with new private plans, never editing old pins.
Requalification does not replace the plans or controller source already sealed
by an existing family; that family requires review after a software change.

From adapter 0.21, native test plans use schema/profile 2 and pin each reviewed
script separately from the manifest's model file allowlist. A trusted script
must not be included in a child's or parent's patch capabilities, including
case/Unicode aliases or the same physical file. Qualification and each new
execution verify those pins; the test executes the held verified bytes. Old
unbound plans are refused. Prepare fresh plans explicitly instead of editing old
plan hashes or widening the model's capabilities to include the check scripts.

## Private configuration

`prepare --config-file` reads one bounded private JSON file, at most 512 KiB.
Its exact top-level fields are `schema: 1`, `request` and `workflows`.
It contains no credential fields or connection token. Keep registration
responses and instructions private; do not commit or paste this file on the board.

| Object | Required fields |
| --- | --- |
| `request` | `scope`, `parent`, `children` (exactly two), `limits` |
| `request.scope` | `owner_id`, `agent_id`, `project_id`, `ticket_id`, `board_session_id`, `board_endpoint` |
| `request.parent` | `broker_directory`, `backend_directory`, `planner_policy_directory`, `model`, `instruction`, `budgets` |
| Each `request.children` entry | All parent-input fields plus `session_id`, `role`, `title`, `description`, `acceptance_criteria`, `files`, `root` |
| `request.limits` | `max_model_turns`, `max_tool_actions`, `max_concurrent`, `deadline` |
| `workflows` | Exactly `child_0`, `child_1`, `parent` |
| Each workflow | `journal`, `runs`, `runtime`, `registration` |

Each workflow's three paths select its private WorkJournal, RunStore and
RuntimeJournal. `registration` is the complete existing `register_agent` response
for that slot's exact session, not a synthesized object or another owner's copy.
The parent session is `scope.board_session_id`; children use their own
`session_id`. All three sessions must differ. The two role names must also differ.
Paths are absolute controller inputs, never paths discovered in shared messages.

Per-slot `budgets` accepts the `PlannerBudgets` fields, all strict finite integers:

```json
{
  "max_turns": 4,
  "max_actions": 4,
  "max_wall_seconds": 120,
  "max_prompt_bytes": 65536,
  "max_response_bytes": 65536,
  "max_total_bytes": 1048576,
  "turn_timeout_seconds": 30
}
```

Use reviewed values for the actual operation. Family totals include both children
and the parent. Their turn/action sums must fit `limits`; their wall allowances
plus a cleanup margin must fit the absolute Unix `deadline`, which itself must
fit the live parent lease and be within the next 24 hours. `max_concurrent` is an
upper bound, not a scheduler: this implementation runs the children sequentially.
Limits and identities are sealed; retries do not reset them or choose new work.

## Commands and phases

The common prefix is `blackoutburst-agents family --journal PRIVATE_FAMILY_DIR`.
Select that journal once. All commands below except `status` and `stop-local`
require the same private paired `--connection-file CONNECTION`.

1. `prepare --config-file PRIVATE_JSON --connection-file CONNECTION` reads the
   chosen Board scope and reviewed files/plans, seals the configuration, allocates
   a family UUID and three deterministic workflow UUIDs, and atomically reserves
   the three canonical local roots. It launches no model/test and creates no
   remote family or claim. It does reserve local roots durably.
2. `open --connection-file CONNECTION` records an idempotent opening intent and
   creates exactly two visible child tickets with parent dependencies. A lost
   response is checked by the exact opening key; no replacement children are
   created. Obtain the resulting child IDs from the authorized Board family view
   and prepare each child's manifest/backend/policy at the already pinned private
   directories. Use the real child ticket/session, approved subset of files,
   original base bytes and resource. Explicitly qualify those plans on this host.
3. `bind --connection-file CONNECTION` verifies all three actual manifests,
   scopes, inodes, file caps and supplied qualifications and seals their hashes.
   It does not generate a manifest, qualify a backend or start work. The parent's
   original qualification must remain identical. Do not call `run` before binding.
4. `run --connection-file CONNECTION` performs a finite sequence. For each child
   it prepares its exact workflow, executes the fixed-model planner, publishes
   its dated audit in review and releases that child claim. The controller then
   independently reads the declared files, test receipts and confirmed Runtime
   cleanup, and accepts the exact child result using current revisions. Acceptance
   marks that child ticket done. A model's own summary cannot accept it.
5. Once both children are accepted, the same `run` supplies bounded, inspected
   candidate replacements as untrusted data to the parent's separate workflow.
   The parent must apply/test the agreed changes in its own checkout. The final
   inspection requires the exact combined child bytes and unchanged remaining
   allowlisted files, matching passed-test proof and confirmed cleanup. Only then
   does the controller finalize the family as `review_ready`.
6. `publish-parent --connection-file CONNECTION` is a separate explicit action.
   It revalidates authority/context and publishes the parent's dated audit,
   sets the parent ticket to review and releases its own claim. Family finalization
   alone does not publish or complete the parent. No command commits, pushes,
   merges, deploys or certifies that an old inspected snapshot is still current.

Child candidate source is bounded to 8 KiB in aggregate. The family has a 32 MiB
inspection allowance, charged conservatively by 8 MiB before each audit. A crash
does not refund that charge; repeated inspections can exhaust the allowance.
Private journals have separate durable ceilings. Oversized candidates, missing tests,
changed files/context, exhausted budgets or uncertain cleanup stop for review
instead of silently truncating or treating a declaration as proof. The server
records coordination and supplied digests; it does not inspect the local checkout.

## Interruption, stop and cleanup

`status` is offline and returns bounded family metadata. `reconcile
--connection-file CONNECTION` checks already-written opening, acceptance and
finalization intents against existing receipts. It never opens a new family,
claims, runs inference, applies patches or reruns tests. An unexecuted intent
stays pending. A recovered ready phase still needs an explicit `run`; changed
authority or incompatible receipts remain for review.

`stop-local` writes an irreversible private family marker without a connection
or waiting for the execution lock. Participating workflows observe it before
further effects and through their current control checks. `stop --connection-file
CONNECTION` also signals known local workflows and requests the server stop at
the recorded revision. A lost/stale server confirmation remains pending; it
does not clear the local stop. The owner can also request server stop from the
Board card. Server stop is cooperative and cannot prove an external process died.
If opening was still unconfirmed when stop arrived, remote stop cannot yet be
confirmed either. `reconcile` may look up the existing opening; a separate
explicit `stop` can then target its confirmed identity. It never opens or retries
the opening automatically, and the local stop remains set throughout.

`cleanup --connection-file CONNECTION` uses exact existing workflow/Runtime
identities and cached completion receipts, including after normal source grants
or the claim are lost. It never infers, patches, tests, publishes or creates new
Runtime work. Missing or unknown cleanup stays `needs_review`; revoked credentials
may prevent remote confirmation. Stop or lease expiry never releases capacity
by itself, and cleanup does not clear a stop or authorize the operation to resume.

The canonical anchor remains
`~/.local/state/blackoutburst-agents/development`, derived from the OS account
database rather than `HOME`, `CODEX_HOME` or a CLI option. Format 3 stores all three
reservations under one lock and atomic replacement. Generic development bind or
attach cannot adopt them. Reserved-to-parent and parent-to-planner transfers
require the exact private write-ahead target, family, slot, session and workflow;
retries verify known prefixes without inventing targets from fence contents.
An absent active anchor or a partial family requires review, not reconstruction.

Stop older coordinators before sharing format 3; old readers refuse it. Preserve
matching adapter code, family/workflow journals, stores and canonical anchor for
recovery. Family controller source pins measure twelve explicit family/integration
modules and reject changed code on resume. They do not attest every Python import,
the running process or its OS sandbox. There is no expiry, release/rekey shortcut or
provider-session adoption. This fence coordinates one OS user and machine only;
it cannot protect against an administrator rolling back all state or coordinate
multiple hosts. Restoring state does not reverse source or external effects.
