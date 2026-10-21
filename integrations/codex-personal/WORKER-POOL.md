# Local worker pool core

This component schedules explicit local work on distinct threads. It does not
provide a cloud team, Board profile, Git workspace, login, Runtime permission,
development driver, listener or service. The default driver registry is empty:
submission is refused until protected controller code supplies an approved
`PoolDriverBinding`. A driver ID or revision in data is never an approval.

```python
pool = WorkerPool(directory, concurrency=4, ceiling=8, drivers=approved_bindings)
job = pool.submit(request_id="selected-request", driver_id="development", payload=selected_metadata)
pool.dispatch_pending()  # One scheduling pass, no background polling.
pool.stop(job["id"])     # Or pool.stop() to stop all work/admissions.
pool.shutdown(timeout=1)
pool.close()             # Refuses while a thread/start/reconciliation is alive.
```

Concurrency is an integer from 1 through 8 under the persisted immutable ceiling.
`set_concurrency` changes the local limit; lowering it does not preempt existing
work or free retained places. Reopening with omitted settings retains them;
explicit conflicting settings refuse. `resume_admissions` requires that no place
is held. It never restarts a stopped operation.

`submit` durably records three distinct UUIDs: operation, local worker and session.
The worker UUID is bookkeeping, not a Board agent profile. The driver must use an
approved mapping to real profiles and pin the generated session. Each operation
has separate private `queue`, `work`, `runs` and `runtime` directories. Construct
QueueJournal, WorkJournal, RunStore and RuntimeJournal inside the actual worker
thread: their SQLite connections are not shared across threads. Payload is
private bounded JSON metadata (4096 bytes), with existing credential-key checks;
credentials remain in the driver's protected connection loader. The pool retains
at most 256 operations and never automatically deletes their evidence.

A `PoolDriverBinding(revision, factory, reconcile, verify_cleanup)` is trusted
local code supplied by the protected application, with four explicit contracts:

- `factory(context)` returns a fresh per-thread driver with `run()` and `close()`.
  It constructs resources, checks cancellation, and starts no implicit model turn.
- `run()` uses the existing Queue/DevelopmentAuthorization/Runtime admission
  contracts unchanged. All Board, Git, source, profile, lease, budget and cleanup
  checks belong to the approved adapter. Pool capacity is no Runtime authority.
- `reconcile(context)` only inspects or cleans the exact existing effects. It
  never selects new work, calls run/factory, or replays an ambiguous turn.
- `verify_cleanup(context, outcome)` freshly checks the real underlying private
  journals and authenticated Runtime/child receipts. Only exactly `True` confirms
  cleanup. A boolean in an outcome, dead thread, path absence, declared digest,
  model text or human metadata assertion is insufficient.

`PoolContext` exposes `operation_id`, `worker_id`, `session_id`, copied `payload`,
the four `journals` paths, `should_stop()` and `report_execution(bool)`. The latter
is an observation metric: the adapter reports activity only after a real Runtime
start receipt. It is never used to grant execution or release capacity. The pool
does not derive execution from a thread starting. `PoolOutcome` carries the exact
operation/session, status (`complete`, `blocked`, `cancelled`, `needs_review`) and
cleanup confirmation. A settled pool operation does not mark a Board ticket done.

Before thread start, a durable `dispatching` intent reserves a place. No pool
mutex or metadata transaction covers factory/run/close, network, verification,
reconciliation, thread start or joins. A separate lifetime owner lease prevents
two controller processes from opening the same pool. It does not serialize worker
execution. A changed or missing owner lease fails closed.

Reopening converts `dispatching`/`running` to `retained` with unknown execution and
cleanup. It never dispatches automatically or calls a driver to recover work.
Exceptions, uncertain start, false/unknown cleanup, failed verification or failed
reconciliation retain capacity. `reconcile(operation_id)` is explicit, requires
the same approved driver revision and no live worker, and checks the exact job
revision before releasing a place. Missing local paths never prove cleanup.

Stop writes its intent before setting cooperative events. If persistence fails,
the events still fire, global admissions remain fenced in memory and status says
`stop_unconfirmed`; no cleanup is invented. Cancellation requires driver
cooperation. A blocked driver can outlast `shutdown`'s bounded join; its thread and
place remain visible and `close` refuses. No numeric process ID is signalled.

Status separates `configured`, `live_threads`, `driver_calls`,
`executing_reported`, `execution_unknown`, `capacity_held`, `retained`, `queued`,
and persisted/unconfirmed Stop. `executing_reported` counts workers whose approved
driver reports activity, not independently observed model processes. Four real
threads and barriers in the synthetic tests prove local parallelism only. A
concrete opt-in [development adapter](DEVELOPMENT-POOL.md) and
[Linux job preparer](LINUX-POOL-PREPARATION.md) are available separately. They
retain their own authority, native qualification and cleanup checks; the core
does not supply or activate them. A service and a hierarchical team remain
separate prerequisites.
