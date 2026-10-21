# Local Git workspace foundation

This component is **not wired to a worker, service, Board API or browser**. It
provides the local-copy portion of cloud development ticket #49. It does not
run a model or tests, create a commit, contact GitHub, publish a PR, authenticate
an owner, or enable a server agent. No new permission is granted by its records.

`blackoutburst_agents.git_workspace.LocalRepositoryGrant` is an exact resource
declaration supplied by a trusted owner/controller: owner ID, repository ID,
absolute local repository, absolute workspace root and a full commit object ID.
Board messages and model responses must never supply or broaden this grant.
The controller must still establish live profile, session, project, ticket,
claim, actions and resource authority before using a resulting checkout.

## Interface

Create `LocalGitWorkspaceController(state_root, grant=grant,
git_executable='/usr/bin/git', timeout_seconds=30)`. The state and workspace
roots must already be separate mode-0700 owned directories. The source must
also be owned and not writable by another group/user. Root paths, the actual
source `.git` directory and the trusted Git executable are pinned. The Git
executable and the cooperative controller are trusted prerequisites; this
component is not an OS sandbox against arbitrary same-user Python code.

- `plan(operation_id=UUID, scope=...)` records the exact request. Its scope uses
  the existing development manifest fields: owner, agent/profile ID, profile
  version, device, session, project and ticket. Planning reads local Git
  metadata and creates only private bookkeeping, without a checkout or branch.
- `materialize(operation_id)` creates one independent full local Git copy and
  one deterministic `codex/cloud-...` branch at the exact base. There is no
  shared Git administrative directory, object hardlink or alternate store.
- Repeating the same plan or materialization returns the same workspace. A
  changed scope, grant, base, repository identity, root, Git executable, branch,
  HEAD or destination Git configuration is refused. Working-file edits are
  preserved; no reset, clean, removal or recopy API exists.
- Before reusing a ready copy, the complete physical `.git` tree is inspected
  through held directory descriptors. Symlinks, shared file hardlinks, special
  files, unsafe ownership/modes and external alternate/common directories are
  refused. The bounded metadata inventory brackets each subsequent Git read;
  an observed substitution refuses the copy without touching working files.
- `status(operation_id)` returns recorded local metadata. It does **not** prove
  current process health, Board ownership, credentials or checkout validity.
- `manifest_inputs(operation_id)` revalidates a ready checkout and returns
  `root`, `base_sha` and `scope` for existing `dev_manifest.prepare_manifest`.
  That API must still receive explicit files, actions, resource and its live
  authorization callback. This does not create a `WorkJournal`, claim or
  Runtime. It does not attest that edited files equal the base tree.

The checkout HEAD remains fixed at the base in this first interface. A future
commit/publication controller needs a separately reviewed transition and exact
snapshot binding; committing and silently reusing this record is refused.

## Interruption and limits

The complete request is persisted first. A `preparing` intent is fsynced before
the first copy effect. Copied files and directories are fsynced before a
fully verified checkout produces the durable
`ready` record. Losing the response after that record is harmless: reopening
finds the same ready workspace. An interruption before `ready`, including after
a complete copy, remains uncertain. A retry refuses it and retains the files
for inspection; it does not infer ownership from directory contents, adopt the
copy, reset its files, or start a replacement. A failed initial plan may leave
an empty reservation, which is likewise retained rather than reconstructed.

Per-operation nonblocking locks prevent competing copies of the same request;
different operations can copy concurrently. This is not a pool scheduler or
owner Runtime admission, which must be supplied by the future worker.

The first contract accepts ordinary local checkouts only. Bare or linked
worktree sources, shallow repositories, alternate stores, source metadata
symlinks, included configurations, partial-clone configuration and symlink or
gitlink entries in the selected tree are refused. Private manifest path names
are refused. The source administrative tree is bounded to 25,000 entries and
512 MiB, and the selected tree to 10,000 regular files, 16 MiB per file and
128 MiB total; Git output and elapsed time are independently bounded. These limits
do not constitute a disk quota for hostile repositories; broader repositories
and hard resource enforcement require a separate host contract.

Git uses fixed arguments without a shell, an explicit local source and
`clone --local --no-hardlinks --no-checkout`, then its own branch checkout.
The copied object database passes a strict local integrity check before checkout.
The process receives a minimal environment, disabled inherited/system/global
Git configuration, disabled hooks/templates/fsmonitor, no recursive submodules,
no replacement objects or lazy fetching, and file-only protocol permission.
It neither requests nor exports an authentication credential. A local origin
path is recorded; source remote credentials and configuration are not copied.

The current synthetic tests cover independent copies, actual simultaneous
copy boundaries, same-operation exclusion, exact reopen preserving edits,
scope/grant changes, source and path refusals, source hooks/configuration,
interruption before and after copying, lost ready responses, destination
tampering, and composition with the existing manifest authorization callback.
They do not qualify a Linux deployment, a developer pool, repository tests,
provider access, GitHub publication or a reviewer.
