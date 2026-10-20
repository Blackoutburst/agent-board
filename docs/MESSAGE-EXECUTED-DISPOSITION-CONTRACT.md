# Explicit disposition of an executed, blocked message

This source contract concerns one already executed message turn: the saved result
is `blocked/protocol_error`, its PID and provider session are recorded, and
`requires_review=1`. It is not a never-admitted seal, retry, successful answer,
current process-liveness claim, or permission to execute anything.

## Positive evidence and private RunStore boundary

The coordinator `MessageExecutedDisposition` holds the actual owner lock and
`RunStore.work_lock(work_id, inspection_only=True)`. It pins the original proposal
and journals, persists its Host intent, and only then performs these authenticated
read-only calls using the original device/session: `lookup_execution(original_key)`
and `get_execution_control(exact_execution_id)`. No registration, profile refresh,
reservation, heartbeat, finish, seal, signal, or model call is permitted.

`RunStore._dispose_executed_blocked(work_id, *, expected_state_sha256, intent_id,
proof)` consumes the following closed data projection:

* `schema='message-executed-runstore-proof-v1'`, `intent_id`, `state_sha256`
  (the original single-work/turn CAS), and `result_sha256` (the UTF-8 bytes of the
  original `result_json`);
* `configuration`: the exact message `RuntimeJournal.configuration`;
* `identity`: exactly `owner_id, agent_id, device_id, profile_version, session_id`;
* `local_runtime`: exactly `id, key, phase, revision, start_confirmed, receipt`.
  The ID is the local Runtime-operation UUID, key is derived from configuration,
  phase is `finished`, and start_confirmed is Boolean `true`;
* `receipt`, `lookup`, and `control` are three identical closed terminal cores.
  Each contains exactly `id, revision, state, owner_id, agent_id, device_id,
  profile_version, session_id, scope_kind, thread_id, message_id, source_sha256,
  work_id, turn_id, provider, model, outcome, reason, cleanup_confirmed`.
  State is `finished`, outcome `blocked`, reason `protocol_error`, and cleanup is
  Boolean `true`. Receipt comes from the original finished local Runtime row;
  lookup/control come from the two actual observations. IDs, revision and scope
  must agree with the saved identity/configuration. Integers never accept booleans.

The transport result must independently retain exact Boolean flags:
cleanup_confirmed=true, exit_zero=false, session_verified=true, tool_free=true,
turn_completed=false. Its session must match both saved RunStore rows. Data and
hashes alone do not authenticate a remote observation: the concrete coordinator,
verified source, original journals and held locks remain essential preconditions.
The helper performs no I/O outside its existing database and no PID/PGID probe.

The helper requires the exact original predecessor. It changes only work.status
to `closed`, turn.resolution to `abandon`, turn.resolution_reason to
`message_executed_blocked_disposed:<intent_id>`, and the two updated_at timestamps.
The original turn status, result bytes, PID, provider session, review flag, prompt
hash, cancel flag and all identities remain exact. It does not accept its own
successor as a fresh request: resume must recognize the exact recorded successor.
Invalid or regressed timestamps refuse before mutation; the exact two-write
postcondition includes the clock boundary and rolls back any unexpected write.
RunStore schema2 and all historical resolution behavior remain unchanged.

## Durable coordinator and message terminal

Host schema4 uses `host_message_dispositions`, managed by
`HostExecutedDispositionMixin`. The five monotone phases/revisions are `intent/1`,
`runtime_verified/2`, `work_disposed/3`, `message_disposed/4`, `complete/5`.
Each separate child commit is followed by an exact checkpoint. Cold resume accepts
only the original predecessor or the same-intent, same-proof successor. An
unrecorded or failed observation requires an explicit human retry. Pending intents
block Start, work and source/catalogue transitions; Stop and cleanup remain possible.

Message schema3 adds terminal `executed_disposed`. Its `disposition` marker is
exactly `{schema:'message-executed-disposition-v1', intent_id, proof_sha256,
original_payload_sha256, reason:'message_executed_blocked_disposed'}`. Removing
only this marker must recover the exact original payload, including its existing
`message_result_requires_review` reason. It never uses the seal47 abandonment
marker. Runtime rows/receipts, budgets, activations, generations, source, delivery
and reader are unchanged; no answer is posted and no delivery is acknowledged.

Before a source transition, a new exhaustive verifier covers both the historical
never-admitted child and this executed child, their exact checkpoints and the
complete bounded delivery/reader history. No orphan, extra child or pending effect
is ignored. The existing singleton seal47 verifier remains unchanged. Historical
cleanup is not current source authority; current source/owner/configuration CAS
and explicit stopped-unit checks remain separate requirements.

## Pending human Stop controls

A human Stop may already be queued locally and accepted remotely while the failed
message prevents its local application. The executed-disposition fingerprint
therefore validates and binds the complete bounded pending Stop-control rows.
Only canonical zero-budget Stop commands for the exact host, configured target,
instance, launch hash and non-regressed generation are eligible. Pending effects
must be exact acknowledgements for those Stops or their stopped/cleanup reports;
Start/Restart, needs-review reports and every other unsettled effect refuse.
This inspection performs no write or RPC and derives no cleanup evidence.

The recorded controls must remain identical through disposition. Once the child
closure is consumed by a v2 source intent, the separate finite controller may
complete the genuine Stop. The source commit still requires all commands and
effects settled and a fresh stopped/cleanup fence. The old pretransport seal and
v1 source-transition requirements remain unchanged.
