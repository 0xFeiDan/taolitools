"""Order book state, market-data quality, and fee-aware arbitrage sizing.

One book class serves both feed protocols: zkLighter sends a snapshot plus
diffs (dict maintenance), Hyperliquid's l2Book sends full snapshots.
Legacy freshness remains connection-based.  V2 can additionally enforce the
age of the last actual book update in milliseconds, independently from
websocket heartbeat traffic.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

Level = Tuple[float, float]


@dataclass(frozen=True)
class BookQuality:
    ok: bool
    reason: str
    connection_state: str
    book_age_ms: Optional[float]
    message_age_ms: Optional[float]
    exchange_lag_ms: Optional[float]


class OrderBook:
    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.ready = False
        self.last_update_ts = 0.0
        self.last_message_ts = 0.0
        self.alive_ts = 0.0  # backward-compatible alias for last_message_ts
        self.exchange_ts: Optional[float] = None
        self.connection_state = "disconnected"
        self.last_disconnect_reason: Optional[str] = None

    def touch(self, received_ts: Optional[float] = None) -> None:
        ts = time.time() if received_ts is None else float(received_ts)
        self.last_message_ts = ts
        self.alive_ts = ts

    def mark_connecting(self) -> None:
        self.connection_state = "connecting"
        self.last_disconnect_reason = None

    def mark_connected(self) -> None:
        self.connection_state = "syncing" if not self.ready else "live"
        self.last_disconnect_reason = None

    def mark_disconnected(self, reason: Optional[str] = None) -> None:
        self.connection_state = "disconnected"
        self.last_disconnect_reason = reason
        self.ready = False

    def clear(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.ready = False

    def _mark_book_update(self, received_ts: Optional[float],
                          exchange_ts: Optional[float]) -> None:
        ts = time.time() if received_ts is None else float(received_ts)
        self.ready = True
        self.last_update_ts = ts
        self.exchange_ts = exchange_ts
        self.connection_state = "live"
        self.touch(ts)

    # ---- zkLighter snapshot + diff ----
    def apply_lighter(self, ob: dict, snapshot: bool, *,
                      received_ts: Optional[float] = None,
                      exchange_ts: Optional[float] = None) -> None:
        if snapshot:
            self.bids.clear()
            self.asks.clear()
        for name, side in (("bids", self.bids), ("asks", self.asks)):
            for lvl in ob.get(name) or []:
                px, sz = float(lvl["price"]), float(lvl["size"])
                if not (math.isfinite(px) and math.isfinite(sz) and px > 0):
                    continue
                if sz <= 0:
                    side.pop(px, None)
                else:
                    side[px] = sz
        self._mark_book_update(received_ts, exchange_ts)

    # ---- Hyperliquid full snapshot ----
    def apply_hl(self, levels: list, *, received_ts: Optional[float] = None,
                 exchange_ts: Optional[float] = None) -> None:
        def clean(raw):
            parsed = ((float(level["px"]), float(level["sz"]))
                      for level in raw)
            return {px: size for px, size in parsed
                    if (math.isfinite(px) and math.isfinite(size)
                        and px > 0 and size > 0)}

        self.bids = clean(levels[0])
        self.asks = clean(levels[1])
        self._mark_book_update(received_ts, exchange_ts)

    def sorted_bids(self) -> List[Level]:
        return sorted(self.bids.items(), key=lambda kv: -kv[0])

    def sorted_asks(self) -> List[Level]:
        return sorted(self.asks.items())

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def mid(self) -> Optional[float]:
        if not (self.bids and self.asks):
            return None
        return (max(self.bids) + min(self.asks)) / 2.0

    def book_age_ms(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_update_ts <= 0:
            return None
        current = time.time() if now is None else float(now)
        return max((current - self.last_update_ts) * 1000.0, 0.0)

    def message_age_ms(self, now: Optional[float] = None) -> Optional[float]:
        if self.last_message_ts <= 0:
            return None
        current = time.time() if now is None else float(now)
        return max((current - self.last_message_ts) * 1000.0, 0.0)

    def exchange_lag_ms(self) -> Optional[float]:
        if self.exchange_ts is None or self.last_update_ts <= 0:
            return None
        return max((self.last_update_ts - self.exchange_ts) * 1000.0, 0.0)

    def quality(self, max_connection_age_sec: float, *,
                max_book_age_ms: Optional[float] = None,
                now: Optional[float] = None) -> BookQuality:
        book_age = self.book_age_ms(now)
        message_age = self.message_age_ms(now)
        common = dict(connection_state=self.connection_state,
                      book_age_ms=book_age,
                      message_age_ms=message_age,
                      exchange_lag_ms=self.exchange_lag_ms())
        if self.connection_state == "disconnected":
            return BookQuality(False, "disconnected", **common)
        if not self.ready:
            return BookQuality(False, "not_ready", **common)
        if not self.bids or not self.asks:
            return BookQuality(False, "empty_book", **common)
        if self.best_bid() >= self.best_ask():
            return BookQuality(False, "crossed_book", **common)
        if message_age is None \
                or message_age > max_connection_age_sec * 1000.0:
            return BookQuality(False, "connection_stale", **common)
        if max_book_age_ms is not None \
                and (book_age is None or book_age > max_book_age_ms):
            return BookQuality(False, "book_stale", **common)
        if max_book_age_ms is not None \
                and common["exchange_lag_ms"] is not None \
                and common["exchange_lag_ms"] > max_book_age_ms:
            return BookQuality(False, "exchange_stale", **common)
        return BookQuality(True, "ok", **common)

    def is_fresh(self, max_age_sec: float) -> bool:
        """Legacy connection freshness used when V2 book-age guard is off."""
        return self.quality(max_age_sec).ok


def floor_step(x: float, step: float) -> float:
    return round(math.floor(x / step + 1e-9) * step, 12)


def crossable_base(asks: List[Level], bids: List[Level], threshold: float,
                   buy_fee: float = 0.0, sell_fee: float = 0.0) -> Tuple[float, float]:
    """Walk both books level by level and return (base qty, buy notional) that
    can be crossed while every marginal slice still clears fees + threshold."""
    qty = 0.0
    buy_notional = 0.0
    i = j = 0
    a_px = a_rem = 0.0
    b_px = b_rem = 0.0
    while True:
        if a_rem <= 0:
            if i >= len(asks):
                break
            a_px, a_rem = asks[i]
            i += 1
        if b_rem <= 0:
            if j >= len(bids):
                break
            b_px, b_rem = bids[j]
            j += 1
        if b_px * (1.0 - sell_fee) < a_px * (1.0 + buy_fee) * (1.0 + threshold):
            break
        take = min(a_rem, b_rem)
        qty += take
        buy_notional += take * a_px
        a_rem -= take
        b_rem -= take
    return qty, buy_notional


def walk_depth(levels: List[Level], qty: float) -> Tuple[float, float]:
    remaining = qty
    notional = 0.0
    marginal_px = levels[0][0]
    for px, sz in levels:
        take = min(remaining, sz)
        notional += take * px
        marginal_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    return marginal_px, notional


def qty_within_notional(levels: List[Level], cap: float) -> float:
    """Maximum visible base quantity whose walked notional stays under cap."""
    remaining_notional = cap
    qty = 0.0
    for px, size in levels:
        take = min(size, remaining_notional / px)
        qty += take
        remaining_notional -= take * px
        if remaining_notional <= 1e-9:
            break
    return qty


@dataclass
class ArbPlan:
    qty: float
    buy_limit: float
    sell_limit: float
    buy_notional: float
    sell_notional: float
    q_max: float
    q_max_notional: float
    top_premium_bps: float
    marginal_premium_bps: float
    buy_fee: float
    sell_fee: float
    sizing_mode: str = "legacy"
    buy_vwap: float = 0.0
    sell_vwap: float = 0.0
    gross_vwap_edge_bps: float = 0.0
    adjusted_gross_edge_bps: float = 0.0
    stablecoin_basis_bps: float = 0.0
    funding_cost_bps: float = 0.0
    expected_net_edge_bps: float = 0.0
    required_net_edge_bps: float = 0.0
    fee_cost_bps: float = 0.0
    extra_cost_bps: float = 0.0
    max_vwap_slippage_bps: float = 0.0
    max_book_impact_bps: float = 0.0
    modeled_net_profit_usd: Optional[float] = None
    signal_spread_bps: Optional[float] = None
    signal_midline_bps: Optional[float] = None
    signal_fast_midline_bps: Optional[float] = None
    signal_volatility_bps: Optional[float] = None
    signal_deviation_bps: Optional[float] = None
    signal_z_score: Optional[float] = None
    regime_state: str = "static"
    signal_action: str = "OPEN"
    pair_id: Optional[str] = None

    @property
    def gross_edge_usd(self) -> float:
        return self.sell_notional - self.buy_notional

    @property
    def exp_edge_usd(self) -> float:
        if self.modeled_net_profit_usd is not None:
            return self.modeled_net_profit_usd
        return (self.sell_notional * (1.0 - self.sell_fee)
                - self.buy_notional * (1.0 + self.buy_fee))


def plan_arb(buy_book: OrderBook, sell_book: OrderBook, *, threshold_bps: float,
             buy_fee_bps: float, sell_fee_bps: float, take_fraction: float,
             cap_notional: float, min_base: float, min_notional: float,
             size_step: float, max_base: Optional[float] = None):
    """Size a two-leg taker slice: buy on buy_book, sell on sell_book.

    A slice qualifies when the executable premium (sell bid over buy ask)
    clears both venues' taker fees plus threshold_bps. Returns
    (ArbPlan | None, reason).
    """
    numeric = (
        threshold_bps, buy_fee_bps, sell_fee_bps, take_fraction,
        cap_notional, min_base, min_notional, size_step)
    if not all(math.isfinite(float(value)) for value in numeric):
        raise ValueError("all planner inputs must be finite")
    if not 0 < take_fraction <= 1:
        raise ValueError("take_fraction must be in (0, 1]")
    if cap_notional <= 0 or size_step <= 0:
        raise ValueError("cap_notional and size_step must be > 0")
    if min_base < 0 or min_notional < 0:
        raise ValueError("minimum size/notional must be >= 0")
    if not (0 <= buy_fee_bps < 10000
            and 0 <= sell_fee_bps < 10000):
        raise ValueError("fee bps must be in [0, 10000)")
    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    if not asks or not bids:
        return None, "empty_book"
    threshold = threshold_bps / 1e4
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    top_premium_bps = (bids[0][0] / asks[0][0] - 1.0) * 1e4
    if bids[0][0] * (1.0 - sell_fee) < asks[0][0] * (1.0 + buy_fee) * (1.0 + threshold):
        return None, "no_edge"
    q_max, q_max_notional = crossable_base(asks, bids, threshold, buy_fee, sell_fee)
    if q_max <= 0:
        return None, "no_edge"
    target = min(q_max * take_fraction,
                 qty_within_notional(asks, cap_notional),
                 qty_within_notional(bids, cap_notional))
    if max_base is not None:
        target = min(target, max_base)
    target = floor_step(target, size_step)
    if target < min_base:
        return None, "below_min_base"
    buy_limit, buy_notional = walk_depth(asks, target)
    sell_limit, sell_notional = walk_depth(bids, target)
    if buy_notional < min_notional or sell_notional < min_notional:
        return None, "below_min_notional"
    buy_vwap = buy_notional / target
    sell_vwap = sell_notional / target
    exp_edge = (sell_notional * (1.0 - sell_fee)
                - buy_notional * (1.0 + buy_fee))
    fee_usd = buy_notional * buy_fee + sell_notional * sell_fee
    return ArbPlan(
        qty=target, buy_limit=buy_limit, sell_limit=sell_limit,
        buy_notional=buy_notional, sell_notional=sell_notional,
        q_max=q_max, q_max_notional=q_max_notional,
        top_premium_bps=top_premium_bps,
        marginal_premium_bps=(sell_limit / buy_limit - 1.0) * 1e4,
        buy_fee=buy_fee, sell_fee=sell_fee,
        buy_vwap=buy_vwap, sell_vwap=sell_vwap,
        gross_vwap_edge_bps=(sell_vwap / buy_vwap - 1.0) * 1e4,
        expected_net_edge_bps=exp_edge / buy_notional * 1e4,
        required_net_edge_bps=threshold_bps,
        fee_cost_bps=fee_usd / buy_notional * 1e4,
        max_vwap_slippage_bps=max(
            (buy_vwap / asks[0][0] - 1.0) * 1e4,
            (bids[0][0] - sell_vwap) / bids[0][0] * 1e4),
        max_book_impact_bps=max(
            (buy_limit / asks[0][0] - 1.0) * 1e4,
            (bids[0][0] - sell_limit) / bids[0][0] * 1e4),
    ), "ok"


def plan_vwap_arb(
        buy_book: OrderBook, sell_book: OrderBook, *,
        required_net_edge_bps: float,
        buy_fee_bps: float, sell_fee_bps: float,
        min_order_usd: float, max_order_usd: float,
        max_vwap_slippage_bps: float, max_book_impact_bps: float,
        safety_buffer_bps: float, expected_latency_cost_bps: float,
        min_base: float, size_step: float,
        max_base: Optional[float] = None,
        funding_cost_bps: float = 0.0,
        buy_funding_rate: Optional[float] = None,
        sell_funding_rate: Optional[float] = None,
        expected_holding_hours: float = 0.0,
        buy_quote_usd: float = 1.0, sell_quote_usd: float = 1.0):
    """Build an ``ArbPlan`` from current-book VWAP binary sizing."""
    from .pricing import find_max_executable_size

    asks = buy_book.sorted_asks()
    bids = sell_book.sorted_bids()
    result, reason = find_max_executable_size(
        asks, bids,
        min_order_usd=min_order_usd, max_order_usd=max_order_usd,
        min_base=min_base, size_step=size_step,
        max_base=max_base,
        required_net_edge_bps=required_net_edge_bps,
        max_vwap_slippage_bps=max_vwap_slippage_bps,
        max_book_impact_bps=max_book_impact_bps,
        buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps,
        safety_buffer_bps=safety_buffer_bps,
        expected_latency_cost_bps=expected_latency_cost_bps,
        funding_cost_bps=funding_cost_bps,
        buy_funding_rate=buy_funding_rate,
        sell_funding_rate=sell_funding_rate,
        expected_holding_hours=expected_holding_hours,
        buy_quote_usd=buy_quote_usd,
        sell_quote_usd=sell_quote_usd,
    )
    if result is None:
        return None, reason
    edge = result.edge
    return ArbPlan(
        qty=edge.qty,
        buy_limit=edge.buy.worst_px,
        sell_limit=edge.sell.worst_px,
        buy_notional=edge.buy.notional_usd * buy_quote_usd,
        sell_notional=edge.sell.notional_usd * sell_quote_usd,
        q_max=result.search_upper_qty,
        q_max_notional=(result.search_upper_qty * asks[0][0]
                        * buy_quote_usd),
        top_premium_bps=(bids[0][0] / asks[0][0] - 1.0) * 1e4,
        marginal_premium_bps=(edge.sell.worst_px / edge.buy.worst_px
                              - 1.0) * 1e4,
        buy_fee=buy_fee_bps / 1e4,
        sell_fee=sell_fee_bps / 1e4,
        sizing_mode="vwap",
        buy_vwap=edge.buy.vwap,
        sell_vwap=edge.sell.vwap,
        gross_vwap_edge_bps=edge.gross_edge_bps,
        adjusted_gross_edge_bps=edge.adjusted_gross_edge_bps,
        stablecoin_basis_bps=edge.stablecoin_basis_bps,
        funding_cost_bps=edge.funding_cost_bps,
        expected_net_edge_bps=edge.expected_net_edge_bps,
        required_net_edge_bps=required_net_edge_bps,
        fee_cost_bps=edge.fee_cost_bps,
        extra_cost_bps=edge.extra_cost_bps,
        max_vwap_slippage_bps=edge.max_vwap_slippage_bps,
        max_book_impact_bps=edge.max_book_impact_bps,
        modeled_net_profit_usd=edge.expected_net_profit_usd,
    ), "ok"
