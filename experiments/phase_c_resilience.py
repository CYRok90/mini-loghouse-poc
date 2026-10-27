"""Phase C: correctness and failure recovery against the running test environment.

Prereqs: scripts/testenv.py start, clusters A (a1,a2) and B (b1) registered.
The script owns the backend process (stop/start) using scripts/dev.env.

Usage: python experiments/phase_c_resilience.py [c1|c2|c3|c4|c5|all]
Writes evidence/phase_c/<scenario>.json
"""

from __future__ import annotations

import json
import os
from collections import Counter
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import testenv  # noqa: E402

OUT = ROOT / "evidence" / "phase_c"
BASE = "http://127.0.0.1:8765"
PID_FILE = ROOT / "var" / "backend.pid"
SETTLE = 30  # flush 7.5s + safety lag 8s + 2 refreshes (5s) + margin


def env_file() -> dict[str, str]:
    values = {}
    for line in (ROOT / "scripts" / "dev.env").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            values[k] = v
    return {**os.environ, **values}


def client() -> httpx.Client:
    return httpx.Client(base_url=BASE, timeout=60)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- backend control
def backend_alive() -> bool:
    try:
        return client().get("/api/health").status_code == 200
    except httpx.HTTPError:
        return False


def backend_stop() -> float:
    pid = int(PID_FILE.read_text().split()[0])
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    if "-m mlh" not in cmd:  # stale pid file must never signal an unrelated process (Codex P2-11)
        raise RuntimeError(f"pid {pid} is not the mini-loghouse backend")
    started = time.monotonic()
    os.kill(pid, signal.SIGTERM)
    while time.monotonic() - started < 60:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return time.monotonic() - started
        time.sleep(0.2)
    raise RuntimeError("backend did not stop")


def backend_start(logname: str = "backend.log") -> subprocess.Popen:
    OUT.mkdir(parents=True, exist_ok=True)
    out = open(OUT / logname, "ab")
    proc = subprocess.Popen([sys.executable, "-m", "mlh"], cwd=ROOT, env=env_file(), stdout=out, stderr=out, start_new_session=True)
    PID_FILE.write_text(str(proc.pid))
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not backend_alive():
        time.sleep(0.5)
    return proc


# ---------------------------------------------------------------- data helpers
A, B = "A", "B"  # cluster_a endpoint a1:19401 (TLS), cluster_b endpoint b1:19003


def clusters() -> dict[str, dict]:
    """Keyed by role (A/B) via the endpoint port, so renaming a cluster in the UI does not matter."""
    roles = {19401: A, 19003: B}
    return {roles.get(c["port"], c["name"]): c for c in client().get("/api/clusters").json()["clusters"]}


def diag(cluster_id: str, prefix: str | None = None) -> dict:
    params = {"prefix": prefix} if prefix else {}
    return client().get(f"/api/clusters/{cluster_id}/diagnostics", params=params).json()


def flush_all() -> None:
    for node in testenv.NODES.values():
        if testenv.pid_of(node):
            testenv.http_query(node, "SYSTEM FLUSH LOGS")


def source_events(prefix: str, nodes: list[str]) -> list[tuple]:
    sql = ("SELECT displayName() AS node, query_id, toString(type) AS type, toUnixTimestamp64Micro(event_time_microseconds) AS t,"
           " is_initial_query AS initial"
           f" FROM system.query_log WHERE (startsWith(query_id, '{prefix}') OR startsWith(initial_query_id, '{prefix}'))"
           " AND log_comment = 'mlh-testenv' FORMAT JSONEachRow")
    found = []
    for name in nodes:
        for line in testenv.http_query(testenv.NODES[name], sql).splitlines():
            r = json.loads(line)
            found.append((r["node"], r["query_id"], r["type"], int(r["t"]), int(r["initial"])))
    return found


def dest_events(cluster_ids: list[str], prefix: str) -> tuple[list[tuple], dict]:
    """Multiset (list), not set: a duplicated row in the dedup view must fail the check (Codex P2-12)."""
    found, stats = [], {}
    for cid in cluster_ids:
        d = diag(cid, prefix)["prefix"]
        stats[cid] = {k: d[k] for k in ("raw_rows", "final_rows", "initial_queries")}
        found += [(e["node"], e["query_id"], e["type"], int(e["t"])) for e in d["events"]]
    return found, stats


