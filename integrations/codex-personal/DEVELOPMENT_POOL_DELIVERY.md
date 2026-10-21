# Deliver one private Pool result

`DevelopmentPoolResultDelivery` connects the concrete existing components for
one explicitly selected job. It does not dispatch work, select a queue task, run
a model/test/qualification, reconcile Runtime cleanup, start a service, choose a
reviewer or merge a PR. Importing/constructing it performs no network operation.

```python
delivery = DevelopmentPoolResultDelivery(
    private_delivery_directory,
    pool=owned_worker_pool,
    driver=concrete_development_driver,
    workspaces=concrete_git_workspace_controller,
    publication_grant=GitHubPublicationGrant(
        owner_id, "Blackoutburst/agent-board", "main", exact_base_commit),
    gh_executable=protected_gh,
    python_executable=protected_python,
)
receipt = delivery.deliver(
    exact_job_uuid,
    changes=[{"path": "existing.py", "sha256": exact_postimage, "mode": "100644"}],
    commit_message=operator_commit_message,
    pr_title=operator_title,
    pr_body=operator_summary,
)
```

All arguments come from protected local operator code. Board/model text does not
authorize a repository, job, destination, command or changed file. The local
repository ID is opaque and cannot contain `/`; the operator explicitly maps
its exact concrete local grant to the fixed GitHub grant above. Owner/base,
checkout, full Git scope and actual selection must match. The checkout's remote
configuration is never used to infer a destination.

The existing-files-only limit remains: the bounded explicit changes must be
exactly the actual changes, within the manifest, with declared postimage hashes
and original modes. Additions, deletions, symlinks, gitlinks, Git metadata edits,
undeclared/ignored files and path escapes are refused by the existing Git/proof
components. Output directories cannot overlap the repository/workspaces/state,
pool or selection roots; parent paths are opened without following aliases
before creating the private output root.

## Real reopening and admission

The caller owns an existing concrete `WorkerPool` and `DevelopmentPoolDriver`.
Opening a previously closed Pool uses its existing exclusive owner lease; this
component does not bypass or independently reopen that lease. The Pool's own
constructor retains ambiguous dispatch/running jobs after a restart. Such jobs
must be separately reconciled and cannot be promoted by this delivery API.

The selected job must be `settled`, `outcome=complete`, `cleanup_confirmed=True`,
`execution_active=False`, with no selected live thread/launch/reconciliation and
with exact job/worker/session/driver/payload/revision. A brief pool mutex protects
only local observations. Unrelated jobs may continue; there is no global mutex
across Git, RPC, command launch or network I/O.

The actual `PoolContext` comes from `pool._context(create=False)`. The immutable
selection is reloaded using `DevelopmentSelections.load`; the exact private
prepare/run/result files must identify that same operation/session/selection and
an unpublished private review-ready result. Real WorkJournal/RunStore/Runtime
databases are reopened through `driver._open(existing=True, observe=False)` with
fresh clients for this call. No old worker/SQLite/client object from its launch
thread is reused. No registration is refreshed.

The existing `_cleanup`/`_verify_cleanup` driver methods are deliberately not
called: even their `reconcile=False` path can finish Runtime receipts. Instead,
before capture, sealing, PR publication, Board publication and handoff, every
actual selected Runtime execution is read through `lookup_execution`. Its real
normalized identity/receipt must exactly match an already finished, completed,
clean local receipt. Local `cleanup=True` or text output alone grants nothing.
The concrete delivery proof additionally checks all original RunStore/planner/
broker-admitted/native terminal records and complete source snapshot.

The canonical existing standalone fence is read with an already-present
nofollow lock file and a bounded shared OS lock. Its full row must match the
planner's exact configuration/parent/work/root binding; missing, changed or
family rows refuse. Neither the fence constructor/bind/attach methods nor the
worker's creating `_existing_fence_check` are used. No RPC/Git effect holds that
metadata lock. Existing worker publication/handoff calls use the standalone
fence-wait scope.

The author must already have been assigned before execution. Neither
`claim_ticket` nor `LinuxPoolJobPreparer` creates that assignment. If the sealed
`claimed_ticket.assignee_agent_id` is not the author, delivery refuses **before
Git capture**. A human must assign tasks before running the batch; this component
does not relax the human-only assignment rule.

## Durable concrete chain

One private per-job lock serializes this explicit delivery. Its fsynced immutable
request pins operation/session/selection/configuration, local Git grant/pins,
GitHub grant/tools, output roots, bounded postimages and fixed operator texts.
Each effect has an immutable intent preceding its existing component's own
intent protocol:

1. `LocalGitDeliveryController.capture` records the complete actual source tree.
2. `DevelopmentDeliveryProof.evidence` reconstructs the planner map of **all**
   manifest paths from that tree and verifies actual test/native/Runtime records.
   Its Git snapshot hash and planner map hash remain distinct; neither is renamed.
3. `finalize` receives that exact snapshot and structured evidence, producing one
   frozen commit without moving checkout HEAD/index.
4. `GitHubDraftPublisher` receives the concrete
   `DevelopmentGitPublicationAuthority` for that same worker/operation. Current
   Board context/claim/grants are checked after the last remote reads, before each
   push/PR POST, followed by the final local Stop/intent checks at actual launch.
5. `DevelopmentPublishedDelivery` is passed to the existing `worker.publish`,
   placing review/evidence/SHA/PR in its one existing CAS before releasing claim.
6. `DevelopmentReviewHandoff` performs the separately idempotent author
   self-unassignment. A different reviewer can then claim normally; this API
   selects and assigns no reviewer.

Retries use the same job UUID, fixed data and component keys. Changed parameters
or incomplete/mutated prefix records refuse. Lost successful effects are read
back using the existing exact protocols; no alternative commit/branch/PR/key is
created to escape ambiguity. A partially `capturing`/`sealing` local Git record
remains retained under its existing contract. After Board publishing starts,
replay requires an already sealed Git record and complete PR record, so it cannot
recreate missing earlier effects without the original execution claim.

`status(operation_id)` is a local historical receipt, with no client reopening or
RPC. Completion crosses the pipeline's copies with the actual sealed Git
`delivery.json`, completed `publication.json`, exact completed WorkJournal row
and completed `review-handoff-<operation>.json`. Snapshot, test evidence,
commit/tree/PR, original scope/configuration/context and publication keys must
agree. A missing or changed underlying receipt refuses; it is never restored
from a summary. The historical WorkJournal reader opens only its existing
private database in SQLite read-only mode, permits fixed metadata/SELECT reads,
and brackets the read with directory/file identity and signature checks. It
does not construct/migrate a journal and refuses pending sidecars/recovery.

A complete delivery retry likewise returns its historical receipt without
touching a later reviewer or creating another remote effect. These local checks
do not promise that GitHub or the ticket remains unchanged forever.

## Stop and validation limits

`stop(operation_id)` opens the exact existing local worker journal and calls the
real `worker.stop`, with no RPC, claim or new cleanup inference. The hierarchy
controller can wire its delivery Stop to this method. Stop of Pool admission is
also observed at delivery phase boundaries. `pool.stop()` does not signal already
settled contexts, so it is not advertised as immediate cancellation of their
delivery. A remote command already started is not rolled back by later Stop.

Tests use real small local Git repositories, temporary Pool/Board/worker/Runtime
databases and private bare Git transport. Their archived Linux/model/GitHub
observations are explicitly synthetic. They prove composition/reopening and
refusals, not native host qualification, authentication, live GitHub publication
or deployment. Protected release source closure/rosters and native qualification
must be refreshed separately before an operator enables this path.
