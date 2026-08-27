import asyncio
import math
import time

import pytest

from entropy_arb.costs import CostMonitor
from entropy_arb.pricing import executable_edge


class FakeResponse:
    def __init__(self, payload, *, status=200):
        self.payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.responses.pop(0)


def kraken_book(pair, bid, ask, *, bid_ts, ask_ts):
    return {
        "error": [],
        "result": {
            pair: {
                "bids": [[str(bid), "100", bid_ts]],
                "asks": [[str(ask), "100", ask_ts]],
            }
        },
    }


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


def test_kraken_direct_usdg_usdc_book_preserves_executable_sides():
    now = time.time()
    session = FakeSession([
        FakeResponse(kraken_book(
            "USDCUSD", 0.9998, 1.0, bid_ts=now - 2, ask_ts=now - 1)),
        FakeResponse(kraken_book(
            "USDGUSDC", 1.0000, 1.0010,
            bid_ts=now - 600, ask_ts=now - 300)),
    ])
    costs = monitor(funding_enabled=False)

    before = time.time()
    asyncio.run(costs.refresh_stablecoins(
        session, "https://api.kraken.com"))

    assert costs.quote_usd["USDC"].value == pytest.approx(0.9999)
    assert costs.quote_usd["USDG"].value == pytest.approx(
        0.9999 * 1.0005)
    assert costs.quote_usd["USDC"].observed_at >= before
    assert costs.quote_usd["USDG"].observed_at >= before
    assert costs.fresh_quote_pair("USDG", "USDC") == pytest.approx(
        (1.0, 1.001))
    assert all(request[1]["params"]["count"] == "1"
               for request in session.requests)
    assert [request[1]["params"]["pair"] for request in session.requests] == [
        "USDCUSD", "USDGUSDC"]
    # BUY hedge spends USDG at the cross ask; SELL hedge receives the bid.
    assert costs.directional_quote_rates(
        buy_key="hedge", sell_key="entropy") == pytest.approx(
            (1.001 * 0.9999, 0.9999))
    assert costs.directional_quote_rates(
        buy_key="entropy", sell_key="hedge") == pytest.approx(
            (0.9999, 1.0 * 0.9999))
    assert costs.stablecoin_basis_cost_bps(
        buy_key="hedge", sell_key="entropy") == pytest.approx(
            (1.0 - 1.0 / 1.001) * 1e4)


@pytest.mark.parametrize("bad_payload", [
    {"error": ["EQuery:Unknown asset pair"], "result": {}},
    kraken_book("WRONGPAIR", 0.9999, 1.0,
                bid_ts=time.time(), ask_ts=time.time()),
    kraken_book("USDGUSDC", 1.0002, 1.0,
                bid_ts=time.time(), ask_ts=time.time()),
    kraken_book("USDGUSDC", "NaN", 1.0,
                bid_ts=time.time(), ask_ts=time.time()),
    kraken_book("USDGUSDC", 0.99, 1.01,
                bid_ts=time.time(), ask_ts=time.time()),
])
def test_kraken_stablecoin_refresh_rejects_unsafe_book_without_partial_update(
        bad_payload):
    now = time.time()
    session = FakeSession([
        FakeResponse(kraken_book(
            "USDCUSD", 0.9998, 1.0, bid_ts=now, ask_ts=now)),
        FakeResponse(bad_payload),
    ])
    costs = monitor(funding_enabled=False)

    asyncio.run(costs.refresh_stablecoins(
        session, "https://api.kraken.com"))

    assert "USDC" not in costs.quote_usd
    assert "USDG" not in costs.quote_usd
    assert costs.pause_reason() == "stablecoin_stale:USDC"


def test_exact_ten_bps_direct_cross_is_accepted_despite_old_resting_levels():
    now = time.time()
    session = FakeSession([
        FakeResponse(kraken_book(
            "USDCUSD", 0.9999, 1.0, bid_ts=now - 500, ask_ts=now - 400)),
        FakeResponse(kraken_book(
            "USDGUSDC", 1.0, 1.001, bid_ts=now - 3600,
            ask_ts=now - 1800)),
    ])
    costs = monitor(funding_enabled=False)

    asyncio.run(costs.refresh_stablecoins(
        session, "https://api.kraken.com"))

    assert costs.fresh_quote_pair("USDG", "USDC") == pytest.approx(
        (1.0, 1.001))