def compare(prefix: str, nodes: list[str], cluster_ids: list[str]) -> dict:
    flush_all()
    src_full = source_events(prefix, nodes)
    src = [e[:4] for e in src_full]
    dst, stats = dest_events(cluster_ids, prefix)
    src_c, dst_c = Counter(src), Counter(dst)
    # computed from the SOURCE (independent of the dashboard SQL): initial, non-QueryStart rows
    expected_initial = sum(1 for e in src_full if e[4] == 1 and e[2] != "QueryStart")
    final_rows = sum(v["final_rows"] for v in stats.values())
    initial = sum(v["initial_queries"] for v in stats.values())
    checks = {
        "non_empty": len(src) > 0,
        "multiset_equal": src_c == dst_c,
        "final_rows_equal_source": final_rows == len(src),
        "initial_queries_independent": initial == expected_initial,
    }
    return {
        "prefix": prefix, "source_events": len(src), "dest_events": len(dst),
        "missing_in_dest": sorted(map(list, (src_c - dst_c).elements()))[:20],
        "extra_in_dest": sorted(map(list, (dst_c - src_c).elements()))[:20],
        "dest_stats": stats, "expected_initial_queries": expected_initial, "checks": checks,
        "match": all(checks.values()),
    }


def save(name: str, data: dict) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    log(f"{name}: {json.dumps(data, ensure_ascii=False)[:600]}")
    return data


def node_states(role: str) -> dict:
    c = clusters()[role]
    return {"cluster_state": c["state"], "coverage": c["coverage"]["label"],
            "nodes": {n["display_name"]: {s: (v["state"], (v["error"] or {}).get("category")) for s, v in n["sources"].items()} for n in c["nodes"]}}


# ---------------------------------------------------------------- scenarios
def c1() -> dict:
    """Reconciliation, raw duplicates, query_id reuse, distributed sub-queries."""
    tag = f"c1x{int(time.time())}"
    testenv.workload(tag, 5)
    a1 = testenv.NODES["a1"]
    reuse = f"{tag}-reuse"
    testenv.http_query(a1, "SELECT 1", reuse)
    testenv.http_query(a1, "SELECT 2", reuse)  # same query_id, different query: must stay 2 events
    testenv.http_query(a1, "SELECT count() FROM clusterAllReplicas('cluster_a', system.one)", f"{tag}-dist")
    time.sleep(SETTLE)
    cl = clusters()
    ids = [cl[A]["id"], cl[B]["id"]]
    result = compare(tag, ["a1", "a2", "b1"], ids)
    a = diag(ids[0], f"{tag}-reuse")["prefix"]["events"]
    dist = diag(ids[0], f"{tag}-dist")["prefix"]["events"]
    result["query_id_reuse_events"] = [(e["query_id"], e["type"]) for e in a]
    result["distributed_events"] = [(e["node"], e["type"], e["is_initial_query"]) for e in dist]
    result["raw_vs_final_all"] = {cid: diag(cid)["per_node"] for cid in ids}
    return save("c1_reconciliation", result)


def c2() -> dict:
    """Backend down while sources produce events -> gap + explicit backfill after restart."""
    tag = f"c2x{int(time.time())}"
    before = {name: c["nodes"] for name, c in clusters().items()}
    stop_s = backend_stop()
    log(f"backend stopped in {stop_s:.1f}s; generating {tag} while down")
    testenv.workload(tag, 3)
    down_for = 45
    time.sleep(down_for)
    backend_start("backend-c2.log")
    log("backend restarted; waiting for refresh + backfill")
    time.sleep(SETTLE + 15)
    cl = clusters()
    ids = [cl[A]["id"], cl[B]["id"]]
    result = compare(tag, ["a1", "a2", "b1"], ids)
    result.update({
        "backend_stop_seconds": round(stop_s, 1), "downtime_seconds": down_for,
        "registry_preserved": sorted(before) == sorted(cl), "states_after": {n: node_states(n) for n in cl},
        "gaps": {cid: [g for g in diag(cid)["gaps"] if g["reason"] == "collection_interrupted"][-3:] for cid in ids},
        "credential_reentry_needed": any(n["phase"] == "needs_credentials" for c in cl.values() for n in c["nodes"]),
    })
    return save("c2_backend_restart", result)


