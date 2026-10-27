"""Probe result shaping and the register-cluster flow.

The browser's probe result is never trusted for saving: registration re-probes
every selected node on the server, keeps only nodes that pass the required
checks (connect + system.metrics + system.query_log), de-duplicates addresses
that point at the same server (serverUUID), then provisions NC + RMVs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import provisioner
from .chdb_owner import ChdbOwner
from .probe import required_ok, run_probe
from .registry import DuplicateError, Registry
from .settings import Settings
from .sqlsafe import RegisterRequest, new_id


class RegistrationError(Exception):
    def __init__(self, status: int, category: str, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.status, self.category, self.message, self.detail = status, category, message, detail


@dataclass(frozen=True)
class Discovery:
    cluster_name: str | None
    candidate_count: int


def endpoint_payload(req: RegisterRequest | Any) -> dict[str, Any]:
    return {"host": req.host, "port": req.port, "tls": req.tls, "username": req.username, "password": req.password}


def discovery(result: dict[str, Any]) -> Discovery:
    rows = result.get("endpoint", {}).get("checks", {}).get("clusters", {}).get("rows")
    if not isinstance(rows, list):
        return Discovery(None, 0)
    local = sorted({r["cluster"] for r in rows if int(r["is_local"]) == 1})
    if not local:
        return Discovery(None, 0)
    members = {(r["host_name"], int(r["port"])) for r in rows if r["cluster"] in local}
    return Discovery(",".join(local), len(members))


def summarize(result: dict[str, Any]) -> dict[str, Any]:
    """Browser-facing probe result. Contains no password (the child never echoes it)."""
    if "fatal" in result:
        return {"ok": False, "fatal": result["fatal"]}
    endpoint = result["endpoint"]
    found = discovery(result)
    return {
        "ok": required_ok(endpoint), "endpoint": endpoint,
        "candidates": [{**c, "required_ok": required_ok(c)} for c in result.get("candidates", [])],
        "candidates_source": result.get("candidates_source"),
        "discovered_cluster": found.cluster_name, "candidate_count": found.candidate_count,
    }


def _verified_nodes(result: dict[str, Any]) -> list[dict[str, Any]]:
    targets = [result["endpoint"], *result.get("candidates", [])]
    seen_uuid: set[str] = set()
    nodes = []
    for target in targets:
        if not required_ok(target):
            continue
        info = target["connect"]
        if info["server_uuid"] in seen_uuid:
            continue  # two addresses for the same server: collect once
        seen_uuid.add(info["server_uuid"])
        nodes.append({"host": target["host"], "port": target["port"], "display_name": info["display_name"],
                      "server_uuid": info["server_uuid"], "server_version": info["version"]})
    return nodes


def register(owner: ChdbOwner, registry: Registry, settings: Settings, req: RegisterRequest) -> str:
    endpoint = endpoint_payload(req)
    others = [n.model_dump() for n in req.nodes if (n.host, n.port) != (req.host, req.port)]
    result = run_probe(settings, endpoint, others)
    shaped = summarize(result)
    if not shaped["ok"]:
        raise RegistrationError(400, "probe_failed", "endpoint의 연결 또는 필수 권한 검사를 통과하지 못했습니다.", shaped)
    verified = _verified_nodes(result)
    found = Discovery(shaped["discovered_cluster"], shaped["candidate_count"])
    cluster_id = new_id("c")
    cluster = {
        "id": cluster_id, "name": req.name, "host": req.host, "port": req.port, "tls": int(req.tls),
        "username": req.username, "discovered_cluster": found.cluster_name,
        "candidate_count": found.candidate_count or len(verified),
        "server_version": result["endpoint"]["connect"]["version"],
    }
    nodes = [{**n, "id": new_id("n"), "cluster_id": cluster_id} for n in verified]
    try:
        registry.insert_cluster(cluster, nodes)
    except DuplicateError as exc:
        raise RegistrationError(409, "duplicate", "이미 등록된 endpoint/계정 또는 노드입니다.") from exc
    provisioner.provision(owner, registry, settings, registry.cluster(cluster_id), req.password)
    return cluster_id
