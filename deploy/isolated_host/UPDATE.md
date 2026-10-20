# Existing installation: public code and managed control channel

This updater is separate from the initial installer. It requires an existing
installation whose two owner units are already `inactive`, `dead` and
`disabled`. It never stops a unit to satisfy that requirement.

`prepare --unit-mode legacy` retains the original foreground entry point and
is the default. `prepare --unit-mode managed` selects the tracked managed unit
template. The template's exact bytes enter the bundle inventory and change its
full bundle ID. The managed entry is `managed-run`; every other service
restriction is retained. Neither preparation nor installation starts it.

The update command requires both full 64-character IDs:

```sh
python3 -m deploy.isolated_host.manage update --bundle /ABSOLUTE/PUBLIC/BUNDLE \
  --expected-old-release OLD_FULL_ID --expected-new-release NEW_FULL_ID
```

Without `--apply`, it validates the supplied public bundle and prints a
prospective plan. It does not assert that the live installation passed
preflight. Applying the reviewed plan requires the administrator, Linux,
the same toolchain, endpoint and owner mapping, and an installed old release
matching the old ID. Runtime Python and both bundled ELF tools retain their
exact pins. No download or dependency installation occurs.

The updater reads the old root-private installation receipt only for its
bounded installation metadata and the two recorded UID/GID mappings. It checks
the existing account names, primary groups, home paths and home directory
metadata. It does not open any owner file: no login, credential, connection,
configuration, work journal or Runtime reservation is copied or changed.

Both old unit files must match the old template rendered for the pinned old
release. Loaded unit paths, disabled state, dead state, process metadata,
drop-ins, aliases and public systemd link paths are checked again at effect
boundaries. A concurrent service start is a refusal, never an instruction to
stop or repair a service.

All directory ancestors are traversed with retained descriptors and
`O_DIRECTORY | O_NOFOLLOW`. Public inventories are bounded and reject unknown
files, links and empty directories. New releases and recovery directories are
created exclusively; an existing partial directory is never adopted.

Unit replacement requires Linux `renameat2(RENAME_EXCHANGE)`. There is no
check-then-overwrite fallback. The exchanged old inode and its exact bytes must
match the prior observation before the operation is accepted. Unexpected
inodes are preserved. `RENAME_NOREPLACE` archives recognized exchanged files in
the private recovery directory. These primitives are not qualified by the
simulated unit tests; unsupported hosts refuse the update.

Backups are retained under `/var/lib/blackoutburst-agent-host/updates/UUID/`.
Each immutable, fsynced phase record is created before its corresponding
effect. An error attempts to restore only recognized unit writes from this
operation, while the units remain inactive and disabled. An unrecognized
replacement is never overwritten to manufacture a successful rollback.
If an exchange succeeds but its archival or fsync fails before its write is
confirmed, the new inactive unit may remain in place. The operation then
requires review; complete automatic restoration is not guaranteed.
The old release, new release, backup and any incomplete stage remain
inspectable; no release or owner data is deleted.

An interrupted update is never resumed or adopted. Failure during the final
installation receipt is treated as an ambiguous commit: units and receipts are
preserved for review, rather than restoring units below a possibly committed
new receipt. Success is recorded only after that commit boundary.

The only service effect is `systemctl daemon-reload`. The updater does not
invoke an agent CLI, provider, model, pairing, `managed-prepare`, account
creation, grant, budget, activation, enable, start, stop or restart.
`updated_disabled` is not execution qualification or confirmation that either
owner has configured the managed channel. Those are separate explicit steps.

Tests use synthetic public files, fake systemd metadata and a simulated
exchange primitive. They do not run an updater as root, alter a real service,
exercise an account or claim a Linux backend qualification.
