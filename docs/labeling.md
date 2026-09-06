# Labeling conventions

Predictable labels are part of the machine-facing interface: agents form
queries against these names without reverse-engineering per-target
schemes.

## Canonical labels

| Label | Meaning | Example values |
|---|---|---|
| `site` | Physical location / site (applied via `global.external_labels`) | `vienna`, `freistadt`, `vps` |
| `host` | Hostname or stable host identifier (use per-job labels or instance) | `pve1`, `nas1` |
| `role` | What the target is, per scrape job | `self`, `host`, `pve-node`, `pve-cluster`, `pbs`, `llm-hub`, `llm-vllm`, `llm-llamacpp` |
| `service` | Service identity when a job scrapes one service on many hosts | `grafana`, `pihole` |
| `instance` | `<host:port>` — keep as generated; do not hand-edit | |
| `job` | Scrape job name — short, stable, snake-case | `node-exporter`, `pve-exporter-node` |
| `gpu` | GPU identifier (hub sidecar) | |
| `server` | Inference server (hub) | |
| `model` | Model id (hub) | |
| `environment` | Optional; only if you actually run dev/staging alongside prod | |

## Cardinality rules

**Do not add labels containing:**

- request IDs, session IDs, arbitrary URLs, filenames
- dynamic user strings or task-specific free text
- per-connection or per-file series

High-cardinality labels silently blow up the TSDB and break agents'
assumptions about query cost. If a metric has a dimension like that,
aggregate before storing it (exporter-side) or exclude the metric.

## Naming style

- metric names: `snake_case`, unit suffix where meaningful (`_bytes`,
  `_seconds`, `_ratio`, `_total` for counters)
- ratios are 0..1; percentages are `*_pct` or 0..100
- recording rules: `homelab:<concept>[:<aggregation>]`
  (`homelab:fs_used:ratio`, `homelab:backup_age_seconds`,
  `homelab:llm_kv_used:ratio`)
- job names: stable and short; renaming a job is a breaking change for
  agents and dashboards

## PVE exporters

The central pve-exporter is scraped twice (node metrics vs cluster
metrics, via `node=1` / `cluster=1` url params). Node metrics carry the
PVE node name; **cluster metrics come from exactly one node** to avoid
duplicates.
