# Dashboard Design — The Contract

The rulebook for anything that lands in `grafana/dashboards/`. Agents and
humans alike. A dashboard that breaks this contract is a defect, not a
style preference.

## The two audiences

- **Humans (operators)**: read the home dashboard, drill into one host or
  one subsystem, get an answer in under a minute.
- **Agents**: read the metric catalog (`docs/metrics.yml`), build PromQL,
  and validate against the recording rules. Agents rarely open the UI —
  the dashboards are the *reference implementation* of the questions the
  fleet must be able to answer.

Every panel is therefore a **promoted question**: a question an operator
actually asks, written once in PromQL, rendered in a fixed layout, and
stable enough that an agent can predict what it shows.

## The question first

Before adding any panel, write the question it answers, as the panel
title or a one-line description. If you cannot state the question in one
line, the panel does not exist yet.

Good questions: "Which host is most stressed right now?", "When was my
last backup, and for how long has it not been verified?", "Is the 35B
model keeping up?" Bad: "CPU metrics", "Network", "Overview".

## The fleet

Today a Prometheus instance scrapes: one or more node_exporter hosts
(hosts), the central pve-exporter (all PVE nodes), the central
pbs-exporter (all PBS datastores), and the LLM endpoints (hub, vLLM,
llama.cpp routers). More node_exporter hosts arrive over time.

**Consequence for panels:** anything keyed on `instance` must scale to
N hosts without hand-editing. Per-host panels use a template variable
driven by `label_values(node_cpu_seconds_total, instance)` — never a
hardcoded IP. A fleet table must stay readable at 10 and 50 rows:
sort by severity, cap series, one row per host.

## Recording rules first

Dashboards prefer the `homelab:*` recording rules
(`prometheus/rules/homelab.rules.yml`) over raw metrics: rules are where
unit normalization, zero/absent semantics, and engine-specific `or`
unions live. A panel that recomputes a rule's expression raw is a bug
unless the rule does not exist yet (then file the gap, don't fork the
logic). See `docs/metrics.yml` for the catalog.

## Units and honesty

- Every panel sets a Grafana unit (`percent`, `bytes`, `Bps`, `short`,
  `s`, `h`, ...). A dimensionless number in a fleet dashboard is a bug.
- **Zero is a measured zero. Absent is unknown.** A panel must show "no
  data" (empty, not a flat 0 line) when the source is absent. Do not
  fabricate zeros with `or vector(0)`.
- Percentages are 0–100 (`ratio * 100`) or 0–1 with a `percentunit`
  unit — pick one per dashboard and stay consistent.

## Layout

- Grid is 24 columns. Stat rows: 8 columns × 3. Time series: 12 wide
  (2 per row) or 24 wide for multi-series comparisons.
- Panel height: stats 4, time series 8, tables up to 12.
- Sections are **rows**, in reading order — answer the cheap question
  before the expensive one: up/down first, capacity second, trends
  third.
- A dashboard is at most ~10 visible panels. More means it is doing two
  dashboards' jobs — split it.
- Dashboards in `grafana/dashboards/` are **versioned files** imported
  by the `grafana` Ansible role via provisioning. Edit the JSON in git;
  never hand-edit in the UI (UI edits get clobbered on the next
  deploy).

## The dashboards

| File | Answers |
|------|---------|
| `homelab-overview` | Is anything wrong right now? (home dashboard) |
| `host-detail` | What is wrong with *this* host? |
| `backups` | Are we protected? When, where, verified? |
| `proxmox` | What is the hypervisor fleet doing? |
| `storage-zfs` | How full / how healthy is storage? |
| `gpu-llm` | Are the inference models keeping up? |
| `prometheus-self` | Is monitoring itself trustworthy? |

Each file carries `--homelab-dashboard: <name>` in its description for
idempotent import. `scripts/verify-monitoring.py` validates every file
and its PromQL against the live Prometheus, then checks that the expected
UIDs exist in the Grafana store after provisioning — it never writes to
Grafana; deployment (copy + provision) stays with Ansible.

## Verification

A dashboard change is not done until:

1. Every target in the file that is **not activity-gated** returns
   actual series for the current fleet (a query returning empty is a
   defect, not "no data yet") — verified against the live Prometheus,
   not by reading the JSON. **Activity-gated metrics** (LLM
   throughput/latency/preemptions, ZFS ARC hit — see the zero/absent
   column in `docs/metrics.yml` and `TRANSIENT_RULES` in
   `scripts/verify-monitoring.py`) may legitimately return no series
   while the system is idle; the verifier reports those as info, not
   failure.
2. Units are set on every numeric panel.
3. After Ansible provisioning, the expected UID exists in the Grafana
   store and all panel expressions validate (the verifier never imports
   anything — deployment is Ansible's job).
4. Committed in the public repo; the private repo's monitoring service
   doc points at it.

Community dashboards are **source material** for panel ideas, never
imports. Anything borrowed gets rewritten to our metric names, labels,
and this contract.
