import math
import time

import pytest

from entropy_arb.costs import CostMonitor
from entropy_arb.pricing import executable_edge


def monitor(**overrides):
    values = dict(
        funding_enabled=True, expected_holding_hours=2.0,
        funding_max_age_seconds=60.0, stablecoin_enabled=True,
        stablecoin_max_age_seconds=60.0, warning_deviation_bps=10.0,
        halt_deviation_bps=30.0,
        quote_assets={"entropy": "USDC", "hedge": "USDG"})
    values.update(overrides)
    return CostMonitor(**values)


def test_external_cost_observations_reject_nonfinite_values():
    costs = monitor()
    with pytest.raises(ValueError, match="finite"):
        costs.set_funding("entropy", math.nan)
    with pytest.raises(ValueError, match="finite"):
        costs.set_quote_usd("USDC", math.inf)
    with pytest.raises(ValueError, match="out of range"):
        costs.set_funding("entropy", 1.0)


def test_enabled_cost_inputs_fail_closed_when_missing_or_stale():
    costs = monitor()
    assert costs.pause_reason().startswith("funding_stale:")
    costs.set_funding("entropy", 0.0001)
    costs.set_funding("hedge", 0.0002)
    assert costs.pause_reason() == "stablecoin_stale:USDC"
    costs.set_quote_usd("USDC", 1.0)
    costs.set_quote_usd("USDG", 0.999)
    assert costs.pause_reason() is None
    costs.set_funding("entropy", 0.0, observed_at=time.time() - 61)
    assert costs.pause_reason() == "funding_stale:entropy"


def test_funding_direction_and_depeg_halt():
    costs = monitor()
    costs.set_funding("entropy", 0.0001)
    costs.set_funding("hedge", 0.0003)
    costs.set_quote_usd("USDC", 1.0)
    costs.set_quote_usd("USDG", 0.9969)
    # Sell Entropy is short Entropy + long hedge: -1bp/h +3bp/h for 2h.
    assert abs(costs.funding_cost_bps("sell_entropy") - 4.0) < 1e-12
    assert abs(costs.funding_cost_bps("buy_entropy") + 4.0) < 1e-12
    assert costs.pause_reason() == "stablecoin_depeg:USDG"


def test_executable_edge_normalizes_each_quote_asset_exactly():
    edge = executable_edge(
        [(100.0, 10.0)], [(100.5, 10.0)], 1.0,
        buy_fee_bps=0.0, sell_fee_bps=0.0,
        funding_cost_bps=2.0,
        buy_quote_usd=1.0, sell_quote_usd=0.997)
    assert edge is not None
    assert abs(edge.gross_edge_bps - 50.0) < 1e-9
    expected_adjusted = (100.5 * 0.997 / 100.0 - 1.0) * 1e4
    assert abs(edge.adjusted_gross_edge_bps - expected_adjusted) < 1e-9
    assert abs(edge.stablecoin_basis_bps
               - (edge.gross_edge_bps - expected_adjusted)) < 1e-9
    assert abs(edge.expected_net_edge_bps
               - (expected_adjusted - 2.0)) < 1e-9


def test_funding_uses_each_legs_actual_usd_notional():
    edge = executable_edge(
        [(100.0, 10.0)], [(101.0, 10.0)], 1.0,
        buy_fee_bps=0.0, sell_fee_bps=0.0,
        buy_quote_usd=0.99, sell_quote_usd=1.0,
        buy_funding_rate=0.0003, sell_funding_rate=0.0001,
        expected_holding_hours=2.0)
    assert edge is not None
    buy_usd = 100.0 * 0.99
    sell_usd = 101.0
    expected_funding = (buy_usd * 0.0003 - sell_usd * 0.0001) * 2.0
    expected_bps = expected_funding / buy_usd * 1e4
    assert abs(edge.funding_cost_bps - expected_bps) < 1e-12
    expected_profit = sell_usd - buy_usd - expected_funding
    assert abs(edge.expected_net_profit_usd - expected_profit) < 1e-12
