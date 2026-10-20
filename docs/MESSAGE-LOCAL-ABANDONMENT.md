# Explicit local abandonment of one pretransport message

This SDK primitive resolves one narrowly defined legacy interruption. It does
not choose work, refresh a profile, activate a target, contact a provider, send a
reply, ACK a feed, refund a dispatch or finish any remote Runtime execution. It
is not an operator entry point. The protected operator is supplied separately.

The caller must first stop the real owner managers, validate the installed
source and immutable configuration, and establish that all four original
private databases exist before constructing their handles. Use
`RunStore(path, existing_only=True)` for this path: version 2, application ID 0,
the exact known tables/indexes and DELETE-mode header are required. This mode
never creates a directory/file, repairs tables/indexes or rewrites user_version.
Ordinary RunStore construction retains its previous behavior.

Construct `MessageAdmissionAbandonment(host, messages, runs, runtime,
seal_client=original_identity_client)`. The handles must refer to the Host's
`messages`, `message-runs` and `message-runtime` directories. Hold the existing
Host owner lock throughout. The coordinator obtains an inspection-only work
lock which refuses a running turn without marking it interrupted. It never
reads the connection file or registers/refreshes an agent. The injected closed
seal client uses the actual original connection/session; its endpoint and
session are compared to the configuration.

## Closed proposal and manual API

`propose(request_key=..., target_id=..., batch_id=..., worker_id=...,
identity=..., expected_config_sha256=..., expected_host_instance=...)` is a
read-only semantic inspection. `identity` has exactly owner_id, agent_id,
device_id, profile_version and session_id. The last four journal versions before
resolution may be Host 2, Message 1, Runs 2 and Runtime 1/2/3. Prior compatible
terminal histories can also exist. No uncertain record is inferred safe from a
missing lookup or an argument alone.

The proposal has exactly schema, request_key, host, identity, dispatch, message,
reader, delivery, work, turn, runtime and proposal_sha256. Schema is
`message-local-abandonment-v1`. `host` pins instance_id, configuration_sha256,
catalog_sha256, revision, target_id and fingerprint. `message` pins the original
operation row, payload_sha256, scope, configuration_sha256 and context_sha256.
The saved canonical prompt is checked against its original local instruction,
context hash, references, source and profile/session/version; it is not copied
into the proposal. The proposal is canonical and limited to 32 KiB.

The device is explicitly pinned and must match the authenticated seal. The
legacy receipt-null record does not independently contain a device ID. The
saved profile projection likewise excludes it. This limitation is not replaced
with a claim that the argument proves its identity. Local source snapshots do
not replace current server authorization/source checks performed by the seal.

`resolve(proposal, expected_revision=0, retry_seal=False)` creates a fresh intent
before its one explicit seal request. It requires the exact stopped Host,
managed catalog/targets, original dispatch, feed/delivery, one blocked turn with
PID/session NULL, the exact legacy runtime_start_requires_review result and a
needs_review Runtime row with receipt NULL/start_confirmed false. Unexpected
triggers, views and temp shadows of sensitive tables are refused. Unsettled
other dispatches, commands or effects are refused.

After an interruption, retrieve the saved closed document with
`proposal(request_key)`, compare its immutable public pins and hash, and call
`resume(intent_id, expected_revision=..., retry_seal=False)`. Intent IDs are
canonical UUIDs. Every resumed boundary compares the exact saved predecessor
or its unique intent-bound successor; an unrelated closed work or completed
message is never adopted. A changed configuration, identity, source snapshot,
row, checkpoint or Host fingerprint is a refusal.

An unconfirmed seal leaves phase intent and changes no other database. Only a
separate explicit `retry_seal=True` may resend that exact request. The server
seal permanently fences the original real Runtime key and owner/work/turn;
it refuses any actual Runtime row. It never fabricates an execution or cleanup
receipt. A confirmed immutable seal is persisted before local dispositions.

`status(request_key=... | intent_id=...)` revalidates the journals, then returns
only intent_id, revision, phase and reason. Missing intent raises the fixed
`message_abandon_absent` error. Reasons are constants `message_abandon_` plus
the phase; errors contain fixed codes, no source text or transport diagnostics.

## Durable boundaries and retained history

1. **intent**: Host schema 3 + intent are committed atomically. All other rows
   remain their exact original values.
2. **sealed**: the authenticated, exact original-key/identity seal is stored.
3. **runtime_abandoned**: Runtime schema 4 and `abandoned_never_started` are
   committed atomically. Receipt is a distinct local-disposition wrapper with
   intent ID and seal; it contains no remote finished/cleanup assertion.
4. **work_abandoned**: the existing `resolve_interruption(decision='abandon')`
   is used with a full-state CAS and intent-bound resolution reason. The old
   blocked result, IDs, prompt hash and session/PID absence remain intact.
5. **message_abandoned**: Message schema 2 and retained `abandoned` operation
   are committed atomically. Original payload is preserved with a closed
   intent/seal-hash marker. Generic update paths cannot create this terminal.
6. **complete**: one Host transaction changes only the targeted dispatch and
   target receipt to explicit abandoned disposition and closes the intent.
   Runtime/work/message checkpoints and a distinct final Host fingerprint are
   retained. Revision equals the one-based phase position.

A crash after a database commit but before the Host phase checkpoint accepts
only that exact intent-bound successor. No phase rolls back earlier databases.
After sealing or a private schema transition, remain stopped and resume with
compatible code; never restore an old private database or open it with an older
SDK. Host 3, Runtime 4 and Message 2 deliberately reject old readers.

Unsettled intents block managed start/restart and append preparation. A terminal
resume also refuses a later Host change, including a legitimate new catalog;
future source replacement must validate the terminal first and retain its own
separate transition proof. This primitive does not adopt another source epoch.

Normal message handling recognizes abandoned as terminal. A later ordinary feed
ACK can advance the original reader and delivery; it retains the abandoned
operation and its original work. The coordinator never ACKs, replaces the feed,
advances a cursor, creates a new turn, resets remaining budget or enables a
manager. An unrelated preexisting Runtime stays byte-for-byte unchanged.

## Validation scope

Tests use a real temporary Board/SQLite plus four real local journals and a
strictly synthetic provider counter which remains zero. They cover real
capacity rejection, permanent seal identity, lost replies, CAS, interrupted
commits across the databases, schema rollback, independent owner/agent proof
corruption, PID/session divergence, retained ACK history and strict existing-only
opens. No real account, provider, process, host installation or recovery has been
used to qualify this source increment.
