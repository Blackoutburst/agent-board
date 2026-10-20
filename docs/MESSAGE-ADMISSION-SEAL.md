# Explicit pretransport admission seal

`seal_message_execution_admission` permanently denies one original message
reservation. It is a separate, explicitly requested recovery operation; no
worker, scheduler or retry path invokes it automatically. The caller must retain
the original registered agent/device/session. Board binds the seal to the
authenticated current identity and requires current read/reply permission,
thread membership, an addressed human message and its exact current source
hash. Board cannot discover an old local request on the caller's behalf; it
does not register, refresh or reconcile the profile.

The closed MCP arguments are exactly the original **legacy**
`reserve_message_execution` request: `session_id`, `idempotency_key`, `thread_id`,
`message_id`, `source_sha256`, `work_id`, `turn_id`, `provider`, `model`. Do not add
`refusal_mode="capacity-v1"` to an old request. Unknown fields, JSON strings in
place of argument objects, coerced identifiers and invalid types are rejected
before the generic MCP parser. The tool schema advertises the closed object.

In the same `BEGIN IMMEDIATE` serialization used by both reservation methods,
Board first rechecks current authorization and source, then rejects **any** real
Runtime matching either the owner/device/session/key or owner/work/turn. A
finished Runtime also prevents this absence proof. A reservation that wins the
race remains a real reservation; a seal that wins prevents a late reservation,
including one using an alias key for the same owner/work/turn. Owners retain
independent namespaces. The seal does not examine or affect unrelated work.

The schema-15 `runtime_admission_refusals` table is reused without a database
migration. A new seal and its metadata-only audit commit atomically. Exact
identity, source, payload and response are immutable on replay. A preexisting
capacity-v1 refusal, a conflicting denial, changed profile version or a
positive/negative contradiction is refused, never rewritten. Replay still
requires fresh permissions and source; a lost response permits an **explicit**
identical retry but is not itself evidence. The client never retries an RPC.

The successful response has exactly this shape:

```json
{"reserved":false,"refusal":{"schema":"message-runtime-admission-seal-v1","reason":"explicit_pretransport_abandonment","request_sha256":"<seal request SHA-256>","admission_request_sha256":"<original legacy reservation SHA-256>","owner_id":"<authenticated owner>","agent_id":"<current profile>","device_id":"<authenticated device>","profile_version":1,"session_id":"<original session>"}}
```

Both request digests use UTF-8 JSON, sorted keys, compact separators,
`ensure_ascii=False` and no nonfinite numbers, of
`{"method":"<method>","arguments":{...}}`. They differ only by the method name:
`seal_message_execution_admission` versus `reserve_message_execution`. Neither
contains a fabricated capacity mode. The narrow `MessageAdmissionSealClient`
requires caller-pinned owner, agent, device and profile version, validates every
field and both hashes, and exposes neither registration nor generic tool calls.
It returns only the validated proof. Other clients do not gain a seal method.

This is evidence of **no Board admission for that sealed request**, not evidence
of local source integrity, no provider transport, process cleanup or completed
work. It creates no Runtime, calls no Runtime finish, frees no occupied slot,
refunds no host budget, activates no target and publishes no reply. An existing
unrelated uncertain Runtime continues to count toward capacity unchanged.

Resolving an old cached attempt requires a later separately reviewed local
coordinator. Before consuming the proof, that coordinator must validate the
protected source/configuration chain, original request and identity, no positive
receipt, `start_confirmed=false`, no provider PID/session, and the exact blocked
pretransport result. It must preserve all Runtime, RunStore, MessageJournal and
HostJournal history through explicit crash-resumable administrative transitions.
This change intentionally implements **none** of those local transitions or a
multi-database operator. It does not convert old uncertainty to the fresh-only
`not_admitted` state, report false cleanup, or make `lookup_execution` absence
authoritative. A source/catalogue upgrade for managed hosts remains a separate
prerequisite; prepared catalogues cannot simply be overwritten.
