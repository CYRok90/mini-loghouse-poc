"""Cluster status views and dashboard aggregations (chDB stored data only).

Aggregation definitions (also rendered in the UI):
- 기록된 initial 쿼리 수: is_initial_query = 1 AND type != 'QueryStart'
  (QueryFinish + ExceptionBeforeStart + ExceptionWhileProcessing). QueryStart
  is excluded so one query is never counted twice. Client retries with a new
  query_id are separate queries.
- 실패 수: initial rows with type ExceptionBeforeStart / ExceptionWhileProcessing.
- 노드 실행 이벤트 수: all non-QueryStart rows on every node, including the
  is_initial_query = 0 sub-queries a distributed query runs on each shard.
- 성공 쿼리 지연 p95: quantileExact(0.95) of query_duration_ms over initial
  QueryFinish rows (sample count returned; null when there are no samples).
- Dedup: every query reads telemetry.query_log_v (ReplacingMergeTree FINAL) whose
  identity is (cluster, node, event_time_microseconds, type, query_id,
  query_start_time_microseconds). query_id alone is never used to merge.
- Collector's own queries are excluded at collection time (log_comment).
"""

from __future__ import annotations

import time
from typing import Any

from .chdb_owner import ChdbOwner
from .errors import CONNECTION_FAILURES
from .registry import Registry
from .settings import Settings
from .sqlsafe import literal
from .status import STATE_LABEL, coverage_label, worst_state

RANGES = {"15m": (900, 60), "1h": (3600, 60), "24h": (86400, 1800)}
EXCEPTIONS = "('ExceptionBeforeStart', 'ExceptionWhileProcessing')"
CURATED_METRICS = (
    "Query", "TCPConnection", "HTTPConnection", "MemoryTracking", "Merge",
    "BackgroundMergesAndMutationsPoolTask", "PartsActive", "GlobalThreadActive",
)
_METRIC_SET = "(" + ", ".join(literal(m) for m in CURATED_METRICS) + ")"
_LIMITS = (" SETTINGS max_execution_time = 10, max_result_rows = 200000, result_overflow_mode = 'throw',"
           " output_format_json_quote_64bit_integers = 0")


def _ms(ts: float | None) -> int | None:
    return int(ts * 1000) if ts else None


# --------------------------------------------------------------------------- status
def node_view(registry: Registry, node: dict) -> dict[str, Any]:
    sources = {s: registry.source_status(node["id"], s) or {} for s in ("metrics", "query_log")}
    states = [sources[s].get("state", "preparing") for s in sources]
    confirmed = node["phase"] == "active" and all(sources[s].get("last_success_at") for s in sources)
    return {
        "id": node["id"], "host": node["host"], "port": node["port"], "display_name": node["display_name"],
        "server_version": node["server_version"], "phase": node["phase"], "confirmed": confirmed,
        "state": worst_state(states), "state_label": STATE_LABEL[worst_state(states)],
        "sources": {s: _source_view(v) for s, v in sources.items()},
    }


def _source_view(status: dict) -> dict[str, Any]:
    state = status.get("state", "preparing")
    return {
        "state": state, "state_label": STATE_LABEL[state],
        "last_attempt_ms": _ms(status.get("last_attempt_at")), "last_success_ms": _ms(status.get("last_success_at")),
        "error": {"category": status["last_error_category"], "message": status["last_error_message"],
                  "consecutive_failures": status.get("consecutive_failures", 0)} if status.get("last_error_category") else None,
    }


RECOVERY_ERROR = {"category": "recovery_mode", "message": "복구 모드: 암호화 키 문제로 수집이 중단되었습니다.",
                  "consecutive_failures": 0}


def cluster_view(registry: Registry, settings: Settings, cluster: dict, recovery: bool = False) -> dict[str, Any]:
    view = _cluster_view(registry, settings, cluster)
    if not recovery:
        return view
    # SQLite still holds the last persisted states; collection is not running, so never show them as live.
    return {**view, "state": "error", "state_label": STATE_LABEL["error"], "latest_error": RECOVERY_ERROR,
            "last_check": {**view["last_check"], "ok": False, "state": "unknown"}}


