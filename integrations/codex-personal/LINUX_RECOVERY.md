# Synthetic Linux recovery — product execution remains refused

This is an explicit step-2 experiment on the
[step-1 namespace foundation](LINUX_FOUNDATION.md), not a product backend or
planner. Every output retains `linux_unqualified` and
`backend_execution_qualified: false`. No Codex, bubblewrap, model, login,
subscription, service, account, sysctl, AppArmor or host permission configuration
is involved. Only fixed synthetic canaries execute.

The [original 19 October receipt](docs/linux-recovery-2026-10-19.json)
retains the five earlier measurements for driver `73389402…`. Its receipt
reader was subsequently found to accept boolean/float launch counters and an
incomplete `completed` observation in pure fixtures. Recomputed integrity hashes
were not enough to reject those malformed records. Those measurements remain
historical evidence, not qualification of the revised reader.

The separate [schema-correction receipt](docs/linux-recovery-schema-2026-10-19.json)
binds revised driver SHA-256
`5b4061b7d7d8b34fb8d23902445fcc6266a32b99c3ccc81ea9cdfb048a3e0b75`
and five fresh jobs. Protocol
`synthetic-linux-recovery-v2` requires exact fields and primitive types in
state, receipt, observation, admission, cleanup, namespace identity and pins.
Job UUID/nonce and public runtime/tool inventories are also strictly bounded; an
admission present on disk cannot be hidden by omitting its state reference.
No old receipt is migrated or retroactively augmented with missing evidence.
The foundation source remains
`b3cf52bf76d6cfd80651e562b761db394a3431c025f764522935cf5fe90af3cc`.

## Durable protocol

Preparation creates a new private 0700 job directory, never adopts an old one
and launches nothing. Its 0600 canonical request binds UUID, nonce, private
directory identity, selected fixed scenario, source/foundation hashes, public
runtime/tool pins, filter and hard limits. No credential path is accepted in
the runtime inventory. Files are opened without following symlinks and checked
for owner, type, link count and size. The request and directory are fsynced.

An explicit controller takes its private admission lock, rereads the state under
that lock, and records `launch_intent` before starting one helper. Concurrent
cooperating calls cannot regress a terminal state to a new launch intention.
The helper runs in a separate session with no SSH/controller stdio, acquires
the job lock, verifies the exact packet and pins, and durably records its
single namespace launch intention. An existing open intention never starts
another helper or source through `controller` or `inspect`.

The helper owns the launcher and its pidfd. The trusted init transmits its own
pidfd through SCM_RIGHTS. Nonce, kernel fdinfo, parent relation and namespace
identity are validated before admission. Numeric namespace PIDs and persisted
PID fields are audit metadata, never restart-cleanup capabilities.

After the root/guards are ready, a distinct `admission.json` is fsynced with
UUID/nonce/request/plan/source/runtime bindings, exact init identity, work-input
pin and `source_release_intent`. State references its hash before the SOURCE
ACK is sent. This is an admission/intention record, **not** a premature source
result or cleanup receipt. A crash after that intent cannot produce a
retroactive `not_started` claim. The explicit pre-SOURCE refusal fixture has
no release admission or SOURCE ACK.

The controller alone holds its heartbeat writer. The helper drains heartbeats
and EOF, with a 1.5-second freshness deadline; an EOF or expired control aborts
the attempt. Init/source never inherit this writer or reader. Source's separate
ACK descriptor is closed before fixed code, together with other non-stdio
descriptors. Queued heartbeats do not hide EOF.

Each source-start/result frame is retained in the final receipt and carries protocol, job UUID, nonce, request hash,
plan hash, driver/foundation/runtime hashes, phase and typed guard metadata.
Duplicate JSON keys, nonfinite values, wrong identities, phase ordering and
oversized output refuse the result. Receipt consumption recomputes each frame
hash, checks its exact binding/phase/27 boolean guards/15 integer errno results,
and verifies that `completed` has both frames and successful guards. Launch
counters and output sizes are exact integers, never booleans or floats; elapsed
time is finite and bounded. A `cleanup_confirmed` flag alone is insufficient:
received kernel identity, init termination and launcher reaping are separately
required, with strict boolean types and no namespace PID signaling. These are
consistency checks of trusted measured records, not protection against an
operator fabricating all their contents. A source-start frame alone is not a final
source result. Cancelled jobs therefore retain a null final-source-frame hash.

The helper independently observes init-pidfd termination and launcher reaping.
Only then can it confirm cleanup. It fsyncs a final result and its hash, atomically
publishes `receipt.json`, fsyncs the directory, then durably publishes the
terminal state and receipt hash. The final result hash binds the helper's full
observation; cancellation does not invent a final source output. Without a
reliable init handle or an exact final receipt, inspection stays `needs_review`.

