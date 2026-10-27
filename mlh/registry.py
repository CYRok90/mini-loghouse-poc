"""Registration metadata, collection cursors, gaps and status in SQLite.

SQLite holds everything except telemetry and secrets: the app can still show
registrations, last status and gap history when chDB cannot be opened (for
example in key recovery mode). Passwords are never stored here.
All timestamps are epoch seconds (UTC).
"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clusters (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, host TEXT NOT NULL, port INTEGER NOT NULL,
    tls INTEGER NOT NULL, username TEXT NOT NULL, phase TEXT NOT NULL,
    discovered_cluster TEXT, candidate_count INTEGER NOT NULL DEFAULT 0,
    server_version TEXT, last_check_at REAL, last_check_ok INTEGER, last_check_category TEXT,
    created_at REAL NOT NULL,
    UNIQUE (host, port, username)
);
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL REFERENCES clusters(id),
    host TEXT NOT NULL, port INTEGER NOT NULL, display_name TEXT, server_uuid TEXT,
    server_version TEXT, phase TEXT NOT NULL, last_error_category TEXT, last_error_message TEXT,
    created_at REAL NOT NULL,
    UNIQUE (host, port)
);
CREATE UNIQUE INDEX IF NOT EXISTS nodes_server_uuid ON nodes(server_uuid) WHERE server_uuid IS NOT NULL;
CREATE TABLE IF NOT EXISTS source_status (
    node_id TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL,
    last_attempt_at REAL, last_success_at REAL, last_error_category TEXT, last_error_message TEXT,
    last_error_codes TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
    PRIMARY KEY (node_id, source)
);
CREATE TABLE IF NOT EXISTS cursors (
    node_id TEXT PRIMARY KEY, first_lo INTEGER NOT NULL, committed_hi INTEGER NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, lo INTEGER NOT NULL, hi INTEGER NOT NULL,
    next_lo INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, node_id TEXT NOT NULL, source TEXT NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL, detail TEXT, at REAL NOT NULL
);
"""

DuplicateError = sqlite3.IntegrityError