def _connection_check(cluster: dict, attempts: list[int], errors: list[dict]) -> dict[str, Any]:
    """Connection result is separate from collection result (Codex P2-9).

    failed : some source failed with a reach/auth category (refused, TLS, auth, ...)
    unknown: the only errors are local (credential missing) - reachability was not tested
    ok     : otherwise; permission/table/column errors mean the server answered and authenticated
    """
    if not attempts:
        state = "ok" if cluster["last_check_ok"] else "failed"
        return {"at_ms": _ms(cluster["last_check_at"]), "state": state, "ok": state == "ok", "source": "registration_probe"}
    categories = {e["category"] for e in errors}
    if categories & CONNECTION_FAILURES:
        state = "failed"
    elif categories and categories <= {"nc_missing", "recovery_mode"}:
        state = "unknown"
    else:
        state = "ok"
    return {"at_ms": max(attempts), "state": state, "ok": state == "ok", "source": "collection"}


def _cluster_view(registry: Registry, settings: Settings, cluster: dict) -> dict[str, Any]:
    nodes = [node_view(registry, n) for n in registry.nodes(cluster["id"])]
    sources = [src for n in nodes for src in n["sources"].values()]
    attempts = [s["last_attempt_ms"] for s in sources if s["last_attempt_ms"]]
    successes = [s["last_success_ms"] for s in sources if s["last_success_ms"]]
    errors = [s["error"] for s in sources if s["state"] == "error" and s["error"]]
    confirmed = sum(1 for n in nodes if n["confirmed"])
    discovered = bool(cluster["discovered_cluster"])
    state = worst_state([n["state"] for n in nodes])
    return {
        "id": cluster["id"], "name": cluster["name"], "host": cluster["host"], "port": cluster["port"],
        "tls": bool(cluster["tls"]), "username": cluster["username"], "server_version": cluster["server_version"],
        "phase": cluster["phase"], "state": state, "state_label": STATE_LABEL[state],
        "last_check": _connection_check(cluster, attempts, errors),
        "last_success_ms": min(successes) if len(successes) == len(sources) and sources else None,
        "newest_success_ms": max(successes) if successes else None,
        "latest_error": errors[0] if errors else None,
        "coverage": {
            "label": coverage_label(confirmed, cluster["candidate_count"], discovered),
            "confirmed_nodes": confirmed, "registered_nodes": len(nodes),
            "candidate_nodes": cluster["candidate_count"], "discovered_cluster": cluster["discovered_cluster"],
        },
        "stale_after_seconds": settings.stale_after_seconds, "refresh_seconds": settings.refresh_seconds,
        "nodes": nodes,
    }


# --------------------------------------------------------------------------- dashboard
def _where(node_id: str | None, time_col: str) -> str:
    node = " AND node_id = {nid:String}" if node_id else ""
    if time_col == "event_time":
        span = "event_time >= toDateTime({f:UInt32}) AND event_time < toDateTime({t:UInt32})"
    else:
        span = f"{time_col} >= toDateTime64({{f:UInt32}}, 3) AND {time_col} < toDateTime64({{t:UInt32}}, 3)"
    return f" WHERE cluster_id = {{cid:String}} AND {span}{node}"


def _query_sections(owner: ChdbOwner, params: dict, node_id: str | None) -> dict[str, Any]:
    w = _where(node_id, "event_time")
    src = "FROM telemetry.query_log_v"
    summary = owner.rows(
        "SELECT countIf(is_initial_query = 1 AND type != 'QueryStart') AS initial_queries,"
        f" countIf(is_initial_query = 1 AND type IN {EXCEPTIONS}) AS initial_failed,"
        " countIf(type != 'QueryStart') AS node_events, countIf(type = 'QueryStart') AS query_start_rows,"
        " quantileExactIf(0.95)(query_duration_ms, is_initial_query = 1 AND type = 'QueryFinish') AS p95_ms,"
        f" countIf(is_initial_query = 1 AND type = 'QueryFinish') AS p95_samples {src}{w}{_LIMITS}", params)[0]
    series = owner.rows(
        "SELECT toUnixTimestamp(toStartOfInterval(event_time, toIntervalSecond({b:UInt32}))) AS bucket,"
        " countIf(is_initial_query = 1 AND type != 'QueryStart') AS queries,"
        f" countIf(is_initial_query = 1 AND type IN {EXCEPTIONS}) AS failed,"
        " quantileExactIf(0.95)(query_duration_ms, is_initial_query = 1 AND type = 'QueryFinish') AS p95_ms,"
        f" countIf(is_initial_query = 1 AND type = 'QueryFinish') AS ok_samples {src}{w}"
        f" GROUP BY bucket ORDER BY bucket{_LIMITS}", params)
    cols = ("node_id, source_display_name, toUnixTimestamp64Milli(event_time_microseconds) AS ts_ms, query_id, type,"
            " is_initial_query, user, query_duration_ms, read_rows, exception_code,"
            " substring(exception, 1, 300) AS exception, substring(query, 1, 2000) AS query")
    tables = {
        "recent": f"SELECT {cols} {src}{w} AND type != 'QueryStart' ORDER BY event_time_microseconds DESC LIMIT 50",
        "slow": f"SELECT {cols} {src}{w} AND type = 'QueryFinish' ORDER BY query_duration_ms DESC LIMIT 20",
        "errors": f"SELECT {cols} {src}{w} AND type IN {EXCEPTIONS} ORDER BY event_time_microseconds DESC LIMIT 20",
    }
    return {
        "summary": {**summary, "p95_ms": summary["p95_ms"] if int(summary["p95_samples"]) else None},
        "series": [{**r, "p95_ms": r["p95_ms"] if int(r["ok_samples"]) else None} for r in series],
        "tables": {k: owner.rows(sql + _LIMITS, params) for k, sql in tables.items()},
    }


