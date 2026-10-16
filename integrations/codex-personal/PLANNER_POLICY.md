# Explicit read-only planner policy

`PlannerPolicy` is an opt-in transport boundary for a trusted local development
controller. No worker, queue, event, listener or CLI command activates it by
default. `PersonalRunner` and its durable state API are unchanged; a controller
may explicitly pass `partial(execute_turn, planner_policy=policy)` as transport.

The planner uses an **exclusive empty 0700 directory**, separate from the checkout,
private journal, controller package and toolchain. The model receives repository
content only through the controller's bounded, authorized broker responses. The
planner's workspace cannot contain repository files, configuration or artifacts
when a turn starts. Directory identities, including case aliases on Mac, prevent
substituting an overlapping checkout through another spelling.

## Preparation and qualification

`prepare_planner_policy(state, workspace=..., checkout=..., executable=...,
cli_version=...)` writes an immutable private plan. Preparation launches nothing.
The plan pins the CLI version and binary hash, qualification interpreter hash,
host OS/release/architecture, checkout/workspace directory identities, permission
profile version, exact policy and synthetic canary hash. The state is owned 0700
with regular single-link 0600 files outside the readable planner workspace.

Open it with `PlannerPolicy(state)`, then explicitly call `qualify()`. Missing,
failed, stale or corrupt qualification never authorizes a turn. Linux, Windows
and root controllers are refused. Qualification uses no model, authentication
RPC, login command, credential copy or API key. It runs `--version`, followed by
one trusted synthetic command and a temporary loopback listener.

The command checks five conditions in its actual native sandbox: a synthetic
file inside is readable; creating a file inside is denied; reading and writing
synthetic files outside are denied; loopback connection is denied. The CLI is
selected using `-c default_permissions`, **without `-P` or legacy `--sandbox`**.
Its current `sandbox` command uses the process cwd; `-C` requires `-P` and is
deliberately omitted. Any unsupported selector, failed guard, timeout, output
limit, changed pin or unconfirmed cleanup leaves qualification disabled.

```python
from functools import partial
from blackoutburst_agents.codex_turn import execute_turn
from blackoutburst_agents.planner_policy import PlannerPolicy, prepare_planner_policy

prepare_planner_policy(
    "/absolute/private/planner-state", workspace="/absolute/private/empty-planner",
    checkout="/absolute/disposable/checkout", executable="/absolute/path/to/codex",
    cli_version="EXACT INSTALLED VERSION",
)
with PlannerPolicy("/absolute/private/planner-state") as policy:
    policy.qualify()  # Explicit local synthetic qualification; no model.
    transport = partial(execute_turn, planner_policy=policy)
    # A separately authorized controller may use transport, while this context is open.
```

`fingerprint`, `workspace`, `workspace_identity`, `executable` and `cli_sha256`
are stable metadata for the controller's own immutable operation binding.
`validate_for(executable, workspace)` rechecks the private plan, current binaries,
host, directory identities, empty workspace and exact completed qualification.
Paths are supplied only by the local controller, never discovered from a board
message or a model response.

## Transport behavior

`execute_turn(..., planner_policy=policy)` validates before inspecting the client,
then again immediately before launch. It retains `--ignore-user-config`, forced
ChatGPT/OpenAI selection, no approvals, disabled web search and all existing
disabled execution/plugin/browser features. The optional path replaces only the
legacy `--sandbox read-only` with the qualified default permission profile:
root denied, minimal runtime reads, exact planner workspace read-only, temporary
roots denied and network disabled. Exact session resumption and bounded process
observation remain unchanged. A refusal never retries with a broader policy.
Without this option, the existing argument vector and behavior remain unchanged.

Permission profiles are beta. The official
[permission documentation](https://learn.chatgpt.com/docs/permissions) distinguishes
local command policy from client service traffic and warns that legacy sandbox
flags override profile selection; the
[CLI reference](https://learn.chatgpt.com/docs/developer-commands) documents the
separate exec and sandbox options. The installed binary and a fresh local
qualification are required; documentation alone is not an enforcement receipt.

## Scope of proof

The native fixture measures five synthetic command restrictions on the recorded
Darwin host. It does **not** run a real model turn or certify model/authentication
traffic, the unsandboxed main CLI process, arbitrary privileged programs,
another operating system or a compromised host. Controller, toolchain, managed
configuration and exclusive ownership of the empty workspace remain trusted.
The CLI keeps its existing HOME/CODEX_HOME values; this code neither opens nor
copies their authentication files. Permission profiles govern local sandboxed
commands, not every client surface. The controller must still keep execution
features disabled and validate all broker operations and publication separately.

Requalification is explicit. A process/filesystem failure can leave an
unqualified fixture requiring inspection; an unexpected file is not silently
adopted or removed to make the workspace appear empty. No development grant,
model invocation, production activation or autonomous listener is included here.

The [synthetic Mac receipt](docs/planner-policy-2026-10-16.json) records the exact
binary/module hashes, five guards and removed disposable fixture, with no host
paths, private output or credentials. To reproduce on an explicitly authorized
host:

```sh
PYTHONPATH=integrations/codex-personal python3 \
  integrations/codex-personal/tests/qualify_planner_policy.py \
  --codex /absolute/path/to/pinned/codex --output /private/tmp/planner-proof.json
```
