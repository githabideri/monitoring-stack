# Host onboarding

Adding a new host to the monitoring stack is three steps: inventory
entry, exporter deploy, scrape target. Everything else (dashboards,
recording rules) picks the host up automatically because it queries by
label, not by hardcoded instance list.

## 1. Add to the site inventory

```ini
[node_exporter_servers]
newhost.example.lan ansible_host=10.0.0.X ansible_user=...
```

Give it any site labels you need (e.g. `site: vienna` via
`prometheus_external_labels` or per-job labels).

## 2. Deploy node_exporter

```bash
ansible-playbook -i <site-inventory> monitoring.yml --limit newhost.example.lan
```

Per-host options (in the site inventory):

- `node_exporter_extra_collectors: [zfs]` — enable the zfs collector on
  ZFS hosts (reads `/proc/spl/kstat/zfs`)
- `node_exporter_rootfs: /host` — only when the exporter runs in a
  container and should see host filesystem metrics
- `node_exporter_disable_collectors: [...]` — trim noisy collectors

## 3. Add the scrape target

In the Prometheus vars (site inventory), extend the `node-exporter`
job's `targets` (or add a new job with an appropriate `role` label):

```yaml
- job: node-exporter
  targets: ["10.0.0.X:9100"]
  labels: { role: host }
```

Re-run the prometheus role. Verify:

```bash
curl -s localhost:9090/api/v1/targets | jq '.data.activeTargets[] | select(.labels.instance=="10.0.0.X:9100") | {health, lastError}'
curl -s "localhost:9090/api/v1/query?query=up{job=\"node-exporter\",instance=\"10.0.0.X:9100\"}"
```

## Other exporters

- **PVE node** — add an entry to `pve_exporter_servers` in the
  Prometheus LXC's vars (read-only token per node). No per-host
  installation; the central exporter picks it up.
- **LLM endpoint** — add a job targeting `<host:port>/metrics`
  (vLLM, llama.cpp, or the hub). The hub needs
  `scheme: https` + `tls_insecure_skip_verify: true` for self-signed
  certs.
- **SBC / other service** — same pattern: exporter on the device, job
  in the scrape config, `role` label set.

## Verification

After onboarding, confirm (in this order):

1. exporter answers: `curl -s <host>:9100/metrics | head`
2. target is healthy in Prometheus (`/api/v1/targets`)
3. `up{instance="..."}` = 1 and recording rules evaluate
   (`/api/v1/query?query=homelab:fs_used:ratio{instance="..."}`)
4. host appears in Grafana Overview/Host Detail
