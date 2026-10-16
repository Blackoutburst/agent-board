# Per-attempt supervision for named tests

`SupervisedNativeNamedPythonBackend` adds an explicitly prepared supervisor to
the [native single-process backend](NATIVE_TESTS.md). It remains opt-in: no
default worker, event listener, queue or service starts it. The explicit
[development command](DEVELOPMENT_CLI.md) can use it only with separately
prepared and qualified plans and current authorization. Backend qualification
performs no model or authentication operation. Darwin is the only qualified
platform; other operating systems remain refused by the underlying backend.

The supervisor closes the unsupervised backend's controller-crash gap. A
short-lived trusted Python process owns the sandbox launch, time limit,
cancellation, process cleanup and durable receipt. It runs in a new session and
is independent of the controller that requested the test. It is not a daemon or
an automatic work listener: one helper handles one exact attempt, then exits.

## Preparation and authority

After `prepare_backend`, a trusted controller calls `prepare_supervision`. This
pins a regular non-writable-by-others interpreter, the six controller module
hashes, bootstrap hash and signal-canary hash. The helper bootstrap checks those
module hashes and compiles the exact held source bytes, using an empty package
search path. It never imports an existing package bytecode cache. Preparation
never launches a process.
The backend is refused until its separate `qualify()` succeeds for that exact
plan and supervisor revision. There is no fallback to the unsupervised backend.

The existing scope, root/inode, native CLI/interpreter hashes, profile and suite
budgets remain fixed. A private job packet additionally binds a random nonce,
the exact request hash, manifest and supervision hash. The packet and state are
0600 files in the owned 0700 backend directory outside the checkout. A per-job
lock and a prepared-only state transition prevent a second helper from starting
an existing attempt. Changing the code, request, nonce, plan or owner scope does
not authorize adoption of an old job.

The checkout must not overlap the controller package, pinned toolchain,
private journal or known connection/credential directories. Directory
containment is checked through opened ancestor identities, including Mac case
aliases, rather than path spelling alone. User-writable pinned binaries must
have one hard link. Multi-linked system binaries are accepted only when owned
by root and not writable by the non-root test UID (the macOS Python launcher is
such a system binary). Unknown boundaries are refused.

Named scripts are separately pinned by the native schema/profile-2 plan and
remain outside the manifest's model/broker patch caps. The helper revalidates
their current nofollow pins and passes exact held source bytes to the wrapper;
the wrapper digest-checks and compiles those bytes instead of reopening a path.
An old unpinned native plan or a changed script refuses. This is separate from
the six controller-module pins and from the model's data-file permissions.

The controller sends only a one-byte authorization heartbeat through its owned
pipe. The helper verifies the exact scope/action/resource and consumes that
pipe before each effect and while observing the native test. EOF cancels
immediately; absence of fresh heartbeats for 1.5 seconds also cancels. Queued
heartbeats do not mask EOF. The pipe is never inherited by the source test,
which receives its separate validated ACK gate and then `/dev/null` as stdin.
The controller keeps `HOME`/`CODEX_HOME` values for the trusted CLI without
copying authentication files; the source test still has only the fixed minimal
environment.

## A test cannot signal its supervisor

Before every supervised source script, an additional canary attempts SIGUSR1
against a separate synthetic process outside the native sandbox. That process
confirms its PID before the probe and installs a harmless handler with a
receipt. The signal must fail with EPERM/EACCES and the canary must exit cleanly
without a handler receipt. Success or ambiguity prevents source execution.

This check matters because a same-user repository test could otherwise kill
its watchdog. It is evaluated in the same native process and policy as the
source, together with all ten filesystem/network/process-limit guards. The
supervisor and its private control files remain outside the sandboxed checkout.
No real host process or service is targeted during qualification.

## Crash, timeout and recovery

The helper owns the native backend's fixed startup deadline, suite wall timeout
and TERM/KILL cleanup. If the controller is hard-killed while the script sleeps
or ignores TERM, the control pipe closes, the helper cancels the exact owned
process groups and confirms their disappearance. It fsyncs the backend receipt,
then the nonce-bound job completion, before exiting. Once a helper has accepted
the job, parent process death cannot prevent those writes. A crash before helper
launch can still leave an uncertain intent, which is never automatically
repeated. The helper does not depend on CPU time to stop sleep.

The controller returns only a validated helper receipt. If the helper itself
vanishes or cannot prove cleanup, the operation remains `needs_review` and
blocks new tests. Recovery reads the original durable backend receipt; it
never restarts the source, adopts a PID from a journal or signals a recovered
PID. The outer development broker can reconcile its pending intent from that
receipt and cache the completed result.

The supervisor and host are trusted components. This lot does not guarantee
cleanup after an operator or compromised host kills the supervisor itself.
The signal guard tests the measured native boundary; it does not certify
unrelated privileged tools, arbitrary host configurations or another OS. The
exclusive disposable checkout and no-secret conditions in `NATIVE_TESTS.md`
still apply. This qualification activates no production executor and launches
no model; explicit development execution remains a separate authorized action.

## Verification

Unit tests use fake helpers and private pipes, without processes or models:

```sh
cd integrations/codex-personal
python3 -m unittest discover -s tests -p 'test_dev_test_supervisor*.py'
```

The explicit native fixture qualifies eleven guards, runs a normal test and a
TERM-ignoring timeout, then SIGKILLs only its own synthetic controller while a
TERM-ignoring script sleeps. It verifies independent cleanup and durable
completion, reconciles the broker, confirms that replay leaves the execution
counter at one, and checks that both test and helper disappeared. Only synthetic
temporary files and processes are used; no production backend is activated.
The fixture permits patching only its two data counters; its normal, timeout and
controller-crash scripts are separately trusted. It also verifies broker refusal
to patch a script. No script executes source read from a model-modifiable file.

Run the qualification again against the final adapter source before a pilot.
Preparation pins all six controller files, including `__init__.py`: even a
package-version change invalidates an existing supervision plan. Prepare new
private plan directories and receipts instead of changing old pins or adopting
old attempts. Keep the final source revision with the new receipt; the dated
receipt below remains a record of its original measurement. Also repeat the
separate named-test and planner-policy fixtures documented in the release
procedure. These qualification commands use no model inference, login or
account RPC.
The dated receipt below predates the separate script-pin plan; a new native
qualification receipt is required for schema/profile 2 and the final package.

The [16 October 2026 Mac receipt](docs/native-supervisor-2026-10-16.json) records
the measured eleven guards, hard controller crash, durable cancelled receipt,
successful reconciliation, no second source execution and disappearance of the
test and helper. Its two source hashes identify the measured implementation.

```sh
PYTHONPATH=integrations/codex-personal python3 \
  integrations/codex-personal/tests/qualify_native_supervisor.py \
  --codex /absolute/path/to/pinned/codex --output /private/tmp/supervisor-proof.json
```