Inspection is read-only: it checks the original directory, request, result,
receipt, admission and state bindings. It performs no qualification, launch,
pidfd/PID adoption, model call or retroactive proof construction. A terminal
`controller` call takes this same read-only path. Rewriting/deleting journals,
inventing another directory/identity or operator tampering is outside this
prototype's exclusive trusted-operator boundary.

## Measured scenarios

| Scenario | Observation | Receipt consumption |
|---|---|---|
| Normal fixed source | Completed; 27 foundation guards true; exact init/launcher cleanup | Stable receipt hash, one namespace launch |
| Timeout, TERM ignored | Wall deadline; TERM to init pidfd did not terminate it; SIGKILL cleanup confirmed | Cancelled supervisor observation; no final source frame |
| Controller self-SIGKILL | Only that synthetic controller kills itself after source-start notice; helper sees EOF and completes cleanup/receipt | A later SSH controller reads the exact receipt |
| Final response deliberately lost | Helper finished and fsynced; controller discards its response | New inspection consumes the same completed receipt |
| Refusal before SOURCE | Explicit stop; no SOURCE ACK/admission; owned init cleaned | Exact `not_started`, not inferred from an old intention |
| Metadata corruptions | Wrong nonce/UUID/protocol/request/plan/source/foundation/runtime, open phases and missing admission all rejected | No namespace launch or journal mutation |

Each native job has one launch intention and one validated init identity. The
five clean terminal receipts and their admission hashes were checked again
after collection. A repeated normal `controller` call returned exactly the
same receipt hash with no new launch. The remote temporary directory was
removed after metadata were preserved privately.

The Mac invoked each controller directly through SSH. No second remote Python
coordinator, killer or watchdog ran alongside it. Controller, helper, unshare
launcher and init are the entire intended chain: **three added processes outside
the controller**. After its self-SIGKILL the Mac observed SSH exit 255 with no
output, then opened a separate inspection command; it did not capture a kernel
wait status and does not present 255 as such. The exact reviewed source performs
the self-SIGKILL, and the independent helper records controller EOF and cleanup.

Limits remain 256 MiB address space each, CPU 5 s helper / 1 s launcher and
source, startup 5 s, source wall 3 s, exchange 10 s, followed by one shared 2 s
cleanup deadline. Combined output is bounded while reading to 16 KiB. Root
tmpfs is 32 MiB, `/work` 4 MiB/128 inodes; no real checkout or private state is
mounted. Runtime, libraries and tested source are public and pinned.

## What is still unqualified

Supervisor SIGKILL, every pre/post-ACK startup crash window, power loss or
arbitrary storage failure, arbitrary test/import graphs, uncooperative writers
of the private trusted source/journal, another kernel/ABI and product integration
are not certified. Early failure without init pidfd still means unknown complete
cleanup even when the launcher exits. A failed terminal-state write can leave
an existing receipt unconsumable; this remains review, not automatic repair.

The product Linux backend and planner remain refused. This proof supplies no
scope, grants, Runtime capacity or authority to a real worker. No listener,
daemon, deployment or fallback is activated.

## Explicit reproduction and ordinary tests

Only after authorization, transfer both public probe scripts to a new private
directory on the qualified non-root x86_64 Linux/Python 3.10 host. Per scenario,
use a distinct new job directory and keep it until its receipts are inspected:

```sh
/usr/bin/python3.10 -I -S -B qualify_linux_recovery.py --prepare --directory /absolute/private/new-job --case normal
/usr/bin/python3.10 -I -S -B qualify_linux_recovery.py --controller --directory /absolute/private/new-job
/usr/bin/python3.10 -I -S -B qualify_linux_recovery.py --inspect --directory /absolute/private/new-job
```

Other fixed cases are `timeout_ignore_term`, `controller_sigkill`,
`lost_response` and `refuse_before_source`. Self-SIGKILL intentionally ends its
SSH controller command; inspect afterward from a new controller. The separate
`--corruptions` command checks only an already completed normal job in memory.
Do not change a prepared request, nonce, source or limits to retry it.

The ordinary tests use only temporary metadata/fakes, without any native job:

```sh
cd integrations/codex-personal
python3 -m pytest -q tests/test_linux_recovery.py tests/test_linux_foundation.py tests/test_linux_feasibility.py
```

The revised recorded run has 44 passing pure tests, including thirteen schema/concurrency regression methods. Real scenario results and pure
tests are separate evidence; neither declares the overall project finished.
