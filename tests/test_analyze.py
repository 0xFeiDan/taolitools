import csv

import pytest

from tools.analyze import load_rows


def test_analyzer_discards_nonfinite_recorded_values(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = ["minute_ts", "samples", "premium_close_bps",
              "premium_mean_bps", "sell_edge_max_bps",
              "buy_edge_max_bps"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "minute_ts": 1, "samples": 10, "premium_close_bps": 2,
            "premium_mean_bps": 2, "sell_edge_max_bps": 3,
            "buy_edge_max_bps": 3})
        writer.writerow({
            "minute_ts": 2, "samples": 10, "premium_close_bps": "NaN",
            "premium_mean_bps": 2, "sell_edge_max_bps": 3,
            "buy_edge_max_bps": 3})
    rows = load_rows(str(path), hours=0, min_samples=1, basis="raw")
    assert len(rows) == 1 and rows[0]["ts"] == 1.0


def test_analyzer_defaults_to_usd_adjusted_rows_and_requires_fx_samples(tmp_path):
    path = tmp_path / "minutes.csv"
    fields = ["minute_ts", "samples", "fx_samples",
              "premium_usd_close_bps", "premium_usd_mean_bps",
              "sell_edge_usd_max_bps", "buy_edge_usd_max_bps"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "minute_ts": 1, "samples": 60, "fx_samples": 60,
            "premium_usd_close_bps": 2, "premium_usd_mean_bps": 2,
            "sell_edge_usd_max_bps": 3, "buy_edge_usd_max_bps": 4})
        writer.writerow({
            "minute_ts": 2, "samples": 60, "fx_samples": 0,
            "premium_usd_close_bps": 999, "premium_usd_mean_bps": 999,
            "sell_edge_usd_max_bps": 999, "buy_edge_usd_max_bps": 999})

    rows = load_rows(str(path), hours=0, min_samples=10)

    assert rows == [{"ts": 1.0, "prem": 2.0, "prem_mean": 2.0,
                     "sell_max": 3.0, "buy_max": 4.0}]


def test_analyzer_refuses_old_csv_as_usd_instead_of_assuming_parity(tmp_path):
    path = tmp_path / "old-minutes.csv"
    path.write_text("minute_ts,samples,premium_close_bps\n1,60,2\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="--basis raw"):
        load_rows(str(path), hours=0, min_samples=1)
