"""Create / reconcile / delete per-node collection objects in chDB.

Order per node: NC (secret, local only) -> metrics RMV -> query_log RMV.
RMV creation fetches the remote table structure (Day-0: fails with 519 when
the source is unreachable), so a node whose RMVs could not be created stays in
phase 'retrying' and the supervisor retries without needing the password again
(it is already inside the encrypted NC).

Every lifecycle change (register, retry, delete, backfill of a node) runs under
LIFECYCLE so the supervisor can never recreate RMVs of a node being deleted.
Each node stores the RMV spec (SQL version, refresh, lag, window) that created
its RMVs; reconcile recreates RMVs whose spec differs from current settings, and
the cursor is always computed with the stored spec (Codex review P1-1).
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from . import schema
from .chdb_owner import ChdbOwner
from .errors import classify_exception
from .registry import Registry
from .settings import Settings

log = logging.getLogger(__name__)
LIFECYCLE = threading.RLock()


def _spec_json(settings: Settings) -> str:
    return json.dumps(schema.rmv_spec(settings), sort_keys=True)


def drop_rmvs(owner: ChdbOwner, node_id: str) -> None:
    owner.execute(schema.drop_mv_sql(schema.metrics_mv(node_id)))
    owner.execute(schema.drop_mv_sql(schema.query_log_mv(node_id)))


def create_rmvs(owner: ChdbOwner, registry: Registry, settings: Settings, node: dict[str, Any], tls: bool) -> None:
    for build in (schema.metrics_mv_sql, schema.query_log_mv_sql):
        sql, params = build(settings, node["cluster_id"], node["id"], tls)
        owner.execute(sql, params)
    registry.set_node_spec(node["id"], _spec_json(settings))


def _activate(owner: ChdbOwner, registry: Registry, settings: Settings, node: dict, tls: bool) -> bool:
    try:
        create_rmvs(owner, registry, settings, node, tls)
    except Exception as exc:  # noqa: BLE001 - classified; raw text may contain SQL
        info = classify_exception(exc)
        registry.set_node_phase(node["id"], "retrying", info.category, info.message)
        log.warning("node %s rmv creation failed: %s %s", node["id"], info.category, list(info.codes))
        return False
    registry.set_node_phase(node["id"], "active")
    return True


def provision(owner: ChdbOwner, registry: Registry, settings: Settings, cluster: dict, password: str) -> None:
    tls = bool(cluster["tls"])
    with LIFECYCLE:
        for node in registry.nodes(cluster["id"]):
            try:
                owner.execute(schema.create_nc_sql(node["id"], node["host"], node["port"], cluster["username"], password))
            except Exception as exc:  # noqa: BLE001
                info = classify_exception(exc)
                registry.set_node_phase(node["id"], "needs_credentials", info.category, "자격증명 저장에 실패했습니다.")
                continue
            _activate(owner, registry, settings, node, tls)
        registry.set_cluster_phase(cluster["id"], "active")


def existing_objects(owner: ChdbOwner) -> tuple[set[str], set[str]]:
    ncs = {r["name"] for r in owner.rows("SELECT name FROM system.named_collections")}
    mvs = {r["name"] for r in owner.rows(
        "SELECT name FROM system.tables WHERE database = {db:String} AND engine = 'MaterializedView'", {"db": schema.DB})}
    return ncs, mvs


def _reconcile_node(owner: ChdbOwner, registry: Registry, settings: Settings, node: dict, tls: bool,
                    ncs: set[str], mvs: set[str]) -> None:
    if schema.nc_name(node["id"]) not in ncs:
        registry.set_node_phase(node["id"], "needs_credentials", "nc_missing", "저장된 자격증명이 없습니다. 재입력이 필요합니다.")
        return
    wanted = {schema.metrics_mv(node["id"]), schema.query_log_mv(node["id"])}
    if node.get("rmv_spec") != _spec_json(settings) and wanted & mvs:
        log.info("node %s rmv spec changed %s -> %s; recreating", node["id"], node.get("rmv_spec"), _spec_json(settings))
        drop_rmvs(owner, node["id"])
        mvs = mvs - wanted
    if not wanted <= mvs:
        _activate(owner, registry, settings, node, tls)
    elif node["phase"] != "active":
        registry.set_node_phase(node["id"], "active")


def reconcile(owner: ChdbOwner, registry: Registry, settings: Settings) -> None:
    """Startup: compare SQLite registrations with chDB NCs/RMVs; finish interrupted deletions."""
    for stmt in schema.BOOTSTRAP:
        owner.execute(stmt)
    ncs, mvs = existing_objects(owner)
    with LIFECYCLE:
        for cluster in registry.all_clusters():
            if cluster["phase"] == "deleting":
                delete_cluster(owner, registry, cluster["id"])
                continue
            for node in registry.nodes(cluster["id"]):
                _reconcile_node(owner, registry, settings, node, bool(cluster["tls"]), ncs, mvs)
            if cluster["phase"] == "provisioning":
                registry.set_cluster_phase(cluster["id"], "active")


def retry(owner: ChdbOwner, registry: Registry, settings: Settings, node: dict) -> bool:
    with LIFECYCLE:
        cluster = registry.cluster(node["cluster_id"])
        current = next((n for n in registry.nodes(node["cluster_id"]) if n["id"] == node["id"]), None)
        if cluster is None or current is None or current["phase"] != "retrying":
            return False  # deleted or already handled meanwhile
        return _activate(owner, registry, settings, current, bool(cluster["tls"]))


def delete_cluster(owner: ChdbOwner, registry: Registry, cluster_id: str) -> None:
    """Persist 'deleting' first, then stop collection (RMVs), drop secrets (NCs), remove registry rows."""
    with LIFECYCLE:
        registry.set_cluster_phase(cluster_id, "deleting")
        nodes = registry.nodes(cluster_id)
        for node in nodes:
            drop_rmvs(owner, node["id"])
        for node in nodes:
            owner.execute(schema.drop_nc_sql(node["id"]))
        with registry.tx() as c:
            for node in nodes:
                for table in ("source_status", "cursors", "gaps"):
                    c.execute(f"DELETE FROM {table} WHERE node_id = ?", (node["id"],))
            c.execute("DELETE FROM nodes WHERE cluster_id = ?", (cluster_id,))
            c.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
