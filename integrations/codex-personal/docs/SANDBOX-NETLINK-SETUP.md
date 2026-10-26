# Bubblewrap network-namespace setup

Codex 0.163 builds its restricted Linux sandbox with `--unshare-net`.
The bundled bubblewrap helper configures the new namespace's loopback interface
using a `PF_NETLINK` / `NETLINK_ROUTE` socket. The outer pool unit previously
allowed only `AF_UNIX`, `AF_INET` and `AF_INET6`, so its inherited socket filter
could reject this setup before the synthetic Python canary produced JSON.

Adapter 0.37 adds `AF_NETLINK` to that outer unit list. This gives the controller
and sandbox setup process access to that family; it does not grant host
`CAP_NET_ADMIN`. The existing empty capability bounding set, no-new-privileges,
filesystem protections, budgets and three previously permitted families remain.
After setup, Codex's restricted inner seccomp policy rejects non-Unix sockets
and network operations. The separate native test backend continues to deny
socket syscalls. The original five planner checks still decide qualification;
there is no fallback to unsandboxed execution.

The stopped upgrade path accepts this difference only for a source 0.35.0 or
0.36.0 release using the shipped template and a destination 0.37.0. It compares
both exact template byte sequences. Any additional edit, a modified old
template or a different version transition remains refused. The previous
receipted unit is verified and saved before replacement, private state is
retained, and installation never starts the unit.

This corrects a setup dependency. A synthetic comparison is separate from a
fresh owner workflow acceptance: failed historical batches stay preserved and
must not be replayed.

Public version-specific sources:
- [restricted-network mapping](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/linux-sandbox/src/linux_run_main.rs)
- [bubblewrap arguments](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/linux-sandbox/src/bwrap.rs)
- [loopback setup](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/vendor/bubblewrap/network.c)
- [inner network filter](https://github.com/openai/codex/blob/rust-v0.163.0/codex-rs/linux-sandbox/src/seccomp.rs)
