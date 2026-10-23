# Private manager and writer Pool roles

`TeamRolePoolDriver` adapts an explicitly selected manager or writer ticket to
the existing `WorkerPool`. Each role has its own driver, profile, connection and
session. It uses real `WorkJournal`, `RunStore`, `RuntimeJournal`, Runtime
reserve/start/finish and `PersonalRunner` components. Its protected selection
pins one explicit model and a qualified `LinuxPlannerPolicy`; there is no model
fallback. The model receives text only in the policy's empty planner workspace.

```python
driver = TeamRolePoolDriver(
    selections,
    role="manager",  # or "writer"
    connection_file=connection,
    owner_id=owner_id,
    agent_id=profile_id,
    board_endpoint=endpoint,
)
pool = WorkerPool(pool_directory, concurrency=4, ceiling=4,
                  drivers={"manager": driver.binding()})
job = pool.submit(request_id=request_id, driver_id="manager",
                  payload={"project_id": project_id, "ticket_id": ticket_id})
driver.bind_selection(job, approved_selection_file)
pool.dispatch_pending()
```

The selection JSON has exactly `schema` (1), `role`, `operation_id`,
`worker_id`, `session_id`, `scope`, `resource`, `model`, `instruction`,
`timeout`, `planner_policy_directory` and `input_file`. The three identifiers
must be those returned by `submit`. Scope contains the selected owner, profile,
project, ticket, endpoint and the job's Board session. The protected input and
the three policy documents are pinned before dispatch and checked again at
execution boundaries. Binding records an intent before registration; an
interrupted binding is not silently adopted. Selection cannot create a profile,
grant or resource.

The driver claims only that selected `todo` ticket once and revalidates its
exact context and grants. A lost claim response is resolved by an exact read,
never another claim. Its final operation uses the existing journal stage graph
but `publishing` means only retaining a private result. It never calls
`BoardWorker.run`, `update_ticket`, `release_ticket`, `post_message`, GitHub,
deployment or merge. The ticket remains claimed and in progress for its
controller. A restarted Pool reconciles cleanup but does not replay the model.
Stop reaches the actual runner's cancellation callback; unknown cleanup keeps
the Pool capacity held.

## Manager

Input schema `team-manager-slots-v1` contains one to sixteen protected `slots`.
Each has exactly `slot_id`, `resource`, `agent_id` and `model`. Slot, resource and
profile identities are unique. The manager returns strict
`team-manager-plan-v1` JSON containing `steps`, each with `step_id`, `slot_id`,
`title`, `acceptance_criteria` and `depends_on`. Every selected slot must exist
in the catalog, may appear once, and the closed step graph must be acyclic.
Unknown fields, extra permissions, duplicate identities or missing dependencies
refuse the result. This is a private plan, never execution authority.

## Writer

Input schema `team-writer-inputs-v1` names one to eight `sources`, each with
`source_id` and the SHA256 of the exact historical source. The driver receives
protected factories as `writer_sources={source_id: factory}`. Each factory must
open a concrete `TeamWriterSource` on its caller's thread and give it an
`ExitStack` owning all opened handles. Metadata, callbacks returning successful
flags and cross-thread SQLite handles are not accepted as evidence.

`TeamWriterSource` correlates an actual sealed `LocalGitDeliveryController`,
its concrete development test proof, the independent `DeliveryReviewWorker`,
its complete `WorkJournal` operation, its one completed RunStore turn and its
finished, cleaned Runtime record. It verifies the unchanged readonly Git
export, exact delivery SHA, material, reviewer scope and parsed opinion. It
never invokes the reviewer, reserves Runtime, repairs an execution or treats
historical data as current publication authority. The writer must be distinct
from both author and reviewer and belong to their owner and project.

The strict private output schema `team-writer-summary-v1` has `summary` and
`items`. Each item preserves the exact `source_id`, `review_operation_id` and
review `verdict`, plus bounded text. It cannot turn `corrections` into `accept`
by changing the JSON verdict. No summary completes a ticket or merges code.

## Remaining integration

These adapters do not schedule each other. A protected team controller still
has to consume the manager plan, map its approved slots to coder/reviewer
drivers, maintain step dependencies and hand the exact historical review
receipts to the writer. In particular, post-handoff source readers must use the
review Pool driver's concrete historical delivery guard rather than reactivating
an author's former live scope. Operator configuration and live owner/unit
qualification remain separate from synthetic model tests.