def _metric_sections(owner: ChdbOwner, params: dict, node_id: str | None) -> dict[str, Any]:
    p = params
    node = " AND node_id = {nid:String}" if node_id else ""
    current = owner.rows(
        "SELECT node_id, any(source_display_name) AS display_name, metric, argMax(value, collected_at) AS value,"
        " toUnixTimestamp64Milli(max(collected_at)) AS at_ms FROM telemetry.metrics_raw"
        f" WHERE cluster_id = {{cid:String}}{node} AND metric IN {_METRIC_SET}"
        " AND collected_at >= now64(3) - INTERVAL 1 DAY GROUP BY node_id, metric ORDER BY metric, node_id" + _LIMITS, p)
    series = owner.rows(
        "SELECT bucket, metric, sum(v) AS value, count() AS nodes FROM ("
        " SELECT toUnixTimestamp(toStartOfInterval(collected_at, toIntervalSecond({b:UInt32}))) AS bucket, node_id, metric,"
        f" max(value) AS v FROM telemetry.metrics_raw{_where(node_id, 'collected_at')} AND metric IN {_METRIC_SET}"
        " GROUP BY bucket, node_id, metric) GROUP BY bucket, metric ORDER BY bucket" + _LIMITS, p)
    return {"current": current, "series": series, "curated": list(CURATED_METRICS)}


