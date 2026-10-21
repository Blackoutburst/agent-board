# Explicit development Pool driver

`DevelopmentPoolDriver` is an opt-in local adapter for one explicitly selected
Linux development operation. It calls the real `DevelopmentBoardWorker.prepare`
and `run`. Its endpoint is a **private result ready for review**. A settled Pool
job means that this code phase's capacity was released after cleanup verification;
the ticket remains in progress with its claim. No publication, claim release,
Git commit, PR, review worker, merge or ticket completion is implicit.

```python
driver = DevelopmentPoolDriver(
    protected_selection_directory,
    connection_file=existing_protected_connection_file,
    owner_id=existing_owner_id,
    agent_id=existing_agent_id,
    board_endpoint=approved_board_endpoint,
)
pool = WorkerPool(pool_directory, concurrency=1, ceiling=8,
                  drivers={"development": driver.binding()})
job = pool.submit(request_id=explicit_request, driver_id="development",
                  payload={"project_id": selected_project, "ticket_id": selected_ticket})
# The authorized local controller now prepares/qualifies fresh resources for
# job["session_id"]. This driver never performs those qualification steps.
driver.bind_selection(job, approved_selection_file)
pool.dispatch_pending()  # explicit scheduling pass, no listener or polling loop
```

The approved selection is an owned regular 0600 JSON file in an owned 0700
directory. It has exactly these fields:

- `schema`: integer `1`.
- `operation_id`, `worker_id`, `session_id`: the exact three distinct UUIDs returned
  by `submit` (`operation_id` corresponds to `job['id']`).
- `scope`: exactly `owner_id`, `agent_id`, `project_id`, `ticket_id`,
  `board_endpoint`, `board_session_id`. The last field equals the Pool session.
  Owner, agent and endpoint equal the protected driver configuration; project
  and ticket equal the submitted payload.
- `broker_directory`, `backend_directory`, `planner_policy_directory`: absolute
  existing paths to the selected Linux execution manifest, supervised backend and
  planner policy. Their qualifications must already be valid for the exact
  checkout, sources, environment, CLI and UID. A different session or copied
  checkout cannot borrow those qualifications.
- `instruction_file`: absolute owned regular 0600 text file in an owned 0700
  directory, at most 16384 bytes.
- `model`: a required explicit bounded model identifier. No fallback is chosen.
- `budgets`: all seven fields returned by `PlannerBudgets(...).document()`;
  values must be explicit bounded integers, including wall and turn timeouts.

Binding is immutable; an identical retry is accepted, a different one is refused.
Paths, file identities and hashes pin consistency only. The real worker still
checks current grants, registration/profile/device/session, context, claim,
Linux qualifications, sources and every execution boundary. Missing approval or
changed pins refuse before registration. Payload contains only selected project
and ticket identifiers, never credentials, shell commands, instruction text or
provider configuration. The existing protected connection loader is reused.
The provider remains the existing `codex_chatgpt` path with its pinned Codex CLI
and forced ChatGPT/OpenAI configuration; this adapter adds no provider or account
qualification.

Every dispatch opens its own WorkJournal, RunStore, RuntimeJournal and clients on
the Pool thread. The `queue` directory contains only the adapter's prepare/run
intents and metadata result; QueueWorker's distinct claim contract is not used.
An intent is fsynced before `run`. Reopening Pool never calls the factory or
`run` again. `pool.reconcile(operation_id)` opens fresh existing journals and uses
identity-only `reconcile_cleanup`; it does not register, claim, renew, resume a
model, apply a patch, run a test or publish a result.

The per-operation Stop companion checks the local Pool stop every 25 ms and calls
the existing immutable `worker.stop` through its own WorkJournal connection.
Its join is bounded to one second. A failed Stop, a companion still alive, missing
receipts, an interrupted preparation, a lost control response or unknown cleanup
retains capacity. In particular, missing planner/Run/Runtime evidence after a Stop
before execution is not a cleanup proof. No force-kill or invented non-launch
receipt is added. Runtime activity is reported only from an actual authenticated
running response, validated against its exact local reservation and manifest
profile/device. It is an observation of Runtime state, not a process census.

Cleanup verification associates the exact Work operation, deterministic Run work,
all model turns and broker/native effects with their Runtime keys. It rereads
finished/cleanup-confirmed Runtime receipts through the current protected channel.
It refuses unrelated records, changed scope, absent stores and boolean-only
claims. Cleanup may succeed while the private result still needs review; that
settles only Pool capacity.

The driver explicitly enters the reviewed `standalone_fence_wait()` context on
its owner thread for each prepare/run/cleanup verification. This opt-in bounds
only local canonical metadata contention; other callers retain nonblocking
behavior, and another process holding the fence still refuses. Family controller
operations are refused in this adapter, including reconciliation. Waits do not
extend grant/source TTLs or authorize replay; the core refreshes the relevant
current grants after a wait and fails closed when its retry bound is exhausted.
The required SDK installation must include this opt-in API and its reviewed
planner/source refresh changes; no fallback to old sources is provided.

The composed test exercises four actual Pool threads with four explicitly
authorized local resources, real prepare/run/fresh checks and real Board/Runtime
SQLite reservations. A double barrier observes four correlated running Runtime
executions and four reported active operations; all sixteen reservations then
finish with verified cleanup, eight synthetic text turns run exactly once, Pool
capacity settles and all four tickets remain in progress. It contains no xfail.
These are synthetic policy/backend/source observations and text turns, including
an in-process ASGI client path. No model, native qualification, owner account,
production network, listener or service has been exercised by this lot.
