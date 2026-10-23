# Visible independent cloud reviews

`DeliveryReviewLifecycle` connects the concrete author delivery to an explicit
reviewer Pool job and makes its exact opinion visible on the Board. It keeps the
ticket in `review`, releases the review claim and preserves the original author
outcome/evidence, delivery commit, draft pull request and dated test proofs.
No verdict completes a ticket, approves a stale delivery or merges a pull request.

```python
from blackoutburst_agents.review_lifecycle import DeliveryReviewLifecycle

# The local controller constructs the same open WorkerPool with the concrete
# development and independent reviewer bindings before starting the batch.
# source is the completed DevelopmentPoolResultDelivery for this author job.
lifecycle = DeliveryReviewLifecycle(
    lifecycle_directory, pool=pool, driver=reviewer_driver, source=source,
    executable=approved_codex, cli_version=approved_cli_version,
    interpreter=approved_python,
)
job = pool.submit(
    request_id=stable_review_request_id, driver_id=reviewer_driver_id,
    payload={"project_id": project_id, "ticket_id": ticket_id},
)
prepared = lifecycle.prepare(
    job, source_operation_id=author_operation_id,
    instruction=selected_review_instruction, material=exact_bounded_delivery_diff,
    model=explicit_review_model, timeout=120, skills=["review", "python"],
)
if prepared["status"] == "prepared":
    pool.dispatch_pending()
    # The controller waits for this actual Pool worker to settle and keeps its
    # Stop companion alive through publication. No second model is started here.
    result = lifecycle.publish(job["id"])
```

`prepare` registers the already bound distinct reviewer profile/session
explicitly, acquires the selected review claim with a CAS, composes its current
Board context and prepares the exact Git read-only export. It then prepares and
qualifies a fresh `LinuxPlannerPolicy` against that export and a separate empty
model workspace **in the same execution unit**. The actual reviewer is the
existing `DeliveryReviewPoolDriver` with `LinuxDeliveryReviewBinding`; no
configuration document or successful flag stands in for these components.
Owner/device pairing and profile permissions must already exist. Preparation
never grants permissions, chooses a different profile/model or dispatches work.

The preparation intention is durable before registration. An existing intention
returns `retained` and requires explicit operator reconciliation. A crash or lost
claim response never automatically repeats registration, claiming, qualification
or a model. Failed qualification is retained rather than waived or retried.

`publish` requires the real settled/clean Pool result and correlated completed
WorkJournal, RunStore model result, parsed opinion, material digest and finished
Runtime receipt. It verifies the exact source and current independent review
claim/context/permissions before the review-only CAS. The original opinion is
rendered as selected text in outcome/evidence; it is never interpreted as a
command or an approval to perform another action.

The existing `update_ticket` protocol provides a CAS version, without a server
idempotency-key argument. The lifecycle therefore saves one stable publication
key and exact CAS arguments, reuses their original version/text and confirms a
lost response only when both the exact post-state and durable `ticket_updated`
event match its original reviewer profile, session, version and changed fields.
A human or another agent creating an identical-looking post-state is rejected.
Editing a retained intention cannot substitute a different verdict or text.

`stop(job_id)` writes a separate durable stop marker and invokes the real
`WorkerPool.stop(job_id)`, including when recording the marker fails. The model
transport owns process cleanup; unknown cleanup continues to retain Pool
capacity. Stop is checked again before each publication effect.

After completion, `status(job_id)` returns exactly the same public metadata as
`publish(job_id)`. This is a historical receipt: it reads the real existing
WorkJournal without migration, checks immutable file fingerprints for the
RunStore/Runtime/private result/selection and read-only Git export, and crosses
the concrete author's `source.status()`. It constructs no client/journal and
performs no RPC, inference, claim or replay. Missing or changed proof is refused.
Its historical success is not current authority for another write.

The SDK integration tests use real local Board, Git objects, Pool threads,
SQLite journals and Runtime protocol. Model/GitHub transports and native
qualification observations are synthetic. They cover visible opinion delivery,
the same Pool retaining coder/reviewer bindings, lost claim/response behavior,
exact CAS reuse, forged post-state/intention refusal, Stop, stale context and
read-only status with a removed real proof. They do not prove a running installed
service, owner authentication or native qualification in the final cloud unit.
