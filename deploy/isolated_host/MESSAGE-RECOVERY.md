# Explicit recovery of a legacy pretransport message attempt

This is an operator procedure, separate from the normal managed worker. It
resolves one precisely identified legacy attempt. It does not send a reply,
start a provider, return dispatch budget, checkpoint a delivery, or enable a
target. A message left pending is not evidence that no remote Runtime exists.

## Install compatible public source first

Use the reviewed `update.update_existing` procedure with both full release IDs.
Both owner services must remain inactive, dead and disabled, with zero process
IDs. The existing updater retains the old protected release, checks identical
Python/Codex/bwrap pins and owner identities, and changes only public source and
the two disabled unit definitions. It must not copy authentication, configuration
or private journals. Do not use the initial installer or reconfigure an existing
host to bypass a journal or catalogue conflict.

Run the protected `owner_entry.py` with the pinned Python interpreter, under the
actual Unix owner, using the installed release directory. The entry rechecks
both unit definitions against that exact release and their loaded public service
metadata. The owner lock remains held throughout the finite operation. This
does not claim authority over a malicious root administrator.

## Review, apply, resume

`managed-message-abandon` exposes four explicit operations:

- `plan` validates the existing host, message, run and Runtime journals and the
  original source/catalogue chain. It produces a closed proposal and prints
  only its SHA256. It does not call the Board or create a recovery intent.
- `apply` requires that reviewed proposal SHA256 and the expected initial
  intent revision. The coordinator records a durable intent before requesting
  the exact legacy admission seal with the original paired target connection
  and original session. It never registers or refreshes the agent profile.
- `status` reads a previously created intent by its stable request key. Output
  is restricted to a canonical intent UUID, revision, fixed phase/reason codes,
  and zero-model/no-activation metadata.
- `resume` requires the same reviewed proposal hash, exact intent UUID and
  current revision. It checks every explicit host/configuration/target/batch/
  worker/device pin against the stored proposal. An intent whose seal response
  was lost requires the additional explicit `--retry-seal` option before another
  seal RPC; inspection alone never retries a request.

All operations require the full old protected release ID, original configuration
SHA256, host instance, target, batch, worker, original device and stable request
key. No journal, configuration, connection or executable path can be supplied
through this command. A missing journal, sidecar, changed connection/source,
active service or unexpected predecessor refuses the operation. Local journal
and work locks do not establish the state of an unrelated Runtime.

The coordinator requires the exact legacy blocked result, no recorded process or
provider session, and the original uncertain Runtime without a positive receipt.
The old result's cleanup flag remains historical data; it is not remote cleanup
evidence. Only the authenticated, permanent admission seal authorizes the new
`abandoned_never_started` Runtime terminal and the corresponding explicit local
abandonment. A remote positive Runtime prevents this seal.

The local durable order is intent, confirmed seal, Runtime abandonment, RunStore
abandonment, retained message abandonment, and completed host dispatch with an
abandonment receipt. Each step checks its exact predecessor. A crash retains the
intent and only the same explicit operation can resume it. The old result, IDs,
delivery/feed/checkpoint, configuration, activation, generations and budgets are
preserved. Abandoned message history is retained through any later ordinary ACK.

## Keep source and catalogue transitions separate

This procedure still verifies the original launch catalogue. Installing new
source does not itself replace it. After the attempt is resolved, a separate
reviewed catalogue transition must confirm actual human Stop commands and fence
all concurrent instance changes before replacing launch hashes. Neither
`managed-prepare` nor starting `managed-run` is a recovery shortcut.

`managed-catalog-source` performs this second, finite operation. It requires
the same original release/configuration/host/target/batch/worker/device pins,
the completed abandonment request key (`--abandon-request-key`), and a separate
stable catalogue request key (`--request-key`). Both services must still be
inactive and disabled. The original owner and target connection remain in use.

- `plan` validates the completed abandonment and public release chain without
  polling the Board or changing any journal. It prints only a proposal SHA256.
- `apply` requires that reviewed hash and initial revision zero. It durably
  records the source transition before processing an already requested human
  Stop. It never creates a Stop, Start, Restart or provider turn. After the
  actual Stop is confirmed, the Board replaces launch hashes using exact host,
  catalogue and instance fences. A fresh read must confirm the result before
  the local catalogue is committed.
- `status` and `resume` use the derived source intent ID, exact proposal hash
  and current revision. A lost reply keeps the original immutable request;
  explicit resume retries that same request and then checks current state.

The source intent blocks normal worker admission until complete. At every
boundary, the procedure verifies the original abandoned children independently
of legitimate changes to the Host catalogue or generation. The older complete
abandonment proof is retained; its whole-Host checkpoint is never rewritten to
pretend that a later Host epoch was the original one. Any new child, changed
configuration, unexpected command or contradictory remote state stops recovery.

Completion leaves the instances stopped and preserves their IDs, profiles,
sessions, configuration, activation history and consumed budget. Starting the
service, granting a new bounded Start and posting a new human message are
separate decisions. The abandoned historical message is never replayed.

Once the private journals contain Host3, Runtime4 or Message2 state, do not start
an older adapter or restore pre-recovery journals. Keep the compatible protected
release and permanent Board admission seal. The old release is retained for
source provenance, not as permission to replay the abandoned attempt.
