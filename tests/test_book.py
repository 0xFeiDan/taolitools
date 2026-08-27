"""plan_arb sizing math: thresholds, fees, caps, minimums.

Run:  python3 -m pytest tests/  (or  python3 tests/test_book.py)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.book import OrderBook, plan_arb  # noqa: E402


def make_book(bids, asks):
    b = OrderBook()
    b.apply_hl([[{"px": str(p), "sz": str(s)} for p, s in bids],
                [{"px": str(p), "sz": str(s)} for p, s in asks]])
    return b


def common(**over):
    kw = dict(threshold_bps=0.0, buy_fee_bps=0.0, sell_fee_bps=0.0,
              take_fraction=1.0, cap_notional=1e9, min_base=0.0,
              min_notional=0.0, size_step=1e-4)
    kw.update(over)
    return kw


def test_no_edge_below_threshold():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps top
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=6.0))
    assert plan is None and reason == "no_edge"


def test_edge_above_threshold():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps top
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=4.0))
    assert reason == "ok"
    assert abs(plan.qty - 10.0) < 1e-9
    assert abs(plan.top_premium_bps - 5.0) < 0.01
    assert plan.exp_edge_usd > 0


def test_planner_rejects_nan_threshold_instead_of_bypassing_edge_gate():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])
    with pytest.raises(ValueError, match="finite"):
        plan_arb(buy, sell, **common(threshold_bps=float("nan")))


def test_fees_kill_marginal_edge():
    buy = make_book(bids=[(99.9, 10)], asks=[(100.0, 10)])
    sell = make_book(bids=[(100.05, 10)], asks=[(100.2, 10)])  # +5 bps gross
    # 3 + 3 bps of fees swallow the 5 bps premium
    plan, reason = plan_arb(buy, sell, **common(buy_fee_bps=3.0,
                                                sell_fee_bps=3.0))
    assert plan is None and reason == "no_edge"


def test_take_fraction_and_cap():
    buy = make_book(bids=[(99.9, 100)], asks=[(100.0, 100)])
    sell = make_book(bids=[(100.5, 100)], asks=[(100.6, 100)])
    plan, reason = plan_arb(buy, sell, **common(take_fraction=0.5))
    assert reason == "ok" and abs(plan.qty - 50.0) < 1e-9
    plan, reason = plan_arb(buy, sell, **common(cap_notional=1000.0))
    assert reason == "ok"
    assert plan.buy_notional <= 1000.0 and plan.sell_notional <= 1000.0
    assert abs(plan.qty - 9.9502) < 1e-9  # sell leg is the tighter cap


def test_legacy_cap_applies_to_both_leg_notionals():
    buy = make_book(bids=[(99.0, 100)], asks=[(100.0, 100)])
    sell = make_book(bids=[(110.0, 100)], asks=[(111.0, 100)])
    plan, reason = plan_arb(buy, sell, **common(cap_notional=1000.0))
    assert reason == "ok"
    assert plan.buy_notional <= 1000.0 + 1e-9
    assert plan.sell_notional <= 1000.0 + 1e-9


def test_min_notional():
    buy = make_book(bids=[(99.9, 0.05)], asks=[(100.0, 0.05)])
    sell = make_book(bids=[(100.5, 0.05)], asks=[(100.6, 0.05)])
    plan, reason = plan_arb(buy, sell, **common(min_notional=10.0))
    assert plan is None and reason == "below_min_notional"


def test_marginal_slice_respects_threshold():
    # second ask level only clears 2 bps — with threshold 3 the crossable
    # size must stop at the first level
    buy = make_book(bids=[(99.9, 5)], asks=[(100.0, 5), (100.08, 5)])
    sell = make_book(bids=[(100.1, 20)], asks=[(100.3, 20)])
    plan, reason = plan_arb(buy, sell, **common(threshold_bps=3.0))
    assert reason == "ok"
    assert abs(plan.q_max - 5.0) < 1e-9


def test_lighter_diff_maintenance():
    b = make_book(bids=[(99.0, 5)], asks=[(100.0, 2), (100.1, 3)])
    # diff: the 100.0 ask level is removed server-side, a new bid appears
    b.apply_lighter({"bids": [{"price": "99.1", "size": "1"}],
                     "asks": [{"price": "100.0", "size": "0"}]},
                    snapshot=False)
    assert b.best_ask() == 100.1 and b.best_bid() == 99.1
    # a snapshot replaces the whole book
    b.apply_lighter({"bids": [{"price": "98.9", "size": "1"}],
                     "asks": [{"price": "100.2", "size": "3"}]},
                    snapshot=True)
    assert b.best_bid() == 98.9 and b.best_ask() == 100.2


def test_nonfinite_orderbook_levels_are_discarded():
    b = OrderBook()
    b.apply_hl([
        [{"px": "100", "sz": "1"}, {"px": "inf", "sz": "10"}],
        [{"px": "101", "sz": "1"}, {"px": "nan", "sz": "10"}],
    ])
    assert b.sorted_bids() == [(100.0, 1.0)]


def test_exchange_timestamp_age_cannot_be_hidden_by_recent_receive_time():
    b = OrderBook()
    b.apply_hl([
        [{"px": "100", "sz": "1"}],
        [{"px": "101", "sz": "1"}],
    ], received_ts=200.0, exchange_ts=199.0)
    quality = b.quality(10.0, max_book_age_ms=300.0, now=200.0)
    assert not quality.ok
    assert quality.reason == "exchange_stale"
    assert b.sorted_asks() == [(101.0, 1.0)]


def test_book_quality_separates_heartbeat_from_book_age():
    b = OrderBook()
    b.apply_hl([[{"px": "100", "sz": "1"}],
                [{"px": "101", "sz": "1"}]],
               received_ts=100.0, exchange_ts=99.975)
    quality = b.quality(1.0, max_book_age_ms=300.0, now=100.2)
    assert quality.ok and quality.reason == "ok"
    assert abs(quality.book_age_ms - 200.0) < 1e-9
    assert abs(quality.exchange_lag_ms - 25.0) < 1e-9

    # A heartbeat proves the socket is alive, but does not make an old book
    # update fresh under the V2 millisecond guard.
    b.touch(100.45)
    quality = b.quality(1.0, max_book_age_ms=300.0, now=100.5)
    assert not quality.ok and quality.reason == "book_stale"
    assert quality.message_age_ms < 100.0

    # Legacy mode still treats the same connected feed as fresh.
    assert b.quality(1.0, now=100.5).ok


def test_book_quality_connection_states_fail_closed():
    b = OrderBook()
    assert b.quality(1.0, now=100.0).reason == "disconnected"
    b.mark_connecting()
    assert b.quality(1.0, now=100.0).reason == "not_ready"
    b.apply_lighter(
        {"bids": [{"price": "100", "size": "1"}],
         "asks": [{"price": "101", "size": "1"}]},
        snapshot=True, received_ts=100.0)
    assert b.quality(0.2, now=100.3).reason == "connection_stale"
    b.mark_disconnected("test close")
    quality = b.quality(1.0, now=100.3)
    assert quality.reason == "disconnected"
    assert b.last_disconnect_reason == "test close"


def test_book_quality_rejects_crossed_or_locked_book():
    crossed = make_book(bids=[(101.0, 1.0)], asks=[(100.0, 1.0)])
    assert crossed.quality(1.0).reason == "crossed_book"
    locked = make_book(bids=[(100.0, 1.0)], asks=[(100.0, 1.0)])
    assert locked.quality(1.0).reason == "crossed_book"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"{name:40s} OK")
