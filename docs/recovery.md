# Recovery

What is what:

| Asset | Disposable? | Recovery path |
|---|---|---|
| Prometheus/Grafana LXCs (rootfs, config) | no (but cheap to rebuild) | PBS restore; or re-run the ansible roles from this repo (config is in git) |
| Prometheus TSDB (ZFS dataset) | **yes** (worst case: lose local history) | nightly NAS snapshots; rebuild dataset and copy latest snapshot back |
| Grafana state (rootfs) | no, low value | PBS restore |
| Exporter binaries | trivially rebuilt | roles re-install |

## TSDB recovery (loss of the local dataset)

The nightly snapshot is a **static copy of the TSDB** and can be used as
a data directory directly. On Prometheus 3.x a snapshot dir contains one
or more block dirs (`<name>/<block-ULID>/{meta.json,index,chunks/}`) —
no separate replay step is needed: start Prometheus with
`--storage.tsdb.path` pointing at the snapshot dir. This procedure was
verified on 3.14 (a scratch instance booted on a copied snapshot served
the same values as the live instance for the covered period).

1. Recreate the dataset and mountpoint:
   ```bash
   zfs create -o quota=20G <pool>/monitoring-prometheus
   # re-add the CT mountpoint / mp entry if it was recreated
   ```
2. From the latest NAS snapshot copy (`DEST/snapshots/<newest>`):
   ```bash
   rsync -a <nas-mount>/snapshots/<newest>/ /var/lib/prometheus/
   ```
   (equivalently, for a one-off test: run a scratch
   `prometheus --storage.tsdb.path=<the-snapshot-dir>` alongside; do
   **not** point the production unit at a directory the backup script
   still prunes.)
3. Start the Prometheus service; it opens the copied blocks.
   Expect a compaction burst on first start.
4. Verify: `prometheus_tsdb_storage_blocks_bytes` (3.x — see
   [retention.md](retention.md) for the full sum), `/api/v1/status/config`,
   and a sample recording-rule query.

The gap in history = time since the last successful nightly copy.

## LXC loss (crash, failed upgrade, bad config)

1. `pct` snapshot before risky changes is the standing rule (PVE
   snapshots are cheap and reversible).
2. Rollback: stop → `pct rollback` → start.
3. Or: PBS restore of the latest LXC backup, then re-apply the
   Prometheus config (it is fully in git: re-run the role).
4. If only the *config* broke: `git` — the deployed prometheus.yml is
   a template render of committed vars; restore from git and re-run.

## Backup pipeline failure

- A failed TSDB copy leaves the live TSDB untouched (copies happen to
  the destination; the local snapshot is deleted only after the copy
  verifies; failed copies are removed).
- If the NAS is down at the scheduled window, the run fails in the
  journal and is **not** auto-retried (one attempt per day — see the
  backup README's retry model). The failed run's local snapshot survives
  until the next run; rerun manually inside the window (`systemctl start
  tsdb-backup.service`) if you want the backup sooner. The *system*
  being down at 23:30 is caught up automatically via `Persistent=true`.

## What this stack does NOT recover

- Host OS-level damage on the Proxmox host itself (use the PVE host's
  own backup story).
- Other guests' data (PBS namespaces — unrelated to this stack).
