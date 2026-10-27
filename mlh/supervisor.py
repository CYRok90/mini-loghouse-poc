"""Background supervisor: observes RMVs, keeps status/cursors, backfills gaps.

It never collects steady-state data itself (that is the RMVs' job). It only:
  1. reads local system.view_refreshes and persists per-source status to SQLite
     (view_refreshes resets on restart, SQLite keeps history),
  2. advances the query_log coverage cursor and records gaps,
  3. runs explicit backfill for pending gaps, one bounded chunk per tick,
  4. retries RMV creation for nodes whose source was unreachable at setup.
Its lifetime is the backend process, independent of any browser or HTTP request.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import provisioner, schema
from .chdb_owner import ChdbOwner
from .errors import classify_exception
from .registry import Registry
from .settings import Settings
from .status import advance_cursor, evaluate_source, status_changed

log = logging.getLogger(__name__)

REFRESH_SQL = (
    "SELECT view, status, toUnixTimestamp(last_success_time) AS last_success_ts, "
    "toUnixTimestamp(last_refresh_time) AS last_refresh_ts, exception, retry "
    "FROM system.view_refreshes WHERE database = {db:String}"
)
MAX_GAP_ATTEMPTS = 10


class Supervisor:
    def __init__(self, owner: ChdbOwner, registry: Registry, settings: Settings) -> None:
        self._owner, self._registry, self._settings = owner, registry, settings
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="supervisor", daemon=True)
        self._retry_after: dict[str, float] = {}

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick(time.time())
            except Exception as exc:  # noqa: BLE001 - keep supervising; raw text may contain SQL, log category only
                info = classify_exception(exc)
                log.error("supervisor tick failed: %s %s %s", type(exc).__name__, info.category, list(info.codes))
            self._stop.wait(self._settings.supervisor_poll_seconds)

    def tick(self, now: float) -> None:
        refreshes = {r["view"]: r for r in self._owner.rows(REFRESH_SQL, {"db": schema.DB})}
        for node in self._registry.nodes():
            self._maybe_retry(node, now)
            self._observe(node, "metrics", refreshes.get(schema.metrics_mv(node["id"])), now)
            self._observe(node, "query_log", refreshes.get(schema.query_log_mv(node["id"])), now)
        self._backfill_one(now)

    def _maybe_retry(self, node: dict, now: float) -> None:
        if node["phase"] != "retrying" or now < self._retry_after.get(node["id"], 0):
            return
        if not provisioner.retry(self._owner, self._registry, self._settings, node):
            self._retry_after[node["id"]] = now + 4 * self._settings.refresh_seconds

    def _observe(self, node: dict, source: str, refresh: dict | None, now: float) -> None:
        persisted = self._registry.source_status(node["id"], source)
        status = evaluate_source(node, source, refresh, persisted, now, self._settings)
        if status_changed(persisted, status):
            self._registry.upsert_source_status(status)
        success = (refresh or {}).get("last_success_ts")
        previous_success = (persisted or {}).get("last_success_at") or 0
        if source == "query_log" and success and success > previous_success:
            self._advance(node, int(success), now)

    def _advance(self, node: dict, success_ts: int, now: float) -> None:
        node_id = node["id"]
        spec = json.loads(node["rmv_spec"]) if node.get("rmv_spec") else None
        new_cursor, gaps = advance_cursor(self._registry.cursor(node_id), success_ts, now, self._settings, spec)
        for gap in gaps:
            self._registry.add_gap(node_id, gap.lo, gap.hi, gap.state, gap.reason)
            log.info("node %s gap %s [%s, %s) %s", node_id, gap.state, gap.lo, gap.hi, gap.reason)
        if new_cursor:
            self._registry.save_cursor(node_id, *new_cursor)

    def _backfill_one(self, now: float) -> None:
        nodes = {n["id"]: n for n in self._registry.nodes()}
        for gap in self._registry.gaps(list(nodes), ("pending",)):
            status = self._registry.source_status(gap["node_id"], "query_log") or {}
            backoff = min(300, 5 * 2 ** int(gap["attempts"]))
            if status.get("state") != "collecting" or (gap["attempts"] and now - gap["updated_at"] < backoff):
                continue
            with provisioner.LIFECYCLE:  # never backfill a node that is being deleted
                if self._registry.cluster(nodes[gap["node_id"]]["cluster_id"]) is not None:
                    self._run_chunk(gap, nodes[gap["node_id"]])
            return  # one chunk per tick keeps the shared chDB executor responsive

    def _run_chunk(self, gap: dict[str, Any], node: dict) -> None:
        cluster = self._registry.cluster(node["cluster_id"])
        if cluster is None:
            return
        lo = int(gap["next_lo"])
        hi = min(int(gap["hi"]), lo + self._settings.backfill_chunk_seconds)
        params = {"cid": node["cluster_id"], "nid": node["id"], "lo": lo, "hi": hi}
        try:
            self._owner.execute(schema.backfill_sql(self._settings, node["id"], bool(cluster["tls"])), params)
        except Exception as exc:  # noqa: BLE001
            attempts = int(gap["attempts"]) + 1
            state = "failed" if attempts >= MAX_GAP_ATTEMPTS else "pending"
            self._registry.update_gap(gap["id"], lo, state, attempts)
            log.warning("backfill node %s [%s,%s) failed: %s", node["id"], lo, hi, classify_exception(exc).category)
            return
        done = hi >= int(gap["hi"])
        self._registry.update_gap(gap["id"], int(gap["hi"]) if done else hi, "backfilled" if done else "pending", 0)