def _subtract(spans: list[tuple[int, int]], holes: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out = spans
    for h_lo, h_hi in holes:
        nxt = []
        for lo, hi in out:
            if h_hi <= lo or h_lo >= hi:
                nxt.append((lo, hi))
                continue
            if lo < h_lo:
                nxt.append((lo, h_lo))
            if h_hi < hi:
                nxt.append((h_hi, hi))
        out = nxt
    return out


def node_coverage(cursor: dict | None, gaps: list[dict], f: int, t: int, tail_allowance: int) -> dict[str, Any]:
    """Seconds of [f, t) actually collected for one node (Codex P2-7).

    Collected = [min(first_lo, backfilled gap lo), committed_hi) minus gaps not yet backfilled.
    'full' tolerates the normal not-yet-collected tail (safety lag + one refresh).
    """
    backfilled = [g["lo"] for g in gaps if g["state"] == "backfilled"]
    starts = ([cursor["first_lo"]] if cursor else []) + backfilled
    if not cursor or not starts:
        spans: list[tuple[int, int]] = []
    else:
        lo, hi = max(f, min(starts)), min(t, int(cursor["committed_hi"]))
        holes = [(int(g["lo"]), int(g["hi"])) for g in gaps if g["state"] != "backfilled"]
        spans = _subtract([(lo, hi)] if hi > lo else [], holes)
    covered = sum(hi - lo for lo, hi in spans)
    expected = max(1, (t - tail_allowance) - f)
    state = "none" if covered == 0 else ("full" if covered >= expected - 1 else "partial")
    return {"state": state, "covered_seconds": covered, "expected_seconds": expected,
            "spans_ms": [[_ms(lo), _ms(hi)] for lo, hi in spans]}


def _coverage(registry: Registry, node_ids: list[str], f: int, t: int, tail_allowance: int) -> dict[str, Any]:
    out = {}
    gaps = registry.gaps(node_ids)
    for nid in node_ids:
        cursor = registry.cursor(nid)
        node_gaps = [g for g in gaps if g["node_id"] == nid]
        backfilled_lo = [g["lo"] for g in node_gaps if g["state"] == "backfilled"]
        starts = ([cursor["first_lo"]] if cursor else []) + backfilled_lo
        out[nid] = {
            "collected_from_ms": _ms(min(starts)) if starts else None,
            "committed_hi_ms": _ms(cursor["committed_hi"]) if cursor else None,
            "gaps": [{"lo_ms": _ms(g["lo"]), "hi_ms": _ms(g["hi"]), "state": g["state"], "reason": g["reason"]}
                     for g in node_gaps if g["state"] != "backfilled"],
            "range": node_coverage(cursor, node_gaps, f, t, tail_allowance),
        }
    return out


def range_state(coverage: dict[str, Any]) -> str:
    states = {c["range"]["state"] for c in coverage.values()}
    if not states or states == {"none"}:
        return "none"
    return "full" if states == {"full"} else "partial"


def dashboard(owner: ChdbOwner, registry: Registry, settings: Settings, cluster: dict,
              range_key: str, node_id: str | None) -> dict[str, Any]:
    span, bucket = RANGES[range_key]
    now = int(time.time())
    params: dict[str, Any] = {"cid": cluster["id"], "f": now - span, "t": now, "b": bucket}
    if node_id:
        params["nid"] = node_id
    node_ids = [node_id] if node_id else [n["id"] for n in registry.nodes(cluster["id"])]
    coverage = _coverage(registry, node_ids, now - span, now, settings.safety_lag_seconds + 2 * settings.refresh_seconds)
    return {
        "generated_ms": now * 1000, "range": range_key, "from_ms": (now - span) * 1000, "to_ms": now * 1000,
        "bucket_seconds": bucket, "node_id": node_id,
        "queries": _query_sections(owner, params, node_id),
        "metrics": _metric_sections(owner, params, node_id),
        "coverage": coverage,
        "range_state": range_state(coverage),
        "safety_lag_seconds": settings.safety_lag_seconds,
    }


# --------------------------------------------------------------------------- diagnostics
def diagnostics(owner: ChdbOwner, registry: Registry, cluster: dict, prefix: str | None) -> dict[str, Any]:
    """Raw vs deduplicated counts, collection objects and (optionally) the event set for a query_id prefix."""
    params: dict[str, Any] = {"cid": cluster["id"]}
    per_node = owner.rows(
        "SELECT node_id, collect_path, count() AS raw_rows, uniqExact(event_time_microseconds, type, query_id,"
        " query_start_time_microseconds) AS distinct_events FROM telemetry.query_log_raw"
        " WHERE cluster_id = {cid:String} GROUP BY node_id, collect_path ORDER BY node_id, collect_path" + _LIMITS, params)
    mvs = owner.rows("SELECT name FROM system.tables WHERE database = 'telemetry' AND engine = 'MaterializedView'"
                     " AND (name LIKE 'mv_m_%' OR name LIKE 'mv_q_%') ORDER BY name" + _LIMITS)
    node_ids = {n["id"] for n in registry.nodes(cluster["id"])}
    out: dict[str, Any] = {
        "per_node": per_node,
        "collection_views": [m["name"] for m in mvs if m["name"][5:] in node_ids],
        "cursors": {nid: registry.cursor(nid) for nid in node_ids},
        "gaps": registry.gaps(sorted(node_ids)),
    }
    if prefix:
        p = {**params, "prefix": prefix}
        cond = (" WHERE cluster_id = {cid:String} AND (startsWith(query_id, {prefix:String})"
                " OR startsWith(initial_query_id, {prefix:String}))")
        out["prefix"] = {
            "raw_rows": owner.rows("SELECT count() AS c FROM telemetry.query_log_raw" + cond + _LIMITS, p)[0]["c"],
            "final_rows": owner.rows("SELECT count() AS c FROM telemetry.query_log_v" + cond + _LIMITS, p)[0]["c"],
            "initial_queries": owner.rows("SELECT countIf(is_initial_query = 1 AND type != 'QueryStart') AS c"
                                          " FROM telemetry.query_log_v" + cond + _LIMITS, p)[0]["c"],
            "events": owner.rows(
                "SELECT source_display_name AS node, query_id, type, toUnixTimestamp64Micro(event_time_microseconds) AS t,"
                " is_initial_query, collect_path"
                " FROM telemetry.query_log_v" + cond + " ORDER BY t LIMIT 5000" + _LIMITS, p),
        }
    return out
