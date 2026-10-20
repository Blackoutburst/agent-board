# Explicit local catalogue source transition

`CatalogSourceTransition` in `blackoutburst_agents/catalog_source_transition.py`
uses existing, already-open journals and the real Host owner lock. It never
opens a path, constructs a journal, starts a thread/service/worker, opens a feed,
registers a model session, invokes a provider or changes private configuration.
The protected caller must inspect existing journal headers before constructing
the handles. The first operational wrapper is scoped to the known schema-2
message host; this library does not qualify universal ticket/family cleanup.

The constructor takes `(host, client, *, source_guard, stopped_guard,
abandonment=None, executed_disposition=None)`. Guards are trusted code supplied by the protected entry,
not serialized attestations. `source_guard(old_release, new_release, config_sha)`
must verify both exact protected public manifests and their source/tool/runtime
pins and the inactive unit observations, and return literal `True`. `stopped_guard()` must verify both owner units
remain inactive, Host/targets off, and actual stopped-child dispositions. PID
absence alone is insufficient. In the known recovery it must use Fresh's exact
completed message-child checkpoints and admission seal, without touching or
pretending to clean the old unrelated Mac Runtime. Every RPC is followed by
these guards again. The full stopped-child guard runs immediately before the
final local transaction. Sources/units and the Host CAS are rechecked inside
that transaction; the child helper is not called with any open SQLite
transaction, as required by its own contract. The owner lock remains held.

## Closed API

* `plan(*, old_release_sha256, new_release_sha256, request_key)` returns a closed
  proposal: `schema`, `request_key`, `host_id`, `host_instance`,
  `configuration_sha256`, `old_release_sha256`, `new_release_sha256`,
  `old_catalog`, `new_catalog`, `preserved_sha256`, `closures`, `proposal_sha256`.
  The two catalogues are recomputed from the unchanged Host configuration and
  the exact release digests; IDs/profiles/versions/modes cannot be edited.
* `resolve(proposal, *, expected_revision=0)` is a first explicit attempt only.
  An existing intent requires explicit `resume`; it does not reset or re-key it.
* `status(intent_id)` returns only `intent_id`, `revision`, `phase`,
  `proposal_sha256`. It performs local guards, not a network poll or activation.
* `resume(intent_id, *, expected_revision, expected_proposal_sha256)` checks
  both exact values before effects and makes one finite attempt.
* The final receipt contains only `schema`, `proposal_sha256`, `catalog_version`
  and `instances_sha256`. It describes the completed source transition, never
  current permission to run. A later legitimate Host mutation can invalidate
  an old proposal's local guards; no old receipt overrides that fact.

The intent ID is `source-` plus `digest(request_key)`. Revision/phase is derived
from durable states: 1 `stop_pending` (local intent), 2 `replace_pending`
(remote effect pending), 3 `commit_pending` (remote receipt), 4 `complete`
(local binding committed). No mutable revision field or new DDL is introduced.
The local `catalog_source_transition` method is **never an MCP tool** and can
neither be sent by ordinary outbox recovery nor superseded.

## Durable boundaries

Before the first Host mutation all targets/control must be off, all dispatches
complete. V1 requires all commands/effects settled. V2 may first bind only exact
pending human Stop commands and their permitted ACK/stopped-cleanup effects, using
the read-only stopped-control validator. Its executed child closure has already
pinned those rows. No Stop is applied or acknowledged before the source intent,
and the final source commit remains strict about settled commands/effects.
An explicit abandonment is a completed
historical disposition with its exact receipt; it is not fake Runtime cleanup.
The proposal retains all dispatches, configuration, subscription/target receipts,
activation IDs and remaining budgets. Only genuine Stop settlement can advance
a target generation/reason before source replacement.

Under the same owner lock and Host transaction, all not-yet-consumed Fresh
abandonments must be complete and revalidated with the actual four-journal
coordinator `status`/`proposal`. Their IDs/revisions/proposal digests are then
recorded in the immutable local intent before subsequent outbox work. The old
Fresh Host checkpoint is historical from that point and is never rewritten or
revalidated by an old `resume`. The stopped-child guard remains current.
For an exact managed Host2, the same transaction creates Fresh's exact empty
ledger via `_abandon_schema(create=True)`, promotes to Host3 and inserts the
intent. Unknown tables/triggers/shadows, incomplete recoveries, conflicting
configuration or missing bound instance IDs refuse; promotion does not invent
an abandonment. Host3 uses its existing exact ledger.

The private `_finite_stop` first requires this exact pending local intent.
It polls the old mapping and accepts only real current human Stop commands with
`desired_state=stopped`. There is no generated Stop/Start/Restart request.
It queues/applies only Stop, ACKs through the ordinary durable outbox, and
reports stopped/cleanup only after the current stopped-child guard. Existing
outbox retries are filtered to exact current Stop ACKs or stopped/cleanup
reports; historical ready/start effects refuse. It never calls `step`,
`_remote_pending`, a supervisor or a background worker. Bounds are at most
16 targets, 33 pending effects and five polls per attempt including the final
post-replacement poll; there is no wait loop or automatic HTTP retry.

Only after a fresh poll shows every Stop complete, stopped and cleanup-confirmed,
and all other effects settled, a second effect `replace_server_catalog` stores
its final host/catalog versions, fence, entries and idempotency key. A timeout
keeps this exact request pending. An explicit resume replays that same request;
it never changes its CAS under the same key. The historical reply is followed
by a fresh poll, current owner/profile permissions, exact host/catalog versions,
IDs, generations, new launch hashes and unchanged Stop fence. Drift leaves the
local intent pending and makes no attempt to roll back Board or restore a DB.

The final SQLite transaction changes only `managed_settings.catalog_sha256`
and `managed_targets.launchspec_sha256` and completes the local intent together.
All other state is preserved. Post-write Host/source/unit checks run before commit; any
contradiction rolls back this local transaction. There is no budget refill,
feed reset, config replacement, instance rebinding or activation.

## Startup after transition

`ServerController.publish` with all instance IDs already bound polls freshly,
checks the exact Host/IDs/profile/version/mode/hash mapping and returns that
binding. It never fabricates a `publish(CAS0)` effect from a replacement receipt.
All blank IDs retain the actual bootstrap path; mixed IDs refuse. Worker startup
checks pending local transitions before creating its thread and again under the
real owner lock. Start/Restart application is also guarded, while genuine Stop
remains possible. Ordinary outbox recovery refuses the local purpose.

The new instance initially has observation unknown/cleanup false while retaining
its historical complete Stop with the old launch hash and old instance version.
A newly started controller can report the new instance stopped/clean through
current CAS without reapplying that old Stop or admitting work. Only a later
new human Start using the new hash and generation can activate it.

Tests use actual temporary Board/Host SQLite plus deterministic workers and
trusted fixture guards. They do not qualify installed protected sources, real
systemd units, private child journals, providers or the future CLI wrapper.
Source versions/manifests and root-owned installation remain separate work.
