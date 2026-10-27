"""chDB telemetry schema and per-node collection DDL (RMV).

Collection path (the original hypothesis under test):
  per node: NC  mlh_<nid>            host/port/user/password (encrypted storage)
            RMV telemetry.mv_m_<nid> REFRESH EVERY R  APPEND -> metrics_raw
            RMV telemetry.mv_q_<nid> REFRESH EVERY R  APPEND -> query_log_raw
query_log RMV re-reads the fixed window [now()-lag-W, now()-lag) every refresh
(W = refresh + overlap). now() is evaluated on chDB and pushed to the source as
a constant (Day-0 evidence). Duplicates from the overlap are expected in raw
and removed by ReplacingMergeTree identity + FINAL in query_log_v.
Downtime longer than the overlap is NOT covered by the RMV; the supervisor
records it as a gap and backfills it explicitly (backfill_sql).
"""

from __future__ import annotations

from .settings import Settings
from .sqlsafe import identifier, literal

DB = "telemetry"

QUERY_LOG_COLUMNS = (
    "event_date", "event_time", "event_time_microseconds", "query_start_time_microseconds",
    "type", "query_id", "initial_query_id", "is_initial_query", "query_kind", "user",
    "query_duration_ms", "read_rows", "read_bytes", "result_rows", "memory_usage",
    "exception_code", "exception", "query", "normalized_query_hash", "log_comment",
)

BOOTSTRAP = (
    f"CREATE DATABASE IF NOT EXISTS {DB} ENGINE = Atomic",
    f"""CREATE TABLE IF NOT EXISTS {DB}.metrics_raw (
        cluster_id LowCardinality(String), node_id LowCardinality(String),
        source_display_name LowCardinality(String), collected_at DateTime64(3, 'UTC'),
        metric LowCardinality(String), value Int64
    ) ENGINE = MergeTree
    PARTITION BY toYYYYMMDD(collected_at)
    ORDER BY (cluster_id, node_id, metric, collected_at)
    TTL toDate(collected_at) + INTERVAL 3 DAY""",
    f"""CREATE TABLE IF NOT EXISTS {DB}.query_log_raw (
        cluster_id LowCardinality(String), node_id LowCardinality(String),
        source_display_name LowCardinality(String),
        event_date Date, event_time DateTime('UTC'), event_time_microseconds DateTime64(6, 'UTC'),
        query_start_time_microseconds DateTime64(6, 'UTC'),
        type LowCardinality(String), query_id String, initial_query_id String, is_initial_query UInt8,
        query_kind LowCardinality(String), user LowCardinality(String),
        query_duration_ms UInt64, read_rows UInt64, read_bytes UInt64, result_rows UInt64, memory_usage Int64,
        exception_code Int32, exception String, query String, normalized_query_hash UInt64,
        collected_at DateTime64(3, 'UTC'), collect_path LowCardinality(String)
    ) ENGINE = ReplacingMergeTree(collected_at)
    PARTITION BY toYYYYMMDD(event_time)
    ORDER BY (cluster_id, node_id, event_time_microseconds, type, query_id, query_start_time_microseconds)
    TTL event_date + INTERVAL 7 DAY""",
    # Every dashboard query reads through this deduplicated view.
    f"CREATE VIEW IF NOT EXISTS {DB}.query_log_v AS SELECT * FROM {DB}.query_log_raw FINAL",
)

_REMOTE_SETTINGS = (
    "connect_timeout_with_failover_ms = 2000, connect_timeout_with_failover_secure_ms = 2000, "
    "connections_with_failover_max_tries = 1, receive_timeout = 15, send_timeout = 15, "
    "max_execution_time = 30"
)

# Source-side projection. exception/query are truncated at the source.
_QUERY_LOG_SELECT = (
    "event_date, event_time, event_time_microseconds, query_start_time_microseconds, "
    "toString(type) AS type, query_id, initial_query_id, is_initial_query, query_kind, user, "
    "query_duration_ms, read_rows, read_bytes, result_rows, memory_usage, exception_code, "
    "substring(exception, 1, 1000) AS exception, substring(query, 1, 4000) AS query, normalized_query_hash"
)


# Bump when the RMV SELECT changes; stored per node so a restart with different
# settings or SQL recreates the RMVs and the cursor uses the spec that produced the data.
RMV_VERSION = 2


def rmv_spec(settings: Settings) -> dict:
    return {"v": RMV_VERSION, "refresh": settings.refresh_seconds, "lag": settings.safety_lag_seconds,
            "window": settings.query_window_seconds}


