import csv

import pytest

from tools.opportunities import load_opportunities


FIELDS = [
    "minute_ts", "time_utc", "fx_samples", "opportunity_threshold_bps",
    "book_update_skew_ms_max", "sell_edge_usd_mean_bps",
    "sell_edge_usd_max_bps", "sell_edge_usd_p95_bps",
    "sell_edge_usd_ge_10_samples",
    "sell_edge_usd_longest_ge_10_samples",
    "sell_edge_usd_longest_ge_10_span_seconds",
    "sell_edge_usd_max_time_utc", "sell_edge_usd_max_entropy_bid",
    "sell_edge_usd_max_hedge_ask", "sell_edge_usd_max_fx_ask",
    "buy_edge_usd_mean_bps", "buy_edge_usd_max_bps",
    "buy_edge_usd_p95_bps", "buy_edge_usd_ge_10_samples",
    "buy_edge_usd_longest_ge_10_samples",
    "buy_edge_usd_longest_ge_10_span_seconds",
    "buy_edge_usd_max_time_utc", "buy_edge_usd_max_entropy_ask",
    "buy_edge_usd_max_hedge_bid", "buy_edge_usd_max_fx_bid",
]


def _row(ts, sell_longest, sell_span, sell_max):
    return {
        "minute_ts": ts, "time_utc": "2026-08-29T00:00:00Z",
        "fx_samples": 60, "opportunity_threshold_bps": 10,
        "book_update_skew_ms_max": 40,
        "sell_edge_usd_mean_bps": -2, "sell_edge_usd_max_bps": sell_max,
        "sell_edge_usd_p95_bps": 8, "sell_edge_usd_ge_10_samples": 4,
        "sell_edge_usd_longest_ge_10_samples": sell_longest,
        "sell_edge_usd_longest_ge_10_span_seconds": sell_span,
        "sell_edge_usd_max_time_utc": "2026-08-29T00:00:10.000Z",
        "sell_edge_usd_max_entropy_bid": 100.3,
        "sell_edge_usd_max_hedge_ask": 100,
        "sell_edge_usd_max_fx_ask": 1,
        "buy_edge_usd_mean_bps": -5, "buy_edge_usd_max_bps": 1,
        "buy_edge_usd_p95_bps": 0, "buy_edge_usd_ge_10_samples": 0,
        "buy_edge_usd_longest_ge_10_samples": 0,
        "buy_edge_usd_longest_ge_10_span_seconds": 0,
        "buy_edge_usd_max_time_utc": "2026-08-29T00:00:20.000Z",
        "buy_edge_usd_max_entropy_ask": 100.4,
        "buy_edge_usd_max_hedge_bid": 99.9,
        "buy_edge_usd_max_fx_bid": 1,
    }


def test_opportunity_report_rejects_spike_and_keeps_sustained_run(tmp_path):
    path = tmp_path / "minutes.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(_row(100, 1, 0, 25))
        writer.writerow(_row(200, 4, 3, 18))

    rows = load_opportunities(str(path), hours=0)

    assert len(rows) == 1
    assert rows[0]["minute_ts"] == 200
    assert rows[0]["longest"] == 4
    assert rows[0]["span"] == 3
    assert rows[0]["book_update_skew_ms_max"] == 40


def test_opportunity_report_requires_new_recorder_schema(tmp_path):
    path = tmp_path / "old.csv"
    path.write_text("minute_ts,fx_samples\n1,60\n", encoding="utf-8")

    with pytest.raises(ValueError, match="persistence fields"):
        load_opportunities(str(path), hours=0)
