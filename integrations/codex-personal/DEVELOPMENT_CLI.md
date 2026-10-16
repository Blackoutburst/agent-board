# Explicit development command and Board connection

This is an opt-in local coordinator, not a background listener or queue consumer.
It uses the owner's paired Board connection and personal Codex CLI. No API fallback,
authentication copy, HOME/CODEX_HOME change or implicit profile re-registration is
available. The same model is fixed for the operation. The current native policy
and supervised named-test backend must already be qualified on this Darwin host;
unsupported or changed plans refuse. This release has only fixture-model tests
against a temporary Board. It has not run a real model, MCP endpoint or repository.

## Existing reviewed controller inputs

An operator first prepares the immutable broker manifest, named-test backend and
planner policy using the controller APIs in `DEVELOPMENT.md`, `SUPERVISED_TESTS.md`
and `PLANNER_POLICY.md`. Qualification is a separate explicit synthetic action;
`development prepare` never qualifies or starts a subprocess. The real checkout
must be disposable and exclusive. The planner workspace is empty and separate.
Credentials, controller code, journals, toolchain and canonical fence stay outside
the test checkout. Paired connection files require owner-only mode 0600, no
symlink ancestors/final component and exactly one hard link.

The manifest resource and every manifest action must be explicitly present in the
current effective owner/profile policy. Empty action/resource lists refuse before
claim, Runtime reservation, model or test. The coordinator never grants these
rights or infers them from a project description/comment. No live pilot should be
started while the owner's grants are empty.

The new command group is `blackoutburst-agents development --journal PRIVATE_DIR`.
A private journal directory is selected once. Its association is then pinned in
the canonical fence; renaming it cannot create another execution for this checkout.

- `prepare --state RUNS --runtime-journal RUNTIME --connection-file CONNECTION
  --session SESSION --owner OWNER --agent PROFILE --project PROJECT --ticket TICKET
  --request STABLE_KEY --model MODEL --instruction-file INSTRUCTIONS --broker BROKER
  --test-backend BACKEND --planner-policy POLICY` reads the selected Board scope and
  seals the instruction, input directories/inodes, manifest, qualification hashes,
  explicit model and finite budgets. It does not claim/renew a ticket, reserve
  Runtime, create a canonical execution fence or run a model/test/patch. Optional
  finite limits are `--max-turns`, `--max-actions`, `--max-wall-seconds` and
  `--turn-timeout-seconds`.
- `run --operation UUID --connection-file CONNECTION` checks the fixed scope and
  takes or renews only that explicitly selected ticket. It records intention before
  the MCP effect, confirms the exact optimistic revision/claim, captures the actual
  post-transition context and then starts the finite planner. Claims from other
  sessions/devices and expired claims require human reconciliation; no recovery is
  attempted automatically. The wall budget is narrowed to the remaining confirmed
  lease minus cleanup margin before it is sealed. Later execution does not silently
  renew/rebase its context; an expired/changed lease stops.
- `reconcile --operation UUID --connection-file CONNECTION` confirms exact cached
  model/broker/Runtime receipts. It never takes or renews a claim, infers, patches
  or reruns a test. A merely prepared operation or unexecuted claim intent remains
  pending without any MCP call. A recovered
  ready state requires a separate explicit `run`. A missing receipt, changed
  authority/context or uncertain effect remains for review.
- `publish --operation UUID --connection-file CONNECTION` explicitly publishes the
  dated audit in ticket evidence, sets `review` and releases this own claim. It does
  not mark the ticket done, merge or apply code. It revalidates live authority and
  the sealed shared context; retries compare the exact publication fields/version
  rather than append a duplicate. The audit describes the inspected snapshot,
  never a guarantee that current files still have those hashes.
- `status --operation UUID`, `stop --operation UUID` and `inspect --operation UUID
  --output PRIVATE_FILE` are offline. Stop uses an irreversible private marker
  independent of the work lock, so it remains available during a model/test.
  Inspection writes a new 0600 file in a private 0700 directory, never the checkout
  or empty planner workspace. Both directory identities are revalidated before
  export; a renamed/replaced directory refuses. It may include private instruction/context/model
  material; stdout contains only bounded metadata, hashes and selected relative
  source paths.

## Fresh context and authority

Only initial explicit preparation registers the selected session. Reopening adopts
its saved identity/version; the server refuses a changed or paused profile. The
narrow client allows selected ticket/thread reads, a limited `get_work` policy
read, exact claim/renew and explicit review publication. It cannot read the inbox,
select queue work, send general messages, delegate or execute a shell.

Every effect checks current owner/profile policy, project membership/archival,
assignment/status/dependencies/human waits, and the exact own claim with its lease.
`build_context` recomposes the bounded shared context, verifies its sealed
fingerprint, and the coordinator rechecks the ticket revision after composition.
Shared text remains untrusted model data. The known claim/renew can change only
its expected ticket status/revision; all other source hashes must remain identical.
A simultaneous external message, project/ticket edit or policy change stops the
operation instead of being silently folded into the new baseline. Final context
is captured after the confirmed own transition, avoiding a stale pre-claim prompt.
This is checked multi-read composition, not an atomic server-wide transaction.

During native tests, freshness checks can take time; slow/lost Board responses
fail closed and may cancel the supervised test. Revoked authority can prevent
normal context-sensitive reconciliation/publication. The operator must then use
the existing private/Runtime inspection and human reconciliation workflow; this
release never refreshes the old profile or asserts cleanup from lease expiry.
For an exact cached **model** result, identity-only Runtime reconciliation accepts
`runtime --journal RUNTIME reconcile --operation RUNTIME_UUID --state RUNS
--connection-file CONNECTION --development-operation WORKJOURNAL_UUID`. It verifies
the persisted real scope and derived work UUID/private alias, then confirms only
the existing cleanup receipt. Another parent UUID refuses. This path does not
register, claim, admit execution, infer, or use revoked source grants. It is not a
local-broker test/patch replay or a way to resume the development operation. Local
broker ambiguity still requires its exact private receipt and operator workflow.

## Parent fence and interruption

Before any claim/renew, run reserves the canonical same-user checkout fence with
the WorkJournal UUID, journal path/inode, configuration hash and model/runtime
child scope. A conflicting existing parent is rejected locally before any MCP call
in run. After confirmed claim and actual context capture, the planner attaches its
configuration to exactly that parent UUID. There is no adoption, release, expiry
or replacement-key shortcut. Cuts after linkage, claim or context capture reuse
that exact operation and optimistic transition.

Each operation preallocates a deterministic model-work UUID from its WorkJournal
UUID and a distinct private local alias. A new operation never adopts the provider
session or turns of older work, even for the same ticket, empty planner workspace
and runs directory. Runtime guards verify the operation/alias/work correspondence
before admission; server scope still uses the real Board agent/session. Reopening
the same operation reuses exactly its existing UUID/session. The optional
`RunStore.create_work(work_id=...)` extension refuses any scope/ID collision;
callers that omit it retain their existing behavior. Production `DevelopmentPlanner`
construction requires this parent operation; parentless legacy behavior is only
an internal fixture path.

An uncertainty between final
attachment and planner-journal creation remains for explicit review.

The first parent linkage marks local fence format 2 transactionally. New readers
retain format 1 records; old readers refuse format 2. Stop older development
coordinators before introducing the extension. This does not change the Board
server database. The fence remains one OS user/machine and cannot resist an
administrator erasing private state or coordinate multiple hosts. It retains
completed audit roots until explicit operator resolution; using a fresh isolated
checkout for the next operation is the supported route in this release.
