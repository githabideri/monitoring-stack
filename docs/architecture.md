# Architecture

## Shape

```
exporters
  node_exporter      on each host (local kernel/device metrics)
  pve-exporter       central, in the Prometheus LXC (PVE API, read-only)
  pbs-exporter       central, in the Prometheus LXC (PBS API, read-only)
  LLM endpoints      hub /metrics, vLLM /metrics, llama.cpp /metrics
        |
        v
Prometheus LXC  <--- local administration agents (read-only PromQL)
  TSDB on a local ZFS dataset (quota-capped)
  nightly snapshot -> NAS (TrueNAS) over NFS
        |
        v
Grafana LXC  <--- humans
```

**Design rule: exporters that need local kernel/device access run on the
target host; API-based collectors run centrally.** This keeps hypervisors
clean and credentials in one place.

## The two consumers

1. **Humans** — Grafana dashboards (overview / host detail / storage /
   GPU-LLM). Optimized for visual exploration.
2. **Local administration agents** — Prometheus HTTP API and the
   `homelab:` recording rules. Optimized for predictable, low-token,
   repeatable queries (see [agent-queries.md](agent-queries.md)).

Agents must **not** scrape Grafana HTML, OCR dashboards, or parse panel
layouts. Anything an automation needs must be available through
Prometheus.

## Storage

| Data | Where | Backup |
|---|---|---|
| Prometheus LXC rootfs | LXC root disk (ZFS zvol/subvol) | normal host backup (PBS) |
| Prometheus TSDB | **dedicated local ZFS dataset**, mounted into the LXC | **excluded from normal backup**; nightly TSDB snapshot → NAS |
| Grafana state | LXC rootfs (or optional ZFS dataset) | normal host backup (PBS) |
| Exporter state | none (stateless binaries) | — |

- The **live TSDB never lives on NFS/SMB** (write-heavy, latency
  sensitive). NFS is acceptable as a *backup transport/destination*.
- The TSDB dataset is deliberately outside the LXC's backed-up root
  volume: high-churn monitoring data does not belong in PBS.
- What is disposable: the whole TSDB (worst case you lose ~90 days of
  history). What is reconstructable from git: all config (this repo).
  What is backed up: LXCs via PBS; TSDB via nightly snapshot to the NAS.
  See [recovery.md](recovery.md).

## Security

- Prometheus, Grafana, and all exporters bind to **internal interfaces
  only** and are reachable from the trusted LAN/Tailnet. Nothing is
  exposed to the public internet.
- **Prometheus has no per-user API auth** in phase 1: the trust boundary
  is the network (LAN/TS only) plus the admin API being enabled but
  only reachable from that network. This matches the fleet's existing
  pattern for keyless internal endpoints. Revisit (basic-auth reverse
  proxy or similar) only if access from less-trusted networks becomes a
  requirement.
- Agents query Prometheus **read-only** by convention (queries, no
  admin writes). Actions always go through separately scoped mechanisms:
  PVE API identities with restricted roles, Ansible, SSH service
  accounts, service-specific APIs. Monitoring never becomes an
  execution channel.
- Exporters use **read-only credentials**: PVE `PVEAuditor`-style
  users/tokens per node, a read-only PBS user (e.g. Audit/DatastoreAudit
  on the datastore). Store tokens in gitignored `.env` files or an
  equivalent secret store; never in committed files.

## Autonomy levels (future, not implemented here)

The architecture is deliberately compatible with a progression of agent
autonomy without any of it being built now:

- **L0 observe** — query Prometheus / read config / recommend. *(works today)*
- **L1 supervised changes** — human starts the task; agent snapshots,
  changes, restarts, verifies. *Prometheus serves the verify step.*
- **L2 pre-approved routines** — snapshot → change → verify → rollback
  runs within a defined scope.
- **L3 event-driven** — monitoring conditions (disk pressure, failed
  backup, stale data) trigger bounded agent workflows. *This repo stays
  read-oriented; triggering belongs to whatever system is built for it.*

Future autonomous actions should emit structured audit records
(timestamp, agent identity, target, task, safety point, result,
verification, rollback result). Where those records live (Loki, DB,
ledger) is deliberately undecided.
