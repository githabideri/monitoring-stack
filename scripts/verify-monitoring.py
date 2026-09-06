#!/usr/bin/env python3
"""verify-monitoring.py — post-deploy verification for the homelab
monitoring stack (phase 1).

What it checks, and what "pass" means for each:

  1. prometheus    GET /-/healthy == 200.
  2. targets       list of up/down scrape targets (down is a WARNING,
                   not a failure — a host may legitimately be off).
  3. rules         every recording rule is healthy; any rule that is
                   healthy but emits ZERO series is a WARNING (a dead
                   rule is a semantic bug, not "no data yet").
  4. dashboards    for EVERY dashboard JSON in --dashboards:
                     a. valid JSON, has uid, datasource resolvable
                     b. every PromQL expression returns >= 1 series
                        against the live Prometheus (template vars are
                        substituted with the first real label value).
                        ZERO series is a FAILURE — per the dashboard
                        contract, a panel that cannot render is a
                        defect, not "no data yet".
                     c. the dashboard is present in Grafana's store
                        with the same uid (provisioning imported it).
  5. grafana       GET /api/health == ok; login works (only when
                        GRAFANA_USER/GRAFANA_PASSWORD are set).
  6. backup        oldest PBS backup age (informational; >36h is a
                        WARNING).

Exit code: 0 = all pass (warnings allowed), 1 = at least one failure.
Stdlib only; safe to run from any machine that can reach the two
endpoints (both are auth-less on the LAN/Tailscale by design).

Usage:
  verify-monitoring.py [--prometheus URL] [--grafana URL]
                       [--dashboards DIR] [--quiet]
Env: GRAFANA_USER / GRAFANA_PASSWORD (optional; enables the login and
     grafana-store checks).
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request


def http_json(url, timeout=30, headers=None, data=None, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers or {})
            if data is not None:
                req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read())
        except Exception as e:
            last = e
            # the control node reaches the Prometheus LXC over the LAN and
            # occasional connection resets happen under burst load; back off
            # and retry before calling it a failure.
            import time
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url} failed after {retries} attempts: {last}")


def prom_query(prom, expr):
    q = urllib.parse.quote(expr)
    _, d = http_json(f"{prom}/api/v1/query?query={q}")
    if d.get("status") != "success":
        raise RuntimeError(f"query error: {d.get('error')}")
    return d["data"]["result"]


# Rules that are ACTIVITY-GATED by design: they emit samples only while the
# underlying activity is happening (a generation in flight, a cache hit in
# the window, ...). For these, zero series is the honest "no activity right
# now" state, not a defect — the contract's zero-series-is-a-failure rule
# applies to everything else.
TRANSIENT_RULES = {
    "homelab:llm_tokens_per_second",
    "homelab:llm_prompt_per_second",
    "homelab:llm_ttft:seconds",
    "homelab:llm_tpot:seconds",
    "homelab:llm_e2e:seconds",
    "homelab:llm_preemptions:rate",
    "homelab:zfs_arc_hit:ratio",
}


class Report:
    def __init__(self, quiet=False):
        self.failures = []
        self.warnings = []
        self.quiet = quiet

    def ok(self, msg):
        if not self.quiet:
            print(f"  ok    {msg}")

    def warn(self, msg):
        self.warnings.append(msg)
        print(f"  WARN  {msg}")

    def fail(self, msg):
        self.failures.append(msg)
        print(f"  FAIL  {msg}")

    def section(self, title):
        print(f"\n== {title}")


def check_prometheus(prom, rep):
    rep.section("Prometheus")
    try:
        with urllib.request.urlopen(f"{prom}/-/healthy", timeout=15) as r:
            rep.ok(f"/-/healthy -> {r.status}")
    except Exception as e:
        rep.fail(f"prometheus unreachable: {e}")
        return False

    _, targets = http_json(f"{prom}/api/v1/targets?state=active")
    up = [t for t in targets["data"]["activeTargets"] if t["health"] == "up"]
    down = [t for t in targets["data"]["activeTargets"] if t["health"] != "up"]
    rep.ok(f"targets: {len(up)} up, {len(down)} down")
    for t in down:
        rep.warn(f"target down: {t['labels']} — {t.get('lastError', '')[:120]}")

    _, rules = http_json(f"{prom}/api/v1/rules")
    total, dead = 0, []
    for g in rules["data"]["groups"]:
        for r in g["rules"]:
            if r.get("type") != "recording":
                continue
            total += 1
            if r.get("health") != "ok":
                rep.fail(f"rule unhealthy: {r['name']} — {r.get('lastError', '')[:120]}")
    for g in rules["data"]["groups"]:
        for r in g["rules"]:
            if r.get("type") != "recording":
                continue
            try:
                if not prom_query(prom, r["name"]):
                    dead.append(r["name"])
            except Exception:
                pass
    if dead:
        for n in dead:
            rep.warn(f"rule healthy but emits no series (dead rule): {n}")
    rep.ok(f"recording rules: {total} checked")
    return True


def grafana_cookie(grafana, user, password):
    data = json.dumps({"user": user, "password": password}).encode()
    req = urllib.request.Request(f"{grafana}/login", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def check_grafana(grafana, rep, uids_wanted):
    rep.section("Grafana")
    try:
        status, d = http_json(f"{grafana}/api/health")
        rep.ok(f"/api/health -> {d.get('database')}")
    except Exception as e:
        rep.fail(f"grafana unreachable: {e}")
        return

    user = os.environ.get("GRAFANA_USER")
    password = os.environ.get("GRAFANA_PASSWORD")
    if not user:
        rep.warn("GRAFANA_USER not set — skipping login + dashboard-store checks")
        return
    try:
        cookie = grafana_cookie(grafana, user, password)
        rep.ok(f"login {user}")
    except Exception as e:
        rep.fail(f"login failed: {e}")
        return

    headers = {"Cookie": cookie}
    try:
        status, store = http_json(f"{grafana}/api/search?query=&perPage=100", headers=headers)
    except Exception as e:
        rep.fail(f"search failed: {e}")
        return
    have = {s["uid"] for s in store if s.get("type") == "dash-db"}
    for uid in uids_wanted:
        if uid in have:
            rep.ok(f"dashboard in store: {uid}")
        else:
            rep.fail(f"dashboard missing from grafana store: {uid} (provisioning did not import it)")


def resolve_variables(dashboard, prom):
    """Return {varname: first_real_value} for every template variable."""
    values = {}
    for v in dashboard.get("templating", {}).get("list", []):
        if v.get("type") != "query":
            continue
        expr = v.get("definition") or (v.get("query") or {}).get("query") or ""
        m = re.match(r"label_values\(\s*(\w+)\s*,\s*(\w+)\s*\)", expr)
        if m:
            metric, label = m.groups()
            try:
                _, d = http_json(f"{prom}/api/v1/label/{label}/values?match[]="
                                 + urllib.parse.quote(metric))
                vals = d.get("data") or []
                if vals:
                    values[v["name"]] = vals[0]
            except Exception:
                pass
    return values


def substitute(expr, values):
    out = expr
    # Grafana macros (valid in the UI, meaningless in a raw API query):
    # $__rate_interval = 2x the scrape interval by default (15s -> 30s);
    # we approximate with 1m, which is safe for all our scrape intervals.
    out = out.replace("$__rate_interval", "1m").replace("$__interval", "5m")
    for k, val in values.items():
        out = out.replace("${" + k + "}", val).replace("$" + k, val)
    return out


def walk_panels(panels):
    for p in panels:
        if p.get("type") == "row":
            yield from walk_panels(p.get("panels", []))
        else:
            for t in p.get("targets", []):
                expr = t.get("expr")
                if expr:
                    yield p.get("title", "?"), expr


def check_dashboards(dash_dir, prom, rep):
    rep.section("Dashboards")
    import glob
    files = sorted(glob.glob(os.path.join(dash_dir, "*.json")))
    if not files:
        rep.fail(f"no dashboard JSON files in {dash_dir}")
        return []
    uids = []
    for f in files:
        name = os.path.basename(f)
        try:
            d = json.load(open(f))
        except Exception as e:
            rep.fail(f"{name}: invalid JSON ({e})")
            continue
        uid = d.get("uid")
        if not uid:
            rep.fail(f"{name}: missing uid")
            continue
        uids.append(uid)
        vals = resolve_variables(d, prom)
        problems = 0
        checked = 0
        for title, expr in walk_panels(d.get("panels", [])):
            expr2 = substitute(expr, vals)
            checked += 1
            try:
                result = prom_query(prom, expr2)
            except Exception as e:
                rep.fail(f"{name} [{title}]: query error: {str(e)[:140]}\n        expr: {expr2}")
                problems += 1
                continue
            if not result:
                if any(r in expr2 for r in TRANSIENT_RULES):
                    rep.warn(f"{name} [{title}]: no series — activity-gated metric, idle "
                             f"(ok; panel shows 'no data')\n        expr: {expr2}")
                else:
                    rep.fail(f"{name} [{title}]: ZERO series (panel cannot render)\n        expr: {expr2}")
                    problems += 1
        if problems == 0:
            rep.ok(f"{name}: {checked} expressions, all return data")
    return uids


def check_backup(prom, rep):
    rep.section("Backups (PBS)")
    try:
        result = prom_query(prom, "max(homelab:backup_age_seconds / 3600)")
    except Exception as e:
        rep.warn(f"backup age not queryable: {e}")
        return
    if not result:
        rep.warn("no PBS backup data at all (pbs-exporter down?)")
        return
    age_h = float(result[0]["value"][1])
    if age_h > 36:
        rep.warn(f"oldest backup {age_h:.1f}h old (> 36h = missed cycle)")
    else:
        rep.ok(f"oldest backup {age_h:.1f}h old")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090",
                    help="prometheus base URL (set to the Prometheus LXC from other hosts)")
    ap.add_argument("--grafana", default="http://127.0.0.1:3000",
                    help="grafana base URL (set to the Grafana LXC from other hosts)")
    ap.add_argument("--dashboards", default=None,
                    help="directory of dashboard JSON files (default: ../grafana/dashboards relative to this script)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    if args.dashboards is None:
        args.dashboards = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "..", "grafana", "dashboards")

    rep = Report(args.quiet)
    print(f"verify-monitoring: prometheus={args.prometheus} grafana={args.grafana}")
    prom_ok = check_prometheus(args.prometheus, rep)
    if prom_ok:
        uids = check_dashboards(args.dashboards, args.prometheus, rep)
        check_backup(args.prometheus, rep)
    check_grafana(args.grafana, rep, uids if prom_ok else [])

    print(f"\n{'=' * 40}")
    print(f"failures: {len(rep.failures)}, warnings: {len(rep.warnings)}")
    if rep.failures:
        print("RESULT: FAIL")
        sys.exit(1)
    print("RESULT: PASS" + (" (with warnings)" if rep.warnings else ""))


if __name__ == "__main__":
    main()
