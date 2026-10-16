# Linux feasibility: measured, execution still refused

The opt-in native named-test backend still refuses Linux. This lot adds a
standalone, explicit feasibility probe and a real Linux receipt; it changes no
backend, worker, host, CLI dispatch, production service or default permission.
A Darwin qualification and a mocked Linux platform are never accepted as Linux
execution evidence.

## What was measured

The [16 October receipt](docs/linux-feasibility-2026-10-16.json) was produced as
an ordinary, non-root user on Ubuntu 22.04.5 LTS, x86_64, kernel
5.15.0-185-generic. `/usr/bin/unshare` from util-linux 2.37.2 successfully created
an unprivileged user namespace and combined user, mount, PID and network
namespaces. `kernel.unprivileged_userns_clone` was 1, the user-namespace ceiling
was 62688, and the current AppArmor label was `unconfined`. The
`apparmor_restrict_unprivileged_userns` sysctl did not exist. No sysctl, AppArmor
profile, package, service, account or system configuration was changed.

The public Codex CLI and bubblewrap helper were absent. The official
[OpenAI release rust-v0.163.0](https://github.com/openai/codex/releases/tag/rust-v0.163.0),
published 15 October 2026, supplies these assets:

| Asset | Bytes | Archive SHA-256 |
| --- | ---: | --- |
| `codex-x86_64-unknown-linux-musl.tar.gz` | 112589966 | `beb339e8ecd9ba057a6bb2dc81ad8ca474812edfbf0fc5056ca818797648dde4` |
| `bwrap-x86_64-unknown-linux-musl.tar.gz` | 261563 | `bcc196b1d25dc2c2b912dcedf780b562deb0552f3c50648761ced145c3466a23` |

The archives were fetched over verified HTTPS, checked against the official
GitHub release metadata, and extracted strictly as one regular file each in a
private 0700 `/tmp` directory. The helper lives beside the CLI under
`codex-resources/bwrap`. Archive sizes and SHA-256 were checked before any
execution; executable hashes appear in the receipt. No installer script was
executed, no system `PATH` was changed, and no binary is included in this repo.
This is an integrity check against release metadata, not a separate Sigstore
attestation verification. The tools remain temporary and contain no login.

The installed CLI reports `codex-cli 0.163.0`. Its actual host-selected command
is `codex sandbox`; the downloaded build does not need a `linux` subcommand.
Version, help, and sandbox commands were the only Codex operations used. No
`exec`, app-server, login/status, account, model or provider API was requested.
The controller never opens authentication files. The CLI receives any existing
`HOME`, `CODEX_HOME` and `TMPDIR` values unchanged, with a fixed system `PATH`;
these values are neither exported in the receipt nor replaced with another
user's home.

## Results and gaps

Twelve of thirteen guard checks passed in the same synthetic Python process:
checkout read/write, outside read denial, TCP loopback denial, Unix pathname and
abstract-socket denial, fork/spawn denial, hard process-limit protection, session
and process-group restrictions, and invisibility of an outside owned process.
The socket checks connect only to synthetic listeners made by the controller;
there is no external network probe. Each fixture uses a new private directory
and is removed afterward.

The strict **outside-write guard failed**: the process could create the selected
outside path in its private masked filesystem view (`errno` 0). The real host
outside path stayed absent and its pre-existing canary stayed unchanged. This
observes an ephemeral write, not a write to the host's outside file. It is still
a failure of the existing native backend's stronger guard contract and is not
silently reclassified as success. Any future contract accepting ephemeral
writes must explicitly distinguish them from host effects and qualify both.

The first launch failed because the CLI archive alone has no adjacent helper.
After adding the official helper, a Darwin-shaped policy containing explicit
`:slash_tmp` and `:tmpdir` denials prevented the CLI's internal re-exec in `/tmp`.
The measured Linux policy instead denies root, allows `:minimal` reads, allows
the private public-tool directory read-only, and allows only the synthetic
checkout to be written. An outside synthetic read remained hidden. This policy
is a feasibility fixture, not an adopted production profile; the precise
contents of `:minimal`, writable anonymous mounts, sockets and helper re-exec
paths need a Linux-specific reviewed contract.

Bubblewrap put Python in a PID namespace: `pid=2`, `pgid=2`, `sid=2` after the
trusted canary created its session. These identifiers cannot be passed to host
`killpg`, interpreted as host PIDs, or used for a restart cleanup decision. The
probe never signals a PID obtained from sandbox output. In a separate
five-second synthetic sleep, it killed only its exact owned `Popen` launcher
and reaped it. No descendant was captured in the launcher's observed subtree;
therefore the receipt deliberately does **not** certify complete namespace
cleanup, a durable supervisor, orphan recovery or arbitrary subprocess trees.
The intrinsic five-second sleep bounds that synthetic payload independently of
controller cleanup.

A future Linux backend needs a distinct protocol and qualification: host-owned
PID handles or verified namespace ownership, a trusted outer supervisor that
survives controller failure, exact start/ACK/finish identities and retry
semantics, fail-closed crash recovery, and OS-enforced termination of the entire
namespace. It must cover timeout, ignored TERM, controller/supervisor crash,
revocation, namespace lifecycle and bounded output without using inner PID
numbers as host signals. Binary/helper/interpreter/kernel/policy changes must
invalidate that proof. Until those guards pass, the current backend's Linux
platform refusal remains in force.

## Reproduce explicitly

The [probe](tests/qualify_linux_primitives.py) does not install or download
anything. It accepts only the exact verified CLI/helper hashes above. Run it
only on an authorized non-root Linux host with those public tools prepared in
an otherwise empty private directory:

```sh
python3 integrations/codex-personal/tests/qualify_linux_primitives.py \
  --codex /absolute/private/tools/codex
```

It prints only synthetic guard outcomes, fixed errno values, hashes and host
metadata. Exit status 1 is expected for the recorded outside-write gap;
`backend_execution_qualified` always remains false. The probe is outside test
discovery and is never invoked automatically. Its ordinary unit checks use
mocked process objects and are separate from the real receipt.

OpenAI's official [sandbox prerequisites](https://learn.chatgpt.com/docs/sandboxing)
recommend distribution bubblewrap and explain unprivileged namespace support.
The [permissions documentation](https://learn.chatgpt.com/docs/permissions)
identifies Linux bubblewrap/seccomp enforcement and refusal when the necessary
operations are unavailable. If another host refuses namespaces, record the
actual error and require an administrator-approved compatible installation or
isolated environment. Do not disable AppArmor, change sysctls, request sudo, or
retry without the sandbox to make a qualification pass.
