"""Runtime settings. Every value has a safe default; env overrides are validated."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = int(raw)
    if not low <= value <= high:
        raise ValueError(f"{name} must be within [{low}, {high}]")
    return value


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    key_file: Path
    tls_ca_file: Path | None
    bind_host: str
    port: int
    refresh_seconds: int  # RMV period for metrics and query_log
    safety_lag_seconds: int  # query_log async flush allowance
    overlap_seconds: int  # extra re-read window per refresh
    initial_backfill_seconds: int  # history fetched right after registration
    max_backfill_seconds: int  # older gaps are recorded as unrecoverable
    backfill_chunk_seconds: int
    probe_timeout_seconds: int
    supervisor_poll_seconds: int
    collector_tag: str  # log_comment marking our own queries on the sources

    @property
    def chdb_path(self) -> Path:
        return self.data_dir / "chdb"

    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.sqlite3"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "app.lock"

    @property
    def chdb_config_path(self) -> Path:
        return self.data_dir / "chdb-config.xml"

    @property
    def query_window_seconds(self) -> int:
        """Length of the re-read window [now - lag - window, now - lag)."""
        return self.refresh_seconds + self.overlap_seconds

    @property
    def stale_after_seconds(self) -> int:
        """No successful refresh for this long => '수집 지연'."""
        return 3 * self.refresh_seconds + self.safety_lag_seconds


def load_settings() -> Settings:
    data_dir = _path_env("MLH_DATA_DIR", PROJECT_ROOT / "var" / "app")
    key_file = _path_env("MLH_KEY_FILE", Path.home() / ".config" / "mini-loghouse" / "nc.key")
    ca_raw = os.environ.get("MLH_TLS_CA_FILE")
    refresh = _int_env("MLH_REFRESH_SECONDS", 15, 5, 300)
    overlap = _int_env("MLH_OVERLAP_SECONDS", 30, 5, 3600)
    return Settings(
        data_dir=data_dir,
        key_file=key_file,
        tls_ca_file=Path(ca_raw).expanduser().resolve() if ca_raw else None,
        bind_host="127.0.0.1",
        port=_int_env("MLH_PORT", 8765, 1024, 65535),
        refresh_seconds=refresh,
        safety_lag_seconds=_int_env("MLH_SAFETY_LAG_SECONDS", 10, 0, 600),
        overlap_seconds=overlap,
        initial_backfill_seconds=_int_env("MLH_INITIAL_BACKFILL_SECONDS", 900, 0, 86400),
        max_backfill_seconds=_int_env("MLH_MAX_BACKFILL_SECONDS", 86400, 600, 7 * 86400),
        backfill_chunk_seconds=_int_env("MLH_BACKFILL_CHUNK_SECONDS", 300, 30, 3600),
        probe_timeout_seconds=_int_env("MLH_PROBE_TIMEOUT_SECONDS", 15, 3, 120),
        supervisor_poll_seconds=_int_env("MLH_SUPERVISOR_POLL_SECONDS", 2, 1, 60),
        collector_tag="mini-loghouse-collector",
    )
