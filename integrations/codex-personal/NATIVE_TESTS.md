# Explicit native named Python tests

`dev_test_backend.py` is an opt-in backend for the disabled development broker.
No worker, event, queue, CLI command or production service instantiates it. The
controller must create an immutable plan, bind it to the exact development
manifest, and call `qualify()` explicitly. Missing or failed qualification makes
`qualified_for()` return false before a named test can start. No model, provider
API, account login, authentication-file copy or paid fallback is used.

This first backend supports **standalone, single-process Python scripts on a
qualified Darwin host**. Tests needing child processes, package installation,
network, writable temporary directories outside the checkout, or an additional
toolchain are unsupported. Linux and Windows are refused; a successful Mac
canary is not evidence for another operating system. The separate
[Linux feasibility probe](LINUX_FEASIBILITY.md) records real namespace and
sandbox measurements with unresolved write/cleanup guards; Linux remains
refused.

## Controller plan and execution

`pin_test_plan(...)` pins the host's OS/release/architecture, CLI path/version and
binary hash, interpreter path and binary hash, wrapper hash, permission-profile
version, exact checkout root/inode, test names, relative script paths, time and
output budgets. The test environment is exactly `LANG=C` and
`PATH=/usr/bin:/bin`. It contains no inherited credentials, SSH agent, proxies,
Python path or shell startup variables. The CLI itself preserves the existing
`HOME`, `CODEX_HOME` and temporary-directory values, without replacing a user's
home or copying/opening their authentication files.

`prepare_backend(...)` stores this plan outside the checkout in a private owned
0700 directory with 0600 files. The backend ID is derived from the plan hash.
The development manifest must authorize exactly these named tests with matching
budgets, backend ID and allowlisted script paths. The only model-facing broker
argument remains the test name; no model-supplied argv, executable, environment,
working directory or shell command exists.

The sandbox command uses the official CLI's named permission profile with an
inline filesystem policy: root denied, `:minimal` read, the exact checkout
writable, `:tmpdir` and `:slash_tmp` denied, network disabled. The controller
rechecks the plan, binary hashes, root identity and current permission before
launch. Unsupported policies or sandbox-start failures do not cause an
unsandboxed retry. Permission profiles are beta; the command shape and its
actual enforcement are qualified against the pinned CLI build. See the official
[permissions](https://learn.chatgpt.com/docs/permissions) and
[developer commands](https://learn.chatgpt.com/docs/developer-commands) documentation.

A trusted wrapper enters its own session, sets a hard `RLIMIT_NPROC=1`, disables
core dumps, bounds file descriptors and CPU time, then checks the actual policy
inside that same process. It verifies synthetic inside read/write, outside
read/write denial, loopback denial, fork and spawn denial, inability to increase
the hard process limit, and inability to leave its session/process group. Only
after every guard passes does `runpy.run_path` run the reviewed named script in
that interpreter. The wrapper also requires a one-byte controller ACK after
the controller validates that guard, identifies the process and rechecks current
permission. EOF, missing ACK or the five-second gate deadline exits before the
source script. The control pipe is closed before source execution. There is no second interpreter launch after the process limit
is installed. The environment and `sys.argv` are replaced with the fixed test
values first. The isolated interpreter does not add the project to `sys.path`;
this lot targets standalone scripts using the available standard library.

The guard runs before **every** named test, not just once during qualification.
Sandbox startup/guard work has a separate fixed 15-second wall deadline. The
suite's wall deadline starts when its successful guard is observed. A timeout,
output ceiling or permission revocation terminates the positively identified
wrapper group, escalating from TERM to KILL, and reaps the launcher. A launcher
exit never excuses leaving its wrapper alive. Cleanup is confirmed only after
both groups disappear and the launcher is reaped; otherwise the result remains
`needs_review`.

Stdout is streamed with the suite's byte ceiling; only its bounded-prefix size
and SHA-256 are retained. Stderr has a separate fixed 8-KiB ceiling and is
discarded. Raw stdout/stderr never enter a receipt or MCP message. Qualification
and per-operation journals hold metadata and fixed reasons only.

## Durable attempts and recovery

The backend fsyncs an operation intent before launching. It records the receipt
only after observation and cleanup. Repeating a completed operation or looking
it up returns the same receipt without executing it again. If the controller
crashes before its durable receipt, lookup returns an unknown-cleanup
`needs_review` result; the backend never repeats that attempt or starts another
operation. **A controller crash can leave a sleeping test alive without its
wall timeout:** `RLIMIT_CPU` bounds CPU use and does not stop sleep. This lot has
no independent durable supervisor, so it cannot certify cleanup after a
controller crash. Such supervision is required before a production executor.
The backend does not guess a PID after restart or kill an unrelated process.
The separate opt-in [supervised backend](SUPERVISED_TESTS.md) supplies per-attempt
supervision; this class keeps its original unsupervised behavior.
An operator must inspect uncertain processes and effects before preparing a
separately authorized new manifest. There is no force-abandon command here.

The backend also persists the manifest's test-count ceiling through the number
of recorded attempts. Unknown cleanup or an unfinished intent blocks further
operations even when the backend is called directly. The outer development
broker retains its own intent, budget, current-permission and reconciliation
checks.

## Conditions and scope of proof

The native policy allows the **whole isolated checkout** to be read and written
by the test, including files outside the broker's individual patch allowlist.
The controller must prepare an exclusive disposable checkout with no secrets,
credentials, privileged sockets or private unrelated files. This backend does
not turn a user's existing working directory into a safe test environment and
does not certify arbitrary installed setuid tools or a compromised host. The
controller and pinned toolchain are trusted. A mutable source test remains
untrusted executable code; a fixed name alone provides no isolation.

The public qualification receipt covers only the synthetic scenarios and the
recorded OS, binary hashes, wrapper and profile. It confirms the measured
filesystem/network/process restrictions and cleanup conditions, without making
a claim of absolute isolation outside them. There is still no model transport,
worktree preparer, server development grant, artifact-review flow or production
activation in this lot.

The [16 October 2026 synthetic Mac receipt](docs/native-named-tests-2026-10-16.json)
records ten guards, five named outcomes, refusal to start the source without an
ACK, and a cached replay that leaves the execution counter at one.

## Reproduce on an explicitly authorized host

Unit tests use fake executables and mocked execution, without subprocesses:

```sh
cd integrations/codex-personal
python3 -m unittest discover -s tests -p 'test_dev_test_backend*.py'
```

The native qualification is a separate explicit command. It creates only a
disposable synthetic fixture, starts a synthetic loopback listener, then tests
success, failure, timeout with TERM ignored, excess output, cancellation and
cached replay. It removes its fixture and writes an expurgated receipt. On Mac,
the session's outer sandbox may prevent nesting Seatbelt; run only through an
authorized native-test escalation, never by changing the production policy.

```sh
PYTHONPATH=integrations/codex-personal python3 \
  integrations/codex-personal/tests/qualify_native_named_tests.py \
  --codex /absolute/path/to/pinned/codex --output /private/tmp/qualification.json
```
