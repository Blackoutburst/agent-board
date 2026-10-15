# Independent review — explicit event drain

Base: board `76160d3e96b529c078185c98a656bbd89aac6bd2`.

A separate reviewer inspected the activity client, command line, one-shot drain and private journal. The review covered transactional page/cursor persistence, replayed pages, stable batch dispatch intent, crash recovery after publication, exact local scope/configuration, live permissions, ignored agent events, bounded ingestion and manual reconciliation against a completed or explicitly abandoned WorkJournal operation.

The reviewer found one reproducible backlog saturation problem: when a page exceeded the pending-event capacity, ingestion retried it before dispatching existing durable work, preventing progress. The drain now preserves the rejected page's cursor, permits an explicitly authorized eligible durable batch to free capacity, and reports `event_buffer_full` in preview. Regression tests use a smaller capacity to verify no event loss and successful processing after saturation.

The independent final run passed **271 tests**, with no remaining actionable findings and a clean `git diff --check`. A separate read-only production check ingested four existing events and then zero on repetition, preserved its cursor, and constructed no worker or model invocation. The sanitized receipt is `local-event-drain-2026-10-15.json`.

This validates the selected-ticket, finite drain. It does not activate or validate a permanent listener, automatic task selection, agent-targeted mentions, multiple real model accounts or development-tool execution.
