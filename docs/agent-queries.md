# Agent queries

High-value PromQL for local administration agents. Keep the set small;
each query answers a health question an agent actually asks. The
`homelab:` recording rules do the heavy lifting — query the recording
rule, not the raw multi-term expression.

All queries assume the Prometheus HTTP API:
`GET /api/v1/query?query=<expr>` (instant) and
`/api/v1/query_range?query=<expr>&start=...&end=...&step=...`.

## Hosts

```promql
# Hosts that are up (count, and list)
count(up{job="node-exporter"} == 1)
up{job="node-exporter"} == 0                 # what is down

# CPU pressure per host (0..100)
homelab:cpu_util:avg5m

# Memory pressure per host (0..1)
homelab:mem_used:ratio

# Swap pressure (0..1)
homelab:swap_used:ratio

# Load per core (1.0 = saturated)
homelab:load_per_core
```

## Storage

```promql
# Filesystem pressure per mount (0..1)
homelab:fs_used:ratio

# Free bytes per mount
homelab:fs_free:bytes

# ZFS ARC efficiency (0..1; sustained < 0.8 is a smell)
homelab:zfs_arc_hit:ratio

# Growth of the most full filesystems over 7 days (delta of used ratio)
homelab:fs_used:ratio - homelab:fs_used:ratio offset 7d

# PBS datastore fill (0..1)
homelab:pbs_datastore_used:ratio
```

## Backups

```promql
# Age of last backup per guest (seconds)
homelab:backup_age_seconds

# Stale backups (> 2 days)
homelab:backup_age_seconds > 2 * 86400

# Backups whose last snapshot is unverified
homelab:backup_verified == 0
```

## GPU / LLM

```promql
# GPU utilisation (0..100)
homelab:gpu_utilization

# KV-cache usage per model (0..1)
homelab:llm_kv_used:ratio

# Queue pressure
homelab:llm_queue:sum

# Generation / prompt throughput (tok/s)
homelab:llm_tokens_per_second
hub_model_prompt_tokens_per_second

# Latency p95 (seconds)
hub_model_ttft_p95_seconds
hub_model_tpot_p95_seconds

# Preemptions in the last hour
increase(hub_model_preemptions_window[1h])
```

## Before / after service-change comparison

The verify stage of the agent loop: capture a baseline *before* the
change, then compare *after* with a range query over the change window.

```promql
# Baseline (instant, pre-change)
homelab:mem_used:ratio{instance="target-host"}
homelab:fs_used:ratio{instance="target-host"}
up{job="llm-hub"}

# After the change: same queries over the change window, e.g.:
#   /api/v1/query_range?query=up{job="llm-hub"}&start=<t0>&end=<now>&step=30s
# A service that was up before must be up after, with comparable resource
# usage; otherwise keep the change only if the regression is explained,
# or roll back (your PVE snapshot makes that cheap).
```

## Prometheus self

```promql
# Storage size / series count (the "is 20 GiB sane?" answers)
prometheus_tsdb_storage_size_bytes
prometheus_tsdb_wal_segment_files
count({__name__=~"."})                          # active series
rate(prometheus_tsdb_samples_appended_total[1h])  # ingest rate

# Scrape health
up
prometheus_target_scrape_pool_targets
```

## Conventions for agent tooling

- Prefer the recording rules; they are stable and pre-averaged.
- Filter by `instance` / `server` / `model` labels — see
  [labeling.md](labeling.md).
- `NaN` in a hub rate metric means "no data / idle", not an error.
- Rate metrics re-baseline on counter resets; do not compute deltas
  across restarts yourself.
