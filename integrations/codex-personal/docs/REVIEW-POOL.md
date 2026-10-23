# Explicit independent delivery reviews in the cloud Pool

`DeliveryReviewPoolDriver` is a separate `WorkerPool` driver. It reviews one
controller-selected delivery using the real `DeliveryReviewWorker`, private
`WorkJournal`/`RunStore`/`RuntimeJournal` handles opened on its execution thread,
and current Board review claim and Runtime authorization. Its explicit model
returns a private opinion only. It never claims a ticket, registers implicitly,
selects another task, publishes, completes a ticket, or merges a pull request.

The controller prepares work in this order:

1. Submit the selected project/ticket to a Pool containing `driver.binding()`.
   The Pool chooses distinct operation, worker and session UUIDs.
2. Explicitly register that exact session using
   `DeliveryReviewBoardClient.register_initial(skills=['review', ...])`, then
   acquire the existing Board review claim. The reviewer profile must differ
   from the delivery author. Save the returned registration in the protected
   selection and build the current context after the claim.
3. Attach a concrete, completed `DevelopmentPoolResultDelivery` source.
   Construct `LocalGitDeliveryController` with its workspace/delivery/review
   roots and `HistoricalPoolDeliveryTests(source, author_operation_id,
   source.status(author_operation_id))`. Explicitly prepare the independent
   read-only export using `prepare_review`, the review Pool operation UUID,
   reviewer scope, current context fingerprint and selected material.
4. Prepare and qualify the existing Linux planner policy against that export
   and a separate empty model workspace. This remains an explicit controller
   operation in the actual execution environment. No policy is qualified by
   the driver. The policy, executable bytes/ancestry and qualification files
   are pinned to the protected selection.
5. Write an owned private selection file and call
   `driver.bind_selection(job, selection_file, delivery_source=source)` before
   the explicit `pool.dispatch_pending()` call.

The closed selection schema has these fields:

```text
schema: 1
operation_id, worker_id, session_id: exact Pool UUIDs
scope: owner_id, agent_id, project_id, ticket_id, board_session_id, board_endpoint
registration: the exact existing reviewer registration
source_operation_id: completed author Pool operation UUID
context_sha256: current reviewer context fingerprint after its claim
instruction_file, material_file: owned private, immutable selected text
planner_policy_directory: explicitly prepared and qualified Linux policy
executable: absolute selected CLI path, identical to the policy executable
model: explicit requested model
timeout: finite 1–300 seconds
```

`LinuxDeliveryReviewBinding` checks the real Git export but executes the model
in the policy's empty workspace. It uses the qualified Linux transport, with
no fallback to a different execution profile. Changes to selected text, source
receipts, profile/session, claim, context, Git export, executable or policy
refuse further progress. The historical author guard reads the actual sealed
snapshot/evidence and completed handoff receipts through `source.status()`;
it never reopens an author worker or reactivates its old claim.

A driver reopened after an interruption has no source attachment. The local
controller must call `attach_source(review_operation_id, same_concrete_source)`.
That validates the exact persisted source description before any dispatch or
cleanup inspection. Reconciliation only checks existing terminal RunStore and
Runtime receipts; it does not repeat a model or manufacture missing cleanup.
Unknown cleanup retains Pool capacity. Cancellation may leave a private
RunStore operation requiring review even when physical cleanup and finished
Runtime receipts prove that Pool capacity can safely be released.

After a completed private review, an existing local controller can call
`driver.open_completed_source(resources, context)` on the writer's thread,
passing an `ExitStack` and that existing job's `PoolContext`. It returns the
correlated `(LocalGitDeliveryController, DeliveryReviewWorker)` handles for a
historical writer input. It validates the completed private result and opens
existing journals, without a reviewer model, registration or Runtime RPC.
This read is historical data, not current publication or merge authority.

Tests use the real local Pool, Git objects/readonly export, Board claims,
SQLite journals and Runtime protocol. Text inference, GitHub transport and
Linux qualification observations are explicitly synthetic; these tests do not
prove installation, owner authentication, native unit qualification or a live
cloud reviewer execution.
