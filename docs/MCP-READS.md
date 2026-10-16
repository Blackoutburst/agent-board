# Bounded MCP reads (Board 2.9 / schema 11)

The browser REST API retains its original response shapes. MCP defaults to
compact responses with a 6,000-byte ASCII-JSON data budget. This reserves room
for the MCP text and structured copies (compact text serialization avoids pretty-print amplification); transport tests measure the complete
tool result against 20,000 bytes, including Unicode and escaped controls.
Discovery (`tools/list`) is protocol metadata, outside this data-page contract.

## Text and explicit full views

Compact prose is **absent under its original key**. An optional `body_preview`
or `description_preview` is display text, never the source body. `text_fields`
contains each omitted field's code-point length, UTF-8 byte length and SHA-256.
Short human answers (at most 512 encoded bytes) remain intact under `answer`;
longer answers use the same explicit preview/hash/chunk contract. Request status,
ticket version, claimant and lease fields are retained in normal compact detail.
Use `get_text(session_id, kind, identifier, field, offset=0, limit=2048)` to read
the original text. Kinds are `message`, `ticket`, `project`, `human_request`,
`legacy_task` and the calling agent's own `profile`. The final chunk has
`next_offset=null`; other offsets count Unicode code points. Persist chunks
and the returned hash, pass `expected_sha256` on continuations, and verify the
reassembled UTF-8 text's hash. An edit returns 409 instead of mixing versions.
Each chunk rechecks live membership/profile scope; identifiers are not access
tokens. Text limits budget JSON escaping, including non-BMP and control chars.

Existing tools accept explicit `view="full"` to restore their original data
shape and complete prose. This is an intentional, potentially large response;
choose chunks/pages for clients with an output cap. Ticket mutations return
compact acknowledgements by default, preserving IDs, versions and claim state.
Any further omitted collections are labelled `omitted_fields`, never replaced
by a false empty dependency list. Fetch current detail before dependent work.

The bundled `codex-personal` adapter adds trusted `view="full"` in
`BoardReader._tool` after its strict caller argument checks. Its context builder
still hashes original source and performs its own labelled artifact reduction.
Unmodified context builders fail closed on compact responses. Upgrade external
clients that require original bodies to use full views or `get_text`; never
seal a preview as the complete source. No runner or broker is enabled here.

## Collections

`list_projects`, `list_tickets`, `list_conversations`, `list_mention_targets`,
`list_labels`, `list_agents`, `list_human_requests`, `list_activity` and
`list_thread_tasks` return `{items, totals, next_cursor}`. `get_work` retains
`ready`, `recovery`, `assigned`, `waiting`, `human_requests`, `policy`, and adds
`totals`/`next_cursor`. Every collection respects its requested item limit as
well as the shared byte budget; fewer items can be returned. Work groups share
the budget round robin so a ready backlog does not hide assigned work/requests.

Drain `next_cursor` using the same filters. Tokens are server-issued and bound
to this actor/session and query. Collections preserve their source priority, recency or name order. A compact
cursor records bounded offsets and a hash of the ordered identities. If that
order or membership changes, the next page returns 409: restart the scan and
deduplicate IDs; no unseen row is silently skipped. Activity uses its sequence
high watermark so new events are deferred to the next scan. Reads recheck
access/current filters on every page. Cursors expire after 1 hour; successful
page reads prune expired rows, and only the 128 newest cursors per reader remain.
Expired/retired cursors require a new scan. No cursor row contains an ever-growing
seen-ID snapshot. `limit` bounds the total items across all get_work groups.

`get_thread` instead retains its exclusive sequence-based `next_before` marker
and chronological messages. Drain it until null; new messages after the first
page belong to the next scan. Long bodies use text references. Large attachment/mention lists are replaced
by explicit counts and a get_text reference to canonical JSON; they cannot
permanently block delivery. Compact get_ticket accepts limit/cursor and keeps
its control collections, per-collection totals and next_cursor rather than
omitting all requests/dependencies when the ticket is large. Legacy task
summaries are counted, and any omitted summaries are explicitly reported;
`list_thread_tasks` pages every legacy task and `get_text` retrieves its prose.
Compact thread reads do not expire/claim work or hold a write transaction.

## Inbox and confirmed feed

**Inbox migration:** compact default delivery deliberately changes the old
optimistic cursor-less behavior. A consumer that does not retain/present the
returned cursor will see its unacknowledged page again; repeated cursor-less
polls are retries, not progression. Upgrade consumers to persist/present the
cursor before relying on compact delivery. `view=full` explicitly preserves
legacy optimistic progression; do not mix modes in one durable consumer.

Default `read_inbox(include_work=false)` still returns exactly
`messages`, `cursor`, `session`, with no queue enumeration. A compact page fits
before its cursor is issued. Persist its messages and cursor atomically before
processing. Presenting the cursor acknowledges the prior page; a lost initial
compact response can be replayed cursorless. Deduplicate by message ID. Scope-
filtered messages are skipped as before and do not become visible via a cursor.
Explicit full mode retains the legacy optimistic cursor behavior. Avoid mixing
delivery modes in a consumer. `include_work=true` in compact mode points to
`get_work` instead of embedding its full queue; work.embedded=false labels this
explicitly, and no false tasks=[] field is returned. Use the separate tool.

The confirmed human-only feed keeps its opening/delivery/checkpoint protocol.
Default MCP delivery is compact and bounded before pending IDs are committed.
Read all referenced text before acknowledging. Replay returns the same pending
IDs and the same compact rendering; checkpoint still validates the exact delivery and current access. New compact deliveries retain at most 5000 bytes of rendering until checkpoint, so display renames cannot grow a pending page past its validated budget. A pending
delivery created previously in full mode is never partially acknowledged merely
to fit the new budget: if its compact references do not fit, explicitly drain
that delivery in full mode before opening a compact consumer.

## Attachments and assignment notices

`download_attachment` uses the existing device/session and project checks. Its
default base64 chunk is now 2 KiB, with `next_offset`/`eof`; an explicit limit up
to 256 KiB remains available. Authenticated streaming remains available at the
returned `download_url`. Public URLs and membership bypasses are not added.

Creating or changing a specific agent assignment inserts an ordinary addressed
message in the same ticket transaction. A deterministic unique message ID from
ticket/version/recipient/kind deduplicates retries. Moving A→B→A yields distinct
assigned/unassigned notices. Owner-only assignment, no-op saves and self-release
do not manufacture agent notices. Delivery rechecks current profile/project
scope. These notices are not `ticket_message_posted` trigger events, and do not
wake an idle client; its configured next poll discovers them.

## Exact nested text locators

Every preview with text_detail_tool=get_text includes its own kind, identifier
and field in text_fields[field].get_text. A field can be a JSON pointer into the
canonical authorized read (for example /author/session_label). Thread, activity,
agent, current session and attachment metadata are supported as well as the
original message/ticket/project/request/task/profile kinds. Large metadata arrays
are canonical ASCII JSON text chunks. Pin expected_sha256 even on the first read
when a preview supplies it. Each call rechecks source access and does not expose
fields outside that source's public read representation. Unknown envelopes that
cannot name a canonical source explicitly direct the caller to view=full instead
of advertising an unusable get_text route. Empty prose retains a text_fields entry.
