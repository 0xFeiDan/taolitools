import csv

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
    rows = load_rows(str(path), hours=0, min_samples=1)
    assert len(rows) == 1 and rows[0]["ts"] == 1.0
