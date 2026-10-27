"""Connection / permission probe, isolated in a subprocess.

Why a subprocess: chDB allows one data path per process and serialises
foreground queries, so a slow remote probe inside the backend would stall the
dashboard (measured 1.8 s stall for a 2 s connect timeout). The child uses an
in-memory chDB with a TLS-only config, receives the password on stdin, never
inherits the NC encryption key, and can be hard-killed on timeout.

Child entry: python -m mlh.probe  (JSON on stdin -> JSON on stdout)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

from .errors import CONNECTED_CATEGORIES, classify
from .schema import QUERY_LOG_COLUMNS
from .settings import PROJECT_ROOT, Settings
from .sqlsafe import format_address, literal, parse_host

MAX_CANDIDATES = 16
_SETTINGS = (
    "connect_timeout_with_failover_ms = 2000, connect_timeout_with_failover_secure_ms = 2000, "
    "connections_with_failover_max_tries = 1, receive_timeout = 5, send_timeout = 5, max_execution_time = 5"
)


# --------------------------------------------------------------------------- parent side
def render_probe_config(settings: Settings) -> str:
    ca = settings.tls_ca_file
    trust = f"<caConfig>{ca}</caConfig><loadDefaultCAFile>false</loadDefaultCAFile>" if ca else "<loadDefaultCAFile>true</loadDefaultCAFile>"
    return (
        "<clickhouse><openSSL><client>" + trust +
        "<verificationMode>strict</verificationMode>"
        "<invalidCertificateHandler><name>RejectCertificateHandler</name></invalidCertificateHandler>"
        "</client></openSSL></clickhouse>\n"
    )


def _timeout_result(c: dict) -> dict[str, Any]:
    return {"host": c["host"], "port": int(c["port"]), "cluster": c.get("cluster"),
            "connect": {"ok": False, "category": "timeout", "connected": False, "codes": [],
                        "message": "제한 시간 안에 이 노드 검사가 끝나지 않았습니다 (미확인)."}}


def _assemble(lines: list[dict], timed_out: bool) -> dict[str, Any]:
    """Keep every result the child managed to emit; unfinished candidates become 'timeout'."""
    endpoint = next((x["result"] for x in lines if x["kind"] == "endpoint"), None)
    if endpoint is None:
        message = "연결 검사가 제한 시간 안에 끝나지 않았습니다." if timed_out else "연결 검사 프로세스가 비정상 종료했습니다."
        return {"fatal": {"category": "timeout" if timed_out else "unclassified", "message": message, "codes": []}}
    planned = next((x for x in lines if x["kind"] == "plan"), {"candidates": [], "source": "system.clusters"})
    done = {(c["host"], c["port"]): c for c in (x["result"] for x in lines if x["kind"] == "candidate")}
    candidates = [done.get((c["host"], int(c["port"])), _timeout_result(c)) for c in planned["candidates"]]
    return {"endpoint": endpoint, "candidates": candidates, "candidates_source": planned["source"], "partial": timed_out}


def run_probe(settings: Settings, endpoint: dict[str, Any], candidates: list[dict] | None) -> dict[str, Any]:
    payload = {
        **endpoint,
        "candidates": candidates,
        "tag": settings.collector_tag,
        "config": str(settings.data_dir / "probe-config.xml"),
    }
    env = {k: v for k, v in os.environ.items() if not k.startswith("MLH_NC_KEY")}
    budget = settings.probe_timeout_seconds + 8 * (len(candidates) if candidates is not None else 4)
    proc = subprocess.Popen([sys.executable, "-m", "mlh.probe"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, cwd=PROJECT_ROOT, env=env)
    timed_out = False
    try:
        out, _ = proc.communicate(json.dumps(payload), timeout=budget)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        out, _ = proc.communicate()
    lines = [json.loads(line) for line in (out or "").splitlines() if line.strip().startswith("{")]
    return _assemble(lines, timed_out)


def required_ok(target: dict[str, Any]) -> bool:
    checks = target.get("checks", {})
    return bool(target.get("connect", {}).get("ok")) and all(
        checks.get(name, {}).get("ok") for name in ("metrics", "query_log")
    )


# --------------------------------------------------------------------------- child side
def _error(exc: BaseException) -> dict[str, Any]:
    info = classify(str(exc))
    return {"ok": False, **info.to_dict(), "connected": info.category in CONNECTED_CATEGORIES}


def _run(sess, sql: str, params: dict) -> list[dict]:
    out = sess.query(sql, "JSONEachRow", params=params).bytes().decode()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _source(fn: str, table: str) -> str:
    return f"{fn}({{addr:String}}, system.{table}, {{u:String}}, {{p:String}})"


def _probe_target(sess, target: dict, tag: str) -> dict[str, Any]:
    fn = "remoteSecure" if target["tls"] else "remote"
    params = {"addr": format_address(target["host"], int(target["port"])), "u": target["username"], "p": target["password"]}
    settings = f"SETTINGS log_comment = {literal(tag)}, {_SETTINGS}"
    started = time.monotonic()
    result: dict[str, Any] = {"host": target["host"], "port": int(target["port"])}
    try:
        info = _run(sess, "SELECT version() AS version, displayName() AS display_name, toString(serverUUID()) AS server_uuid,"
                          f" timezone() AS timezone FROM {_source(fn, 'one')} {settings}", params)[0]
        result["connect"] = {"ok": True, **info}
    except Exception as exc:  # noqa: BLE001 - classified, raw text dropped
        err = _error(exc)
        result["connect"] = err
        if not err["connected"]:
            result["duration_ms"] = int((time.monotonic() - started) * 1000)
            return result
    checks: dict[str, Any] = {}
    sqls = {
        "metrics": f"SELECT count() AS rows FROM {_source(fn, 'metrics')} {settings}",
        "query_log": f"SELECT count() AS rows FROM (SELECT {', '.join(QUERY_LOG_COLUMNS)} FROM {_source(fn, 'query_log')}"
                     f" WHERE event_date >= today() - 1 AND event_time >= now() - INTERVAL 1 HOUR) {settings}",
        "clusters": f"SELECT cluster, shard_num, replica_num, host_name, port, is_local FROM {_source(fn, 'clusters')} {settings}",
    }
    for name, sql in sqls.items():
        try:
            rows = _run(sess, sql, params)
            checks[name] = {"ok": True, "rows": rows if name == "clusters" else int(rows[0]["rows"])}
        except Exception as exc:  # noqa: BLE001
            checks[name] = _error(exc)
    result["checks"] = checks
    result["duration_ms"] = int((time.monotonic() - started) * 1000)
    return result


def _derive_candidates(endpoint_result: dict) -> list[dict]:
    """Nodes of every cluster definition that contains the endpoint itself (is_local=1)."""
    rows = endpoint_result.get("checks", {}).get("clusters", {}).get("rows")
    if not isinstance(rows, list):
        return []
    local_clusters = {r["cluster"] for r in rows if int(r["is_local"]) == 1}
    seen, found = set(), []
    for r in rows:
        try:  # server-provided names must still be a single host, never an address pattern
            host = parse_host(str(r["host_name"]))
        except ValueError:
            continue
        key = (host, int(r["port"]))
        if r["cluster"] in local_clusters and int(r["is_local"]) == 0 and key not in seen:
            seen.add(key)
            found.append({"host": host, "port": int(r["port"]), "cluster": r["cluster"]})
    return found[:MAX_CANDIDATES]


def _emit(kind: str, **data: Any) -> None:
    sys.stdout.write(json.dumps({"kind": kind, **data}) + "\n")
    sys.stdout.flush()  # the parent keeps everything emitted before a timeout kill


def _child(payload: dict) -> None:
    from chdb import session as chs

    sess = chs.Session(f"file::memory:?config-file={payload['config']}")
    endpoint = _probe_target(sess, payload, payload["tag"])
    _emit("endpoint", result=endpoint)
    explicit = payload.get("candidates")
    candidates = (explicit if explicit is not None else _derive_candidates(endpoint))[:MAX_CANDIDATES]
    _emit("plan", candidates=candidates, source="explicit" if explicit is not None else "system.clusters")
    for c in candidates:
        result = _probe_target(sess, {**payload, "host": c["host"], "port": c["port"]}, payload["tag"])
        _emit("candidate", result={**result, "cluster": c.get("cluster")})
    sess.close()


if __name__ == "__main__":
    _child(json.loads(sys.stdin.read()))
