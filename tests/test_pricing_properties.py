import math

from hypothesis import given, settings, strategies as st

from entropy_arb.pricing import find_max_executable_size


level = st.tuples(
    st.floats(min_value=1.0, max_value=10_000.0,
              allow_nan=False, allow_infinity=False),
    st.floats(min_value=0.001, max_value=100.0,
              allow_nan=False, allow_infinity=False),
)


@settings(max_examples=150, deadline=None)
@given(
    asks=st.lists(level, min_size=1, max_size=8),
    bids=st.lists(level, min_size=1, max_size=8),
    step=st.sampled_from((0.001, 0.01, 0.1, 1.0)),
    max_base=st.floats(min_value=0.001, max_value=50.0,
                       allow_nan=False, allow_infinity=False),
)
def test_vwap_sizing_safety_properties(asks, bids, step, max_base):
    kwargs = dict(
        min_order_usd=1.0,
        max_order_usd=50_000.0,
        min_base=0.0,
        size_step=step,
        max_base=max_base,
        required_net_edge_bps=-9_000.0,
        max_vwap_slippage_bps=20_000.0,
        max_book_impact_bps=20_000.0,
        buy_fee_bps=0.0,
        sell_fee_bps=0.0,
    )
    first, first_reason = find_max_executable_size(asks, bids, **kwargs)
    second, second_reason = find_max_executable_size(asks, bids, **kwargs)
    assert (first, first_reason) == (second, second_reason)
    if first is None:
        return

    edge = first.edge
    assert math.isfinite(edge.qty) and edge.qty > 0
    assert edge.qty <= sum(size for _, size in asks) + 1e-9
    assert edge.qty <= sum(size for _, size in bids) + 1e-9
    assert edge.qty <= max_base + 1e-9
    assert abs(edge.qty / step - round(edge.qty / step)) <= 1e-7
    assert edge.buy.complete and edge.sell.complete
    assert edge.buy.filled_qty <= sum(size for _, size in asks) + 1e-9
    assert edge.sell.filled_qty <= sum(size for _, size in bids) + 1e-9
    assert edge.buy.notional_usd <= kwargs["max_order_usd"] + 1e-7
    assert edge.sell.notional_usd <= kwargs["max_order_usd"] + 1e-7
    assert edge.expected_net_edge_bps >= kwargs["required_net_edge_bps"] - 1e-7


def test_nonfinite_book_levels_never_reach_a_vwap_order():
    result, reason = find_max_executable_size(
        [(100.0, 1.0), (math.inf, 10.0), (101.0, math.nan)],
        [(101.0, 1.0), (math.nan, 10.0), (100.0, math.inf)],
        min_order_usd=1.0, max_order_usd=1_000.0,
        min_base=0.0, size_step=0.01, required_net_edge_bps=0.0,
        max_vwap_slippage_bps=100.0, max_book_impact_bps=100.0,
        buy_fee_bps=0.0, sell_fee_bps=0.0)
    assert reason == "ok"
    assert result.edge.qty == 1.0
    assert all(math.isfinite(value) for value in (
        result.edge.qty, result.edge.buy.vwap, result.edge.sell.vwap))


@settings(max_examples=100, deadline=None)
@given(
    price=st.floats(min_value=1.0, max_value=100_000.0,
                    allow_nan=False, allow_infinity=False),
    size=st.floats(min_value=1.0, max_value=100.0,
                   allow_nan=False, allow_infinity=False),
    discount=st.floats(min_value=0.0001, max_value=0.5,
                       allow_nan=False, allow_infinity=False),
)
def test_nonpositive_executable_edge_never_returns_an_order(
        price, size, discount):
    result, reason = find_max_executable_size(
        [(price, size)], [(price * (1.0 - discount), size)],
        min_order_usd=1.0, max_order_usd=1_000_000.0,
        min_base=0.0, size_step=0.001,
        required_net_edge_bps=0.0,
        max_vwap_slippage_bps=100.0,
        max_book_impact_bps=100.0,
        buy_fee_bps=0.0, sell_fee_bps=0.0)
    assert result is None
    assert reason in {"net_edge", "insufficient_depth"}
