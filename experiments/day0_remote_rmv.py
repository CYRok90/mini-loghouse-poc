"""Day-0 spike: chDB 4.4.0 (engine 26.9.2.1) -> ClickHouse 26.8 test nodes.

Requires `scripts/testenv.py start`. Each step prints one labelled line.
  steps: probe | errors | roundtrip | rmv | inspect | dropnc | reopen
Usage: python experiments/day0_remote_rmv.py <step> <db_path>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from chdb import session as chs

ROOT = Path(__file__).resolve().parent.parent
TESTENV = ROOT / "var" / "testenv"
SECRETS = json.loads((TESTENV / "secrets.json").read_text())
TAG = "mlh-day0"
RT_PASSWORD = "pa'ss\\wo{rd}\"ü한글 %s ;--"


def literal(value: str) -> str:
    if any(ord(ch) < 0x20 for ch in value):
        raise ValueError("control characters are not allowed")
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def config_path(db_path: str) -> str:
    cfg = Path(db_path).with_suffix(".xml")
    cfg.write_text(
        f"""<clickhouse>
    <named_collections_storage>
        <type>local_encrypted</type><algorithm>aes_128_ctr</algorithm>
        <key_hex from_env="MLH_NC_KEY_HEX"/>
    </named_collections_storage>
    <openSSL><client>
        <caConfig>{TESTENV / 'certs' / 'ca.pem'}</caConfig>
        <verificationMode>strict</verificationMode>
        <loadDefaultCAFile>false</loadDefaultCAFile>
    </client></openSSL>
