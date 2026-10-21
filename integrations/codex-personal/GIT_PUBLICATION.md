# Explicit GitHub draft publication

`git_publication.GitHubDraftPublisher` publishes one already sealed local
GitDelivery commit as one draft PR. It is a new part of #49, not a worker,
scheduler, Board permission, test runner, merge bot or deployed service. The
existing Git workspace and delivery contracts are unchanged.

This first destination contract accepts only `github.com/Blackoutburst/agent-board`
and full 40-character commit object IDs. The trusted owner/controller explicitly
constructs `GitHubPublicationGrant(owner_id, repository, base_branch, base_commit,
host='github.com')`. Model/Board text, PR text and stored hashes cannot create or
broaden that grant. The delivery's actual owner, local grant and base must match;
every request records that exact local-to-remote mapping.

Construct `GitHubDraftPublisher(deliveries, publication_root, grant=grant,
git_executable='/usr/bin/git', gh_executable='/usr/bin/gh')` with separate owned
mode-0700 roots and protected executables. The optional `python_executable`
defaults to the resolved current interpreter; it too must be protected and pinned.
The fixed guardian source is pinned and kept outside model-controlled roots.
`deliveries` must be the concrete
`LocalGitDeliveryController`. The existing Git executable must match; Git, gh,
root identities and physical separation are pinned. Missing/changed executables
refuse. No command is run by construction, except existing local inspection
performed by the supplied workspace controller.

## Interface and authority

`publish(operation_id=..., delivery_sha=..., title=..., body=...)` requires the
exact sealed commit/tree, captured source bytes and controller-verified test
evidence. GitDelivery's real verifier is called at each publication boundary;
declared test hashes or a success boolean in a model response never suffice.
The owner/controller must independently check its live Board/resource authority
before using this local API. No automatic ticket/claim/Runtime work is added.

Titles are bounded to 200 UTF-8 bytes; selected body text to 16,000 bytes. Both
pass the existing secret-content checks. A deterministic request SHA is included as an
HTML marker in the PR body. It identifies retries; it is not authorization.
The only remote branch is `codex/delivery-<operation UUID>`. The destination URL
is literal protected code, not a remote name/configuration read from a checkout.

The result contains operation, phase, verified delivery SHA, branch and a strict
PR number/URL/head/base/draft receipt. `status(operation_id)` reads historical
local metadata only; it does not contact GitHub or attest current remote state.
A repeated `publish` freshly revalidates both local and remote evidence.

## Real commands and normal authentication

Before creating a private transport copy, the publisher requires successful
official `gh auth status --hostname github.com`, an actual `gh api user` identity,
and the exact repository API response with push permission and an active repo.
The authenticated public login is pinned for that request. Unknown/changed
identity, missing auth or permission refuses before any push/create.

`HOME` and normal gh/XDG/keyring configuration-location variables retain their
existing values. The module neither opens those configuration files nor copies
their contents, changes accounts, invokes `gh auth token`, passes tokens in argv,
or stores credentials. Environment token overrides, proxy/debug/Git overrides
and checkout paths are not inherited. Authentication output is temporary bounded
command output, never a tool message, journal field or diagnostic payload.

A private independent local clone is made with no checkout, no tags, no shared
object hardlinks and empty templates. Its Git config is replaced by fixed core
settings with no remotes, includes, filters, proxies, credentials or hooks. Only
`.git` may exist in its working directory. Its complete administrative tree is
physically checked using the existing full-copy guard and pinned before reuse.
The sealed author checkout is not changed by publication.

The real push uses fixed argv:

```text
git push --porcelain --no-verify --force-with-lease=refs/heads/codex/delivery-UUID: https://github.com/Blackoutburst/agent-board.git SHA:refs/heads/codex/delivery-UUID
```

The fixed empty expectation is a create-only compare-and-swap: the server must
still see that exact deterministic ref as absent. It cannot replace an existing
ref, even when that ref appeared after `ls-remote` at a commit that could otherwise
be fast-forwarded. There is no generic force mode, old-SHA lease or caller-chosen
push option. A pre-existing exact delivery SHA is reconciled without pushing.

Git receives disabled global/system configuration, hooks/fsmonitor/attributes,
no optional locks, no lazy fetching/replacement objects, HTTPS-only protocol,
TLS verification, no HTTP redirects/proxy, and no prompting. Credential helpers
are cleared, then the sole explicitly configured helper is the pinned official
`gh auth git-credential`. Git consumes that helper's credential exchange itself;
this module never reads or logs its token output. The trusted Git installation
and its normal HTTPS runtime remain prerequisites, not checkout-provided tools.

