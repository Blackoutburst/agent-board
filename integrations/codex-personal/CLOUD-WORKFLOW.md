# Explicit cloud workflow

The installed `run-workflow` entry coordinates existing concrete components in
one finite Linux unit: selected coding jobs, actual tested postimages, a sealed
commit and GitHub draft PR, author evidence/handoff, then an independent review
whose opinion is visible on the same ticket. It never marks a ticket done or
merges a PR. A completed workflow can report a review requesting corrections.

`run-batch` remains available for the original private-results-only operation.
The original `linux-finite-pool-batch-v1` still requires exactly four jobs. The
new explicit `linux-finite-pool-batch-v2` accepts one to four jobs with matching
local concurrency. Both schemas retain the same closed job fields and durable
intentions. A previously interrupted batch is historical only; restarting the
unit does not replay submission, claims, qualification, inference or patches.

## Two protected operator files

`config/batch.json` uses the version-two batch schema described in the addon
README. Its tickets, coder profiles, paired connections, repository/base,
existing relative paths, fixed Python verifier, model and finite budgets are
chosen explicitly. Author assignment must be in place before coding. The
timeout covers coding, qualification, delivery and review together.

The new `config/workflow.json` is a private 0600 file in the same 0700 root.
It selects each coder by its exact batch name and a distinct reviewer profile:

```json
{
  "schema": "linux-cloud-workflow-v1",
  "batch_id": "THE-SAME-UUID-AS-BATCH",
  "jobs": [{
    "name": "coder0",
    "paths": ["existing_module.py"],
    "commit_message": "Fix the selected behavior",
    "pr_title": "Fix the selected ticket",
    "pr_body": "Implement and test the explicitly selected behavior.",
    "reviewer": {
      "agent_id": "DISTINCT-REVIEWER-PROFILE-ID",
      "connection_file": "/var/lib/blackoutburst-agent-pool/owners/bb-agent-thescreamer/connections/reviewer/codex.json",
      "instruction": "Review the exact tested change and state any limitations.",
      "model": "OPERATOR-SELECTED-MODEL",
      "skills": ["review", "python"],
      "timeout": 120
    }
  }]
}
```

There must be exactly one workflow job per batch job. Reviewer identities and
connections differ from their author; a reviewer may review several jobs
sequentially. The destination is fixed protected code:
`Blackoutburst/agent-board`, base branch `main`, at the exact selected base
commit. No destination, path, executable, model, title or grant is taken from
model output or a Board message. Use normal official gh authentication with
write access under the same Unix account/HOME; no token is copied or passed in
configuration. Codex likewise uses that account's normal official login.

The controller reads actual postimages only for the chosen existing paths.
Unchanged paths are omitted; an empty change refuses. The concrete delivery
proof checks the complete source tree against the real planner, test and
Runtime receipts. An unexpected edit after testing, undeclared change, new or
deleted file, mode change, ignored file or missing proof refuses publication.
The reviewer receives a bounded exact diff built from the sealed source, runs
with the concrete Linux review binding, and publishes its exact opinion using
the lifecycle's durable CAS protocol. Oversized material refuses rather than
silently omitting part of the change.

## Inspect then start

From the reviewed root-owned release, under the existing owner account:

```sh
python3.10 -I -B "$RELEASE/pool_entry.py" --release "$RELEASE" --owner "$OWNER" \
  workflow-plan --batch "$POOLROOT/config/batch.json" --workflow "$POOLROOT/config/workflow.json"
```

The plan prints the selected ticket/coder/reviewer/model/path metadata and the
configuration hashes. It neither registers a profile, qualifies execution nor
launches a model. The installed unit calls the matching `run-workflow` action
with these two files. `run-workflow` requires the exact installed unit, UID,
normal HOME, tools and public source release; it is not a way to run outside the
qualified unit. Installation stays disabled/stopped and starting is explicit.

The same Pool remains open through delivery and review. SIGTERM keeps its Stop
companion alive across those phases and additionally calls the real delivery
Stop for an author already settled. Admission Stop alone does not cancel that
publication. A command already launched is not rolled back. Unknown cleanup,
failed Stop, interrupted preparation or ambiguous publication remains for
review and retains the existing evidence; no alternative job/key/PR escapes it.

A completed retry checks its dated underlying delivery and review receipts
without registration, RPC or execution. It does not promise that a later human
has left the ticket or GitHub PR unchanged. Four actual simultaneous code models,
both owners' installations and a live GitHub workflow require separate real
acceptance; synthetic integration tests do not constitute those observations.
