# Closed Linux planner refusal diagnostics

When qualification fails, adapter 0.36 retains the existing first stdout line
(`status=needs_review`, the refusal reason) and exit code 3. A second line with
`status=qualification_diagnostic` and `kind=linux_planner_canary` exposes only:

- `checks`: the fixed five boolean checks, or an empty object if no valid
  canary result was decoded;
- `cleanup_confirmed`: the attempt's cleanup result, without treating unknown
  cleanup as success;
- `phase`: one of `cli_version`, `host_fixtures`, `canary_pins`,
  `canary_execution`, `canary_result`, `cleanup`, `final_pins`;
- optional `errno`: a controller `OSError` number in 1–4095.

The optional errno describes a controller operation. It does not identify the
errno of an individual check inside the sandbox; the canary output retains its
original schema. Boolean results alone cannot establish which child errno
caused a failed check.

Only the exact `PlannerPolicyError` type with an allowlisted qualification
reason and a fully validated payload produces this line. The entry rejects
extra fields, unknown checks, integer substitutes for booleans, unexpected
phases and out-of-range errnos. Copies prevent alias changes after capture.
There is no exception text, stdout/stderr, path, fingerprint, account identity,
grant, conversation or model output in the projection.

This is an observation of the current failed attempt, not permission to rerun
it or evidence of model execution. It does not read historical journals,
create a diagnostic process, call a service or change a persisted receipt.
All qualification checks, fixture ownership guards, cleanup conditions,
budgets and refusal rules remain in force. Historical failed batches remain
preserved; a new run requires a distinct explicit batch.