def nc_name(node_id: str) -> str:
    return identifier(f"mlh_{node_id}")


def metrics_mv(node_id: str) -> str:
    return identifier(f"mv_m_{node_id}")


def query_log_mv(node_id: str) -> str:
    return identifier(f"mv_q_{node_id}")


def remote_fn(tls: bool) -> str:
    return "remoteSecure" if tls else "remote"


def create_nc_sql(node_id: str, host: str, port: int, username: str, password: str) -> str:
    # CREATE NAMED COLLECTION does not accept query parameters -> validated literals.
    return (
        f"CREATE NAMED COLLECTION IF NOT EXISTS {nc_name(node_id)} AS "
        f"host = {literal(host)}, port = {int(port)}, user = {literal(username)}, password = {literal(password)}"
    )


def drop_nc_sql(node_id: str) -> str:
    return f"DROP NAMED COLLECTION IF EXISTS {nc_name(node_id)}"


def metrics_mv_sql(settings: Settings, cluster_id: str, node_id: str, tls: bool) -> tuple[str, dict]:
    sql = (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {DB}.{metrics_mv(node_id)} "
        f"REFRESH EVERY {int(settings.refresh_seconds)} SECOND APPEND TO {DB}.metrics_raw AS "
        "SELECT {cid:String} AS cluster_id, {nid:String} AS node_id, displayName() AS source_display_name, "
        "now64(3, 'UTC') AS collected_at, metric, value "
        f"FROM {remote_fn(tls)}({nc_name(node_id)}, database = 'system', table = 'metrics') "
        f"SETTINGS log_comment = {literal(settings.collector_tag)}, {_REMOTE_SETTINGS}"
    )
    return sql, {"cid": cluster_id, "nid": node_id}


def query_log_mv_sql(settings: Settings, cluster_id: str, node_id: str, tls: bool) -> tuple[str, dict]:
    lag, window = int(settings.safety_lag_seconds), int(settings.query_window_seconds)
    sql = (
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS {DB}.{query_log_mv(node_id)} "
        f"REFRESH EVERY {int(settings.refresh_seconds)} SECOND APPEND TO {DB}.query_log_raw AS "
        "SELECT {cid:String} AS cluster_id, {nid:String} AS node_id, displayName() AS source_display_name, "
        f"{_QUERY_LOG_SELECT}, now64(3, 'UTC') AS collected_at, 'rmv' AS collect_path "
        f"FROM {remote_fn(tls)}({nc_name(node_id)}, database = 'system', table = 'query_log') "
        # event_date is in the SOURCE server time zone; widen by one day so a time-zone difference
        # can never drop rows (event_time, an absolute instant, does the exact filtering).
        f"WHERE event_date >= toDate(now() - INTERVAL {lag + window} SECOND) - 1 "
        f"AND event_time >= now() - INTERVAL {lag + window} SECOND "
        f"AND event_time < now() - INTERVAL {lag} SECOND "
        f"AND log_comment != {literal(settings.collector_tag)} "
        f"SETTINGS log_comment = {literal(settings.collector_tag)}, {_REMOTE_SETTINGS}"
    )
    return sql, {"cid": cluster_id, "nid": node_id}


def backfill_sql(settings: Settings, node_id: str, tls: bool) -> str:
    """Explicit [lo, hi) re-read used for gaps the RMV window cannot cover."""
    return (
        f"INSERT INTO {DB}.query_log_raw "
        "SELECT {cid:String} AS cluster_id, {nid:String} AS node_id, displayName() AS source_display_name, "
        f"{_QUERY_LOG_SELECT}, now64(3, 'UTC') AS collected_at, 'backfill' AS collect_path "
        f"FROM {remote_fn(tls)}({nc_name(node_id)}, database = 'system', table = 'query_log') "
        "WHERE event_date >= toDate(toDateTime({lo:UInt32})) - 1 AND event_date <= toDate(toDateTime({hi:UInt32})) + 1 "
        "AND event_time >= toDateTime({lo:UInt32}) AND event_time < toDateTime({hi:UInt32}) "
        f"AND log_comment != {literal(settings.collector_tag)} "
        f"SETTINGS log_comment = {literal(settings.collector_tag)}, {_REMOTE_SETTINGS}"
    )


def drop_mv_sql(name: str) -> str:
    return f"DROP VIEW IF EXISTS {DB}.{identifier(name)}"
