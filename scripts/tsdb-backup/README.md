# TSDB backup

`tsdb-backup.sh` snapshots the live Prometheus TSDB and copies the snapshot
to a backup destination (typically an NAS dataset over NFS), **verifies the
destination copy, then deletes the local snapshot**, then prunes old copies.

## Guarantee

The local snapshot is deleted **only after** the destination copy passes
verification:

- every top-level block dir in the copy contains `meta.json`, `index`, and
  `chunks/` (the Prometheus 3.x snapshot layout — a static copy of the TSDB,
  one block dir per 2-hour window; 2.x used `index/`+`chunks/` directly),
- the file count of the copy equals the file count of the source,
- the copy's byte size is ≥ 90% of the source's (catches truncated writes
  that keep the file count).

Any failure (mount missing, not writable, rsync error, verification
failure) removes the partial/failed destination copy, **keeps the local
snapshot for the next attempt**, and exits non-zero (visible in the
journal / `systemctl list-timers` / the failed unit). A failed run never
deletes local data, and it never leaves a corrupted copy in DEST that a
future restore could pick up.

## Retry model

One attempt per day, no retry loop. The destination is power-managed
(availability window around the timer); a missed window fails loudly
rather than hammering a down NAS. `Persistent=true` covers the *system*
being down at 23:30 (the timer fires a catch-up run once the system is
back, within the window). A *service* failure (NAS down while the host
is up) is **not** auto-retried — the next run is the next day's 23:30,
which is fine because a failed run keeps its local snapshot: every local
snapshot older than 2 days is pruned, so at most two days of history can
be affected by a missed window. If a window is missed, rerun manually:
`systemctl start tsdb-backup.service`.

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
| `tsdb-backup.sh` | snapshot request → rsync → verify → delete local → prune |
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
