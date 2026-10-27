from dataclasses import replace

from mlh.settings import load_settings
from mlh.status import advance_cursor, coverage_label, evaluate_source, worst_state

S = replace(load_settings(), refresh_seconds=15, safety_lag_seconds=10, overlap_seconds=30,
            initial_backfill_seconds=900, max_backfill_seconds=86400)
NODE = {"id": "n000000000001", "phase": "active"}


def test_first_success_sets_cursor_and_initial_backfill_gap():
    cursor, gaps = advance_cursor(None, 1000, 1000, S)
    window = S.query_window_seconds  # 45
    assert cursor == (1000 + 1 - 10 - window, 990)
    assert [(g.lo, g.hi, g.state, g.reason) for g in gaps] == [(946 - 900, 946, "pending", "initial")]


def test_overlapping_windows_are_contiguous():
    cursor, gaps = advance_cursor({"first_lo": 0, "committed_hi": 990}, 1015, 1015, S)
    assert gaps == [] and cursor == (0, 1005)


def test_interruption_longer_than_overlap_creates_gap():
    cursor, gaps = advance_cursor({"first_lo": 0, "committed_hi": 990}, 1200, 1200, S)
    lo_new = 1200 + 1 - 10 - 45
    assert [(g.lo, g.hi, g.state) for g in gaps] == [(990, lo_new, "pending")]
    assert cursor == (0, 1190)


def test_gap_older_than_max_backfill_is_unrecoverable():
    now = 200000
    cursor, gaps = advance_cursor({"first_lo": 0, "committed_hi": 1000}, now, now, S)
    assert gaps[0].state == "unrecoverable" and gaps[0].lo == 1000 and gaps[0].hi == now - 86400
    assert gaps[1].state == "pending" and gaps[1].lo == now - 86400


def test_old_observation_does_not_move_cursor_backwards():
    assert advance_cursor({"first_lo": 0, "committed_hi": 990}, 990, 2000, S) == (None, [])


def test_states():
    ok = evaluate_source(NODE, "metrics", {"last_success_ts": 100, "last_refresh_ts": 100, "exception": ""}, None, 110, S)
    assert ok["state"] == "collecting"
    late = evaluate_source(NODE, "metrics", {"last_success_ts": 100, "last_refresh_ts": 100, "exception": ""}, None, 100 + S.stale_after_seconds + 1, S)
    assert late["state"] == "delayed"
    first = evaluate_source(NODE, "metrics", {"last_success_ts": None, "last_refresh_ts": None, "exception": ""}, None, 100, S)
    assert first["state"] == "waiting_first"
    err = evaluate_source(NODE, "metrics", {"last_success_ts": 90, "last_refresh_ts": 100, "exception": "Connection refused (127.0.0.1:19402)"}, None, 101, S)
    assert err["state"] == "error" and err["last_error_category"] == "refused" and err["consecutive_failures"] == 1
    again = evaluate_source(NODE, "metrics", {"last_success_ts": 90, "last_refresh_ts": 105, "exception": "Connection refused"}, err, 106, S)
    assert again["consecutive_failures"] == 2


def test_restart_keeps_persisted_success_when_view_refreshes_resets():
    persisted = {"state": "collecting", "last_success_at": 500, "last_attempt_at": 500, "consecutive_failures": 0}
    st = evaluate_source(NODE, "query_log", {"last_success_ts": None, "last_refresh_ts": None, "exception": ""}, persisted, 505, S)
    assert st["state"] == "collecting" and st["last_success_at"] == 500


def test_phase_driven_states():
    assert evaluate_source({**NODE, "phase": "pending"}, "metrics", None, None, 1, S)["state"] == "preparing"
    retry = evaluate_source({**NODE, "phase": "retrying", "last_error_category": "refused", "last_error_message": "m"}, "metrics", None, None, 1, S)
    assert retry["state"] == "error"


def test_worst_state_and_coverage():
    assert worst_state(["collecting", "delayed"]) == "delayed"
    assert worst_state(["collecting", "error", "waiting_first"]) == "error"
    assert coverage_label(2, 2, True) == "전체 검증됨"
    assert coverage_label(1, 2, True) == "일부"
    assert coverage_label(1, 0, False) == "미확인"
