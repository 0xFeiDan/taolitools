"""Rolling in-process latency metrics."""
from entropy_arb.metrics import LatencyStats


def test_latency_percentiles_and_bounded_window():
    stats = LatencyStats(max_samples=4)
    stats.extend("send_to_ack_ms", [1, 2, 3, 4, 5])
    summary = stats.summary("send_to_ack_ms")
    assert summary.count == 4              # oldest sample was evicted
    assert summary.p50_ms == 3.5
    assert 4.8 < summary.p95_ms < 5.0
    assert 4.9 < summary.p99_ms <= 5.0
    assert summary.max_ms == 5.0


def test_latency_rejects_invalid_samples():
    stats = LatencyStats()
    stats.record("x", None)
    stats.record("x", -1)
    stats.record("x", float("nan"))
    assert stats.summary("x") is None
    assert stats.snapshot() == {}
