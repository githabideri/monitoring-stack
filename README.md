# monitoring-stack

Reproducible monitoring for a Proxmox + ZFS homelab: version-controlled
Grafana dashboards, a stable PromQL layer for local administration agents,
Ansible deployment, TSDB backup/recovery, and a live-verification script
that refuses to call anything "done" without live data behind it. It
covers deployment, storage, backups, host onboarding, and the split
between reusable config and private site data.

The same Prometheus data is also consumed by local administration agents.
Labels and recording rules are kept predictable so agents can inspect system
state and verify changes without relying on Grafana screenshots.

## What's here

```
ansible/               Roles: prometheus, grafana, node-exporter,
                       pve-exporter, pbs-exporter (+ example playbook/inventory)
prometheus/            Reference scrape config (examples/) and the
                       homelab: recording-rule namespace (rules/)
grafana/               Provisioning (datasource + dashboards) and the seven
                       dashboard JSONs (see docs/dashboard-design.md)
scripts/               verify-monitoring.py (post-deploy verification)
                       tsdb-backup/ (TSDB snapshot -> NAS, systemd units)
docs/                  architecture, labeling, agent-queries,
                       dashboard-design (the dashboard contract),
                       metrics (the metric catalog), host-onboarding,
                       retention, recovery
```

## Architecture in one diagram

```
exporters (node / pve / pbs / llm endpoints)
        |
        v
Prometheus LXC  <------ local administration agents (read-only, PromQL)
        |  TSDB on a local ZFS dataset
        |  +-- nightly snapshot -> NAS (TrueNAS)
        v
Grafana LXC     <------ humans (dashboards)
```

- **Prometheus** runs in its own LXC. The TSDB lives on a **local
  ZFS-backed dataset** (never on NFS/SMB for live data). Retention is
  time **and** size capped (default 90 days / 16 GiB, measured and
  adjustable).
- **Grafana** runs in its own LXC with a provisioned Prometheus datasource
  and **seven operational dashboards** — one per question (is anything
  wrong / what's wrong with this host / are we protected / what is the PVE
  fleet doing / is storage healthy / are the inference models keeping up /
  is monitoring itself trustworthy). The org home dashboard is the fleet
  overview, set in `grafana.ini` ([dashboards] section — see the
  dashboard contract in `docs/dashboard-design.md`). Dashboards are plain
  JSON in git; provisioning imports them, the UI is for browsing.
- **The `homelab:` recording-rule namespace is the machine-facing contract**:
  few, stable, unit-normalized names with explicit zero/absent semantics,
  documented in `docs/metrics.yml` (the metric catalog) and
  `docs/agent-queries.md`. Agents query Prometheus directly; they do not
  scrape Grafana.
- **Verification is a first-class artifact**: `scripts/verify-monitoring.py`
  checks targets, rule health, that every dashboard expression returns
  live series, that the dashboards are in the Grafana store, and that the
  home dashboard is actually served — run it after every change; a PASS is
  the bar for calling the stack healthy.
- **pve-exporter** and **pbs-exporter** run *centrally inside the
  Prometheus LXC* and query PVE nodes / the PBS API over the network.
  They need read-only API credentials (e.g. a PVE `PVEAuditor` user per
  node, a read-only PBS user). Node-local exporters (`node_exporter`, GPU
  exporters) run on the target host instead — that split is the whole
  design rule.
- **Backups**: nightly, `promtool`-style TSDB snapshot (admin API
  `POST /api/v1/admin/tsdb/snapshot`) is copied to a NAS dataset over
  NFS and pruned on the NAS side. The live TSDB is excluded from normal
  (PBS) backup on purpose. See `docs/recovery.md`.

## Public / private split

This repo is deliberately **generic**: no real hostnames, IPs, LXC IDs,
dataset names, or credentials. Your private repo supplies the overlay:

- real inventory (hosts, IPs, labels like `site`)
- site playbook / entrypoint that references these roles via `roles_path`
- secrets (API tokens, passwords) in gitignored `.env` files
- dashboard values that reference your topology

The convention used in the homelab repo: this repo lives in
`submodules/monitoring-stack/`, the site inventory and playbook live in
the private repo's `ansible/`, and host-level automation (e.g. the
backup timer on a Proxmox host) follows the private repo's `iac/`
pattern.

## Quick start (shape of a deployment)

1. Create two LXCs (unprivileged) — one for Prometheus (with a ZFS
   mountpoint for the TSDB), one for Grafana.
2. Add both to your inventory and run the example playbook
   (`ansible/examples/playbook.yml`) against them.
3. Create a read-only PVE API user per node and a read-only PBS user;
   put their credentials in your secrets store.
4. Add hosts to the inventory as `node_exporter` targets and re-run.
5. Onboarding a new host is: inventory entry + exporter role + scrape
   target. See `docs/host-onboarding.md`.

## Verification vocabulary

When a change to this stack is made, states are distinguished:

- **implemented** — code/config written, not verified
- **locally verified** — services run, endpoints answer, rules evaluate
- **live verified** — dashboards populate, backup completed, agents query
  successfully

Do not collapse these into "works".

## Non-goals (phase 1)

No Loki, Tempo, OpenTelemetry, Alertmanager, Thanos/Mimir, Kubernetes, or
agent orchestration. This is a read-oriented telemetry system: agents
observe and verify; actions go through separately scoped mechanisms
(PVE API identities, Ansible, service APIs).
