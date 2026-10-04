#!/usr/bin/env python3
"""vllm-health-probe — engine-liveness probe for a vLLM endpoint.

Why a bridge: vLLM's /health returns JSON ({"status":"ok"} / 503 when the
engine core is dead while the API process still answers — the classic blind
spot). Prometheus cannot scrape a JSON endpoint (unsupported Content-Type),
so a scrape job pointed at /health sits "down" permanently with no signal.
This tiny server sits on the scraping side, GETs the health endpoint on
every /metrics request, and exposes the outcome as text:

    vllm_engine_health                 1|0  (1 iff /health -> HTTP 200;
                                             0 on 503, any other status,
                                             timeout or connection refused)
    vllm_health_probe_seconds          <t>  probe duration
    vllm_health_response_info{code}    1    last observed HTTP status
                                            (code="error" on no response)

The target's own `up` is this bridge's liveness, NOT the engine's — read
vllm_engine_health for the engine signal. HTTP 200 is always returned from
/metrics (the scrape succeeds; the state is in the gauges), so a broken
engine never masquerades as "up" at the probe level either: the gauge goes
to 0 on the very next scrape.

Env (defaults in parens): VLLM_HEALTH_URL (http://127.0.0.1:8080/health),
VLLM_HEALTH_PROBE_LISTEN (127.0.0.1:10021), VLLM_HEALTH_PROBE_TIMEOUT
(10s), VLLM_HEALTH_PROBE_ENV_FILE (/etc/vllm-health-probe/probe.env — if
present, keys there override the environment). Stdlib only.
"""

import http.server
import json
import os
import time
import urllib.error
import urllib.request

ENV_FILE = os.environ.get("VLLM_HEALTH_PROBE_ENV_FILE", "/etc/vllm-health-probe/probe.env")
LISTEN = os.environ.get("VLLM_HEALTH_PROBE_LISTEN", "127.0.0.1:10021")


def load_env():
    env = {}
    try:
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def probe():
    """Return (up, seconds, code). up=1 iff the health endpoint -> HTTP 200."""
    cfg = load_env()
    url = cfg.get("VLLM_HEALTH_URL") or os.environ.get("VLLM_HEALTH_URL") \
        or "http://127.0.0.1:8080/health"
    try:
        timeout = float((cfg.get("VLLM_HEALTH_PROBE_TIMEOUT")
                         or os.environ.get("VLLM_HEALTH_PROBE_TIMEOUT") or "10s").rstrip("s"))
    except ValueError:
        timeout = 10.0
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read(256)  # drain a little; the status is the signal
            code = str(r.status)
            up = 1 if r.status == 200 else 0
    except urllib.error.HTTPError as e:
        code = str(e.code)
        up = 0
    except Exception:
        code = "error"
        up = 0
    return up, time.time() - t0, code


def render_metrics():
    up, secs, code = probe()
    out = []
    out.append("# HELP vllm_engine_health 1 while the vLLM engine is alive (HTTP 200 from its health endpoint), 0 otherwise (503, other status, timeout, refused).")
    out.append("# TYPE vllm_engine_health gauge")
    out.append("vllm_engine_health %d" % up)
    out.append("# HELP vllm_health_probe_seconds duration of the last health probe")
    out.append("# TYPE vllm_health_probe_seconds gauge")
    out.append("vllm_health_probe_seconds %.4f" % secs)
    out.append("# HELP vllm_health_response_info last observed HTTP status of the health endpoint (code=\"error\" when it did not answer)")
    out.append("# TYPE vllm_health_response_info gauge")
    out.append('vllm_health_response_info{code="%s"} 1' % code)
    return "\n".join(out) + "\n"


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render_metrics().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # quiet: one line per scrape, every 30s, forever


if __name__ == "__main__":
    http.server.ThreadingHTTPServer((LISTEN.rsplit(":", 1)[0], int(LISTEN.rsplit(":", 1)[1])),
                                    Handler).serve_forever()
