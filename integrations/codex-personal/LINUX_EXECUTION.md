# Explicit Linux execution profile

This profile connects the development broker, planner and family workflow to a
Linux namespace test backend. It is opt-in. Source preparation and mocked tests
do not qualify a host, CLI, service account or family. Selection remains refused
until the complete backend and planner-policy qualifications pass on the exact
installed sources and execution environment.

## Preparation and selection

Trusted local controller code calls `pin_linux_execution_plan` with an existing
isolated checkout, a separate directory of approved named Python scripts, the
exact protected controller interpreter and `unshare`. Preparation inventories
only approved scripts, public runtime/source files and the controller's own
bounded kernel metadata. It starts no process. The interpreter must be the same
resolved executable as the controller; there is no runtime or platform fallback.

`prepare_linux_execution_manifest` and `prepare_linux_backend` each require a
new directory. The manifest suites use `linux_execution_backend_id(plan_hash)`.
`prepare_linux_planner_policy` prepares a separate, initially unqualified policy
and an empty planner workspace. The backend directory must be outside the input
and script trees. None of these functions adopts or converts an old journal.

The development CLI selects both concrete factories only when explicitly given
`--execution-profile linux-python-execution-v1`. A family request must include
the same `execution_profile` on its parent and both children. Omitting this field
retains the Darwin profile. There is no selection from `platform`, a short
backend name, a model response or a historical receipt. Sealed configuration
binds the concrete pair, full plan hash, source inventory and environment.

## Journal and source boundary

The executable journal is schema 3. Darwin remains schema 1. Input admission
schema 2 and its `prepared_input` receipts remain non-executable and cannot be
adopted as either launch or non-launch evidence.

An executable attempt requires a live context bound to the concrete planner,
its durable current test step, action key, snapshot, workflow UUID, broker,
backend and fence. The broker operation ID is the hash of the action key; it is
distinct from the workflow UUID. These handles cannot be copied or serialized.

Before the first input read the same held broker lease durably charges one test
and five complete capture passes, reserving
`5 * (min(max_file_bytes, 1 MiB) + 1) * file_count` bytes. Failed attempts retain
their charge. Ordinary `read_file` and `diff` still consume their own budgets
without operation rows; schema 3 therefore does not equate counters to row count.
Intents and counters are monotone, bound to the durable state digest, and cannot
be refunded. Dispatch is durable before entering `Popen`; a write error remains
ambiguous and is never retried as a new launch.

The five captures are initial capture, initial revalidation, the requested
SOURCE boundary, post-result capture and final context exit. Every pass checks
the live lease, exact fence, grants and file identities/content. A namespace
helper waits inert for a challenge-bound parent acknowledgement before releasing
the approved script. Only the parent can collect fresh authority and capture
source. Every control window expires strictly 1.5 seconds after the beginning
of that collection. Capture, hashing, durable writes and transport consume that
window; reception never resets it. Failure to confirm continuation stops source
release or the running job. The helper has no Board or model client.
The namespace rechecks this same dated frame immediately before compiling the
held script. A later heartbeat cannot extend the SOURCE capture deadline.

For the concrete worker authority and its exact bound broker adapter, each
capture collects one complete fresh Board observation, then validates both read
and test permissions, the exact scope/resource, current claim and sealed context
from that observation. It retains no grant between captures. Local fences,
stops, budgets and the already-owned Runtime guard bracket collection and file
reads. Different or opaque callbacks retain their complete permission paths;
they are never silently replaced with a remembered decision.

The backend's own permission callback is also enforced before normal launch,
at SOURCE/result captures and during continuation. It shares an observation only
when it is the exact same concrete authority's bound adapter; a distinct opaque
callback runs in full and may refuse. Its identity is held with the other
callbacks for the single boundary. Offline qualification keeps its explicit
local callback and does not acquire a Board authorization path.

## Namespace and observations

The fixed trusted launcher uses user, mount, PID, network, IPC and UTS namespaces.
It builds a private root, detached old root, bounded read-only `/inputs`, bounded
writable `/work` and read-only public runtime mounts. The approved script gets
only sealed input/script bytes and a fixed import interface. Kernel restrictions
remain the containment boundary even if Python's import interface is bypassed.
The helper checks all kernel canaries before acknowledging readiness, closes
control descriptors before the script, drops capabilities and installs the
fixed seccomp program. CPU, memory, output, file size, descriptor, process and
wall-clock limits are fixed or bounded by the selected named suite.

The supervisor owns the launcher and receives the namespace-init pidfd over
SCM_RIGHTS. It validates live kernel parentage, PID namespace and fdinfo before
source release. Signals use owned pidfds or the exact unreaped owned child;
persisted numeric PIDs are never signal authority. Missing identity, helper
loss, ambiguous launch or incomplete cleanup retains review state.

A native exit/cleanup receipt is only an observation. It explicitly records
`broker_snapshot_admitted: false`. Only the original live broker context may
admit it after the final captures and current guards. A later lookup or replay
cannot promote it to a passed test. Identity-only cleanup after revocation may
confirm that exact historical cleanup and finish a Runtime as blocked; it does
not advance the planner, reopen a family or restore test proof. Family audit
requires the separate durable broker admission for its current inspected snapshot.

## Qualification boundary

Backend qualification requires the complete matrix: normal execution, assertion
failure, output limit, wall timeout, SOURCE revocation, EOF while running,
controller loss, supervisor loss, and replay/corruption. Loss cases use a fresh
synthetic driver and an independent observer that acquires live owned pidfds
before acknowledging the fault. There is no fault injection without that ACK.
Private exact proofs are reread when checking qualification. A six-canary subset,
a missing receipt or an unknown cleanup state never qualifies execution.

The separate Linux planner policy must demonstrate all CLI sandbox canaries,
including an actual refusal of an outside write. An old negative result is not
waived. The reviewed CLI sandbox invocation uses its normal existing configuration;
the controller neither reads nor copies authentication/configuration, and does
not invent unsupported flags. Both qualifications bind the same observed host,
UID/groups, namespaces, cgroup/label/capability/seccomp metadata and exact source
inventory. These metadata do not reconstruct every inherited kernel policy.
Qualification concerns the observed host/unit and source combination, not another
UID, unit, kernel, CLI, account, namespace or future deployment.
