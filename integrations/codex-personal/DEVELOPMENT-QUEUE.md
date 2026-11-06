# Admit one development ticket from a protected catalog

SDK 0.47.0 adds a punctual admission controller for ticket #72. It discovers
ordinary ready work through an already paired coder's own connection and writes
one new finite v3 batch. Preview and admission launch no Pool job, model, test,
Git command, Runtime or service. They never claim work or change permissions.
The later existing finite runner owns execution, with its normal qualification,
Stop, Runtime and cleanup rules. This is not the permanent scheduler/recovery
ticket #73 or proof of four actual simultaneous models (#51).

Execution requires Board 2.18.1's `claim_selected_queue_ticket` and an explicitly
queue-authorized profile/session. A requested-only profile can preview but
cannot admit executable work. No grant is inferred from a catalog, role name,
model preference, transport kind or companion metadata.

## Protected operator catalog

Use the existing private owner root. Its `config`, `connections`, `repositories`,
`checkouts` and `verifiers` directories must already exist with mode 0700.
Configuration/connection/verifier files are owned regular 0600 files with no
hardlink alias. Existing finite execution also requires its normal `batches`
and `git-state` directories. Do not point the root at a checkout or create a
new connection by copying another owner's authentication state.

The catalog fixes one owner, coder profile/version, project and connection,
and 1–16 unique resource entries. Each resource maps to an approved local Git
repository/base, file paths, actions, fixed verifier/hash, model/reasoning and
per-cycle budgets. Board/model text cannot supply these values. Workspace paths
are derived as `root/checkouts/<new request UUID>/<entry ID>`; the catalog cannot
provide a workspace template or change a default configuration pointer.

This example writes a new catalog after the operator replaces the explicit
IDs, commit and paths with reviewed values. It never reads the connection's
credential content or starts an executor:

```python
from pathlib import Path
from blackoutburst_agents.dev_journal import PrivateDirectory
from blackoutburst_agents.dev_manifest import Budgets, canonical, digest
from blackoutburst_agents.dev_planner_journal import PlannerBudgets

root = Path('/absolute/private/owner-root')
verifier = root / 'verifiers' / 'approved.py'
catalog = {
    'schema': 'development-queue-catalog-v1',
    'owner_id': 'thescreamer', 'agent_id': 'EXISTING_CODER_PROFILE_ID',
    'profile_version': 5, 'board_endpoint': 'https://board.blackoutburst.com/mcp',
    'project_id': 'EXISTING_PROJECT_ID',
    'connection_file': str(root / 'connections' / 'coder.json'),
    'timeout_seconds': 3600,
    'entries': [{
        'entry_id': 'approved-code', 'resource': 'repo:agent-board-cloud-pilot',
        'repository': {
            'repository_id': 'agent-board',
            'repository': str(root / 'repositories' / 'approved'),
            'base_commit': 'REPLACE_WITH_APPROVED_40_HEX_COMMIT',
        },
        'paths': ['integrations/codex-personal/CLOUD-WORKFLOW.md'],
        'actions': ['repo.read_file', 'repo.patch_file', 'repo.run_test'],
        'verifier_path': str(verifier), 'verifier_sha256': digest(verifier.read_bytes()),
        'test_name': 'approved-fixed-test', 'test_timeout_seconds': 30,
        'test_max_output_bytes': 4096,
        'instruction': 'Handle the selected ordinary ticket within these exact files and run the approved fixed test.',
        'model': 'EXPLICIT_AVAILABLE_MODEL', 'reasoning_effort': 'low',
        'planner_budgets': PlannerBudgets().document(),
        'broker_budgets': Budgets().document(),
    }],
}
with PrivateDirectory(root / 'config') as directory:
    directory.create('development-catalog.json', canonical(catalog))
```

Unknown fields, duplicate entry IDs/resources, reserved resources, file/hash
changes, out-of-root paths and unavailable scopes refuse the catalog. Documents
are bounded to 128 KiB and candidate reads to 200. Selection keeps the Board's
priority order, filters to catalog resources and excludes delegation families,
recovery claims and review/manual work. An empty queue needs no model call.

## Preview and admit

Use a stable dedicated discovery session. It never takes a claim. Preview
registers that session as non-autonomous and reads the current project/work:

```sh
python3 -m blackoutburst_agents development-queue preview --root /absolute/private/owner-root --catalog /absolute/private/owner-root/config/development-catalog.json --session coder-queue-discovery-01
```

Inspect `selected`, `execution_authorized` and `approval_sha256`. The fingerprint
binds exact local catalog/connection pins, identity, project context, current
permissions and selected ticket/version. Presence, session autonomous state and
project counters/timestamps do not change it. If executable admission is
authorized, explicitly supply that fingerprint and a new UUID:

```sh
python3 -m blackoutburst_agents development-queue admit --root /absolute/private/owner-root --catalog /absolute/private/owner-root/config/development-catalog.json --session coder-queue-discovery-01 --request-id NEW_UNIQUE_UUID --approve-sha256 EXACT_PREVIEW_SHA256
```

Admission persists an intent before autonomous registration or batch creation,
then rechecks the same selection. It writes a new named config under `config`
and a closed receipt under `admissions/<request UUID>`. It never dispatches it.
The receipt has `launchable:false`, `model_calls:0` and the new `batch_file`;
these fields are not execution authority. Feeding that exact file to the
existing finite runner remains a separate explicit protected operation.

Repeat the same admission command after a lost response. The controller looks
up the request before new discovery, validates local pins and current access,
and returns the same historical receipt without selecting another ticket or
rewriting the batch. This remains true when the original ticket has progressed
or the queue is empty/different. An incomplete intent or conflicting catalog,
request, context or receipt refuses rather than adopting or repairing it.

## Claim boundary and limits

Finite v3 carries `queue_admission` with the exact ticket version, profile
version and catalog SHA256. The preparer checks these during qualification; the
driver and worker check them again before execution. The final job's unique
session registers `autonomous:true` and makes the only claim through
`claim_selected_queue_ticket`, using a stable persisted operation key.

In one Board transaction, that endpoint validates current queue rights, replays
an exact prior decision, checks unfinished work for the whole profile across
sessions/devices, checks the selected fresh ordinary Todo and performs the CAS.
It also remembers negative decisions: releasing another job cannot turn an old
refused key into a new admission. A lost positive response is reconciled by
reading that same claim/version/context. There is no fallback to manual claim,
`claim_next_ticket`, another ticket or a new key. Queue clients cannot call
`claim_ticket` or `renew_ticket`; the model's broker actions are unchanged.

Two controllers may prepare the same candidate, or different resources for the
same coder. Only the winning atomic claim can reach Runtime/model execution.
The loser stops without a model call. Historical v1/v2 batches retain their
existing explicit claim/renew path and contain no queue admission metadata.
Quota recovery, automatic rescheduling and ambiguous-interruption reconciliation
remain explicit #73 work; no completion or cleanup is inferred here.

Since 0.50, a `linux-cloud-queue-slots-v1` continuation composes several such
standing queues, one coder profile and private root per slot, under an owner cap
of 1–8. Each slot still admits through this exact boundary; see
[CONTINUATION.md](CONTINUATION.md#plusieurs-emplacements-de-développement-51).
The real run with four simultaneous subscription models has not been performed.
