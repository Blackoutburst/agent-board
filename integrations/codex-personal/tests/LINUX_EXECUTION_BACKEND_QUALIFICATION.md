# Explicit Linux backend matrix qualification

This recipe has not been executed during implementation. Its tests use fake
processes, fake sockets and synthetic file fixtures. They do not produce a Linux
qualification receipt. No model, CLI, owner account, Board request, service or SSH
is part of the recipe. The planner CLI policy and owner subscription need their
own exact qualifications before a paired worker can be enabled. A false
outside-write or tool-network check in the planner policy remains a refusal.

## Scope and prerequisites

Review the complete integrated source and both new entry points:
`launch_linux_execution_backend_qualification.py` and
`qualify_linux_execution_backend.py`. Execute the launcher from externally
verified, retained bytes. It in turn verifies and retains the exact qualifier
bytes before compilation. A hash check followed by execution of the mutable
pathname is insufficient. Neither script self-attests by rereading `__file__`.

Use a fresh, uninstrumented, isolated Python 3.10+ process with `-I -B -S`, on the
explicit approved Linux x86_64 host and nonzero UID/GID. The operator supplies
exact nodename, release, UID, canonical interpreter and unshare paths and their
hashes. The backend independently observes the current host/security environment
from its own bounded procfs metadata and pins runtime, seccomp, sources and
namespace toolchain. Qualification under one UID/unit/context does not qualify
another; in particular a UID1000 experiment says nothing about UID994 or UID993.

The recipe requires the exact closed 56-file source inventory from the integrated
policy, including the matrix module, all execution components and transitive
controller/transport consumers. The source-only runner branch does not contain
all those integration modules; it cannot qualify by substituting placeholder
hashes. Inventory JSON is canonical UTF-8 without a trailing newline, with the
same `blackoutburst-linux-policy-source-inventory-v1` schema as the planner
recipe. Each file is bounded at 256 KiB and the whole source set at 4 MiB. The
qualifier imports only externally hash-pinned held bytes and requires its literal
roster to equal the product roster. No authentication home or data file is added
to that allowlist. Ancestor traversal uses retained directory descriptors, and
the child driver verifies source package/ancestor identities before reading.

The host must support the backend's reviewed unprivileged user/mount/PID/network
namespaces, pidfds, sealed memfds and seccomp profile. This recipe neither changes
kernel settings nor delegates cgroups or enables an owner service. Unsupported
features or unexpected metadata fail closed. The trusted boundary is the exact
reviewed controller, interpreter/standard library and kernel; arbitrary hostile
Python inside that same controller process is outside this contract.

## Operator argument contract

After external launcher verification, pass:

```text
python -I -B -S <held verified launcher>
  /absolute/qualify_linux_execution_backend.py <qualifier sha256> --
  --run-synthetic-backend-matrix
  --package /absolute/reviewed/blackoutburst_agents
  --source-inventory /absolute/reviewed/source-inventory.json
  --expected-source-inventory-sha256 <inventory sha256>
  --interpreter /absolute/canonical/python
  --expected-interpreter-sha256 <Python sha256>
  --unshare /absolute/canonical/unshare
  --expected-unshare-sha256 <unshare sha256>
  --expected-node <exact nodename>
  --expected-release <exact kernel release>
  --expected-uid <nonzero UID>
  --root /absolute/new-attempt-directory
  --output /absolute/new-report.json
```

The unresolved pins above deliberately make this a contract rather than an
activation command. Root/report must be new. Only fixed synthetic input and
approved controller scripts are prepared. The synthetic authorization callback
is limited to that fixture's scope/resource; it is not a Board grant and cannot
be used to admit broker tests. The interpreter, unshare tool and runtime remain
root-owned as required by the backend. Normal account/configuration files are
never read/copied and HOME is not overridden.

## Nine cases and independent interruption observations

The matrix must prove all nine cases in one fresh attempt: normal success,
assertion failure, output limit, wall timeout, source authorization revoked,
control EOF while running, controller loss, supervisor loss, and replay plus
corrupted-receipt refusal. Exact outcomes and positive cleanup are required;
six ordinary canaries cannot stand in for the two loss cases or the replay case.
All namespace guard checks must be true even for the source-revoked case.

The durable `qualification-matrix/intent.json` exists before any case starts,
and a per-case intent exists before that case. Each proof binds the current
backend path/identity, matrix identity/origin, complete request, plan,
environment and the native observation. Failed/partial matrices are retained
and never retried or adopted. Copying a previous matrix into a new directory
cannot qualify it, even if the file hashes are unchanged. Native proof hashes
are never interpreted as broker PASSED evidence.

For the two loss cases, a separate fixed synthetic driver opens the backend and
runs its qualification-only case. At SOURCE, its mandatory hook sends the exact
request/state/nonce and waits for a bounded parent ACK. The parent owns the
unreaped driver Popen and acquires pidfds while verifying the live kernel chain
`driver -> supervisor -> launcher -> namespace init`: PPid, starttime, namespace,
UID/GID and pidfd fdinfo, before/after acquisition. Unknown siblings/threads or
missing identities refuse. A numeric PID from a message or journal is never a
signal authority. No ACK means no intentional fault and no successful case.

After ACK, controller loss is the driver's own SIGKILL. The parent must observe
that exact outcome before any fallback cleanup, so a cleaner's later SIGKILL
cannot masquerade as the test. Supervisor loss kills only the exact helper Popen
owned by the driver and must return the expected uncertainty. The parent then
observes retained pidfds and reaps its own driver. Passive termination is
preferred; bounded fallback can signal only those acquired pidfds, never a PID,
process group or service. An incomplete acquisition leaves cleanup unconfirmed
and cannot qualify. No previous receipt/PID is reconstructed to recover a case.

Controller loss requires the helper's exact native cleanup receipt as well as
independent observation. Supervisor loss deliberately requires no native
receipt, retained SOURCE state and native outcome `unknown`; the independent
observer only attests that its held processes terminated. It cannot turn that
uncertainty into a product receipt or a test pass. The SOURCE protocol TTL stays
1.5 seconds; the observer does not renew or extend it. A slow acquisition is a
refusal, not an exception to freshness.

The replay case attempts the same operation directory and requires its exclusive
creation gate to refuse before Popen, with the original job/state/receipt bytes
unchanged. The native validator must also refuse a corrupted request hash.
These checks do not execute arbitrary input JSON as code.

## Evidence, retention and validation

`native_receipts` in the matrix result maps all nine names to SHA-256 hashes of
their private canonical case-proof files, not to nine successful test receipts.
Those proofs reference the native observations where present. A read-only
validator rechecks exact directory identities/origin, plan/environment/current
pins, the full nine-file proof set and native chains; it invokes no authority
callback, subprocess or signal. Missing, altered, foreign or partial evidence
fails. Historical identity-only product cleanup uses the separate backend
cleanup reader, not this current qualification validator.

The public report contains bounded hashes, observed host/UID, nine booleans and
cleanup observation. It does not copy source bytes, proc labels/cgroup paths,
requests, exception bodies or user output. It explicitly disclaims planner
policy, subscription, broker admission and service activation. Failure remains
failure even if some earlier cases were clean. All fixture directories, journal,
backend jobs and private matrix receipts are retained. No recursive deletion,
unknown-file adoption or stale database restoration occurs. Retained synthetic
files are not reported as removed.
