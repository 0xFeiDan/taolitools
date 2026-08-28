#!/usr/bin/env python3
"""List sustained executable-edge observations from recorder CSV data.

The recorder evaluates a fixed 10 bps observation line once per second. This
tool rejects isolated maxima and only prints runs that meet both a consecutive
sample count and a wall-clock span. It is an observation report, not an order
or a profitability guarantee.
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time


def load_opportunities(path: str, *, hours: float = 24.0,
                       min_fx_samples: int = 10,
                       min_consecutive: int = 4,
                       min_span_sec: float = 3.0) -> list[dict]:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    common = {
        "minute_ts", "time_utc", "fx_samples",
        "opportunity_threshold_bps", "book_update_skew_ms_max",
    }
    direction_fields = {
        "sell": {
            "mean": "sell_edge_usd_mean_bps",
            "max": "sell_edge_usd_max_bps",
            "p95": "sell_edge_usd_p95_bps",
            "hits": "sell_edge_usd_ge_10_samples",
            "longest": "sell_edge_usd_longest_ge_10_samples",
            "span": "sell_edge_usd_longest_ge_10_span_seconds",
            "max_time": "sell_edge_usd_max_time_utc",
            "entropy": "sell_edge_usd_max_entropy_bid",
            "hedge": "sell_edge_usd_max_hedge_ask",
            "fx": "sell_edge_usd_max_fx_ask",
        },
        "buy": {
            "mean": "buy_edge_usd_mean_bps",
            "max": "buy_edge_usd_max_bps",
            "p95": "buy_edge_usd_p95_bps",
            "hits": "buy_edge_usd_ge_10_samples",
            "longest": "buy_edge_usd_longest_ge_10_samples",
            "span": "buy_edge_usd_longest_ge_10_span_seconds",
            "max_time": "buy_edge_usd_max_time_utc",
            "entropy": "buy_edge_usd_max_entropy_ask",
            "hedge": "buy_edge_usd_max_hedge_bid",
            "fx": "buy_edge_usd_max_fx_bid",
        },
    }
    required = set(common)
    for fields in direction_fields.values():
        required.update(fields.values())

    results = []
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise ValueError(
                "CSV lacks persistence fields; restart the updated recorder: "
                + ", ".join(sorted(missing)))
        for row in reader:
            try:
                minute_ts = float(row["minute_ts"])
                fx_samples = int(row["fx_samples"])
                threshold = float(row["opportunity_threshold_bps"])
            except (TypeError, ValueError):
                continue
            if minute_ts < cutoff or fx_samples < min_fx_samples:
                continue
            for direction, fields in direction_fields.items():
                try:
                    longest = int(row[fields["longest"]])
                    span = float(row[fields["span"]])
                    skew_max = float(row["book_update_skew_ms_max"])
                    numeric = {
                        key: float(row[field])
                        for key, field in fields.items()
                        if key not in {"max_time", "longest"}
                    }
                except (TypeError, ValueError):
                    continue
                if (longest < min_consecutive or span < min_span_sec
                        or not math.isfinite(skew_max)
                        or not all(math.isfinite(value)
                                   for value in numeric.values())):
                    continue
                results.append({
                    "direction": direction,
                    "minute_ts": minute_ts,
                    "time_utc": row["time_utc"],
                    "fx_samples": fx_samples,
                    "threshold": threshold,
                    "longest": longest,
                    "book_update_skew_ms_max": skew_max,
                    "max_time": row[fields["max_time"]],
                    **numeric,
                })
    return sorted(results, key=lambda item: item["max"], reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="report sustained >=10 bps observations / 持续价差报告")
    parser.add_argument("--csv", default="logs/minutes.csv")
    parser.add_argument("--hours", type=float, default=24.0,
                        help="recent hours, 0 means all / 最近小时数")
    parser.add_argument("--min-fx-samples", type=int, default=10)
    parser.add_argument("--min-consecutive", type=int, default=4)
    parser.add_argument("--min-span-sec", type=float, default=3.0)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    try:
        rows = load_opportunities(
            args.csv, hours=args.hours, min_fx_samples=args.min_fx_samples,
            min_consecutive=args.min_consecutive,
            min_span_sec=args.min_span_sec)
    except (FileNotFoundError, ValueError) as exc:
        print(f"无法分析 {args.csv}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(f"候选规则: 连续样本 >= {args.min_consecutive}, "
          f"持续时间 >= {args.min_span_sec:g}s, "
          f"有效汇率样本 >= {args.min_fx_samples}")
    if not rows:
        print("没有满足持续性条件的机会；单点 max 已被排除。")
        return

    for row in rows[:max(args.top, 0)]:
        label = "卖 IO / 买对冲平台" if row["direction"] == "sell" \
            else "买 IO / 卖对冲平台"
        print("\n----------------------------------------")
        print(f"分钟: {row['time_utc']}  方向: {label}")
        print(f"均值/95分位/最高: {row['mean']:+.3f} / "
              f"{row['p95']:+.3f} / {row['max']:+.3f} bps")
        print(f">={row['threshold']:.1f} bps 总次数: {int(row['hits'])}  "
              f"最长连续: {row['longest']} 次 / {row['span']:.3f}s")
        print(f"峰值时间: {row['max_time']}")
        print(f"峰值盘口 Entropy/对冲/汇率: {row['entropy']:.10g} / "
              f"{row['hedge']:.10g} / {row['fx']:.10g}")
        print(f"盘口更新时间差 max: {row['book_update_skew_ms_max']:.1f}ms")


if __name__ == "__main__":
    main()
