# Development broker primitives — disabled

This library is a local foundation for a future development executor. It has no
model transport, command runner, production backend, CLI command, listener or
automatic activation. Nothing in an existing worker, queue or event invocation
calls it. `DevelopmentBroker` defaults to `enabled=False`; only a trusted local
controller can opt into these primitives. The tests use temporary files and an
in-memory fake test backend, without a model or subprocess.

## Trusted manifest and current permission

`prepare_manifest(...)` creates a private immutable `manifest.json` containing:

- exact owner/profile/version/device/session/project/ticket scope;
- an absolute root and its filesystem device/inode identity;
- a controller-supplied 40- or 64-digit base commit SHA;
- an explicit list of existing relative files, with baseline hashes and sizes;
- exact `repo.read_file`, `repo.patch_file`, `repo.run_test` action grants and a
  resource identifier;
- named tests with a pinned backend ID, timeout and output-size budget;
- operation, file-size, read-byte, patch-byte and test-count budgets.

The controller must verify the checkout and base commit before preparation. The
broker does not run Git or read `.git`, so the base SHA is a pinned declaration,
not independently verified repository provenance. File hashes describe the
actual captured baseline. The manifest is created once with exclusive creation;
reopening verifies its identity against the journal. Changes require another
manifest and a separately authorized operation. Public Python projections are
copies, so modifying a returned dictionary does not alter the plan.

`authorize_scope(scope, action, resource)` is injected by the trusted controller.
It must read current owner, profile, device and project authorization and return
the literal `True` only if this exact scope/action/resource remains permitted.
Exceptions and all other results deny access. The callback is checked before
each repository read, staging/commit or backend dispatch, including cached
receipts and recovery. Board messages, model text and test output cannot provide
permission. No board credentials or authentication are copied into the broker.
The controller must bind this callback to the intended board connection; these
primitives do not implement a remote authorization client.

## Files and patches

The root and every parent component are opened using directory file descriptors
and `O_NOFOLLOW`. A subsequent operation verifies the original root identity.
Files must be regular, owned by the current user, have exactly one hard link and
not be group/other writable. Repository directories must also be owned and not
group/other writable. Paths cannot traverse outside the root or name private
configuration/credential locations such as `.git`, `.codex`, `.ssh`, `.env`,
`auth.json` or key files. Reads are bounded, UTF-8 only, and reject changes to
inode/size/mtime/ctime during the read.

Known private-key, API-token and credential patterns are refused in both reads
and patches. This conservative filter can produce false positives and does not
prove that an arbitrary source file contains no secrets. Explicit human review
of the allowlist remains necessary; the library does not scan or publish other
files. Error strings are fixed codes, without file content or absolute paths.

`read_file(path)` is the only content-returning method. `patch_file(key, path,
expected_sha256, content)` accepts a complete replacement string for one
allowlisted existing file. There is no shell patch, fuzzy matching, wildcard,
rename, deletion or creation of a new source file. A mismatching expected hash
never writes the target. Preimage hash and inode/stat are checked again after
staging and immediately before the atomic rename. The final file is fsynced
before the rename and its parent directory is fsynced afterward. Visibility is
atomic for that one file; there is no multi-file transaction.

All participating broker writers share a private `flock`. **The controller must
provide an isolated checkout with exclusive write ownership.** Portable POSIX
rename is not a compare-and-swap against a noncooperating editor or process; a
writer could act between the final check and rename. The broker detects the
tested path/inode/hash races at its observation points, but it is not a kernel
sandbox or a lock on unrelated writers. It refuses observed root/parent swaps.
An interrupted parent rename can leave a private stage in the old directory;
the broker does not search other directories to remove it.

`diff()` returns only relative paths, baseline/current hashes and byte counts,
plus manifest hash and declared base SHA. It is not a textual diff or a complete
Git status: only allowlisted files are covered. No content or absolute checkout
path is included in receipts, status or diff results.

