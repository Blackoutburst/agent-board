# Explicit source catalogue replacement

`replace_server_catalog` is distinct from bootstrap `publish_server_catalog`.
It accepts exactly `host_id`, `expected_catalog_version` (at least 1),
`expected_host_version`, `expected_instances_sha256`, `entries` and
`idempotency_key`. The closed payload retains the 16-entry/16-KiB limit.

The fence is SHA-256 of sorted compact ASCII JSON containing schema
`blackoutburst-server-catalog-fence-v1` and instances sorted by `catalog_key`.
The fixed fields are `CATALOG_FENCE_FIELDS` and `CATALOG_COMMAND_FIELDS` in
`server_instances.py`; the SDK's `server_control_protocol.catalog_fence` has the
same projection. It binds identity, launch hashes, generations, revisions,
requested/observed states and current commands. It excludes timestamps,
presence and computed `can_start`/`control_reason`/reason labels.

Inside one serialized transaction, the current owner/device grant, profile
bindings, versions and permissions must remain valid, even before replaying a
cached response. A fresh operation requires exact host/catalog versions and
fence, the same complete active key set and unchanged profiles/versions/modes.
Every old hash must change. Every instance must have a real complete human
Stop as its current command, with matching generation, instance version and
old launch hash, plus `desired_state=stopped`, `observed_state=stopped` and
confirmed cleanup. A clean observation after a rejected/completed Start is
insufficient. A completed Start→Stop cycle changes the fence.

Only launch hashes change. IDs, profiles, modes, generations and command
history remain intact; the catalogue and instance revisions advance. The new
observation is `unknown`, with cleanup false. The bounded closed response is
`{host_id,catalog_version,instances}`, as for bootstrap publication. Final
checks after the audit and receipt INSERT verify the exact host, instances,
Stop commands, audit and canonical receipt; a contradiction rolls back.

Replay preserves the first request/CAS and returns historical delivery
evidence. It is never current authority: a later human Start may supersede
the returned snapshot. Reusing the key with a changed body is refused; using
a new key with old versions does not reset the fence. The client validates
the closed receipt and makes no automatic retry or registration.

This patch provides no local catalogue transition, HostJournal migration,
pending-abandonment resolution, worker, feed, Runtime, budget, grant or service
activation. A later reviewed coordinator must preserve private configuration,
and verify that the new hashes really derive from the exact protected release:
Board cannot inspect or certify a private launch configuration from a hash.
The coordinator must preserve
identities, feeds and budgets, block on incomplete recovery, persist its intent
before RPC, obtain a fresh post-response observation and atomically commit
only the local catalogue/hash binding. Installation and human Stop settlement
remain separate prerequisites. Bootstrap `ServerController.publish(CAS0)` is
not an updater.