class Registry:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        found = self.rows(sql, args)
        return found[0] if found else None

    # --- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, value))

    # --- clusters / nodes ---------------------------------------------------
    def insert_cluster(self, cluster: dict[str, Any], nodes: list[dict[str, Any]]) -> None:
        """Insert a cluster and its nodes atomically; raises DuplicateError on re-registration."""
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO clusters(id, name, host, port, tls, username, phase, discovered_cluster, candidate_count,"
                " server_version, last_check_at, last_check_ok, last_check_category, created_at)"
                " VALUES (:id, :name, :host, :port, :tls, :username, 'provisioning', :discovered_cluster,"
                " :candidate_count, :server_version, :now, 1, NULL, :now)",
                {**cluster, "now": now},
            )
            for node in nodes:
                c.execute(
                    "INSERT INTO nodes(id, cluster_id, host, port, display_name, server_uuid, server_version, phase, created_at)"
                    " VALUES (:id, :cluster_id, :host, :port, :display_name, :server_uuid, :server_version, 'pending', :now)",
                    {**node, "now": now},
                )

    def clusters(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM clusters WHERE phase != 'deleted' ORDER BY created_at")

    def cluster(self, cluster_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM clusters WHERE id = ? AND phase != 'deleted'", (cluster_id,))

    def nodes(self, cluster_id: str | None = None) -> list[dict[str, Any]]:
        if cluster_id is None:
            return self.rows("SELECT n.* FROM nodes n JOIN clusters c ON c.id = n.cluster_id"
                             " WHERE c.phase != 'deleted' ORDER BY n.created_at")
        return self.rows("SELECT * FROM nodes WHERE cluster_id = ? ORDER BY created_at, host, port", (cluster_id,))

    def set_cluster_phase(self, cluster_id: str, phase: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE clusters SET phase = ? WHERE id = ?", (phase, cluster_id))

    def set_node_phase(self, node_id: str, phase: str, category: str | None = None, message: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE nodes SET phase = ?, last_error_category = ?, last_error_message = ? WHERE id = ?",
                      (phase, category, message, node_id))

    def record_check(self, cluster_id: str, ok: bool, category: str | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE clusters SET last_check_at = ?, last_check_ok = ?, last_check_category = ? WHERE id = ?",
                      (time.time(), int(ok), category, cluster_id))

    # --- status -------------------------------------------------------------
    def source_status(self, node_id: str, source: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM source_status WHERE node_id = ? AND source = ?", (node_id, source))

    def all_source_status(self) -> list[dict[str, Any]]:
        return self.rows("SELECT * FROM source_status")

    def upsert_source_status(self, status: dict[str, Any]) -> None:
        """Write the latest status; append a transition row only when the state changes."""
        previous = self.source_status(status["node_id"], status["source"])
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO source_status(node_id, source, state, last_attempt_at, last_success_at,"
                " last_error_category, last_error_message, last_error_codes, consecutive_failures, updated_at)"
                " VALUES (:node_id, :source, :state, :last_attempt_at, :last_success_at, :last_error_category,"
                " :last_error_message, :last_error_codes, :consecutive_failures, :now)"
                " ON CONFLICT(node_id, source) DO UPDATE SET state=excluded.state,"
                " last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at,"
                " last_error_category=excluded.last_error_category, last_error_message=excluded.last_error_message,"
                " last_error_codes=excluded.last_error_codes, consecutive_failures=excluded.consecutive_failures,"
                " updated_at=excluded.updated_at",
                {**status, "now": now},
            )
            prev_state = previous["state"] if previous else None
            if prev_state != status["state"]:
                c.execute(
                    "INSERT INTO transitions(node_id, source, from_state, to_state, detail, at) VALUES (?, ?, ?, ?, ?, ?)",
                    (status["node_id"], status["source"], prev_state, status["state"], status.get("last_error_category"), now),
                )

    def transitions(self, node_ids: list[str], limit: int = 50) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        marks = ",".join("?" for _ in node_ids)
        return self.rows(f"SELECT * FROM transitions WHERE node_id IN ({marks}) ORDER BY at DESC LIMIT ?",
                         (*node_ids, limit))

    # --- cursors / gaps -----------------------------------------------------
    def cursor(self, node_id: str) -> dict[str, Any] | None:
        return self.one("SELECT * FROM cursors WHERE node_id = ?", (node_id,))

    def save_cursor(self, node_id: str, first_lo: int, committed_hi: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO cursors(node_id, first_lo, committed_hi, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(node_id) DO UPDATE SET committed_hi = max(cursors.committed_hi, excluded.committed_hi),"
                " updated_at = excluded.updated_at",
                (node_id, first_lo, committed_hi, time.time()),
            )

    def add_gap(self, node_id: str, lo: int, hi: int, state: str, reason: str) -> None:
        if hi <= lo:
            return
        now = time.time()
        with self.tx() as c:
            c.execute("INSERT INTO gaps(node_id, lo, hi, next_lo, state, reason, created_at, updated_at)"
                      " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (node_id, lo, hi, lo, state, reason, now, now))

    def gaps(self, node_ids: list[str], states: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        if not node_ids:
            return []
        marks = ",".join("?" for _ in node_ids)
        sql = f"SELECT * FROM gaps WHERE node_id IN ({marks})"
        args: tuple = tuple(node_ids)
        if states:
            sql += f" AND state IN ({','.join('?' for _ in states)})"
            args += states
        return self.rows(sql + " ORDER BY lo", args)

    def update_gap(self, gap_id: int, next_lo: int, state: str, attempts: int) -> None:
        """Progress a gap; the original [lo, hi) is kept for coverage reporting."""
        with self.tx() as c:
            c.execute("UPDATE gaps SET next_lo = ?, state = ?, attempts = ?, updated_at = ? WHERE id = ?",
                      (next_lo, state, attempts, time.time(), gap_id))
