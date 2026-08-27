"""Market-data timestamp extraction without opening network connections."""
import asyncio
import math

from entropy_arb.book import OrderBook
from entropy_arb.feeds import HLBookFeed, LighterBookFeed, _epoch_seconds


def test_epoch_units_are_normalized():
    assert _epoch_seconds(1_700_000_000) == 1_700_000_000
    assert _epoch_seconds(1_700_000_000_000) == 1_700_000_000
    assert _epoch_seconds(1_700_000_000_000_000) == 1_700_000_000
    assert _epoch_seconds("bad") is None
    assert _epoch_seconds(math.nan) is None
    assert _epoch_seconds(math.inf) is None


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


def test_lighter_delta_gap_clears_book_and_resubscribes():
    class StubWs:
        def __init__(self):
            self.messages = []

        async def send(self, message):
            self.messages.append(message)

    ws = StubWs()
    book = OrderBook()
    feed = LighterBookFeed("RH", "wss://unused", 7, book,
                           notify=lambda: None)
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7",
        "order_book": {"nonce": 10,
                       "bids": [{"price": "100", "size": "1"}],
                       "asks": [{"price": "101", "size": "1"}]},
    }, snapshot=True))
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7",
        "order_book": {"begin_nonce": 12, "nonce": 12,
                       "bids": [{"price": "99", "size": "1"}]},
    }, snapshot=False))
    assert not book.ready and not book.bids and not book.asks
    assert len(ws.messages) == 2


def test_lighter_duplicate_or_out_of_order_delta_cannot_revert_book():
    class StubWs:
        async def send(self, _message):
            pass

    ws = StubWs()
    book = OrderBook()
    feed = LighterBookFeed("RH", "wss://unused", 7, book,
                           notify=lambda: None)
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7", "order_book": {
            "nonce": 10, "bids": [{"price": "100", "size": "1"}],
            "asks": [{"price": "101", "size": "1"}]},
    }, snapshot=True))
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7", "order_book": {
            "begin_nonce": 11, "nonce": 11,
            "bids": [{"price": "100", "size": "2"}]},
    }, snapshot=False))
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7", "order_book": {
            "begin_nonce": 10, "nonce": 10,
            "bids": [{"price": "100", "size": "99"}]},
    }, snapshot=False))
    assert book.bids[100.0] == 2.0


def test_lighter_old_snapshot_cannot_replace_newer_nonce_state():
    class StubWs:
        async def send(self, _message):
            pass

    ws = StubWs()
    book = OrderBook()
    feed = LighterBookFeed("RH", "wss://unused", 7, book,
                           notify=lambda: None)
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7", "order_book": {
            "nonce": 20, "bids": [{"price": "100", "size": "1"}],
            "asks": [{"price": "101", "size": "1"}]},
    }, snapshot=True))
    asyncio.run(feed._handle_book(ws, {
        "channel": "order_book:7", "order_book": {
            "nonce": 19, "bids": [{"price": "1", "size": "1"}],
            "asks": [{"price": "2", "size": "1"}]},
    }, snapshot=True))
    assert book.best_bid() == 100.0 and book.best_ask() == 101.0


def test_hyperliquid_old_exchange_timestamp_cannot_replace_new_book():
    book = OrderBook()
    feed = HLBookFeed("ENTROPY", "wss://unused", "io:SNDK", book,
                      notify=lambda: None)
    fresh = {"channel": "l2Book", "data": {
        "coin": "io:SNDK", "time": 1_700_000_000_000,
        "levels": [[{"px": "100", "sz": "1"}],
                   [{"px": "101", "sz": "1"}]]}}
    old = {"channel": "l2Book", "data": {
        "coin": "io:SNDK", "time": 1_699_999_999_000,
        "levels": [[{"px": "1", "sz": "1"}],
                   [{"px": "2", "sz": "1"}]]}}
    feed._on_frame(fresh, received_ts=1_700_000_000.025)
    feed._on_frame(old, received_ts=1_700_000_001.0)
    assert book.best_bid() == 100.0
    assert book.best_ask() == 101.0


def test_hyperliquid_missing_or_nonfinite_exchange_time_is_ignored():
    book = OrderBook()
    feed = HLBookFeed("ENTROPY", "wss://unused", "io:SNDK", book,
                      notify=lambda: None)
    for bad_time in (None, "NaN", "Infinity"):
        feed._on_frame({"channel": "l2Book", "data": {
            "coin": "io:SNDK", "time": bad_time,
            "levels": [[{"px": "100", "sz": "1"}],
                       [{"px": "101", "sz": "1"}]]}})
    assert not book.ready


def test_hyperliquid_far_future_timestamp_is_ignored_without_poisoning_sequence():
    book = OrderBook()
    feed = HLBookFeed("ENTROPY", "wss://unused", "io:SNDK", book,
                      notify=lambda: None)
    levels = [[{"px": "100", "sz": "1"}],
              [{"px": "101", "sz": "1"}]]
    feed._on_frame({"channel": "l2Book", "data": {
        "coin": "io:SNDK", "time": 1_700_001_000_000,
        "levels": levels}}, received_ts=1_700_000_000.0)
    assert not book.ready
    feed._on_frame({"channel": "l2Book", "data": {
        "coin": "io:SNDK", "time": 1_700_000_000_000,
        "levels": levels}}, received_ts=1_700_000_000.025)
    assert book.ready
    assert book.exchange_ts == 1_700_000_000.0
