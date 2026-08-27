"""zkLighter venue adapter (Lighter mainnet / Lighter Robinhood chain).

Market data and account state come from Lighter's public REST + websocket
APIs via plain aiohttp/websockets, so --record-only data collection works
without the SDK. Trading lazily imports the official `lighter` SDK
(https://github.com/elliottech/lighter-python) for transaction signing only.

Market orders carry mandatory avg-execution-price protection and settle
asynchronously on the authenticated account_orders websocket; send_taker()
hides that behind the same result shape the HL venue returns:
{status, filled_base, avg_px, err, unresolved}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import OrderedDict
from typing import Optional

import aiohttp

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:
    from websockets import connect as ws_connect  # type: ignore

from .book import OrderBook
from .config import VenueConf
from .feeds import LighterBookFeed

log = logging.getLogger("lighter")

OPEN_STATUSES = {"in-progress", "pending", "open"}
TERMINAL_STATUS_PREFIXES = ("filled", "canceled", "cancelled", "rejected",
                            "expired")
AUTH_REFRESH_SEC = 8 * 60
REST_TIMEOUT = 10.0


def _timed_result(result: dict, *, order_send_ts: Optional[float] = None,
                  order_ack_ts: Optional[float] = None,
                  fill_ts: Optional[float] = None) -> dict:
    result.update({
        "order_send_ts": order_send_ts,
        "order_ack_ts": order_ack_ts,
        "first_fill_ts": fill_ts,
        "final_fill_ts": fill_ts,
    })
    return result


class AccountOrdersFeed:
    """Authenticated stream of our own order updates (settlement channel)."""

    def __init__(self, name: str, ws_url: str, market_id: int,
                 account_index: int, signer) -> None:
        self.name = name
        self.ws_url = ws_url
        self.market_id = market_id
        self.account_index = account_index
        self.signer = signer
        self.ready = asyncio.Event()
        self._pending: dict[int, asyncio.Future] = {}
        self._terminal: OrderedDict[int, dict] = OrderedDict()

    def watch(self, coi: int) -> asyncio.Future:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if coi in self._terminal:
            fut.set_result(self._terminal[coi])
            return fut
        self._pending[coi] = fut
        return fut

    def unwatch(self, coi: int) -> None:
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def _resolve(self, coi: int, info: dict) -> None:
        previous = self._terminal.get(coi)
        if previous is not None:
            old_fill = float(previous.get("filled_base") or 0.0)
            new_fill = float(info.get("filled_base") or 0.0)
            if new_fill <= old_fill:
                return
        self._terminal[coi] = info
        while len(self._terminal) > 512:
            self._terminal.popitem(last=False)
        fut = self._pending.pop(coi, None)
        if fut is not None and not fut.done():
            fut.set_result(info)

    def _handle_orders(self, msg: dict) -> None:
        for lst in (msg.get("orders") or {}).values():
            for o in lst or []:
                status = str(o.get("status", "")).strip().lower()
                if status in OPEN_STATUSES:
                    continue
                try:
                    coi = int(o.get("client_order_index"))
                except (TypeError, ValueError):
                    continue
                try:
                    fb = float(o.get("filled_base_amount") or 0.0)
                    fq = float(o.get("filled_quote_amount") or 0.0)
                except (TypeError, ValueError):
                    fb = fq = float("nan")
                valid_amounts = (math.isfinite(fb) and math.isfinite(fq)
                                 and fb >= 0 and fq >= 0)
                avg_px = fq / fb if valid_amounts and fb > 0 else None
                valid_price = (fb <= 0 or (avg_px is not None
                                           and math.isfinite(avg_px)
                                           and avg_px > 0))
                known_status = status.startswith(TERMINAL_STATUS_PREFIXES)
                unresolved = not (valid_amounts and valid_price and known_status)
                self._resolve(coi, {
                    "status": status or "unknown",
                    "filled_base": fb if valid_amounts else 0.0,
                    "filled_quote": fq if valid_amounts else 0.0,
                    "avg_px": avg_px if valid_price else None,
                    "unresolved": unresolved,
                    "observed_ts": time.time(),
                })

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                auth, err = self.signer.create_auth_token_with_expiry()
                if err is not None:
                    raise RuntimeError("account websocket auth token failed")
                connected_at = time.time()
                async with ws_connect(self.ws_url, max_size=2**23, open_timeout=10,
                                      ping_interval=15, ping_timeout=15) as ws:
                    async for raw in ws:
                        backoff = 1.0
                        msg = json.loads(raw)
                        t = msg.get("type")
                        if t in ("subscribed/account_orders", "update/account_orders"):
                            if t.startswith("subscribed"):
                                log.info("[%s] account orders stream ready", self.name)
                            self.ready.set()
                            self._handle_orders(msg)
                        elif t == "connected":
                            await ws.send(json.dumps({
                                "type": "subscribe",
                                "channel": f"account_orders/{self.market_id}/"
                                           f"{self.account_index}",
                                "auth": auth}))
                        elif t == "ping":
                            await ws.send(json.dumps({"type": "pong"}))
                        if stop.is_set():
                            break
                        if (time.time() - connected_at > AUTH_REFRESH_SEC
                                and not self._pending):
                            log.info("[%s] refreshing account ws auth", self.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("[%s] account ws error: %s — retry in %.0fs",
                            self.name, e, backoff)
                self.ready.clear()
                if stop.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            self.ready.clear()


class LighterVenue:
    kind = "lighter"

    def __init__(self, conf: VenueConf, session: aiohttp.ClientSession,
                 settle_timeout_sec: float) -> None:
        assert conf.lighter_profile is not None
        self.conf = conf
        self.key = conf.key
        self.name = conf.label
        self.session = session
        self.settle_timeout = settle_timeout_sec
        self.profile = conf.lighter_profile
        self.book = OrderBook()
        self.position = 0.0
        self.cash = 0.0
        self.accounting_complete = True
        self.volume_usd = 0.0     # cumulative filled notional this session
        self.equity = None
        self.free = None
        self.start_equity = None
        self.fee_bps = conf.fee_bps
        self.cap_usd = conf.cap_usd
        self.orders_per_min = conf.orders_per_min
        self.last_traded_ts = 0.0
        self.market_id = -1
        self.price_decimals = 2
        self.size_decimals = 4
        self.min_base = 0.0
        self.min_quote = 10.0
        self.signer = None
        self.orders_feed: Optional[AccountOrdersFeed] = None
        self._coi = time.time_ns() // 1000

    # ------------------------------------------------------------------ REST

    async def _get(self, path: str, params: Optional[dict] = None):
        async with self.session.get(
                self.profile.api_url + path, params=params,
                timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            r.raise_for_status()
            return await r.json()

    # ------------------------------------------------------------- lifecycle

    async def load_market(self) -> None:
        data = await self._get("/api/v1/orderBooks")
        for ob in data.get("order_books") or []:
            if ob.get("symbol") != self.conf.symbol:
                continue
            if ob.get("status") != "active":
                raise RuntimeError(f"[{self.name}] market status={ob.get('status')}")
            self.market_id = int(ob["market_id"])
            self.price_decimals = int(ob["supported_price_decimals"])
            self.size_decimals = int(ob["supported_size_decimals"])
            self.min_base = float(ob["min_base_amount"])
            self.min_quote = float(ob["min_quote_amount"])
            log.info("[%s] %s market_id=%d px_dec=%d sz_dec=%d min_base=%s "
                     "min_quote=%s taker_fee=%s", self.name, ob["symbol"],
                     self.market_id, self.price_decimals, self.size_decimals,
                     ob["min_base_amount"], ob["min_quote_amount"],
                     ob.get("taker_fee"))
            return
        raise RuntimeError(f"[{self.name}] {self.conf.symbol} not found on "
                           f"{self.profile.name}")

    def init_signer(self) -> None:
        c = self.conf.lighter_creds
        assert c is not None and c.complete, f"[{self.name}] missing credentials"
        try:
            from lighter import SignerClient
        except ImportError as e:
            raise RuntimeError(
                "live trading on Lighter needs the official SDK — "
                "pip install -r requirements-live.txt "
                "(git+https://github.com/elliottech/lighter-python.git)") from e
        signer = SignerClient(
            url=self.profile.api_url,
            account_index=c.account_index,
            api_private_keys={c.api_key_index: c.api_private_key},
            chain_id=self.profile.chain_id,
        )
        err = signer.check_client()
        if err is not None:
            raise RuntimeError(f"[{self.name}] API key check failed")
        self.signer = signer
        log.info("[%s] signer ready (account %d)", self.name, c.account_index)

    def start_tasks(self, stop: asyncio.Event, notify, live: bool) -> list:
        tasks = [asyncio.create_task(
            LighterBookFeed(self.name, self.profile.ws_url, self.market_id,
                            self.book, notify).run(stop),
            name=f"book-{self.key}")]
        if live:
            self.orders_feed = AccountOrdersFeed(
                self.name, self.profile.ws_url, self.market_id,
                self.conf.lighter_creds.account_index, self.signer)
            tasks.append(asyncio.create_task(self.orders_feed.run(stop),
                                             name=f"acct-{self.key}"))
        return tasks

    def ready_to_trade(self) -> bool:
        return self.orders_feed is not None and self.orders_feed.ready.is_set()

    async def warm_http(self) -> None:
        """Keep the order-path HTTPS connections warm (a cold TLS handshake
        adds 10-15ms to the first order after an idle spell)."""
        try:
            await self._get("/api/v1/status")
        except Exception as e:
            log.debug("[%s] keepalive ping failed: %r", self.name, e)
        if self.signer is None:
            return
        try:
            sess = self.signer.api_client.rest_client.pool_manager
            async with sess.get(self.profile.api_url + "/api/v1/status",
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                await r.read()
        except Exception as e:
            log.debug("[%s] signer keepalive failed: %s", self.name,
                      type(e).__name__)

    # ------------------------------------------------------------ price grid

    def px_round(self, px: float, round_up: bool) -> float:
        f = 10 ** self.price_decimals
        v = math.ceil(px * f - 1e-9) / f if round_up else math.floor(px * f + 1e-9) / f
        return round(v, 8)

    # ------------------------------------------------------------- execution

    def _next_coi(self) -> int:
        self._coi += 1
        return self._coi

    async def send_taker(self, *, is_buy: bool, qty: float, limit_px: float,
                         reduce_only: bool = False) -> dict:
        """Market order with avg-price protection; settle via account ws."""
        if (not math.isfinite(float(qty)) or qty <= 0
                or not math.isfinite(float(limit_px)) or limit_px <= 0):
            return _timed_result({
                "status": "preflight-rejected", "filled_base": 0.0,
                "avg_px": None, "err": "qty and limit_px must be finite and > 0",
                "unresolved": False})
        assert self.signer is not None
        from lighter import SignerClient
        coi = self._next_coi()
        fut = self.orders_feed.watch(coi) if self.orders_feed else None
        base_amount = int(round(qty * 10 ** self.size_decimals))
        price = int(round(limit_px * 10 ** self.price_decimals))
        order_send_ts = time.time()
        try:
            _tx, resp, err = await self.signer.create_order(
                market_index=self.market_id,
                client_order_index=coi,
                base_amount=base_amount,
                price=price,
                is_ask=not is_buy,
                order_type=SignerClient.ORDER_TYPE_MARKET,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                reduce_only=reduce_only,
                order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
            )
        except Exception as e:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = f"{type(e).__name__} during create_order"
            if getattr(e, "status", None) == 429:
                msg = "RATE_LIMITED: " + msg
            return _timed_result(
                {"status": "send-unknown", "filled_base": 0.0,
                 "avg_px": None, "err": msg, "unresolved": True},
                order_send_ts=order_send_ts)
        order_ack_ts = time.time()
        try:
            response_code = int(getattr(resp, "code", None))
        except (TypeError, ValueError):
            response_code = None
        if err is not None or response_code != 200:
            if fut is not None:
                self.orders_feed.unwatch(coi)
            msg = (f"{type(err).__name__} returned by create_order"
                   if err is not None else
                   f"tx rejected code={response_code} "
                   "by exchange")
            if response_code == 429 or "rate limit" in msg.lower():
                msg = "RATE_LIMITED: " + msg
            unresolved = (err is not None or response_code is None
                          or response_code == 408
                          or response_code >= 500)
            return _timed_result(
                {"status": ("send-unknown" if unresolved else "send-failed"),
                 "filled_base": 0.0, "avg_px": None,
                 "err": msg, "unresolved": unresolved},
                order_send_ts=order_send_ts, order_ack_ts=order_ack_ts)
        if fut is None:
            return _timed_result(
                {"status": "sent-unconfirmed", "filled_base": 0.0,
                 "avg_px": None, "err": None, "unresolved": True},
                order_send_ts=order_send_ts, order_ack_ts=order_ack_ts)
        try:
            info = await asyncio.wait_for(fut, timeout=self.settle_timeout)
            fill_ts = (info.get("observed_ts")
                       if info.get("filled_base", 0.0) > 0 else None)
            return _timed_result(
                {"status": info["status"],
                 "filled_base": info["filled_base"],
                 "avg_px": info.get("avg_px"), "err": None,
                 "unresolved": bool(info.get("unresolved"))},
                order_send_ts=order_send_ts, order_ack_ts=order_ack_ts,
                fill_ts=fill_ts)
        except asyncio.TimeoutError:
            self.orders_feed.unwatch(coi)
            log.warning("[%s] no settle confirmation for coi %d in %.1fs",
                        self.name, coi, self.settle_timeout)
            return _timed_result(
                {"status": "timeout", "filled_base": 0.0, "avg_px": None,
                 "err": None, "unresolved": True},
                order_send_ts=order_send_ts, order_ack_ts=order_ack_ts)

    # -------------------------------------------------------------- accounts

    async def _account(self) -> Optional[dict]:
        c = self.conf.lighter_creds
        if c is None or c.account_index is None:
            return None
        data = await self._get("/api/v1/account",
                               params={"by": "index",
                                       "value": str(c.account_index)})
        for acct in data.get("accounts") or []:
            return acct
        return None

    async def fetch_equity(self):
        acct = await self._account()
        if acct is None:
            return None
        return (float(acct.get("total_asset_value") or 0.0),
                float(acct.get("available_balance") or 0.0))

    async def fetch_position(self) -> float:
        acct = await self._account()
        if acct is None:
            raise RuntimeError(f"[{self.name}] account not found")
        for p in acct.get("positions") or []:
            if int(p.get("market_id", -1)) == self.market_id:
                return float(p.get("sign") or 1.0) * float(p.get("position") or 0.0)
        return 0.0

    async def fetch_funding_rate(self) -> float:
        """Current Lighter rate normalized by the API to an 8h equivalent."""
        data = await self._get("/api/v1/funding-rates")
        candidates = []
        for row in data.get("funding_rates") or []:
            if int(row.get("market_id", -1)) != self.market_id:
                continue
            candidates.append(row)
            if str(row.get("exchange", "")).lower() == "lighter":
                return float(row.get("rate") or 0.0) / 8.0
        if candidates:
            return float(candidates[0].get("rate") or 0.0) / 8.0
        raise RuntimeError(f"[{self.name}] funding rate for market "
                           f"{self.market_id} missing")

    async def fetch_funding_cost_since(self, start_ts: float) -> float:
        """Sum public account funding entries; positive means paid."""
        c = self.conf.lighter_creds
        if c is None or c.account_index is None:
            raise RuntimeError(f"[{self.name}] account index unavailable")
        data = await self._get("/api/v1/positionFunding", params={
            "account_index": c.account_index,
            "market_id": self.market_id,
            "limit": 100,
            "side": "all",
            "start_timestamp": int(start_ts * 1000),
        })
        total = 0.0
        for row in (data.get("position_fundings")
                    or data.get("fundings") or []):
            # API versions have used either funding_paid_out or change. The
            # former is a cost; the latter is an account cash change.
            if row.get("funding_paid_out") is not None:
                total += float(row["funding_paid_out"])
            elif row.get("change") is not None:
                total -= float(row["change"])
            elif row.get("amount") is not None:
                total += float(row["amount"])
        return total

    async def close(self) -> None:
        if self.signer is not None:
            try:
                await self.signer.api_client.close()
            except Exception:
                pass
