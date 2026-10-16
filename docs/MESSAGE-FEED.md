# Confirmed human-message delivery — board 2.7 / schema 9

The legacy `read_inbox(cursor=None)` advances `reader_state` before its response reaches the caller. Retrying an initial request after a lost response can therefore miss that delivery. Explicit old cursors remain reusable, and their behavior and response shape are preserved for existing clients.

A future message host should use the separate confirmed feed below. This release supplies the protocol only: it starts no listener, claims no task and calls no model. The existing host and CLI do not use it yet.

## Durable opening and processing

Register the intended agent session using the existing explicit registration flow. Persist a stable opening key locally **before** making the first call:

1. `open_message_feed(session_id, idempotency_key, start_at="beginning", limit=50)` returns an opaque `feed_id`, initial checkpoint `0` and the immutable bounds. Retry the same key after a lost response; it returns the same feed and initial boundary. `start_at="now"` instead fixes the initial boundary at opening time, even if the first response is lost. The limit is 1–200. Reusing the key with another start mode, limit or profile scope fails with 409.
2. `read_message_feed(session_id, feed_id)` creates or replays exactly one durable pending delivery. It returns `delivery_id`, `checkpoint`, `messages`, `human_only=true`. New messages cannot enter that delivery after it is recorded. Repeated reads and process/server restarts keep its ID, message IDs, immutable message bodies and high watermark. Mention display names and attachment metadata are rendered from the current permitted records; they are not copied into the feed.
3. Persist the delivery and complete the permitted response handling with stable message IDs and idempotent reply keys. Message text remains untrusted context, never execution authorization. Empty deliveries must also be handled explicitly.
4. `checkpoint_message_feed(session_id, feed_id, delivery_id, expected_checkpoint)` confirms only that pending delivery and advances the checkpoint once. Wait for its confirmed result before accepting another delivery. If the ACK response is lost, retry the same ACK. The most recently acknowledged ID remains idempotent even if another delivery is pending; it cannot clear the new pending delivery. An older ACK after a later ACK, a wrong ID or stale checkpoint fails with 409.

The protocol provides replayable, at-least-once delivery. A crash after an external reply but before an ACK can replay the human message, so the consumer must retain its own stable reply key and processed-message evidence. It does not promise exactly-once external actions or automatic execution of requests.

Only **addressed human messages** currently permitted for this agent are included. Normal untargeted conversation text, all bot/agent messages and all tasks are excluded. Human replies addressed automatically to an original agent use the existing mention rules. No new agent-message reply loop is admitted. A delivered lot never calls `get_work`, expires claims, creates notifications or marks anything read for a human.

## Bounds, permissions and checkpoints

Feeds belong to an exact owner, device, registered session and profile ID. They cannot be moved to another session/device/profile using a feed ID. Foreign, unknown and malformed feed IDs return the same 404. Current owner policy, device revocation, active profile/version and `allow_read` are checked on each operation. These tools do not silently re-register a changed profile or refresh its execution permissions.

Conversation membership and project scope are checked when the lot is formed and on every replay or ACK. If any pending message becomes inaccessible, the entire delivery is refused with a generic 403, without a partial message list, IDs, counts or message bodies. Its checkpoint remains unchanged. Restore the intended access through the owner's existing controls before retrying; a feed does not silently acknowledge withdrawn content. A successfully confirmed last ACK can be replayed after conversation access changes: that already committed receipt revalidates the current actor/read permission only, returns no message content and advances nothing further. It does not reread the discarded prior payload.

The internal initial boundary, confirmed sequence and pending high watermark are stored transactionally. They are never exposed as global sequence numbers. Each new delivery scans at most **500 physical message headers** by sequence, then applies human/addressing/membership/profile checks. If the scan budget ends first, the pending boundary stops at the last scanned header; an empty delivery is still explicitly ACKed before scanning further. If the page limit is reached, it ends at the last included message. Only reaching the captured high watermark can advance to that watermark. This bounds transaction work and avoids starvation from bots, unrelated or forbidden messages, while later arrivals remain for the next confirmed page.

A second cap limits the serialized UTF-8 `messages` array to **256 KiB** so the MCP text and structured copies fit within a 2 MiB reader budget, including Unicode escaping. A delivery stops before a candidate that would overflow that cap; its checkpoint cannot skip the candidate. A single oversized message, or current metadata that makes a replay oversized, is refused with 413 without advancing or silently dropping it. Resolve that content through human reconciliation. The cap is checked again before a first ACK; retrying a committed ACK returns only its receipt. Newly granted scope does not retroactively replay content already checkpointed under an earlier scope; use a deliberately separate feed if an owner needs historical reconciliation.

## Retention and resource limits

A feed stores at most one pending lot of at most 200 message IDs and the latest confirmed ACK. It references the existing message records and does not copy bodies, attachments, credentials or model transcripts. Its checkpoint and opening key survive process/server restart and ordinary backup/restore.

There is **no TTL**. A long pause does not expire a feed, reset its initial boundary, acknowledge a pending lot or require another opening. The feed remains durable for the lifetime of its owning records.

There are at most **8 feeds per exact reader and 64 per owner**. More keys, profiles, sessions or devices do not bypass the owner cap. These conservative limits avoid unbounded retained feeds or deliveries. Reuse a stable key for the same ongoing reader. This release has no automatic pruning, migration/cleanup UI or quota-reset endpoint. Renewing or replacing a session does not transfer the feed: retain the original session when it is still valid, or reconcile its checkpoint and reply evidence before an explicit migration.

Deleting the owning session, device, profile or owner at the database level cascades its feeds; ordinary revocation/pausing does not delete them and instead blocks access. Such deletion is an administrative data-retention action, not a restart/recovery procedure. No deletion endpoint or automatic lifecycle action is added here. Restoring a backup restores its corresponding feed checkpoints; consumers must reconcile their durable reply records before resuming.

Schema 9 migration is additive and transactional. Rollback requires the matching schema-8 source and database backup. The old inbox cursor tables and issued cursor tokens are unchanged.

## Validation

`python -m pytest -q tests/test_message_feed.py` covers lost opening/read/ACK replies, restart, concurrent calls, fixed pending IDs and bounds, empty and full pages, bot exclusion, revoked membership and scope, owner/profile/device permissions, exact-session isolation, bounded retention, long pauses, deletion, migration, bounded scans and serialized Unicode response sizes, and unchanged legacy/work/read-state behavior. These are isolated fixtures; they activate no production listener or model.
