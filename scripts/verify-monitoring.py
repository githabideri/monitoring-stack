#!/usr/bin/env python3
"""verify-monitoring.py — post-deploy verification for the homelab
monitoring stack (phase 1).

What it checks, and what "pass" means for each:

  1. prometheus    GET /-/healthy == 200.
  2. targets       list of up/down scrape targets (down is a WARNING,
                   not a failure — a host may legitimately be off).
  3. rules         every recording rule is healthy; a rule that is healthy
                   but emits zero series is a WARNING — unless it is one of
                   the activity-gated rules (TRANSIENT_RULES, documented in
                   docs/metrics.yml), for which zero series while idle is
                   the designed state and is reported as info, not warning.
  4. dashboards    for EVERY dashboard JSON in --dashboards:
                     a. valid JSON with a uid; every datasource uid it
                        references exists in Grafana (needs login)
                     b. every PromQL expression returns >= 1 series against
                        the live Prometheus (template vars substituted with
                        the first real label value; Grafana macros
                        expanded). ZERO series is a FAILURE — a panel that
                        cannot render is a defect, not "no data yet" —
                        EXCEPT activity-gated metrics (idle LLM, quiet ZFS
                        cache), which are reported as info.
                        Known limitation: vars are tested with the FIRST
                        label value only; per-value coverage is a planned
                        extension (exporters differ per host).
                     c. the dashboard is present in Grafana's store with
                        the same uid (i.e. provisioning imported it — this
                        script never writes to Grafana; deployment is
                        Ansible's job).
  5. grafana       GET /api/health == ok; login works; and (the important
                        one) the HOME DASHBOARD: a throwaway user with no
                        stored preference is created, /api/dashboards/home
                        is called as that user, and the served dashboard
                        must be --expected-home-uid (default
                        homelab-overview). This exercises the exact
                        mechanism the Ansible role configures
                        (grafana.ini [dashboards] default_home_dashboard_path);
                        the throwaway user is deleted afterwards.
  6. backups       per PBS namespace, using the newest backup in each:
                     - newest < 36h: current, ok
                     - newest >= 36h and namespace is a node known to the
                       central pve-exporter (pve_node_info): WARNING — a
                       monitored host is missing backup cycles
                     - newest >= 36h, namespace not in the exporter's
                       scope: INFO — historical/decommissioned host (the
                       Backups dashboard shows these explicitly; the exit
                       code is not affected by them).

Exit code: 0 = all pass (info and warnings allowed), 1 = at least one
failure.
Stdlib only; safe to run from any machine that can reach the two
endpoints (both are auth-less on the LAN/Tailscale by design).

Usage:
  verify-monitoring.py [--prometheus URL] [--grafana URL]
                       [--dashboards DIR] [--expected-home-uid UID]
                       [--quiet]
Env: GRAFANA_USER / GRAFANA_PASSWORD (optional; enables the login,
     datasource, store and home-dashboard checks; the home check needs a
     user with admin rights to create the throwaway user).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
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
    total, dead, idle = 0, [], []
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
                    (idle if r["name"] in TRANSIENT_RULES else dead).append(r["name"])
            except Exception:
                pass
    if dead:
        for n in dead:
            rep.warn(f"rule healthy but emits no series (dead rule): {n}")
    if idle:
        rep.ok(f"activity-gated rules currently idle (by design): {', '.join(idle)}")
    rep.ok(f"recording rules: {total} checked")
    return True


def grafana_cookie(grafana, user, password):
    data = json.dumps({"user": user, "password": password}).encode()
    req = urllib.request.Request(f"{grafana}/login", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.headers.get("Set-Cookie", "").split(";")[0]


def grafana_api(grafana, cookie, path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{grafana}{path}", data=data, method=method,
                                 headers={"Cookie": cookie,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or "{}")


def datasource_uids(d):
    """All datasource uids referenced anywhere in a dashboard file:
    file-level, panel-level, target-level — including nested rows."""
    wanted = set()
    if isinstance(d.get("datasource"), dict) and d["datasource"].get("uid"):
        wanted.add(d["datasource"]["uid"])
    for p in d.get("panels", []):
        if isinstance(p.get("datasource"), dict) and p["datasource"].get("uid"):
            wanted.add(p["datasource"]["uid"])
        if p.get("type") == "row":
            wanted |= datasource_uids(p)
        for t in p.get("targets", []):
            if isinstance(t.get("datasource"), dict) and t["datasource"].get("uid"):
                wanted.add(t["datasource"]["uid"])
    return wanted


def check_datasources(grafana, cookie, rep, dashboards):
    """Every datasource uid referenced by a dashboard file must exist."""
    try:
        status, d = grafana_api(grafana, cookie, "/api/datasources?perPage=100")
    except Exception as e:
        rep.warn(f"datasource listing failed: {e}")
        return
    if status != 200:
        rep.warn(f"datasource listing returned {status}")
        return
    have = {x["uid"] for x in (d.get("datasources", []) if isinstance(d, dict) else d)}
    for name, d in dashboards:
        missing = datasource_uids(d) - have
        if missing:
            rep.fail(f"{name}: references missing datasource(s): {sorted(missing)}")


def check_home(grafana, cookie, rep, expected_uid, admin_user):
    """Exercise the home-dashboard mechanism the role configures:
    create a throwaway user (no stored preference), ask Grafana for its
    home dashboard, compare, delete the user."""
    import secrets
    login = "verifycheck_" + secrets.token_hex(3)
    pw = secrets.token_hex(12)
    status, d = grafana_api(grafana, cookie, "/api/admin/users", "POST",
                            {"name": login, "login": login, "password": pw})
    if status != 200:
        rep.warn(f"home check skipped — cannot create throwaway user as {admin_user} "
                 f"(got {status}; needs admin rights)")
        return
    new_id = d.get("id")
    try:
        c2 = grafana_cookie(grafana, login, pw)
        status, d = grafana_api(grafana, c2, "/api/dashboards/home")
        if status != 200:
            rep.fail(f"home dashboard request failed ({status}): {str(d)[:120]}")
            return
        # two shapes: a redirect to the user's stored preference, or the
        # server-served default (file from [dashboards] default_home_dashboard_path)
        if "redirectUri" in d:
            got = d["redirectUri"].split("/")[2]
        else:
            got = d.get("uid") or (d.get("dashboard") or {}).get("uid") or d.get("meta", {}).get("slug")
        if got == expected_uid:
            rep.ok(f"home dashboard: {got} (as a user with no stored preference)")
        else:
            rep.fail(f"home dashboard is {got!r}, expected {expected_uid!r} "
                     f"(check grafana.ini [dashboards] default_home_dashboard_path)")
    finally:
        if new_id:
            status, _ = grafana_api(grafana, cookie, f"/api/admin/users/{new_id}", "DELETE")
            if status != 200:
                rep.warn(f"could not delete throwaway user {login!r} (got {status}) — "
                         f"remove it via the Grafana UI / admin API")


def check_grafana(grafana, rep, uids_wanted, expected_home_uid, dashboards):
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
        rep.warn("GRAFANA_USER not set — skipping login, datasource, store and home checks")
        return
    try:
        cookie = grafana_cookie(grafana, user, password)
        rep.ok(f"login {user}")
    except Exception as e:
        rep.fail(f"login failed: {e}")
        return

    check_datasources(grafana, cookie, rep, dashboards)

    status, store = grafana_api(grafana, cookie, "/api/search?query=&perPage=100")
    if status != 200:
        rep.fail(f"search failed ({status})")
        return
    have = {s["uid"] for s in store if s.get("type") == "dash-db"}
    for uid in uids_wanted:
        if uid in have:
            rep.ok(f"dashboard in store: {uid}")
        else:
            rep.fail(f"dashboard missing from grafana store: {uid} (provisioning did not import it)")

    check_home(grafana, cookie, rep, expected_home_uid, user)


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
        return [], []
    uids, loaded = [], []
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
        loaded.append((name, d))
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
                    rep.ok(f"{name} [{title}]: no series — activity-gated metric, idle (by design)")
                else:
                    rep.fail(f"{name} [{title}]: ZERO series (panel cannot render)\n        expr: {expr2}")
                    problems += 1
        if problems == 0:
            rep.ok(f"{name}: {checked} expressions, all return data")

    # Cross-dashboard links: every /d/<uid> link (top-nav and panel links) must
    # point at a dashboard uid that exists in this set — a wrong prefix (e.g.
    # /d/proxmox when the uid is homelab-proxmox) 404s in the browser.
    link_problems = 0
    checked_links = 0

    def collect_links(d, cands):
        for l in d.get("links", []):
            cands.append((l.get("url"), "top-nav"))

        def walk(panels):
            for p in panels:
                if p.get("type") == "row":
                    walk(p.get("panels", []))
                else:
                    for lk in p.get("links") or []:
                        cands.append((lk.get("url"), "panel:" + str(p.get("title", "?"))))

        walk(d.get("panels", []))

    for name, d in loaded:
        cands = []
        collect_links(d, cands)
        for url, where in cands:
            if not isinstance(url, str) or not url.startswith("/d/"):
                continue
            target = url.split("/d/", 1)[1].split("?", 1)[0].split("/", 1)[0]
            checked_links += 1
            if target not in uids:
                rep.fail(f"{name} [{where}]: link {url} -> no dashboard with uid '{target}'")
                link_problems += 1
    if checked_links and link_problems == 0:
        rep.ok(f"cross-dashboard links: {checked_links} /d/ links, all resolve to a known uid")
    return uids, loaded


def check_backup(prom, rep):
    """Per-namespace backup freshness, three classes (see module docstring):
    current / stale-on-monitored-host / historical-or-out-of-scope."""
    rep.section("Backups (PBS)")
    try:
        newest = prom_query(prom, "max by (namespace) (pbs_snapshot_vm_last_timestamp)")
        never = {r["metric"].get("namespace") for r in
                 prom_query(prom, "count by (namespace) (pbs_snapshot_vm_last_timestamp == 0) > 0")}
        active = {r["metric"].get("node") for r in prom_query(prom, "count by (node) (pve_node_info)")}
    except Exception as e:
        rep.warn(f"backup data not queryable: {e}")
        return
    if not newest:
        rep.warn("no PBS backup data at all (pbs-exporter down?)")
        return
    active.discard(None)
    now = float(newest[0]["value"][0])
    for r in sorted(newest, key=lambda r: float(r["value"][1])):
        ns = r["metric"].get("namespace")
        ts = float(r["value"][1])
        age_h = (now - ts) / 3600 if ts > 0 else None
        extra = " (some guests never backed up)" if ns in never else ""
        if age_h is not None and age_h < 36:
            rep.ok(f"namespace {ns}: newest backup {age_h:.1f}h ago")
        elif ns in active:
            rep.warn(f"namespace {ns} STALE — monitored host, newest backup "
                     f"{age_h / 24:.0f}d ago{extra}")
        else:
            rep.ok(f"namespace {ns}: historical or outside exporter scope — "
                   f"newest backup {age_h / 24:.0f}d ago, no failure "
                   f"(shown on the Backups dashboard)" if age_h is not None
                   else f"namespace {ns}: only never-backed-up entries{extra}")
        if ns in never and ns in active:
            rep.warn(f"namespace {ns}: guests never backed up (pve live-backup job missing?)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090",
                    help="prometheus base URL (set to the Prometheus LXC from other hosts)")
    ap.add_argument("--grafana", default="http://127.0.0.1:3000",
                    help="grafana base URL (set to the Grafana LXC from other hosts)")
    ap.add_argument("--expected-home-uid", default="homelab-overview",
                    help="uid that a user without a stored preference must land on at / (default: homelab-overview)")
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
    uids, loaded = [], []
    if prom_ok:
        uids, loaded = check_dashboards(args.dashboards, args.prometheus, rep)
        check_backup(args.prometheus, rep)
    check_grafana(args.grafana, rep, uids, args.expected_home_uid, loaded)

    print(f"\n{'=' * 40}")
    print(f"failures: {len(rep.failures)}, warnings: {len(rep.warnings)}")
    if rep.failures:
        print("RESULT: FAIL")
        sys.exit(1)
    print("RESULT: PASS" + (" (with warnings)" if rep.warnings else ""))


if __name__ == "__main__":
    main()
