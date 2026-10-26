# Protected public content pins

Adapter 0.35 reduces repeated hashing in the Linux parent controller's bounded
authorization loop. A qualification attempt with adapter 0.34 completed its
first three cases, then expired before SOURCE in `wall_timeout`. A synthetic
timing reproduction confirms that local content checks and preparation writes
can consume the previous 1.5-second window even with one Board request. This
does not establish that hashing was the only cause of the live refusal.

The optimization stores content facts for root-protected public files only.
Admission hashes the entire file through one held descriptor. Reuse checks
that descriptor, its named leaf and root-owned, non-group/other-writable
ancestors, including the leaf's device, inode, mode, owner, group, link count,
size and nanosecond modification/change times. Symlinks, hardlinked leaves,
set-ID files and writable public paths do not qualify.
The file's filesystem must also remain in the closed kernel-local allowlist
(ext4, XFS, Btrfs, tmpfs or overlay). Unknown, network, FUSE, procfs and sysfs
files keep the complete-read path. Root and local storage remain trusted;
metadata observations cannot protect against a hostile host administrator.

The controller must be non-root with matching real/effective/saved/filesystem
IDs, no new privileges, no active/permitted/inheritable/ambient capabilities,
and the full identity mapping of the initial user namespace. A nonzero
capability bounding set alone does not grant authority under these conditions.
These observations are rechecked on reuse; an observed drift refuses the epoch
instead of adopting a replacement. An initially ineligible controller or file
uses the original complete-read path.

The cache is process-local, bounded and shared between parent threads. Cold
streaming hashes do not hold the global metadata lock or block unrelated hot
lookups. Descriptors are close-on-exec and closed on fork and normal exit. The
fresh Linux supervisor disables and closes the cache before loading its job,
so its descriptor limit and original complete-read behavior are retained.

Only executable digests and public runtime pin checks use this path. Byte reads
used to assemble the namespace remain complete reads. SDK source closure,
private configuration, selected connections, verifier/input/script files,
repository contents, journals, claims, Stop and Board authority are not cached.
The native 1.5-second deadline, SOURCE challenge and cleanup protocol are
unchanged. This cache grants no execution or file authority.

Board 2.17.3/schema 17 and its 76 MCP tools are unchanged. Packaging requires the
new module for adapter 0.35 and later while retaining verification of receipted
0.34 bundles during a stopped upgrade. Installation neither starts the unit nor
replays a failed batch. A new explicit batch and exact-source qualification in
the final owner unit are still necessary; synthetic tests do not prove real
model execution, publication or independent review.
