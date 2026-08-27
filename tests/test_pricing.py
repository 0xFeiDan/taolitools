"""Current-orderbook VWAP, executable-edge, and sizing tests."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.pricing import (  # noqa: E402
    executable_edge,
    find_max_executable_size,
    simulate_vwap,
)


def approx(actual, expected, tolerance=1e-9):
    assert abs(actual - expected) <= tolerance, (actual, expected)


def sizing(asks, bids, **overrides):
    params = dict(
        min_order_usd=100.0,
        max_order_usd=10_000.0,
        min_base=0.01,
        size_step=0.01,
        required_net_edge_bps=0.0,
        max_vwap_slippage_bps=100.0,
        max_book_impact_bps=100.0,
        buy_fee_bps=0.0,
        sell_fee_bps=0.0,
    )
    params.update(overrides)
    return find_max_executable_size(asks, bids, **params)


def test_buy_vwap_walks_multiple_levels_and_sorts_input():
    fill = simulate_vwap([(101.0, 2.0), (100.0, 1.0)], 2.0, side="buy")
    assert fill.complete
    approx(fill.vwap, 100.5)
    approx(fill.notional_usd, 201.0)
    approx(fill.vwap_slippage_bps, 50.0)
    approx(fill.book_impact_bps, 100.0)


def test_sell_vwap_uses_best_bid_as_slippage_reference():
    fill = simulate_vwap([(99.0, 2.0), (100.0, 1.0)], 2.0, side="sell")
    assert fill.complete
    approx(fill.vwap, 99.5)
    approx(fill.vwap_slippage_bps, 50.0)
    approx(fill.book_impact_bps, 100.0)


def test_vwap_reports_incomplete_when_visible_depth_is_insufficient():
    fill = simulate_vwap([(100.0, 1.0)], 2.0, side="buy")
    assert not fill.complete
    approx(fill.filled_qty, 1.0)
    assert executable_edge([(100.0, 1.0)], [(101.0, 1.0)], 2.0,
                           buy_fee_bps=0.0, sell_fee_bps=0.0) is None


def test_executable_edge_deducts_fees_and_explicit_costs_once():
    edge = executable_edge(
        [(100.0, 10.0)], [(100.2, 10.0)], 5.0,
        buy_fee_bps=5.0, sell_fee_bps=5.0,
        safety_buffer_bps=2.0, expected_latency_cost_bps=1.0)
    approx(edge.gross_edge_bps, 20.0)
    approx(edge.fee_cost_bps, 10.01)
    approx(edge.extra_cost_bps, 3.0)
    approx(edge.expected_net_edge_bps, 6.99)
    approx(edge.expected_net_profit_usd, 0.3495)


def test_binary_sizing_finds_largest_step_that_keeps_required_edge():
    result, reason = sizing(
        [(100.1, 10.0), (100.0, 10.0)],
        [(100.0, 10.0), (100.2, 10.0)],
        size_step=1.0, required_net_edge_bps=10.0,
        max_order_usd=2_000.0)
    assert reason == "ok"
    # q=14 clears 10 bps; q=15 is just below it.
    approx(result.edge.qty, 14.0)
    assert result.edge.expected_net_edge_bps > 10.0


def test_sizing_respects_vwap_slippage_and_book_impact_limits():
    asks = [(100.0, 10.0), (100.1, 10.0)]
    bids = [(100.3, 10.0), (100.2, 10.0)]
    by_slip, _ = sizing(
        asks, bids, size_step=1.0, max_vwap_slippage_bps=4.0,
        max_book_impact_bps=100.0, max_order_usd=2_000.0)
    by_impact, _ = sizing(
        asks, bids, size_step=1.0, max_vwap_slippage_bps=100.0,
        max_book_impact_bps=5.0, max_order_usd=2_000.0)
    assert by_slip.edge.qty < 20.0
    # Entering the second ask level impacts by 10 bps, so only level one fits.
    approx(by_impact.edge.qty, 10.0)


def test_sizing_rejects_thin_book_below_minimum_order():
    result, reason = sizing(
        [(100.0, 0.5)], [(101.0, 0.5)], min_order_usd=100.0)
    assert result is None
    assert reason == "insufficient_depth"


def test_minimum_notional_is_derived_from_walked_depth_not_top_price():
    result, reason = sizing(
        [(80.0, 10.0)],
        [(100.0, 0.1), (90.0, 10.0)],
        min_order_usd=100.0, max_order_usd=200.0,
        size_step=0.1, max_vwap_slippage_bps=2_000.0,
        max_book_impact_bps=2_000.0)
    assert reason == "ok"
    assert result.edge.sell.notional_usd >= 100.0
    assert result.edge.buy.notional_usd <= 200.0


def test_cost_buffer_can_reject_an_otherwise_visible_edge():
    result, reason = sizing(
        [(100.0, 10.0)], [(100.1, 10.0)],
        required_net_edge_bps=5.0, safety_buffer_bps=6.0)
    assert result is None
    assert reason == "net_edge"


def test_sizing_hard_caps_base_quantity_for_pair_exit():
    result, reason = sizing(
        [(100.0, 100.0)], [(101.0, 100.0)],
        max_order_usd=10_000.0, size_step=0.1, max_base=2.35)
    assert reason == "ok"
    # Never rounds above the remaining pair position.
    assert result.edge.qty == 2.3


def test_sizing_min_max_notional_are_converted_from_quote_to_usd():
    result, reason = sizing(
        [(100.0, 10.0)], [(100.1, 10.0)],
        min_order_usd=10.0, max_order_usd=100.0,
        size_step=0.1, buy_quote_usd=0.5, sell_quote_usd=0.5)
    assert reason == "ok"
    # 1.9 base is 190 quote units but USD 95 at a 0.5 quote rate.
    approx(result.edge.qty, 1.9)
    assert result.edge.buy.notional_usd > 100.0
    assert result.edge.buy.notional_usd * 0.5 <= 100.0
