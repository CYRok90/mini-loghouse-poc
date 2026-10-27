"""Phase A: minimal chDB Refreshable Materialized View proof.

Each step runs in its own process so "restart" means a real new process
opening the same persistent path.

  create  : persistent session, local target table, REFRESH EVERY ... APPEND MV
  idle    : keep the same connection open, send no SQL for N seconds, then count
            refreshes whose timestamps fall inside the idle window
  inspect : print rows, MV definition and system.view_refreshes

Usage:
  python experiments/phase_a_rmv.py create  <db_path>
  python experiments/phase_a_rmv.py idle    <db_path> <seconds>
  python experiments/phase_a_rmv.py inspect <db_path>
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from chdb import session as chs

INTERVAL_SECONDS = 5


def connect(path: str) -> chs.Session:
    return chs.Session(path)


def rows(sess: chs.Session, sql: str) -> list[dict]:
    out = sess.query(sql, "JSONEachRow").bytes().decode()
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def create(path: str) -> None:
    sess = connect(path)
    print("engine", rows(sess, "SELECT version() AS v")[0]["v"])
    print("databases", rows(sess, "SELECT name, engine FROM system.databases ORDER BY name"))
    sess.query("CREATE DATABASE IF NOT EXISTS phase_a ENGINE = Atomic")
    sess.query(
        "CREATE TABLE IF NOT EXISTS phase_a.ticks "
        "(refreshed_at DateTime64(3, 'UTC'), pid UInt32) "
        "ENGINE = MergeTree ORDER BY refreshed_at"
    )
    sess.query(
        f"CREATE MATERIALIZED VIEW IF NOT EXISTS phase_a.tick_mv "
        f"REFRESH EVERY {INTERVAL_SECONDS} SECOND APPEND TO phase_a.ticks "
        f"AS SELECT now64(3, 'UTC') AS refreshed_at, toUInt32(0) AS pid"
    )
    time.sleep(1)
    print("after create", rows(sess, "SELECT count() c, max(refreshed_at) m FROM phase_a.ticks"))
    sess.close()


def idle(path: str, seconds: int) -> None:
    sess = connect(path)
    opened = datetime.now(timezone.utc)
    before = rows(sess, "SELECT count() AS c, toString(max(refreshed_at)) AS m FROM phase_a.ticks")[0]
    print("opened", opened.isoformat(), "before", before)
    idle_start = datetime.now(timezone.utc)
    time.sleep(seconds)  # same connection, no SQL sent during this window
    idle_end = datetime.now(timezone.utc)
    inside = rows(
        sess,
        "SELECT toString(refreshed_at) AS t FROM phase_a.ticks "
        f"WHERE refreshed_at > parseDateTime64BestEffort('{idle_start.isoformat()}', 3) "
        f"AND refreshed_at <= parseDateTime64BestEffort('{idle_end.isoformat()}', 3) "
        "ORDER BY refreshed_at",
    )
    distinct = sorted({r["t"] for r in inside})
    print("idle window", idle_start.isoformat(), "->", idle_end.isoformat())
    print("refreshes inside idle window", len(distinct), distinct)
    print("verdict", "PASS" if len(distinct) >= 3 else "FAIL")
    sess.close()


def inspect(path: str) -> None:
    sess = connect(path)
    print("count", rows(sess, "SELECT count() c, toString(min(refreshed_at)) lo, toString(max(refreshed_at)) hi FROM phase_a.ticks"))
    print("mv", rows(sess, "SELECT create_table_query FROM system.tables WHERE database='phase_a' AND name='tick_mv'"))
    print(
        "view_refreshes",
        rows(
            sess,
            "SELECT view, status, toString(last_success_time) last_success, "
            "toString(next_refresh_time) next_refresh, exception "
            "FROM system.view_refreshes WHERE database='phase_a'",
        ),
    )
    sess.close()


def main(argv: list[str]) -> int:
    cmd, path = argv[1], argv[2]
    if cmd == "create":
        create(path)
    elif cmd == "idle":
        idle(path, int(argv[3]))
    elif cmd == "inspect":
        inspect(path)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
