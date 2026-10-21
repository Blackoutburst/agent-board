# Explicit Linux planner policy qualification

This recipe qualifies only the CLI's read-only planner sandbox on the exact
observed Linux host, unprivileged UID, namespace/cgroup/security context,
interpreter, CLI and public source inventory. It does not qualify the Python test
backend, an owner subscription, a model turn, a Board session or a service.
Nothing in this recipe selects or activates a worker. Do not reuse a historical
negative or a receipt from another source tree. A successful outside write is
always a refusal, even if other checks pass.

The recipe has not been executed as part of this implementation. Unit tests mock
all Linux metadata and CLI observations; those tests are not qualification
receipts. The source-only policy branch deliberately lacks the new execution
modules required by its closed inventory and therefore refuses product
preparation until the complete reviewed integration is present.

## Reviewed entry points and prerequisites

The two entry points are `launch_linux_planner_policy_qualification.py` and
`qualify_linux_planner_policy.py`. The operator must approve the complete final
source, these two exact scripts and their SHA-256 values before any invocation.
Execute the launcher itself from externally verified, retained bytes; checking a
pathname and then running that pathname is insufficient. The launcher verifies
and retains the qualifier bytes before compilation and supplies their observed
executed hash to the qualifier. Neither script self-attests by rereading
`__file__`.

Use a fresh, uninstrumented Linux x86_64 process running a canonical regular
Python executable with `-I -B -S`. Python 3.10 or newer is required. The operator
must supply the exact nodename, kernel release and nonzero UID, plus hashes for
the explicit canonical CLI and Python executables. Paths containing symlink
components fail closed; for example `/usr/bin/python3` may need to be replaced
by the reviewed regular binary it designates. No account or authentication
files are inspected or copied and HOME is not overridden. The CLI keeps its
normal configuration. No `--ignore-user-config` placement is assumed for the
sandbox command; future changes to that invocation require a new review and
qualification.

Build the source inventory from the final reviewed public package, using exactly
`LINUX_EXECUTION_SOURCES` (also closed literally in the qualifier). Its canonical
UTF-8 JSON representation, without a trailing newline, is:

```json
{"files":{"<each exact allowed filename>":{"bytes":123,"sha256":"<reviewed sha256>"}},"schema":"blackoutburst-linux-policy-source-inventory-v1"}
```

All filenames must be present, with no extra name, no symlink and no unpinned
import fallback. The operator pins the inventory's own SHA-256 independently.
The qualifier holds each file's verified bytes, imports only that held package
and compares it with the live policy's inventory. File bounds are 256 KiB each
and 4 MiB total. These are public sources, not configuration, user input or an
authentication home. Root/current UID ownership and no group/other write access
are required. The controller trusts the reviewed interpreter, its isolated
standard library and the host kernel; this is not a hostile same-UID process
isolation claim.

## One explicit attempt

After the external launcher verification, its argument shape is:

```text
python -I -B -S <held verified launcher>
  /absolute/qualify_linux_planner_policy.py <qualifier sha256> --
  --run-synthetic-policy-probe
  --package /absolute/reviewed/blackoutburst_agents
  --source-inventory /absolute/reviewed/source-inventory.json
  --expected-source-inventory-sha256 <inventory sha256>
  --codex /absolute/canonical/codex
  --expected-cli-sha256 <CLI sha256>
  --cli-version "codex-cli <exact version>"
  --interpreter /absolute/canonical/python
  --expected-interpreter-sha256 <Python sha256>
  --expected-node <exact nodename>
  --expected-release <exact kernel release>
  --expected-uid <nonzero UID>
  --root /absolute/new-attempt-directory
  --output /absolute/new-report.json
```

This is an argument contract with unresolved final pins, not an activation
command. Both root and report must be new. Preparation is process-free. The
explicit `qualify()` attempt runs only `--version`, followed by the fixed
synthetic filesystem/loopback canary through the same default permissions
selector later returned by `validate_for`. It never invokes `exec`, login,
subscription checks or a model. The selector denies root/tmp reads, allows only
the planner workspace read access and disables tool network access.

The private attempt marker is durable before the first CLI call. A partial,
failed, ambiguous or completed attempt is never retried in that directory. The
bounded runner stops/reaps only the exact process session it creates. Cleanup
requires the expected directory identities, exact predefined file sets and
retained file descriptors. Unknown entries or substitutions are retained and
reported as cleanup unconfirmed. No recursive deletion or directory adoption is
used. The attempt root, checkout, planner directory and private policy receipts
are intentionally retained, even after a clean canary result.

## Evidence and refusal

The public JSON report is exclusive, bounded and contains hashes, observed
host/UID, the five boolean checks and positive/negative cleanup observation.
Proc labels, cgroup paths, source bytes and CLI output are not copied into it.
The environment metadata remains in the private policy plan and is represented
publicly by its canonical hash. A refused attempt may have cleanup true; that
never makes the policy qualified. A successful mock test is never such a report.

The checks are: permitted inside read, inside write denied, outside read denied,
outside write denied and loopback connection denied. The controller independently
checks that both denied-write destinations remain absent. Every check must be
exactly boolean true; malformed output, changed sources/environment/toolchain,
unknown cleanup or any false check prevents validation. Receipts bind the exact
plan, environment, source pins and one attempt marker. `validate_for` launches
nothing and rechecks those live pins. A backend paired with this policy must
independently compare the same execution environment and source pins.
