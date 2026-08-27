"""Websocket order-book feeds, writing into entropy_arb.book.OrderBook.

Two protocols, one per exchange family:

LighterBookFeed: zkLighter order_book channel (snapshot + diffs, server
    pings, diff-nonce gap detection — a gapped book is dropped and
    resubscribed rather than traded as a fiction).
HLBookFeed: the official Hyperliquid websocket (wss://api.hyperliquid.xyz/ws)
    l2Book channel with fast snapshots and client app-pings. Every price this
    bot trades on comes straight from the exchange that will fill the order.

Both retain connection heartbeat timestamps and actual book-update timestamps.
Hyperliquid's documented millisecond ``time`` is also stored; the official
Lighter websocket client does not expose a server timestamp, so its exchange
timestamp remains unknown rather than being fabricated.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook

log = logging.getLogger("feeds")


def _epoch_seconds(value) -> Optional[float]:
    """Normalize common epoch units to seconds; unknown values stay absent."""
    if value in (None, ""):
        return None
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    magnitude = abs(ts)
    if magnitude >= 1e17:       # nanoseconds
        return ts / 1e9
    if magnitude >= 1e14:       # microseconds
        return ts / 1e6
    if magnitude >= 1e11:       # milliseconds (documented Hyperliquid unit)
        return ts / 1e3
    return ts


def _lighter_exchange_ts(msg: dict, ob: dict) -> Optional[float]:
    """Accept a future timestamp field without assuming one exists today."""
    for source in (ob, msg):
        for key in ("timestamp", "time", "ts"):
            if key in source:
                return _epoch_seconds(source.get(key))
    return None


def _chan_id(channel: str) -> Optional[int]:
    """'order_book:32' / 'order_book/32' -> 32."""
    for sep in (":", "/"):
        if sep in channel:
            try:
                return int(channel.rsplit(sep, 1)[1])
            except ValueError:
                return None
    return None


class LighterBookFeed:
    """zkLighter order book for one market over one connection."""

    def __init__(self, name: str, ws_url: str, market_id: int, book: OrderBook,
                 notify: Callable[[], None]) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.book = book
        self.notify = notify
        self._nonce: Optional[int] = None
        self._synced = False

    async def _subscribe(self, ws) -> None:
        await ws.send(json.dumps({"type": "subscribe",
                                  "channel": f"order_book/{self.market_id}"}))

    async def _handle_book(self, ws, msg: dict, snapshot: bool, *,
                           received_ts: Optional[float] = None) -> None:
        if _chan_id(msg.get("channel", "")) != self.market_id:
            return
        ob = msg["order_book"]
        if snapshot:
            self._nonce = ob.get("nonce")
            self._synced = True
            self.book.apply_lighter(
                ob, snapshot=True, received_ts=received_ts,
                exchange_ts=_lighter_exchange_ts(msg, ob))
            log.info("[%s] snapshot: %d bids / %d asks", self.name,
                     len(self.book.bids), len(self.book.asks))
            self.notify()
            return
        # diff: a skipped nonce means we lost a level update — the book is now
        # a fiction. Drop it and resubscribe rather than quote off a ghost.
        if not self._synced:
            return  # no snapshot yet (fresh connection, or one pending after a gap)
        prev, begin, end = self._nonce, ob.get("begin_nonce"), ob.get("nonce")
        if prev is not None and begin is not None and begin > prev + 1:
            log.warning("[%s] diff gap (had %s, got %s) — resubscribing",
                        self.name, prev, begin)
            self._nonce = None
            self._synced = False
            self.book.clear()
            self.book.mark_connected()
            self.notify()
            await ws.send(json.dumps({"type": "unsubscribe",
                                      "channel": f"order_book/{self.market_id}"}))
            await self._subscribe(ws)
            return
        if end is not None:
            self._nonce = end
        self.book.apply_lighter(
            ob, snapshot=False, received_ts=received_ts,
            exchange_ts=_lighter_exchange_ts(msg, ob))
        self.notify()

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            self.book.mark_connecting()
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (%s)", self.name, self.ws_url)
                    self.book.clear()
                    self.book.mark_connected()
                    self._nonce = None
                    self._synced = False
                    async for raw in ws:
                        backoff = 1.0
                        received_ts = time.time()
                        msg = json.loads(raw)
                        t = msg.get("type")
                        self.book.touch(received_ts)
                        if t == "update/order_book":
                            await self._handle_book(
                                ws, msg, snapshot=False,
                                received_ts=received_ts)
                        elif t == "subscribed/order_book":
                            await self._handle_book(
                                ws, msg, snapshot=True,
                                received_ts=received_ts)
                        elif t == "connected":
                            await self._subscribe(ws)
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            self.book.mark_disconnected("websocket closed")
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


class HLBookFeed:
    """Official Hyperliquid l2Book consumer for one coin (e.g. 'io:SNDK')."""

    def __init__(self, name: str, ws_url: str, coin: str, book: OrderBook,
                 notify: Callable[[], None], ping_sec: float = 5.0) -> None:
        self.name = name
        self.ws_url = ws_url
        self.coin = coin
        self.book = book
        self.notify = notify
        self.ping_sec = ping_sec
        self._snapped = False

    def _on_frame(self, msg: dict,
                  received_ts: Optional[float] = None) -> None:
        received_ts = time.time() if received_ts is None else received_ts
        self.book.touch(received_ts)
        if msg.get("channel") == "l2Book":
            d = msg.get("data") or {}
            if d.get("coin") == self.coin:
                self.book.apply_hl(
                    d["levels"], received_ts=received_ts,
                    exchange_ts=_epoch_seconds(d.get("time")))
                if not self._snapped:
                    self._snapped = True
                    log.info("[%s] snapshot: %d bids / %d asks", self.name,
                             len(self.book.bids), len(self.book.asks))
                self.notify()

    async def _pinger(self, ws) -> None:
        try:
            while True:
                await asyncio.sleep(self.ping_sec)
                await ws.send(json.dumps({"method": "ping"}))
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            ptask = None
            self.book.mark_connecting()
            try:
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    log.info("[%s] connected (official ws, %s)", self.name, self.coin)
                    self.book.clear()
                    self.book.mark_connected()
                    self._snapped = False
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "subscription": {"type": "l2Book", "coin": self.coin,
                                         "fast": True}}))
                    ptask = asyncio.create_task(self._pinger(ws))
                    async for raw in ws:
                        backoff = 1.0
                        self._on_frame(json.loads(raw), received_ts=time.time())
                        if stop.is_set():
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] ws error: %s — reconnect in %.0fs",
                            self.name, e, backoff)
            finally:
                if ptask is not None:
                    ptask.cancel()
            self.book.mark_disconnected("websocket closed")
            self.notify()
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
