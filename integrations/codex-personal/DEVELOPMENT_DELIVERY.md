# Exact Linux test proof and one Board publication

`development_delivery.py` connects the existing development worker's durable
proof to GitDelivery and its existing Board review CAS. It adds no scheduler,
model, test runner, PR creator, merge or automatic ticket acceptance.

The protected controller creates `DevelopmentDeliveryProof(worker, workspaces,
operation_id)`, where both arguments are concrete existing components. The
operation must have finished the planner with `result_ready`; the same proof
remains valid during the exact publication retry and after completion. A stop,
scope/configuration/root change or incomplete operation refuses.

Construct the existing `LocalGitDeliveryController(..., test_guard=proof)`.
After an explicit bounded `capture`, call `proof.evidence(snapshot)` and pass
its result to the existing explicit `finalize`. The callback accepts only the
same operation, scope, complete captured tree and recomputed evidence.

## Different snapshots, explicitly bound

The planner's hash covers the map of **all manifest paths to current SHA256
postimages**. GitDelivery's snapshot covers the full Git tree, scope, base and
declared change list. The proof reconstructs the entire planner map from the
captured tree; it never relabels a Git hash as a planner hash.

The exact full tree is reread through the existing bounded physical inventory.
Base entries and original manifest hashes are read with the existing protected
Git controller. The base, checkout, workspace request and every scope field
must match. Every base path remains present with its original mode; no added,
deleted, linked or unselected path is admitted. Changes must be inside the
manifest. The existing Git guards still reject unsafe metadata and links.

Linux input-only and Darwin receipts refuse. The concrete validator reads the
existing PlannerJournal and schema-3 DevelopmentJournal without constructing a
backend or launching qualification. It checks exact configuration, private root
pins, policy/backend plans, parent binding, final planner receipt, all required
named suites, actual passed receipts and each matching `broker_admitted` record.
It separately reopens the native terminal chain using the existing historical
cleanup reader, verifies the exact native hash, completed outcome and matching
output hashes. Cleanup by itself never becomes a passed test.

Every model turn and every tool step must match exactly one finished authentic
Runtime record, scope and clean RunStore result. Extra, missing, interrupted,
unclean or unrelated records refuse. The existing work lock is opened without
creation; the guard does not call `RunStore.work_lock`, which can reconcile an
interrupted turn. Missing locks refuse without creating them.

Each evidence receipt hashes a structured binding of the distinct Git/planner
snapshots, configuration, complete planner/broker documents, exact named test,
native observation and aggregate Runtime cleanup receipts. The stored evidence
therefore rejects later receipt drift even when all replacement fields look
individually valid. No `broker.diff`, broker budget charge, journal update,
Runtime finish, claim, model or native test occurs in this guard.

## Already complete PR, same review CAS

PR publication remains an explicit preceding controller action through the
concrete `GitHubDraftPublisher`. After that publisher has a confirmed complete
receipt, create `DevelopmentPublishedDelivery(proof, deliveries, publisher)` and
call `worker.publish(operation_id, delivery_publication=link)`.

The closed reader only uses concrete publisher read operations under its
existing locks. It freshly checks authentication, destination, source/tests,
remote refs and the unique exact open draft PR. It never calls `publish`, pushes,
POSTs, edits or creates a PR. Historical local status alone does not suffice.

The existing WorkJournal publication intent pins the resulting full commit,
tree, snapshot and exact PR metadata. The same existing optimistic Board update
sets review, adds the verified commit and canonical PR URL, and releases the
claim. There is no second `update_ticket` to append evidence after release.
Adding, removing or changing the delivery parameter on retry refuses. Lost
responses are reconciled by the existing exact version/claim/evidence checks.

Full live Board authority, context and claim are read **after all GitHub lookups**
and before the CAS. A revocation or context change during those reads cannot
publish the prepared delivery. Model text cannot construct the closed reader or
provide free-form delivery evidence. Existing publication without a delivery
keeps its previous behavior.

Locks are per job: WorkJournal scope (when publishing) → Git workspace → delivery
→ publication → existing RunStore work lock. The test guard reads WorkJournal
and workspace/delivery documents without reacquiring their locks; it only takes
the separate existing RunStore lock. No global pool mutex spans a GitHub read.
The standalone evidence builder must be called by the protected controller;
normal finalization and publication already hold their existing Git locks.

## Remaining delivery work

This component is not deployed or qualified for native Linux by its tests. The
tests compose real small Git repositories, Board/Runtime/RunStore databases and
explicit synthetic Linux terminal observations; they run no real model, Linux
sandbox, Google/GitHub authentication or external network request. The GH layer
is closed and simulated; Git transports are private local bare repositories.

The pool/Linux driver, per-checkout native qualification, protected configuration
and normal GitHub authentication are separate deployment work. Source changes
require rebuilding the qualified source bundle. Ticket #49 remains open.

Publishing review releases the claim but retains the author's assignment. An
independent reviewer needs the existing explicit, durable handoff through
`release_ticket(status='review', unassign_self=True, reason=...,
idempotency_key=...)`. That preserves the SHA/URL evidence and is separate from
this publication CAS. This component does not perform that handoff or loosen
human-only assignment rules.