## Durable intent, budgets and recovery

The manifest and journal live outside the checkout in an owned `0700` directory;
their regular single-link files use `0600`. The journal is replaced atomically
and fsynced with its directory. Schema, manifest hash, counters, request hashes
and strict receipt fields are validated on every operation. The initial state
is created alongside the manifest; missing state is never recreated on reopen,
so losing a journal cannot silently reset budgets or repeat a backend call.
Budget reservations
are durable before file access or backend dispatch. Conservative read allowance
is charged at the maximum possible size, including failed reads; a restart does
not replenish it. Each new patch/test attempt has one fixed idempotency key.

A patch records its intent before creating a same-directory staging file. The
intent contains hashes, sizes and source identity, never patch content. A test
records intent before calling its qualified backend. A stored successful retry
returns the old receipt without repeating the effect; it never overwrites a
subsequent edit. Reusing a key with different arguments is refused. Any pending
or uncertain operation blocks every new patch/test in this manifest. Authorized
reads remain available for inspection.

After interruption, use `reconcile(key)` explicitly:

- For a patch, the desired hash yields `verified_after_restart`; the unchanged
  original hash **and** source identity yield `not_applied`. Neither case writes
  target content. The current file and parent are fsynced before a completion
  receipt, including when the original rename was visible but not yet durable.
  A different state requires review. Any remaining stage also blocks recovery
  with `stage_cleanup_requires_review` and is left for operator inspection.
  A deterministic stage name and matching hash cannot prove that this broker
  created the file; no stage is automatically deleted by recovery.
- For a test, only the backend's lookup of the exact recorded operation is used.
  The test is never rerun. Unknown effects or unconfirmed descendant cleanup
  remain `needs_review`, retaining the local barrier. A later confirmed lookup
  can resolve it.

Recovery reads and cleanup have fixed per-file bounds and current permission
checks, but do not consume new work budgets, so exhaustion cannot prevent
reconciliation. There is no force-abandon command for ambiguous effects in this
lot. If the file has changed to unrelated content, a stage is partial, the root
has moved or the backend cannot prove cleanup, an operator must inspect it; the
broker does not invent success or silently start another operation.

## Named tests and missing integration

`NamedTest` contains only a name, backend ID and budgets, never model-provided
argv, environment, shell command or executable path. No real backend ships.
Without a matching backend whose `qualified_for(manifest_hash, suite)` returns
literal `True`, `run_test` is refused before creating an execution intent.

A future qualified backend must enforce filesystem/network/environment limits,
the named test's deadline and output bounds, descendant cleanup, and durable
idempotent lookup across restarts. Repository tests execute repository-controlled
code, so a fixed test name alone is not isolation. Qualification is a trusted
controller decision, not a claim a model or board message can make. The Python
callback itself cannot be sandboxed or forcibly stopped by this library; a real
backend must supply and prove that boundary.

The trusted backend receives a `TestRequest` with the exact operation and
manifest hashes, root identity, name and budgets. It returns exactly `status`,
`cleanup_confirmed`, `output_bytes`, `output_sha256`. Raw output and diagnostics
are never accepted. Failed/unknown cleanup blocks new effects. Backend exceptions
leave an uncertain intent and expose only a fixed error code.

Before an actual development executor is usable, it still needs a bounded model
transport, server policy/Runtime integration, a qualified test backend, scoped
worktree preparation, cancellation and cleanup, a reviewable artifact flow and
an explicitly authorized end-to-end pilot. This lot does not change existing
text workers or claim that these missing components are complete.

## Local verification

From the repository root:

```sh
PYTHONPATH=integrations/codex-personal python -m unittest discover -s integrations/codex-personal/tests -p 'test_development_broker*.py'
```

Tests exercise temporary synthetic workspaces, filesystem races and failed
checkpoints. They launch no model, shell command, provider authentication or
network service.
