# Subscription probe: known CLI notices and bounded diagnostics

Adapter0.28.1 distinguishes a documented completed CLI notice from a tool call.
This is a narrow connectivity check, not a qualification of the requested model,
execution permissions, sandbox, hosted service, another account or future quota.
Existing0.28 native qualifications remain bound to their original sources.

The public Codex0.163 schema and producer at
[`83086188`](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/exec_events.rs)
define an `item.completed` with `id`, `type:error`, and `message`. The
[`JSONL producer`](https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/event_processor_with_jsonl_output.rs)
also uses that item for non-fatal warnings, configuration notices and model
rerouting. A known shape does not establish that its private text is benign or
that the requested model or permissions were honored. These public sources do
not identify the type or cause of a previously discarded private stream, or
establish installed binary/source equivalence.

The parser requires the exact completed-item shape and bounded strings. Its
presence never supplies a positive gate: doctor must independently confirm
ChatGPT authentication, the process must exit zero, one completed terminal must
be observed and the only completed response must equal the fresh random nonce.
Known notices may coexist with those facts. Such success retains a schema2
fixed-counter diagnostic rather than hiding the notice. A clean success keeps
its original shape. Actual tools (including collaboration), unknown items,
malformed or misplaced notices, protocol errors, fatal top-level errors,
turn.failed and failed/truncated transport remain refused. A notice alone,
missing terminal, wrong nonce, duplicate terminal or nonzero exit cannot pass.

Diagnostic schema2 has ten fixed event counters: agent_message, reasoning,
command_execution, file_change, mcp_tool_call, collab_tool_call, web_search,
todo_list, error, unknown. Counts saturate at1024, while every later event is
still validated. They describe decoded events, not executions. No item text,
identifier, hash, preview or unrecognized type leaves the parser. Transport
failures are not reparsed. Terminal failures retain their existing category
without treating the notice text as a fatal authentication/quota error.

The installed schema1 diagnostic operator must not use this module. A distinct
reviewed operator must pin its exact source and strictly validate schema2 on
both positive and negative receipts. Old unknown-item receipts remain failed;
none is reclassified or replayed. No feature flag, tool control, invocation,
timeout, authentication or provider policy changes. No real account or probe is
invoked by these protocol tests. No host is enabled by a connectivity receipt.
