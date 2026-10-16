# Finite development planner (not activated)

`DevelopmentPlanner` is an explicit controller API over the development broker.
It has no CLI entrypoint, listener, task picker, account fallback, board write,
publication or merge operation. The existing PersonalRunner behavior is unchanged. A disabled constructor refuses
before creating a work, journal or canonical fence; probing the default cannot
strand a checkout.
This lot has been exercised with simulated model/test work and a temporary Board;
no actual model turn or production project was used.

The production transport depends on `execute_turn(planner_policy=...)` and the
separately qualified `PlannerPolicy` API (policy commit `99f67479`). Its workspace
must be empty, private and distinct from the real checkout and every private
journal. Source reaches the model only through explicitly allowlisted broker
reads. An enabled planner requires a qualified supervised native test backend;
an unsupervised named-test backend is refused. The underscored transport, backend
and anchor fixture overrides are Python test injections, never CLI/model options.

Each model response must be exactly one JSON object, with schema 1 and one kind:

- `read`: `path`.
- `patch`: `path`, `expected_sha256`, and complete UTF-8 `content`.
- `test`: the controller-approved `name`.
- `final`: a bounded untrusted `summary`.

Extra fields, duplicate keys, markdown wrappers, unknown files/tests and secret
content are refused. Executable, model, environment, working directory, toolchain,
claim and policy come from the controller; a response cannot select them. The
same provider session is resumed for subsequent model turns. Unexpected model
transport events, missing verification, policy changes or uncertain cleanup stop
at `needs_review`.

## Sequential Runtime capacity

Model turns retain provider `codex_chatgpt`. Each local controller action uses a
separate Runtime reservation tagged `local_broker`, with the fixed label
`read_file`, `patch_file`, or the named suite. A named-test action also takes
bounded before/after hash snapshots to establish which sources were tested.
A final action reserves `read_file` for actual diff/test-receipt verification.
These are sequential reservations: model cleanup and confirmed Runtime finish
precede action admission; actual action receipt and Runtime finish precede the
next model. No reservation is nested and no refusal causes waiting, API fallback,
automatic rekeying or a quota observation inferred from local output.

RuntimeJournal remains format 1 for model-only records. Its first `local_broker`
record commits local format 2, so older readers refuse on a new opening. Stop
older coordinators sharing this journal before that first record: a process
already holding the database does not rerun its opening version check. The server
already accepts opaque provider labels; no server database migration is needed.

A trusted fresh `authorization()` callback supplies an exact manifest scope,
claim ID, four current permission assertions (owner/profile/project/claim), and
explicit action/resource grants. A model turn always requires `repo.read_file`
for the manifest resource, because its prompt contains source metadata and
possibly prior file content; removing this grant stops before/during inference.
Scope includes device, session and profile
version. The controller cross-checks it before admission and each broker effect,
alongside stop/budget and Runtime cancellation. The adapter implementing this
callback must read authoritative current state and validate the exact claim
(including its lease), membership and permissions. Ticket descriptions, comments,
model output and a cached registration cannot supply these assertions. This lot
does not implement a live MCP adapter or manufacture grants on the caller's behalf.
It freezes the local instruction and does not compose/revalidate a shared Board
context descriptor; that separate integration is required before a live pilot.

## Durable stop, budgets and canonical fence

`planner.json`, `planner.lock` and an irreversible `planner-stop.json` are fixed
names in the broker's private directory. Every step receives one UUID and stable
model request/action keys before any effect. Total turn/action/byte budgets and
an absolute wall deadline according to the trusted system clock survive reopening;
nonfinite clocks or a time before the operation start refuse. This is not an
anti-manipulation guarantee for the OS clock, and a smaller backwards adjustment
that remains after the start is not detected.
Reads and source-proof snapshots also consume the broker's existing budgets.
Oversized prompts/results refuse rather than silently truncate.

The canonical fence is derived from the OS account database, not HOME,
CODEX_HOME, CLI options or the manifest:

`~/.local/state/blackoutburst-agents/development`

Its key binds normalized MCP origin, owner/agent/project/ticket and checkout
`device/inode`. It records the broker path/inode, operation UUID and model/runtime
child scope. Another journal name, instruction, child-state directory or ticket
cannot silently restart the same checkout. Locks and path components are checked
without following symlinks. It deliberately retains all operations, including
`review_ready`, until a later explicit operator-resolution workflow; this lot has
no automatic expiry, anchor deletion or “start fresh” shortcut.

This fence is local to one OS user and machine. It does not resist an administrator
or the same user erasing private state, and provides no cross-host coordination.
The kernel still requires an exclusive disposable checkout; file rename cannot
provide a portable CAS against unrelated noncooperating writers. Only manifest
allowlisted files participate in source proof; this is not a claim about every
unlisted file or dependency of a repository.

## Recovery and review result

Ordinary `run()` retries retain the same child request/action keys. PersonalRunner
can return a completed cached model response; it cannot rerun an interrupted turn.
A tool intent lacking a confirmed finish stops for explicit `reconcile()`.
Reconciliation uses cached model text, exact broker reconciliation/backend lookup
and the existing Runtime key. It never runs inference, applies a patch or reruns
a test. Confirmed recovered work becomes `action_ready`/`ready` for a separate
explicit `run()` call. Missing intent/receipt, an unapplied patch or stale final
inspection remains review. Stops/deadlines do not prevent receipt-only cleanup
confirmation, and reconciliation cannot clear a durable stop.

`review_ready` is a dated audit receipt for the last authorized inspection, with
`evidence_kind=inspected_snapshot`, `inspected_at`, base SHA and snapshot hash. It
requires an actual diff and hashes for all allowlisted files at that inspection,
all manifest test suites passed against that exact snapshot, and rechecked durable
broker test receipts. A patch or source mutation invalidates prior test proof.
The public result contains hashes, bounded metadata and test evidence, never source,
prompts, model summary or captured stdout. Returning a cached receipt performs no fresh source validation. External writes
during/after Runtime finish can age the inspected snapshot; an eventual
acceptance/application must revalidate current hashes in its own authorized
effect. The model's final declaration cannot mark a ticket done. Human review/publication/claim release remain outside this API.

The existing native supervisor qualification covers its measured Darwin fixture
and controller-crash scenario only; it does not establish every failure window,
helper/host death recovery, another OS, or a real model execution. See
`SUPERVISED_TESTS.md` and the separate planner-policy qualification before a pilot.
