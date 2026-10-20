# Explicit message admission refusal (capacity-v1)

`reserve_message_execution` accepts the optional literal
`refusal_mode="capacity-v1"`. Without it, capacity exhaustion retains the legacy
tool error. Authorization, exact source, existing-key/payload checks and
owner/work/turn conflicts precede a new refusal. Ticket admission does not
produce negative evidence.

With the option, the normal MCP result for saturation is exactly:

```json
{"reserved":false,"refusal":{"schema":"message-runtime-admission-refusal-v1","reason":"capacity_exhausted","request_sha256":"<64 lowercase hex>","owner_id":"<authenticated owner>","agent_id":"<current profile>","device_id":"<authenticated device>","profile_version":1,"session_id":"<exact session>"}}
```

The request digest is SHA-256 of UTF-8 JSON with sorted keys, no whitespace,
`ensure_ascii=False`, no non-finite numbers. Its value is
`{"method":"reserve_message_execution","arguments":{...}}`; arguments contain
exactly `idempotency_key,thread_id,message_id,source_sha256,work_id,turn_id,
provider,model,session_id,refusal_mode`. The contract literal is included. Values
are the strictly validated wire values, not display metadata or provider data.
Owner/profile/device/session must match the current registered message target;
the SDK validates exact keys and strict types. No free-text error is evidence.

Board schema 15 adds only `runtime_admission_refusals`. In the same serialized
transaction as the capacity count, it persists the response and exact payload
identity before returning the negative result. This table contains no execution
and is excluded from active capacity. It is unique both by owner/device/session
and key, and by owner/work/turn. A matching retry with the option returns the
original refusal even after capacity increases; a legacy retry is rejected.
A different key reusing the same owner/work/turn, a changed payload or any ticket
admission colliding with the tombstone is rejected before INSERT. A previous
real reservation returns its existing positive result, never a new refusal.
Different owners retain independent capacity and identities.

Fresh authorization/source checks still precede replay. A revoked or changed
binding may therefore return an error rather than the old proof. That error
cannot authorize anything. An older Board refuses schema 15; source rollback
must use code compatible with this schema, never delete/downgrade tombstones.

The SDK records a confirmed negative proof only from its fresh `reserving`
state with no positive receipt/start confirmation. Schema 3 of the private
Runtime journal adds terminal phase `not_admitted`; migration consists solely
of the atomic version marker plus this new row transition. Existing rows,
receipts, IDs and phases remain intact. Earlier `needs_review`, `reserved`,
`starting`, `active` or finished records cannot transition to `not_admitted`.
Older private readers refuse version 3. The private refusal is immutable.

PersonalRunner caches a local blocked result with
`reason=runtime_capacity_exhausted`, `uncertain=false`, and
`verification={"runtime_not_admitted":true}`. It does **not** attest cleanup.
No remote Runtime is fabricated and no `finish_execution` is called. The message
worker and host become `needs_review/message_runtime_capacity_exhausted`, keeping
the message, dispatch charge and all histories. No new message, inference,
automatic retry or budget refund occurs after a refusal or a capacity increase.

Timeouts, generic `isError`, malformed/partial proofs, mismatched identities and
lost responses retain the existing uncertain path. `lookup_execution` remains
an execution-only lookup: `found=false` says nothing about a denial or an
in-flight RPC. A lost negative response may leave a Board tombstone and a local
uncertain result; this patch deliberately does not recover it. A lost positive
reservation can still reconcile the exact real slot without launching a model.
No existing uncertain cached attempt is converted, even if capacity is raised.
An explicit human resolution for old attempts is a separate future change.
