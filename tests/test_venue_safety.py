import asyncio
import sys
from types import SimpleNamespace

from entropy_arb.venue_lighter import AccountOrdersFeed, LighterVenue


def test_lighter_dispatch_exception_is_unknown(monkeypatch):
    class FakeSignerClient:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=FakeSignerClient))

    class Signer:
        async def create_order(self, **_kwargs):
            raise asyncio.TimeoutError("response lost")

    venue = object.__new__(LighterVenue)
    venue.signer = Signer()
    venue.orders_feed = None
    venue.market_id = 7
    venue.size_decimals = 4
    venue.price_decimals = 2
    venue._coi = 100
    result = asyncio.run(venue.send_taker(
        is_buy=True, qty=1.0, limit_px=100.0))
    assert result["unresolved"] is True
    assert result["filled_base"] == 0.0
    assert "response lost" not in result["err"]


def test_lighter_transport_error_or_5xx_response_is_unknown(monkeypatch):
    class FakeSignerClient:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=FakeSignerClient))

    class Signer:
        def __init__(self, response):
            self.response = response

        async def create_order(self, **_kwargs):
            return self.response

    async def send(response):
        venue = object.__new__(LighterVenue)
        venue.signer = Signer(response)
        venue.orders_feed = None
        venue.market_id = 7
        venue.size_decimals = 4
        venue.price_decimals = 2
        venue._coi = 100
        return await venue.send_taker(is_buy=True, qty=1.0, limit_px=100.0)

    transport = asyncio.run(send((None, None, "connection reset")))
    server = asyncio.run(send((None, SimpleNamespace(code=503), None)))
    assert transport["unresolved"] is True
    assert server["unresolved"] is True


def test_account_order_duplicates_and_out_of_order_lower_fill_are_idempotent():
    feed = AccountOrdersFeed("RH", "wss://unused", 7, 1, signer=None)

    async def scenario():
        future = feed.watch(55)
        terminal = {"type": "update/account_orders", "orders": {"7": [{
            "status": "filled", "client_order_index": 55,
            "filled_base_amount": "2", "filled_quote_amount": "200"}]}}
        duplicate = {"type": "update/account_orders", "orders": {"7": [{
            "status": "filled", "client_order_index": 55,
            "filled_base_amount": "2", "filled_quote_amount": "200"}]}}
        older = {"type": "update/account_orders", "orders": {"7": [{
            "status": "canceled", "client_order_index": 55,
            "filled_base_amount": "1", "filled_quote_amount": "100"}]}}
        feed._handle_orders(terminal)
        feed._handle_orders(duplicate)
        feed._handle_orders(older)
        return await future

    result = asyncio.run(scenario())
    assert result["filled_base"] == 2.0
    assert feed._terminal[55]["filled_base"] == 2.0


def test_lighter_waits_for_delayed_account_order_settlement(monkeypatch):
    class FakeSignerClient:
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 2
        DEFAULT_IOC_EXPIRY = 3

    monkeypatch.setitem(
        sys.modules, "lighter", SimpleNamespace(SignerClient=FakeSignerClient))

    class Signer:
        async def create_order(self, **_kwargs):
            return None, SimpleNamespace(code=200), None

    venue = object.__new__(LighterVenue)
    venue.name = "RH"
    venue.signer = Signer()
    venue.market_id = 7
    venue.size_decimals = 4
    venue.price_decimals = 2
    venue.settle_timeout = 1.0
    venue._coi = 100
    venue.orders_feed = AccountOrdersFeed(
        "RH", "wss://unused", 7, 1, signer=venue.signer)

    async def scenario():
        task = asyncio.create_task(venue.send_taker(
            is_buy=True, qty=2.0, limit_px=100.0))
        await asyncio.sleep(0.01)
        assert not task.done()
        venue.orders_feed._handle_orders({
            "type": "update/account_orders", "orders": {"7": [{
                "status": "filled", "client_order_index": 101,
                "filled_base_amount": "2",
                "filled_quote_amount": "201"}]}})
        return await task

    result = asyncio.run(scenario())
    assert result["unresolved"] is False
    assert result["filled_base"] == 2.0
    assert result["avg_px"] == 100.5
