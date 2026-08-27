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


def test_hl_fill_history_vwap_is_weighted_and_order_scoped():
    summary = HLVenue._fill_summary_for_oid([
        {"oid": 7, "px": "100", "sz": "2"},
        {"oid": 7, "px": "101", "sz": "1"},
        {"oid": 8, "px": "999", "sz": "9"},
    ], 7)
    assert summary == (3.0, 301.0 / 3.0)


def test_hl_fill_history_deduplicates_repeated_trade_ids():
    summary = HLVenue._fill_summary_for_oid([
        {"oid": 7, "tid": 10, "px": "100", "sz": "2"},
        {"oid": 7, "tid": 10, "px": "100", "sz": "2"},
        {"oid": 7, "tid": 11, "px": "101", "sz": "1"},
    ], 7)
    assert summary == (3.0, 301.0 / 3.0)


def test_hl_malformed_success_response_remains_unknown():
    result = HLVenue._parse({"status": "ok", "response": {"data": {}}})
    assert result["unresolved"] is True


def test_hl_http_429_after_dispatch_is_unknown():
    class Response:
        status = 429

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return "rate limited"

    class Session:
        def post(self, *_args, **_kwargs):
            return Response()

    venue = object.__new__(HLVenue)
    venue.session = Session()
    venue.api_url = "https://unused"
    body, err, unresolved = asyncio.run(venue._post_exchange({"order": {}}))
    assert body is None
    assert err.startswith("RATE_LIMITED")
    assert unresolved is True


def test_hl_unknown_response_recovers_actual_vwap_from_fill_history():
    venue = object.__new__(HLVenue)
    venue.coin = "io:SNDK"
    venue.asset_id = 1
    venue.settle_timeout = 1.0
    venue.account = SimpleNamespace(
        query_address="0xabc", wallet=object(), is_mainnet=True,
        nonces=SimpleNamespace(next=lambda: 1))
    venue._signing = SimpleNamespace(
        order_request_to_order_wire=lambda request, asset_id: {},
        order_wires_to_order_action=lambda wires: {},
        sign_l1_action=lambda *args: {})
    venue._next_cloid = lambda: SimpleNamespace(to_raw=lambda: "0x01")

    async def post_exchange(payload):
        return None, None, True

    async def info(payload):
        if payload["type"] == "orderStatus":
            return {"status": "order", "order": {
                "status": "filled", "order": {
                    "oid": 77, "origSz": "3", "sz": "0"}}}
        assert payload["type"] == "userFillsByTime"
        return [
            {"oid": 77, "px": "100", "sz": "2"},
            {"oid": 77, "px": "101", "sz": "1"},
        ]

    venue._post_exchange = post_exchange
    venue._info = info
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=3.0, limit_px=102.0))
    assert result["filled_base"] == 3.0
    assert result["avg_px"] == 301.0 / 3.0
    assert result["accounting_complete"] is True
    assert result["unresolved"] is False


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
