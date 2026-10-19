# Linux input preparation — adapter 0.28, INERT

This is private controller code for preparing inputs only. It does not select,
register or qualify a backend, start a process, create a namespace, execute/import
approved scripts, advance a worker/planner or call a model. Board remains 2.13 / SQL
schema 13. The new **private journal schema 2** is a separate input-only protocol,
not a Board migration. No existing 0.24/0.26/0.27 private directory is converted or
adopted. No native 0.27 receipt or UID 1000 kernel observation qualifies this code,
a Linux backend, an owner service or UID 994.

## Concrete controller API

1. `pin_linux_input_plan(root=..., scripts_root=..., scripts=...)` explicitly
   inventories at most 16 approved Python text scripts, each at most 64 KiB,
   outside the checkout. It reads only their exact allowlist, checks ancestor,
   inode, signature, content and Unicode alias pins, and imports/executes none.
   It returns a private process/thread-bound handle; JSON cannot reconstruct it.
   Its hash covers only this input plan. It carries no runtime inventory or grant
   and cannot serve as a future execution plan.
2. `prepare_linux_input_manifest(directory, **prepare_manifest_arguments)` first
   creates an exclusively **new** directory. It uses the existing authorized
   manifest inventory with its existing bounds, then creates the input-only
   schema 2 origin. The named suite must use `linux_backend_id(plan.sha256)` and
   the exact script timeout/output limits. Any pre-existing or partial directory
   is refused, including an empty schema 1 journal. Failure retains its files;
   there is no cleanup, migration or automatic recovery/adoption API.
3. Inside the actual `broker.journal.lock()`, obtain its real
   `held_lock_lease()`. `prepare_linux_input_attempt_locked` accepts the concrete
   broker, that exact live lease, actual fence/binding/workflow, exact planner
   projection, private plan handle, named suite and idempotency key. A bool,
   copied lease, old acquisition, plan JSON or caller-asserted fence is refused.
   The returned ephemeral handle exposes a copy of the canonical request and
   packet bytes, plus two ordered one-shot **input observations**:
   `observe_boundary('before_source')`, then `observe_boundary('after_result')`.
   These names do not attest an ACK, an execution or an actual result. No raw
   snapshot/revalidation method is exposed by this API. A separate private live
   permit also limits the underlying schema2 snapshot implementation itself to
   five passes, bound to the exact durable intent, lease, fence and planner
   projection; an absent, boolean, JSON, stale or spent permit is refused before
   reading workspace contents. The ordinary schema1 snapshot API is unchanged. The handle is invalidated
   on every exit or observed mismatch and can never be reconstructed from disk.

Full current broker authorization for `repo.read_file` and `repo.run_test` is
performed before admission and freshly before **each** of five capture passes.
Callbacks are not copied into scripts or test data. The fresh journal origin,
workflow, fence hash, full plan hash, manifest/scope/planner hashes, suite limits
and private output directory identity/ancestor-chain hash are linked in intent.
The existing codec checks the actual canonical request against snapshot and
packet bytes; a shortened backend name alone is insufficient.

## Durable bounds and crash semantics

The manifest's initial explicit inventory and approved script inventory are
separate from the capture reservation. Before the first workspace capture, one
atomic state replacement commits both the intent and the entire conservative
reservation: **5 × file count × min(manifest per-file bound,1MiB)**, plus one
operation. It is deliberately not capped by the snapshot total bound: a capture
can read its last bounded file before discovering a total-limit excess. Existing
manifest budget ceilings are unchanged. Large manifests can therefore be refused
conservatively; no reservation is reduced or refunded after a failure.

Five passes are possible: constructor capture+revalidation, the two one-shot
observations, and normal context exit. Any missing, duplicate or reversed
observation refuses. A failure can use fewer passes but still costs the full
reservation. Packet/request getters re-read only the bounded private payload;
they never recapture checkout contents. Script revalidation has its own fixed
16 × 64 KiB bound and does not import code.

Schema2 accepts only the distinct `linux_input` kind, with a separate strict
schema. Its counters equal all accumulated reservations, with tests/patch bytes
zero. Before every schema 2 save, the journal checks its actual lock-state object,
live lease, private digest of the previous durable state, retained origin,
non-removal/immutability of operations, and only permitted transitions. A copied
or same-object reset cannot erase/reduce old reservations, intents or review
states. Terminal records are immutable. These are cooperating-controller
invariants; they are not protection against an administrator rolling back all
private storage or arbitrary hostile Python in the trusted controller process.

Private0600 canonical plan/snapshot/request files and the bounded input packet
are created exclusively in a new 0700 subdirectory. Inodes/hashes and all private
ancestors are revalidated. No source content, output packet or private paths are
returned by the dedicated inspector. Success writes preparation metadata, then a
terminal journal record **last**. Its only result is `prepared_input`, with
`execution_supported:false` and `adoptable:false`. It is never `passed`,
`not_started`, a cleanup receipt, or a process observation.

An exception preserves durable intent or records a bounded `needs_review` reason,
without exception/source text. An ambiguous state write is never retrospectively
completed. Crash after payload creation or before terminal state leaves review
required even if all files happen to exist. Reusing a complete attempt/key is
refused without recapture/recharge; pending/review blocks later attempts. The
read-only inspector reports only phase, identity and reserved bytes; it does not
reconcile, consume old receipts, reconstruct authority or change the journal.
The descriptor/path checks remain a cooperative filesystem boundary; portable
file operations are not a CAS against unrelated same-UID or administrative
writers. No unknown file or directory is removed.

## Consumer audit and remaining work

The old schema 1 parser rejects schema 2 immediately. Existing broker read/patch/
test/diff APIs reject this input-only profile before callbacks/effects. Broker
reconcile rejects it before authorization or checkout inspection. Planner
construction refuses it before any backend/store effect. Family audit refuses
before its authorization callback or source inspection; identity cleanup refuses
before Runtime reconciliation. Both retain their schema 1 test/proof contracts.
No decoder treats input preparation as a successful patch/test, ignores an
unsettled Linux intent, or emits a non-launch proof from missing/partial files.

Pure tests cover five actual captures and accounting, stale/copy/epoch resets,
ambiguous durable writes, partial directories, strict types, replay, revocation,
thread/PID/lease invalidation, private-payload changes during final capture, and
old consumer refusals. On Darwin the lease observations are explicit synthetic
fixtures, not a simulated successful Linux qualification.

A future lot needs a separately reviewed execution-plan/runtime inventory,
external supervisor identity and fresh OS-monotonic 1.5 s control protocol, durable
launch admission and receipts, bounded namespaces/cgroup/UID cleanup, and real
kernel **and backend** qualification on the actual owner unit. After a possible
spawn, unknown identity or missing receipts remains unknown; this input-only
record grants no exception. No selector, spawn or gate is added here.
