from mlh.dashboard import node_coverage, range_state
from mlh.probe import _assemble

ENDPOINT = {"host": "h", "port": 9440, "connect": {"ok": True}, "checks": {}}


def test_probe_timeout_keeps_endpoint_and_marks_unfinished_candidates():
    # Codex P2-8: candidate checks timing out must not erase the endpoint result.
    lines = [
        {"kind": "endpoint", "result": ENDPOINT},
        {"kind": "plan", "candidates": [{"host": "a2", "port": 9440}, {"host": "a3", "port": 9440}], "source": "system.clusters"},
        {"kind": "candidate", "result": {"host": "a2", "port": 9440, "connect": {"ok": True}}},
    ]
    out = _assemble(lines, timed_out=True)
    assert out["endpoint"] is ENDPOINT and out["partial"] is True
    assert [c["connect"].get("category") for c in out["candidates"]] == [None, "timeout"]


def test_probe_without_endpoint_is_fatal():
    assert _assemble([], timed_out=True)["fatal"]["category"] == "timeout"


def test_range_coverage_none_partial_full():
    cursor = {"first_lo": 1000, "committed_hi": 2000}
    # Codex P2-7: node stopped long ago -> selected recent range is not collected at all
    assert node_coverage(cursor, [], 5000, 5900, 20)["state"] == "none"
    assert node_coverage(cursor, [], 1500, 2010, 20)["state"] == "full"
    gap = [{"lo": 1600, "hi": 1700, "state": "pending"}]
    assert node_coverage(cursor, gap, 1500, 2010, 20)["state"] == "partial"
    assert node_coverage(None, [], 1500, 2010, 20)["state"] == "none"


def test_range_state_aggregates_nodes():
    full = {"range": {"state": "full"}}
    none = {"range": {"state": "none"}}
    assert range_state({"a": full, "b": full}) == "full"
    assert range_state({"a": full, "b": none}) == "partial"
    assert range_state({"a": none}) == "none"
