# Linux foundation, step 1 — product execution remains refused

This explicit prototype measures fixed synthetic canaries in Linux namespaces.
It is separate from the backend, worker, host, model transport and normal CLI.
Every receipt retains `result: linux_unqualified` and
`backend_execution_qualified: false`, including successful canaries. It uses
neither Codex nor bubblewrap and performs no account, model or login operation.

The [19 October receipt](docs/linux-foundation-2026-10-19.json) records 27 guards
passing on x86_64 Linux 5.15.0-198-generic, with final source SHA-256
`b3cf52bf76d6cfd80651e562b761db394a3431c025f764522935cf5fe90af3cc`.
Earlier failures and an earlier successful source revision are preserved.
The product backend still refuses Linux; it consumes no qualification from this
probe.

## Measured boundary

The ordinary non-root owner starts an explicit Python probe, its owned
`unshare` launcher and one namespace-init Python process. No shell, sudo,
installer, account, service, sysctl or AppArmor configuration is changed. The
fixed launcher creates user, mount, PID, network, IPC and UTS namespaces with
private propagation and `--kill-child=SIGKILL`.

The trusted init mounts a 32 MiB tmpfs root. It bind-mounts only exact public
Python runtime files pinned by the parent as root-owned, then remounts them
read-only, nodev, nosuid and noexec. Empty public Python module files are valid.
Host UID0 is unmapped inside this user namespace: their inside recheck compares
the parent's exact device/inode, size and hash instead of treating the overflow
UID as root ownership. An aggregate SHA-256 binds 139 runtime files.

`pivot_root` moves into this root; the old root is detached and removed, and
the new root is remounted read-only. There is no `/home`, `/root`, `/run`, host
`/proc`, private state or authentication file in this view. Only `/work` stays
writable: a separate **ephemeral synthetic tmpfs**, 4 MiB and 128 inodes.
No product checkout is mounted writable. Unlike the older masked-filesystem
probe, creation at `/outside-created` fails with **EROFS (30)** instead of
succeeding in an ephemeral outside layer. The selected runtime-write probe
fails with EACCES (13); the host canary read fails with ENOENT (2).

Before fixed canary code, the init clears capability sets and bounding bits,
locks root/setuid capability semantics, sets irreversible no-new-privs and
installs an x86_64 seccomp filter. Unknown audit architectures kill the process.
The x32 range and process/network/mount/namespace/security control syscalls are
denied. The receipt checks x32, fork/clone3/exec, caps, hard limits, network
sockets, session and process-group restrictions. This is not a portable or
complete arbitrary-test syscall policy.

## Kernel identity and cleanup proof

The init sends its **own pidfd** using SCM_RIGHTS before root construction. The
parent validates nonce, kernel fdinfo, exact launcher parent relation and new
PID namespace identity before accepting it. A separate READY/ACK precedes the
canary. Inherited non-stdio descriptors, including the control socket, are
closed before the canary.

The host signals only that validated pidfd and its owned launcher pidfd. It
never passes a numeric PID from sandbox output to host `kill`/`killpg` and
never recovers a process from a stored PID. The receipt labels its kernel NSpid
pair for audit, not later cleanup. Confirmation requires the init pidfd to
become readable and the launcher to be reaped. **Without reliable init identity,
launcher exit cannot establish cleanup.**

Outside-process invisibility is checked **before** generic seccomp signal
denial: `pidfd_open` for the synthetic supervisor returns ESRCH in the new PID
namespace. A later SIGUSR1 attempt independently returns EPERM; the harmless
supervisor handler is untriggered. EPERM alone never proves disappearance.

The parent creates, reads and pins its external canary before launch. Its
device/inode, size and SHA-256 match afterward. `/work/input` is separately
pinned before the second ACK with its expected synthetic hash. Exact source
segments bind fixed canaries, namespace setup, filesystem construction,
lockdown, supervising code and cleanup. Whole-file SHA-256 covers shared
constants and remaining code.

## Bounds and gaps

Three intended probe processes run concurrently, excluding the SSH controller.
Each has a 256 MiB address-space ceiling; CPU hard ceilings are 5 seconds for
the supervisor and 1 second each for launcher/test. The stricter test ceiling
avoids raising the hard limit inherited from the launcher. Startup is bounded
to 5 seconds, source wall time to 3 seconds and the exchange to 10 seconds,
followed by bounded cleanup. Both pipes are capped at a combined 16 KiB while
reading. Core dumps are disabled, descriptors limited to 64 and file size to
4 MiB. The `/work` tmpfs also bounds aggregate space and inodes. A failed fork
guard could create one temporary synthetic child that the fixed canary exits
and reaps; the passing filter denies creation entirely.

This phase measures ordinary completion and cleanup only. It does **not**
qualify controller/supervisor SIGKILL, ignored-TERM timeout, every startup crash
window, durable receipts/restart/reconciliation, arbitrary-test imports,
another ABI/kernel/host or a real checkout. Runtime, interpreter, kernel and
probe code are trusted; the temporary fixture is exclusive to its operator.
It is not a boundary against an administrator or another process rewriting
trusted source before launch.

Step 2 requires independent review and separate explicit crash/timeout/
revocation/replay qualification with durable nonce-bound receipts. Product
integration and any snapshot-versus-real-checkout contract need their own
review. Neither installation nor this receipt enables a backend or listener.

## Explicit reproduction

Only after authorization, on non-root x86_64 Linux with Python 3.10,
`/usr/bin/unshare`, unprivileged namespaces and pidfd support, transfer the
public probe into a fresh private temporary directory and run:

```sh
/usr/bin/python3.10 -I -S /absolute/private/qualify_linux_foundation.py
```

It emits fixed checks, errno values, hashes and runtime/process metadata,
never file contents, provider output or credential environment. Exit zero
means step-1 guards passed; JSON still says Linux is unqualified. Unsupported
hosts fail closed. Do not use sudo, weaken AppArmor/sysctls or retry outside
the namespaces to obtain success.

Ordinary local tests launch no namespace or process:

```sh
cd integrations/codex-personal
python3 -m pytest -q tests/test_linux_foundation.py tests/test_linux_feasibility.py
```

The lifecycle follows the exact
[util-linux 2.37.2 source](https://github.com/util-linux/util-linux/blob/v2.37.2/sys-utils/unshare.c)
and Linux's [pidfd API](https://man7.org/linux/man-pages/man2/pidfd_open.2.html),
[PID namespaces](https://man7.org/linux/man-pages/man7/pid_namespaces.7.html) and
[seccomp API](https://man7.org/linux/man-pages/man2/seccomp.2.html). These explain
the primitives; the receipt supplies this prototype's limited measured evidence.
