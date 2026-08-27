import time

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
