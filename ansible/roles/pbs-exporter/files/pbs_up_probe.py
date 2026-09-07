#!/usr/bin/env python3
"""pbs-up-probe — liveness probe for the Proxmox Backup Server.

The natrontech/pbs-exporter's `pbs_up` is a false negative: its liveness
probe hits /api2/json/nodes, which a read-only DatastoreAudit token is not
permitted on in PBS 3.x (403 since day one). This probe hits an endpoint
the token *is* permitted on (/api2/json/version) and exposes the result:

    pbs_api_up                 1|0   (HTTP 200 from /api2/json/version)
    pbs_probe_seconds          <t>   probe duration
    pbs_version_info{version}  1     version the daemon reports

Credentials: reads the same /etc/pbs-exporter/pbs-exporter.env file as the
pbs-exporter (PBS_USERNAME, PBS_API_TOKEN, PBS_API_TOKEN_NAME, PBS_ENDPOINT,
PBS_INSECURE, PBS_TIMEOUT). Probe runs on demand (per /metrics request),
like the exporter. Stdlib only.

The real PBS health signal for dashboards remains data freshness
(pbs_snapshot_vm_last_timestamp) + datastore usage; pbs_api_up is the
fast liveness bit (e.g. "is the daemon up and accepting API traffic").
"""

import http.server
import json
import os
import ssl
import time
import urllib.request

ENV_FILE = os.environ.get("PBS_PROBE_ENV_FILE", "/etc/pbs-exporter/pbs-exporter.env")
LISTEN = os.environ.get("PBS_PROBE_LISTEN", "0.0.0.0:10020")


def load_env():
    env = {}
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def probe():
    """Return (up, seconds, version_str). up=1 iff /api2/json/version -> 200."""
    env = load_env()
    endpoint = env.get("PBS_ENDPOINT", "").rstrip("/")
    user = env.get("PBS_USERNAME", "")
    name = env.get("PBS_API_TOKEN_NAME", "")
    secret = env.get("PBS_API_TOKEN", "")
    insecure = env.get("PBS_INSECURE", "false").lower() in ("1", "true", "yes")
    timeout = float(env.get("PBS_TIMEOUT", "10s").rstrip("s"))

    if not (endpoint and user and secret):
        return 0, 0.0, ""

    ctx = ssl._create_unverified_context() if insecure else None
    req = urllib.request.Request(
        endpoint + "/api2/json/version",
        headers={"Authorization": f"PBSAPIToken {user}!{name}:{secret}"},
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            if r.status == 200:
                body = json.loads(r.read().decode())
                return 1, time.time() - t0, str(body.get("data", {}).get("version", ""))
        return 0, time.time() - t0, ""
    except Exception:
        return 0, time.time() - t0, ""


def render():
    up, secs, version = probe()
    lines = [
        "# HELP pbs_api_up 1 if the PBS API answered /api2/json/version with 200 (unlike pbs_up, which probes /nodes and 403s for read-only tokens).",
        "# TYPE pbs_api_up gauge",
        f"pbs_api_up {up}",
        "# HELP pbs_probe_seconds PBS API probe duration.",
        "# TYPE pbs_probe_seconds gauge",
        f"pbs_probe_seconds {secs:.4f}",
    ]
    if version:
        lines += [
            "# HELP pbs_version_info 1 if the probe saw this PBS version.",
            "# TYPE pbs_version_info gauge",
            f'pbs_version_info{{version="{version}"}} 1',
        ]
    return "\n".join(lines) + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/metrics", "/"):
            body = render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    host, port = LISTEN.rsplit(":", 1)
    http.server.ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
