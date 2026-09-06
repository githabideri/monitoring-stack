# Recovery

What is what:

| Asset | Disposable? | Recovery path |
|---|---|---|
| Prometheus/Grafana LXCs (rootfs, config) | no (but cheap to rebuild) | PBS restore; or re-run the ansible roles from this repo (config is in git) |
| Prometheus TSDB (ZFS dataset) | **yes** (worst case: lose local history) | nightly NAS snapshots; rebuild dataset and copy latest snapshot back |
| Grafana state (rootfs) | no, low value | PBS restore |
| Exporter binaries | trivially rebuilt | roles re-install |

## TSDB recovery (loss of the local dataset)

1. Recreate the dataset and mountpoint:
   ```bash
   zfs create -o quota=20G <pool>/monitoring-prometheus
   # re-add the CT mountpoint / mp entry if it was recreated
   ```
2. From the latest NAS snapshot copy (`DEST/snapshots/<newest>`):
   ```bash
   rsync -a <nas-mount>/snapshots/<newest>/ /var/lib/prometheus/
   ```
3. Restart the Prometheus service; it opens the copied blocks.
   Expect a compaction burst on first start.
4. Verify: `prometheus_tsdb_storage_size_bytes`, `/api/v1/status/config`,
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
  the destination; pruning happens in the destination).
- If the NAS is down at the scheduled window, the run fails in the
  journal; the next window retries, and the snapshot taken at that time
  simply becomes the newest backup. History older than that remains in
  previously copied snapshots.

## What this stack does NOT recover

- Host OS-level damage on the Proxmox host itself (use the PVE host's
  own backup story).
- Other guests' data (PBS namespaces — unrelated to this stack).
