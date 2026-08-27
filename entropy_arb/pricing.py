"""Current-orderbook VWAP, executable edge, and automatic V2 sizing.

All functions are pure and exchange-agnostic.  VWAP already captures visible
book slippage; additional costs here are only fees and explicit buffers, so
the same slippage is never deducted twice.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple

Level = Tuple[float, float]


@dataclass(frozen=True)
class VwapFill:
    side: str
    requested_qty: float
    filled_qty: float
    notional_usd: float
    vwap: float
    top_px: float
    worst_px: float
    complete: bool
    vwap_slippage_bps: float
    book_impact_bps: float


@dataclass(frozen=True)
class ExecutableEdge:
    qty: float
    buy: VwapFill
    sell: VwapFill
    gross_edge_bps: float
    adjusted_gross_edge_bps: float
    stablecoin_basis_bps: float
    funding_cost_bps: float
    fee_cost_bps: float
    extra_cost_bps: float
    expected_net_edge_bps: float
    expected_net_profit_usd: float

    @property
    def max_vwap_slippage_bps(self) -> float:
        return max(self.buy.vwap_slippage_bps,
                   self.sell.vwap_slippage_bps)

    @property
    def max_book_impact_bps(self) -> float:
        return max(self.buy.book_impact_bps, self.sell.book_impact_bps)


@dataclass(frozen=True)
class VwapSizingResult:
    edge: ExecutableEdge
    search_upper_qty: float
    iterations: int


def _floor_step(value: float, step: float) -> float:
    return round(math.floor(value / step + 1e-9) * step, 12)


def _ceil_step(value: float, step: float) -> float:
    return round(math.ceil(value / step - 1e-9) * step, 12)


def _qty_to_reach_notional(levels: List[Level], target: float) -> Optional[float]:
    """Return exact base quantity needed to reach target through depth."""
    remaining = target
    qty = 0.0
    for px, size in levels:
        take = min(size, remaining / px)
        qty += take
        remaining -= take * px
        if remaining <= 1e-9:
            return qty
    return None


def _qty_within_notional(levels: List[Level], cap: float) -> float:
    """Return maximum exact base quantity whose walked notional is <= cap."""
    capped = _qty_to_reach_notional(levels, cap)
    return sum(size for _, size in levels) if capped is None else capped


def simulate_vwap(levels: List[Level], qty: float, *,
                  side: str) -> Optional[VwapFill]:
    """Walk one side of a current book for a common base quantity."""
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if qty <= 0:
        raise ValueError("qty must be > 0")
    usable = [(float(px), float(size)) for px, size in levels
              if px > 0 and size > 0]
    usable.sort(key=lambda level: level[0], reverse=side == "sell")
    if not usable:
        return None

    remaining = qty
    filled = notional = 0.0
    top_px = usable[0][0]
    worst_px = top_px
    for px, size in usable:
        take = min(remaining, size)
        if take <= 0:
            continue
        filled += take
        notional += take * px
        worst_px = px
        remaining -= take
        if remaining <= 1e-12:
            break
    if filled <= 0:
        return None

    vwap = notional / filled
    if side == "buy":
        vwap_slippage = (vwap / top_px - 1.0) * 1e4
        impact = (worst_px / top_px - 1.0) * 1e4
    else:
        vwap_slippage = (top_px - vwap) / top_px * 1e4
        impact = (top_px - worst_px) / top_px * 1e4
    return VwapFill(
        side=side, requested_qty=qty, filled_qty=filled,
        notional_usd=notional, vwap=vwap, top_px=top_px,
        worst_px=worst_px, complete=remaining <= 1e-12,
        vwap_slippage_bps=max(vwap_slippage, 0.0),
        book_impact_bps=max(impact, 0.0),
    )


def executable_edge(asks: List[Level], bids: List[Level], qty: float, *,
                    buy_fee_bps: float, sell_fee_bps: float,
                    safety_buffer_bps: float = 0.0,
                    expected_latency_cost_bps: float = 0.0,
                    funding_cost_bps: float = 0.0,
                    stablecoin_basis_cost_bps: float = 0.0,
                    buy_quote_usd: float = 1.0,
                    sell_quote_usd: float = 1.0,
                    ) -> Optional[ExecutableEdge]:
    buy = simulate_vwap(asks, qty, side="buy")
    sell = simulate_vwap(bids, qty, side="sell")
    if buy is None or sell is None or not (buy.complete and sell.complete):
        return None

    if buy_quote_usd <= 0 or sell_quote_usd <= 0:
        raise ValueError("quote USD rates must be > 0")
    buy_fee = buy_fee_bps / 1e4
    sell_fee = sell_fee_bps / 1e4
    extra_cost_bps = (safety_buffer_bps + expected_latency_cost_bps
                      + funding_cost_bps + stablecoin_basis_cost_bps)
    buy_usd = buy.notional_usd * buy_quote_usd
    sell_usd = sell.notional_usd * sell_quote_usd
    fee_usd = buy_usd * buy_fee + sell_usd * sell_fee
    extra_usd = buy_usd * extra_cost_bps / 1e4
    expected_profit = (sell_usd - buy_usd
                       - fee_usd - extra_usd)
    raw_gross_bps = (sell.vwap / buy.vwap - 1.0) * 1e4
    adjusted_gross_bps = (sell.vwap * sell_quote_usd
                          / (buy.vwap * buy_quote_usd) - 1.0) * 1e4
    return ExecutableEdge(
        qty=qty, buy=buy, sell=sell,
        gross_edge_bps=raw_gross_bps,
        adjusted_gross_edge_bps=adjusted_gross_bps,
        stablecoin_basis_bps=raw_gross_bps - adjusted_gross_bps,
        funding_cost_bps=funding_cost_bps,
        fee_cost_bps=fee_usd / buy_usd * 1e4,
        extra_cost_bps=extra_cost_bps,
        expected_net_edge_bps=expected_profit / buy_usd * 1e4,
        expected_net_profit_usd=expected_profit,
    )


def find_max_executable_size(
        asks: List[Level], bids: List[Level], *,
        min_order_usd: float, max_order_usd: float,
        min_base: float, size_step: float, max_base: Optional[float] = None,
        required_net_edge_bps: float,
        max_vwap_slippage_bps: float,
        max_book_impact_bps: float,
        buy_fee_bps: float, sell_fee_bps: float,
        safety_buffer_bps: float = 0.0,
        expected_latency_cost_bps: float = 0.0,
        funding_cost_bps: float = 0.0,
        stablecoin_basis_cost_bps: float = 0.0,
        buy_quote_usd: float = 1.0,
        sell_quote_usd: float = 1.0,
        max_iterations: int = 64,
        ) -> Tuple[Optional[VwapSizingResult], str]:
    """Binary-search the largest common base size passing every constraint.

    With correctly sorted asks (ascending) and bids (descending), VWAP edge is
    non-increasing as quantity grows, making the feasibility predicate
    monotonic.
    """
    asks = sorted(((px, size) for px, size in asks if px > 0 and size > 0),
                  key=lambda level: level[0])
    bids = sorted(((px, size) for px, size in bids if px > 0 and size > 0),
                  key=lambda level: level[0], reverse=True)
    if not asks or not bids:
        return None, "empty_book"
    if size_step <= 0:
        raise ValueError("size_step must be > 0")

    depth_upper = min(sum(size for _, size in asks),
                      sum(size for _, size in bids))
    upper_candidates = [
        depth_upper,
        _qty_within_notional(asks, max_order_usd),
        _qty_within_notional(bids, max_order_usd),
    ]
    if max_base is not None:
        upper_candidates.append(max(float(max_base), 0.0))
    upper = _floor_step(min(upper_candidates), size_step)
    buy_min_qty = _qty_to_reach_notional(asks, min_order_usd)
    sell_min_qty = _qty_to_reach_notional(bids, min_order_usd)
    if buy_min_qty is None or sell_min_qty is None:
        return None, "insufficient_depth"
    minimum_qty = max(min_base, buy_min_qty, sell_min_qty)
    lower = _ceil_step(minimum_qty, size_step)
    if upper < lower or upper <= 0:
        return None, "insufficient_depth"

    edge_kwargs = dict(
        buy_fee_bps=buy_fee_bps, sell_fee_bps=sell_fee_bps,
        safety_buffer_bps=safety_buffer_bps,
        expected_latency_cost_bps=expected_latency_cost_bps,
        funding_cost_bps=funding_cost_bps,
        stablecoin_basis_cost_bps=stablecoin_basis_cost_bps,
        buy_quote_usd=buy_quote_usd,
        sell_quote_usd=sell_quote_usd,
    )

    def evaluate(qty: float) -> Tuple[Optional[ExecutableEdge], str]:
        edge = executable_edge(asks, bids, qty, **edge_kwargs)
        if edge is None:
            return None, "insufficient_depth"
        if (edge.buy.notional_usd < min_order_usd
                or edge.sell.notional_usd < min_order_usd):
            return None, "below_min_notional"
        if (edge.buy.notional_usd > max_order_usd + 1e-8
                or edge.sell.notional_usd > max_order_usd + 1e-8):
            return None, "above_max_notional"
        if edge.max_vwap_slippage_bps > max_vwap_slippage_bps + 1e-9:
            return None, "vwap_slippage"
        if edge.max_book_impact_bps > max_book_impact_bps + 1e-9:
            return None, "book_impact"
        if edge.expected_net_edge_bps < required_net_edge_bps - 1e-9:
            return None, "net_edge"
        return edge, "ok"

    lower_edge, reason = evaluate(lower)
    if lower_edge is None:
        return None, reason
    upper_edge, _ = evaluate(upper)
    if upper_edge is not None:
        return VwapSizingResult(upper_edge, upper, 0), "ok"

    lo, hi = lower, upper
    best = lower_edge
    iterations = 0
    while hi - lo > size_step + 1e-12 and iterations < max_iterations:
        iterations += 1
        mid = _floor_step((lo + hi) / 2.0, size_step)
        if mid <= lo:
            break
        candidate, _ = evaluate(mid)
        if candidate is not None:
            lo, best = mid, candidate
        else:
            hi = mid

    return VwapSizingResult(best, upper, iterations), "ok"
