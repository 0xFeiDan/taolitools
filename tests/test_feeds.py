"""Market-data timestamp extraction without opening network connections."""
import asyncio

from entropy_arb.book import OrderBook
from entropy_arb.feeds import HLBookFeed, LighterBookFeed, _epoch_seconds


def test_epoch_units_are_normalized():
    assert _epoch_seconds(1_700_000_000) == 1_700_000_000
    assert _epoch_seconds(1_700_000_000_000) == 1_700_000_000
    assert _epoch_seconds(1_700_000_000_000_000) == 1_700_000_000
    assert _epoch_seconds("bad") is None


def test_hyperliquid_documented_time_is_stored():
    book = OrderBook()
    feed = HLBookFeed("ENTROPY", "wss://unused", "io:SNDK", book,
                      notify=lambda: None)
    feed._on_frame({
        "channel": "l2Book",
        "data": {
            "coin": "io:SNDK",
            "time": 1_700_000_000_000,
            "levels": [[{"px": "100", "sz": "2"}],
                       [{"px": "101", "sz": "3"}]],
        },
    }, received_ts=1_700_000_000.025)
    assert book.exchange_ts == 1_700_000_000.0
    assert abs(book.exchange_lag_ms() - 25.0) < 0.001
    assert book.connection_state == "live"


def test_lighter_uses_local_time_when_server_time_absent():
    class StubWs:
        async def send(self, _message):
            pass

    book = OrderBook()
    feed = LighterBookFeed("RH", "wss://unused", 7, book,
                           notify=lambda: None)
    asyncio.run(feed._handle_book(
        StubWs(),
        {"channel": "order_book:7", "order_book": {
            "nonce": 1,
            "bids": [{"price": "100", "size": "2"}],
            "asks": [{"price": "101", "size": "3"}],
        }},
        snapshot=True, received_ts=500.125))
    assert book.last_update_ts == 500.125
    assert book.exchange_ts is None
    assert book.exchange_lag_ms() is None
