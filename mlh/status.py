"""Pure status / cursor logic (no I/O) so it can be unit tested.

States per (node, source):
  preparing      RMV not created yet (준비 중)
  waiting_first  RMV exists, no successful refresh yet (첫 수집 대기)
  collecting     last success within stale_after (수집 중)
  delayed        last success older than stale_after, no current error (수집 지연)
  error          latest refresh failed, or RMV/credential setup failed (수집 오류)

Coverage cursor for query_log (per node):
  An RMV refresh that succeeded at second S read [S+1-lag-W, S-lag) at least
  (S has 1 s precision in system.view_refreshes; now() is evaluated in [S, S+1)).
  Measured: last_success_time is the refresh START (floored), last_refresh_time
  the end (evidence/phase_a2/view_refreshes_time_semantics.log), so S - lag never
  overstates what was read.
  Consecutive successes are contiguous while their windows overlap; otherwise
  the hole becomes a gap that the supervisor backfills explicitly.
  The cursor is NOT max(event_time): it is the end of a window that was fully
  read by a successful statement, independent of whether events existed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import classify
from .settings import Settings

STATE_RANK = {"collecting": 0, "preparing": 1, "waiting_first": 2, "delayed": 3, "error": 4}
STATE_LABEL = {
    "preparing": "준비 중", "waiting_first": "첫 수집 대기", "collecting": "수집 중",
    "delayed": "수집 지연", "error": "수집 오류",
}


@dataclass(frozen=True)
class Gap:
    lo: int
    hi: int
    state: str  # pending | unrecoverable
    reason: str


def _phase_status(node: dict, source: str, persisted: dict | None, now: float) -> dict[str, Any] | None:
    phase = node["phase"]
    if phase == "active":
        return None
    base = {
        "node_id": node["id"], "source": source,
        "last_attempt_at": (persisted or {}).get("last_attempt_at"),
        "last_success_at": (persisted or {}).get("last_success_at"),
        "last_error_codes": None, "consecutive_failures": (persisted or {}).get("consecutive_failures", 0),
    }
    if phase == "pending":
        return {**base, "state": "preparing", "last_error_category": None, "last_error_message": None}
    return {**base, "state": "error", "last_error_category": node.get("last_error_category"),
            "last_error_message": node.get("last_error_message")}


def evaluate_source(node: dict, source: str, refresh: dict | None, persisted: dict | None,
                    now: float, settings: Settings) -> dict[str, Any]:
    by_phase = _phase_status(node, source, persisted, now)
    if by_phase is not None:
        return by_phase
    prev = persisted or {}
    if refresh is None:
        return {"node_id": node["id"], "source": source, "state": "preparing",
                "last_attempt_at": prev.get("last_attempt_at"), "last_success_at": prev.get("last_success_at"),
                "last_error_category": None, "last_error_message": None, "last_error_codes": None,
                "consecutive_failures": 0}
    successes = [t for t in (refresh.get("last_success_ts"), prev.get("last_success_at")) if t]
    last_success = max(successes) if successes else None
    last_attempt = refresh.get("last_refresh_ts") or prev.get("last_attempt_at")
    result = {"node_id": node["id"], "source": source, "last_attempt_at": last_attempt, "last_success_at": last_success}
    if refresh.get("exception"):
        info = classify(refresh["exception"])
        new_attempt = last_attempt != prev.get("last_attempt_at") or prev.get("state") != "error"
        failures = int(prev.get("consecutive_failures") or 0) + (1 if new_attempt else 0)
        return {**result, "state": "error", "last_error_category": info.category, "last_error_message": info.message,
                "last_error_codes": ",".join(map(str, info.codes)), "consecutive_failures": max(failures, 1)}
    if last_success is None:
        state = "waiting_first"
    else:
        state = "collecting" if now - last_success <= settings.stale_after_seconds else "delayed"
    return {**result, "state": state, "last_error_category": None, "last_error_message": None,
            "last_error_codes": None, "consecutive_failures": 0}


def status_changed(old: dict | None, new: dict) -> bool:
    if old is None:
        return True
    keys = ("state", "last_attempt_at", "last_success_at", "last_error_category", "consecutive_failures")
    return any(old.get(k) != new.get(k) for k in keys)


def advance_cursor(cursor: dict | None, success_ts: int, now: float,
                   settings: Settings) -> tuple[tuple[int, int] | None, list[Gap]]:
    """Return ((first_lo, committed_hi) or None if unchanged, gaps to record)."""
    lag, window = settings.safety_lag_seconds, settings.query_window_seconds
    hi = success_ts - lag
    lo = success_ts + 1 - lag - window
    if cursor is None:
        gaps = [Gap(lo - settings.initial_backfill_seconds, lo, "pending", "initial")] if settings.initial_backfill_seconds else []
        return (lo, hi), gaps
    committed = int(cursor["committed_hi"])
    if hi <= committed:
        return None, []
    gaps: list[Gap] = []
    if lo > committed:
        gap_lo, floor = committed, int(now) - settings.max_backfill_seconds
        if gap_lo < floor:
            gaps.append(Gap(gap_lo, min(floor, lo), "unrecoverable", "older_than_max_backfill"))
            gap_lo = floor
        if lo > gap_lo:
            gaps.append(Gap(gap_lo, lo, "pending", "collection_interrupted"))
    return (int(cursor["first_lo"]), hi), gaps


def worst_state(states: list[str]) -> str:
    return max(states, key=lambda s: STATE_RANK.get(s, 0)) if states else "preparing"


def coverage_label(confirmed: int, candidates: int, discovered: bool) -> str:
    if not discovered:
        return "미확인"
    return "전체 검증됨" if confirmed >= candidates > 0 else "일부"
