import asyncio
from types import SimpleNamespace

from entropy_arb.venue_hl import HLVenue
from entropy_arb.venue_lighter import LighterVenue


def test_hl_current_funding_and_account_history_are_parsed():
    venue = object.__new__(HLVenue)
    venue.name = "ENTROPY"
    venue.coin = "io:SNDK"
    venue.conf = SimpleNamespace(hl_dex="io")
    venue._query_address = lambda: "0xabc"

    async def info(payload):
        if payload["type"] == "metaAndAssetCtxs":
            return [
                {"universe": [{"name": "io:SNDK"}]},
                [{"funding": "0.000125"}],
            ]
        return [
            {"delta": {"coin": "io:SNDK", "usdc": "-1.25"}},
            {"delta": {"coin": "io:OTHER", "usdc": "-99"}},
        ]

    venue._info = info
    assert asyncio.run(venue.fetch_funding_rate()) == 0.000125
    assert asyncio.run(venue.fetch_funding_cost_since(1000.0)) == 1.25


def test_lighter_funding_rate_and_history_are_parsed():
    venue = object.__new__(LighterVenue)
    venue.name = "RH"
    venue.market_id = 7
    venue.conf = SimpleNamespace(
        lighter_creds=SimpleNamespace(account_index=42))

    async def get(path, params=None):
        if path == "/api/v1/funding-rates":
            return {"funding_rates": [
                {"market_id": 7, "exchange": "binance", "rate": 0.08},
                {"market_id": 7, "exchange": "lighter", "rate": 0.0008},
            ]}
        return {"position_fundings": [
            {"funding_paid_out": "1.2"},
            {"change": "-0.3"},
        ]}

    venue._get = get
    assert abs(asyncio.run(venue.fetch_funding_rate()) - 0.0001) < 1e-12
    assert abs(asyncio.run(venue.fetch_funding_cost_since(1000.0)) - 1.5) < 1e-12
