# Retention

## Phase 1 policy

- **Time:** `90d`
- **Size:** `16GB` — with a 20 GiB quota on the TSDB dataset, that is
  ~80%: compaction temporarily needs headroom, so the size limit stays
  at ~80-85% of the dataset.
- Both limits are set in the **config file** (`storage.tsdb.retention`):
  the old singular `--storage.tsdb.retention` CLI flag was removed in
  3.0; in 3.x `retention.time` / `retention.size` are config fields
  (CLI aliases of the same fields exist but are not used here).
  Whichever limit is hit first wins.
- The data directory is a **CLI flag**, not config: in Prometheus 3.x
  `storage.tsdb.path` is *not* a config-file field (the 3.x parser
  rejects it — "field path not found in type config.plain").
  It is set on the systemd command line: `--storage.tsdb.path=/var/lib/prometheus`.

```yaml
# /etc/prometheus/prometheus.yml — retention only; path lives on the CLI
storage:
  tsdb:
    retention:
      time: 90d
      size: 16GB
```

## Measure, then adjust

After ~7 days of running, answer "is 20 GiB sane?" from the system's
own metrics (see [agent-queries.md](agent-queries.md), Prometheus self):

```promql
# (verified metric names, Prometheus 3.14)
prometheus_tsdb_storage_blocks_bytes        # compacted blocks (0 until the
  + prometheus_tsdb_wal_storage_size_bytes   #  first 2h block compacts)
  + prometheus_tsdb_head_chunks_storage_size_bytes
prometheus_tsdb_head_series
rate(prometheus_tsdb_head_samples_appended_total[1h])
prometheus_tsdb_retention_limit_bytes       # the size cap (16 GiB)
prometheus_tsdb_retention_limit_seconds     # the time cap (90d)
```

While the TSDB is young (no compacted blocks yet) the sum undercounts:
add `prometheus_tsdb_head_chunks` × ~chunk bytes, or simply use the
on-disk dataset size (`du -s`) for the projection.

Projected size ≈ current size ÷ days running × 90, with compaction
discount (~20-30%). If it will fit in 16 GB, keep the quota; otherwise
grow the dataset — ZFS makes this painless (`zfs set quota=...`).

## Retention classes (future, not implemented)

Not per-metric retention in phase 1. Conceptually the fleet splits into:

| Class | Examples | Future class |
|---|---|---|
| host high-resolution | node_exporter raw metrics | shorter (days) |
| GPU/LLM | hub/vLLM/llama.cpp metrics | medium (weeks-months) |
| filesystem/storage trends | `homelab:fs_*` recording rules | longer (months) |
| environmental/energy | Home Assistant temperature/energy | years |
| backup history | `homelab:backup_age_seconds`, PBS metrics | long |
| transient process metrics | per-process CPU/mem | short (hours-days) |

The clean mechanism for this is **`remote_write`** to a long-term
backend (none selected yet); keep it in mind when choosing label
schemes so future backends can route by job/metric family. Do not
deploy long-term storage in phase 1.