Draft creation uses `gh api --hostname github.com --method POST
repos/Blackoutburst/agent-board/pulls --input <private JSON>`. The JSON has only
the controller-selected title/body, exact head/base and `draft: true`. Its bytes
are checked again after the last protected test callback and before the POST.
There are no caller-provided commands, replacement pushes, main/base updates, comments, labels,
reviewer assignments, permission changes, merge or PR-edit endpoints.

## Remote proof and bounded retries

Before each remote effect, the publisher checks the normal authenticated account,
authorized repo, actual base/head refs, exact source/test proof, transport metadata
and unchanged private request. `ls-remote` must show the authorized base branch
still at its declared full SHA. A delivery branch occupied by another SHA refuses.
An existing branch at the exact delivery SHA can be read back without pushing.

PR lookup is bounded and requires a unique exact head/base result. The PR must be
open and draft, with matching author, title, marked body, repository, head SHA/ref,
base SHA/ref and canonical `https://github.com/Blackoutburst/agent-board/pull/N`
URL. A closed, unrelated, duplicate or changed PR refuses; no replacement PR is
created. The publisher rereads the PR after the final source/test callback.

The existing workspace lock and separate private publication lock are held for
that delivery. No global pool mutex is held during commands or network I/O.
Each private intent is fsynced **before** clone, push or create. A failed/partial
clone remains `cloning` and is never adopted or recopied. An uncertain `pushing`
state can only be reconciled by reading the exact remote head. Missing proof
retains it; no second push is made. An uncertain `creating` state can only be
reconciled by reading the exact existing PR. Absence retains it; no second POST
is made. A lost response after the complete durable receipt is safe to retry.
There is no cleanup/reset/repair API, and no inference of success from exit zero.

The official subprocess wrapper has fixed environment and bounded output/time.
A small pinned Python guardian remains the live group leader while Git/gh runs
as its child. The target inherits neither private control/status descriptor.
The guardian sends only the target's actual integer exit code on a bounded pipe,
then waits. The controller terminates that still-owned group before reaping the
guardian, even on target success. Children with detached streams cannot survive
a successful call; no signal targets a PGID after its leader was reaped. Closing
the controller's life pipe also cleans the group during or after the command.
Raw stderr is not returned. Read failures do not
authorize retries of ambiguous writes. This is a cooperative protected-controller
boundary, not a sandbox against arbitrary same-UID Python or external changes
made after an observed GitHub response.

## Optional concrete development authority

The generic publisher still accepts its explicit local operator grant without a
Board worker. A pool delivery must instead supply
`authority=DevelopmentGitPublicationAuthority(worker, operation_id)`. This exact
closed type binds the real standalone Linux `DevelopmentBoardWorker`, sealed
configuration/manifest/context/claim and its private WorkJournal directory. Its
GitDelivery must use the exact `DevelopmentDeliveryProof` for that same worker
and operation; a callable, subclass or claimed SHA is insufficient.

The authority descriptor is pinned in the publication request before effects.
Adding, removing or changing it conflicts with the retained request on restart.
Standalone requests retain their original representation without that optional
field. The descriptor is local and stable after Board review publication: remote
read-only verification does not demand a now-released execution claim.

Immediately before **each** push or PR POST, after its last GitHub/remote GET and
after the durable `pushing`/`creating` intent, the concrete authority observes the
real current Board policy, exact sealed context and original live claim. It
checks policy again after the context reads and checks local Stop/pins. Only
local source/test/transport/intent revalidation follows that observation before
the command. A final closed `confirm_local` bracket then revalidates the bound
operation/manifest/pins and reads the actual worker Stop marker last, with no
RPC or later proof/transport read before the mutation. The real command launcher
repeats that final bracket after its own tool/environment checks, against a
single-use exact argv/intent frame. A local refusal is propagated rather than
classified as a lost network reply and cannot be swallowed into a successful
publication. No registration, renewal,
Runtime effect, test, model or qualification is performed by this guard.

This provides fresh observations at mutation boundaries, not an atomic
transaction spanning Board and GitHub or cancellation of an already-started
remote command. Pipeline integration and native qualification remain separate.

## Verification and deployment limits

Tests use small real local repositories. The HTTP command layer is simulated;
an explicitly test-owned adapter redirects the already checked literal push argv
to a private bare Git repository. No product-configurable alternate endpoint,
fake-publication callback or live network/authentication is used by these tests.
Synthetic executables also exercise the bounded command wrapper locally.

No live GitHub publication has been proved. Root separately found that gh is not
installed on the server at preparation time. Installation and normal account
authentication require their own explicit deployment step; a library/test result
does not supply them. The concrete broker-backed test guard, pool driver, live
Board authority and final ticket review publication must still be integrated.
