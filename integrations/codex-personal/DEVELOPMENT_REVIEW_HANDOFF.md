# Explicit author → independent reviewer handoff

`DevelopmentReviewHandoff(worker, published_delivery)` is a local controller
component for an already completed `DevelopmentBoardWorker` publication. Both
arguments must have their concrete production types. It does not run a model,
listen to a queue, publish another PR/message, choose a reviewer or mark work
done. Deploying/running it remains an explicit controller action.

```python
handoff = DevelopmentReviewHandoff(worker, published_delivery)
receipt = handoff.release(operation_id)
historical_receipt = handoff.status(operation_id)
```

The selected operation must be `complete`, with confirmed Board publication and
the exact `DevelopmentPublishedDelivery` SHA/tree/snapshot/PR metadata pinned in
its publication intent. A text URL or model answer cannot enable this path.
The concrete link revalidates the Git/test/native/Runtime proof, remote branch
and draft PR through its existing read-only verification. No second push/POST
or `update_ticket` is issued here.

After those GitHub reads, fresh Board permissions must allow `enabled`,
`allow_read` and `allow_requests`. Assignment maintenance does not require old
code actions/resources or an in-progress execution claim. The endpoint/session,
worker operation/controller binding, local Stop and current server profile
authorization remain mandatory. The MCP client adds only the closed
`release_ticket` shape used below; `WorkerBoardClient` is unchanged.

The live ticket must be in `review`, at the exact publication version + 1,
assigned to this author, without any claim (including an expired claim), and
without blocking prerequisites or unanswered human requests. Its owner
assignment, outcome, evidence and all other projected ticket fields must match
the worker's sealed post-claim context with only its known publication delta.
Every other context source (profile/session, project, thread and bounded
discussion hashes) must match. A completed dependency is allowed. These are
fresh observations followed by an existing server CAS, not an atomic remote
multi-source snapshot.

## Durable transition and restart

Under the existing worker's per-scope job lock, a private mode-0600 intent
`review-handoff-<canonical operation UUID>.json` is fsynced in its pinned
WorkJournal directory **before** the only effect:

```text
release_ticket(
  ticket_id=<exact selected ticket>,
  expected_version=<exact post-publication version>,
  idempotency_key="development-review-handoff-" + operation_id,
  reason=<fixed controller text containing the exact delivery SHA>,
  status="review",
  unassign_self=True)
```

The intent contains scope, operation/configuration binding, publication metadata,
context source hashes and exact arguments, not conversation bodies or credentials.
There is one RPC attempt per invocation, and no lock shared by unrelated pool
jobs during network reads. Lock order is WorkJournal scope → existing
workspace/delivery/publisher locks during link verification; the latter are
released before Board reads/RPC. The concrete test guard reads WorkJournal
without re-acquiring the scope lock, so this does not introduce recursion.

Successful completion requires acknowledgement of this exact idempotent RPC and
a fresh matching unassigned post-state at exactly version + 1, preserving the
publication/context. A lost response retains `releasing`. A later call can
recognize the exact possible post-state only to replay the **same key and
arguments**; that observation alone is never success. If a human makes a
similar unassignment without that server key, the replay CAS refuses. A foreign
reviewer claim, other revision, changed context/permissions or modified private
intent retains/refuses the operation without repairing or overwriting it.

`complete` receipts are historical acknowledgements. Reopening that exact local
receipt performs no RPC and does not disturb a reviewer who subsequently claimed
the ticket. A failed/lost acknowledgement followed by a later claim remains
unconfirmed rather than adopting that claim. The component never generates a
new ID, republishes evidence or creates an alternative handoff on such a retry.

## Review origin and limits

The existing server implementation preserves outcome/evidence and review origin
when `release_ticket(status="review")` is applied to an already-review ticket.
`get_ticket` does not expose that origin, and the author's `get_work.review`
excludes its own delivery: this component does not invent a remote origin proof.
Tests inspect the actual temporary Board's audit/origin before and after this
transition. Another identity can then use ordinary review claiming; selecting,
assigning and running that reviewer are separate controller responsibilities.

Tests use real small Git repositories and temporary Board/Runtime/RunStore/
WorkJournal databases. Linux native/model/GitHub observations are explicit
synthetic fixtures. They do not qualify a real Linux host or authenticate to
GitHub. No deployment/service/listener is enabled by importing this module.
The trust boundary remains protected cooperating controller code; private modes
and repeated validation are not a sandbox against arbitrary code under its UID.
