# Retention

## Phase 1 policy

- **Time:** `90d`
- **Size:** `16GB` — with a 20 GiB quota on the TSDB dataset, that is
  ~80%: compaction temporarily needs headroom, so the size limit stays
  at ~80-85% of the dataset.
- Both limits are set in the **config file** (`storage.tsdb.retention`);
  the CLI retention flags are deprecated. Whichever limit is hit first
  wins.

```yaml
storage:
  tsdb:
    path: /var/lib/prometheus
    retention:
      time: 90d
      size: 16GB
```

## Measure, then adjust

After ~7 days of running, answer "is 20 GiB sane?" from the system's
own metrics (see [agent-queries.md](agent-queries.md), Prometheus self):

```promql
prometheus_tsdb_storage_blocks_bytes
  + prometheus_tsdb_wal_storage_size_bytes
  + prometheus_tsdb_head_chunks_storage_size_bytes
prometheus_tsdb_head_series
rate(prometheus_tsdb_head_samples_appended_total[1h])
```

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
