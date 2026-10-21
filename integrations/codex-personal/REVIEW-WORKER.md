# Controller-selected delivery review

`DeliveryReviewWorker` is an SDK component for one already claimed ordinary
ticket in `review`. It keeps a structured opinion in a private `WorkJournal`;
it does not select queue work, acquire/release claims, change tickets, publish
messages, mark work done, merge, inspect Git or connect a service/PR workflow.
The coding workers and `DevelopmentAuthorization` retain their existing rules.

The constructor takes `journal`, `run_store`, the pinned reader `client`,
`runtime_journal`, and the separately pinned `runtime_client`. The optional
`runner_factory` defaults to `PersonalRunner`. The required execution dependency
is a protected local `delivery_guard`; omitting it safely refuses the operation.

`run` takes keyword arguments `scope`, `request_id`, `selection`, `instruction`,
`material`, `workspace`, `executable`, `model`, and optionally `timeout` (1–300
seconds) and `should_cancel`. Scope has the existing six WorkJournal fields.
The controller must already hold the exact reviewer profile/device/session claim
with enough lease remaining for this bounded turn. A selection has exactly:

```json
{
  "schema": "delivery-review-selection-v1",
  "author_agent_id": "the-delivery-author-profile",
  "delivery_sha": "the-exact-40-or-64-lowercase-hex-commit",
  "context_sha256": "the-64-hex-build_context-fingerprint-after-review-claim",
  "material_sha256": "the-64-hex-SHA256-of-the-exact-UTF8-material"
}
```

Hashes and Board/model text are data. They never authorize execution. The local
guard receives keyword arguments `scope`, `selection`, the fresh bounded Board
`context`, `material`, and resolved `workspace`. It must freshly inspect the
controller's accepted delivery, exact commit, material, author, and independent
reviewer copy, tying them to the context's outcome/evidence source hashes. It must
return exactly `True` only on verified evidence; failure or uncertainty refuses
the review. The worker calls it before preparing a prompt, immediately before
prompt delivery, during cancellation checks, after the turn, and on cached
completion. Supply this callback from protected controller code; do not construct
it from Board fields, a model response, or an operator's declared digest.

The existing authenticated Runtime `reserve_execution`/start/control API supplies
the budget and live review authorization. The server chooses its review branch
from the actual ticket, requires a different author profile, and pins delivery,
claim and permission epochs internally. No new wire argument or public receipt
field is assumed. Private work identity includes `work_phase=review`, selection
and operation ID, preventing coder or other-review provider conversation reuse.
After truthful cleanup, and before sealing or reusing an opinion, the worker
revalidates the exact existing Runtime reservation with its original key and
work/turn arguments. The server checks its original review epoch even when
current rights were revoked and restored between cancellation polls. A finished
cleanup receipt alone never authorizes an opinion. Revalidation requires a
correlated `retried` response and never starts another turn.

A clean, tool-free completed turn must return strict JSON with exactly `verdict`
(`accept`, `corrections`, or `limited`) and nonempty `text`. The private operation's
`payload.opinion` contains this opinion and exact selection. Public run receipts
contain metadata only. An `accept` opinion has no completion or merge effect.
The delivery must be revalidated before any separate publication by a controller.

The same request/configuration reuses the durable operation and model turn.
Lost local result writes can reconcile the cached RunStore result. Ambiguous
Runtime start/finish or uncertain cleanup requires explicit reconciliation; it
never silently runs another model turn. Changed context, lost/reclaimed claim,
proof loss, or changed selection cannot adopt an old opinion for a new delivery.

`LocalGitDeliveryController.review_guard` supplies the concrete readonly Git
export guard. Its test-evidence callback must still verify the accepted source
snapshot and actual test receipts; a declared passed test never supplies that
proof. The worker and Git components have local synthetic-inference tests, not
an activated reviewer service.

## Explicit Linux binding

`LinuxDeliveryReviewBinding(deliveries, operation_id=..., review_operation_id=...,
planner_policy_directory=...)` supplies `guard` and `runner_factory` for this
worker. It requires the concrete delivery controller and an already qualified
Linux policy in the current execution environment. Prepare that policy with a
separate empty planner workspace and the exact readonly export as its checkout.
Use `binding.workspace` and `binding.executable` in the worker's normal `run`.
The model receives the selected material through its prompt; its cwd stays
empty. The guard inspects the separate exact export, and qualification is checked
before and after that inspection. There is no automatic policy qualification,
claim acquisition, queue dispatch or publication. The live binding is confined
to its creating thread and must be closed.

The author must release its own assignment before a distinct reviewer can claim
the ordinary ticket. The existing explicit `release_ticket` operation with
`unassign_self=True` supports this handoff; publishing evidence alone preserves
the author assignment. Never impersonate a human or rewrite another profile's
assignment. A controller must retain the exact handoff intent and correlate its
receipt before selecting the reviewer.
