"""Automatic 1-minute orderbook data recorder.

While the bot runs (live or --record-only), both venues' actual order books
are sampled once per second and aggregated into one CSV row per minute.
This is the dataset users analyze (tools/analyze.py) to choose
thresholds.midline_bps / upper_bps / lower_bps for config.yaml.

Definitions (all in bps, fees NOT included — the engine adds fees on top):

    premium    = (entropy_mid / hedge_mid - 1) * 1e4
                 the mid-to-mid premium of Entropy over the hedge venue;
                 its long-run center is what midline_bps hardcodes.
    sell_edge  = (entropy_bid / hedge_ask - 1) * 1e4
                 the EXECUTABLE premium for SELL-entropy/BUY-hedge; the
                 engine fires this direction when sell_edge clears
                 midline_bps + upper_bps (plus fees).
    buy_edge   = (hedge_bid / entropy_ask - 1) * 1e4
                 the executable premium for BUY-entropy/SELL-hedge; fires
                 when buy_edge clears lower_bps - midline_bps (plus fees).

The corresponding ``*_usd_*`` fields multiply each leg by its fresh
quote/USD observation before taking the ratio. Raw samples are still retained
when FX is unavailable, but USD fields stay blank and ``fx_samples`` remains
zero; missing FX is never represented as parity.

Bid/ask columns are the minute's last fresh sample (close). A row is only
written for minutes with at least one sample where both books were fresh;
`samples` says how many of the ~60 seconds qualified; ``fx_samples`` counts
the subset that also had fresh quote/USD observations.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Callable, Optional

from .book import OrderBook

log = logging.getLogger("recorder")

HEADER = ["minute_ts", "time_utc",
          "entropy_bid", "entropy_ask", "hedge_bid", "hedge_ask",
          "premium_open_bps", "premium_high_bps", "premium_low_bps",
          "premium_close_bps", "premium_mean_bps", "premium_std_bps",
          "sell_edge_mean_bps", "sell_edge_max_bps",
          "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
          "entropy_quote_asset", "hedge_quote_asset",
          "entropy_quote_usd_close", "hedge_quote_usd_close",
          "hedge_entropy_quote_bid_close",
          "hedge_entropy_quote_ask_close",
          "hedge_entropy_quote_spread_close_bps",
          "hedge_entropy_quote_basis_close_bps",
          "premium_usd_open_bps", "premium_usd_high_bps",
          "premium_usd_low_bps", "premium_usd_close_bps",
          "premium_usd_mean_bps", "premium_usd_std_bps",
          "sell_edge_usd_mean_bps", "sell_edge_usd_max_bps",
          "buy_edge_usd_mean_bps", "buy_edge_usd_max_bps", "fx_samples"]


class _MinuteAgg:
    __slots__ = ("minute", "n", "p_open", "p_high", "p_low", "p_close",
                 "p_sum", "p_sumsq", "s_sum", "s_max", "b_sum", "b_max",
                 "e_bid", "e_ask", "h_bid", "h_ask", "entropy_asset",
                 "hedge_asset", "fx_n", "fx_p_open", "fx_p_high",
                 "fx_p_low", "fx_p_close", "fx_p_sum", "fx_p_sumsq",
                 "fx_s_sum", "fx_s_max", "fx_b_sum", "fx_b_max",
                 "e_rate", "h_rate", "pair_bid", "pair_ask",
                 "pair_spread", "quote_basis")

    def __init__(self, minute: int, entropy_asset: str,
                 hedge_asset: str) -> None:
        self.minute = minute
        self.n = 0
        self.p_open = self.p_high = self.p_low = self.p_close = 0.0
        self.p_sum = self.p_sumsq = 0.0
        self.s_sum = 0.0
        self.s_max = -math.inf
        self.b_sum = 0.0
        self.b_max = -math.inf
        self.e_bid = self.e_ask = self.h_bid = self.h_ask = 0.0
        self.entropy_asset = entropy_asset
        self.hedge_asset = hedge_asset
        self.fx_n = 0
        self.fx_p_open = self.fx_p_high = self.fx_p_low = 0.0
        self.fx_p_close = self.fx_p_sum = self.fx_p_sumsq = 0.0
        self.fx_s_sum = 0.0
        self.fx_s_max = -math.inf
        self.fx_b_sum = 0.0
        self.fx_b_max = -math.inf
        self.e_rate = self.h_rate = 0.0
        self.pair_bid = self.pair_ask = self.pair_spread = 0.0
        self.quote_basis = 0.0

    def add(self, e_bid: float, e_ask: float, h_bid: float, h_ask: float,
            e_rate: Optional[float] = None,
            h_rate: Optional[float] = None,
            pair_bid: Optional[float] = None,
            pair_ask: Optional[float] = None) -> None:
        e_mid = (e_bid + e_ask) / 2.0
        h_mid = (h_bid + h_ask) / 2.0
        prem = (e_mid / h_mid - 1.0) * 1e4
        sell_edge = (e_bid / h_ask - 1.0) * 1e4
        buy_edge = (h_bid / e_ask - 1.0) * 1e4
        if self.n == 0:
            self.p_open = self.p_high = self.p_low = prem
        self.n += 1
        self.p_high = max(self.p_high, prem)
        self.p_low = min(self.p_low, prem)
        self.p_close = prem
        self.p_sum += prem
        self.p_sumsq += prem * prem
        self.s_sum += sell_edge
        self.s_max = max(self.s_max, sell_edge)
        self.b_sum += buy_edge
        self.b_max = max(self.b_max, buy_edge)
        self.e_bid, self.e_ask, self.h_bid, self.h_ask = e_bid, e_ask, h_bid, h_ask
        if e_rate is None or h_rate is None:
            return
        values = (e_rate, h_rate)
        if (not all(math.isfinite(float(value)) and float(value) > 0
                    for value in values)):
            return
        e_rate, h_rate = float(e_rate), float(h_rate)
        if pair_bid is None or pair_ask is None:
            pair_bid = pair_ask = h_rate / e_rate
        pair_bid, pair_ask = float(pair_bid), float(pair_ask)
        if (not all(math.isfinite(value) and value > 0
                    for value in (pair_bid, pair_ask))
                or pair_bid > pair_ask):
            return
        pair_mid = (pair_bid + pair_ask) / 2.0
        pair_spread = (pair_ask / pair_bid - 1.0) * 1e4
        fx_prem = (e_mid / (h_mid * pair_mid) - 1.0) * 1e4
        # SELL Entropy receives its quote asset and BUY hedge must acquire the
        # hedge quote asset at the direct cross ask. Reverse direction sells
        # the hedge quote asset at the direct cross bid.
        fx_sell = (e_bid / (h_ask * pair_ask) - 1.0) * 1e4
        fx_buy = (h_bid * pair_bid / e_ask - 1.0) * 1e4
        quote_basis = (pair_mid - 1.0) * 1e4
        if not all(math.isfinite(value) for value in (
                fx_prem, fx_sell, fx_buy, pair_spread, quote_basis)):
            return
        if self.fx_n == 0:
            self.fx_p_open = self.fx_p_high = self.fx_p_low = fx_prem
        self.fx_n += 1
        self.fx_p_high = max(self.fx_p_high, fx_prem)
        self.fx_p_low = min(self.fx_p_low, fx_prem)
        self.fx_p_close = fx_prem
        self.fx_p_sum += fx_prem
        self.fx_p_sumsq += fx_prem * fx_prem
        self.fx_s_sum += fx_sell
        self.fx_s_max = max(self.fx_s_max, fx_sell)
        self.fx_b_sum += fx_buy
        self.fx_b_max = max(self.fx_b_max, fx_buy)
        self.e_rate, self.h_rate = e_rate, h_rate
        self.pair_bid, self.pair_ask = pair_bid, pair_ask
        self.pair_spread = pair_spread
        self.quote_basis = quote_basis

    def row(self) -> list:
        mean = self.p_sum / self.n
        var = max(self.p_sumsq / self.n - mean * mean, 0.0)
        ts = self.minute * 60
        raw = [ts,
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"),
                f"{self.e_bid:.10g}", f"{self.e_ask:.10g}",
                f"{self.h_bid:.10g}", f"{self.h_ask:.10g}",
                f"{self.p_open:.3f}", f"{self.p_high:.3f}",
                f"{self.p_low:.3f}", f"{self.p_close:.3f}",
                f"{mean:.3f}", f"{math.sqrt(var):.3f}",
                f"{self.s_sum / self.n:.3f}", f"{self.s_max:.3f}",
                f"{self.b_sum / self.n:.3f}", f"{self.b_max:.3f}",
                self.n]
        if self.fx_n == 0:
            return raw + [self.entropy_asset, self.hedge_asset] + [""] * 16 + [0]
        fx_mean = self.fx_p_sum / self.fx_n
        fx_var = max(self.fx_p_sumsq / self.fx_n - fx_mean * fx_mean, 0.0)
        return raw + [
            self.entropy_asset, self.hedge_asset,
            f"{self.e_rate:.10g}", f"{self.h_rate:.10g}",
            f"{self.pair_bid:.10g}", f"{self.pair_ask:.10g}",
            f"{self.pair_spread:.3f}",
            f"{self.quote_basis:.3f}",
            f"{self.fx_p_open:.3f}", f"{self.fx_p_high:.3f}",
            f"{self.fx_p_low:.3f}", f"{self.fx_p_close:.3f}",
            f"{fx_mean:.3f}", f"{math.sqrt(fx_var):.3f}",
            f"{self.fx_s_sum / self.fx_n:.3f}", f"{self.fx_s_max:.3f}",
            f"{self.fx_b_sum / self.fx_n:.3f}", f"{self.fx_b_max:.3f}",
            self.fx_n]


class MinuteRecorder:
    def __init__(self, path: str, entropy_book: OrderBook, hedge_book: OrderBook,
                 staleness_sec: float, interval_sec: float = 1.0, *,
                 quote_rate_getter: Optional[
                     Callable[[str, float], float]] = None,
                 quote_pair_getter: Optional[
                     Callable[[str, str, float], tuple[float, float]]] = None,
                 entropy_quote_asset: str = "UNKNOWN",
                 hedge_quote_asset: str = "UNKNOWN") -> None:
        self.path = path
        self.entropy_book = entropy_book
        self.hedge_book = hedge_book
        self.staleness_sec = staleness_sec
        self.interval_sec = interval_sec
        self.quote_rate_getter = quote_rate_getter
        self.quote_pair_getter = quote_pair_getter
        self.entropy_quote_asset = entropy_quote_asset.upper()
        self.hedge_quote_asset = hedge_quote_asset.upper()
        self.rows_written = 0
        self._agg: Optional[_MinuteAgg] = None
        self._fh = None
        self._writer = None

    def _open(self) -> None:
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        if os.path.exists(self.path) and os.path.getsize(self.path) > 0:
            # never append rows under a different schema's header
            with open(self.path) as fh0:
                if fh0.readline().strip() != ",".join(HEADER):
                    log.warning("%s has an old header — rotated to %s.old",
                                self.path, self.path)
                    os.replace(self.path, self.path + ".old")
        new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if new:
            self._writer.writerow(HEADER)
            self._fh.flush()
        log.info("recording 1-minute orderbook data -> %s", self.path)

    def _flush_agg(self) -> None:
        if self._agg is None or self._agg.n == 0:
            self._agg = None
            return
        if self._writer is None:
            self._open()
        self._writer.writerow(self._agg.row())
        self._fh.flush()
        self.rows_written += 1
        self._agg = None

    def sample(self, now: Optional[float] = None) -> None:
        """Take one sample; call ~1/sec. Rolls the minute over as needed."""
        now = time.time() if now is None else now
        minute = int(now // 60)
        if self._agg is not None and self._agg.minute != minute:
            self._flush_agg()
        if not (self.entropy_book.is_fresh(self.staleness_sec)
                and self.hedge_book.is_fresh(self.staleness_sec)):
            return
        e_bid, e_ask = self.entropy_book.best_bid(), self.entropy_book.best_ask()
        h_bid, h_ask = self.hedge_book.best_bid(), self.hedge_book.best_ask()
        if None in (e_bid, e_ask, h_bid, h_ask):
            return
        if self._agg is None:
            self._agg = _MinuteAgg(
                minute, self.entropy_quote_asset, self.hedge_quote_asset)
        e_rate = h_rate = None
        pair_bid = pair_ask = None
        if self.quote_rate_getter is not None:
            try:
                e_rate = self.quote_rate_getter("entropy", now)
                h_rate = self.quote_rate_getter("hedge", now)
            except (KeyError, TypeError, ValueError):
                e_rate = h_rate = None
        if self.quote_pair_getter is not None:
            try:
                pair_bid, pair_ask = self.quote_pair_getter(
                    self.hedge_quote_asset, self.entropy_quote_asset, now)
            except (KeyError, TypeError, ValueError):
                pair_bid = pair_ask = None
        self._agg.add(e_bid, e_ask, h_bid, h_ask, e_rate, h_rate,
                      pair_bid, pair_ask)

    def close(self) -> None:
        """Flush the partial minute and close the file (call on shutdown)."""
        self._flush_agg()
        if self._fh is not None:
            self._fh.close()
            self._fh = self._writer = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    self.sample()
                except Exception:
                    log.exception("recorder sample failed")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval_sec)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.close()
            log.info("recorder stopped — %d minute row(s) written to %s",
                     self.rows_written, self.path)
