# Subscription probe: negative JSONL diagnostics

This development branch changes failure diagnostics only. It is not a new
qualified release or an installed owner operator. Its eventual release version
and source pins must be set during integration; existing0.28 qualifications
remain bound to their original sources.

The public Codex0.163 schema and producer at
[`83086188`](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/exec_events.rs)
define an `item.completed` with `id`, `type:error`, and `message`. The
[`JSONL producer`](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/event_processor_with_jsonl_output.rs)
also uses that item for non-fatal warnings. Its presence alone does not identify
an authentication or quota failure. These public sources do not establish the
type or cause of any previously discarded private stream, or a binary/source
equivalence for an installed executable.

The probe now validates that completed item's exact shape and bounded strings.
It still rejects qualification, including when exit status, terminal and nonce
checks are all positive. Its fixed reason is `cli_diagnostic_item` unless a
validated terminal failure supplies an existing failure category. Tool and
unknown-item refusals keep their priority. Malformed or misplaced error items
remain protocol errors. No warning is silently ignored or converted to success.

Failure diagnostic schema2 has ten fixed event counters: agent_message,
reasoning, command_execution, file_change, mcp_tool_call, collab_tool_call,
web_search, todo_list, error, unknown. Counts saturate at1024. They describe
decoded events, not executions. No item text, identifier, hash, preview or
unrecognized type leaves the parser. The successful receipt shape is unchanged.
Transport failures are not reparsed.

The existing installed schema1 diagnostic operator must not be combined with
this module. A separately reviewed operator with exact new source pins and
schema2 validation is required before any personal trial. No feature flags,
tool controls, invocation, timeout, authentication or provider policy change in
this patch. No real probe or account is invoked by its pure protocol tests.
