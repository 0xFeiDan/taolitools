"""Counterexamples and invariants for strategy claims in the documentation."""

from entropy_arb.book import OrderBook, plan_arb


def book(*, bid: float, ask: float) -> OrderBook:
    result = OrderBook()
    result.apply_hl([
        [{"px": str(bid), "sz": "1"}],
        [{"px": str(ask), "sz": "1"}],
    ])
    return result


def plan(buy: OrderBook, sell: OrderBook, threshold_bps: float):
    result, reason = plan_arb(
        buy, sell, threshold_bps=threshold_bps,
        buy_fee_bps=0.0, sell_fee_bps=0.0, take_fraction=1.0,
        cap_notional=1_000_000.0, min_base=0.0,
        min_notional=0.0, size_step=1.0)
    assert reason == "ok"
    return result


def test_static_band_traversal_is_not_an_unconditional_dollar_profit_floor():
    # midline=5, upper=4: open SELL Entropy at a 9.1 bps premium.
    opening = plan(
        buy=book(bid=99.99, ask=100.0),
        sell=book(bid=100.091, ask=100.10),
        threshold_bps=9.0)
    # lower=3: close via the reverse direction at -1.99 bps. The common
    # price level rose 10x, so the close-side dollar loss exceeds entry gain.
    closing = plan(
        buy=book(bid=999.99, ask=1000.0),
        sell=book(bid=999.801, ask=999.90),
        threshold_bps=-2.0)

    assert opening.qty == closing.qty == 1.0
    assert opening.exp_edge_usd + closing.exp_edge_usd < 0.0