</clickhouse>
"""
    )
    return str(cfg)


def open_session(db_path: str) -> chs.Session:
    return chs.Session(f"file:{db_path}?config-file={config_path(db_path)}")


def q(sess: chs.Session, sql: str, params: dict | None = None) -> str:
    try:
        return "OK " + sess.query(sql, "TSV", params=params).bytes().decode().strip()
    except Exception as exc:  # noqa: BLE001 - spike records the raw error text
        text = str(exc)
        leak = any(p in text for p in SECRETS.values()) or RT_PASSWORD in text
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return f"ERR(leak={leak}) " + " | ".join(lines[-2:])[:500]


PROBE_SETTINGS = (
    "SETTINGS connect_timeout_with_failover_ms=1000, connect_timeout_with_failover_secure_ms=1000, "
    "connections_with_failover_max_tries=1, receive_timeout=5, send_timeout=5, max_execution_time=5"
)


def probe(sess: chs.Session) -> None:
    p = {"addr": "127.0.0.1:19401", "u": "mlh_collector", "p": SECRETS["mlh_collector"]}
    print("[local displayName/hostName]", q(sess, "SELECT displayName(), hostName()"))
    print("[probe a1 outer-fn]", q(sess, f"SELECT version(), displayName(), serverUUID(), hostName(), dummy FROM remoteSecure({{addr:String}}, system.one, {{u:String}}, {{p:String}}) {PROBE_SETTINGS}", p))
    print("[probe a1 view()]", q(sess, f"SELECT * FROM remoteSecure({{addr:String}}, view(SELECT version() v, displayName() d, toString(serverUUID()) u, timezone() tz, toString(now64(3)) n), {{u:String}}, {{p:String}}) {PROBE_SETTINGS}", p))
    print("[probe a1 clusters]", q(sess, f"SELECT cluster, shard_num, replica_num, host_name, port, is_local FROM remoteSecure({{addr:String}}, system.clusters, {{u:String}}, {{p:String}}) {PROBE_SETTINGS}", p))
    print("[probe b1 plain]", q(sess, f"SELECT displayName() FROM remote('127.0.0.1:19003', system.one, {{u:String}}, {{p:String}}) {PROBE_SETTINGS}", p))


def errors(sess: chs.Session) -> None:
    c, n = SECRETS["mlh_collector"], SECRETS["mlh_noperm"]
    cases = {
        "tls_to_plain_port": ("remoteSecure('127.0.0.1:19003', system.one, 'mlh_collector', {p:String})", c),
        "plain_to_tls_port": ("remote('127.0.0.1:19401', system.one, 'mlh_collector', {p:String})", c),
        "http_port_as_native": ("remote('127.0.0.1:18003', system.one, 'mlh_collector', {p:String})", c),
        "wrong_password": ("remote('127.0.0.1:19003', system.one, 'mlh_collector', {p:String})", "wrong-" + c),
        "noperm_query_log": ("remote('127.0.0.1:19003', system.query_log, 'mlh_noperm', {p:String})", n),
        "noperm_metrics": ("remote('127.0.0.1:19003', system.metrics, 'mlh_noperm', {p:String})", n),
        "missing_table": ("remote('127.0.0.1:19003', system.no_such_table, 'mlh_collector', {p:String})", c),
        "collector_query_log": ("remote('127.0.0.1:19003', system.query_log, 'mlh_collector', {p:String})", c),
    }
    for name, (src, pw) in cases.items():
        started = time.monotonic()
        result = q(sess, f"SELECT count() FROM {src} {PROBE_SETTINGS}", {"p": pw})
        print(f"[err {name}] {time.monotonic() - started:.2f}s {result}")


def roundtrip(sess: chs.Session) -> None:
    from subprocess import run

    admin_sql = f"CREATE USER OR REPLACE mlh_rt IDENTIFIED WITH sha256_password BY {literal(RT_PASSWORD)}"
    for stmt in (admin_sql, "GRANT SELECT ON system.one TO mlh_rt"):
        run([sys.executable, str(ROOT / "scripts" / "testenv.py"), "sql", "b1", stmt], check=True, capture_output=True)
    print("[nc create]", q(sess, f"CREATE NAMED COLLECTION IF NOT EXISTS nc_rt AS host='127.0.0.1', port=19003, user='mlh_rt', password={literal(RT_PASSWORD)}"))
    print("[nc login]", q(sess, f"SELECT displayName() FROM remote(nc_rt, database='system', table='one') {PROBE_SETTINGS}"))
    print("[param login]", q(sess, f"SELECT displayName() FROM remote('127.0.0.1:19003', system.one, 'mlh_rt', {{p:String}}) {PROBE_SETTINGS}", {"p": RT_PASSWORD}))


def rmv(sess: chs.Session) -> None:
    sess.query("CREATE DATABASE IF NOT EXISTS telemetry ENGINE = Atomic")
    sess.query(
        "CREATE TABLE IF NOT EXISTS telemetry.metrics_raw (cluster_id String, node_id String, source_display_name String, "
        "collected_at DateTime64(3,'UTC'), metric LowCardinality(String), value Int64) ENGINE = MergeTree ORDER BY (cluster_id, node_id, metric, collected_at)"
    )
    sess.query(
        "CREATE TABLE IF NOT EXISTS telemetry.query_log_raw (cluster_id String, node_id String, source_display_name String, "
        "event_time DateTime, event_time_microseconds DateTime64(6), query_start_time_microseconds DateTime64(6), type String, "
        "query_id String, is_initial_query UInt8, query_duration_ms UInt64, log_comment String, collected_at DateTime64(3,'UTC')) "
        "ENGINE = ReplacingMergeTree(collected_at) PARTITION BY toYYYYMMDD(event_time) "
        "ORDER BY (cluster_id, node_id, event_time_microseconds, type, query_id, query_start_time_microseconds)"
    )
    pw = SECRETS["mlh_collector"]
    print("[nc a1]", q(sess, f"CREATE NAMED COLLECTION IF NOT EXISTS nc_day0_a1 AS host='127.0.0.1', port=19401, user='mlh_collector', password={literal(pw)}"))
    print("[rmv metrics]", q(sess,
        "CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.day0_metrics_mv REFRESH EVERY 5 SECOND APPEND TO telemetry.metrics_raw AS "
        "SELECT {cid:String} AS cluster_id, {nid:String} AS node_id, displayName() AS source_display_name, now64(3,'UTC') AS collected_at, metric, value "
        f"FROM remoteSecure(nc_day0_a1, database='system', table='metrics') SETTINGS log_comment='{TAG}'",
        {"cid": "c_day0", "nid": "n_a1"}))
    print("[rmv query_log]", q(sess,
        "CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry.day0_qlog_mv REFRESH EVERY 5 SECOND APPEND TO telemetry.query_log_raw AS "
        "SELECT {cid:String} AS cluster_id, {nid:String} AS node_id, displayName() AS source_display_name, event_time, event_time_microseconds, "
        "query_start_time_microseconds, toString(type) AS type, query_id, is_initial_query, query_duration_ms, log_comment, now64(3,'UTC') AS collected_at "
        "FROM remoteSecure(nc_day0_a1, database='system', table='query_log') "
        f"WHERE event_time >= now() - INTERVAL 40 SECOND AND event_time < now() - INTERVAL 8 SECOND AND log_comment != '{TAG}' "
        f"SETTINGS log_comment='{TAG}'",
        {"cid": "c_day0", "nid": "n_a1"}))


def inspect(sess: chs.Session, db_path: str) -> None:
    print("[metrics rows]", q(sess, "SELECT count(), uniqExact(collected_at), groupUniqArray(source_display_name), toString(max(collected_at)) FROM telemetry.metrics_raw"))
    print("[metrics rows per refresh]", q(sess, "SELECT groupArray(c) FROM (SELECT collected_at, count() c FROM telemetry.metrics_raw GROUP BY collected_at ORDER BY collected_at)"))
    print("[qlog raw vs final]", q(sess, "SELECT (SELECT count() FROM telemetry.query_log_raw), (SELECT count() FROM telemetry.query_log_raw FINAL), (SELECT countIf(query_id LIKE 'day0w-%') FROM telemetry.query_log_raw FINAL)"))
    print("[qlog tagged events]", q(sess, "SELECT query_id, groupArray(type) FROM telemetry.query_log_raw FINAL WHERE query_id LIKE 'day0w-%' GROUP BY query_id ORDER BY query_id"))
    print("[view_refreshes]", q(sess, "SELECT view, status, toString(last_success_time), exception FROM system.view_refreshes WHERE database='telemetry'"))
    print("[ddl stored]", q(sess, "SELECT name, create_table_query FROM system.tables WHERE database='telemetry' AND engine='MaterializedView'"))
    print("[local system.query_log exists]", q(sess, "SELECT count() FROM system.tables WHERE database='system' AND name IN ('query_log','text_log')"))
    marks = list(SECRETS.values()) + [RT_PASSWORD]
    hits = []
    for path in Path(db_path).rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            hits += [f"{path.relative_to(db_path)}" for m in marks if m.encode() in data]
    print("[plaintext secrets in chdb path]", hits)


def dropnc(sess: chs.Session) -> None:
    print("[drop nc]", q(sess, "DROP NAMED COLLECTION nc_day0_a1"))
    time.sleep(6)
    print("[view_refreshes after drop]", q(sess, "SELECT view, status, exception FROM system.view_refreshes WHERE database='telemetry'"))


def reopen_report(sess: chs.Session) -> None:
    print("[after reopen: mvs]", q(sess, "SELECT name FROM system.tables WHERE database='telemetry' AND engine='MaterializedView'"))
    time.sleep(6)
    print("[after reopen: view_refreshes]", q(sess, "SELECT view, status, substring(exception, 1, 160) FROM system.view_refreshes WHERE database='telemetry'"))


def main(argv: list[str]) -> int:
    step, db_path = argv[1], argv[2]
    if "MLH_NC_KEY_HEX" not in os.environ:
        print("MLH_NC_KEY_HEX required")
        return 2
    sess = open_session(db_path)
    if step == "probe":
        probe(sess)
    elif step == "errors":
        errors(sess)
    elif step == "roundtrip":
        roundtrip(sess)
    elif step == "rmv":
        rmv(sess)
    elif step == "inspect":
        inspect(sess, db_path)
    elif step == "dropnc":
        dropnc(sess)
    elif step == "reopen":
        reopen_report(sess)
    sess.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
