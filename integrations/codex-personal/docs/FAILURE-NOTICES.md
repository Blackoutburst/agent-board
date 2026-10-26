# Closed development failure notices — adapter 0.39

A selected code worker can publish a diagnostic on its ticket discussion after
its run fails, or when its private result is ready but cleanup is unconfirmed.
Successful code delivery still follows the separate PR/reviewer workflow.

The message contains only schema 1, the fixed `worker_run` phase, an explicitly
allowlisted code and `cleanup_state` (`confirmed` or `unknown`). Unknown reasons
use `development_run_requires_review`. A ready result with unconfirmed cleanup
uses `development_cleanup_requires_review`. `unknown` does not assert that a
process is alive or stopped. The notice contains no exception text, prompt,
model output, command, path, authentication data or private journal content.

The client validates and serializes a private snapshot of the notice. Its
separate typed method requests the full MCP acknowledgement and verifies the
message/thread/body/session. Generic worker calls still cannot post messages.
Only the existing initialized session and pinned connection are used; the
server rechecks current message rights and project membership. There is no
registration, identity fallback or automatic permission change.

A private queue intent is written before the single post attempt. Its key is
stable for that operation; an explicit notification-only repeat can therefore
retrieve the same message after a lost acknowledgement. The separate receipt
records only the confirmed message metadata. Conflicting intents are retained.
Pool reconciliation does not retry the notice or restart a model.

Notification failure preserves the original run error, Pool outcome and cleanup
state. No claim is released, no ticket status is changed, and no additional
Runtime reservation, finish or reconciliation is performed. Notices do not
cover failures before the selected run/client is available, process death,
notification outages or later Pool verification errors after client closure.
They improve observation; owner workflow acceptance remains to be demonstrated.
