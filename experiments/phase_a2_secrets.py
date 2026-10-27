"""Phase A2: where do remote() credentials end up inside a chDB data path?

Runs without any reachable ClickHouse server: every remote target is a closed
or unroutable address, so it also records how connection failures surface.

  plain    : inline password in RMV DDL + default named collection storage
  enc      : named collection in local_encrypted storage (key from env)
  reopen   : reopen the encrypted path (with/without key) in a new process
  errors   : error codes/messages for refused / timeout / DNS failures

Usage:
  python experiments/phase_a2_secrets.py plain  <db_path>
  python experiments/phase_a2_secrets.py enc    <db_path> <config_xml>
  python experiments/phase_a2_secrets.py reopen <db_path> <config_xml>
  python experiments/phase_a2_secrets.py errors <db_path>
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from chdb import session as chs

PLAIN_MARK = "SECRET_INLINE_456"
NC_PLAIN_MARK = "SECRET_NCPLAIN_123"
NC_ENC_MARK = "SECRET_NCENC_789"


def rows(sess: chs.Session, sql: str) -> list[dict]:
    out = sess.query(sql, "JSONEachRow").bytes().decode()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def try_query(sess: chs.Session, sql: str) -> str:
    try:
        return "OK " + sess.query(sql, "CSV").bytes().decode()[:200]
    except Exception as exc:  # noqa: BLE001 - experiment records raw error class/text
        return f"ERR {type(exc).__name__}: {str(exc)[:400]}"


def grep_marks(root: str, marks: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {m: [] for m in marks}
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for mark in marks:
            if mark.encode() in data:
                found[mark].append(str(path.relative_to(root)))
    return found


def setup_target(sess: chs.Session) -> None:
    sess.query("CREATE DATABASE IF NOT EXISTS a2 ENGINE = Atomic")
    sess.query(
        "CREATE TABLE IF NOT EXISTS a2.metrics_sink "
        "(collected_at DateTime64(3,'UTC'), metric String, value Int64) "
        "ENGINE = MergeTree ORDER BY collected_at"
    )


def plain(path: str) -> None:
    sess = chs.Session(path)
    setup_target(sess)
    mv_result = try_query(
        sess,
        "CREATE MATERIALIZED VIEW IF NOT EXISTS a2.inline_mv REFRESH EVERY 1 HOUR APPEND TO a2.metrics_sink AS "
        "SELECT now64(3,'UTC') AS collected_at, metric, value "
        f"FROM remote('127.0.0.1:1', system.metrics, 'u', '{PLAIN_MARK}')",
    )
    print("inline mv create (unreachable source):", mv_result.splitlines()[0], "leak=", PLAIN_MARK in mv_result)
    print("nc create:", try_query(sess, f"CREATE NAMED COLLECTION nc_plain AS host='127.0.0.1', port=1, user='u', password='{NC_PLAIN_MARK}'"))
    time.sleep(2)
    print("system.tables create_table_query:", rows(sess, "SELECT create_table_query FROM system.tables WHERE database='a2' AND name='inline_mv'"))
    print("named_collections:", try_query(sess, "SELECT name, collection FROM system.named_collections"))
    print("view_refreshes:", rows(sess, "SELECT view, status, exception FROM system.view_refreshes WHERE database='a2'"))
    sess.close()
    print("plaintext marks on disk:", grep_marks(path, [PLAIN_MARK, NC_PLAIN_MARK]))


def enc(path: str, config: str) -> None:
    sess = chs.Session(f"file:{path}?config-file={config}")
    setup_target(sess)
    print("nc create:", try_query(sess, f"CREATE NAMED COLLECTION nc_enc AS host='127.0.0.1', port=1, user='u', password='{NC_ENC_MARK}'"))
    print(
        "mv create:",
        try_query(
            sess,
            "CREATE MATERIALIZED VIEW IF NOT EXISTS a2.nc_mv REFRESH EVERY 1 HOUR APPEND TO a2.metrics_sink AS "
            "SELECT now64(3,'UTC') AS collected_at, metric, value "
            "FROM remote(nc_enc, database='system', table='metrics')",
        ).splitlines()[0],
    )
    time.sleep(2)
    print("system.tables create_table_query:", rows(sess, "SELECT create_table_query FROM system.tables WHERE database='a2' AND name='nc_mv'"))
    print("named_collections:", try_query(sess, "SELECT name, collection FROM system.named_collections"))
    print("view_refreshes:", rows(sess, "SELECT view, status, exception FROM system.view_refreshes WHERE database='a2'"))
    sess.close()
    print("plaintext marks on disk:", grep_marks(path, [NC_ENC_MARK]))
    print("files:", sorted(str(p.relative_to(path)) for p in Path(path).rglob("*") if "named" in str(p)))


def reopen(path: str, config: str) -> None:
    print("key present in env:", bool(os.environ.get("MLH_NC_KEY_HEX")))
    try:
        sess = chs.Session(f"file:{path}?config-file={config}")
    except Exception as exc:  # noqa: BLE001
        print("open failed:", type(exc).__name__, str(exc)[:300])
        return
    print("named_collections:", try_query(sess, "SELECT name FROM system.named_collections"))
    print("mv still defined:", try_query(sess, "SELECT name FROM system.tables WHERE database='a2' AND name='nc_mv'"))
    time.sleep(2)
    print("view_refreshes:", try_query(sess, "SELECT view, status, substring(exception,1,200) FROM system.view_refreshes WHERE database='a2'"))
    sess.close()


def errors(path: str) -> None:
    sess = chs.Session(path)
    cases = {
        "refused": "SELECT 1 FROM remote('127.0.0.1:1', system.one, 'u', 'pw_should_not_leak') SETTINGS connect_timeout_with_failover_ms=1000",
        "timeout": "SELECT 1 FROM remote('10.255.255.1:9000', system.one, 'u', 'pw_should_not_leak') SETTINGS connect_timeout_with_failover_ms=1000, connect_timeout_with_failover_secure_ms=1000",
        "dns": "SELECT 1 FROM remote('no-such-host.invalid:9000', system.one, 'u', 'pw_should_not_leak') SETTINGS connect_timeout_with_failover_ms=1000",
        "secure_refused": "SELECT 1 FROM remoteSecure('127.0.0.1:1', system.one, 'u', 'pw_should_not_leak') SETTINGS connect_timeout_with_failover_secure_ms=1000",
    }
    for name, sql in cases.items():
        started = time.monotonic()
        result = try_query(sess, sql)
        print(f"[{name}] {time.monotonic() - started:.1f}s leak={'pw_should_not_leak' in result} {result}")
    sess.close()


def main(argv: list[str]) -> int:
    cmd, path = argv[1], argv[2]
    handlers = {
        "plain": lambda: plain(path),
        "enc": lambda: enc(path, argv[3]),
        "reopen": lambda: reopen(path, argv[3]),
        "errors": lambda: errors(path),
    }
    if cmd not in handlers:
        print(__doc__)
        return 2
    handlers[cmd]()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