def c3() -> dict:
    """Stop node a2: a2 error, a1 + cluster B keep collecting, dashboard responsive; then recover."""
    tag = f"c3x{int(time.time())}"
    testenv.stop(["a2"])
    time.sleep(20)
    during = {n: node_states(n) for n in clusters()}
    cl = clusters()
    timings = {}
    for name, c in cl.items():
        t0 = time.monotonic()
        code = client().get(f"/api/clusters/{c['id']}/dashboard", params={"range": "15m"}).status_code
        timings[name] = {"status": code, "seconds": round(time.monotonic() - t0, 3)}
    testenv.workload(f"{tag}-down", 2)  # a1 and b1 only (a2 stopped)
    time.sleep(SETTLE)
    down_cmp = compare(f"{tag}-down", ["a1", "b1"], [cl[A]["id"], cl[B]["id"]])
    testenv.start(["a2"])
    time.sleep(10)
    testenv.workload(f"{tag}-up", 2)
    time.sleep(SETTLE + 15)
    up_cmp = compare(f"{tag}-up", ["a1", "a2", "b1"], [cl[A]["id"], cl[B]["id"]])
    a_id = cl[A]["id"]
    return save("c3_node_failure", {
        "during_outage": during, "dashboard_during_outage": timings, "collected_while_a2_down": down_cmp,
        "after_recovery": {n: node_states(n) for n in clusters()}, "collected_after_recovery": up_cmp,
        "a_transitions": client().get(f"/api/clusters/{a_id}").json()["transitions"][:8],
        "a_gaps": diag(a_id)["gaps"][-4:],
    })


def c4() -> dict:
    """No duplicate collectors: second backend refused; page loads/tabs do not create RMVs."""
    cl = clusters()
    views_before = {n: sorted(diag(c["id"])["collection_views"]) for n, c in cl.items()}
    second = subprocess.run([sys.executable, "-m", "mlh"], cwd=ROOT, env=env_file(), capture_output=True, text=True, timeout=60)
    lines = [ln for ln in (second.stdout + second.stderr).splitlines() if "another mini-loghouse backend" in ln or "startup failed" in ln.lower()]
    c = client()
    for _ in range(10):  # simulate many tabs / auto-refresh
        for name, item in cl.items():
            c.get("/"); c.get(f"/clusters/{item['id']}")
            c.get(f"/api/clusters/{item['id']}"); c.get(f"/api/clusters/{item['id']}/dashboard", params={"range": "1h"})
    views_after = {n: sorted(diag(i["id"])["collection_views"]) for n, i in cl.items()}
    return save("c4_no_duplicates", {
        "second_instance_exit_code": second.returncode, "second_instance_log": lines[:3],
        "original_still_serving": backend_alive(), "views_before": views_before, "views_after": views_after,
        "views_unchanged": views_before == views_after,
    })


def c5() -> dict:
    """Plaintext secret scan over data dir, logs, evidence and API responses."""
    secrets_ = json.loads((ROOT / "var" / "testenv" / "secrets.json").read_text())
    marks = {k: v.encode() for k, v in secrets_.items()}
    roots = [ROOT / "var" / "app", ROOT / "evidence", ROOT / "mlh", ROOT / "var" / "testenv"]
    allowed = {ROOT / "var" / "testenv" / "secrets.json", ROOT / "var" / "testenv" / "e2e_password"}
    hits = []
    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and path not in allowed:
                data = path.read_bytes()
                hits += [f"{path.relative_to(ROOT)}:{k}" for k, m in marks.items() if m in data]
    c = client()
    api_text = "".join(c.get(u).text for u in ["/api/clusters", "/api/health"] +
                       [f"/api/clusters/{x['id']}/dashboard" for x in clusters().values()] +
                       [f"/api/clusters/{x['id']}/diagnostics" for x in clusters().values()])
    nc_files = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "var" / "app" / "chdb" / "named_collections").glob("*"))
    return save("c5_secret_scan", {
        "plaintext_hits": hits, "api_hits": [k for k, m in marks.items() if m.decode() in api_text],
        "named_collection_files": nc_files, "scanned_roots": [str(r.relative_to(ROOT)) for r in roots],
    })


def passed(name: str, r: dict) -> bool:
    """Required conditions per scenario; any failure makes the script exit non-zero (Codex P2-12)."""
    if name == "c1":
        return r["match"] and len(r["query_id_reuse_events"]) == 4
    if name == "c2":
        return r["match"] and r["registry_preserved"] and not r["credential_reentry_needed"]
    if name == "c3":
        down = r["during_outage"][A]["nodes"]
        return (r["collected_while_a2_down"]["match"] and r["collected_after_recovery"]["match"]
                and down["a2"]["metrics"][0] == "error" and down["a1"]["metrics"][0] == "collecting")
    if name == "c4":
        return r["second_instance_exit_code"] != 0 and r["original_still_serving"] and r["views_unchanged"]
    return not r["plaintext_hits"] and not r["api_hits"]


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    steps = {"c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5}
    failed = []
    for key, fn in steps.items():
        if which in (key, "all"):
            log(f"== {key} ==")
            if not passed(key, fn()):
                failed.append(key)
    log(f"RESULT: {'FAIL ' + ','.join(failed) if failed else 'PASS'}")
    sys.exit(1 if failed else 0)
