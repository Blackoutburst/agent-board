# Explicit local delivery and independent review copy

`git_delivery.LocalGitDeliveryController` is a local portion of ticket #49. It
composes `LocalGitWorkspaceController` without changing that component or its
contract. It creates no Board claim, Runtime, worker, test execution, network
request, credential, GitHub PR or merge. It does not close #49 or qualify cloud
development. The controller must still check live ownership and resource grants.

Construct it with an existing workspace controller and two separate owned,
mode-0700 roots: `LocalGitDeliveryController(workspaces, delivery_root,
review_root, test_guard=protected_callback)`. All roots are physically compared
with the repository and existing state/workspace roots; overlapping roots and
symlink aliases refuse. The callback is trusted controller code, never a value
or instruction taken from a model, repository, Board message or stored hash.

## Capture exactly the selected changes

`capture(operation_id=..., scope=..., changes=[...])` requires a ready workspace
for that exact operation and the original seven-field development scope. Every
selected change is a closed `{path, sha256, mode}` object. For an added/modified
regular text file, the SHA256 is the expected **post-image** and mode is `100644`
or `100755`. For a deletion both fields are null. The list is limited to 128
unique paths. It must equal the complete set of changed, added and deleted files
against the pinned base, including untracked and Git-ignored files. There is no
`git add .`, automatic file selection or index adoption.

Capture physically inventories the complete working tree. Symlinks, hardlinks,
special files, private/escaping paths, Git metadata paths, case/normalization
aliases, unsafe ownership/modes and unsupported Git layouts refuse. Gitlinks are
not regular files and cannot be selected. Edited/added files reuse the existing
manifest's UTF-8 text and secret-content checks; unchanged regular binary files
may be retained from the authorized base. Nonempty Git hooks are refused.
Directory/file/byte limits reuse the workspace bounds (10,000 entries, 128 MiB
total, 16 MiB per file), with an 8 MiB private record bound.

The result is a `local-captured-tree-v1` snapshot containing the exact request,
scope, base, path/mode/length/SHA256/blob object ID of every resulting file.
Its ID is `digest(canonical(snapshot))`, using existing manifest helpers.
Capture does not establish that tests passed. Exact captured bytes
are stored in separate private files. The complete current Git metadata is also
pinned; it is not included as review material. Capture writes a durable intent
before copying bytes and rechecks the source afterwards.

## Freeze a commit only after controller test verification

`finalize(operation_id=..., snapshot_sha256=..., test_evidence=..., message=...)`
requires the same current working-tree bytes and Git metadata. Test evidence has
the closed shape:

```json
{"schema":"controller-test-evidence-v1","snapshot_sha256":"...",
 "receipts":[{"name":"selected-test","sha256":"..."}]}
```

There must be 1–32 unique named receipt hashes. These declared values grant
nothing. `test_guard(operation_id=..., scope=..., snapshot=..., evidence=...)`
must independently validate the actual selected test receipts for **this exact
snapshot** and return exactly `True`. An absent callback, unknown receipt,
different snapshot, false/ambiguous return or exception refuses. This component
does not execute tests, infer a pass from an exit-code field or transform a Git
commit into test evidence. The actual integration must reuse broker receipts
and their admitted source snapshot; no such adapter or claim is implemented in
this first API. An integration whose tests covered only a subset must establish
the intended whole-tree relationship itself, rather than relabeling the hash.

Source bytes/metadata are rechecked after the callback. Git plumbing writes blobs
with `hash-object --no-filters`, explicit tree bytes and a commit with the exact
base parent, fixed local author identity and controller-selected message. It
updates only `refs/heads/codex/delivery-<operation UUID>`. The original HEAD,
working branch and index remain at the base; workspace `manifest_inputs` still
enforces its original contract. Hooks, attributes/filter execution, `git add`,
`git commit`, remote helpers and shell commands are not used.

A sealed receipt records delivery/tree/base object IDs, snapshot, dedicated ref,
exact message/commit time and controller-verified test evidence. Subsequent use
revalidates actual Git objects/ref/tree, all original current files, captured
bytes, metadata and test callback. Author edits after capture/seal are retained
and refuse reuse; this component never resets, cleans, overwrites or silently
re-seals them.

## Distinct readonly review export

`prepare_review(operation_id=..., review_operation_id=..., reviewer_scope=...,
context_sha256=..., material=...)` requires a sealed delivery. The reviewer uses
the existing six-field Board-worker scope, with the same owner/project/ticket
and a distinct agent ID. Material is explicit controller-selected text (a diff
or summary may be used), bounded to 24,000 UTF-8 bytes. It is context, not authority.

The method copies the captured resulting tree to a distinct exclusive export.
Files are mode 0400 (0500 for an executable), directories 0500. There are no
hardlinks or `.git` data. The export therefore provides **no Git history/diff
commands**; the controller may include appropriate bounded material separately.
Paths, every file's bytes/mode and physical identity are checked again. These
modes are a cooperative restriction, not a sandbox against arbitrary same-UID
code, which can chmod its own files. A live guard must continue to revalidate.

The result includes `workspace` and an exact `delivery-review-selection-v1`.
To compose with the existing `DeliveryReviewWorker`:

```python
delivery_guard = functools.partial(controller.review_guard, operation_id,
                                   review_operation_id)
```

The callback accepts the worker's `scope, selection, context, material,
workspace` keyword arguments. It requires exact scope/session, distinct author,
delivery commit, context fingerprint and material hash, plus the still-current
sealed source, test evidence and complete immutable export. It returns exactly
`True` on success. A stored summary or selection alone cannot replace it. The
worker's existing authenticated Runtime channel and review claim remain required
independently. No worker/service is wired by this module.

## Durable reuse and interruption

All operations hold the original workspace's operation lock plus a private
delivery/review record lock. Different author operations use separate records,
refs and directories. A repeated exact captured/sealed/ready request returns its
existing result after fresh validation. Request, scope, message or evidence
changes refuse. Lost responses after the durable final record can be retried.

`capturing`, `sealing` or `preparing` remains ambiguous after interruption. No
directory, partial object, commit ref or completed-looking export is adopted or
replayed from that state. Files remain for explicit inspection. This API has no
cleanup or repair operation. Private records carry no credentials and are not
authentication proofs. It is local plumbing, pending independent review and
the actual coding-worker/test-broker/PR integration.
