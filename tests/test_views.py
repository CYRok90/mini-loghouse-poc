from dataclasses import replace

from mlh.dashboard import cluster_view
from mlh.registry import Registry
from mlh.settings import load_settings

S = replace(load_settings(), refresh_seconds=5, safety_lag_seconds=8, overlap_seconds=15)


def _registry(tmp_path):
    reg = Registry(tmp_path / "r.sqlite3")
    reg.insert_cluster({"id": "c000000000001", "name": "x", "host": "h", "port": 9000, "tls": 0, "username": "u",
                        "discovered_cluster": "cl", "candidate_count": 2, "server_version": "26.8"},
                       [{"id": "n000000000001", "cluster_id": "c000000000001", "host": "h", "port": 9000,
                         "display_name": "a1", "server_uuid": "u1", "server_version": "26.8"}])
    reg.set_node_phase("n000000000001", "active")
    for source in ("metrics", "query_log"):
        reg.upsert_source_status({"node_id": "n000000000001", "source": source, "state": "collecting",
                                  "last_attempt_at": 100.0, "last_success_at": 100.0, "last_error_category": None,
                                  "last_error_message": None, "last_error_codes": None, "consecutive_failures": 0})
    return reg


def test_partial_coverage_when_fewer_nodes_than_discovered(tmp_path):
    reg = _registry(tmp_path)
    view = cluster_view(reg, S, reg.cluster("c000000000001"))
    assert view["state"] == "collecting"
    assert view["coverage"]["label"] == "일부" and view["coverage"]["confirmed_nodes"] == 1


def test_recovery_mode_never_shows_persisted_state_as_live(tmp_path):
    reg = _registry(tmp_path)
    view = cluster_view(reg, S, reg.cluster("c000000000001"), recovery=True)
    assert view["state"] == "error" and view["latest_error"]["category"] == "recovery_mode"
    assert view["last_check"]["ok"] is False
    assert view["last_success_ms"] == 100000  # history is still shown


def test_transition_rows_only_on_state_change(tmp_path):
    reg = _registry(tmp_path)
    status = {"node_id": "n000000000001", "source": "metrics", "state": "collecting", "last_attempt_at": 105.0,
              "last_success_at": 105.0, "last_error_category": None, "last_error_message": None,
              "last_error_codes": None, "consecutive_failures": 0}
    reg.upsert_source_status(status)
    reg.upsert_source_status({**status, "state": "error", "last_error_category": "refused"})
    states = [t["to_state"] for t in reg.transitions(["n000000000001"])]
    assert states.count("error") == 1 and states.count("collecting") == 2  # initial metrics + query_log
