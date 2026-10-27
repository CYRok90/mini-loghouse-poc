"""FastAPI app: lifecycle (lock -> key -> chDB -> reconcile -> supervisor) and routes.

The collection machinery is created once in the lifespan of the single backend
process. HTTP handlers only read state or trigger one-off registration; page
loads, auto-refresh and multiple tabs never create or restart collectors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from . import dashboard, provisioner, security
from .chdb_owner import ChdbOwner, acquire_lock, write_private
from .keystore import KeyError_, load_or_create
from .probe import render_probe_config, run_probe
from .registration import RegistrationError, endpoint_payload, register, summarize
from .registry import Registry
from .settings import Settings
from .sqlsafe import NodeAddress, ProbeRequest, RegisterRequest
from .supervisor import Supervisor

log = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"
CLUSTER_ID_RE = re.compile(r"^c[0-9a-f]{12}$")
NODE_ID_RE = re.compile(r"^n[0-9a-f]{12}$")


@dataclass(frozen=True)
class Runtime:
    registry: Registry
    owner: ChdbOwner | None
    supervisor: Supervisor | None
    recovery_reason: str | None
    lock_fd: int


class ProbeBody(ProbeRequest):
    candidates: list[NodeAddress] | None = Field(default=None, max_length=16)


def start_runtime(settings: Settings) -> Runtime:
    # Lock FIRST (Codex P2-6): a losing second process must not read or write the
    # registry/key fingerprint before it learns another backend owns the data dir.
    lock_fd = acquire_lock(settings.lock_path)
    try:
        return _start_locked(settings, lock_fd)
    except BaseException:
        os.close(lock_fd)
        raise


def _start_locked(settings: Settings, lock_fd: int) -> Runtime:
    registry = Registry(settings.registry_path)
    fingerprint = registry.get_meta("key_fingerprint")
    try:
        key = load_or_create(settings.key_file, fingerprint)
    except KeyError_ as exc:
        log.error("recovery mode: %s", exc)
        return Runtime(registry, None, None, f"암호화 키 문제: {exc}", lock_fd)
    if fingerprint is None:
        registry.set_meta("key_fingerprint", key.fingerprint)
    owner = ChdbOwner(settings, key)
    write_private(settings.data_dir / "probe-config.xml", render_probe_config(settings))
    provisioner.reconcile(owner, registry, settings)
    supervisor = Supervisor(owner, registry, settings)
    supervisor.start()
    return Runtime(registry, owner, supervisor, None, lock_fd)


def stop_runtime(runtime: Runtime) -> None:
    try:
        if runtime.supervisor:
            runtime.supervisor.stop()
        if runtime.owner:
            runtime.owner.close()
    finally:
        os.close(runtime.lock_fd)


def create_app(settings: Settings) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runtime = start_runtime(settings)
        app.state.register_lock = asyncio.Lock()
        yield
        stop_runtime(app.state.runtime)

    app = FastAPI(title="mini-loghouse", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    security.install(app, settings.port)
    _pages(app)
    _api(app, settings)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def _pages(app: FastAPI) -> None:
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/clusters/new", include_in_schema=False)
    async def add_page() -> FileResponse:
        return FileResponse(STATIC / "add.html")

    @app.get("/clusters/{cluster_id}", include_in_schema=False)
    async def cluster_page(cluster_id: str) -> FileResponse:
        if not CLUSTER_ID_RE.fullmatch(cluster_id):
            raise HTTPException(404)
        return FileResponse(STATIC / "cluster.html")


def _rt(app: FastAPI) -> Runtime:
    return app.state.runtime


def _require_owner(rt: Runtime) -> ChdbOwner:
    if rt.owner is None:
        raise HTTPException(503, detail=rt.recovery_reason or "recovery mode")
    return rt.owner


def _cluster_or_404(rt: Runtime, cluster_id: str) -> dict[str, Any]:
    cluster = rt.registry.cluster(cluster_id) if CLUSTER_ID_RE.fullmatch(cluster_id) else None
    if cluster is None:
        raise HTTPException(404, detail="cluster not found")
    return cluster


def _api(app: FastAPI, settings: Settings) -> None:
    @app.get("/api/health")
    async def health() -> dict:
        rt = _rt(app)
        return {
            "mode": "recovery" if rt.owner is None else "normal", "recovery_reason": rt.recovery_reason,
            "refresh_seconds": settings.refresh_seconds, "stale_after_seconds": settings.stale_after_seconds,
            "safety_lag_seconds": settings.safety_lag_seconds, "overlap_seconds": settings.overlap_seconds,
            "credential_policy": "encrypted_named_collection",
        }

    @app.post("/api/probe")
    async def probe(body: ProbeBody) -> dict:
        _require_owner(_rt(app))
        candidates = [c.model_dump() for c in body.candidates] if body.candidates is not None else None
        result = await run_in_threadpool(run_probe, settings, endpoint_payload(body), candidates)
        return summarize(result)

    @app.post("/api/clusters", status_code=201)
    async def create_cluster(body: RegisterRequest) -> JSONResponse:
        rt = _rt(app)
        owner = _require_owner(rt)
        async with app.state.register_lock:  # double-click / resend cannot race the unique checks
            try:
                cluster_id = await run_in_threadpool(register, owner, rt.registry, settings, body)
            except RegistrationError as exc:
                body_ = security.error_body(exc.category, exc.message, probe=exc.detail)
                return JSONResponse(body_, status_code=exc.status)
        return JSONResponse({"id": cluster_id}, status_code=201)

    @app.get("/api/clusters")
    async def list_clusters() -> dict:
        rt = _rt(app)
        views = [dashboard.cluster_view(rt.registry, settings, c, rt.owner is None) for c in rt.registry.clusters()]
        return {"mode": "recovery" if rt.owner is None else "normal", "clusters": views}

    @app.get("/api/clusters/{cluster_id}")
    async def get_cluster(cluster_id: str) -> dict:
        rt = _rt(app)
        cluster = _cluster_or_404(rt, cluster_id)
        view = dashboard.cluster_view(rt.registry, settings, cluster, rt.owner is None)
        node_ids = [n["id"] for n in view["nodes"]]
        return {**view, "transitions": rt.registry.transitions(node_ids, 30)}

    @app.get("/api/clusters/{cluster_id}/dashboard")
    async def get_dashboard(cluster_id: str, range: str = Query("1h", pattern="^(15m|1h|24h)$"),
                            node: str | None = Query(None)) -> dict:
        rt = _rt(app)
        owner = _require_owner(rt)
        cluster = _cluster_or_404(rt, cluster_id)
        if node is not None and (not NODE_ID_RE.fullmatch(node) or node not in {n["id"] for n in rt.registry.nodes(cluster_id)}):
            raise HTTPException(400, detail="unknown node")
        return await run_in_threadpool(dashboard.dashboard, owner, rt.registry, settings, cluster, range, node)

    @app.get("/api/clusters/{cluster_id}/diagnostics")
    async def get_diagnostics(cluster_id: str, prefix: str | None = Query(None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")) -> dict:
        rt = _rt(app)
        owner = _require_owner(rt)
        cluster = _cluster_or_404(rt, cluster_id)
        return await run_in_threadpool(dashboard.diagnostics, owner, rt.registry, cluster, prefix)

    @app.delete("/api/clusters/{cluster_id}")
    async def delete_cluster(cluster_id: str) -> dict:
        rt = _rt(app)
        owner = _require_owner(rt)
        _cluster_or_404(rt, cluster_id)
        async with app.state.register_lock:
            await run_in_threadpool(provisioner.delete_cluster, owner, rt.registry, cluster_id)
        return {"deleted": cluster_id}
