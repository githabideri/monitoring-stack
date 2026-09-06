# TSDB backup

`tsdb-backup.sh` snapshots the live Prometheus TSDB and copies the snapshot
to a backup destination (typically an NAS dataset over NFS), then prunes
old copies.

## Why this shape

- The **live TSDB stays on local ZFS** — never on NFS (write-heavy,
  latency-sensitive). NFS is fine as a *backup transport*.
- The script runs **on the host** (or anywhere the TSDB dataset is
  mounted), because unprivileged LXCs cannot mount NFS. The host sees the
  same ZFS dataset the LXC uses.
- The destination can be intentionally powered down most of the day:
  schedule the timer inside the availability window, and let a failed
  run fail loudly in the journal (the local TSDB is never touched by a
  failed backup).
- A failed transfer keeps the local snapshot until the next successful
  run, since pruning happens in DEST, not in the live TSDB.

## Files

| File | Purpose |
|---|---|
| `tsdb-backup.sh` | snapshot request → rsync → prune |
| `systemd/tsdb-backup.service` | oneshot unit (site-adjusted env) |
| `systemd/tsdb-backup.timer` | daily, inside the backup window |

## Deploy (shape)

1. Put the script at `/usr/local/bin/tsdb-backup.sh` on the host.
2. Add the env paths in the service unit (TSDB_DIR, DEST, KEEP) matching
   your layout; ensure DEST is a real mountpoint (fstab entry).
3. `systemctl enable --now tsdb-backup.timer`.

## Retention

`KEEP` counts snapshots in DEST (default 14 nightly). Combine with
NAS-side dataset quota/retention. For longer history, future option:
periodic `zfs send/receive` of the TSDB dataset instead of/plus this.
